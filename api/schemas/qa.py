from __future__ import annotations

from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field

from app.qa_models import (
    QaConversationAggregate,
    QaConversationPage,
    QaDeletion,
    QaJob,
    QaMessage,
    QaMessagePage,
    QaSource,
)


class QaCapabilitiesResponse(BaseModel):
    enabled: bool


class CreateConversationRequest(BaseModel):
    document_ids: list[str] = Field(min_length=1, max_length=10)


class AskRequest(BaseModel):
    question: str = Field(min_length=1, max_length=20_000)
    mode: Literal["auto", "joint", "compare"] = "auto"
    client_request_id: UUID


class SummaryRequest(BaseModel):
    instruction: str | None = Field(default=None, max_length=20_000)
    client_request_id: UUID


class RetryRequest(BaseModel):
    client_request_id: UUID


class QaDocumentSnapshotResponse(BaseModel):
    document_id: str
    document_name: str
    position: int


class QaConversationResponse(BaseModel):
    conversation_id: str
    title: str
    origin: Literal["product", "legacy_json", "legacy_gradio"]
    rolling_summary: str
    summary_version: int
    created_at: str
    updated_at: str
    last_message_at: str
    documents: list[QaDocumentSnapshotResponse]


class QaConversationPageResponse(BaseModel):
    items: list[QaConversationResponse]
    next_cursor: str | None


class QaCitationResponse(BaseModel):
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


class QaMessageResponse(BaseModel):
    message_id: str
    conversation_id: str
    turn_id: str
    role: Literal["user", "assistant"]
    status: Literal["pending", "completed", "failed", "cancelled"]
    mode: Literal["auto", "joint", "compare", "summary"] | None
    content: str
    source_state: Literal["available", "none", "legacy_unavailable"]
    retry_of_message_id: str | None
    safe_error_code: str | None
    trace_id: str | None
    created_at: str
    updated_at: str
    completed_at: str | None
    sources: list[QaCitationResponse]


class QaMessagePageResponse(BaseModel):
    items: list[QaMessageResponse]
    next_cursor: str | None


class QaJobResponse(BaseModel):
    job_id: str
    conversation_id: str
    input_message_id: str
    assistant_message_id: str
    status: Literal["queued", "running", "completed", "failed", "cancelled"]
    stage: str
    progress: int
    cancel_requested_at: str | None
    attempt_count: int
    max_attempts: int
    safe_error_code: str | None
    trace_id: str | None
    created_at: str
    started_at: str | None
    finished_at: str | None
    updated_at: str


class QaActiveJobResponse(BaseModel):
    job: QaJobResponse | None


class QaDeletionResponse(BaseModel):
    deletion_id: str
    target_type: Literal["conversation", "document"]
    target_id: str
    status: Literal["queued", "running", "completed", "failed"]
    stage: str
    affected_conversation_count: int
    attempt_count: int
    safe_error_code: str | None
    trace_id: str | None
    created_at: str
    updated_at: str


def conversation_response(
    aggregate: QaConversationAggregate,
) -> QaConversationResponse:
    conversation = aggregate.conversation
    return QaConversationResponse(
        conversation_id=conversation.id,
        title=conversation.title,
        origin=conversation.origin,
        rolling_summary=conversation.rolling_summary,
        summary_version=conversation.summary_version,
        created_at=conversation.created_at,
        updated_at=conversation.updated_at,
        last_message_at=conversation.last_message_at,
        documents=[
            QaDocumentSnapshotResponse(
                document_id=item.document_id,
                document_name=item.document_name,
                position=item.position,
            )
            for item in aggregate.documents
        ],
    )


def conversation_page_response(
    page: QaConversationPage,
) -> QaConversationPageResponse:
    return QaConversationPageResponse(
        items=[conversation_response(item) for item in page.items],
        next_cursor=page.next_cursor,
    )


def source_response(source: QaSource) -> QaCitationResponse:
    return QaCitationResponse(
        citation_id=source.citation_id,
        document_id=source.document_id,
        document_name=source.document_name,
        page_number=source.page_number,
        section=source.section,
        excerpt=source.excerpt,
        reference=source.reference,
        truncated=source.truncated,
        source_type=source.source_type,
        chunk_id=source.chunk_id,
    )


def message_response(message: QaMessage) -> QaMessageResponse:
    return QaMessageResponse(
        message_id=message.id,
        conversation_id=message.conversation_id,
        turn_id=message.turn_id,
        role=message.role,
        status=message.status,
        mode=message.mode,
        content=message.content,
        source_state=message.source_state,
        retry_of_message_id=message.retry_of_message_id,
        safe_error_code=message.safe_error_code,
        trace_id=message.trace_id,
        created_at=message.created_at,
        updated_at=message.updated_at,
        completed_at=message.completed_at,
        sources=[source_response(item) for item in message.sources],
    )


def message_page_response(page: QaMessagePage) -> QaMessagePageResponse:
    return QaMessagePageResponse(
        items=[message_response(item) for item in page.items],
        next_cursor=page.next_cursor,
    )


def job_response(job: QaJob) -> QaJobResponse:
    return QaJobResponse(
        job_id=job.id,
        conversation_id=job.conversation_id,
        input_message_id=job.input_message_id,
        assistant_message_id=job.assistant_message_id,
        status=job.status,
        stage=job.stage,
        progress=job.progress,
        cancel_requested_at=job.cancel_requested_at,
        attempt_count=job.attempt_count,
        max_attempts=job.max_attempts,
        safe_error_code=job.safe_error_code,
        trace_id=job.trace_id,
        created_at=job.created_at,
        started_at=job.started_at,
        finished_at=job.finished_at,
        updated_at=job.updated_at,
    )


def active_job_response(job: QaJob | None) -> QaActiveJobResponse:
    return QaActiveJobResponse(job=job_response(job) if job is not None else None)


def deletion_response(deletion: QaDeletion) -> QaDeletionResponse:
    return QaDeletionResponse(
        deletion_id=deletion.id,
        target_type=deletion.target_type,
        target_id=deletion.target_id,
        status=deletion.status,
        stage=deletion.stage,
        affected_conversation_count=deletion.affected_conversation_count,
        attempt_count=deletion.attempt_count,
        safe_error_code=deletion.safe_error_code,
        trace_id=deletion.trace_id,
        created_at=deletion.created_at,
        updated_at=deletion.updated_at,
    )
