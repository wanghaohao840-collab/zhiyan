# hello_agents/memory/rag/pipeline.py

from __future__ import annotations

import json
import os
import re
import uuid
from datetime import datetime
from itertools import islice, zip_longest
from pathlib import Path
from threading import RLock
from typing import Any, Dict, List, Mapping, Optional

from app.import_models import ProgressCallback
from hello_agents.memory.embedding import get_text_embedder, get_dimension
from hello_agents.memory.rag.contracts import DocumentSegment
from hello_agents.memory.rag.embedding_runtime import (
    CHUNKING_BY_BACKEND, RAGEmbeddingRuntime, build_rag_embedding,
)
from hello_agents.memory.rag.errors import RAGConfigError
from hello_agents.memory.rag.index_identity import IndexIdentity
from hello_agents.memory.rag.index_registry import IndexRegistry
from hello_agents.memory.rag.index_cutover import require_no_incomplete_cutover
from hello_agents.memory.rag.json_index_cache import JsonIndexCache
from hello_agents.memory.rag.prepare import (
    default_chunk_id,
    prepare_document_chunks,
    progress_with_chunking_boundary,
    report_progress,
    utc_now_iso,
)
from hello_agents.memory.storage.vector_store import InMemoryVectorStore, VectorPoint


QdrantRAGPipeline = None
_QDRANT_COLLECTION_RE = re.compile(r"^[A-Za-z0-9_-]+$")
_QDRANT_COLLECTION_MAX_LENGTH = 255
from hello_agents.memory.rag.result_utils import (
    dedupe_results_by_source,
    hybrid_rank_results,
    relevance_rerank_results,
    mmr_select,
    normalize_document_scope,
    RETRIEVAL_MODES,
    sample_evenly,
)


class SimpleRAGPipeline:
    """最小可运行 RAG 管道 + 本地 JSON 持久化

    功能：
    1. 支持 add_text
    2. 支持 search
    3. 支持 stats
    4. 支持 clear
    5. 支持本地 JSON 持久化
    """

    def __init__(
        self,
        collection_name: str = "rag_knowledge_base",
        rag_namespace: str = "default",
        qdrant_url: Optional[str] = None,
        qdrant_api_key: Optional[str] = None,
        cache_path: Optional[str] = None,
        *,
        embedding_runtime: RAGEmbeddingRuntime | None = None,
        index_registry: IndexRegistry | None = None,
    ):
        rag_namespace = _validate_rag_namespace(rag_namespace)
        self.collection_name = collection_name
        self.rag_namespace = rag_namespace
        self.qdrant_url = qdrant_url
        self.qdrant_api_key = qdrant_api_key

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
                "Managed JSON pipeline configuration requires both "
                "embedding_runtime and index_registry"
            )
        if (embedding_runtime is not None
                and embedding_runtime.profile.chunking != CHUNKING_BY_BACKEND["json"]):
            raise RAGConfigError("Managed JSON embedding backend does not match")
        self.embedding_runtime = embedding_runtime
        self.index_registry = index_registry
        self._managed = embedding_runtime is not None
        self._managed_lock = RLock()

        self.embedder = None if self._managed else get_text_embedder()
        self.dimension = (
            embedding_runtime.profile.dimension
            if embedding_runtime is not None
            else get_dimension(384)
        )

        # Internal vector store key scoped to collection + namespace.
        self._collection = f"{collection_name}__{rag_namespace}"

        # Unified in-memory backend via the VectorStore protocol.
        self._vector_store = InMemoryVectorStore()
        self._store_ready = False

        # 每一个 collection + namespace 使用一个独立 JSON 缓存文件
        legacy_cache_path = (
            Path(cache_path) if cache_path else self._default_cache_path()
        )
        self.legacy_cache_path = legacy_cache_path
        self._managed_cache: JsonIndexCache | None = None
        self.index_identity: IndexIdentity | None = None
        if self._managed:
            self.index_identity = IndexIdentity(
                "json", collection_name, embedding_runtime.profile
            )
            self._managed_cache = JsonIndexCache(
                legacy_cache_path, self.index_identity, rag_namespace
            )
            self.cache_path = self._managed_cache.path
        else:
            self.cache_path = legacy_cache_path
            self.cache_path.parent.mkdir(parents=True, exist_ok=True)

        # 启动时自动加载历史 chunks
        if self._managed:
            self._require_managed_active()
            self._vector_store = self._build_managed_store(
                self._managed_cache.load()
            )
            self._store_ready = True
        else:
            self._load_cache()

    # ── backward-compatible chunks property ────────────────────────────

    @property
    def chunks(self) -> List[Dict[str, Any]]:
        """Return all chunks as dicts (backward-compatible with tests)."""
        self._require_managed_active()
        store = self._vector_store
        points = store.scroll(
            self._collection, with_vectors=True)
        return [self._point_to_chunk(p) for p in points]

    @chunks.setter
    def chunks(self, value: List[Dict[str, Any]]) -> None:
        """Replace all chunks (used by replace_document for bulk replace)."""
        if self._managed:
            raise RAGConfigError(
                "Managed JSON pipeline rejects direct chunks assignment"
            )
        self._vector_store.delete_by_filter(self._collection)
        if value:
            points = [self._chunk_to_vector_point(c) for c in value]
            self._ensure_store_ready()
            self._vector_store.upsert(self._collection, points)

    # ── conversion helpers ─────────────────────────────────────────────

    @staticmethod
    def _point_to_chunk(point: VectorPoint) -> Dict[str, Any]:
        """Convert a VectorPoint back to the legacy chunk dict format."""
        return {
            "id": point.id,
            "document_id": point.payload.get("document_id", ""),
            "content": point.payload.get("content", ""),
            "vector": point.vector,
            "metadata": point.payload,
        }

    @staticmethod
    def _chunk_to_vector_point(chunk: Dict[str, Any]) -> VectorPoint:
        """Convert a legacy chunk dict to a VectorPoint."""
        payload = dict(chunk.get("metadata", {}) or {})
        payload.setdefault("document_id", chunk.get("document_id", ""))
        payload.setdefault("content", chunk.get("content", ""))
        return VectorPoint(
            id=str(chunk.get("id", "")),
            vector=chunk.get("vector", []),
            payload=payload,
        )

    def _ensure_store_ready(self) -> None:
        """Lazily register the collection dimension on first write.

        Tests that patch ``self.dimension`` after construction rely on this
        deferred initialisation.
        """
        if not self._store_ready:
            self._vector_store.ensure_collection(
                self._collection, self.dimension, "Cosine")
            self._store_ready = True

    def add_text(
            self,
            text: str,
            document_id: Optional[str] = None,
            metadata: Optional[Dict[str, Any]] = None,
            replace_existing: bool = True,
            save_cache: bool = True,
            progress_callback: ProgressCallback | None = None,
    ) -> Dict[str, Any]:
        """添加文本到 RAG 知识库，并可选择是否立即持久化"""

        if self._managed:
            return self._add_text_managed(
                text=text,
                document_id=document_id,
                metadata=metadata,
                replace_existing=replace_existing,
                save_cache=save_cache,
                progress_callback=progress_callback,
            )

        if not text or not text.strip():
            return {
                "success": False,
                "message": "文本为空，无法添加"
            }

        document_id = document_id or str(uuid.uuid4())
        metadata = metadata or {}

        # 同一个 document_id 默认覆盖旧版本，防止重复导入
        removed = 0
        if replace_existing:
            removed = self._remove_document_chunks(document_id)

        report_progress(progress_callback, "chunking", 0, 1, "chunking")
        chunk_texts = self._split_text(text)
        report_progress(progress_callback, "chunking", 1, 1, "chunking")

        added = 0

        existing_count = self._vector_store.count(
            self._collection,
            filters={"document_id": document_id},
        )

        points: list[VectorPoint] = []
        for index, chunk_text in enumerate(chunk_texts):
            chunk_id = f"{document_id}_{existing_count + index}"

            vector = self._to_vector(chunk_text)
            report_progress(
                progress_callback,
                "embedding",
                index + 1,
                len(chunk_texts),
                "embedding",
            )

            payload = {
                "memory_id": chunk_id,
                "document_id": document_id,
                "chunk_index": existing_count + index,
                "content": chunk_text,
                "memory_type": "rag_chunk",
                "is_rag_data": True,
                "data_source": "rag_pipeline",
                "rag_namespace": self.rag_namespace,
                "created_at": datetime.now().isoformat(),
                **metadata,
            }

            points.append(VectorPoint(id=chunk_id, vector=vector, payload=payload))
            added += 1

        persistence_steps = int(bool(points)) + int(save_cache)
        persisted = 0
        if points:
            self._ensure_store_ready()
            self._vector_store.upsert(self._collection, points)
            persisted += 1
            report_progress(
                progress_callback,
                "persisting",
                persisted,
                persistence_steps,
                "persisting",
            )

        # 关键：只有 save_cache=True 时才保存
        if save_cache:
            self._save_cache()
            persisted += 1
            report_progress(
                progress_callback,
                "persisting",
                persisted,
                persistence_steps,
                "persisting",
            )

        message = f"已添加文本知识，生成 {added} 个 chunk"
        if removed:
            message += f"，并覆盖旧 chunk {removed} 个"

        return {
            "success": True,
            "document_id": document_id,
            "chunks_added": added,
            "chunks_removed": removed,
            "cache_path": str(self.cache_path),
            "message": message,
        }

    def replace_document(
        self,
        document_id: str,
        segments: List[DocumentSegment],
        save_cache: bool = True,
        allow_empty: bool = False,
        progress_callback: ProgressCallback | None = None,
    ) -> Dict[str, Any]:
        """Replace all chunks for one document using shared preparation logic."""

        if self._managed:
            return self._replace_document_managed(
                document_id=document_id,
                segments=segments,
                save_cache=save_cache,
                allow_empty=allow_empty,
                progress_callback=progress_callback,
            )

        if not document_id:
            return {
                "success": False,
                "message": "document_id cannot be empty",
                "chunks_added": 0,
                "chunks_removed": 0,
            }

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
            embed_text=self._to_vector,
            id_for_chunk=default_chunk_id,
            progress_callback=prepare_progress,
        )
        complete_chunking()
        if not prepared and not allow_empty:
            return {
                "success": False,
                "document_id": document_id,
                "message": "document contains no non-empty chunks; existing data was preserved",
                "chunks_added": 0,
                "chunks_removed": 0,
            }

        # Collect existing metadata for version tracking.
        existing_points = self._vector_store.scroll(
            self._collection,
            filters={"document_id": document_id},
            with_vectors=True,
        )
        existing_metadata = existing_points[0].payload if existing_points else {}
        created_at = existing_metadata.get("created_at") or utc_now_iso()
        version = int(existing_metadata.get("document_version", 0)) + 1 if existing_points else 1
        updated_at = utc_now_iso()

        # Build replacement points before the commit boundary; no durable state
        # changes until the callback has allowed the replacement to proceed.
        new_points: list[VectorPoint] = []
        for chunk in prepared:
            chunk.metadata.update(
                created_at=created_at,
                updated_at=updated_at,
                document_version=version,
            )
            new_points.append(VectorPoint(
                id=chunk.id,
                vector=chunk.vector,
                payload=chunk.metadata,
            ))
        report_progress(
            progress_callback, "committing", 0, 1, "committing"
        )
        removed = self._remove_document_chunks(document_id)

        # Upsert new chunks.
        persistence_steps = int(bool(new_points)) + int(save_cache)
        persisted = 0
        if new_points:
            self._vector_store.upsert(self._collection, new_points)
            persisted += 1
            report_progress(
                progress_callback,
                "persisting",
                persisted,
                persistence_steps,
                "persisting",
            )

        if save_cache:
            self._save_cache()
            persisted += 1
            report_progress(
                progress_callback,
                "persisting",
                persisted,
                persistence_steps,
                "persisting",
            )

        return {
            "success": True,
            "document_id": document_id,
            "chunks_added": len(prepared),
            "chunks_removed": removed,
            "cache_path": str(self.cache_path),
            "message": f"Replaced document {document_id} with {len(prepared)} chunks",
        }

    def search(
            self,
            query: str,
            limit: int = 5,
            min_score: float = 0.0,
            document_id: Optional[str] = None,
            document_ids: Optional[List[str]] = None,
            **kwargs: Any
    ) -> List[Dict[str, Any]]:
        """检索 RAG 知识库，可按 document_id 过滤"""

        self._require_managed_active()
        store = self._vector_store

        if not query:
            return []

        scope = normalize_document_scope(
            document_id=document_id,
            document_ids=document_ids,
        )
        if scope == []:
            raise ValueError("document_ids cannot be empty")

        retrieval_mode = str(kwargs.pop("retrieval_mode", "vector") or "vector").strip().lower()
        if retrieval_mode not in RETRIEVAL_MODES:
            raise ValueError(f"unsupported retrieval_mode: {retrieval_mode}")
        use_mmr = kwargs.pop("use_mmr", retrieval_mode == "hybrid")
        use_rerank = kwargs.pop("use_rerank", False)
        mmr_lambda = float(kwargs.pop("mmr_lambda", 0.75))
        vector_weight = float(kwargs.pop("vector_weight", 0.7))

        query_vector = self._to_vector(query)

        filters: dict[str, Any] = {}
        if scope is not None:
            filters["document_id"] = list(scope)

        hits = store.search(
            self._collection,
            query_vector,
            filters=filters if filters else None,
            limit=max(limit * 3, limit),  # oversample for dedup/diversity
            score_threshold=min_score if min_score > 0 else None,
        )

        vector_results = [
            {
                "id": hit.id,
                "score": hit.score,
                "_vector_score": hit.score,
                "content": hit.payload.get("content", ""),
                "metadata": hit.payload,
            }
            for hit in hits
        ]

        if retrieval_mode == "hybrid":
            lexical_points = store.scroll(
                self._collection,
                filters=filters if filters else None,
                with_vectors=False,
            )
            lexical_results = [
                {
                    "id": point.id,
                    "score": 0.0,
                    "content": point.payload.get("content", ""),
                    "metadata": point.payload,
                }
                for point in lexical_points
            ]
            results = hybrid_rank_results(
                query=query,
                vector_results=vector_results,
                lexical_results=lexical_results,
                limit=max(limit * 3, limit),
                vector_weight=vector_weight,
            )
        else:
            results = vector_results

        results = dedupe_results_by_source(results, max(limit * 3, limit))
        if use_rerank:
            results = relevance_rerank_results(query, results, limit)
        if use_mmr:
            results = mmr_select(results, limit=limit, lambda_mult=mmr_lambda)
        for result in results:
            result.pop("_vector_score", None)
        return results[:limit]

    def stats(self) -> Dict[str, Any]:
        """知识库统计"""

        self._require_managed_active()
        store = self._vector_store
        points = store.scroll(
            self._collection, with_vectors=False,
            payload_fields=["document_id"],
        )
        document_ids = {p.payload.get("document_id", "") for p in points}
        chunk_count = store.count(self._collection)

        return {
            "collection_name": self.collection_name,
            "rag_namespace": self.rag_namespace,
            "document_count": len(document_ids),
            "chunk_count": chunk_count,
            "dimension": self.dimension,
            "cache_path": str(self.cache_path),
            "cache_exists": self.cache_path.exists(),
        }

    def clear(self) -> Dict[str, Any]:
        """清空知识库，并同步清空 JSON 缓存"""

        if self._managed:
            with self._managed_lock:
                self._require_managed_active()
                count = self._vector_store.count(self._collection)
                self._commit_managed([])
                return {
                    "success": True,
                    "message": f"已清空知识库，共删除 {count} 个 chunk",
                    "cache_path": str(self.cache_path),
                }

        count = self._vector_store.count(self._collection)
        self._vector_store.delete_by_filter(self._collection)

        self._save_cache()

        return {
            "success": True,
            "message": f"已清空知识库，共删除 {count} 个 chunk",
            "cache_path": str(self.cache_path),
        }

    def delete_document(self, document_id: str) -> Dict[str, Any]:
        """删除指定 document_id 的所有 chunks，并同步保存缓存"""

        if not document_id:
            return {
                "success": False,
                "message": "document_id 不能为空",
                "chunks_removed": 0,
            }

        if self._managed:
            with self._managed_lock:
                self._require_managed_active()
                current = self._managed_points()
                remaining = [
                    point for point in current
                    if point.payload.get("document_id") != document_id
                ]
                removed = len(current) - len(remaining)
                self._commit_managed(remaining)
                return {
                    "success": True,
                    "document_id": document_id,
                    "chunks_removed": removed,
                    "message": (
                        f"已删除文档 {document_id}，共删除 {removed} 个 chunk"
                    ),
                    "cache_path": str(self.cache_path),
                }

        removed = self._remove_document_chunks(document_id)

        self._save_cache()

        return {
            "success": True,
            "document_id": document_id,
            "chunks_removed": removed,
            "message": f"已删除文档 {document_id}，共删除 {removed} 个 chunk",
            "cache_path": str(self.cache_path),
        }

    def _require_managed_active(self) -> None:
        """Fail closed when a managed runtime is no longer the active identity."""

        if not self._managed:
            return
        if self.index_registry is None or self.index_identity is None:
            raise RAGConfigError("Managed JSON pipeline configuration is incomplete")
        self.index_registry.require_active(self.index_identity)

    def _build_managed_store(
        self, points: List[VectorPoint]
    ) -> InMemoryVectorStore:
        store = InMemoryVectorStore()
        store.ensure_collection(self._collection, self.dimension, "Cosine")
        if points:
            store.upsert(self._collection, points)
        return store

    def _managed_points(self) -> List[VectorPoint]:
        return self._vector_store.scroll(
            self._collection, with_vectors=True
        )

    def _commit_managed(self, points: List[VectorPoint]) -> None:
        if not self._managed or self._managed_cache is None:
            raise RAGConfigError("Managed JSON commit is unavailable")
        candidate = self._build_managed_store(points)
        self._require_managed_active()
        self._managed_cache.write(points)
        self._vector_store = candidate
        self._store_ready = True

    def _replace_document_managed(
        self,
        *,
        document_id: str,
        segments: List[DocumentSegment],
        save_cache: bool,
        allow_empty: bool,
        progress_callback: ProgressCallback | None,
    ) -> Dict[str, Any]:
        if not save_cache:
            raise RAGConfigError(
                "Managed JSON writes require save_cache=True"
            )
        if not document_id:
            return {
                "success": False,
                "message": "document_id cannot be empty",
                "chunks_added": 0,
                "chunks_removed": 0,
            }
        with self._managed_lock:
            self._require_managed_active()
            report_progress(
                progress_callback, "chunking", 0, len(segments), "chunking"
            )
            prepare_progress, complete_chunking = progress_with_chunking_boundary(
                progress_callback, len(segments), len(segments)
            )
            prepared = prepare_document_chunks(
                document_id=document_id,
                segments=segments,
                rag_namespace=self.rag_namespace,
                split_text=self._split_text,
                embed_text=None,
                id_for_chunk=default_chunk_id,
                progress_callback=prepare_progress,
                embedding_runtime=self.embedding_runtime,
            )
            complete_chunking()
            if not prepared and not allow_empty:
                return {
                    "success": False,
                    "document_id": document_id,
                    "message": (
                        "document contains no non-empty chunks; existing data was preserved"
                    ),
                    "chunks_added": 0,
                    "chunks_removed": 0,
                }

            current = self._managed_points()
            existing = [
                point for point in current
                if point.payload.get("document_id") == document_id
            ]
            existing_metadata = existing[0].payload if existing else {}
            created_at = existing_metadata.get("created_at") or utc_now_iso()
            version = (
                int(existing_metadata.get("document_version", 0)) + 1
                if existing else 1
            )
            updated_at = utc_now_iso()
            replacement = []
            for chunk in prepared:
                payload = dict(chunk.metadata)
                payload.update(
                    created_at=created_at,
                    updated_at=updated_at,
                    document_version=version,
                )
                replacement.append(VectorPoint(
                    id=chunk.id, vector=chunk.vector, payload=payload
                ))
            candidate = [
                point for point in current
                if point.payload.get("document_id") != document_id
            ] + replacement
            report_progress(
                progress_callback, "committing", 0, 1, "committing"
            )
            self._commit_managed(candidate)
            report_progress(
                progress_callback, "persisting", 1, 1, "persisting"
            )
            return {
                "success": True,
                "document_id": document_id,
                "chunks_added": len(replacement),
                "chunks_removed": len(existing),
                "cache_path": str(self.cache_path),
                "message": (
                    f"Replaced document {document_id} with {len(replacement)} chunks"
                ),
            }

    def _add_text_managed(
        self,
        *,
        text: str,
        document_id: Optional[str],
        metadata: Optional[Dict[str, Any]],
        replace_existing: bool,
        save_cache: bool,
        progress_callback: ProgressCallback | None,
    ) -> Dict[str, Any]:
        if not save_cache:
            raise RAGConfigError(
                "Managed JSON writes require save_cache=True"
            )
        if not text or not text.strip():
            return {"success": False, "message": "文本为空，无法添加"}
        document_id = document_id or str(uuid.uuid4())
        if replace_existing:
            return self._replace_document_managed(
                document_id=document_id,
                segments=[DocumentSegment(text, metadata or {})],
                save_cache=True,
                allow_empty=False,
                progress_callback=progress_callback,
            )

        with self._managed_lock:
            self._require_managed_active()
            current = self._managed_points()
            existing = [
                point for point in current
                if point.payload.get("document_id") == document_id
            ]
            start_index = max(
                (int(point.payload.get("chunk_index", -1)) for point in existing),
                default=-1,
            ) + 1

            def append_id(namespace: str, doc_id: str, index: int) -> str:
                return default_chunk_id(namespace, doc_id, start_index + index)

            prepared = prepare_document_chunks(
                document_id=document_id,
                segments=[DocumentSegment(text, metadata or {})],
                rag_namespace=self.rag_namespace,
                split_text=self._split_text,
                embed_text=None,
                id_for_chunk=append_id,
                progress_callback=progress_callback,
                embedding_runtime=self.embedding_runtime,
            )
            created_at = (
                existing[0].payload.get("created_at") if existing else None
            ) or utc_now_iso()
            version = (
                int(existing[0].payload.get("document_version", 1))
                if existing else 1
            )
            appended = []
            for offset, chunk in enumerate(prepared):
                payload = dict(chunk.metadata)
                payload.update(
                    memory_id=chunk.id,
                    chunk_index=start_index + offset,
                    created_at=created_at,
                    updated_at=utc_now_iso(),
                    document_version=version,
                )
                appended.append(VectorPoint(
                    chunk.id, chunk.vector, payload
                ))
            self._commit_managed(current + appended)
            return {
                "success": True,
                "document_id": document_id,
                "chunks_added": len(appended),
                "chunks_removed": 0,
                "cache_path": str(self.cache_path),
                "message": f"已添加文本知识，生成 {len(appended)} 个 chunk",
            }

    def _split_text(
            self,
            text: str,
            chunk_size: int = 800,
            chunk_overlap: int = 120
    ) -> List[str]:
        """按段落优先切分文本，避免固定长度硬切导致语义断裂"""

        text = text.strip()

        if not text:
            return []

        if len(text) <= chunk_size:
            return [text]

        # 1. 先按空行切段
        raw_paragraphs = []

        for part in text.replace("\r\n", "\n").split("\n\n"):
            part = part.strip()
            if part:
                raw_paragraphs.append(part)

        # 如果没有明显空行，就按单行切
        if len(raw_paragraphs) <= 1:
            raw_paragraphs = [
                line.strip()
                for line in text.split("\n")
                if line.strip()
            ]

        chunks = []
        current = ""

        # 2. 合并短段落，尽量让 chunk 接近 chunk_size
        for para in raw_paragraphs:
            if not current:
                current = para
                continue

            candidate = current + "\n\n" + para

            if len(candidate) <= chunk_size:
                current = candidate
            else:
                chunks.extend(
                    self._split_long_text_with_overlap(
                        current,
                        chunk_size=chunk_size,
                        chunk_overlap=chunk_overlap
                    )
                )
                current = para

        if current:
            chunks.extend(
                self._split_long_text_with_overlap(
                    current,
                    chunk_size=chunk_size,
                    chunk_overlap=chunk_overlap
                )
            )

        return [chunk.strip() for chunk in chunks if chunk.strip()]

    def _to_vector(self, text: str) -> List[float]:
        """文本转向量"""

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

        if len(vector) < self.dimension:
            vector.extend([0.0] * (self.dimension - len(vector)))
        elif len(vector) > self.dimension:
            vector = vector[:self.dimension]

        return vector

    def _default_cache_path(self) -> Path:
        """生成默认缓存路径"""

        safe_collection = self._safe_name(self.collection_name)
        safe_namespace = self._safe_name(self.rag_namespace)

        return Path("./knowledge_base/rag_cache") / f"{safe_collection}__{safe_namespace}.json"

    def _safe_name(self, name: str) -> str:
        """生成安全文件名"""

        name = name or "default"

        safe_chars = []

        for ch in name:
            if ch.isalnum() or ch in ["_", "-", "."]:
                safe_chars.append(ch)
            else:
                safe_chars.append("_")

        return "".join(safe_chars)

    def _load_cache(self) -> None:
        """从 JSON 加载历史 chunks"""

        if self._managed:
            raise RAGConfigError("Managed JSON cache must use strict startup loading")

        if not self.cache_path.exists():
            return

        try:
            text = self.cache_path.read_text(encoding="utf-8")
            data = json.loads(text)

            if not isinstance(data, dict):
                return

            raw_chunks = data.get("chunks", [])

            if not isinstance(raw_chunks, list):
                return

            chunks = self._normalize_loaded_chunks(raw_chunks)
            if chunks:
                points = [self._chunk_to_vector_point(c) for c in chunks]
                self._ensure_store_ready()
            self._vector_store.upsert(self._collection, points)

            print(
                f"[RAG] 已加载本地缓存: {self.cache_path}, "
                f"chunks={len(chunks)}"
            )

        except Exception as e:
            print(f"[WARNING] RAG 缓存加载失败: {self.cache_path}, error={e}")

    def _save_cache(self) -> None:
        """保存 chunks 到 JSON"""

        if self._managed:
            raise RAGConfigError(
                "Managed JSON pipeline rejects private incremental cache saves"
            )

        try:
            self.cache_path.parent.mkdir(parents=True, exist_ok=True)

            serializable = self._serializable_chunks()
            data = {
                "collection_name": self.collection_name,
                "rag_namespace": self.rag_namespace,
                "dimension": self.dimension,
                "updated_at": datetime.now().isoformat(),
                "chunk_count": len(serializable),
                "chunks": serializable,
            }

            temp_path = self.cache_path.with_suffix(self.cache_path.suffix + ".tmp")

            temp_path.write_text(
                json.dumps(data, ensure_ascii=False, indent=2),
                encoding="utf-8"
            )

            temp_path.replace(self.cache_path)

        except Exception as e:
            print(f"[WARNING] RAG 缓存保存失败: {self.cache_path}, error={e}")

    def _serializable_chunks(self) -> List[Dict[str, Any]]:
        """把 chunks 转成 JSON 可保存格式"""

        points = self._vector_store.scroll(
            self._collection, with_vectors=True)
        serializable = []

        for point in points:
            vector = point.vector
            vector = [float(x) for x in vector]

            serializable.append({
                "id": str(point.id),
                "document_id": str(point.payload.get("document_id", "")),
                "content": str(point.payload.get("content", "")),
                "vector": vector,
                "metadata": self._json_safe_dict(point.payload),
            })

        return serializable

    def _normalize_loaded_chunks(self, chunks: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """规范化从 JSON 读取的 chunks"""

        normalized = []

        for chunk in chunks:
            if not isinstance(chunk, dict):
                continue

            content = str(chunk.get("content", ""))
            if not content.strip():
                continue

            vector = chunk.get("vector", [])

            if not isinstance(vector, list) or not vector:
                vector = self._to_vector(content)

            try:
                vector = [float(x) for x in vector]
            except Exception:
                vector = self._to_vector(content)

            if len(vector) < self.dimension:
                vector.extend([0.0] * (self.dimension - len(vector)))
            elif len(vector) > self.dimension:
                vector = vector[:self.dimension]

            chunk_id = str(chunk.get("id") or str(uuid.uuid4()))
            document_id = str(chunk.get("document_id") or chunk_id)

            metadata = chunk.get("metadata", {}) or {}
            if not isinstance(metadata, dict):
                metadata = {}

            metadata.setdefault("memory_id", chunk_id)
            metadata.setdefault("document_id", document_id)
            metadata.setdefault("content", content)
            metadata.setdefault("memory_type", "rag_chunk")
            metadata.setdefault("is_rag_data", True)
            metadata.setdefault("data_source", "rag_pipeline")
            metadata.setdefault("rag_namespace", self.rag_namespace)

            normalized.append({
                "id": chunk_id,
                "document_id": document_id,
                "content": content,
                "vector": vector,
                "metadata": metadata,
            })

        return normalized

    def _json_safe_dict(self, data: Dict[str, Any]) -> Dict[str, Any]:
        """确保 metadata 可以被 JSON 保存"""

        safe = {}

        for key, value in data.items():
            key = str(key)

            if isinstance(value, (str, int, float, bool)) or value is None:
                safe[key] = value
            elif isinstance(value, (list, tuple)):
                safe[key] = [
                    item if isinstance(item, (str, int, float, bool)) or item is None else str(item)
                    for item in value
                ]
            elif isinstance(value, dict):
                safe[key] = self._json_safe_dict(value)
            else:
                safe[key] = str(value)

        return safe

    def _remove_document_chunks(self, document_id: str) -> int:
        """删除指定 document_id 的旧 chunks"""

        if self._managed:
            raise RAGConfigError(
                "Managed JSON pipeline rejects private incremental deletes"
            )

        return self._vector_store.delete_by_filter(
            self._collection,
            filters={"document_id": document_id},
        )

    def _split_long_text_with_overlap(
            self,
            text: str,
            chunk_size: int = 800,
            chunk_overlap: int = 120
    ) -> List[str]:
        """对超长文本做带 overlap 的切分"""

        text = text.strip()

        if len(text) <= chunk_size:
            return [text]

        chunks = []
        start = 0

        while start < len(text):
            end = start + chunk_size
            chunk = text[start:end].strip()

            if chunk:
                chunks.append(chunk)

            if end >= len(text):
                break

            start = max(0, end - chunk_overlap)

        return chunks

    def get_document_summary_context(
            self,
            document_id: str,
            limit: int = 12
    ) -> List[Dict[str, Any]]:
        """为全文总结类问题获取更合适的上下文

        先按页取样，余量按页轮流补充后续片段。
        """

        self._require_managed_active()
        store = self._vector_store

        if not document_id:
            return []

        points = store.scroll(
            self._collection,
            filters={"document_id": document_id},
            with_vectors=False,
        )
        doc_chunks = [self._point_to_chunk(p) for p in points]

        if not doc_chunks:
            return []

        def get_page_number(chunk):
            metadata = chunk.get("metadata", {}) or {}
            page = metadata.get("page_number", 999999)

            try:
                return int(page)
            except Exception:
                return 999999

        def original_position(chunk):
            return get_page_number(chunk), (chunk.get("metadata") or {}).get("chunk_index", 0)

        doc_chunks.sort(key=original_position)
        by_page = {}
        for chunk in doc_chunks:
            by_page.setdefault(get_page_number(chunk), []).append(chunk)

        selected = sample_evenly([chunks[0] for chunks in by_page.values()], limit)
        if len(by_page) < limit:
            round_robin = (
                chunk
                for group in zip_longest(*(chunks[1:] for chunks in by_page.values()))
                for chunk in group
                if chunk is not None
            )
            selected.extend(islice(round_robin, limit - len(selected)))
            selected.sort(key=original_position)

        results = []

        for chunk in selected[:limit]:
            results.append({
                "id": chunk.get("id"),
                "score": 1.0,
                "content": chunk.get("content", ""),
                "metadata": chunk.get("metadata", {}) or {},
            })

        return results

    def get_document_chunk(self, document_id: str, chunk_id: str, chunk_index: int):
        """Read one exact logical chunk from the namespace's resident store."""
        self._require_managed_active()
        points = self._vector_store.scroll(
            self._collection,
            filters={"document_id": document_id, "_id": [chunk_id], "chunk_index": chunk_index},
            with_vectors=False,
        )
        if len(points) != 1:
            return None
        return self._point_to_chunk(points[0])

    def get_document_chunks(
            self,
            document_id: str
    ) -> List[Dict[str, Any]]:
        """Return every chunk for exactly one document in stable order."""

        self._require_managed_active()
        store = self._vector_store

        document_id = str(document_id or "").strip()
        if not document_id:
            raise ValueError("document_id is required")

        points = store.scroll(
            self._collection,
            filters={"document_id": document_id},
            with_vectors=False,
        )
        chunks = [self._point_to_chunk(point) for point in points]
        chunks.sort(
            key=lambda chunk: (
                int((chunk.get("metadata") or {}).get("chunk_index", 0)),
                str(chunk.get("id") or ""),
            )
        )
        return chunks

    def list_document_ids(self) -> List[str]:
        """Return document IDs in this RAG namespace."""

        self._require_managed_active()
        store = self._vector_store
        points = store.scroll(
            self._collection,
            with_vectors=False,
            payload_fields=["document_id"],
        )
        return sorted({
            str(point.payload.get("document_id"))
            for point in points
            if point.payload.get("document_id")
        })

def index_chunks(
    store=None,
    chunks: Optional[List[Dict[str, Any]]] = None,
    cache_db: Optional[str] = None,
    batch_size: int = 64,
    rag_namespace: str = "default"
) -> None:
    """兼容旧版 index_chunks 调用"""

    if not chunks:
        print("[RAG] No chunks to index")
        return

    if store is None:
        store = create_rag_pipeline(rag_namespace=rag_namespace)

    for chunk in chunks:
        content = chunk.get("content", "")
        metadata = {
            k: v
            for k, v in chunk.items()
            if k != "content"
        }

        if hasattr(store, "add_text"):
            store.add_text(
                text=content,
                document_id=metadata.get("document_id"),
                metadata=metadata,
            )


def _prompt_mqe(query: str, n: int) -> List[str]:
    """查询扩展兜底"""

    return [query]


def _prompt_hyde(query: str) -> Optional[str]:
    """HyDE 兜底"""

    return None


def search_vectors_expanded(
    store=None,
    query: str = "",
    top_k: int = 8,
    rag_namespace: Optional[str] = None,
    only_rag_data: bool = True,
    score_threshold: Optional[float] = None,
    enable_mqe: bool = False,
    mqe_expansions: int = 2,
    enable_hyde: bool = False,
    candidate_pool_multiplier: int = 4,
) -> List[Dict[str, Any]]:
    """兼容旧版增强检索调用"""

    if not query:
        return []

    if store is None:
        store = create_rag_pipeline(rag_namespace=rag_namespace or "default")

    if hasattr(store, "search"):
        return store.search(
            query=query,
            limit=top_k,
            min_score=score_threshold or 0.0,
        )

    return []


def resolve_rag_backend(backend: Optional[str] = None) -> str:
    value = (backend or os.getenv("RAG_BACKEND") or "json").strip().lower()
    if value not in {"json", "qdrant"}:
        raise RAGConfigError(f"Unsupported RAG_BACKEND: {value}")
    return value


def resolve_qdrant_collection(collection_name: Optional[str] = None) -> str:
    value = (os.getenv("QDRANT_COLLECTION") or collection_name or "rag_knowledge_base").strip()
    if not value:
        raise RAGConfigError("Qdrant collection name cannot be empty")
    if len(value) > _QDRANT_COLLECTION_MAX_LENGTH:
        raise RAGConfigError(
            f"Qdrant collection name cannot exceed {_QDRANT_COLLECTION_MAX_LENGTH} characters"
        )
    if not _QDRANT_COLLECTION_RE.fullmatch(value):
        raise RAGConfigError(
            "Qdrant collection name may contain only letters, numbers, underscores, and hyphens"
        )
    return value


def _validate_rag_namespace(rag_namespace: str) -> str:
    value = str(rag_namespace or "").strip()
    if not value:
        raise RAGConfigError("rag_namespace cannot be empty")
    return value


def resolve_rag_embedding_dependencies(
    *,
    backend: str,
    data_root: Path | str | None,
    values: Mapping[str, str | None] | None = None,
    transport: Any = None,
) -> tuple[RAGEmbeddingRuntime | None, IndexRegistry | None]:
    runtime = build_rag_embedding(
        os.environ if values is None else values,
        backend=backend,
        transport=transport,
    )
    registry = None
    if data_root is not None:
        root = Path(data_root)
        if not root.is_absolute():
            raise RAGConfigError("RAG registry checks require an absolute data_root")
        registry = IndexRegistry(root)
        require_no_incomplete_cutover(registry)
    if runtime.profile.provider == "simple":
        if registry is not None:
            if registry.path.exists():
                records = registry.load().values()
                if any(record.identity.profile.provider != "simple"
                       and record.state in {"active", "failed"} for record in records):
                    raise RAGConfigError("RAG embedding downgrade requires a validated rollback")
        return None, None
    if data_root is None:
        raise RAGConfigError(
            "Managed RAG embedding requires an explicit absolute data_root"
        )
    assert registry is not None
    return runtime, registry


def create_rag_pipeline(
    qdrant_url: Optional[str] = None,
    qdrant_api_key: Optional[str] = None,
    collection_name: str = "rag_knowledge_base",
    rag_namespace: str = "default",
    cache_path: Optional[str] = None,
    backend: Optional[str] = None,
    qdrant_client: Any = None,
    data_root: Path | str | None = None,
    embedding_values: Mapping[str, str | None] | None = None,
    embedding_transport: Any = None,
    **kwargs
) -> Any:
    selected_backend = resolve_rag_backend(backend)
    rag_namespace = _validate_rag_namespace(rag_namespace)
    embedding_runtime, index_registry = resolve_rag_embedding_dependencies(
        backend=selected_backend,
        data_root=data_root,
        values=embedding_values,
        transport=embedding_transport,
    )

    if selected_backend == "qdrant":
        resolved_url = qdrant_url or os.getenv("QDRANT_URL")
        if qdrant_client is None and kwargs.get("vector_store") is None and not resolved_url:
            raise RAGConfigError("QDRANT_URL is required when RAG_BACKEND=qdrant")

        global QdrantRAGPipeline
        if QdrantRAGPipeline is None:
            from hello_agents.memory.rag.qdrant_pipeline import QdrantRAGPipeline as ImportedQdrantRAGPipeline

            QdrantRAGPipeline = ImportedQdrantRAGPipeline

        return QdrantRAGPipeline(
            collection_name=resolve_qdrant_collection(collection_name),
            rag_namespace=rag_namespace,
            qdrant_url=resolved_url,
            qdrant_api_key=qdrant_api_key or os.getenv("QDRANT_API_KEY") or None,
            qdrant_client=qdrant_client,
            embedding_runtime=embedding_runtime,
            index_registry=index_registry,
            **kwargs,
        )

    return SimpleRAGPipeline(
        collection_name=collection_name,
        rag_namespace=rag_namespace,
        qdrant_url=qdrant_url,
        qdrant_api_key=qdrant_api_key,
        cache_path=cache_path,
        embedding_runtime=embedding_runtime,
        index_registry=index_registry,
    )
