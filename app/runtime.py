from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from threading import RLock

from app.coordination import UserMutationCoordinator
from app.history import HistoryRepository
from app.learning import LearningService
from app.learning_generation import LearningGenerationService
from app.learning_repository import LearningRepository
from app.memory_repository import MemorySnapshotRepository
from app.recovery import RecoveryService
from app.reports import ReportService
from app.storage import UserPaths, UserStorage
from hello_agents.memory.base import MemoryConfig
from hello_agents.tools.builtin.memory_tool import MemoryTool
from hello_agents.tools.builtin.rag_tool import RAGTool


@dataclass
class UserRuntime:
    user_id: str
    paths: UserPaths
    lock: RLock
    coordinator: UserMutationCoordinator
    rag_tool: RAGTool
    memory_tool: MemoryTool
    history: HistoryRepository
    reports: ReportService
    recovery: RecoveryService
    learning_repository: LearningRepository
    learning_service: LearningService
    learning_generation_service: LearningGenerationService
    active_session_count: int = 0
    active_background_count: int = 0
    import_task_service: object | None = None

    def close(self) -> None:
        close = getattr(self.rag_tool, "close", None)
        if callable(close):
            close()
        close = getattr(self.memory_tool, "close", None)
        if callable(close):
            close()


class UserRuntimeRegistry:
    def __init__(self, db_path: Path | str, storage: UserStorage):
        self.db_path = Path(db_path)
        self.storage = storage
        self._runtimes: dict[str, UserRuntime] = {}
        self._lock = RLock()
        self.import_task_service: object | None = None

    def set_import_task_service(self, service: object | None) -> None:
        """Inject the optional durable-import service into all user runtimes.

        The UI creates its service after the session registry.  Updating both
        future and already-created runtimes keeps destructive-operation guards
        correct for sessions established during application initialization.
        """

        with self._lock:
            self.import_task_service = service
            for runtime in self._runtimes.values():
                runtime.import_task_service = service

    def get_or_create(self, user_id: str) -> UserRuntime:
        with self._lock:
            runtime = self._runtimes.get(user_id)
            if runtime is not None:
                return runtime

            paths = self.storage.ensure_user_dirs(user_id)
            memory_repo = MemorySnapshotRepository(paths.memory_snapshot, user_id=user_id)
            memory_tool = MemoryTool(
                user_id=user_id,
                memory_config=MemoryConfig(database_path=str(paths.root / "memory" / f"memory_{user_id}.db")),
                memory_types=["working", "episodic", "semantic"],
                memory_repository=memory_repo,
            )
            user_lock = RLock()
            memory_tool.coordination_lock = user_lock
            history_repo = HistoryRepository(paths.history)
            coordinator = UserMutationCoordinator(
                user_id=user_id,
                lock=user_lock,
                history=history_repo,
                document_root=paths.documents,
            )
            backup_dir = paths.root / "backups"
            backup_dir.mkdir(parents=True, exist_ok=True)
            recovery = RecoveryService(
                coordinator=coordinator,
                history_repo=history_repo,
                memory_repo=memory_repo,
                backup_dir=backup_dir,
                memory_manager=memory_tool.memory_manager,
            )
            rag_tool = RAGTool(
                knowledge_base_path=str(paths.rag_cache.parent),
                collection_name="pdf_learning_collection",
                rag_namespace=f"pdf_{user_id}",
                cache_path=str(paths.rag_cache),
            )
            learning_repository = LearningRepository(
                paths.root / "learning.json", user_lock
            )
            runtime = UserRuntime(
                user_id=user_id,
                paths=paths,
                lock=user_lock,
                coordinator=coordinator,
                rag_tool=rag_tool,
                memory_tool=memory_tool,
                history=history_repo,
                reports=ReportService(self.db_path, self.storage),
                recovery=recovery,
                learning_repository=learning_repository,
                learning_service=LearningService(learning_repository),
                learning_generation_service=LearningGenerationService(
                    rag_tool, getattr(rag_tool, "llm", None)
                ),
                import_task_service=self.import_task_service,
            )
            self._runtimes[user_id] = runtime
            return runtime

    def acquire_session(self, user_id: str) -> UserRuntime:
        with self._lock:
            runtime = self.get_or_create(user_id)
            runtime.active_session_count += 1
            return runtime

    def release_session(self, user_id: str) -> None:
        with self._lock:
            runtime = self._runtimes.get(user_id)
            if runtime is None:
                return
            runtime.active_session_count = max(0, runtime.active_session_count - 1)
            self._release_if_unused_locked(user_id)

    def acquire_background(self, user_id: str) -> UserRuntime:
        with self._lock:
            runtime = self.get_or_create(user_id)
            runtime.active_background_count += 1
            return runtime

    def release_background(self, user_id: str) -> None:
        with self._lock:
            runtime = self._runtimes.get(user_id)
            if runtime is None:
                return
            runtime.active_background_count = max(0, runtime.active_background_count - 1)
            self._release_if_unused_locked(user_id)

    def has_runtime(self, user_id: str) -> bool:
        with self._lock:
            return user_id in self._runtimes

    def release_if_unused(
        self, user_id: str, active_session_count: int | None = None
    ) -> None:
        """Compatibility wrapper for callers that do not own a lease."""

        with self._lock:
            self._release_if_unused_locked(user_id)

    def _release_if_unused_locked(self, user_id: str) -> None:
        runtime = self._runtimes.get(user_id)
        if runtime is None:
            return
        if runtime.active_session_count or runtime.active_background_count:
            return
        self._runtimes.pop(user_id, None)
        runtime.close()
