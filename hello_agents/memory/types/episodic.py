from __future__ import annotations

import json
import math
from datetime import datetime
from typing import Any, Dict, List, Optional, Set

from hello_agents.memory.base import MemoryConfig, MemoryItem, Episode
from hello_agents.memory.memory_embedding import (
    get_memory_embedder as create_embedding_model_with_fallback, memory_vector_collection,
)
from hello_agents.memory.rag.embedding_profile import EmbeddingFailure
from hello_agents.memory.storage.document_store import SQLiteDocumentStore
from hello_agents.memory.storage.qdrant_store import QdrantConnectionManager
from hello_agents.memory.storage.vector_store import VectorPoint, VectorRange, VectorStore


class EpisodicMemory:
    """情景记忆实现

    特点：
    - SQLite + Qdrant 混合存储
    - SQLite 保存结构化内容和上下文
    - Qdrant 保存向量，支持语义检索
    - 支持 session_id、时间范围、重要性过滤
    """

    def __init__(self, config: MemoryConfig, storage_backend=None):
        self.config = config
        self.storage_backend = storage_backend

        self.doc_store = SQLiteDocumentStore(config.database_path)

        self.embedder = create_embedding_model_with_fallback()
        self.vector_collection = memory_vector_collection(
            getattr(config, "qdrant_collection", "hello_agents_vectors"), self.embedder,
        )
        dimension = getattr(self.embedder, "dimension", config.qdrant_vector_size)
        self.vector_store: VectorStore = storage_backend or QdrantConnectionManager.get_instance(
            qdrant_url=getattr(config, "qdrant_url", None),
            qdrant_api_key=getattr(config, "qdrant_api_key", None),
            collection_name=self.vector_collection,
            vector_size=dimension,
            tenant_id=getattr(config, "tenant_id", None),
            rag_namespace=getattr(config, "rag_namespace", None),
        )
        self.vector_store.ensure_collection(
            self.vector_collection,
            dimension,
        )
        self.vector_store.ensure_payload_indexes(
            self.vector_collection,
            {
                "memory_type": "keyword",
                "user_id": "keyword",
                "session_id": "keyword",
                "importance": "float",
                "timestamp": "datetime",
            },
        )

        self.sessions: Dict[str, List[str]] = {}
        self._episodes: Dict[str, Episode] = {}
        self._normalized_timestamp_users: Set[str] = set()

    def close(self) -> None:
        if hasattr(self, "doc_store") and self.doc_store is not None:
            self.doc_store.close()
            self.doc_store = None

    def add(self, memory_item: MemoryItem) -> str:
        """添加情景记忆"""

        episode = Episode(
            episode_id=memory_item.id,
            session_id=memory_item.metadata.get("session_id", "default"),
            timestamp=self._canonical_timestamp(memory_item.timestamp),
            content=memory_item.content,
            context={
                **memory_item.metadata,
                "memory_type": "episodic",
                "importance": memory_item.importance,
                "content": memory_item.content
            }
        )

        # Remote failures must be reported before mutating the memory caches.
        self._persist_episode(episode)
        previous = self._episodes.get(episode.episode_id)
        if previous is not None:
            previous_ids = self.sessions.get(previous.session_id, [])
            self.sessions[previous.session_id] = [
                episode_id
                for episode_id in previous_ids
                if episode_id != episode.episode_id
            ]

        if episode.session_id not in self.sessions:
            self.sessions[episode.session_id] = []

        if episode.episode_id not in self.sessions[episode.session_id]:
            self.sessions[episode.session_id].append(episode.episode_id)
        self._episodes[episode.episode_id] = episode

        return memory_item.id

    def retrieve(self, query: str, limit: int = 5, **kwargs) -> List[MemoryItem]:
        """混合检索：结构化过滤 + 语义向量检索"""

        if not query:
            return []

        candidate_ids = self._structured_filter(**kwargs)

        hits = self._vector_search(
            query=query,
            limit=max(limit * 5, 20),
            user_id=kwargs.get("user_id"),
            session_id=kwargs.get("session_id"),
            min_importance=kwargs.get("min_importance", 0.0),
            start_time=kwargs.get("start_time"),
            end_time=kwargs.get("end_time"),
        )

        results = []

        for hit in hits:
            if self._should_include(hit, candidate_ids, kwargs):
                score = self._calculate_episode_score(hit)
                memory_item = self._create_memory_item(hit)
                results.append((score, memory_item))

        results.sort(key=lambda x: x[0], reverse=True)
        return [item for _, item in results[:limit]]

    def forget(
        self,
        strategy: str = "importance_based",
        threshold: float = 0.1,
        max_age_days: int = 30
    ) -> int:
        """遗忘情景记忆"""

        before_count = len(self._episodes)
        previous_ids = set(self._episodes)
        keep: Dict[str, Episode] = {}

        for episode_id, episode in self._episodes.items():
            context = episode.context or {}
            importance = float(context.get("importance", 0.5))

            should_forget = False

            if strategy == "importance_based":
                should_forget = importance < threshold

            elif strategy == "time_based":
                age_days = self._calculate_age_days(episode.timestamp)
                should_forget = age_days > max_age_days

            elif strategy == "capacity_based":
                should_forget = importance < threshold

            if not should_forget:
                keep[episode_id] = episode

        removed_ids = sorted(previous_ids - set(keep))
        if removed_ids:
            self._delete_episode_ids(removed_ids)

        self._episodes = keep

        self.sessions = {}
        for episode in self._episodes.values():
            self.sessions.setdefault(episode.session_id, []).append(episode.episode_id)

        return before_count - len(self._episodes)

    def clear(self) -> None:
        """清空情景记忆"""

        episode_ids = list(self._episodes)
        if episode_ids:
            self._delete_episode_ids(episode_ids)
        self.sessions.clear()
        self._episodes.clear()

    def remove(self, memory_id: str) -> bool:
        """Remove one exact episode across SQLite, vector and in-memory state."""

        episode = self._episodes.get(memory_id)
        if episode is None:
            return False
        self._delete_episode_ids([memory_id])
        self._episodes.pop(memory_id, None)
        remaining = [
            episode_id
            for episode_id in self.sessions.get(episode.session_id, [])
            if episode_id != memory_id
        ]
        if remaining:
            self.sessions[episode.session_id] = remaining
        else:
            self.sessions.pop(episode.session_id, None)
        return True

    def _delete_episode_ids(self, episode_ids: List[str]) -> None:
        """Delete both durable copies, restoring SQLite if cleanup is interrupted."""

        snapshots = {}
        for episode_id in episode_ids:
            document = self.doc_store.get_document(episode_id)
            if document is not None:
                snapshots[episode_id] = document

        try:
            for episode_id in episode_ids:
                self.doc_store.delete_document(episode_id)

            self.vector_store.delete_by_filter(
                self.vector_collection,
                {"_id": episode_ids},
            )
        except Exception as cleanup_error:
            rollback_errors = []
            for episode_id, document in snapshots.items():
                try:
                    self.doc_store.add_document(
                        doc_id=episode_id,
                        content=document["content"],
                        metadata=document["metadata"],
                    )
                except Exception as rollback_error:
                    rollback_errors.append(f"{episode_id}: {rollback_error}")

            if rollback_errors:
                details = "; ".join(rollback_errors)
                raise RuntimeError(
                    "Episodic cleanup failed and SQLite rollback was incomplete: "
                    f"{details}"
                ) from cleanup_error
            raise

    def count(self) -> int:
        """返回情景记忆数量"""

        return len(self._episodes)

    def _persist_episode(self, episode: Episode) -> None:
        """持久化情景记忆到 SQLite 和 Qdrant"""

        metadata = {
            "memory_id": episode.episode_id,
            "episode_id": episode.episode_id,
            "session_id": episode.session_id,
            "timestamp": episode.timestamp,
            "memory_type": "episodic",
            "content": episode.content,
            **(episode.context or {})
        }

        embedding = self.embedder.encode(episode.content)
        if hasattr(embedding, "tolist"):
            embedding = embedding.tolist()
        if isinstance(embedding, list) and embedding and isinstance(embedding[0], list):
            embedding = embedding[0]
        embedding = [float(x) for x in embedding]

        # Publish vectors before recording success in SQLite or memory.
        self.vector_store.upsert(
            self.vector_collection,
            [VectorPoint(episode.episode_id, embedding, metadata)],
        )

        try:
            if hasattr(self.doc_store, "add_document"):
                self.doc_store.add_document(
                    doc_id=episode.episode_id,
                    content=episode.content,
                    metadata=json.dumps(metadata, ensure_ascii=False)
                )
        except Exception as e:
            print(f"[WARNING] SQLite 保存情景记忆失败: {e}")

    def _structured_filter(self, **kwargs) -> Optional[Set[str]]:
        """结构化过滤"""

        if not self._episodes:
            return None

        session_id = kwargs.get("session_id")
        start_time = kwargs.get("start_time")
        end_time = kwargs.get("end_time")
        min_importance = kwargs.get("min_importance", 0.0)
        user_id = kwargs.get("user_id")

        candidate_ids: Set[str] = set()

        for episode_id, episode in self._episodes.items():
            context = episode.context or {}

            if session_id and episode.session_id != session_id:
                continue

            if user_id and context.get("user_id") != user_id:
                continue

            importance = float(context.get("importance", 0.5))
            if importance < min_importance:
                continue

            episode_time = self._parse_timestamp(episode.timestamp)

            if start_time:
                if episode_time < self._parse_timestamp(start_time):
                    continue

            if end_time:
                if episode_time > self._parse_timestamp(end_time):
                    continue

            candidate_ids.add(episode_id)

        return candidate_ids

    def _vector_search(
        self,
        query: str,
        limit: int = 20,
        user_id: Optional[str] = None,
        session_id: Optional[str] = None,
        min_importance: float = 0.0,
        start_time: Any = None,
        end_time: Any = None,
    ) -> List[Dict[str, Any]]:
        """向量检索"""

        where = {"memory_type": "episodic"}

        if user_id:
            where["user_id"] = user_id

        if session_id:
            where["session_id"] = session_id

        where["importance"] = VectorRange(gte=float(min_importance))

        if start_time or end_time:
            where["timestamp"] = VectorRange(
                gte=self._parse_timestamp(start_time) if start_time else None,
                lte=self._parse_timestamp(end_time) if end_time else None,
            )

        try:
            if user_id and (start_time or end_time):
                self._normalize_remote_timestamps(user_id)

            query_vector = self.embedder.encode(query)

            if hasattr(query_vector, "tolist"):
                query_vector = query_vector.tolist()

            if isinstance(query_vector, list) and query_vector and isinstance(query_vector[0], list):
                query_vector = query_vector[0]

            query_vector = [float(x) for x in query_vector]

            hits = self.vector_store.search(
                self.vector_collection,
                query_vector,
                filters=where,
                limit=limit,
            )

            if hits:
                return [
                    {
                        "id": hit.id,
                        "score": hit.score,
                        "metadata": hit.payload,
                    }
                    for hit in hits
                ]

        except EmbeddingFailure:
            raise
        except Exception as e:
            print(f"[WARNING] Qdrant 情景记忆检索失败，改用关键词兜底: {e}")

        return self._fallback_keyword_search(query, limit, user_id)

    def _normalize_remote_timestamps(self, user_id: str) -> None:
        """Normalize recognized legacy timestamps for one user's episodic points."""

        if user_id in self._normalized_timestamp_users:
            return

        points = self.vector_store.scroll(
            self.vector_collection,
            filters={"memory_type": "episodic", "user_id": user_id},
            with_vectors=True,
        )
        updates = []
        for point in points:
            timestamp = point.payload.get("timestamp")
            parsed = self._try_parse_timestamp(timestamp)
            if parsed is None:
                continue

            canonical = parsed.isoformat()
            if timestamp == canonical:
                continue

            payload = dict(point.payload)
            payload["timestamp"] = canonical
            updates.append(VectorPoint(point.id, point.vector, payload))

        if updates:
            self.vector_store.upsert(self.vector_collection, updates)

        self._normalized_timestamp_users.add(user_id)

    def _fallback_keyword_search(
        self,
        query: str,
        limit: int,
        user_id: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        """关键词兜底检索"""

        query_terms = set(query.lower().split())
        results = []

        for episode in self._episodes.values():
            context = episode.context or {}

            if user_id and context.get("user_id") != user_id:
                continue

            content = episode.content or ""
            content_terms = set(content.lower().split())

            if query_terms:
                overlap = query_terms & content_terms
                score = len(overlap) / len(query_terms)
            else:
                score = 0.0

            if score == 0.0 and query in content:
                score = 1.0

            if score > 0:
                metadata = {
                    "memory_id": episode.episode_id,
                    "episode_id": episode.episode_id,
                    "session_id": episode.session_id,
                    "timestamp": episode.timestamp,
                    "content": episode.content,
                    "memory_type": "episodic",
                    **context
                }

                results.append({
                    "id": episode.episode_id,
                    "score": score,
                    "metadata": metadata
                })

        results.sort(key=lambda x: float(x.get("score", 0.0)), reverse=True)
        return results[:limit]

    def _should_include(
        self,
        hit: Dict[str, Any],
        candidate_ids: Optional[Set[str]],
        kwargs: Dict[str, Any]
    ) -> bool:
        """判断命中结果是否保留"""

        metadata = hit.get("metadata", {}) or {}

        memory_id = (
            metadata.get("memory_id")
            or metadata.get("episode_id")
            or hit.get("id")
        )

        if candidate_ids is not None and memory_id not in candidate_ids:
            return False

        min_importance = kwargs.get("min_importance", 0.0)
        importance = float(metadata.get("importance", 0.5))

        if importance < min_importance:
            return False

        session_id = kwargs.get("session_id")
        if session_id and metadata.get("session_id") != session_id:
            return False

        user_id = kwargs.get("user_id")
        if user_id and metadata.get("user_id") != user_id:
            return False

        return True

    def _calculate_episode_score(self, hit: Dict[str, Any]) -> float:
        """情景记忆评分算法"""

        metadata = hit.get("metadata", {}) or {}

        vec_score = float(hit.get("score", 0.0))
        recency_score = self._calculate_recency(metadata.get("timestamp"))
        importance = float(metadata.get("importance", 0.5))

        base_relevance = vec_score * 0.8 + recency_score * 0.2
        importance_weight = 0.8 + importance * 0.4

        return base_relevance * importance_weight

    def _calculate_recency(self, timestamp: str) -> float:
        """计算时间近因性"""

        try:
            age_days = self._calculate_age_days(timestamp)
            score = math.exp(-age_days / 7.0)
            return max(0.1, min(1.0, score))
        except Exception:
            return 0.5

    def _create_memory_item(self, hit: Dict[str, Any]) -> MemoryItem:
        """把检索结果转换为 MemoryItem"""

        metadata = hit.get("metadata", {}) or {}

        memory_id = (
            metadata.get("memory_id")
            or metadata.get("episode_id")
            or hit.get("id")
        )

        content = metadata.get("content", "")

        if not content and memory_id in self._episodes:
            content = self._episodes[memory_id].content

        importance = float(metadata.get("importance", 0.5))
        timestamp = metadata.get("timestamp")

        item = MemoryItem(
            content=content,
            memory_type="episodic",
            importance=importance,
            metadata={
                **metadata,
                "retrieval_score": float(hit.get("score", 0.0))
            }
        )

        if memory_id:
            item.id = memory_id

        if timestamp:
            item.timestamp = timestamp

        return item

    def _calculate_age_days(self, timestamp: str) -> float:
        """计算记忆年龄"""

        memory_time = self._parse_timestamp(timestamp)
        delta = datetime.now() - memory_time
        return max(0.0, delta.total_seconds() / 86400)

    def _parse_timestamp(self, timestamp: Any) -> datetime:
        """解析时间戳"""

        return self._try_parse_timestamp(timestamp) or datetime.now()

    def _canonical_timestamp(self, timestamp: Any) -> str:
        return (self._try_parse_timestamp(timestamp) or datetime.now()).isoformat()

    def _try_parse_timestamp(self, timestamp: Any) -> Optional[datetime]:
        if isinstance(timestamp, datetime):
            return timestamp

        if not timestamp:
            return None

        value = str(timestamp)
        try:
            return datetime.fromisoformat(value)
        except ValueError:
            pass

        for timestamp_format in (
            "%Y/%m/%d",
            "%Y/%m/%d %H:%M:%S",
            "%Y-%m-%d %H:%M:%S",
        ):
            try:
                return datetime.strptime(value, timestamp_format)
            except ValueError:
                continue

        return None
