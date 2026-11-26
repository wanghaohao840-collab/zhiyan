from __future__ import annotations

import json
import logging
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

from app.database import connect
from app.qa_models import QaDeletion, QaDeletionTarget, QaValidationError
from app.note_repository import NoteRepository


logger = logging.getLogger(__name__)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _add_seconds(timestamp: str, seconds: int) -> str:
    parsed = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return (
        parsed + timedelta(seconds=seconds)
    ).astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


@dataclass(frozen=True)
class _DeletionPayload:
    deletion: QaDeletion
    conversation_ids: tuple[str, ...]
    memory_ids: tuple[str, ...]


class QaDeletionRepository:
    """Durable, user-scoped deletion fences with opaque cleanup payloads."""

    def __init__(self, db_path: Path | str, note_repository: NoteRepository | None = None, *, learning_repository=None) -> None:
        self.db_path = Path(db_path)
        self.note_repository = note_repository
        self.learning_repository = learning_repository

    def create_conversation_deletion(
        self, user_id: str, conversation_id: str, *, now: str | None = None
    ) -> QaDeletion | None:
        return self._create(
            user_id, "conversation", conversation_id, now=now
        )

    def create_document_deletion(
        self,
        user_id: str,
        document_id: str,
        affected_conversation_ids: tuple[str, ...] | None = None,
        *,
        now: str | None = None,
    ) -> QaDeletion:
        result = self._create(
            user_id,
            "document",
            document_id,
            affected_conversation_ids=affected_conversation_ids,
            now=now,
        )
        if result is None:  # document ownership is validated by the service
            raise QaValidationError("QA_DOCUMENT_NOT_FOUND", "document not found")
        return result

    def _create(
        self,
        user_id: str,
        target_type: QaDeletionTarget,
        target_id: str,
        *,
        affected_conversation_ids: tuple[str, ...] | None = None,
        now: str | None,
    ) -> QaDeletion | None:
        timestamp = now or _utc_now()
        conn = connect(self.db_path)
        try:
            conn.execute("begin immediate")
            existing = conn.execute(
                """
                select * from qa_deletion_fences
                where user_id = ? and target_type = ? and target_id = ?
                  and status != 'completed'
                """,
                (user_id, target_type, target_id),
            ).fetchone()
            restart_terminal = bool(
                existing is not None
                and existing["status"] == "failed"
                and existing["attempt_count"] >= 3
            )
            if existing is not None and not restart_terminal:
                conn.commit()
                return _deletion_from_row(existing)

            previous_conversation_ids = (
                set(json.loads(existing["conversation_ids_json"]))
                if restart_terminal
                else set()
            )
            previous_memory_ids = (
                set(json.loads(existing["memory_ids_json"]))
                if restart_terminal
                else set()
            )

            if target_type == "conversation":
                if not restart_terminal:
                    owned = conn.execute(
                        "select id from qa_conversations where id = ? and user_id = ?",
                        (target_id, user_id),
                    ).fetchone()
                    if owned is None:
                        conn.commit()
                        return None
                conversation_ids = tuple(
                    sorted(previous_conversation_ids | {target_id})
                )
            else:
                rows = conn.execute(
                    """
                    select conversation_id from qa_conversation_documents
                    where user_id = ? and document_id = ?
                    order by conversation_id
                    """,
                    (user_id, target_id),
                ).fetchall()
                discovered = tuple(row["conversation_id"] for row in rows)
                if affected_conversation_ids is not None:
                    requested = tuple(sorted(set(affected_conversation_ids)))
                    if requested != discovered:
                        raise QaValidationError(
                            "QA_DELETION_SCOPE_CHANGED",
                            "document conversation scope changed",
                        )
                conversation_ids = tuple(
                    sorted(previous_conversation_ids | set(discovered))
                )

            memory_ids = tuple(sorted(previous_memory_ids))
            if conversation_ids:
                marks = ",".join("?" for _ in conversation_ids)
                discovered_memory_ids = {
                    row["memory_id"]
                    for row in conn.execute(
                        f"""
                        select distinct memory_id from qa_messages
                        where user_id = ? and conversation_id in ({marks})
                          and memory_id is not null
                        order by memory_id
                        """,
                        (user_id, *conversation_ids),
                    ).fetchall()
                }
                memory_ids = tuple(
                    sorted(previous_memory_ids | discovered_memory_ids)
                )
                conn.execute(
                    f"""
                    update qa_jobs
                    set status = 'cancelled', stage = 'cancelled',
                        cancel_requested_at = coalesce(cancel_requested_at, ?),
                        lease_owner = null, lease_expires_at = null,
                        lease_duration_seconds = null, finished_at = ?,
                        updated_at = ?, version = version + 1
                    where user_id = ? and conversation_id in ({marks})
                      and status in ('queued','running')
                    """,
                    (timestamp, timestamp, timestamp, user_id, *conversation_ids),
                )
                conn.execute(
                    f"""
                    update qa_messages
                    set status = 'cancelled', memory_sync_status = 'not_required',
                        memory_sync_lease_owner = null,
                        memory_sync_lease_expires_at = null,
                        updated_at = ?, completed_at = ?, version = version + 1
                    where user_id = ? and conversation_id in ({marks})
                      and role = 'assistant' and status = 'pending'
                    """,
                    (timestamp, timestamp, user_id, *conversation_ids),
                )

            if restart_terminal:
                deletion_id = existing["id"]
                updated = conn.execute(
                    """
                    update qa_deletion_fences
                    set status = 'queued', stage = 'fenced',
                        affected_conversation_count = ?,
                        conversation_ids_json = ?, memory_ids_json = ?,
                        attempt_count = 0, lease_owner = null,
                        lease_expires_at = null, safe_error_code = null,
                        trace_id = null, finished_at = null, updated_at = ?
                    where id = ? and user_id = ? and target_type = ?
                      and target_id = ? and status = 'failed'
                      and attempt_count >= 3
                    """,
                    (
                        len(conversation_ids),
                        json.dumps(conversation_ids),
                        json.dumps(memory_ids),
                        timestamp,
                        deletion_id,
                        user_id,
                        target_type,
                        target_id,
                    ),
                )
                if not updated.rowcount:
                    raise QaValidationError(
                        "QA_DELETION_RETRY_CONFLICT",
                        "deletion retry state changed",
                    )
            else:
                deletion_id = str(uuid4())
                conn.execute(
                    """
                    insert into qa_deletion_fences (
                        id, user_id, target_type, target_id, status, stage,
                        affected_conversation_count, conversation_ids_json,
                        memory_ids_json, created_at, updated_at
                    ) values (?, ?, ?, ?, 'queued', 'fenced', ?, ?, ?, ?, ?)
                    """,
                    (
                        deletion_id,
                        user_id,
                        target_type,
                        target_id,
                        len(conversation_ids),
                        json.dumps(conversation_ids),
                        json.dumps(memory_ids),
                        timestamp,
                        timestamp,
                    ),
                )
            row = conn.execute(
                "select * from qa_deletion_fences where id = ?", (deletion_id,)
            ).fetchone()
            conn.commit()
            return _deletion_from_row(row)
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def has_active_fence(
        self, user_id: str, target_type: QaDeletionTarget, target_id: str
    ) -> bool:
        with connect(self.db_path) as conn:
            row = conn.execute(
                """
                select 1 from qa_deletion_fences
                where user_id = ? and target_type = ? and target_id = ?
                  and (
                      status in ('queued', 'running')
                      or (status = 'failed' and attempt_count < 3)
                  )
                """,
                (user_id, target_type, target_id),
            ).fetchone()
            return row is not None

    def get_for_target(
        self,
        user_id: str,
        target_type: QaDeletionTarget,
        target_id: str,
    ) -> QaDeletion | None:
        with connect(self.db_path) as conn:
            row = conn.execute(
                """
                select * from qa_deletion_fences
                where user_id = ? and target_type = ? and target_id = ?
                order by (status != 'completed') desc, created_at desc, id desc
                limit 1
                """,
                (user_id, target_type, target_id),
            ).fetchone()
            return _deletion_from_row(row) if row is not None else None

    def claim_next_deletion(
        self,
        worker_id: str,
        *,
        lease_seconds: int,
        now: str | None = None,
    ) -> QaDeletion | None:
        if lease_seconds < 1:
            raise QaValidationError(
                "QA_DELETION_LEASE_INVALID", "lease seconds must be positive"
            )
        timestamp = now or _utc_now()
        expires_at = _add_seconds(timestamp, lease_seconds)
        conn = connect(self.db_path)
        try:
            conn.execute("begin immediate")
            self._recover_expired_in_connection(conn, timestamp)
            candidates = conn.execute(
                """
                select id from qa_deletion_fences
                where attempt_count < 3 and status in ('queued','failed')
                order by created_at, id
                limit 1
                """
            ).fetchall()
            for candidate in candidates:
                updated = conn.execute(
                    """
                    update qa_deletion_fences
                    set status = 'running', attempt_count = attempt_count + 1,
                        lease_owner = ?, lease_expires_at = ?,
                        safe_error_code = null, trace_id = null, updated_at = ?
                    where id = ? and attempt_count < 3
                      and status in ('queued','failed')
                    """,
                    (worker_id, expires_at, timestamp, candidate["id"]),
                )
                if updated.rowcount:
                    row = conn.execute(
                        "select * from qa_deletion_fences where id = ?",
                        (candidate["id"],),
                    ).fetchone()
                    conn.commit()
                    return _deletion_from_row(row)
            conn.commit()
            return None
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def recover_expired(self, *, now: str | None = None) -> int:
        timestamp = now or _utc_now()
        conn = connect(self.db_path)
        try:
            conn.execute("begin immediate")
            recovered = self._recover_expired_in_connection(conn, timestamp)
            conn.commit()
            return recovered
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    @staticmethod
    def _recover_expired_in_connection(conn, timestamp: str) -> int:
        updated = conn.execute(
            """
            update qa_deletion_fences
            set status = 'failed', lease_owner = null,
                lease_expires_at = null,
                safe_error_code = 'QA_DELETION_INTERRUPTED', trace_id = null,
                finished_at = case
                    when attempt_count >= 3 then ? else finished_at
                end,
                updated_at = ?
            where status = 'running' and lease_expires_at <= ?
            """,
            (timestamp, timestamp, timestamp),
        )
        return updated.rowcount

    def advance_deletion(
        self,
        deletion_id: str,
        worker_id: str,
        expected_stage: str,
        next_stage: str,
        *,
        now: str | None = None,
    ) -> bool:
        timestamp = now or _utc_now()
        with connect(self.db_path) as conn:
            updated = conn.execute(
                """
                update qa_deletion_fences set stage = ?, updated_at = ?
                where id = ? and status = 'running' and stage = ?
                  and lease_owner = ? and lease_expires_at > ?
                """,
                (
                    next_stage,
                    timestamp,
                    deletion_id,
                    expected_stage,
                    worker_id,
                    timestamp,
                ),
            )
            return bool(updated.rowcount)

    def complete_deletion(
        self, deletion_id: str, worker_id: str, *, now: str | None = None
    ) -> bool:
        timestamp = now or _utc_now()
        with connect(self.db_path) as conn:
            updated = conn.execute(
                """
                update qa_deletion_fences
                set status = 'completed', stage = 'completed', lease_owner = null,
                    lease_expires_at = null, finished_at = ?, updated_at = ?
                where id = ? and status = 'running' and lease_owner = ?
                  and lease_expires_at > ?
                """,
                (timestamp, timestamp, deletion_id, worker_id, timestamp),
            )
            return bool(updated.rowcount)

    def fail_or_retry_deletion(
        self,
        deletion_id: str,
        worker_id: str,
        error_code: str,
        trace_id: str | None,
        *,
        now: str | None = None,
    ) -> QaDeletion:
        timestamp = now or _utc_now()
        conn = connect(self.db_path)
        try:
            conn.execute("begin immediate")
            updated = conn.execute(
                """
                update qa_deletion_fences
                set status = 'failed', lease_owner = null,
                    lease_expires_at = null, safe_error_code = ?, trace_id = ?,
                    finished_at = case
                        when attempt_count >= 3 then ? else finished_at
                    end,
                    updated_at = ?
                where id = ? and status = 'running' and lease_owner = ?
                  and lease_expires_at > ?
                """,
                (
                    error_code,
                    trace_id,
                    timestamp,
                    timestamp,
                    deletion_id,
                    worker_id,
                    timestamp,
                ),
            )
            if not updated.rowcount:
                raise QaValidationError(
                    "QA_DELETION_LEASE_LOST", "deletion lease was lost"
                )
            row = conn.execute(
                "select * from qa_deletion_fences where id = ?", (deletion_id,)
            ).fetchone()
            conn.commit()
            return _deletion_from_row(row)
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def get_deletion(self, user_id: str, deletion_id: str) -> QaDeletion | None:
        with connect(self.db_path) as conn:
            row = conn.execute(
                "select * from qa_deletion_fences where id = ? and user_id = ?",
                (deletion_id, user_id),
            ).fetchone()
            return _deletion_from_row(row) if row is not None else None

    def payload(self, deletion_id: str, worker_id: str) -> _DeletionPayload | None:
        timestamp = _utc_now()
        with connect(self.db_path) as conn:
            row = conn.execute(
                """
                select * from qa_deletion_fences
                where id = ? and status = 'running' and lease_owner = ?
                  and lease_expires_at > ?
                """,
                (deletion_id, worker_id, timestamp),
            ).fetchone()
            if row is None:
                return None
            return _DeletionPayload(
                _deletion_from_row(row),
                tuple(json.loads(row["conversation_ids_json"])),
                tuple(json.loads(row["memory_ids_json"])),
            )

    def remove_qa_rows(
        self, deletion_id: str, worker_id: str, *, now: str | None = None
    ) -> bool:
        timestamp = now or _utc_now()
        conn = connect(self.db_path)
        try:
            conn.execute("begin immediate")
            row = conn.execute(
                """
                select * from qa_deletion_fences
                where id = ? and status = 'running' and stage = 'fenced'
                  and lease_owner = ? and lease_expires_at > ?
                """,
                (deletion_id, worker_id, timestamp),
            ).fetchone()
            if row is None:
                conn.commit()
                return False
            conversation_ids = tuple(json.loads(row["conversation_ids_json"]))
            if row["target_type"] == "document" and self.learning_repository is not None:
                self.learning_repository.delete_document_in_transaction(
                    conn, user_id=row["user_id"], document_id=row["target_id"]
                )
            if self.note_repository is not None:
                if row["target_type"] == "document":
                    self.note_repository.scrub_sources_in_transaction(
                        conn, user_id=row["user_id"], document_id=row["target_id"], deleted_at=timestamp
                    )
                else:
                    self.note_repository.scrub_sources_in_transaction(
                        conn, user_id=row["user_id"], thread_id=row["target_id"], deleted_at=timestamp
                    )
            if conversation_ids:
                marks = ",".join("?" for _ in conversation_ids)
                conn.execute(
                    f"""
                    delete from qa_conversations
                    where user_id = ? and id in ({marks})
                    """,
                    (row["user_id"], *conversation_ids),
                )
            conn.execute(
                """
                update qa_deletion_fences set stage = 'qa_rows_removed', updated_at = ?
                where id = ? and status = 'running' and stage = 'fenced'
                  and lease_owner = ? and lease_expires_at > ?
                """,
                (timestamp, deletion_id, worker_id, timestamp),
            )
            conn.commit()
            return True
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()


class QaDeletionService:
    def __init__(
        self,
        session_registry,
        document_library,
        qa_repository,
        deletion_repository: QaDeletionRepository,
        worker_wake,
    ) -> None:
        self.session_registry = session_registry
        self.document_library = document_library
        self.qa_repository = qa_repository
        self.deletion_repository = deletion_repository
        self.worker_wake = worker_wake

    def request_conversation(
        self, session_token: str, conversation_id: str
    ) -> QaDeletion | None:
        session = self.session_registry.get_session(session_token)
        user_id = str(session.user_id)
        existing = self.deletion_repository.get_for_target(
            user_id, "conversation", conversation_id
        )
        if existing is not None:
            if existing.status == "failed" and existing.attempt_count >= 3:
                deletion = self.deletion_repository.create_conversation_deletion(
                    user_id, conversation_id
                )
                if deletion is not None:
                    self.worker_wake.notify()
                return deletion
            if existing.status != "completed":
                self.worker_wake.notify()
            return existing
        if self.qa_repository.get_conversation(user_id, conversation_id) is None:
            return None
        deletion = self.deletion_repository.create_conversation_deletion(
            user_id, conversation_id
        )
        if deletion is not None:
            self.worker_wake.notify()
        return deletion

    def request_document(
        self, session_token: str, document_id: str
    ) -> QaDeletion | None:
        session = self.session_registry.get_session(session_token)
        user_id = str(session.user_id)
        existing = self.deletion_repository.get_for_target(
            user_id, "document", document_id
        )
        if existing is not None:
            if existing.status == "failed" and existing.attempt_count >= 3:
                deletion = self.deletion_repository.create_document_deletion(
                    user_id, document_id
                )
                self.worker_wake.notify()
                return deletion
            if existing.status != "completed":
                self.worker_wake.notify()
            return existing
        if hasattr(self.document_library, "has_document"):
            owned = self.document_library.has_document(user_id, document_id)
        else:
            owned = any(
                item.document_id == document_id
                for item in self.document_library.list_documents(session_token)
            )
        if not owned:
            return None
        deletion = self.deletion_repository.create_document_deletion(
            user_id, document_id
        )
        self.worker_wake.notify()
        return deletion

    def get_deletion(
        self, session_token: str, deletion_id: str
    ) -> QaDeletion | None:
        session = self.session_registry.get_session(session_token)
        return self.deletion_repository.get_deletion(
            str(session.user_id), deletion_id
        )


class QaDeletionWorker:
    def __init__(
        self,
        repository: QaDeletionRepository,
        runtime_registry,
        document_library,
        session_registry,
        *,
        legacy_migration=None,
        graph_service=None,
        lease_seconds: int = 300,
        poll_interval: float = 0.5,
    ) -> None:
        if lease_seconds < 1 or poll_interval <= 0:
            raise ValueError("invalid QA deletion worker configuration")
        self.repository = repository
        self.runtime_registry = runtime_registry
        self.document_library = document_library
        self.session_registry = session_registry
        self.legacy_migration = legacy_migration
        self.graph_service = graph_service
        self.lease_seconds = lease_seconds
        self.poll_interval = poll_interval
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self.repository.recover_expired()
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._loop,
            name="qa-deletion-worker",
            daemon=False,
        )
        self._thread.start()
        self.notify()

    def stop(self, wait: bool = True) -> None:
        self._stop.set()
        self._wake.set()
        if wait and self._thread is not None:
            self._thread.join()

    def notify(self) -> None:
        self._wake.set()

    def _loop(self) -> None:
        worker_id = f"qa-deletion-worker-{uuid4()}"
        while not self._stop.is_set():
            try:
                if self.run_once(worker_id):
                    continue
            except Exception:
                logger.exception("QA deletion worker iteration failed")
            self._wake.wait(self.poll_interval)
            self._wake.clear()

    def run_once(self, worker_id: str) -> bool:
        deletion = self.repository.claim_next_deletion(
            worker_id, lease_seconds=self.lease_seconds
        )
        if deletion is None:
            return False
        runtime = None
        try:
            if deletion.stage == "fenced":
                if not self.repository.remove_qa_rows(deletion.id, worker_id):
                    return True
                deletion = self.repository.payload(deletion.id, worker_id).deletion

            payload = self.repository.payload(deletion.id, worker_id)
            if payload is None:
                return True
            deletion = payload.deletion
            needs_runtime = (
                bool(payload.memory_ids)
                or deletion.target_type == "document"
                or self.legacy_migration is not None
            )
            if needs_runtime:
                runtime = self.runtime_registry.acquire_background(deletion.user_id)

            if deletion.stage == "qa_rows_removed":
                if self.legacy_migration is not None:
                    self.legacy_migration.scrub_conversations(
                        deletion.user_id,
                        runtime.history,
                        payload.conversation_ids,
                    )
                if runtime is not None:
                    manager = runtime.memory_tool.memory_manager
                    for memory_id in payload.memory_ids:
                        manager.remove_memory(memory_id, memory_type="episodic")
                if not self.repository.advance_deletion(
                    deletion.id,
                    worker_id,
                    "qa_rows_removed",
                    "memory_removed",
                ):
                    return True
                deletion = self.repository.payload(deletion.id, worker_id).deletion

            if deletion.target_type == "conversation":
                self.repository.complete_deletion(deletion.id, worker_id)
                return True

            if deletion.stage == "memory_removed":
                if self.graph_service is not None:
                    self.graph_service.delete_document(deletion.user_id, deletion.target_id)
                self.document_library.perform_document_delete(
                    deletion.user_id,
                    runtime,
                    deletion.target_id,
                    replay_if_missing=True,
                )
                if self.legacy_migration is not None:
                    self.legacy_migration.scrub_document(
                        deletion.user_id,
                        runtime.history,
                        deletion.target_id,
                    )
                self.session_registry.clear_document_selection(
                    deletion.user_id, deletion.target_id
                )
                if not self.repository.advance_deletion(
                    deletion.id,
                    worker_id,
                    "memory_removed",
                    "document_removed",
                ):
                    return True
            self.repository.complete_deletion(deletion.id, worker_id)
            return True
        except Exception:
            self.repository.fail_or_retry_deletion(
                deletion.id,
                worker_id,
                "QA_DELETION_FAILED",
                str(uuid4()),
            )
            return True
        finally:
            if runtime is not None:
                self.runtime_registry.release_background(deletion.user_id)


def _deletion_from_row(row) -> QaDeletion:
    return QaDeletion(
        id=row["id"],
        user_id=row["user_id"],
        target_type=row["target_type"],
        target_id=row["target_id"],
        status=row["status"],
        stage=row["stage"],
        affected_conversation_count=row["affected_conversation_count"],
        attempt_count=row["attempt_count"],
        safe_error_code=row["safe_error_code"],
        trace_id=row["trace_id"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )
