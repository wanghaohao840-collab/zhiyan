from __future__ import annotations

import base64
import binascii
import json
import unicodedata
from dataclasses import dataclass
from typing import Literal, Sequence


QaMode = Literal["auto", "joint", "compare", "summary"]
QaRole = Literal["user", "assistant"]
QaMessageStatus = Literal["pending", "completed", "failed", "cancelled"]
QaSourceState = Literal["available", "none", "legacy_unavailable"]
QaMemorySyncStatus = Literal[
    "pending", "running", "completed", "failed", "not_required"
]
QaJobStatus = Literal["queued", "running", "completed", "failed", "cancelled"]
QaDeletionTarget = Literal["conversation", "document"]
QaDeletionStatus = Literal["queued", "running", "completed", "failed"]


class QaValidationError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class QaConflictError(RuntimeError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class QaDocumentCandidate:
    document_id: str
    document_name: str
    user_id: str
    status: str = "ready"


@dataclass(frozen=True)
class QaDocumentScopeItem:
    document_id: str
    document_name: str
    position: int


@dataclass(frozen=True)
class QaConversation:
    id: str
    user_id: str
    title: str
    origin: str
    rolling_summary: str
    summary_through_message_id: str | None
    summary_version: int
    version: int
    created_at: str
    updated_at: str
    last_message_at: str


@dataclass(frozen=True)
class QaConversationDocument:
    conversation_id: str
    user_id: str
    document_id: str
    document_name: str
    position: int


@dataclass(frozen=True)
class QaSource:
    id: str
    assistant_message_id: str
    conversation_id: str
    user_id: str
    position: int
    citation_id: str
    document_id: str
    document_name: str
    page_number: int | None
    section: str | None
    excerpt: str
    reference: str
    truncated: bool
    source_type: str
    chunk_id: str | None = None


@dataclass(frozen=True)
class QaSourceDraft:
    citation_id: str
    document_id: str
    document_name: str
    page_number: int | None = None
    section: str | None = None
    excerpt: str = ""
    reference: str = ""
    truncated: bool = False
    source_type: str = "rag"
    chunk_id: str | None = None


@dataclass(frozen=True)
class QaMessage:
    id: str
    conversation_id: str
    user_id: str
    turn_id: str
    role: QaRole
    status: QaMessageStatus
    mode: QaMode | None
    content: str
    source_state: QaSourceState
    client_request_id: str | None
    retry_of_message_id: str | None
    memory_id: str | None
    memory_sync_status: QaMemorySyncStatus
    memory_sync_attempt_count: int
    memory_sync_lease_owner: str | None
    memory_sync_lease_expires_at: str | None
    safe_error_code: str | None
    trace_id: str | None
    version: int
    created_at: str
    updated_at: str
    completed_at: str | None
    sources: tuple[QaSource, ...] = ()


@dataclass(frozen=True)
class QaConversationAggregate:
    conversation: QaConversation
    documents: tuple[QaConversationDocument, ...]

    @property
    def id(self) -> str:
        return self.conversation.id

    @property
    def user_id(self) -> str:
        return self.conversation.user_id

    @property
    def title(self) -> str:
        return self.conversation.title


@dataclass(frozen=True)
class QaConversationPage:
    items: tuple[QaConversationAggregate, ...]
    next_cursor: str | None


@dataclass(frozen=True)
class QaMessagePage:
    items: tuple[QaMessage, ...]
    next_cursor: str | None


@dataclass(frozen=True)
class PendingTurn:
    user_message: QaMessage
    assistant_message: QaMessage
    duplicate: bool


@dataclass(frozen=True)
class QaJob:
    id: str
    conversation_id: str
    user_id: str
    input_message_id: str
    assistant_message_id: str
    status: QaJobStatus
    stage: str
    progress: int
    cancel_requested_at: str | None
    attempt_count: int
    max_attempts: int
    lease_owner: str | None
    lease_expires_at: str | None
    lease_duration_seconds: int | None
    safe_error_code: str | None
    trace_id: str | None
    version: int
    created_at: str
    started_at: str | None
    finished_at: str | None
    updated_at: str


@dataclass(frozen=True)
class SummaryEnqueueResult:
    pending: PendingTurn
    job: QaJob
    duplicate: bool


@dataclass(frozen=True)
class QaDeletion:
    id: str
    user_id: str
    target_type: QaDeletionTarget
    target_id: str
    status: QaDeletionStatus
    stage: str
    affected_conversation_count: int
    attempt_count: int
    safe_error_code: str | None
    trace_id: str | None
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class QaReportTurn:
    question: str
    answer: str
    document_ids: tuple[str, ...]
    document_names: tuple[str, ...]
    mode: QaMode
    asked_at: str


def normalize_question(value: str) -> str:
    normalized = " ".join(str(value or "").split())
    if not normalized:
        raise QaValidationError("QA_QUESTION_REQUIRED", "question is required")
    return normalized


def validate_mode(mode: str, documents: Sequence[object]) -> QaMode:
    normalized = str(mode or "").strip().lower()
    if normalized not in {"auto", "joint", "compare", "summary"}:
        raise QaValidationError("QA_MODE_INVALID", "unsupported QA mode")
    if normalized == "compare" and len(documents) < 2:
        raise QaValidationError(
            "QA_COMPARE_REQUIRES_MULTIPLE_DOCUMENTS",
            "compare mode requires at least two documents",
        )
    return normalized  # type: ignore[return-value]


def validate_document_candidates(
    user_id: str,
    documents: Sequence[QaDocumentCandidate],
) -> tuple[QaDocumentScopeItem, ...]:
    if not 1 <= len(documents) <= 10:
        raise QaValidationError(
            "QA_DOCUMENT_COUNT_INVALID", "a conversation requires 1-10 documents"
        )

    seen: set[str] = set()
    scope: list[QaDocumentScopeItem] = []
    for position, document in enumerate(documents):
        document_id = str(document.document_id or "").strip()
        document_name = str(document.document_name or "").strip()
        if not document_id or not document_name:
            raise QaValidationError(
                "QA_DOCUMENT_INVALID", "document id and name are required"
            )
        if document.user_id != user_id:
            raise QaValidationError("QA_DOCUMENT_NOT_FOUND", "document was not found")
        if document.status != "ready":
            raise QaValidationError(
                "QA_DOCUMENT_NOT_READY", "document is not ready for QA"
            )
        if document_id in seen:
            raise QaValidationError(
                "QA_DOCUMENT_DUPLICATE", "document scope must be distinct"
            )
        seen.add(document_id)
        scope.append(QaDocumentScopeItem(document_id, document_name, position))
    return tuple(scope)


def conversation_title(question: str, limit: int = 40) -> str:
    normalized = normalize_question(question)
    clusters = _graphemes(normalized)
    return "".join(clusters[:limit])


def encode_cursor(timestamp: str, item_id: str) -> str:
    payload = json.dumps([timestamp, item_id], ensure_ascii=False, separators=(",", ":"))
    return base64.urlsafe_b64encode(payload.encode("utf-8")).decode("ascii").rstrip("=")


def decode_cursor(cursor: str) -> tuple[str, str]:
    try:
        padding = "=" * (-len(cursor) % 4)
        value = json.loads(base64.urlsafe_b64decode(cursor + padding).decode("utf-8"))
        if not isinstance(value, list) or len(value) != 2 or not all(
            isinstance(item, str) and item for item in value
        ):
            raise ValueError
        return value[0], value[1]
    except (ValueError, UnicodeDecodeError, json.JSONDecodeError, binascii.Error) as exc:
        raise QaValidationError("QA_CURSOR_INVALID", "cursor is invalid") from exc


def _graphemes(value: str) -> list[str]:
    clusters: list[str] = []
    regional_run = 0
    join_next = False
    for char in value:
        codepoint = ord(char)
        is_mark = bool(unicodedata.combining(char))
        is_variation = 0xFE00 <= codepoint <= 0xFE0F
        is_modifier = 0x1F3FB <= codepoint <= 0x1F3FF
        is_regional = 0x1F1E6 <= codepoint <= 0x1F1FF
        if char == "\u200d" and clusters:
            clusters[-1] += char
        elif clusters and (is_mark or is_variation or is_modifier or join_next):
            clusters[-1] += char
        elif clusters and is_regional and regional_run % 2 == 1:
            clusters[-1] += char
        else:
            clusters.append(char)
        join_next = char == "\u200d"
        regional_run = regional_run + 1 if is_regional else 0
    return clusters
