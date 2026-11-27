from __future__ import annotations

import sqlite3
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Sequence
from uuid import uuid4

from app.database import connect
from app.qa_models import (
    PendingTurn,
    QaConflictError,
    QaConversation,
    QaConversationAggregate,
    QaConversationDocument,
    QaConversationPage,
    QaDocumentCandidate,
    QaMessage,
    QaMessagePage,
    QaReportTurn,
    QaSource,
    QaSourceDraft,
    QaValidationError,
    conversation_title,
    decode_cursor,
    encode_cursor,
    normalize_question,
    validate_document_candidates,
    validate_mode,
)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


class QaRepository:
    def __init__(self, db_path: Path | str):
        self.db_path = Path(db_path)

    def create_conversation(
        self,
        user_id: str,
        documents: Sequence[QaDocumentCandidate],
        *,
        origin: str = "product",
        now: str | None = None,
    ) -> QaConversationAggregate:
        if origin not in {"product", "legacy_json", "legacy_gradio"}:
            raise QaValidationError("QA_ORIGIN_INVALID", "unsupported QA origin")
        scope = validate_document_candidates(user_id, documents)
        timestamp = now or _utc_now()
        conversation_id = str(uuid4())
        conn = connect(self.db_path)
        try:
            conn.execute("begin immediate")
            fenced_document = conn.execute(
                f"""
                select 1 from qa_deletion_fences
                where user_id = ? and target_type = 'document'
                  and (
                      status in ('queued', 'running')
                      or (status = 'failed' and attempt_count < 3)
                  )
                  and target_id in ({','.join('?' for _ in scope)})
                limit 1
                """,
                (user_id, *(item.document_id for item in scope)),
            ).fetchone()
            if fenced_document is not None:
                raise QaValidationError(
                    "QA_DOCUMENT_DELETING", "a selected document is being deleted"
                )
            conn.execute(
                """
                insert into qa_conversations (
                    id, user_id, title, origin, created_at, updated_at,
                    last_message_at
                ) values (?, ?, '新对话', ?, ?, ?, ?)
                """,
                (
                    conversation_id,
                    user_id,
                    origin,
                    timestamp,
                    timestamp,
                    timestamp,
                ),
            )
            conn.executemany(
                """
                insert into qa_conversation_documents (
                    conversation_id, user_id, document_id, document_name, position
                ) values (?, ?, ?, ?, ?)
                """,
                [
                    (
                        conversation_id,
                        user_id,
                        item.document_id,
                        item.document_name,
                        item.position,
                    )
                    for item in scope
                ],
            )
            created = self._get_conversation(conn, user_id, conversation_id)
            conn.commit()
            return created
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def list_conversations(
        self,
        user_id: str,
        *,
        cursor: str | None = None,
        limit: int = 20,
    ) -> QaConversationPage:
        page_size = _validated_limit(limit, maximum=100)
        params: list[object] = [user_id]
        cursor_clause = ""
        if cursor:
            timestamp, conversation_id = decode_cursor(cursor)
            cursor_clause = (
                "and (last_message_at < ? or "
                "(last_message_at = ? and id < ?))"
            )
            params.extend((timestamp, timestamp, conversation_id))
        params.append(page_size + 1)
        with connect(self.db_path) as conn:
            rows = conn.execute(
                f"""
                select * from qa_conversations
                where user_id = ?
                  and {_not_fenced_clause('qa_conversations')}
                  {cursor_clause}
                order by last_message_at desc, id desc
                limit ?
                """,
                params,
            ).fetchall()
            has_more = len(rows) > page_size
            rows = rows[:page_size]
            items = tuple(
                self._aggregate_from_row(conn, row)
                for row in rows
            )
        next_cursor = None
        if has_more and items:
            last = items[-1].conversation
            next_cursor = encode_cursor(last.last_message_at, last.id)
        return QaConversationPage(items, next_cursor)

    def get_conversation(
        self, user_id: str, conversation_id: str
    ) -> QaConversationAggregate | None:
        with connect(self.db_path) as conn:
            return self._get_conversation(conn, user_id, conversation_id)

    def list_messages(
        self,
        user_id: str,
        conversation_id: str,
        *,
        cursor: str | None = None,
        limit: int = 50,
    ) -> QaMessagePage:
        page_size = _validated_limit(limit, maximum=200)
        params: list[object] = [user_id, conversation_id]
        cursor_clause = ""
        if cursor:
            timestamp, message_id = decode_cursor(cursor)
            cursor_clause = (
                "and (created_at > ? or (created_at = ? and id > ?))"
            )
            params.extend((timestamp, timestamp, message_id))
        params.append(page_size + 1)
        with connect(self.db_path) as conn:
            rows = conn.execute(
                f"""
                select * from qa_messages
                where user_id = ? and conversation_id = ?
                  and exists (
                      select 1 from qa_conversations
                      where qa_conversations.id = qa_messages.conversation_id
                        and qa_conversations.user_id = qa_messages.user_id
                        and {_not_fenced_clause('qa_conversations')}
                  )
                  {cursor_clause}
                order by created_at, id
                limit ?
                """,
                params,
            ).fetchall()
            has_more = len(rows) > page_size
            rows = rows[:page_size]
            items = tuple(self._message_from_row(conn, row) for row in rows)
        next_cursor = None
        if has_more and items:
            last = items[-1]
            next_cursor = encode_cursor(last.created_at, last.id)
        return QaMessagePage(items, next_cursor)

    def list_recent_messages(
        self,
        user_id: str,
        conversation_id: str,
        *,
        cursor: str | None = None,
        limit: int = 50,
    ) -> QaMessagePage:
        page_size = _validated_limit(limit, maximum=200)
        params: list[object] = [user_id, conversation_id]
        cursor_clause = ""
        if cursor:
            timestamp, message_id = decode_cursor(cursor)
            cursor_clause = (
                "and (created_at < ? or (created_at = ? and id < ?))"
            )
            params.extend((timestamp, timestamp, message_id))
        params.append(page_size + 1)
        with connect(self.db_path) as conn:
            rows = conn.execute(
                f"""
                select * from qa_messages
                where user_id = ? and conversation_id = ?
                  and exists (
                      select 1 from qa_conversations
                      where qa_conversations.id = qa_messages.conversation_id
                        and qa_conversations.user_id = qa_messages.user_id
                        and {_not_fenced_clause('qa_conversations')}
                  )
                  {cursor_clause}
                order by created_at desc, id desc
                limit ?
                """,
                params,
            ).fetchall()
            has_more = len(rows) > page_size
            page_rows = rows[:page_size]
            items = tuple(
                self._message_from_row(conn, row)
                for row in reversed(page_rows)
            )
        next_cursor = None
        if has_more and page_rows:
            oldest = page_rows[-1]
            next_cursor = encode_cursor(oldest["created_at"], oldest["id"])
        return QaMessagePage(items, next_cursor)

    def get_message(self, user_id: str, message_id: str) -> QaMessage | None:
        with connect(self.db_path) as conn:
            row = conn.execute(
                f"""
                select * from qa_messages where id = ? and user_id = ?
                  and exists (
                      select 1 from qa_conversations
                      where qa_conversations.id = qa_messages.conversation_id
                        and qa_conversations.user_id = qa_messages.user_id
                        and {_not_fenced_clause('qa_conversations')}
                  )
                """,
                (message_id, user_id),
            ).fetchone()
            return self._message_from_row(conn, row) if row is not None else None

    def create_pending_turn(
        self,
        user_id: str,
        conversation_id: str,
        question: str,
        mode: str,
        client_request_id: str,
        *,
        admit_new_turn: Callable[[], None] | None = None,
        now: str | None = None,
    ) -> PendingTurn:
        timestamp = now or _utc_now()
        conn = connect(self.db_path)
        try:
            conn.execute("begin immediate")
            result = self._create_pending_turn_in_connection(
                conn,
                user_id,
                conversation_id,
                question,
                mode,
                client_request_id,
                retry_of_message_id=None,
                timestamp=timestamp,
                admit_new_turn=admit_new_turn,
            )
            conn.commit()
            return result
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def create_pending_retry(
        self,
        user_id: str,
        conversation_id: str,
        failed_assistant_message_id: str,
        client_request_id: str,
        *,
        admit_new_turn: Callable[[], None] | None = None,
        now: str | None = None,
    ) -> PendingTurn:
        request_id = str(client_request_id or "").strip()
        if not request_id:
            raise QaValidationError(
                "QA_CLIENT_REQUEST_REQUIRED", "client request id is required"
            )
        timestamp = now or _utc_now()
        conn = connect(self.db_path)
        try:
            conn.execute("begin immediate")
            result = self._create_pending_retry_in_connection(
                conn,
                user_id,
                conversation_id,
                failed_assistant_message_id,
                request_id,
                timestamp,
                admit_new_turn=admit_new_turn,
            )
            conn.commit()
            return result
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def complete_turn(
        self,
        user_id: str,
        assistant_message_id: str,
        expected_version: int,
        answer: str,
        sources: Sequence[QaSourceDraft],
        source_state: str,
        memory_id: str | None,
        *,
        now: str | None = None,
    ) -> bool:
        if source_state not in {"available", "none", "legacy_unavailable"}:
            raise QaValidationError("QA_SOURCE_STATE_INVALID", "invalid source state")
        source_list = tuple(sources)
        timestamp = now or _utc_now()
        conn = connect(self.db_path)
        try:
            conn.execute("begin immediate")
            row = conn.execute(
                f"""
                select conversation_id from qa_messages
                where id = ? and user_id = ? and role = 'assistant'
                  and status = 'pending' and version = ?
                  and exists (
                      select 1 from qa_conversations
                      where qa_conversations.id = qa_messages.conversation_id
                        and qa_conversations.user_id = qa_messages.user_id
                        and {_not_fenced_clause('qa_conversations')}
                  )
                """,
                (assistant_message_id, user_id, expected_version),
            ).fetchone()
            if row is None:
                conn.commit()
                return False
            conversation_id = row["conversation_id"]
            self._validate_source_scope(
                conn, user_id, conversation_id, source_list
            )
            memory_status = "completed" if memory_id else "pending"
            updated = conn.execute(
                """
                update qa_messages
                set status = 'completed', content = ?, source_state = ?,
                    memory_id = ?, memory_sync_status = ?, safe_error_code = null,
                    trace_id = null, version = version + 1, updated_at = ?,
                    completed_at = ?
                where id = ? and user_id = ? and role = 'assistant'
                  and status = 'pending' and version = ?
                """,
                (
                    str(answer),
                    source_state,
                    memory_id,
                    memory_status,
                    timestamp,
                    timestamp,
                    assistant_message_id,
                    user_id,
                    expected_version,
                ),
            )
            if not updated.rowcount:
                conn.commit()
                return False
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
                        assistant_message_id,
                        conversation_id,
                        user_id,
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
            self._touch_conversation(conn, user_id, conversation_id, timestamp)
            conn.commit()
            return True
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def fail_turn(
        self,
        user_id: str,
        assistant_message_id: str,
        expected_version: int,
        error_code: str,
        trace_id: str | None,
        *,
        now: str | None = None,
    ) -> bool:
        return self._finish_pending(
            user_id,
            assistant_message_id,
            expected_version,
            "failed",
            error_code,
            trace_id,
            now=now,
        )

    def cancel_turn(
        self,
        user_id: str,
        assistant_message_id: str,
        expected_version: int,
        *,
        now: str | None = None,
    ) -> bool:
        return self._finish_pending(
            user_id,
            assistant_message_id,
            expected_version,
            "cancelled",
            None,
            None,
            now=now,
        )

    def recover_interrupted_questions(
        self,
        error_code: str = "QA_REQUEST_INTERRUPTED",
        *,
        now: str | None = None,
    ) -> int:
        timestamp = now or _utc_now()
        with connect(self.db_path) as conn:
            updated = conn.execute(
                """
                update qa_messages
                set status = 'failed', safe_error_code = ?, trace_id = null,
                    memory_sync_status = 'not_required', version = version + 1,
                    updated_at = ?, completed_at = ?
                where role = 'assistant' and status = 'pending'
                  and not exists (
                      select 1 from qa_jobs
                      where qa_jobs.assistant_message_id = qa_messages.id
                        and qa_jobs.user_id = qa_messages.user_id
                        and qa_jobs.status in ('queued','running')
                  )
                """,
                (error_code, timestamp, timestamp),
            )
            return updated.rowcount

    def hard_delete_conversation(
        self, user_id: str, conversation_id: str
    ) -> bool:
        conn = connect(self.db_path)
        try:
            conn.execute("begin immediate")
            deleted = conn.execute(
                "delete from qa_conversations where id = ? and user_id = ?",
                (conversation_id, user_id),
            )
            conn.commit()
            return bool(deleted.rowcount)
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def list_completed_turns_for_report(
        self, user_id: str
    ) -> tuple[QaReportTurn, ...]:
        with connect(self.db_path) as conn:
            rows = conn.execute(
                f"""
                select user_message.content as question,
                       assistant_message.content as answer,
                       user_message.mode as mode,
                       user_message.created_at as asked_at,
                       user_message.conversation_id as conversation_id
                from qa_messages user_message
                join qa_messages assistant_message
                  on assistant_message.user_id = user_message.user_id
                 and assistant_message.conversation_id = user_message.conversation_id
                 and assistant_message.turn_id = user_message.turn_id
                 and assistant_message.role = 'assistant'
                 and assistant_message.status = 'completed'
                join qa_conversations
                  on qa_conversations.id = user_message.conversation_id
                 and qa_conversations.user_id = user_message.user_id
                where user_message.user_id = ?
                  and user_message.role = 'user'
                  and user_message.status = 'completed'
                  and {_not_fenced_clause('qa_conversations')}
                order by user_message.created_at, user_message.id
                """,
                (user_id,),
            ).fetchall()
            turns: list[QaReportTurn] = []
            for row in rows:
                documents = conn.execute(
                    """
                    select document_id, document_name
                    from qa_conversation_documents
                    where user_id = ? and conversation_id = ?
                    order by position
                    """,
                    (user_id, row["conversation_id"]),
                ).fetchall()
                turns.append(
                    QaReportTurn(
                        question=row["question"],
                        answer=row["answer"],
                        document_ids=tuple(item["document_id"] for item in documents),
                        document_names=tuple(item["document_name"] for item in documents),
                        mode=row["mode"] or "auto",
                        asked_at=row["asked_at"],
                    )
                )
            return tuple(turns)

    def list_recent_completed_turns(
        self, user_id: str, *, limit: int = 5
    ) -> tuple[QaReportTurn, ...]:
        if not 1 <= limit <= 20:
            raise ValueError("limit must be between 1 and 20")
        with connect(self.db_path) as conn:
            rows = conn.execute(
                f"""
                select user_message.content as question,
                       assistant_message.content as answer,
                       user_message.mode as mode,
                       user_message.created_at as asked_at,
                       user_message.conversation_id as conversation_id
                from qa_messages user_message
                join qa_messages assistant_message
                  on assistant_message.user_id = user_message.user_id
                 and assistant_message.conversation_id = user_message.conversation_id
                 and assistant_message.turn_id = user_message.turn_id
                 and assistant_message.role = 'assistant'
                 and assistant_message.status = 'completed'
                join qa_conversations
                  on qa_conversations.id = user_message.conversation_id
                 and qa_conversations.user_id = user_message.user_id
                where user_message.user_id = ? and user_message.role = 'user'
                  and user_message.status = 'completed'
                  and {_not_fenced_clause('qa_conversations')}
                order by user_message.created_at desc, user_message.id desc
                limit ?
                """,
                (user_id, limit),
            ).fetchall()
            turns = []
            for row in rows:
                documents = conn.execute(
                    """select document_id, document_name from qa_conversation_documents
                       where user_id = ? and conversation_id = ? order by position""",
                    (user_id, row["conversation_id"]),
                ).fetchall()
                turns.append(QaReportTurn(
                    question=row["question"], answer=row["answer"],
                    document_ids=tuple(item["document_id"] for item in documents),
                    document_names=tuple(item["document_name"] for item in documents),
                    mode=row["mode"] or "auto", asked_at=row["asked_at"],
                ))
        return tuple(turns)

    def count_completed_turns(self, user_id: str) -> int:
        with connect(self.db_path) as conn:
            row = conn.execute(
                f"""
                select count(*) as count from qa_messages assistant_message
                join qa_conversations
                  on qa_conversations.id = assistant_message.conversation_id
                 and qa_conversations.user_id = assistant_message.user_id
                where assistant_message.user_id = ?
                  and assistant_message.role = 'assistant'
                  and assistant_message.status = 'completed'
                  and {_not_fenced_clause('qa_conversations')}
                """, (user_id,),
            ).fetchone()
        return int(row["count"])

    def list_completed_activity_dates(
        self, user_id: str, *, since: str
    ) -> tuple[str, ...]:
        with connect(self.db_path) as conn:
            rows = conn.execute(
                f"""
                select assistant_message.completed_at as occurred_at
                from qa_messages assistant_message
                join qa_conversations
                  on qa_conversations.id = assistant_message.conversation_id
                 and qa_conversations.user_id = assistant_message.user_id
                where assistant_message.user_id = ?
                  and assistant_message.role = 'assistant'
                  and assistant_message.status = 'completed'
                  and assistant_message.completed_at >= ?
                  and {_not_fenced_clause('qa_conversations')}
                order by assistant_message.completed_at
                """,
                (user_id, since),
            ).fetchall()
        return tuple(row["occurred_at"] for row in rows if row["occurred_at"])

    def update_rolling_summary(
        self,
        user_id: str,
        conversation_id: str,
        expected_version: int,
        expected_summary_version: int,
        summary: str,
        through_message_id: str,
        *,
        now: str | None = None,
    ) -> bool:
        timestamp = now or _utc_now()
        with connect(self.db_path) as conn:
            updated = conn.execute(
                f"""
                update qa_conversations
                set rolling_summary = ?, summary_through_message_id = ?,
                    summary_version = summary_version + 1,
                    version = version + 1, updated_at = ?
                where id = ? and user_id = ? and version = ?
                  and summary_version = ?
                  and {_not_fenced_clause('qa_conversations')}
                  and exists (
                      select 1 from qa_messages
                      where id = ? and conversation_id = qa_conversations.id
                        and user_id = qa_conversations.user_id
                        and status = 'completed'
                  )
                """,
                (
                    summary,
                    through_message_id,
                    timestamp,
                    conversation_id,
                    user_id,
                    expected_version,
                    expected_summary_version,
                    through_message_id,
                ),
            )
            return bool(updated.rowcount)

    def claim_next_memory_sync(
        self,
        worker_id: str,
        *,
        lease_seconds: int,
        now: str | None = None,
    ) -> QaMessage | None:
        if lease_seconds < 1:
            raise QaValidationError(
                "QA_MEMORY_LEASE_INVALID", "lease seconds must be positive"
            )
        timestamp = now or _utc_now()
        expires_at = _add_seconds(timestamp, lease_seconds)
        conn = connect(self.db_path)
        try:
            conn.execute("begin immediate")
            candidates = conn.execute(
                f"""
                select * from qa_messages
                where role = 'assistant' and status = 'completed'
                  and exists (
                      select 1 from qa_conversations
                      where qa_conversations.id = qa_messages.conversation_id
                        and qa_conversations.user_id = qa_messages.user_id
                        and {_not_fenced_clause('qa_conversations')}
                  )
                  and (
                      memory_sync_status in ('pending','failed')
                      or (
                          memory_sync_status = 'running'
                          and memory_sync_lease_expires_at <= ?
                      )
                  )
                order by completed_at, id
                """,
                (timestamp,),
            ).fetchall()
            for candidate in candidates:
                updated = conn.execute(
                    f"""
                    update qa_messages
                    set memory_sync_status = 'running',
                        memory_sync_lease_owner = ?,
                        memory_sync_lease_expires_at = ?,
                        memory_sync_attempt_count = memory_sync_attempt_count + 1,
                        version = version + 1, updated_at = ?
                    where id = ? and user_id = ? and version = ?
                      and role = 'assistant' and status = 'completed'
                      and exists (
                          select 1 from qa_conversations
                          where qa_conversations.id = qa_messages.conversation_id
                            and qa_conversations.user_id = qa_messages.user_id
                            and {_not_fenced_clause('qa_conversations')}
                      )
                      and (
                          memory_sync_status in ('pending','failed')
                          or (
                              memory_sync_status = 'running'
                              and memory_sync_lease_expires_at <= ?
                          )
                      )
                    """,
                    (
                        worker_id,
                        expires_at,
                        timestamp,
                        candidate["id"],
                        candidate["user_id"],
                        candidate["version"],
                        timestamp,
                    ),
                )
                if updated.rowcount:
                    row = conn.execute(
                        "select * from qa_messages where id = ?",
                        (candidate["id"],),
                    ).fetchone()
                    result = self._message_from_row(conn, row)
                    conn.commit()
                    return result
            conn.commit()
            return None
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def heartbeat_memory_sync(
        self,
        message_id: str,
        worker_id: str,
        *,
        lease_seconds: int,
        now: str | None = None,
    ) -> bool:
        if lease_seconds < 1:
            return False
        timestamp = now or _utc_now()
        expires_at = _add_seconds(timestamp, lease_seconds)
        with connect(self.db_path) as conn:
            updated = conn.execute(
                f"""
                update qa_messages
                set memory_sync_lease_expires_at = ?, updated_at = ?
                where id = ? and memory_sync_status = 'running'
                  and memory_sync_lease_owner = ?
                  and memory_sync_lease_expires_at > ?
                  and exists (
                      select 1 from qa_conversations
                      where qa_conversations.id = qa_messages.conversation_id
                        and qa_conversations.user_id = qa_messages.user_id
                        and {_not_fenced_clause('qa_conversations')}
                  )
                """,
                (expires_at, timestamp, message_id, worker_id, timestamp),
            )
            return bool(updated.rowcount)

    def complete_memory_sync(
        self,
        user_id: str,
        message_id: str,
        worker_id: str,
        memory_id: str,
        expected_version: int,
        *,
        now: str | None = None,
    ) -> bool:
        return self._finish_memory_sync(
            user_id,
            message_id,
            worker_id,
            expected_version,
            "completed",
            memory_id,
            now=now,
        )

    def fail_memory_sync(
        self,
        user_id: str,
        message_id: str,
        worker_id: str,
        expected_version: int,
        safe_error_code: str,
        *,
        now: str | None = None,
    ) -> bool:
        return self._finish_memory_sync(
            user_id,
            message_id,
            worker_id,
            expected_version,
            "failed",
            None,
            now=now,
        )

    def _finish_memory_sync(
        self,
        user_id: str,
        message_id: str,
        worker_id: str,
        expected_version: int,
        status: str,
        memory_id: str | None,
        *,
        now: str | None,
    ) -> bool:
        timestamp = now or _utc_now()
        with connect(self.db_path) as conn:
            updated = conn.execute(
                f"""
                update qa_messages
                set memory_sync_status = ?, memory_id = ?,
                    memory_sync_lease_owner = null,
                    memory_sync_lease_expires_at = null,
                    version = version + 1, updated_at = ?
                where id = ? and user_id = ? and role = 'assistant'
                  and status = 'completed' and memory_sync_status = 'running'
                  and memory_sync_lease_owner = ?
                  and memory_sync_lease_expires_at > ? and version = ?
                  and exists (
                      select 1 from qa_conversations
                      where qa_conversations.id = qa_messages.conversation_id
                        and qa_conversations.user_id = qa_messages.user_id
                        and {_not_fenced_clause('qa_conversations')}
                  )
                """,
                (
                    status,
                    memory_id,
                    timestamp,
                    message_id,
                    user_id,
                    worker_id,
                    timestamp,
                    expected_version,
                ),
            )
            return bool(updated.rowcount)

    def _create_pending_turn_in_connection(
        self,
        conn: sqlite3.Connection,
        user_id: str,
        conversation_id: str,
        question: str,
        mode: str,
        client_request_id: str,
        *,
        retry_of_message_id: str | None,
        timestamp: str,
        admit_new_turn: Callable[[], None] | None = None,
    ) -> PendingTurn:
        normalized_question = normalize_question(question)
        request_id = str(client_request_id or "").strip()
        if not request_id:
            raise QaValidationError(
                "QA_CLIENT_REQUEST_REQUIRED", "client request id is required"
            )
        conversation = conn.execute(
            f"""
            select * from qa_conversations where id = ? and user_id = ?
              and {_not_fenced_clause('qa_conversations')}
            """,
            (conversation_id, user_id),
        ).fetchone()
        if conversation is None:
            raise QaValidationError("QA_CONVERSATION_NOT_FOUND", "conversation not found")
        document_rows = conn.execute(
            """
            select document_id from qa_conversation_documents
            where conversation_id = ? and user_id = ? order by position
            """,
            (conversation_id, user_id),
        ).fetchall()
        normalized_mode = validate_mode(mode, document_rows)

        existing_user = conn.execute(
            """
            select * from qa_messages
            where user_id = ? and conversation_id = ? and role = 'user'
              and client_request_id = ?
            """,
            (user_id, conversation_id, request_id),
        ).fetchone()
        if existing_user is not None:
            assistant = conn.execute(
                """
                select * from qa_messages
                where user_id = ? and conversation_id = ?
                  and turn_id = ? and role = 'assistant'
                """,
                (user_id, conversation_id, existing_user["turn_id"]),
            ).fetchone()
            if assistant is None:
                raise QaConflictError("QA_TURN_INCOMPLETE")
            return PendingTurn(
                self._message_from_row(conn, existing_user),
                self._message_from_row(conn, assistant),
                True,
            )

        if retry_of_message_id is not None:
            retried = conn.execute(
                """
                select 1 from qa_messages
                where id = ? and user_id = ? and conversation_id = ?
                  and role = 'assistant' and status = 'failed'
                """,
                (retry_of_message_id, user_id, conversation_id),
            ).fetchone()
            if retried is None:
                raise QaValidationError(
                    "QA_RETRY_NOT_ALLOWED", "retry target is not a failed answer"
                )

        # Keep admission and the idempotency check in the same write transaction.
        # The callback only reserves a slot and must not wait for queue capacity.
        if admit_new_turn is not None:
            admit_new_turn()

        busy = conn.execute(
            """
            select 1 from qa_messages
            where user_id = ? and conversation_id = ?
              and role = 'assistant' and status = 'pending'
            """,
            (user_id, conversation_id),
        ).fetchone()
        if busy is not None:
            raise QaConflictError("QA_CONVERSATION_BUSY")

        turn_id = str(uuid4())
        # The page cursor deliberately uses (created_at, id). Both rows share a
        # transaction timestamp, so assigning the lower ID to the user keeps
        # the pair in conversational order without another ordering column.
        user_message_id, assistant_message_id = sorted(
            (str(uuid4()), str(uuid4()))
        )
        conn.execute(
            """
            insert into qa_messages (
                id, conversation_id, user_id, turn_id, role, status, mode,
                content, source_state, client_request_id, memory_sync_status,
                created_at, updated_at, completed_at
            ) values (
                ?, ?, ?, ?, 'user', 'completed', ?, ?, 'none', ?,
                'not_required', ?, ?, ?
            )
            """,
            (
                user_message_id,
                conversation_id,
                user_id,
                turn_id,
                normalized_mode,
                normalized_question,
                request_id,
                timestamp,
                timestamp,
                timestamp,
            ),
        )
        conn.execute(
            """
            insert into qa_messages (
                id, conversation_id, user_id, turn_id, role, status, mode,
                content, source_state, retry_of_message_id,
                memory_sync_status, created_at, updated_at
            ) values (
                ?, ?, ?, ?, 'assistant', 'pending', ?, '', 'none', ?,
                'not_required', ?, ?
            )
            """,
            (
                assistant_message_id,
                conversation_id,
                user_id,
                turn_id,
                normalized_mode,
                retry_of_message_id,
                timestamp,
                timestamp,
            ),
        )
        prior_count = conn.execute(
            """
            select count(*) as count from qa_messages
            where conversation_id = ? and user_id = ? and id != ? and id != ?
            """,
            (conversation_id, user_id, user_message_id, assistant_message_id),
        ).fetchone()["count"]
        title = conversation_title(normalized_question) if prior_count == 0 else None
        conn.execute(
            """
            update qa_conversations
            set title = coalesce(?, title), last_message_at = ?, updated_at = ?,
                version = version + 1
            where id = ? and user_id = ?
            """,
            (title, timestamp, timestamp, conversation_id, user_id),
        )
        user_row = conn.execute(
            "select * from qa_messages where id = ? and user_id = ?",
            (user_message_id, user_id),
        ).fetchone()
        assistant_row = conn.execute(
            "select * from qa_messages where id = ? and user_id = ?",
            (assistant_message_id, user_id),
        ).fetchone()
        return PendingTurn(
            self._message_from_row(conn, user_row),
            self._message_from_row(conn, assistant_row),
            False,
        )

    def _create_pending_retry_in_connection(
        self,
        conn: sqlite3.Connection,
        user_id: str,
        conversation_id: str,
        failed_assistant_message_id: str,
        client_request_id: str,
        timestamp: str,
        *,
        admit_new_turn: Callable[[], None] | None = None,
    ) -> PendingTurn:
        conversation = conn.execute(
            f"""
            select * from qa_conversations where id = ? and user_id = ?
              and {_not_fenced_clause('qa_conversations')}
            """,
            (conversation_id, user_id),
        ).fetchone()
        if conversation is None:
            raise QaValidationError("QA_CONVERSATION_NOT_FOUND", "conversation not found")

        request = conn.execute(
            """
            select * from qa_retry_requests
            where user_id = ? and conversation_id = ? and client_request_id = ?
            """,
            (user_id, conversation_id, client_request_id),
        ).fetchone()
        if request is not None:
            if request["failed_assistant_message_id"] != failed_assistant_message_id:
                raise QaValidationError(
                    "QA_CLIENT_REQUEST_REUSED", "client request id is already in use"
                )
            return self._pending_retry_from_ids(
                conn,
                user_id,
                conversation_id,
                request["failed_assistant_message_id"],
                request["assistant_message_id"],
            )

        request = conn.execute(
            """
            select * from qa_retry_requests
            where user_id = ? and conversation_id = ?
              and failed_assistant_message_id = ?
            """,
            (user_id, conversation_id, failed_assistant_message_id),
        ).fetchone()
        if request is not None:
            return self._pending_retry_from_ids(
                conn,
                user_id,
                conversation_id,
                request["failed_assistant_message_id"],
                request["assistant_message_id"],
            )

        failed = conn.execute(
            """
            select * from qa_messages
            where id = ? and user_id = ? and conversation_id = ?
              and role = 'assistant' and status = 'failed'
            """,
            (failed_assistant_message_id, user_id, conversation_id),
        ).fetchone()
        if failed is None:
            raise QaValidationError(
                "QA_RETRY_NOT_ALLOWED", "retry target is not a failed answer"
            )
        paired_user = conn.execute(
            """
            select * from qa_messages
            where user_id = ? and conversation_id = ? and turn_id = ?
              and role = 'user' and status = 'completed'
            order by created_at, id limit 1
            """,
            (user_id, conversation_id, failed["turn_id"]),
        ).fetchone()
        if paired_user is None:
            raise QaValidationError(
                "QA_RETRY_NOT_ALLOWED", "retry target has no user message"
            )

        # Databases created before the retry ledger may already contain a
        # linked assistant. Adopt one canonical child without rewriting history.
        legacy_retry = conn.execute(
            """
            select * from qa_messages
            where user_id = ? and conversation_id = ?
              and retry_of_message_id = ? and role = 'assistant'
            order by created_at, id limit 1
            """,
            (user_id, conversation_id, failed_assistant_message_id),
        ).fetchone()
        if legacy_retry is not None:
            conn.execute(
                """
                insert into qa_retry_requests (
                    user_id, conversation_id, failed_assistant_message_id,
                    assistant_message_id, client_request_id, created_at
                ) values (?, ?, ?, ?, ?, ?)
                """,
                (
                    user_id,
                    conversation_id,
                    failed_assistant_message_id,
                    legacy_retry["id"],
                    client_request_id,
                    timestamp,
                ),
            )
            return PendingTurn(
                self._message_from_row(conn, paired_user),
                self._message_from_row(conn, legacy_retry),
                True,
            )

        if admit_new_turn is not None:
            admit_new_turn()

        busy = conn.execute(
            """
            select 1 from qa_messages
            where user_id = ? and conversation_id = ?
              and role = 'assistant' and status = 'pending'
            """,
            (user_id, conversation_id),
        ).fetchone()
        if busy is not None:
            raise QaConflictError("QA_CONVERSATION_BUSY")

        assistant_message_id = str(uuid4())
        conn.execute(
            """
            insert into qa_messages (
                id, conversation_id, user_id, turn_id, role, status, mode,
                content, source_state, retry_of_message_id,
                memory_sync_status, created_at, updated_at
            ) values (
                ?, ?, ?, ?, 'assistant', 'pending', ?, '', 'none', ?,
                'not_required', ?, ?
            )
            """,
            (
                assistant_message_id,
                conversation_id,
                user_id,
                failed["turn_id"],
                failed["mode"],
                failed_assistant_message_id,
                timestamp,
                timestamp,
            ),
        )
        conn.execute(
            """
            insert into qa_retry_requests (
                user_id, conversation_id, failed_assistant_message_id,
                assistant_message_id, client_request_id, created_at
            ) values (?, ?, ?, ?, ?, ?)
            """,
            (
                user_id,
                conversation_id,
                failed_assistant_message_id,
                assistant_message_id,
                client_request_id,
                timestamp,
            ),
        )
        conn.execute(
            """
            update qa_conversations
            set last_message_at = ?, updated_at = ?, version = version + 1
            where id = ? and user_id = ?
            """,
            (timestamp, timestamp, conversation_id, user_id),
        )
        assistant = conn.execute(
            "select * from qa_messages where id = ? and user_id = ?",
            (assistant_message_id, user_id),
        ).fetchone()
        return PendingTurn(
            self._message_from_row(conn, paired_user),
            self._message_from_row(conn, assistant),
            False,
        )

    def _pending_retry_from_ids(
        self,
        conn: sqlite3.Connection,
        user_id: str,
        conversation_id: str,
        failed_assistant_message_id: str,
        assistant_message_id: str,
    ) -> PendingTurn:
        failed = conn.execute(
            """
            select turn_id from qa_messages
            where id = ? and user_id = ? and conversation_id = ?
            """,
            (failed_assistant_message_id, user_id, conversation_id),
        ).fetchone()
        assistant = conn.execute(
            """
            select * from qa_messages
            where id = ? and user_id = ? and conversation_id = ?
            """,
            (assistant_message_id, user_id, conversation_id),
        ).fetchone()
        if failed is None or assistant is None:
            raise QaConflictError("QA_RETRY_INCOMPLETE")
        paired_user = conn.execute(
            """
            select * from qa_messages
            where user_id = ? and conversation_id = ? and turn_id = ?
              and role = 'user' and status = 'completed'
            order by created_at, id limit 1
            """,
            (user_id, conversation_id, failed["turn_id"]),
        ).fetchone()
        if paired_user is None:
            raise QaConflictError("QA_RETRY_INCOMPLETE")
        return PendingTurn(
            self._message_from_row(conn, paired_user),
            self._message_from_row(conn, assistant),
            True,
        )

    def _finish_pending(
        self,
        user_id: str,
        assistant_message_id: str,
        expected_version: int,
        status: str,
        error_code: str | None,
        trace_id: str | None,
        *,
        now: str | None,
    ) -> bool:
        timestamp = now or _utc_now()
        conn = connect(self.db_path)
        try:
            conn.execute("begin immediate")
            row = conn.execute(
                f"""
                select conversation_id from qa_messages
                where id = ? and user_id = ? and role = 'assistant'
                  and status = 'pending' and version = ?
                  and exists (
                      select 1 from qa_conversations
                      where qa_conversations.id = qa_messages.conversation_id
                        and qa_conversations.user_id = qa_messages.user_id
                        and {_not_fenced_clause('qa_conversations')}
                  )
                """,
                (assistant_message_id, user_id, expected_version),
            ).fetchone()
            if row is None:
                conn.commit()
                return False
            updated = conn.execute(
                """
                update qa_messages
                set status = ?, safe_error_code = ?, trace_id = ?,
                    memory_sync_status = 'not_required', version = version + 1,
                    updated_at = ?, completed_at = ?
                where id = ? and user_id = ? and role = 'assistant'
                  and status = 'pending' and version = ?
                """,
                (
                    status,
                    error_code,
                    trace_id,
                    timestamp,
                    timestamp,
                    assistant_message_id,
                    user_id,
                    expected_version,
                ),
            )
            if updated.rowcount:
                self._touch_conversation(
                    conn, user_id, row["conversation_id"], timestamp
                )
            conn.commit()
            return bool(updated.rowcount)
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _validate_source_scope(
        self,
        conn: sqlite3.Connection,
        user_id: str,
        conversation_id: str,
        sources: Sequence[QaSourceDraft],
    ) -> None:
        scope = {
            row["document_id"]
            for row in conn.execute(
                """
                select document_id from qa_conversation_documents
                where user_id = ? and conversation_id = ?
                """,
                (user_id, conversation_id),
            )
        }
        if any(source.document_id not in scope for source in sources):
            raise QaValidationError(
                "QA_SOURCE_OUT_OF_SCOPE", "source document is outside the conversation"
            )

    def _touch_conversation(
        self,
        conn: sqlite3.Connection,
        user_id: str,
        conversation_id: str,
        timestamp: str,
    ) -> None:
        conn.execute(
            """
            update qa_conversations
            set last_message_at = ?, updated_at = ?, version = version + 1
            where id = ? and user_id = ?
            """,
            (timestamp, timestamp, conversation_id, user_id),
        )

    def _get_conversation(
        self, conn: sqlite3.Connection, user_id: str, conversation_id: str
    ) -> QaConversationAggregate | None:
        row = conn.execute(
            f"""
            select * from qa_conversations where id = ? and user_id = ?
              and {_not_fenced_clause('qa_conversations')}
            """,
            (conversation_id, user_id),
        ).fetchone()
        return self._aggregate_from_row(conn, row) if row is not None else None

    def _aggregate_from_row(
        self, conn: sqlite3.Connection, row: sqlite3.Row
    ) -> QaConversationAggregate:
        documents = conn.execute(
            """
            select * from qa_conversation_documents
            where conversation_id = ? and user_id = ? order by position
            """,
            (row["id"], row["user_id"]),
        ).fetchall()
        return QaConversationAggregate(
            _conversation_from_row(row),
            tuple(_document_from_row(document) for document in documents),
        )

    def _message_from_row(
        self, conn: sqlite3.Connection, row: sqlite3.Row
    ) -> QaMessage:
        source_rows = conn.execute(
            """
            select * from qa_message_sources
            where assistant_message_id = ? and user_id = ? order by position
            """,
            (row["id"], row["user_id"]),
        ).fetchall()
        return _message_from_row(row, tuple(_source_from_row(item) for item in source_rows))


def _validated_limit(limit: int, *, maximum: int) -> int:
    if not 1 <= limit <= maximum:
        raise QaValidationError(
            "QA_PAGE_LIMIT_INVALID", f"limit must be between 1 and {maximum}"
        )
    return limit


def _not_fenced_clause(conversation_alias: str) -> str:
    return f"""
    not exists (
        select 1 from qa_deletion_fences deletion_fence
        where deletion_fence.user_id = {conversation_alias}.user_id
          and (
              deletion_fence.status in ('queued', 'running')
              or (
                  deletion_fence.status = 'failed'
                  and deletion_fence.attempt_count < 3
              )
          )
          and (
              (
                  deletion_fence.target_type = 'conversation'
                  and deletion_fence.target_id = {conversation_alias}.id
              )
              or (
                  deletion_fence.target_type = 'document'
                  and exists (
                      select 1 from qa_conversation_documents fenced_document
                      where fenced_document.conversation_id = {conversation_alias}.id
                        and fenced_document.user_id = {conversation_alias}.user_id
                        and fenced_document.document_id = deletion_fence.target_id
                  )
              )
          )
    )
    """


def _add_seconds(timestamp: str, seconds: int) -> str:
    parsed = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return (parsed + timedelta(seconds=seconds)).astimezone(timezone.utc).isoformat().replace(
        "+00:00", "Z"
    )


def _conversation_from_row(row: sqlite3.Row) -> QaConversation:
    return QaConversation(
        id=row["id"],
        user_id=row["user_id"],
        title=row["title"],
        origin=row["origin"],
        rolling_summary=row["rolling_summary"],
        summary_through_message_id=row["summary_through_message_id"],
        summary_version=row["summary_version"],
        version=row["version"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        last_message_at=row["last_message_at"],
    )


def _document_from_row(row: sqlite3.Row) -> QaConversationDocument:
    return QaConversationDocument(
        conversation_id=row["conversation_id"],
        user_id=row["user_id"],
        document_id=row["document_id"],
        document_name=row["document_name"],
        position=row["position"],
    )


def _message_from_row(row: sqlite3.Row, sources: tuple[QaSource, ...]) -> QaMessage:
    return QaMessage(
        id=row["id"],
        conversation_id=row["conversation_id"],
        user_id=row["user_id"],
        turn_id=row["turn_id"],
        role=row["role"],
        status=row["status"],
        mode=row["mode"],
        content=row["content"],
        source_state=row["source_state"],
        client_request_id=row["client_request_id"],
        retry_of_message_id=row["retry_of_message_id"],
        memory_id=row["memory_id"],
        memory_sync_status=row["memory_sync_status"],
        memory_sync_attempt_count=row["memory_sync_attempt_count"],
        memory_sync_lease_owner=row["memory_sync_lease_owner"],
        memory_sync_lease_expires_at=row["memory_sync_lease_expires_at"],
        safe_error_code=row["safe_error_code"],
        trace_id=row["trace_id"],
        version=row["version"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        completed_at=row["completed_at"],
        sources=sources,
    )


def _source_from_row(row: sqlite3.Row) -> QaSource:
    return QaSource(
        id=row["id"],
        assistant_message_id=row["assistant_message_id"],
        conversation_id=row["conversation_id"],
        user_id=row["user_id"],
        position=row["position"],
        citation_id=row["citation_id"],
        document_id=row["document_id"],
        document_name=row["document_name"],
        page_number=row["page_number"],
        section=row["section"],
        excerpt=row["excerpt"],
        reference=row["reference"],
        truncated=bool(row["truncated"]),
        source_type=row["source_type"],
        chunk_id=row["chunk_id"],
    )
