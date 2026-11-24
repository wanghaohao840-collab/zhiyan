from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import RLock

from app.coordination import UserMutationCoordinator
from app.history import HistoryRepository
from app.memory_repository import MemorySnapshotRepository
from app.recovery import RecoveryService
from app.reports import ReportService
from app.storage import UserPaths, UserStorage
from hello_agents.memory.base import MemoryConfig
from hello_agents.tools.builtin.memory_tool import MemoryTool
from hello_agents.tools.builtin.rag_tool import RAGTool
from hello_agents.memory.rag.pipeline import resolve_rag_embedding_dependencies, resolve_rag_backend
from hello_agents.memory.rag.index_identity import IndexIdentity
from hello_agents.memory.rag.json_index_cache import JsonIndexCache


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
    active_session_count: int = 0
    active_background_count: int = 0
    import_task_service: object | None = None
    note_service: object | None = None

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
        self.note_service: object | None = None

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

    def set_note_service(self, service: object | None) -> None:
        """Inject the shared Note facade into existing and future runtimes."""

        with self._lock:
            self.note_service = service
            for runtime in self._runtimes.values():
                runtime.note_service = service

    def get_or_create(self, user_id: str) -> UserRuntime:
        with self._lock:
            runtime = self._runtimes.get(user_id)
            if runtime is not None:
                return runtime

            paths = self.storage.user_paths(user_id)
            new_user_directory = not paths.root.exists()
            # Only a newly initialized user can start with an empty managed
            # JSON index. Existing missing indexes still require recovery.
            if new_user_directory and resolve_rag_backend(None) == "json":
                embedding, indexes = resolve_rag_embedding_dependencies(
                    backend="json", data_root=self.storage.data_root,
                )
                if embedding is not None:
                    identity = IndexIdentity("json", "pdf_learning_collection", embedding.profile)
                    indexes.require_active(identity)
                    bootstrap = indexes.registry_root / "user-bootstrap"
                    bootstrap.mkdir(parents=True, exist_ok=True)
                    self.storage.users_root.mkdir(parents=True, exist_ok=True)
                    # Publish both files together. An interrupted first login
                    # leaves the final user directory absent and can be retried.
                    with TemporaryDirectory(prefix="new-user-", dir=bootstrap) as temporary:
                        staged = Path(temporary)
                        JsonIndexCache(staged / "rag" / "rag_cache.json", identity, f"pdf_{user_id}").write([])
                        initial_history = HistoryRepository(staged / "history.json")
                        initial_history.save(initial_history.load())
                        os.replace(staged, paths.root)
            paths = self.storage.ensure_user_dirs(user_id)
            memory_repo = MemorySnapshotRepository(paths.memory_snapshot, user_id=user_id)
            memory_tool = MemoryTool(
                user_id=user_id,
                memory_config=MemoryConfig(
                    database_path=str(paths.root / "memory" / f"memory_{user_id}.db"),
                    qdrant_collection=f"hello_agents_vectors_{user_id}",
                ),
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
            runtime = UserRuntime(
                user_id=user_id,
                paths=paths,
                lock=user_lock,
                coordinator=coordinator,
                rag_tool=RAGTool(
                    knowledge_base_path=str(paths.rag_cache.parent),
                    collection_name="pdf_learning_collection",
                    rag_namespace=f"pdf_{user_id}",
                    cache_path=str(paths.rag_cache),
                    data_root=self.storage.data_root,
                    # Product routes currently use document RAG only. Optional
                    # Neo4j credentials must not trigger network I/O during
                    # login or startup's migration of existing users.
                    enable_graph=False,
                ),
                memory_tool=memory_tool,
                history=history_repo,
                reports=ReportService(self.db_path, self.storage),
                recovery=recovery,
                import_task_service=self.import_task_service,
                note_service=self.note_service,
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
