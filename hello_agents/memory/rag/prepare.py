from __future__ import annotations

import json
import logging
import uuid
from collections.abc import Callable, Sequence
from datetime import datetime, timezone
from typing import Any

from app.import_models import ProgressCallback
from hello_agents.memory.rag.contracts import DocumentSegment, PreparedChunk
from hello_agents.memory.rag.embedding_profile import EmbeddingFailure
from hello_agents.memory.rag.embedding_runtime import RAGEmbeddingRuntime, validate_texts


PROJECT_POINT_NAMESPACE_UUID = uuid.UUID("c273c00a-40ac-47a9-b475-164f135ada18")
logger = logging.getLogger(__name__)
_PREVIEW_CONTEXT_CHARS = 350
_PREVIEW_LOOKAHEAD_CHARS = 4096
_SECRET_METADATA_KEYS = {
    "apikey", "authorization", "accesstoken", "refreshtoken", "secret",
    "password", "token",
}


def is_secret_metadata_key(key: object) -> bool:
    normalized = "".join(
        character for character in str(key).strip().lower()
        if character.isalnum()
    )
    return (
        normalized in _SECRET_METADATA_KEYS
        or normalized.endswith("apikey")
        or normalized.endswith("password")
        or normalized.endswith("secret")
    )


def contains_secret_metadata(value: object) -> bool:
    if isinstance(value, dict):
        return any(
            is_secret_metadata_key(key) or contains_secret_metadata(item)
            for key, item in value.items()
        )
    if isinstance(value, (list, tuple)):
        return any(contains_secret_metadata(item) for item in value)
    return False


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def default_chunk_id(rag_namespace: str, document_id: str, chunk_index: int) -> str:
    return f"{document_id}_{chunk_index}"


def qdrant_point_id(rag_namespace: str, document_id: str, chunk_index: int) -> str:
    point_name = canonical_json([rag_namespace, document_id, chunk_index])
    return str(uuid.uuid5(PROJECT_POINT_NAMESPACE_UUID, point_name))


def json_safe_value(value: Any) -> Any:
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if isinstance(value, (list, tuple)):
        return [json_safe_value(item) for item in value]
    if isinstance(value, dict):
        return json_safe_dict(value)
    return str(value)


def json_safe_dict(data: dict[str, Any]) -> dict[str, Any]:
    return {
        str(key): json_safe_value(value)
        for key, value in data.items()
        if not is_secret_metadata_key(key)
    }


def normalize_vector(vector: Any) -> list[float]:
    if hasattr(vector, "tolist"):
        vector = vector.tolist()
    if isinstance(vector, list) and vector and isinstance(vector[0], list):
        vector = vector[0]
    return [float(item) for item in vector]


def report_progress(
    callback: ProgressCallback | None,
    stage: str,
    done: int,
    total: int,
    message: str,
) -> None:
    """Report best-effort progress without allowing UI callbacks to break imports."""

    if callback is None:
        return
    try:
        callback(stage, done, total, message)
    except Exception:
        logger.warning("RAG progress callback failed", exc_info=True)


def progress_with_chunking_boundary(
    callback: ProgressCallback | None,
    done: int,
    total: int,
) -> tuple[ProgressCallback | None, Callable[[], None]]:
    """Forward progress live after reporting chunking completion exactly once."""

    chunking_completed = False

    def complete_chunking() -> None:
        nonlocal chunking_completed
        if chunking_completed:
            return
        chunking_completed = True
        report_progress(callback, "chunking", done, total, "chunking")

    def forward(stage: str, current: int, stage_total: int, message: str) -> None:
        if stage == "embedding":
            complete_chunking()
        report_progress(callback, stage, current, stage_total, message)

    return (forward if callback is not None else None), complete_chunking


def prepare_document_chunks(
    document_id: str,
    segments: Sequence[DocumentSegment],
    rag_namespace: str,
    split_text: Callable[[str], list[str]],
    embed_text: Callable[[str], list[float]] | None,
    id_for_chunk: Callable[[str, str, int], str] | None = None,
    progress_callback: ProgressCallback | None = None,
    *,
    embedding_runtime: RAGEmbeddingRuntime | None = None,
) -> list[PreparedChunk]:
    if not document_id:
        raise ValueError("document_id is required")
    if not isinstance(rag_namespace, str) or not rag_namespace.strip():
        raise ValueError("rag_namespace is required")
    if ((embedding_runtime is None and not callable(embed_text))
            or (embedding_runtime is not None and embed_text is not None)):
        raise EmbeddingFailure("configuration")

    id_for_chunk = id_for_chunk or qdrant_point_id
    pending: list[tuple[dict[str, Any], str]] = []
    for segment in segments:
        if not segment.content or not segment.content.strip():
            continue
        segment_metadata = json_safe_dict(segment.metadata or {})
        segment_metadata.pop("preview_before", None)
        segment_metadata.pop("preview_after", None)
        scan_start = 0
        for chunk_text in split_text(segment.content):
            chunk_text = str(chunk_text).strip()
            if not chunk_text:
                continue
            metadata = segment_metadata
            # Chunks can overlap, and the JSON splitter sometimes normalizes
            # whitespace. Record context only for an exact, unambiguous match.
            scan_end = scan_start + len(chunk_text) + _PREVIEW_LOOKAHEAD_CHARS
            start = segment.content.find(chunk_text, scan_start, scan_end)
            if start != -1 and segment.content.find(chunk_text, start + 1, scan_end) == -1:
                end = start + len(chunk_text)
                metadata = {
                    **segment_metadata,
                    "preview_before": segment.content[max(0, start - _PREVIEW_CONTEXT_CHARS):start],
                    "preview_after": segment.content[end:end + _PREVIEW_CONTEXT_CHARS],
                }
                scan_start = start + 1
            pending.append((metadata, chunk_text))

    total_chunks = len(pending)
    vectors: list[list[float]] = []
    if embedding_runtime is not None:
        texts = [text for _, text in pending]
        validate_texts(texts)
        for start in range(0, total_chunks, embedding_runtime.batch_size):
            batch = texts[start:start + embedding_runtime.batch_size]
            vectors.extend(embedding_runtime.embed_documents(batch))
            report_progress(progress_callback, "embedding", len(vectors),
                            total_chunks, "embedding")

    prepared: list[PreparedChunk] = []
    for chunk_index, (segment_metadata, chunk_text) in enumerate(pending):
        chunk_id = id_for_chunk(rag_namespace, document_id, chunk_index)
        now = utc_now_iso()
        metadata = {
            **segment_metadata,
            "memory_id": chunk_id,
            "document_id": document_id,
            "chunk_index": chunk_index,
            "content": chunk_text,
            "memory_type": "rag_chunk",
            "is_rag_data": True,
            "data_source": "rag_pipeline",
            "rag_namespace": rag_namespace,
            "created_at": now,
            "updated_at": now,
            "document_version": 1,
        }
        metadata.pop("_vector_store_id", None)
        metadata.pop("embedding_fingerprint", None)
        if embedding_runtime is not None:
            metadata["embedding_fingerprint"] = embedding_runtime.profile.fingerprint
            vector = vectors[chunk_index]
        else:
            vector = normalize_vector(embed_text(chunk_text))
        prepared.append(PreparedChunk(
            id=chunk_id, document_id=document_id, content=chunk_text,
            vector=vector, metadata=json_safe_dict(metadata),
        ))
        if embedding_runtime is None:
            report_progress(progress_callback, "embedding", chunk_index + 1,
                            total_chunks, "embedding")
    return prepared
