from __future__ import annotations

import re
from typing import Annotated
from urllib.parse import quote
from uuid import UUID

from fastapi import APIRouter, Depends, Path, Request, Response, status
from fastapi.responses import StreamingResponse

from api.dependencies import (
    get_csrf_validated_session,
    get_current_session,
    get_document_library_service,
    get_session_token,
)
from api.errors import error_response
from api.schemas.documents import (
    DocumentChunkPreviewResponse,
    DocumentListResponse,
    chunk_preview_response,
    document_response,
)
from api.schemas.qa import QaDeletionResponse, deletion_response
from app.document_library import (
    DocumentDeleteFailedError,
    DocumentImportActiveError,
    DocumentLibraryService,
    DocumentNotFoundError,
    DocumentPreviewUnavailableError,
)
from app.session import UserSession


router = APIRouter(prefix="/api/v1/documents", tags=["documents"])
_PREVIEW_CACHE_CONTROL = "private, no-store"


def _preview_error(status_code: int, code: str, message: str, *, retryable: bool = False):
    response = error_response(status_code, code, message, retryable=retryable)
    response.headers["Cache-Control"] = _PREVIEW_CACHE_CONTROL
    response.headers["X-Content-Type-Options"] = "nosniff"
    return response


@router.get("", response_model=DocumentListResponse)
def list_documents(
    request: Request,
    _session: Annotated[UserSession, Depends(get_current_session)],
    service: Annotated[
        DocumentLibraryService,
        Depends(get_document_library_service),
    ],
) -> DocumentListResponse:
    return DocumentListResponse(
        items=[
            document_response(item)
            for item in service.list_documents(get_session_token(request))
        ]
    )


@router.get("/{document_id}/chunks/{chunk_id}", response_model=DocumentChunkPreviewResponse)
def get_chunk_preview(
    document_id: Annotated[str, Path(min_length=1, max_length=200)],
    chunk_id: Annotated[str, Path(min_length=1, max_length=256)],
    request: Request,
    response: Response,
    _session: Annotated[UserSession, Depends(get_current_session)],
    service: Annotated[DocumentLibraryService, Depends(get_document_library_service)],
):
    try:
        chunk = service.get_chunk_preview(get_session_token(request), document_id, chunk_id)
    except DocumentNotFoundError:
        return _preview_error(status.HTTP_404_NOT_FOUND, "document_not_found", "文档或片段不存在")
    except DocumentPreviewUnavailableError:
        return _preview_error(
            status.HTTP_503_SERVICE_UNAVAILABLE, "document_preview_unavailable",
            "文档预览暂不可用", retryable=True,
        )
    response.headers["Cache-Control"] = _PREVIEW_CACHE_CONTROL
    return chunk_preview_response(chunk)


@router.get(
    "/{document_id}/content",
    response_class=StreamingResponse,
    responses={
        200: {"content": {"application/pdf": {}, "text/plain": {}}},
        206: {"description": "Partial content"},
        416: {"description": "Range not satisfiable"},
    },
)
def get_document_content(
    document_id: Annotated[str, Path(min_length=1, max_length=200)],
    request: Request,
    _session: Annotated[UserSession, Depends(get_current_session)],
    service: Annotated[DocumentLibraryService, Depends(get_document_library_service)],
):
    token = get_session_token(request)
    try:
        content = service.get_original_content(token, document_id)
    except DocumentNotFoundError:
        return _preview_error(status.HTTP_404_NOT_FOUND, "document_not_found", "文档原文不存在")

    size = content.size_bytes
    headers = {
        "Accept-Ranges": "bytes",
        "Cache-Control": _PREVIEW_CACHE_CONTROL,
        "Content-Disposition": f"inline; filename*=UTF-8''{quote(content.name, safe='')}",
        "Content-Type": "application/pdf" if content.file_suffix == ".pdf" else "text/plain",
        "X-Content-Type-Options": "nosniff",
    }
    start, end = 0, size
    status_code = status.HTTP_200_OK
    ranges = request.headers.getlist("Range")
    if ranges and "If-Range" not in request.headers:
        try:
            start, end = _parse_byte_range(ranges, size)
        except ValueError:
            response = _preview_error(
                416,
                "document_range_not_satisfiable",
                "请求的文档范围不可用",
            )
            response.headers["Content-Range"] = f"bytes */{size}"
            return response
        status_code = status.HTTP_206_PARTIAL_CONTENT
        headers["Content-Range"] = f"bytes {start}-{end - 1}/{size}"

    headers["Content-Length"] = str(end - start)
    return StreamingResponse(
        service.read_original_content(token, content, start, end),
        status_code=status_code,
        headers=headers,
    )


def _parse_byte_range(values: list[str], size: int) -> tuple[int, int]:
    # PDF readers issue one byte range per request. Bound integer parsing and
    # reject multi-range requests without allocating a multipart response.
    match = re.fullmatch(r"bytes=([0-9]{0,20})-([0-9]{0,20})", values[0].strip(), re.IGNORECASE)
    if len(values) != 1 or match is None or size == 0:
        raise ValueError("invalid byte range")
    first, last = match.groups()
    if first:
        start, end = int(first), min(int(last) + 1, size) if last else size
    elif last:
        start, end = max(0, size - int(last)), size
    else:
        raise ValueError("empty byte range")
    if start >= end:
        raise ValueError("unsatisfiable byte range")
    return start, end


@router.delete(
    "/{document_id}",
    response_model=QaDeletionResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
def delete_document(
    document_id: UUID,
    request: Request,
    _session: Annotated[UserSession, Depends(get_csrf_validated_session)],
    service: Annotated[
        DocumentLibraryService,
        Depends(get_document_library_service),
    ],
):
    try:
        deletion = service.delete_document(
            get_session_token(request), str(document_id)
        )
    except DocumentNotFoundError:
        return error_response(
            status.HTTP_404_NOT_FOUND,
            "document_not_found",
            "文档不存在",
        )
    except DocumentImportActiveError:
        return error_response(
            status.HTTP_409_CONFLICT,
            "document_import_active",
            "文档仍在导入中，请稍后重试",
        )
    except DocumentDeleteFailedError:
        return error_response(
            status.HTTP_500_INTERNAL_SERVER_ERROR,
            "document_delete_failed",
            "文档删除失败，请重试",
            retryable=True,
        )
    return deletion_response(deletion)
