from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

from app.import_models import ProgressCallback
from hello_agents.memory.embedding import get_dimension, get_text_embedder
from hello_agents.memory.rag.contracts import DocumentSegment
from hello_agents.memory.rag.embedding_runtime import CHUNKING_BY_BACKEND, RAGEmbeddingRuntime
from hello_agents.memory.rag.errors import (
    RAGConfigError,
    RAGDocumentTooLargeError,
    RAGEmbeddingError,
)
from hello_agents.memory.rag.index_identity import (
    IndexIdentity, IndexIdentityError, require_point_identity,
)
from hello_agents.memory.rag.index_registry import IndexRegistry
from hello_agents.memory.rag.prepare import (
    contains_secret_metadata,
    prepare_document_chunks,
    progress_with_chunking_boundary,
    qdrant_point_id,
    report_progress,
    utc_now_iso,
)
from hello_agents.memory.rag.result_utils import (
    RETRIEVAL_MODES,
    dedupe_results_by_source,
    hybrid_rank_results,
    relevance_rerank_results,
    mmr_select,
    sample_evenly,
)
from hello_agents.memory.storage.vector_store import (
    QdrantVectorStore,
    VectorPoint,
    VectorStore,
)
class RAGPipeline:
    """Document/chunk business semantics backed by a VectorStore."""

    DEFAULT_RETRY_DELAYS = (0.5, 1.0, 2.0)
    MAX_SUMMARY_CHUNKS = 10000
    COLLECTION_NAME_RE = re.compile(r"^[A-Za-z0-9_-]+$")
    COLLECTION_NAME_MAX_LENGTH = 255

    def __init__(
        self,
        collection_name: str = "rag_knowledge_base",
        rag_namespace: str = "default",
        qdrant_url: Optional[str] = None,
        qdrant_api_key: Optional[str] = None,
        qdrant_client: Any = None,
        vector_store: Optional[VectorStore] = None,
        retry_delays: Optional[tuple[float, ...]] = None,
        max_summary_chunks: int = MAX_SUMMARY_CHUNKS,
        embedding_runtime: RAGEmbeddingRuntime | None = None,
        index_registry: IndexRegistry | None = None,
        **kwargs,
    ):
        collection_name = str(collection_name or "").strip()
        rag_namespace = str(rag_namespace or "").strip()
        if not collection_name:
            raise RAGConfigError("Qdrant collection name cannot be empty")
        if len(collection_name) > self.COLLECTION_NAME_MAX_LENGTH:
            raise RAGConfigError(
                f"Qdrant collection name cannot exceed {self.COLLECTION_NAME_MAX_LENGTH} characters"
            )
        if not self.COLLECTION_NAME_RE.fullmatch(collection_name):
            raise RAGConfigError(
                "Qdrant collection name may contain only letters, numbers, underscores, and hyphens"
            )
        if not rag_namespace:
            raise RAGConfigError("rag_namespace cannot be empty")
        if (
            (embedding_runtime is None) != (index_registry is None)
            or (
                embedding_runtime is not None
                and not isinstance(embedding_runtime, RAGEmbeddingRuntime)
            )
            or (
                index_registry is not None
                and not isinstance(index_registry, IndexRegistry)
            )
        ):
            raise RAGConfigError(
                "Managed Qdrant pipeline configuration requires both "
                "embedding_runtime and index_registry"
            )
        if (embedding_runtime is not None
                and embedding_runtime.profile.chunking != CHUNKING_BY_BACKEND["qdrant"]):
            raise RAGConfigError("Managed Qdrant embedding backend does not match")
        self.base_collection_name = collection_name
        self.embedding_runtime = embedding_runtime
        self.index_registry = index_registry
        self._managed = embedding_runtime is not None
        self._managed_failed = False
        self.index_identity = (
            IndexIdentity("qdrant", collection_name, embedding_runtime.profile)
            if embedding_runtime is not None else None
        )
        self.collection_name = (
            self.index_identity.physical_collection
            if self.index_identity is not None else collection_name
        )
        self.rag_namespace = rag_namespace
        self.qdrant_url = qdrant_url
        self.qdrant_api_key = qdrant_api_key
        self.retry_delays = self.DEFAULT_RETRY_DELAYS if retry_delays is None else tuple(retry_delays)
        self.max_summary_chunks = int(max_summary_chunks)
        self.embedder = None if self._managed else get_text_embedder()
        self.dimension = (
            embedding_runtime.profile.dimension
            if embedding_runtime is not None else get_dimension(384)
        )
        self.vector_store = vector_store or QdrantVectorStore(
            url=qdrant_url,
            api_key=qdrant_api_key,
            client=qdrant_client,
            retry_delays=self.retry_delays,
        )
        if self._managed:
            self._require_managed_active()
            self.vector_store.require_collection(
                self.collection_name, self.dimension, "Cosine"
            )
        else:
            self.vector_store.ensure_collection(
                self.collection_name,
                self.dimension,
                "Cosine",
            )
            self.vector_store.ensure_payload_indexes(
                self.collection_name,
                {
                    "rag_namespace": "keyword",
                    "document_id": "keyword",
                    "chunk_index": "integer",
                },
            )

    def add_text(
        self,
        text: str,
        document_id: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
        replace_existing: bool = True,
        save_cache: bool = True,
        progress_callback: ProgressCallback | None = None,
    ) -> Dict[str, Any]:
        self._require_managed_active()
        if not text or not text.strip():
            return {"success": False, "message": "text is empty", "chunks_added": 0, "chunks_removed": 0}
        if not document_id:
            import uuid

            document_id = str(uuid.uuid4())
        if replace_existing:
            return self.replace_document(
                document_id,
                [DocumentSegment(text, metadata or {})],
                progress_callback=progress_callback,
            )

        existing_count = self._max_chunk_index(document_id) + 1
        report_progress(progress_callback, "chunking", 0, 1, "chunking")
        prepare_progress, complete_chunking = progress_with_chunking_boundary(
            progress_callback,
            1,
            1,
        )
        prepared = prepare_document_chunks(
            document_id=document_id,
            segments=[DocumentSegment(text, metadata or {})],
            rag_namespace=self.rag_namespace,
            split_text=self._split_text,
            embed_text=None if self._managed else self._to_vector,
            id_for_chunk=lambda ns, doc, index: qdrant_point_id(ns, doc, existing_count + index),
            progress_callback=prepare_progress,
            embedding_runtime=self.embedding_runtime,
        )
        complete_chunking()
        for chunk in prepared:
            chunk.metadata["chunk_index"] = existing_count + int(chunk.metadata["chunk_index"])
        self._upsert_chunks(prepared, progress_callback=progress_callback)
        return {
            "success": True,
            "document_id": document_id,
            "chunks_added": len(prepared),
            "chunks_removed": 0,
            "message": f"Added {len(prepared)} chunks",
        }

    def replace_document(
        self,
        document_id: str,
        segments: List[DocumentSegment],
        save_cache: bool = True,
        allow_empty: bool = False,
        progress_callback: ProgressCallback | None = None,
    ) -> Dict[str, Any]:
        self._require_managed_active()
        if not document_id:
            return {"success": False, "message": "document_id cannot be empty", "chunks_added": 0, "chunks_removed": 0}

        report_progress(progress_callback, "chunking", 0, len(segments), "chunking")
        prepare_progress, complete_chunking = progress_with_chunking_boundary(
            progress_callback,
            len(segments),
            len(segments),
        )
        prepared = prepare_document_chunks(
            document_id=document_id,
            segments=segments,
            rag_namespace=self.rag_namespace,
            split_text=self._split_text,
            embed_text=None if self._managed else self._to_vector,
            id_for_chunk=qdrant_point_id,
            progress_callback=prepare_progress,
            embedding_runtime=self.embedding_runtime,
        )
        complete_chunking()
        existing_payloads = self._scroll_payloads(document_id=document_id)
        old_count = len(existing_payloads)
        if not prepared and not allow_empty:
            return {
                "success": False,
                "document_id": document_id,
                "message": "document contains no non-empty chunks; existing data was preserved",
                "chunks_added": 0,
                "chunks_removed": 0,
            }
        existing = existing_payloads[0] if existing_payloads else {}
        created_at = existing.get("created_at") or utc_now_iso()
        version = int(existing.get("document_version", 0)) + 1 if existing else 1
        updated_at = utc_now_iso()
        for chunk in prepared:
            chunk.metadata.update(
                created_at=created_at,
                updated_at=updated_at,
                document_version=version,
            )
        report_progress(
            progress_callback, "committing", 0, 1, "committing"
        )
        self._upsert_chunks(prepared, progress_callback=progress_callback)
        try:
            self._delete_orphan_chunks(document_id, len(prepared))
        except Exception:
            self._mark_managed_failed()
            raise
        return {
            "success": True,
            "document_id": document_id,
            "chunks_added": len(prepared),
            "chunks_removed": old_count,
            "message": f"Replaced document {document_id} with {len(prepared)} chunks",
        }

    def search(
        self,
        query: str,
        limit: int = 5,
        min_score: float = 0.0,
        document_id: Optional[str] = None,
        document_ids: Optional[List[str]] = None,
        **kwargs,
    ) -> List[Dict[str, Any]]:
        self._require_managed_active()
        if not query:
            return []

        document_scope = self._normalize_document_scope(document_id, document_ids)
        if document_scope == []:
            raise ValueError("document_ids cannot be empty")

        retrieval_mode = str(kwargs.pop("retrieval_mode", "vector") or "vector").strip().lower()
        if retrieval_mode not in RETRIEVAL_MODES:
            raise ValueError(f"unsupported retrieval_mode: {retrieval_mode}")
        use_mmr = kwargs.pop("use_mmr", retrieval_mode == "hybrid")
        use_rerank = kwargs.pop("use_rerank", False)
        rerank_callback = kwargs.pop("rerank_callback", None)
        mmr_lambda = float(kwargs.pop("mmr_lambda", 0.75))
        vector_weight = float(kwargs.pop("vector_weight", 0.7))
        candidate_limit = max(limit * 3, limit)

        hits = self.vector_store.search(
            self.collection_name,
            self._to_vector(query),
            filters=self._scope_filter(
                document_id=document_id,
                document_ids=document_scope,
            ),
            limit=candidate_limit,
            score_threshold=min_score,
        )
        vector_results = []
        for hit in hits:
            payload = hit.payload
            metadata = self._result_metadata(payload)
            self._validate_managed_metadata(
                metadata, document_id=document_id, document_ids=document_scope
            )
            vector_results.append(
                {
                    "id": hit.id,
                    "score": hit.score,
                    "_vector_score": hit.score,
                    "content": payload.get("content", ""),
                    "metadata": metadata,
                }
            )

        if retrieval_mode == "hybrid":
            lexical_points = self.vector_store.scroll(
                self.collection_name,
                filters=self._scope_filter(
                    document_id=document_id,
                    document_ids=document_scope,
                ),
            )
            for point in lexical_points:
                self._validate_managed_metadata(
                    self._result_metadata(point.payload),
                    document_id=document_id, document_ids=document_scope,
                )
            lexical_results = [
                {
                    "id": point.id,
                    "score": 0.0,
                    "content": point.payload.get("content", ""),
                    "metadata": self._result_metadata(point.payload),
                }
                for point in lexical_points
            ]
            results = hybrid_rank_results(
                query=query,
                vector_results=vector_results,
                lexical_results=lexical_results,
                limit=candidate_limit,
                vector_weight=vector_weight,
            )
        else:
            results = vector_results

        results = dedupe_results_by_source(results, limit=candidate_limit)
        if use_rerank:
            if results and rerank_callback is not None:
                rerank_callback(len(results))
            results = relevance_rerank_results(query, results, limit)
        if use_mmr:
            results = mmr_select(results, limit=limit, lambda_mult=mmr_lambda)
        for result in results:
            result.pop("_vector_score", None)
        return results[:limit]

    def stats(self) -> Dict[str, Any]:
        self._require_managed_active()
        documents = set()
        offset = None
        while True:
            batch = self.vector_store.scroll(
                self.collection_name,
                filters=self._scope_filter(),
                payload_fields=None if self._managed else ["document_id"],
            )
            for point in batch:
                payload = point.payload
                self._validate_managed_metadata(
                    self._result_metadata(payload),
                    document_id=payload.get("document_id"),
                )
                document_id = payload.get("document_id")
                if document_id:
                    documents.add(document_id)
            break

        return {
            "collection_name": self.collection_name,
            "rag_namespace": self.rag_namespace,
            "document_count": len(documents),
            "chunk_count": self._count(),
            "dimension": self.dimension,
            "backend": "qdrant",
            "cache_path": None,
            "cache_exists": None,
        }

    def delete_document(self, document_id: str) -> Dict[str, Any]:
        self._require_managed_active()
        if not document_id:
            return {"success": False, "message": "document_id cannot be empty", "chunks_removed": 0}
        try:
            removed = self.vector_store.delete_by_filter(
                self.collection_name,
                self._scope_filter(document_id=document_id),
            )
        except Exception:
            self._mark_managed_failed()
            raise
        return {
            "success": True,
            "document_id": document_id,
            "chunks_removed": removed,
            "message": f"Deleted document {document_id}, removed {removed} chunks",
        }

    def clear(self) -> Dict[str, Any]:
        self._require_managed_active()
        try:
            removed = self.vector_store.delete_by_filter(
                self.collection_name,
                self._scope_filter(),
            )
        except Exception:
            self._mark_managed_failed()
            raise
        return {"success": True, "chunks_removed": removed, "message": f"Cleared {removed} chunks"}

    def get_document_summary_context(self, document_id: str, limit: int = 12) -> List[Dict[str, Any]]:
        self._require_managed_active()
        chunk_count = self._count(document_id=document_id)
        if chunk_count > self.max_summary_chunks:
            raise RAGDocumentTooLargeError(
                f"Document {document_id} has {chunk_count} chunks; summary limit is {self.max_summary_chunks}"
            )
        chunks = self._scroll_payloads(document_id=document_id)
        chunks.sort(key=lambda payload: int(payload.get("chunk_index", 0)))
        results = []
        for payload in sample_evenly(chunks, limit):
            results.append(
                {
                    "id": payload.get("id", ""),
                    "score": 1.0,
                    "content": payload.get("content", ""),
                    "metadata": self._result_metadata(payload),
                }
            )
        return results

    def get_document_chunk(self, document_id: str, chunk_id: str, chunk_index: int):
        """Resolve one source with a bounded, namespace-scoped payload read."""
        self._require_managed_active()
        page = self.vector_store.scroll_page(
            self.collection_name,
            filters={**self._scope_filter(document_id=document_id), "chunk_index": chunk_index},
            page_size=2,
        )
        if page.next_offset is not None or len(page.records) != 1:
            return None
        record = page.records[0]
        payload = record.payload
        if (record.logical_id != chunk_id or payload.get("document_id") != document_id
                or payload.get("rag_namespace") != self.rag_namespace
                or payload.get("chunk_index") != chunk_index):
            return None
        return self._scoped_document_chunk(record, document_id)

    def get_document_preview_chunks(self, document_id: str, chunk_id: str):
        """Resolve one exact chunk and at most two neighboring payloads."""
        self._require_managed_active()
        scope = self._scope_filter(document_id=document_id)
        page = self.vector_store.scroll_page(
            self.collection_name, filters={**scope, "_id": [chunk_id]}, page_size=2,
        )
        if page.next_offset is not None or len(page.records) != 1:
            return None, None, None
        record = page.records[0]
        target = self._scoped_document_chunk(record, document_id)
        if target is None or record.logical_id != chunk_id:
            return None, None, None
        index = record.payload.get("chunk_index")
        if type(index) is not int or index < 0:
            return None, target, None
        page = self.vector_store.scroll_page(
            self.collection_name,
            filters={**scope, "chunk_index": [index - 1, index + 1]},
            page_size=3,
        )
        if page.next_offset is not None or len(page.records) > 2:
            return None, target, None

        def adjacent(position: int):
            matches = [record for record in page.records if record.payload.get("chunk_index") == position]
            return self._scoped_document_chunk(matches[0], document_id) if len(matches) == 1 else None

        return adjacent(index - 1), target, adjacent(index + 1)

    def _scoped_document_chunk(self, record, document_id: str):
        payload = record.payload
        if (payload.get("document_id") != document_id
                or payload.get("rag_namespace") != self.rag_namespace):
            return None
        metadata = self._result_metadata(payload)
        self._validate_managed_metadata(metadata, document_id=document_id)
        return {"id": record.logical_id, "content": str(payload.get("content") or ""), "metadata": metadata}

    def get_document_chunks(self, document_id: str) -> List[Dict[str, Any]]:
        """Return every chunk for exactly one document in stable order."""

        self._require_managed_active()
        document_id = str(document_id or "").strip()
        if not document_id:
            raise ValueError("document_id is required")
        payloads = self._scroll_payloads(document_id=document_id)
        payloads.sort(
            key=lambda payload: (
                int(payload.get("chunk_index", 0)),
                str(payload.get("id") or ""),
            )
        )
        return [
            {
                "id": str(payload.get("id") or ""),
                "document_id": document_id,
                "content": str(payload.get("content") or ""),
                "metadata": self._result_metadata(payload),
            }
            for payload in payloads
        ]

    def list_document_ids(self) -> List[str]:
        """Return document IDs in this RAG namespace."""

        self._require_managed_active()
        return sorted({
            str(payload.get("document_id"))
            for payload in self._scroll_payloads()
            if payload.get("document_id")
        })

    def _upsert_chunks(
        self,
        chunks,
        progress_callback: ProgressCallback | None = None,
    ) -> None:
        points: list[VectorPoint] = []
        for chunk in chunks:
            metadata = dict(chunk.metadata)
            self._validate_managed_metadata(
                metadata, document_id=chunk.document_id
            )
            payload = {
                "content": chunk.content,
                "document_id": chunk.document_id,
                "rag_namespace": self.rag_namespace,
                "chunk_index": int(metadata.pop("chunk_index", 0)),
                "created_at": metadata.pop("created_at", None),
                "updated_at": metadata.pop("updated_at", None),
                "document_version": metadata.pop("document_version", 1),
                "metadata": metadata,
            }
            for duplicate in ("content", "document_id", "rag_namespace"):
                payload["metadata"].pop(duplicate, None)
            points.append(VectorPoint(chunk.id, chunk.vector, payload))

        if points:
            self._require_managed_active()
            try:
                self.vector_store.upsert(self.collection_name, points)
            except Exception:
                self._mark_managed_failed()
                raise
            report_progress(progress_callback, "persisting", 1, 1, "persisting")

    def _split_text(self, text: str, chunk_size: int = 800, chunk_overlap: int = 120) -> List[str]:
        text = text.strip()
        if not text:
            return []
        if len(text) <= chunk_size:
            return [text]
        chunks = []
        start = 0
        while start < len(text):
            end = start + chunk_size
            chunks.append(text[start:end].strip())
            if end >= len(text):
                break
            start = max(0, end - chunk_overlap)
        return [chunk for chunk in chunks if chunk]

    def _to_vector(self, text: str) -> List[float]:
        if self._managed:
            self._require_managed_active()
            vector = self.embedding_runtime.embed_query(text)
            self._require_managed_active()
            return vector
        vector = self.embedder.encode(text)
        if hasattr(vector, "tolist"):
            vector = vector.tolist()
        if isinstance(vector, list) and vector and isinstance(vector[0], list):
            vector = vector[0]
        vector = [float(x) for x in vector]
        if len(vector) != self.dimension:
            raise RAGEmbeddingError(
                f"Embedding dimension {len(vector)} does not match expected {self.dimension}"
            )
        return vector

    def _require_managed_active(self) -> None:
        if not self._managed:
            return
        if self._managed_failed:
            raise RAGConfigError("Managed Qdrant index has failed closed")
        if self.index_registry is None or self.index_identity is None:
            raise RAGConfigError("Managed Qdrant pipeline configuration is incomplete")
        self.index_registry.require_active(self.index_identity)

    def _validate_managed_metadata(
        self, metadata: Dict[str, Any], *, document_id: object = None,
        document_ids: Optional[List[str]] = None,
    ) -> None:
        if not self._managed:
            return
        if contains_secret_metadata(metadata):
            raise IndexIdentityError("point")
        if document_ids is not None and metadata.get("document_id") not in document_ids:
            raise IndexIdentityError("point")
        require_point_identity(
            metadata,
            identity=self.index_identity,
            rag_namespace=self.rag_namespace,
            document_id=(None if document_ids is not None or document_id is None
                         else str(document_id)),
        )

    def _mark_managed_failed(self) -> None:
        if not self._managed:
            return
        self._managed_failed = True
        if (
            self.index_registry is None
            or self.index_identity is None
        ):
            raise RAGConfigError(
                "Managed Qdrant mutation failed and registry invalidation was unavailable"
            )
        self.index_registry.mark_failed_if_active(self.index_identity)

    def _scope_filter(
        self,
        document_id: Optional[str] = None,
        document_ids: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        filters: Dict[str, Any] = {"rag_namespace": self.rag_namespace}
        if document_ids is not None:
            filters["document_id"] = document_ids
        elif document_id:
            filters["document_id"] = document_id
        return filters

    def _normalize_document_scope(
        self,
        document_id: Optional[str] = None,
        document_ids: Optional[List[str]] = None,
    ) -> Optional[List[str]]:
        if document_ids is None:
            return None

        scoped: List[str] = []
        seen = set()
        for value in document_ids:
            text = str(value or "").strip()
            if not text or text in seen:
                continue
            seen.add(text)
            scoped.append(text)

        return scoped

    def _count(self, document_id: Optional[str] = None) -> int:
        return self.vector_store.count(
            self.collection_name,
            self._scope_filter(document_id=document_id),
        )

    def _scroll_payloads(self, document_id: Optional[str] = None) -> List[Dict[str, Any]]:
        payloads = []
        for point in self.vector_store.scroll(
            self.collection_name,
            filters=self._scope_filter(document_id=document_id),
        ):
            payload = dict(point.payload)
            payload.setdefault("id", point.id)
            self._validate_managed_metadata(
                self._result_metadata(payload),
                document_id=document_id,
            )
            payloads.append(payload)
        return payloads

    def _max_chunk_index(self, document_id: str) -> int:
        indexes = [
            int(payload.get("chunk_index", -1))
            for payload in self._scroll_payloads(document_id=document_id)
        ]
        return max(indexes, default=-1)

    def _delete_orphan_chunks(self, document_id: str, new_chunk_count: int) -> None:
        self._require_managed_active()
        orphan_ids = []
        for payload in self._scroll_payloads(document_id=document_id):
            if int(payload.get("chunk_index", -1)) >= new_chunk_count:
                point_id = payload.get("id")
                if point_id:
                    orphan_ids.append(str(point_id))
        if orphan_ids:
            self.vector_store.delete_by_filter(
                self.collection_name,
                {**self._scope_filter(document_id=document_id), "_id": orphan_ids},
            )

    def _result_metadata(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        metadata = dict(payload.get("metadata", {}) or {})
        metadata.update(
            document_id=payload.get("document_id"),
            rag_namespace=payload.get("rag_namespace"),
            chunk_index=payload.get("chunk_index"),
            content=payload.get("content", ""),
            created_at=payload.get("created_at"),
            updated_at=payload.get("updated_at"),
            document_version=payload.get("document_version"),
        )
        return metadata


QdrantRAGPipeline = RAGPipeline
