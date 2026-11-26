"""Shared construction and lifecycle management for application services."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from threading import RLock

from app.database import initialize_database
from app.learning_repository import LearningRepository
from app.learning_service import LearningService
from app.document_library import DocumentLibraryService
from app.document_search import DocumentSearchService
from app.graph_service import GraphService
from app.import_repository import ImportTaskRepository
from app.import_service import ImportTaskService
from app.import_worker import ImportWorkerPool
from app.migration import LegacyMigrationService
from app.qa_answer_engine import QaAnswerEngine, RagQaAnswerEngine
from app.qa_context import QaContextBuilder
from app.qa_deletion import (
    QaDeletionRepository,
    QaDeletionService,
    QaDeletionWorker,
)
from app.qa_job_repository import QaJobRepository
from app.qa_memory import QaMemoryLinker
from app.qa_migration import QaLegacyMigrationService
from app.qa_observability import InProcessQaTelemetry
from app.qa_repository import QaRepository
from app.qa_service import QaService
from app.qa_worker import QaWorkerPool
from app.note_migration import NoteMigrationService
from app.note_projection import (
    DefaultNoteMemoryProjection,
    NoteProjectionRepository,
    NoteProjectionWorker,
)
from app.note_repository import NoteRepository
from app.note_service import NoteService
from app.session import SessionRegistry
from app.storage import UserStorage


PROJECT_ROOT = Path(__file__).resolve().parents[1]


@dataclass
class ApplicationServices:
    """The persistent services shared by supported application entry points."""

    data_root: Path
    db_path: Path
    storage: UserStorage
    session_registry: SessionRegistry
    legacy_migration: LegacyMigrationService
    import_repository: ImportTaskRepository
    import_worker_pool: ImportWorkerPool
    import_service: ImportTaskService
    document_library: DocumentLibraryService
    qa_repository: QaRepository
    qa_job_repository: QaJobRepository
    qa_deletion_repository: QaDeletionRepository
    qa_legacy_migration: QaLegacyMigrationService
    qa_telemetry: InProcessQaTelemetry
    qa_worker_pool: QaWorkerPool
    qa_deletion_service: QaDeletionService
    qa_deletion_worker: QaDeletionWorker
    qa_service: QaService
    note_repository: NoteRepository
    note_migration: NoteMigrationService
    note_projection_repository: NoteProjectionRepository
    note_projection_worker: NoteProjectionWorker
    note_service: NoteService
    document_search: DocumentSearchService
    learning_repository: LearningRepository
    learning_service: LearningService
    graph_service: GraphService
    _started: bool = field(default=False, init=False, repr=False)
    _lifecycle_lock: RLock = field(default_factory=RLock, init=False, repr=False)

    @classmethod
    def create(
        cls,
        data_root: Path | None = None,
        *,
        qa_answer_engine: QaAnswerEngine | None = None,
    ) -> "ApplicationServices":
        resolved_data_root = Path(
            data_root
            or os.getenv("PDF_ASSISTANT_DATA_DIR")
            or PROJECT_ROOT / "data"
        ).resolve()
        db_path = resolved_data_root / "app.db"

        initialize_database(db_path)
        storage = UserStorage(resolved_data_root)
        session_registry = SessionRegistry(db_path=db_path, storage=storage)
        legacy_migration = LegacyMigrationService(db_path, storage, PROJECT_ROOT)
        import_repository = ImportTaskRepository(db_path)
        graph_service = GraphService(db_path)
        import_worker_pool = ImportWorkerPool(
            import_repository,
            session_registry.runtime_registry,
            storage,
            graph_service=graph_service,
        )
        import_service = ImportTaskService(
            session_registry,
            import_repository,
            storage,
            import_worker_pool,
        )
        session_registry.runtime_registry.set_import_task_service(import_service)
        qa_repository = QaRepository(db_path)
        note_repository = NoteRepository(db_path)
        note_migration = NoteMigrationService(
            db_path, storage, note_repository, session_registry.runtime_registry
        )
        note_projection_repository = NoteProjectionRepository(db_path)
        note_projection_worker = NoteProjectionWorker(
            note_projection_repository,
            note_repository,
            session_registry.runtime_registry,
            DefaultNoteMemoryProjection(),
        )
        qa_job_repository = QaJobRepository(db_path)
        learning_repository = LearningRepository(db_path)
        qa_deletion_repository = QaDeletionRepository(db_path, note_repository, learning_repository=learning_repository)
        qa_legacy_migration = QaLegacyMigrationService(db_path, storage)
        qa_telemetry = InProcessQaTelemetry()
        answer_engine = qa_answer_engine or RagQaAnswerEngine(
            verify_evidence=os.getenv("RAG_QA_VERIFY_EVIDENCE", "").strip() == "1",
            evidence_strategy=os.getenv("RAG_QA_EVIDENCE_STRATEGY", "two_pass").strip(),
            evidence_max_tokens=os.getenv("RAG_QA_EVIDENCE_MAX_TOKENS", "").strip(),
            retrieval_mode=os.getenv("RAG_QA_RETRIEVAL_MODE", "vector").strip().lower(),
            use_rerank=os.getenv("RAG_QA_RERANK", "").strip() == "1",
        )
        summary_engine = answer_engine
        if (
            qa_answer_engine is None
            and os.getenv("ZHIYAN_MULTI_SUMMARY_WORKFLOW", "").strip().lower()
            == "langgraph"
        ):
            summary_engine = RagQaAnswerEngine(enable_summary_workflow=True)
        context_builder = QaContextBuilder(max_input_tokens=6000)
        document_library = DocumentLibraryService(
            session_registry,
            storage,
            import_service,
            deletion_repository=qa_deletion_repository,
        )
        document_search = DocumentSearchService(session_registry, document_library)
        learning_service = LearningService(learning_repository, session_registry, document_library, storage)
        note_service = NoteService(
            note_repository, note_migration, qa_repository, note_projection_worker,
            document_search=document_search,
        )
        session_registry.runtime_registry.set_note_service(note_service)
        qa_worker_pool = QaWorkerPool(
            qa_job_repository,
            qa_repository,
            session_registry.runtime_registry,
            summary_engine,
            context_builder,
            qa_telemetry,
            memory_linker=QaMemoryLinker(qa_repository),
        )
        qa_deletion_worker = QaDeletionWorker(
            qa_deletion_repository,
            session_registry.runtime_registry,
            document_library,
            session_registry,
            legacy_migration=qa_legacy_migration,
            graph_service=graph_service,
        )
        qa_deletion_service = QaDeletionService(
            session_registry,
            document_library,
            qa_repository,
            qa_deletion_repository,
            qa_deletion_worker,
        )
        document_library.deletion_service = qa_deletion_service
        qa_service = QaService(
            session_registry,
            document_library,
            qa_repository,
            answer_engine,
            context_builder,
            qa_telemetry,
            job_repository=qa_job_repository,
            worker_pool=qa_worker_pool,
            deletion_service=qa_deletion_service,
            legacy_migration=qa_legacy_migration,
        )
        return cls(
            data_root=resolved_data_root,
            db_path=db_path,
            storage=storage,
            session_registry=session_registry,
            legacy_migration=legacy_migration,
            import_repository=import_repository,
            import_worker_pool=import_worker_pool,
            import_service=import_service,
            document_library=document_library,
            document_search=document_search,
            qa_repository=qa_repository,
            learning_repository=learning_repository,
            learning_service=learning_service,
            graph_service=graph_service,
            qa_job_repository=qa_job_repository,
            qa_deletion_repository=qa_deletion_repository,
            qa_legacy_migration=qa_legacy_migration,
            qa_telemetry=qa_telemetry,
            qa_worker_pool=qa_worker_pool,
            qa_deletion_service=qa_deletion_service,
            qa_deletion_worker=qa_deletion_worker,
            qa_service=qa_service,
            note_repository=note_repository,
            note_migration=note_migration,
            note_projection_repository=note_projection_repository,
            note_projection_worker=note_projection_worker,
            note_service=note_service,
        )

    def start(self) -> None:
        with self._lifecycle_lock:
            if self._started:
                return
            self.note_migration.migrate_known_users()
            self.note_projection_repository.recover_expired()
            self.qa_job_repository.recover_expired()
            self.qa_deletion_repository.recover_expired()
            self.qa_repository.recover_interrupted_questions()
            started: list[object] = []
            try:
                self.import_worker_pool.start()
                started.append(self.import_worker_pool)
                self.qa_worker_pool.start()
                started.append(self.qa_worker_pool)
                self.qa_deletion_worker.start()
                started.append(self.qa_deletion_worker)
                self.note_projection_worker.start()
                started.append(self.note_projection_worker)
            except Exception:
                for worker in reversed(started):
                    worker.stop()
                raise
            self._started = True

    def stop(self) -> None:
        with self._lifecycle_lock:
            if not self._started:
                return
            first_error: Exception | None = None
            for worker in (
                self.note_projection_worker,
                self.qa_deletion_worker,
                self.qa_worker_pool,
                self.import_worker_pool,
            ):
                try:
                    worker.stop()
                except Exception as error:
                    if first_error is None:
                        first_error = error
            self._started = False
            if first_error is not None:
                raise first_error


_application_services: ApplicationServices | None = None
_application_services_lock = RLock()


def get_application_services() -> ApplicationServices:
    """Return the process-wide application services singleton."""

    global _application_services
    with _application_services_lock:
        if _application_services is None:
            _application_services = ApplicationServices.create()
        return _application_services


def reset_application_services_for_tests() -> None:
    """Stop and clear the singleton so tests can construct isolated services."""

    global _application_services
    with _application_services_lock:
        if _application_services is not None:
            _application_services.stop()
            _application_services = None
