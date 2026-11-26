from __future__ import annotations

import re
import secrets
import time
from typing import Sequence

from app.qa_answer_engine import (
    QaAnswerEngine,
    QaAnswerRequest,
    QaEngineError,
)
from app.qa_context import QaContextBuilder
from app.qa_models import (
    PendingTurn,
    QaConflictError,
    QaConversationAggregate,
    QaConversationPage,
    QaDocumentCandidate,
    QaMessage,
    QaMessagePage,
    validate_mode,
)
from app.qa_observability import QaTelemetry
from app.qa_repository import QaRepository
from app.qa_stream import QaLiveAnswer, QaLiveAnswers
from assistants.document_selection import build_document_scope


RETRYABLE_ERROR_CODES = frozenset(
    {
        "QA_ENGINE_UNAVAILABLE",
        "QA_RAG_CONNECTION",
        "QA_RAG_TRANSIENT",
        "RAG_CONNECTION_FAILED",
        "RAG_EMBEDDING_FAILED",
        "RAG_OPERATION_FAILED",
        "RAG_COLLECTION_FAILED",
    }
)
SAFE_ERROR_CODE = re.compile(r"^[A-Z][A-Z0-9_]{2,63}$")


class QaNotFoundError(LookupError):
    pass


class QaBusyError(RuntimeError):
    code = "QA_CONVERSATION_BUSY"


class QaRetryNotAllowedError(RuntimeError):
    code = "QA_RETRY_NOT_ALLOWED"


class QaEngineUnavailableError(RuntimeError):
    def __init__(
        self,
        trace_id: str,
        *,
        code: str = "QA_ENGINE_UNAVAILABLE",
        retryable: bool = True,
    ) -> None:
        super().__init__(code)
        self.code = code
        self.trace_id = trace_id
        self.retryable = retryable


class QaService:
    def __init__(
        self,
        session_registry,
        document_library,
        repository: QaRepository,
        answer_engine: QaAnswerEngine,
        context_builder: QaContextBuilder,
        telemetry: QaTelemetry,
        *,
        job_repository=None,
        worker_pool=None,
        deletion_service=None,
        legacy_migration=None,
    ) -> None:
        self.session_registry = session_registry
        self.document_library = document_library
        self.repository = repository
        self.answer_engine = answer_engine
        self.context_builder = context_builder
        self.telemetry = telemetry
        self.job_repository = job_repository
        self.worker_pool = worker_pool
        self.deletion_service = deletion_service
        self.legacy_migration = legacy_migration
        self.live_answers = QaLiveAnswers()

    def create_conversation(
        self,
        session_token: str,
        document_ids: Sequence[str],
        *,
        origin: str = "product",
    ) -> QaConversationAggregate:
        session = self.session_registry.get_session(session_token)
        self._ensure_migrated(session)
        with session.runtime.lock:
            available = {
                item.document_id: item
                for item in self.document_library.list_documents(session_token)
            }
            candidates: list[QaDocumentCandidate] = []
            for document_id in document_ids:
                item = available.get(str(document_id))
                if item is None or item.status != "ready":
                    raise QaNotFoundError(str(document_id))
                candidates.append(
                    QaDocumentCandidate(
                        item.document_id,
                        item.name,
                        str(session.user_id),
                        item.status,
                    )
                )
            return self.repository.create_conversation(
                str(session.user_id), tuple(candidates), origin=origin
            )

    def create_legacy_single_turn_conversation(
        self, session_token: str, selected_documents
    ) -> QaConversationAggregate:
        scope = build_document_scope(selected_documents)
        return self.create_conversation(
            session_token,
            scope.document_ids,
            origin="legacy_gradio",
        )

    def list_conversations(
        self,
        session_token: str,
        *,
        cursor: str | None = None,
        limit: int = 20,
    ) -> QaConversationPage:
        session = self.session_registry.get_session(session_token)
        self._ensure_migrated(session)
        return self.repository.list_conversations(
            str(session.user_id), cursor=cursor, limit=limit
        )

    def get_conversation(
        self, session_token: str, conversation_id: str
    ) -> QaConversationAggregate:
        session = self.session_registry.get_session(session_token)
        conversation = self.repository.get_conversation(
            str(session.user_id), conversation_id
        )
        if conversation is None:
            raise QaNotFoundError(conversation_id)
        return conversation

    def list_messages(
        self,
        session_token: str,
        conversation_id: str,
        *,
        cursor: str | None = None,
        limit: int = 50,
    ) -> QaMessagePage:
        session = self.session_registry.get_session(session_token)
        self._require_conversation(str(session.user_id), conversation_id)
        return self.repository.list_recent_messages(
            str(session.user_id), conversation_id, cursor=cursor, limit=limit
        )

    def get_message(self, session_token: str, message_id: str) -> QaMessage:
        session = self.session_registry.get_session(session_token)
        return self._require_message(str(session.user_id), message_id)

    def get_stream_message(
        self, session_token: str, conversation_id: str, message_id: str
    ) -> QaMessage:
        message = self.get_message(session_token, message_id)
        if message.conversation_id != conversation_id or message.role != "assistant":
            raise QaNotFoundError(message_id)
        return message

    def report_turns(self, session_token: str):
        session = self.session_registry.get_session(session_token)
        self._ensure_migrated(session)
        return self.repository.list_completed_turns_for_report(
            str(session.user_id)
        )

    def recent_completed_turns(self, session_token: str, *, limit: int = 5):
        session = self.session_registry.get_session(session_token)
        self._ensure_migrated(session)
        return self.repository.list_recent_completed_turns(
            str(session.user_id), limit=limit
        )

    def completed_turn_count(self, session_token: str) -> int:
        session = self.session_registry.get_session(session_token)
        self._ensure_migrated(session)
        return self.repository.count_completed_turns(str(session.user_id))

    def completed_activity_dates(self, session_token: str, *, since: str):
        session = self.session_registry.get_session(session_token)
        self._ensure_migrated(session)
        return self.repository.list_completed_activity_dates(
            str(session.user_id), since=since
        )

    def ask(
        self,
        session_token: str,
        conversation_id: str,
        question: str,
        mode: str,
        client_request_id: str,
        *,
        stream: bool = False,
    ) -> QaMessage:
        session = self.session_registry.get_session(session_token)
        user_id = str(session.user_id)
        conversation = self._require_conversation(user_id, conversation_id)
        self._require_scope_available(session_token, conversation)
        normalized_mode = validate_mode(mode, conversation.documents)
        try:
            pending = self.repository.create_pending_turn(
                user_id,
                conversation_id,
                question,
                normalized_mode,
                client_request_id,
            )
        except QaConflictError as error:
            self.telemetry.record("qa_busy", conversation_id=conversation_id)
            raise QaBusyError() from error
        if pending.duplicate:
            self.telemetry.record(
                "qa_idempotent_hit",
                conversation_id=conversation_id,
                message_id=pending.assistant_message.id,
            )
            return self._require_message(user_id, pending.assistant_message.id)
        if stream:
            return self._schedule_question(user_id, conversation, pending, normalized_mode)
        return self._execute_pending(
            session.runtime,
            user_id,
            conversation,
            pending,
            pending.user_message.content,
            normalized_mode,
        )

    def retry(
        self,
        session_token: str,
        failed_assistant_message_id: str,
        client_request_id: str,
    ) -> QaMessage:
        session = self.session_registry.get_session(session_token)
        user_id = str(session.user_id)
        failed = self.repository.get_message(user_id, failed_assistant_message_id)
        if (
            failed is None
            or failed.role != "assistant"
            or failed.status != "failed"
            or failed.safe_error_code not in RETRYABLE_ERROR_CODES
        ):
            raise QaRetryNotAllowedError()
        conversation = self._require_conversation(user_id, failed.conversation_id)
        self._require_scope_available(session_token, conversation)
        paired_user = next(
            (
                item
                for item in self._all_messages(user_id, conversation.id)
                if item.turn_id == failed.turn_id and item.role == "user"
            ),
            None,
        )
        if paired_user is None:
            raise QaRetryNotAllowedError()
        mode = failed.mode or "auto"
        try:
            pending = self.repository.create_pending_retry(
                user_id,
                conversation.id,
                failed.id,
                client_request_id,
            )
        except QaConflictError as error:
            self.telemetry.record("qa_busy", conversation_id=conversation.id)
            raise QaBusyError() from error
        if pending.duplicate:
            self.telemetry.record(
                "qa_idempotent_hit",
                conversation_id=conversation.id,
                message_id=pending.assistant_message.id,
            )
            return self._require_message(user_id, pending.assistant_message.id)
        self.telemetry.record(
            "qa_retry",
            conversation_id=conversation.id,
            message_id=pending.assistant_message.id,
        )
        return self._execute_pending(
            session.runtime,
            user_id,
            conversation,
            pending,
            paired_user.content,
            mode,
        )

    def start_summary(
        self,
        session_token: str,
        conversation_id: str,
        instruction: str,
        client_request_id: str,
    ):
        if self.job_repository is None or self.worker_pool is None:
            raise RuntimeError("QA summary workers are not configured")
        session = self.session_registry.get_session(session_token)
        user_id = str(session.user_id)
        conversation = self._require_conversation(user_id, conversation_id)
        self._require_scope_available(session_token, conversation)
        enqueue = self.job_repository.create_summary_turn_and_job(
            user_id,
            conversation_id,
            str(instruction or "").strip()
            or "总结这些文档的核心内容、共识、分歧与证据。",
            client_request_id,
        )
        self.worker_pool.notify()
        return enqueue.job

    def get_job(self, session_token: str, job_id: str):
        if self.job_repository is None:
            raise RuntimeError("QA summary workers are not configured")
        session = self.session_registry.get_session(session_token)
        return self.job_repository.get(str(session.user_id), job_id)

    def get_active_job(self, session_token: str, conversation_id: str):
        if self.job_repository is None:
            raise RuntimeError("QA summary workers are not configured")
        session = self.session_registry.get_session(session_token)
        user_id = str(session.user_id)
        self._require_conversation(user_id, conversation_id)
        return self.job_repository.get_active_for_conversation(
            user_id, conversation_id
        )

    def cancel_job(self, session_token: str, job_id: str):
        if self.job_repository is None or self.worker_pool is None:
            raise RuntimeError("QA summary workers are not configured")
        session = self.session_registry.get_session(session_token)
        job = self.job_repository.request_cancel(str(session.user_id), job_id)
        if job is not None:
            self.worker_pool.notify()
        return job

    def delete_conversation(
        self, session_token: str, conversation_id: str
    ):
        if self.deletion_service is None:
            raise RuntimeError("QA deletion workers are not configured")
        deletion = self.deletion_service.request_conversation(
            session_token, conversation_id
        )
        if deletion is None:
            raise QaNotFoundError(conversation_id)
        return deletion

    def _schedule_question(
        self, user_id: str, conversation: QaConversationAggregate,
        pending: PendingTurn, mode: str,
    ) -> QaMessage:
        message = pending.assistant_message
        live = self.live_answers.start(message.id)
        runtime_registry = self.session_registry.runtime_registry

        def execute(accepted: bool) -> None:
            runtime = None
            try:
                if not accepted:
                    self.repository.fail_turn(
                        user_id, message.id, message.version,
                        "QA_ENGINE_UNAVAILABLE", secrets.token_urlsafe(12),
                    )
                elif self.repository.get_message(user_id, message.id) is not None:
                    runtime = runtime_registry.acquire_background(user_id)
                    self._execute_pending(
                        runtime, user_id, conversation, pending,
                        pending.user_message.content, mode, live=live,
                    )
            except (QaEngineUnavailableError, QaNotFoundError):
                pass
            except Exception:
                self.repository.fail_turn(
                    user_id, message.id, message.version,
                    "QA_ENGINE_UNAVAILABLE", secrets.token_urlsafe(12),
                )
                raise
            finally:
                self.live_answers.finish(message.id)
                if runtime is not None:
                    runtime_registry.release_background(user_id)

        if self.worker_pool is None or not self.worker_pool.schedule_question(execute):
            execute(False)
            return self._require_message(user_id, message.id)
        return message

    def _execute_pending(
        self,
        runtime,
        user_id: str,
        conversation: QaConversationAggregate,
        pending: PendingTurn,
        question: str,
        mode: str,
        *,
        live: QaLiveAnswer | None = None,
    ) -> QaMessage:
        started = time.perf_counter()
        try:
            context = self.context_builder.build(
                rolling_summary=conversation.conversation.rolling_summary,
                summary_through_message_id=conversation.conversation.summary_through_message_id,
                messages=self._all_messages(user_id, conversation.id),
                current_question=question,
            )
            request = QaAnswerRequest(
                question=question,
                conversation_context=context.rendered,
                document_ids=tuple(item.document_id for item in conversation.documents),
                mode=mode,
                structured_output=mode == "compare",
            )
            stream_answer = getattr(self.answer_engine, "answer_stream", None)
            if live is not None and callable(stream_answer):
                result = stream_answer(
                    runtime, request, stage_callback=live.stage, token_callback=live.token,
                )
            else:
                result = self.answer_engine.answer(runtime, request)
        except Exception as error:
            code, retryable = self._safe_engine_failure(error)
            trace_id = secrets.token_urlsafe(12)
            persisted = self.repository.fail_turn(
                user_id,
                pending.assistant_message.id,
                pending.assistant_message.version,
                code,
                trace_id,
            )
            if not persisted:
                raise QaNotFoundError(conversation.id) from error
            self.telemetry.record(
                "qa_ask_failure",
                duration_ms=(time.perf_counter() - started) * 1000,
                error_code=code,
                conversation_id=conversation.id,
                message_id=pending.assistant_message.id,
            )
            raise QaEngineUnavailableError(
                trace_id, code=code, retryable=retryable
            ) from error

        committed = self.repository.complete_turn(
            user_id,
            pending.assistant_message.id,
            pending.assistant_message.version,
            result.answer,
            result.sources,
            "available" if result.sources else "none",
            None,
        )
        if not committed:
            raise QaNotFoundError(conversation.id)
        self.telemetry.record(
            "qa_ask_success",
            duration_ms=(time.perf_counter() - started) * 1000,
            conversation_id=conversation.id,
            message_id=pending.assistant_message.id,
        )
        if self.worker_pool is not None:
            messages = self._all_messages(user_id, conversation.id)
            if self.context_builder.needs_refresh(
                messages=messages,
                summary_through_message_id=(
                    conversation.conversation.summary_through_message_id
                ),
            ):
                self.worker_pool.schedule_summary_refresh(user_id, conversation.id)
        return self._require_message(user_id, pending.assistant_message.id)

    def _all_messages(
        self, user_id: str, conversation_id: str
    ) -> tuple[QaMessage, ...]:
        items: list[QaMessage] = []
        cursor: str | None = None
        while True:
            page = self.repository.list_messages(
                user_id, conversation_id, cursor=cursor, limit=200
            )
            items.extend(page.items)
            if page.next_cursor is None:
                return tuple(items)
            cursor = page.next_cursor

    def _require_scope_available(
        self, session_token: str, conversation: QaConversationAggregate
    ) -> None:
        available = {
            item.document_id
            for item in self.document_library.list_documents(session_token)
            if item.status == "ready"
        }
        if any(item.document_id not in available for item in conversation.documents):
            raise QaNotFoundError(conversation.id)

    def _require_conversation(
        self, user_id: str, conversation_id: str
    ) -> QaConversationAggregate:
        conversation = self.repository.get_conversation(user_id, conversation_id)
        if conversation is None:
            raise QaNotFoundError(conversation_id)
        return conversation

    def _require_message(self, user_id: str, message_id: str) -> QaMessage:
        message = self.repository.get_message(user_id, message_id)
        if message is None:
            raise QaNotFoundError(message_id)
        return message

    def _ensure_migrated(self, session) -> None:
        if self.legacy_migration is not None:
            self.legacy_migration.ensure_user_migrated(
                str(session.user_id), session.runtime.history
            )

    @staticmethod
    def _safe_engine_failure(error: Exception) -> tuple[str, bool]:
        if isinstance(error, QaEngineError):
            raw_code = str(error.code or "")
            retryable = bool(error.retryable)
            if retryable:
                mapped = {
                    "rag_connection": "QA_RAG_CONNECTION",
                    "rag_operation": "QA_RAG_TRANSIENT",
                }.get(raw_code)
                if mapped:
                    return mapped, True
                if raw_code in RETRYABLE_ERROR_CODES:
                    return raw_code, True
                return "QA_ENGINE_UNAVAILABLE", True
            if SAFE_ERROR_CODE.fullmatch(raw_code) and raw_code not in RETRYABLE_ERROR_CODES:
                return raw_code, False
            return "QA_ENGINE_FAILED", False
        return "QA_ENGINE_UNAVAILABLE", True
