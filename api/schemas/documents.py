from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.document_library import DocumentChunkPreview, DocumentLibraryItem


class DocumentResponse(BaseModel):
    document_id: str
    name: str
    file_suffix: str
    size_bytes: int | None
    loaded_at: str | None
    status: Literal["ready"]


class DocumentListResponse(BaseModel):
    items: list[DocumentResponse]


def document_response(item: DocumentLibraryItem) -> DocumentResponse:
    return DocumentResponse(
        document_id=item.document_id,
        name=item.name,
        file_suffix=item.file_suffix,
        size_bytes=item.size_bytes,
        loaded_at=item.loaded_at,
        status=item.status,
    )


class DocumentPreviewChunkResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    chunk_id: str
    content: str
    page_number: int | None
    section: str | None


class DocumentChunkPreviewResponse(DocumentPreviewChunkResponse):
    document_id: str
    context_text: str
    highlight_start: int | None = Field(description="Inclusive Unicode code point index in context_text.")
    highlight_end: int | None = Field(description="Exclusive Unicode code point index in context_text.")
    previous_chunk: DocumentPreviewChunkResponse | None
    next_chunk: DocumentPreviewChunkResponse | None


def chunk_preview_response(preview: DocumentChunkPreview) -> DocumentChunkPreviewResponse:
    return DocumentChunkPreviewResponse(
        document_id=preview.document_id,
        chunk_id=preview.chunk.chunk_id,
        content=preview.chunk.content,
        page_number=preview.chunk.page_number,
        section=preview.chunk.section,
        context_text=preview.context_text,
        highlight_start=preview.highlight_start,
        highlight_end=preview.highlight_end,
        previous_chunk=preview.previous_chunk,
        next_chunk=preview.next_chunk,
    )
