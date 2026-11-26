from __future__ import annotations

import re
import math
from dataclasses import dataclass
from threading import BoundedSemaphore, Lock
from typing import Literal, Mapping
from uuid import UUID, uuid4


class DocumentSearchError(RuntimeError):
    code = "SEARCH_FAILED"
    retryable = False


class DocumentSearchValidationError(ValueError):
    code = "SEARCH_VALIDATION_ERROR"
    retryable = False


class DocumentSearchScopeError(DocumentSearchError):
    code = "SEARCH_SCOPE_INVALID"


class DocumentSearchScopeChangedError(DocumentSearchError):
    code = "SEARCH_SCOPE_CHANGED"


class DocumentSearchBusyError(DocumentSearchError):
    code = "SEARCH_BUSY"
    retryable = True


class DocumentSearchUnavailableError(DocumentSearchError):
    code = "SEARCH_UNAVAILABLE"
    retryable = True


class DocumentSearchSourceStaleError(DocumentSearchError):
    code = "SEARCH_SOURCE_STALE"


@dataclass(frozen=True)
class SearchChunkLocator:
    document_id: str
    chunk_id: str
    chunk_index: int
    content_sha256: str
    page_number: int | None = None

    def __post_init__(self) -> None:
        _require_uuid(self.document_id)
        if not self.chunk_id or len(self.chunk_id) > 256:
            raise DocumentSearchValidationError("chunk_id is invalid")
        if self.chunk_index < 0:
            raise DocumentSearchValidationError("chunk_index is invalid")
        if not re.fullmatch(r"[0-9a-f]{64}", self.content_sha256):
            raise DocumentSearchValidationError("content_sha256 is invalid")


@dataclass(frozen=True)
class DocumentSearchRequest:
    query: str
    document_ids: tuple[str, ...]
    limit: Literal[5, 10, 20] = 10

    def __post_init__(self) -> None:
        query = str(self.query or "").strip()
        if not 1 <= len(query) <= 1000:
            raise DocumentSearchValidationError("query is invalid")
        if not 1 <= len(self.document_ids) <= 10:
            raise DocumentSearchValidationError("document scope is invalid")
        if len(set(self.document_ids)) != len(self.document_ids):
            raise DocumentSearchValidationError("document scope contains duplicates")
        for document_id in self.document_ids:
            _require_uuid(document_id)
        if self.limit not in {5, 10, 20}:
            raise DocumentSearchValidationError("limit is invalid")
        object.__setattr__(self, "query", query)


@dataclass(frozen=True)
class DocumentSearchHit:
    document_id: str
    document_name: str
    excerpt: str
    rank: int
    score: float
    page_number: int | None
    section: str | None
    locator: SearchChunkLocator


@dataclass(frozen=True)
class DocumentSearchResult:
    request_id: str
    document_ids: tuple[str, ...]
    result_count: int
    results: tuple[DocumentSearchHit, ...]


@dataclass(frozen=True)
class ResolvedDocumentChunk:
    document_id: str
    document_name: str
    content: str
    page_number: int | None
    section: str | None
    locator: SearchChunkLocator


class DocumentSearchService:
    def __init__(self, sessions, documents, *, max_concurrent: int = 4) -> None:
        self.sessions = sessions
        self.documents = documents
        self._slots = BoundedSemaphore(max_concurrent)
        self._active_users: set[str] = set()
        self._active_lock = Lock()

    def search(self, token: str, request: DocumentSearchRequest) -> DocumentSearchResult:
        session = self.sessions.get_session(token)
        self._claim(str(session.user_id))
        try:
            names = self._document_names(token, request.document_ids)
            result = self._execute(session,
                "search", query=request.query, document_ids=list(request.document_ids),
                limit=request.limit, min_score=0.08,
            )
            if not result.success:
                raise DocumentSearchUnavailableError(result.error_code or "search failed")
            self._require_unchanged_scope(token, request.document_ids, names)
            raw_results = result.data.get("results")
            if not isinstance(raw_results, list):
                raise DocumentSearchUnavailableError("structured results missing")
            hits = tuple(
                self._hit(item, names, rank)
                for rank, item in enumerate(raw_results[: request.limit], start=1)
            )
            return DocumentSearchResult(
                request_id=str(uuid4()), document_ids=request.document_ids,
                result_count=len(hits), results=hits,
            )
        finally:
            self._release(str(session.user_id))

    def resolve_chunk(self, token: str, locator: SearchChunkLocator) -> ResolvedDocumentChunk:
        session = self.sessions.get_session(token)
        self._claim(str(session.user_id))
        try:
            names = self._document_names(token, (locator.document_id,))
            result = self._execute(session,
                "get_document_chunk", document_id=locator.document_id,
                chunk_id=locator.chunk_id, chunk_index=locator.chunk_index,
            )
            chunk = result.data.get("chunk") if result.success else None
            if not result.success and "chunk" not in result.data:
                raise DocumentSearchUnavailableError("source backend unavailable")
            if not isinstance(chunk, Mapping):
                raise DocumentSearchSourceStaleError("chunk is missing")
            self._require_unchanged_scope(token, (locator.document_id,), names)
            if (chunk.get("content_sha256") != locator.content_sha256
                    or chunk.get("document_id") != locator.document_id
                    or chunk.get("chunk_id") != locator.chunk_id
                    or chunk.get("chunk_index") != locator.chunk_index):
                raise DocumentSearchSourceStaleError("chunk content changed")
            normalized = self._normalized_hit(chunk, names)
            return ResolvedDocumentChunk(
                document_id=locator.document_id,
                document_name=names[locator.document_id],
                content=str(chunk.get("content") or "")[:1200],
                page_number=normalized["page_number"],
                section=normalized["section"],
                locator=locator,
            )
        finally:
            self._release(str(session.user_id))

    @staticmethod
    def _execute(session, action: str, **kwargs):
        try:
            result = session.runtime.rag_tool.execute_result(action, **kwargs)
            if not isinstance(result.data, Mapping) or type(result.success) is not bool:
                raise ValueError("invalid backend envelope")
            return result
        except Exception as exc:
            raise DocumentSearchUnavailableError("search backend unavailable") from exc

    def _claim(self, user_id: str) -> None:
        with self._active_lock:
            if user_id in self._active_users:
                raise DocumentSearchBusyError("user search already active")
            self._active_users.add(user_id)
        if not self._slots.acquire(blocking=False):
            with self._active_lock:
                self._active_users.discard(user_id)
            raise DocumentSearchBusyError("search capacity reached")

    def _release(self, user_id: str) -> None:
        with self._active_lock:
            was_active = user_id in self._active_users
            self._active_users.discard(user_id)
        if was_active:
            self._slots.release()

    def _document_names(self, token: str, requested: tuple[str, ...]) -> dict[str, str]:
        available = {item.document_id: item.name for item in self.documents.list_documents(token)}
        if any(document_id not in available for document_id in requested):
            raise DocumentSearchScopeError("document scope is unavailable")
        return {document_id: available[document_id] for document_id in requested}

    def _require_unchanged_scope(
        self, token: str, requested: tuple[str, ...], before: dict[str, str]
    ) -> None:
        try:
            after = self._document_names(token, requested)
        except DocumentSearchScopeError as exc:
            raise DocumentSearchScopeChangedError("document scope changed") from exc
        if after != before:
            raise DocumentSearchScopeChangedError("document scope changed")

    def _hit(
        self, raw: object, names: dict[str, str], rank: int
    ) -> DocumentSearchHit:
        if not isinstance(raw, Mapping):
            raise DocumentSearchUnavailableError("invalid structured result")
        normalized = self._normalized_hit(raw, names)
        try:
            locator = SearchChunkLocator(
                document_id=normalized["document_id"], chunk_id=normalized["chunk_id"],
                chunk_index=normalized["chunk_index"],
                content_sha256=normalized["content_sha256"],
                page_number=normalized["page_number"],
            )
            score = float(raw.get("score", 0.0))
            if not math.isfinite(score):
                raise ValueError("non-finite score")
        except (ValueError, TypeError) as exc:
            raise DocumentSearchUnavailableError("invalid structured result") from exc
        return DocumentSearchHit(
            document_id=locator.document_id,
            document_name=names[locator.document_id],
            excerpt=str(raw.get("content") or "").strip()[:1200], rank=rank,
            score=score,
            page_number=normalized["page_number"], section=normalized["section"],
            locator=locator,
        )

    @staticmethod
    def _normalized_hit(raw: Mapping, names: dict[str, str]) -> dict:
        document_id = str(raw.get("document_id") or "")
        if document_id not in names:
            raise DocumentSearchUnavailableError("result escaped document scope")
        try:
            chunk_index = int(raw.get("chunk_index"))
            page = raw.get("page_number")
            page_number = int(page) if page not in (None, "") else None
            if chunk_index < 0 or (page_number is not None and page_number < 1):
                raise ValueError("invalid source position")
        except (TypeError, ValueError) as exc:
            raise DocumentSearchUnavailableError("invalid result locator") from exc
        section = str(raw.get("section") or "").strip()[:200] or None
        return {
            "document_id": document_id,
            "chunk_id": str(raw.get("chunk_id") or ""),
            "chunk_index": chunk_index,
            "content_sha256": str(raw.get("content_sha256") or ""),
            "page_number": page_number,
            "section": section,
        }


def _require_uuid(value: str) -> None:
    try:
        if str(UUID(str(value))) != str(value).lower():
            raise ValueError
    except (ValueError, AttributeError, TypeError) as exc:
        raise DocumentSearchValidationError("document_id is invalid") from exc
