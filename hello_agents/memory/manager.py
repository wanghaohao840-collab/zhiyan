from typing import Optional, Dict, List, Any
import logging

from hello_agents.memory.base import MemoryConfig, MemoryItem
from hello_agents.memory.types.working import WorkingMemory
from hello_agents.memory.types.episodic import EpisodicMemory
from hello_agents.memory.rag.embedding_profile import EmbeddingFailure


logger = logging.getLogger(__name__)


class MemoryManager:
    """记忆管理器 - 统一的记忆操作接口

    职责：
    1. 根据配置启用不同记忆类型
    2. 对外提供 add / retrieve / forget / consolidate 等统一接口
    3. 负责把上层 MemoryTool 的请求分发给具体记忆模块
    """

    def __init__(
        self,
        config: Optional[MemoryConfig] = None,
        user_id: str = "default_user",
        enable_working: bool = True,
        enable_episodic: bool = True,
        enable_semantic: bool = True,
        enable_perceptual: bool = False,
        snapshot_repository: Any = None
    ):
        self.config = config or MemoryConfig()
        self.user_id = user_id
        self.snapshot_repository = snapshot_repository

        # 当前阶段先不使用独立 MemoryStore / MemoryRetriever
        # 因为 working / episodic / semantic 各自已经有自己的存储和检索逻辑
        self.store = None
        self.retriever = None

        # 初始化各类型记忆
        self.memory_types: Dict[str, Any] = {}

        if enable_working:
            self.memory_types["working"] = WorkingMemory(
                config=self.config,
                storage_backend=self.store
            )

        if enable_episodic:
            self.memory_types["episodic"] = EpisodicMemory(
                config=self.config,
                storage_backend=self.store
            )

        if enable_semantic:
            try:
                from hello_agents.memory.types.semantic import SemanticMemory

                self.memory_types["semantic"] = SemanticMemory(
                    config=self.config,
                    storage_backend=self.store
                )

            except EmbeddingFailure:
                raise
            except Exception as e:
                logger.warning("SemanticMemory 初始化失败，已跳过 semantic 记忆: %s", e)

        if enable_perceptual:
            try:
                from hello_agents.memory.types.perceptual import PerceptualMemory

                self.memory_types["perceptual"] = PerceptualMemory(
                    config=self.config,
                    storage_backend=self.store
                )

            except Exception as e:
                logger.warning("PerceptualMemory 初始化失败，已跳过 perceptual 记忆: %s", e)

        logger.info(
            "MemoryManager初始化完成，启用记忆类型: %s",
            list(self.memory_types.keys())
        )

        if self.snapshot_repository is not None:
            self.snapshot_repository.restore_to_manager(self)

    def close(self) -> None:
        for memory in self.memory_types.values():
            close = getattr(memory, "close", None)
            if callable(close):
                close()

    def add_memory(
        self,
        content: str,
        memory_type: str = "working",
        importance: float = 0.5,
        metadata: Optional[Dict[str, Any]] = None,
        auto_classify: bool = False,
        memory_id: Optional[str] = None,
    ) -> str:
        """添加记忆"""

        if not content:
            raise ValueError("记忆内容不能为空")

        if auto_classify:
            memory_type = self._classify_memory_type(content, metadata or {})

        if memory_type not in self.memory_types:
            raise ValueError(f"未启用的记忆类型: {memory_type}")

        item_kwargs = {
            "content": content,
            "memory_type": memory_type,
            "importance": importance,
            "metadata": metadata or {},
        }
        if memory_id is not None:
            item_kwargs["id"] = memory_id
        memory_item = MemoryItem(
            **item_kwargs
        )

        memory_item.metadata.setdefault("user_id", self.user_id)

        memory_id = self.memory_types[memory_type].add(memory_item)
        self._save_snapshot()
        return memory_id

    def retrieve_memories(
        self,
        query: str,
        limit: int = 5,
        memory_types: Optional[List[str]] = None,
        min_importance: float = 0.1,
        **kwargs
    ) -> List[MemoryItem]:
        """检索记忆"""

        if not query:
            return []

        target_types = memory_types or list(self.memory_types.keys())

        all_results: List[MemoryItem] = []

        for memory_type in target_types:
            if memory_type not in self.memory_types:
                continue

            memory_module = self.memory_types[memory_type]

            try:
                results = memory_module.retrieve(
                    query=query,
                    limit=limit,
                    user_id=self.user_id,
                    min_importance=min_importance,
                    **kwargs
                )

                for item in results:
                    if item.importance >= min_importance:
                        all_results.append(item)

            except EmbeddingFailure:
                raise
            except Exception as e:
                logger.warning("检索 %s 记忆失败: %s", memory_type, e)

        all_results.sort(key=lambda x: x.importance, reverse=True)

        return all_results[:limit]

    def remove_memory(
        self,
        memory_id: str,
        *,
        memory_type: str,
        missing_ok: bool = False,
    ) -> bool:
        """Remove one exact memory without broad metadata matching."""

        module = self.memory_types.get(memory_type)
        remove = getattr(module, "remove", None)
        if not callable(remove):
            return False
        if memory_type == "semantic":
            removed = bool(
                remove(
                    memory_id,
                    user_id=self.user_id,
                    missing_ok=missing_ok,
                )
            )
        else:
            removed = bool(remove(memory_id))
        if removed:
            self._save_snapshot()
        return removed

    def forget_memories(
        self,
        strategy: str = "importance_based",
        threshold: float = 0.1,
        max_age_days: int = 30
    ) -> int:
        """遗忘记忆"""

        total_count = 0

        for memory_type, memory_module in self.memory_types.items():
            if hasattr(memory_module, "forget"):
                try:
                    count = memory_module.forget(
                        strategy=strategy,
                        threshold=threshold,
                        max_age_days=max_age_days
                    )
                    total_count += count

                except Exception as e:
                    logger.warning("遗忘 %s 记忆失败: %s", memory_type, e)

        self._save_snapshot()
        return total_count

    def consolidate_memories(
        self,
        from_type: str = "working",
        to_type: str = "episodic",
        importance_threshold: float = 0.7
    ) -> int:
        """整合记忆：将重要的短期记忆提升为长期记忆"""

        if from_type not in self.memory_types:
            raise ValueError(f"未启用的来源记忆类型: {from_type}")

        if to_type not in self.memory_types:
            raise ValueError(f"未启用的目标记忆类型: {to_type}")

        source_memory = self.memory_types[from_type]
        target_memory = self.memory_types[to_type]

        if not hasattr(source_memory, "memories"):
            logger.warning("%s 不支持直接整合，因为没有 memories 属性", from_type)
            return 0

        count = 0
        kept_memories = []

        for memory_item in source_memory.memories:
            if memory_item.importance >= importance_threshold:
                new_item = MemoryItem(
                    content=memory_item.content,
                    memory_type=to_type,
                    importance=memory_item.importance,
                    metadata={
                        **memory_item.metadata,
                        "consolidated_from": from_type,
                        "original_memory_id": memory_item.id,
                        "user_id": self.user_id
                    }
                )

                target_memory.add(new_item)
                count += 1

            else:
                kept_memories.append(memory_item)

        source_memory.memories = kept_memories

        self._save_snapshot()
        return count

    def get_summary(self) -> str:
        """获取记忆摘要"""

        lines = ["📊 记忆系统摘要:"]

        for memory_type, memory_module in self.memory_types.items():
            count = 0

            if hasattr(memory_module, "memories"):
                count = len(memory_module.memories)

            elif hasattr(memory_module, "count"):
                try:
                    count = memory_module.count()
                except Exception:
                    count = 0

            lines.append(f"- {memory_type}: {count} 条记忆")

        return "\n".join(lines)

    def get_stats(self) -> Dict[str, Any]:
        """获取记忆统计信息"""

        stats = {
            "user_id": self.user_id,
            "enabled_memory_types": list(self.memory_types.keys()),
            "memory_counts": {}
        }

        for memory_type, memory_module in self.memory_types.items():
            if hasattr(memory_module, "memories"):
                stats["memory_counts"][memory_type] = len(memory_module.memories)

            elif hasattr(memory_module, "count"):
                try:
                    stats["memory_counts"][memory_type] = memory_module.count()
                except Exception:
                    stats["memory_counts"][memory_type] = None

            else:
                stats["memory_counts"][memory_type] = None

        return stats

    def clear_all(self) -> str:
        """清空所有记忆"""

        for memory_type, memory_module in self.memory_types.items():
            if hasattr(memory_module, "memories"):
                memory_module.memories.clear()

            if hasattr(memory_module, "clear"):
                try:
                    memory_module.clear()

                except Exception as e:
                    logger.warning("清空 %s 记忆失败: %s", memory_type, e)

        self._save_snapshot()
        return "所有记忆已清空"

    def _save_snapshot(self) -> None:
        if self.snapshot_repository is not None:
            self.snapshot_repository.save_from_manager(self)

    def _classify_memory_type(
        self,
        content: str,
        metadata: Dict[str, Any]
    ) -> str:
        """简单自动分类记忆类型"""

        if metadata.get("modality") or metadata.get("file_path"):
            return "perceptual"

        if metadata.get("event_type") or metadata.get("session_id"):
            return "episodic"

        if metadata.get("knowledge_type"):
            return "semantic"

        if len(content) > 80:
            return "semantic"

        return "working"
