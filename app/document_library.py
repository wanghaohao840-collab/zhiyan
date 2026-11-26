from __future__ import annotations

import logging
import os
import stat
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal, Mapping

from app.session import InvalidSessionError


class DocumentNotFoundError(LookupError):
    def __init__(self) -> None:
        super().__init__("document was not found")


class DocumentImportActiveError(RuntimeError):
    def __init__(self) -> None:
        super().__init__("document import is active")


class DocumentPreviewUnavailableError(RuntimeError):
    pass


class DocumentDeleteFailedError(RuntimeError):
    def __init__(self) -> None:
        super().__init__("document deletion failed")


@dataclass(frozen=True)
class DocumentLibraryItem:
    document_id: str
    name: str
    file_suffix: str
    size_bytes: int | None
    loaded_at: str | None
    status: Literal["ready"] = "ready"


@dataclass(frozen=True)
class DocumentPreviewChunk:
    chunk_id: str
    content: str
    page_number: int | None
    section: str | None


@dataclass(frozen=True)
class DocumentChunkPreview:
    document_id: str
    chunk: DocumentPreviewChunk
    context_text: str
    highlight_start: int | None
    highlight_end: int | None
    previous_chunk: DocumentPreviewChunk | None
    next_chunk: DocumentPreviewChunk | None


@dataclass(frozen=True)
class DocumentContent:
    document_id: str
    user_id: str
    name: str
    file_suffix: str
    source: Path
    source_stat: os.stat_result

    @property
    def size_bytes(self) -> int:
        return self.source_stat.st_size


class DocumentLibraryService:
    def __init__(
        self,
        session_registry,
        storage,
        import_service,
        *,
        deletion_repository=None,
        deletion_service=None,
    ) -> None:
        self.session_registry = session_registry
        self.storage = storage
        self.import_service = import_service
        self.deletion_repository = deletion_repository
        self.deletion_service = deletion_service

    def list_documents(self, session_token: str) -> tuple[DocumentLibraryItem, ...]:
        session = self.session_registry.get_session(session_token)
        user_id = str(session.user_id)
        with session.runtime.lock:
            history = session.runtime.history.load()
            items = [
                item
                for item in self.project_history_documents(user_id, history.get("documents", []))
                if (
                    self.deletion_repository is None
                    or not self.deletion_repository.has_active_fence(
                        user_id, "document", item.document_id
                    )
                )
            ]

        dated = sorted(
            (item for item in items if item.loaded_at is not None),
            key=lambda item: (
                self._parse_loaded_at(item.loaded_at),
                item.document_id,
            ),
            reverse=True,
        )
        undated = sorted(
            (item for item in items if item.loaded_at is None),
            key=lambda item: item.document_id,
        )
        return tuple((*dated, *undated))

    def project_history_documents(self, user_id: str, records: object) -> tuple[DocumentLibraryItem, ...]:
        """Shared stored-record projection, not an authorization boundary.

        Callers own authentication, deletion fences and stable source reads.
        This method does not create directories or load runtime/history state.
        """
        return tuple(
            item for record in self._latest_records(records).values()
            if (item := self._project_record(user_id, record)) is not None
        )

    def get_chunk_preview(
        self, session_token: str, document_id: str, chunk_id: str
    ) -> DocumentChunkPreview:
        session = self.session_registry.get_session(session_token)
        user_id = str(session.user_id)
        runtime = session.runtime
        with runtime.lock:
            self._preview_record(user_id, runtime, document_id)
            try:
                previous, target, following = runtime.rag_tool.get_document_preview_chunks(
                    document_id, chunk_id
                )
            except Exception as exc:
                self._require_unfenced_document(user_id, document_id)
                raise DocumentPreviewUnavailableError() from exc
            self._require_unfenced_document(user_id, document_id)

        chunk = self._preview_chunk(target, document_id)
        if chunk is None or chunk.chunk_id != chunk_id:
            raise DocumentNotFoundError()

        context_text = chunk.content
        highlight_start = highlight_end = None
        metadata = target["metadata"]
        before, after = metadata.get("preview_before"), metadata.get("preview_after")
        if isinstance(before, str) and isinstance(after, str):
            context_text = f"{before}{chunk.content}{after}"
            highlight_start = len(before)
            highlight_end = highlight_start + len(chunk.content)
        return DocumentChunkPreview(
            document_id=document_id,
            chunk=chunk,
            context_text=context_text,
            highlight_start=highlight_start,
            highlight_end=highlight_end,
            previous_chunk=self._preview_chunk(previous, document_id),
            next_chunk=self._preview_chunk(following, document_id),
        )

    def get_original_content(
        self, session_token: str, document_id: str
    ) -> DocumentContent:
        session = self.session_registry.get_session(session_token)
        user_id = str(session.user_id)
        with session.runtime.lock:
            item, record = self._preview_record(user_id, session.runtime, document_id)
            if item.file_suffix.lower() not in {".pdf", ".txt", ".md", ".markdown"}:
                raise DocumentNotFoundError()
            try:
                source = self._safe_document_path(user_id, record["document_path"])
                if not source.is_file():
                    raise DocumentNotFoundError()
                with source.open("rb") as handle:
                    source_stat = os.fstat(handle.fileno())
            except (OSError, ValueError):
                raise DocumentNotFoundError() from None
            if not stat.S_ISREG(source_stat.st_mode):
                raise DocumentNotFoundError()
            self._require_unfenced_document(user_id, document_id)
            return DocumentContent(
                document_id, user_id, item.name, item.file_suffix.lower(), source, source_stat
            )

    def read_original_content(
        self, session_token: str, content: DocumentContent, start: int, end: int
    ) -> Iterator[bytes]:
        # Release the lock and file before yielding. A slow client must not hold
        # up the deletion worker or keep a Windows source file open.
        while start < end:
            try:
                session = self.session_registry.get_session(session_token)
                if str(session.user_id) != content.user_id:
                    return
                with session.runtime.lock:
                    self._require_unfenced_document(content.user_id, content.document_id)
                    source = self._safe_document_path(content.user_id, str(content.source))
                    if not source.is_file():
                        return
                    with source.open("rb") as handle:
                        opened = os.fstat(handle.fileno())
                        original = content.source_stat
                        if (
                            not stat.S_ISREG(opened.st_mode)
                            or not os.path.samestat(opened, original)
                            or (opened.st_size, opened.st_mtime_ns)
                            != (original.st_size, original.st_mtime_ns)
                        ):
                            return
                        handle.seek(start)
                        data = handle.read(min(1024 * 1024, end - start))
                    self._require_unfenced_document(content.user_id, content.document_id)
            except (DocumentNotFoundError, InvalidSessionError, OSError, ValueError):
                return
            if not data:
                return
            start += len(data)
            yield data

    def _preview_record(self, user_id: str, runtime, document_id: str):
        self._require_unfenced_document(user_id, document_id)
        records = runtime.history.load().get("documents", [])
        record = self._latest_records(records).get(document_id)
        item = self._project_record(user_id, record) if record is not None else None
        if item is None:
            raise DocumentNotFoundError()
        return item, record

    def _require_unfenced_document(self, user_id: str, document_id: str) -> None:
        if self.deletion_repository is not None and self.deletion_repository.has_active_fence(
            user_id, "document", document_id
        ):
            raise DocumentNotFoundError()

    @staticmethod
    def _preview_chunk(raw: object, document_id: str) -> DocumentPreviewChunk | None:
        if not isinstance(raw, Mapping):
            return None
        metadata = raw.get("metadata")
        if not isinstance(metadata, Mapping) or metadata.get("document_id") != document_id:
            return None
        chunk_id = raw.get("id")
        content = raw.get("content")
        if not isinstance(chunk_id, str) or not isinstance(content, str) or not chunk_id or not content:
            return None
        page = metadata.get("page_number")
        section = metadata.get("section")
        return DocumentPreviewChunk(
            chunk_id=chunk_id,
            content=content,
            page_number=page if type(page) is int and page > 0 else None,
            section=(section.strip()[:200] or None) if isinstance(section, str) else None,
        )

    def delete_document(self, session_token: str, document_id: str):
        if self.deletion_service is not None:
            session = self.session_registry.get_session(session_token)
            if self.import_service.has_active_task_for_document(
                str(session.user_id), document_id
            ):
                raise DocumentImportActiveError()
            deletion = self.deletion_service.request_document(
                session_token, document_id
            )
            if deletion is None:
                raise DocumentNotFoundError()
            return deletion

        session = self.session_registry.get_session(session_token)
        user_id = str(session.user_id)
        self.perform_document_delete(
            user_id,
            session.runtime,
            document_id,
            assistant=session.assistant,
        )

        try:
            self.session_registry.clear_document_selection(user_id, document_id)
        except Exception:
            logger.warning("document selection invalidation failed")
            raise DocumentDeleteFailedError() from None

    def perform_document_delete(
        self,
        user_id: str,
        runtime,
        document_id: str,
        *,
        assistant=None,
        replay_if_missing: bool = False,
    ) -> None:
        temporary_assistant = None
        with runtime.lock:
            history = runtime.history.load()
            records = history.get("documents", [])
            record = self._latest_records(records).get(document_id)
            if record is None:
                if replay_if_missing:
                    return
                raise DocumentNotFoundError()
            if self._project_record(user_id, record) is None:
                raise DocumentNotFoundError()
            try:
                for related in records:
                    if (
                        isinstance(related, dict)
                        and related.get("document_id") == document_id
                    ):
                        raw_path = related["document_path"]
                        if not isinstance(raw_path, str) or not raw_path.strip():
                            raise ValueError("invalid document source path")
                        self._safe_document_path(user_id, raw_path)
            except (KeyError, OSError, TypeError, ValueError):
                logger.warning("document deletion source preflight failed")
                raise DocumentDeleteFailedError() from None
            if self.import_service.has_active_task_for_document(
                user_id, document_id
            ):
                raise DocumentImportActiveError()
            try:
                if assistant is None:
                    from assistants.pdf_learning_assistant import (
                        PDFLearningAssistant,
                    )

                    temporary_assistant = PDFLearningAssistant(
                        user_id=user_id,
                        runtime_dir=runtime.paths.root,
                        runtime=runtime,
                    )
                    assistant = temporary_assistant
                result = assistant.delete_document(document_id)
            except Exception:
                logger.warning("coordinated document deletion failed")
                raise DocumentDeleteFailedError() from None
            finally:
                if temporary_assistant is not None:
                    temporary_assistant.close()
            if (
                getattr(result, "document_id", None) != document_id
                or not isinstance(getattr(result, "documents_removed", None), int)
                or result.documents_removed < 1
                or getattr(result, "skipped_source_files", None) != 0
            ):
                logger.warning("coordinated document deletion was incomplete")
                raise DocumentDeleteFailedError()

    @staticmethod
    def _latest_records(records: object) -> dict[str, dict]:
        latest: dict[str, dict] = {}
        if not isinstance(records, list):
            return latest
        for record in records:
            if not isinstance(record, dict):
                logger.warning("skipping malformed document history record")
                continue
            document_id = record.get("document_id")
            if not isinstance(document_id, str) or not document_id.strip():
                logger.warning("skipping malformed document history record")
                continue
            latest[document_id] = record
        return latest

    def _project_record(
        self, user_id: str, record: dict
    ) -> DocumentLibraryItem | None:
        try:
            document_id = record["document_id"]
            name = record["document_name"]
            file_suffix = record["file_suffix"]
            raw_path = record["document_path"]
            if not all(
                isinstance(value, str) and value.strip()
                for value in (document_id, name, file_suffix, raw_path)
            ):
                raise ValueError("invalid document history value")
            source = self._safe_document_path(user_id, raw_path)
            normalized_name = Path(name.replace("\\", "/")).name
            if normalized_name != name or source.suffix.lower() != file_suffix.lower():
                raise ValueError("document display metadata is inconsistent")
            loaded_at = record.get("loaded_at")
            if not isinstance(loaded_at, str) or not loaded_at.strip():
                loaded_at = None
            else:
                try:
                    self._parse_loaded_at(loaded_at)
                except ValueError:
                    logger.warning("invalid document loaded_at was ignored")
                    loaded_at = None
            try:
                size_bytes = source.stat().st_size if source.is_file() else None
            except OSError:
                size_bytes = None
            return DocumentLibraryItem(
                document_id=document_id,
                name=name,
                file_suffix=file_suffix,
                size_bytes=size_bytes,
                loaded_at=loaded_at,
            )
        except (KeyError, OSError, TypeError, ValueError):
            logger.warning("skipping malformed document history record")
            return None

    @staticmethod
    def _parse_loaded_at(value: str) -> datetime:
        normalized = f"{value[:-1]}+00:00" if value.endswith(("Z", "z")) else value
        try:
            parsed = datetime.fromisoformat(normalized)
        except ValueError:
            raise ValueError("loaded_at must be ISO-8601") from None
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise ValueError("loaded_at must include a timezone")
        return parsed.astimezone(timezone.utc)

    def _safe_document_path(self, user_id: str, value: str) -> Path:
        source = Path(value)
        if not source.is_absolute():
            raise ValueError("document source path must be absolute")

        user_paths = self.storage.user_paths(user_id)
        raw_document_root = user_paths.documents
        for component in (user_paths.root.parent, user_paths.root, raw_document_root):
            self._reject_reparse_point(component)
        document_root = raw_document_root.resolve(strict=False)
        normalized = Path(os.path.abspath(source))
        try:
            relative = normalized.relative_to(document_root)
        except ValueError:
            raise ValueError("document source path is outside documents root") from None

        current = document_root
        self._reject_reparse_point(current)
        for part in relative.parts:
            current /= part
            self._reject_reparse_point(current)

        resolved = normalized.resolve(strict=False)
        if resolved != normalized:
            raise ValueError("document source path resolves unexpectedly")
        return resolved

    @staticmethod
    def _reject_reparse_point(path: Path) -> None:
        if not (path.exists() or path.is_symlink()):
            return
        attributes = getattr(path.lstat(), "st_file_attributes", 0)
        if path.is_symlink() or attributes & 0x400:
            raise ValueError("document source path contains a reparse point")


logger = logging.getLogger(__name__)
