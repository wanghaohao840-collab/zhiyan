from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Sequence
from uuid import uuid4

from app.database import connect
from app.qa_models import (
    QaConflictError,
    QaJob,
    QaSourceDraft,
    QaValidationError,
    SummaryEnqueueResult,
)
from app.qa_repository import QaRepository, _not_fenced_clause, _utc_now


# The UTC suffix "+" sorts before fractional seconds, unlike "Z".
# Accept both existing formats without changing the stored lease.
class QaJobRepository:
    def __init__(self, db_path: Path | str):
        self.db_path = Path(db_path)
        self.qa_repository = QaRepository(self.db_path)

    def create_summary_turn_and_job(
        self,
        user_id: str,
        conversation_id: str,
        question: str,
        client_request_id: str,
        *,
        max_attempts: int = 3,
        now: str | None = None,
    ) -> SummaryEnqueueResult:
        if max_attempts < 1:
            raise QaValidationError(
                "QA_JOB_MAX_ATTEMPTS_INVALID", "max attempts must be positive"
            )
        timestamp = now or _utc_now()
        conn = connect(self.db_path)
        try:
            conn.execute("begin immediate")
            pending = self.qa_repository._create_pending_turn_in_connection(
                conn,
                user_id,
                conversation_id,
                question,
                "summary",
                client_request_id,
                retry_of_message_id=None,
                timestamp=timestamp,
            )
            existing = conn.execute(
                """
                select * from qa_jobs
                where user_id = ? and input_message_id = ?
                """,
                (user_id, pending.user_message.id),
            ).fetchone()
            if existing is not None:
                conn.commit()
                return SummaryEnqueueResult(
                    pending, _job_from_row(existing), True
                )

            job_id = str(uuid4())
            try:
                conn.execute(
                    """
                    insert into qa_jobs (
                        id, conversation_id, user_id, input_message_id,
                        assistant_message_id, status, stage, progress,
                        max_attempts, created_at, updated_at
                    ) values (
                        ?, ?, ?, ?, ?, 'queued', 'queued', 0, ?, ?, ?
                    )
                    """,
                    (
                        job_id,
                        conversation_id,
                        user_id,
                        pending.user_message.id,
                        pending.assistant_message.id,
                        max_attempts,
                        timestamp,
                        timestamp,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise QaConflictError("QA_CONVERSATION_BUSY") from exc
            row = conn.execute(
                "select * from qa_jobs where id = ?", (job_id,)
            ).fetchone()
            conn.commit()
            return SummaryEnqueueResult(pending, _job_from_row(row), False)
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def claim_next(
        self,
        worker_id: str,
        *,
        lease_seconds: int,
        now: str | None = None,
    ) -> QaJob | None:
        if lease_seconds < 1:
            raise QaValidationError(
                "QA_JOB_LEASE_INVALID", "lease seconds must be positive"
            )
        conn = connect(self.db_path)
        try:
            conn.execute("begin immediate")
            timestamp = _lease_timestamp(now)
            expires_at = _add_seconds(timestamp, lease_seconds)
            self._recover_expired_in_connection(conn, timestamp)
            candidates = conn.execute(
                f"""
                select id, status, version from qa_jobs
                where attempt_count < max_attempts
                  and exists (
                      select 1 from qa_conversations
                      where qa_conversations.id = qa_jobs.conversation_id
                        and qa_conversations.user_id = qa_jobs.user_id
                        and {_not_fenced_clause('qa_conversations')}
                  )
                  and status = 'queued'
                order by created_at, id
                """
            ).fetchall()
            for candidate in candidates:
                updated = conn.execute(
                    f"""
                    update qa_jobs
                    set status = 'running', stage = 'starting',
                        attempt_count = attempt_count + 1,
                        lease_owner = ?, lease_expires_at = ?,
                        lease_duration_seconds = ?,
                        started_at = coalesce(started_at, ?), updated_at = ?,
                        version = version + 1
                    where id = ? and version = ?
                      and attempt_count < max_attempts
                      and exists (
                          select 1 from qa_conversations
                          where qa_conversations.id = qa_jobs.conversation_id
                            and qa_conversations.user_id = qa_jobs.user_id
                            and {_not_fenced_clause('qa_conversations')}
                      )
                      and status = 'queued'
                    """,
                    (
                        worker_id,
                        expires_at,
                        lease_seconds,
                        timestamp,
                        timestamp,
                        candidate["id"],
                        candidate["version"],
                    ),
                )
                if updated.rowcount:
                    row = conn.execute(
                        "select * from qa_jobs where id = ?", (candidate["id"],)
                    ).fetchone()
                    conn.commit()
                    return _job_from_row(row)
            conn.commit()
            return None
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def heartbeat(
        self,
        job_id: str,
        worker_id: str,
        *,
        progress: int | None = None,
        stage: str | None = None,
        now: str | None = None,
        attempt: int | None = None,
    ) -> bool:
        if progress is not None and not 0 <= progress <= 100:
            raise QaValidationError(
                "QA_JOB_PROGRESS_INVALID", "progress must be between 0 and 100"
            )
        conn = connect(self.db_path)
        try:
            if progress is None and stage is None:
                # Periodic renewal retries contention within a single lease.
                conn.execute("pragma busy_timeout = 100")
            conn.execute("begin immediate")
            timestamp = _lease_timestamp(now)
            row = conn.execute(
                """
                select lease_duration_seconds from qa_jobs
                where id = ? and status = 'running' and lease_owner = ?
                  and replace(lease_expires_at, 'Z', '+00:00') > replace(?, 'Z', '+00:00')
                  and cancel_requested_at is null
                  and (? is null or attempt_count = ?)
                """,
                (job_id, worker_id, timestamp, attempt, attempt),
            ).fetchone()
            duration = row["lease_duration_seconds"] if row is not None else None
            if not isinstance(duration, int) or duration < 1:
                conn.commit()
                return False
            expires_at = _add_seconds(timestamp, duration)
            updated = conn.execute(
                """
                update qa_jobs
                set progress = coalesce(?, progress), stage = coalesce(?, stage),
                    lease_expires_at = ?,
                    updated_at = ?, version = version + 1
                where id = ? and status = 'running' and lease_owner = ?
                  and replace(lease_expires_at, 'Z', '+00:00') > replace(?, 'Z', '+00:00')
                  and cancel_requested_at is null
                """,
                (
                    progress,
                    str(stage) if stage is not None else None,
                    expires_at,
                    timestamp,
                    job_id,
                    worker_id,
                    timestamp,
                ),
            )
            conn.commit()
            return bool(updated.rowcount)
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def request_cancel(
        self,
        user_id: str,
        job_id: str,
        *,
        now: str | None = None,
    ) -> QaJob | None:
        conn = connect(self.db_path)
        try:
            conn.execute("begin immediate")
            timestamp = _lease_timestamp(now)
            row = conn.execute(
                "select * from qa_jobs where id = ? and user_id = ?",
                (job_id, user_id),
            ).fetchone()
            if row is None:
                conn.commit()
                return None
            if row["status"] == "queued":
                conn.execute(
                    """
                    update qa_jobs
                    set status = 'cancelled', stage = 'cancelled',
                        cancel_requested_at = ?, finished_at = ?, updated_at = ?,
                        version = version + 1
                    where id = ? and user_id = ? and status = 'queued'
                    """,
                    (timestamp, timestamp, timestamp, job_id, user_id),
                )
                self._finish_assistant(
                    conn,
                    row["assistant_message_id"],
                    user_id,
                    "cancelled",
                    None,
                    None,
                    timestamp,
                )
            elif row["status"] == "running" and row["cancel_requested_at"] is None:
                conn.execute(
                    """
                    update qa_jobs
                    set cancel_requested_at = ?, updated_at = ?, version = version + 1
                    where id = ? and user_id = ? and status = 'running'
                      and cancel_requested_at is null
                    """,
                    (timestamp, timestamp, job_id, user_id),
                )
            updated_row = conn.execute(
                "select * from qa_jobs where id = ? and user_id = ?",
                (job_id, user_id),
            ).fetchone()
            conn.commit()
            return _job_from_row(updated_row)
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def complete(
        self,
        job_id: str,
        worker_id: str,
        *,
        now: str | None = None,
        attempt: int | None = None,
    ) -> bool:
        conn = connect(self.db_path)
        try:
            conn.execute("begin immediate")
            timestamp = _lease_timestamp(now)
            row = conn.execute(
                "select * from qa_jobs where id = ?", (job_id,)
            ).fetchone()
            if not self._owns_live_lease(row, worker_id, timestamp, attempt):
                conn.commit()
                return False
            if row["cancel_requested_at"] is not None:
                self._cancel_running(conn, row, timestamp)
                conn.commit()
                return False
            updated = conn.execute(
                """
                update qa_jobs
                set status = 'completed', stage = 'completed', progress = 100,
                    lease_owner = null, lease_expires_at = null,
                    lease_duration_seconds = null, finished_at = ?, updated_at = ?,
                    version = version + 1
                where id = ? and status = 'running' and lease_owner = ?
                  and replace(lease_expires_at, 'Z', '+00:00') > replace(?, 'Z', '+00:00')
                  and cancel_requested_at is null
                  and exists (
                      select 1 from qa_messages
                      where id = qa_jobs.assistant_message_id
                        and user_id = qa_jobs.user_id
                        and status = 'completed'
                  )
                """,
                (timestamp, timestamp, job_id, worker_id, timestamp),
            )
            conn.commit()
            return bool(updated.rowcount)
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def commit_answer(
        self,
        job_id: str,
        worker_id: str,
        expected_message_version: int,
        answer: str,
        sources: Sequence[QaSourceDraft],
        source_state: str,
        *,
        now: str | None = None,
        attempt: int | None = None,
        stop_requested: Callable[[], bool] | None = None,
    ) -> bool:
        """Atomically commit a summary answer and its owning durable job."""

        if source_state not in {"available", "none", "legacy_unavailable"}:
            raise QaValidationError("QA_SOURCE_STATE_INVALID", "invalid source state")
        source_list = tuple(sources)
        conn = connect(self.db_path)
        try:
            conn.execute("begin immediate")
            if stop_requested is not None and stop_requested():
                conn.rollback()
                return False
            timestamp = _lease_timestamp(now)
            job = conn.execute(
                "select * from qa_jobs where id = ?", (job_id,)
            ).fetchone()
            if not self._owns_live_lease(job, worker_id, timestamp, attempt):
                conn.commit()
                return False
            if job["cancel_requested_at"] is not None:
                self._cancel_running(conn, job, timestamp)
                conn.commit()
                return False
            message = conn.execute(
                """
                select * from qa_messages
                where id = ? and user_id = ? and conversation_id = ?
                  and role = 'assistant' and status = 'pending' and version = ?
                """,
                (
                    job["assistant_message_id"],
                    job["user_id"],
                    job["conversation_id"],
                    expected_message_version,
                ),
            ).fetchone()
            if message is None:
                conn.commit()
                return False
            self.qa_repository._validate_source_scope(
                conn, job["user_id"], job["conversation_id"], source_list
            )
            conn.execute(
                """
                update qa_messages
                set status = 'completed', content = ?, source_state = ?,
                    memory_sync_status = 'pending', version = version + 1,
                    updated_at = ?, completed_at = ?
                where id = ? and user_id = ? and status = 'pending'
                  and version = ?
                """,
                (
                    str(answer),
                    source_state,
                    timestamp,
                    timestamp,
                    message["id"],
                    job["user_id"],
                    expected_message_version,
                ),
            )
            conn.executemany(
                """
                insert into qa_message_sources (
                    id, assistant_message_id, conversation_id, user_id,
                    position, citation_id, document_id, document_name,
                    page_number, section, excerpt, reference, truncated,
                    source_type, chunk_id
                ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        str(uuid4()),
                        message["id"],
                        job["conversation_id"],
                        job["user_id"],
                        position,
                        source.citation_id,
                        source.document_id,
                        source.document_name,
                        source.page_number,
                        source.section,
                        source.excerpt,
                        source.reference,
                        int(source.truncated),
                        source.source_type,
                        source.chunk_id,
                    )
                    for position, source in enumerate(source_list)
                ],
            )
            self.qa_repository._touch_conversation(
                conn, job["user_id"], job["conversation_id"], timestamp
            )
            completed = conn.execute(
                """
                update qa_jobs
                set status = 'completed', stage = 'completed', progress = 100,
                    lease_owner = null, lease_expires_at = null,
                    lease_duration_seconds = null, finished_at = ?, updated_at = ?,
                    version = version + 1
                where id = ? and status = 'running' and lease_owner = ?
                  and replace(lease_expires_at, 'Z', '+00:00') > replace(?, 'Z', '+00:00')
                  and cancel_requested_at is null
                """,
                (timestamp, timestamp, job_id, worker_id, timestamp),
            )
            if not completed.rowcount:
                conn.rollback()
                return False
            conn.commit()
            return True
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def fail_or_retry(
        self,
        job_id: str,
        worker_id: str,
        error_code: str,
        trace_id: str | None,
        *,
        retryable: bool = True,
        now: str | None = None,
        attempt: int | None = None,
    ) -> QaJob:
        conn = connect(self.db_path)
        try:
            conn.execute("begin immediate")
            timestamp = _lease_timestamp(now)
            row = conn.execute(
                "select * from qa_jobs where id = ?", (job_id,)
            ).fetchone()
            if not self._owns_live_lease(row, worker_id, timestamp, attempt):
                raise QaConflictError("QA_JOB_LEASE_LOST")
            if row["cancel_requested_at"] is not None:
                self._cancel_running(conn, row, timestamp)
            elif retryable and row["attempt_count"] < row["max_attempts"]:
                conn.execute(
                    """
                    update qa_jobs
                    set status = 'queued', stage = 'queued', progress = 0,
                        lease_owner = null, lease_expires_at = null,
                        lease_duration_seconds = null, safe_error_code = ?,
                        trace_id = ?, updated_at = ?, version = version + 1
                    where id = ? and status = 'running' and lease_owner = ?
                      and replace(lease_expires_at, 'Z', '+00:00') > replace(?, 'Z', '+00:00')
                    """,
                    (error_code, trace_id, timestamp, job_id, worker_id, timestamp),
                )
            else:
                updated = conn.execute(
                    """
                    update qa_jobs
                    set status = 'failed', stage = 'failed',
                        lease_owner = null, lease_expires_at = null,
                        lease_duration_seconds = null, safe_error_code = ?,
                        trace_id = ?, finished_at = ?, updated_at = ?,
                        version = version + 1
                    where id = ? and status = 'running' and lease_owner = ?
                      and replace(lease_expires_at, 'Z', '+00:00') > replace(?, 'Z', '+00:00')
                    """,
                    (
                        error_code,
                        trace_id,
                        timestamp,
                        timestamp,
                        job_id,
                        worker_id,
                        timestamp,
                    ),
                )
                if not updated.rowcount:
                    raise QaConflictError("QA_JOB_LEASE_LOST")
                self._finish_assistant(
                    conn,
                    row["assistant_message_id"],
                    row["user_id"],
                    "failed",
                    error_code,
                    trace_id,
                    timestamp,
                )
            updated_row = conn.execute(
                "select * from qa_jobs where id = ?", (job_id,)
            ).fetchone()
            conn.commit()
            return _job_from_row(updated_row)
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def recover_expired(self, *, now: str | None = None) -> int:
        conn = connect(self.db_path)
        try:
            conn.execute("begin immediate")
            timestamp = _lease_timestamp(now)
            recovered = self._recover_expired_in_connection(conn, timestamp)
            conn.commit()
            return recovered
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _recover_expired_in_connection(
        self, conn: sqlite3.Connection, timestamp: str
    ) -> int:
        rows = conn.execute(
            """
            select * from qa_jobs
            where status = 'running' and replace(lease_expires_at, 'Z', '+00:00') <= replace(?, 'Z', '+00:00')
            order by created_at, id
            """,
            (timestamp,),
        ).fetchall()
        for row in rows:
            if row["cancel_requested_at"] is not None:
                self._cancel_running(conn, row, timestamp)
            elif row["attempt_count"] < row["max_attempts"]:
                conn.execute(
                    """
                    update qa_jobs
                    set status = 'queued', stage = 'queued', progress = 0,
                        lease_owner = null, lease_expires_at = null,
                        lease_duration_seconds = null, updated_at = ?,
                        version = version + 1
                    where id = ? and status = 'running'
                      and replace(lease_expires_at, 'Z', '+00:00') <= replace(?, 'Z', '+00:00')
                    """,
                    (timestamp, row["id"], timestamp),
                )
            else:
                conn.execute(
                    """
                    update qa_jobs
                    set status = 'failed', stage = 'failed',
                        lease_owner = null, lease_expires_at = null,
                        lease_duration_seconds = null,
                        safe_error_code = 'QA_JOB_INTERRUPTED',
                        trace_id = null, finished_at = ?, updated_at = ?,
                        version = version + 1
                    where id = ? and status = 'running'
                      and replace(lease_expires_at, 'Z', '+00:00') <= replace(?, 'Z', '+00:00')
                    """,
                    (timestamp, timestamp, row["id"], timestamp),
                )
                self._finish_assistant(
                    conn,
                    row["assistant_message_id"],
                    row["user_id"],
                    "failed",
                    "QA_JOB_INTERRUPTED",
                    None,
                    timestamp,
                )
        return len(rows)

    def get(self, user_id: str, job_id: str) -> QaJob | None:
        with connect(self.db_path) as conn:
            row = conn.execute(
                "select * from qa_jobs where id = ? and user_id = ?",
                (job_id, user_id),
            ).fetchone()
            return _job_from_row(row) if row is not None else None

    def get_active_for_conversation(
        self, user_id: str, conversation_id: str
    ) -> QaJob | None:
        with connect(self.db_path) as conn:
            row = conn.execute(
                f"""
                select qa_jobs.*
                from qa_jobs
                join qa_conversations
                  on qa_conversations.id = qa_jobs.conversation_id
                 and qa_conversations.user_id = qa_jobs.user_id
                where qa_jobs.user_id = ?
                  and qa_jobs.conversation_id = ?
                  and qa_jobs.status in ('queued', 'running')
                  and {_not_fenced_clause('qa_conversations')}
                order by qa_jobs.created_at desc, qa_jobs.id desc
                limit 1
                """,
                (user_id, conversation_id),
            ).fetchone()
            return _job_from_row(row) if row is not None else None

    @staticmethod
    def _owns_live_lease(
        row: sqlite3.Row | None, worker_id: str, timestamp: str, attempt: int | None
    ) -> bool:
        return bool(
            row is not None
            and row["status"] == "running"
            and row["lease_owner"] == worker_id
            and (attempt is None or row["attempt_count"] == attempt)
            and row["lease_expires_at"] is not None
            and _parse_lease_timestamp(row["lease_expires_at"]) > _parse_lease_timestamp(timestamp)
        )

    def _cancel_running(
        self, conn: sqlite3.Connection, row: sqlite3.Row, timestamp: str
    ) -> None:
        conn.execute(
            """
            update qa_jobs
            set status = 'cancelled', stage = 'cancelled',
                lease_owner = null, lease_expires_at = null,
                lease_duration_seconds = null, finished_at = ?, updated_at = ?,
                version = version + 1
            where id = ? and status = 'running'
            """,
            (timestamp, timestamp, row["id"]),
        )
        self._finish_assistant(
            conn,
            row["assistant_message_id"],
            row["user_id"],
            "cancelled",
            None,
            None,
            timestamp,
        )

    @staticmethod
    def _finish_assistant(
        conn: sqlite3.Connection,
        assistant_message_id: str,
        user_id: str,
        status: str,
        error_code: str | None,
        trace_id: str | None,
        timestamp: str,
    ) -> None:
        conn.execute(
            """
            update qa_messages
            set status = ?, safe_error_code = ?, trace_id = ?,
                memory_sync_status = 'not_required', version = version + 1,
                completed_at = ?, updated_at = ?
            where id = ? and user_id = ? and role = 'assistant'
              and status = 'pending'
            """,
            (
                status,
                error_code,
                trace_id,
                timestamp,
                timestamp,
                assistant_message_id,
                user_id,
            ),
        )


def _parse_lease_timestamp(timestamp: str) -> datetime:
    value = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


def _lease_timestamp(timestamp: str | None) -> str:
    if timestamp is None:
        return _utc_now()
    value = _parse_lease_timestamp(timestamp).astimezone(timezone.utc)
    return value.isoformat().replace("+00:00", "Z")


def _add_seconds(timestamp: str, seconds: int) -> str:
    parsed = _parse_lease_timestamp(timestamp)
    return (parsed + timedelta(seconds=seconds)).astimezone(timezone.utc).isoformat().replace(
        "+00:00", "Z"
    )


def _job_from_row(row: sqlite3.Row) -> QaJob:
    return QaJob(
        id=row["id"],
        conversation_id=row["conversation_id"],
        user_id=row["user_id"],
        input_message_id=row["input_message_id"],
        assistant_message_id=row["assistant_message_id"],
        status=row["status"],
        stage=row["stage"],
        progress=row["progress"],
        cancel_requested_at=row["cancel_requested_at"],
        attempt_count=row["attempt_count"],
        max_attempts=row["max_attempts"],
        lease_owner=row["lease_owner"],
        lease_expires_at=row["lease_expires_at"],
        lease_duration_seconds=row["lease_duration_seconds"],
        safe_error_code=row["safe_error_code"],
        trace_id=row["trace_id"],
        version=row["version"],
        created_at=row["created_at"],
        started_at=row["started_at"],
        finished_at=row["finished_at"],
        updated_at=row["updated_at"],
    )
