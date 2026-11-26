from __future__ import annotations

import logging
import queue
import shutil
import sqlite3
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from app.database import connect
from app.import_models import ImportTaskRecord
from app.import_repository import ImportTaskRepository, InvalidImportTransition
from app.storage import UserStorage
from assistants.pdf_learning_assistant import PDFLearningAssistant
from hello_agents.memory.rag.errors import (
    RAGAuthenticationError,
    RAGCollectionError,
    RAGConfigError,
    RAGConnectionError,
    RAGDocumentTooLargeError,
    RAGEmbeddingError,
    RAGOperationError,
    sanitize_error_message,
)


logger = logging.getLogger(__name__)
_RETRY_DELAYS = (2, 10, 30)
_RUNNER_FAILURE_RETRY_DELAYS = (0.02, 0.05, 0.1)
_STOP = object()
_SAFE_STRUCTURED_ERROR_CODES = {
    "document_invalid",
    "rag_connection",
    "rag_authentication",
    "rag_config",
    "rag_collection",
    "rag_document_too_large",
    "rag_embedding",
    "rag_collectionerror",
    "rag_documenttoolargeerror",
    "rag_embeddingerror",
    "rag_operation",
    "memory_import_event",
    "database_busy",
    "staged_cleanup_failed",
    "staged_file_missing",
    "process_interrupted",
    "unexpected_error",
}
_STAGE_RANGES = {
    "parsing": (10, 25),
    "chunking": (25, 40),
    "embedding": (40, 80),
    "persisting": (80, 92),
    "committing": (92, 99),
}


class ImportCancelled(BaseException):
    """Internal cooperative-cancellation signal that crosses Exception wrappers."""


class ImportCommitGateFailure(BaseException):
    """Carry a gate exception through producer best-effort progress wrappers."""

    def __init__(self, error: Exception) -> None:
        super().__init__()
        self.error = error


def classify_import_failure(error: BaseException) -> tuple[str, bool, str]:
    error_code = getattr(error, "error_code", None)
    retryable = getattr(error, "retryable", None)
    if error_code and retryable is not None:
        summary = sanitize_error_message(error)[:500] or error.__class__.__name__
        # Structured errors can originate in backend responses.  Persist only
        # known, stable codes so arbitrary values (for example
        # ``token=secret``) cannot become public task metadata.
        normalized_code = str(error_code).strip().lower()
        if normalized_code not in _SAFE_STRUCTURED_ERROR_CODES:
            normalized_code = "unexpected_error"
        return normalized_code, bool(retryable), summary
    if isinstance(error, (RAGConnectionError, TimeoutError, ConnectionError)):
        return "rag_connection", True, sanitize_error_message(error)[:500]
    if isinstance(error, RAGAuthenticationError):
        return "rag_authentication", False, "RAG authentication failed"
    if isinstance(error, RAGConfigError):
        return "rag_config", False, "RAG configuration is invalid"
    if isinstance(
        error,
        (RAGCollectionError, RAGDocumentTooLargeError, RAGEmbeddingError),
    ):
        name = error.__class__.__name__
        return name.lower(), False, name
    if isinstance(error, RAGOperationError):
        summary = sanitize_error_message(error)[:500]
        return "rag_operation", bool(getattr(error, "retryable", False)), summary
    if isinstance(error, sqlite3.OperationalError) and any(
        marker in str(error).lower() for marker in ("locked", "busy")
    ):
        return "database_busy", True, "Import database is temporarily busy"
    if isinstance(error, (FileNotFoundError, ValueError)):
        return "document_invalid", False, sanitize_error_message(error)[:500]
    return "unexpected_error", False, error.__class__.__name__


class ImportTaskRunner:
    def __init__(
        self,
        repository: ImportTaskRepository,
        runtime_registry: Any,
        storage: UserStorage,
        *,
        assistant_factory: Callable[..., Any] = PDFLearningAssistant,
        graph_service=None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.repository = repository
        self.runtime_registry = runtime_registry
        self.storage = storage
        self.assistant_factory = assistant_factory
        self.graph_service = graph_service
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    def run(self, task: ImportTaskRecord) -> None:
        runtime = None
        assistant = None
        formal_path: Path | None = None
        temporary_path: Path | None = None
        staged_path: Path | None = None
        try:
            self._raise_if_cancel_requested(task)
            staged_path = self._resolve_staged_path(task)
            if not staged_path.is_file():
                raise FileNotFoundError("Staged import file is missing")

            self._raise_if_cancel_requested(task)
            runtime = self.runtime_registry.acquire_background(task.user_id)
            # Record that the durable staged copy is ready before any formal
            # document copy or parsing begins.  The progress callback starts
            # from this value and never reports a lower percentage.
            self._raise_if_cancel_requested(task)
            self.repository.update_progress(
                task.user_id, task.task_id, "staged", 10, now=self._now_iso()
            )
            self._raise_if_cancel_requested(task)
            formal_path = self.storage.document_path(
                task.user_id, task.document_id, task.file_suffix
            )
            temporary_path = self.storage.temporary_document_path(
                task.user_id, task.document_id, task.file_suffix
            )
            self._raise_if_cancel_requested(task)
            shutil.copyfile(staged_path, temporary_path)
            self._raise_if_cancel_requested(task)
            temporary_path.replace(formal_path)

            self._raise_if_cancel_requested(task)
            assistant = self.assistant_factory(
                user_id=task.user_id,
                runtime_dir=runtime.paths.root,
                runtime=runtime,
            )
            self._raise_if_cancel_requested(task)
            result = assistant.load_document(
                str(formal_path),
                document_id=task.document_id,
                original_name=task.original_name,
                import_task_id=task.task_id,
                progress_callback=self._progress_callback(task),
            )
            if isinstance(result, str) and result.lstrip().startswith("❌"):
                raise ValueError(result)
            current = self.repository.get_task(task.user_id, task.task_id)
            if current is None or current.stage != "committing":
                if not self._try_begin_committing(task):
                    raise ImportCancelled()
            self.repository.mark_succeeded(
                task.user_id, task.task_id, now=self._now_iso()
            )
            if self.graph_service is not None:
                try:
                    enqueue = getattr(self.graph_service, "enqueue_after_import", None)
                    if callable(enqueue):
                        enqueue(task)
                    else:
                        self.graph_service.build_after_import(task, runtime)
                except Exception:
                    logger.warning("optional document graph enqueue failed")
            try:
                self._cleanup_staged_file(staged_path)
            except OSError:
                # The durable task transition is committed before cleanup;
                # retaining a staged copy is safe and recoverable if deletion
                # fails, so do not downgrade a succeeded task.
                logger.warning(
                    "could not remove completed import staging file", exc_info=True
                )
            else:
                self._remove_empty_batch_dir(staged_path.parent)
        except ImportCancelled:
            self._remove_attempt_files(temporary_path, formal_path)
            if staged_path is None:
                try:
                    staged_path = self._resolve_staged_path(task)
                except (OSError, ValueError):
                    staged_path = None
            if staged_path is not None:
                try:
                    self._cleanup_staged_file(staged_path)
                except OSError:
                    logger.warning(
                        "could not remove cancelled import staging file"
                    )
                else:
                    self._remove_empty_batch_dir(staged_path.parent)
            self.repository.mark_cancelled(
                task.user_id, task.task_id, now=self._now_iso()
            )
        except ImportCommitGateFailure as signal:
            self._remove_attempt_files(temporary_path, formal_path)
            self._record_failure(task, signal.error)
        except Exception as error:
            self._remove_attempt_files(temporary_path, formal_path)
            self._record_failure(task, error)
        finally:
            if assistant is not None:
                try:
                    assistant.close()
                except Exception:
                    logger.warning(
                        "background import assistant close failed", exc_info=True
                    )
            if runtime is not None:
                self.runtime_registry.release_background(task.user_id)

    def _resolve_staged_path(self, task: ImportTaskRecord) -> Path:
        return self.storage.resolve_staged_import_path(
            task.user_id,
            task.batch_id,
            task.task_id,
            task.file_suffix,
            task.staged_relative_path,
        )

    @staticmethod
    def _remove_attempt_files(*paths: Path | None) -> None:
        for path in paths:
            if path is None:
                continue
            try:
                path.unlink(missing_ok=True)
            except OSError:
                logger.warning("could not remove failed import file")

    @staticmethod
    def _cleanup_staged_file(path: Path) -> None:
        """Remove the durable staged source, surfacing cleanup failures."""
        path.unlink(missing_ok=True)

    def _progress_callback(self, task: ImportTaskRecord):
        last_progress = 10
        last_state: tuple[str, int, str] | None = None
        commit_started = False

        def update(stage: str, completed: int, total: int, message: str) -> None:
            nonlocal last_progress, last_state, commit_started
            if commit_started:
                return
            if stage == "committing":
                if not self._try_begin_committing(task):
                    raise ImportCancelled()
                commit_started = True
                last_progress = max(last_progress, 99)
                last_state = (stage, last_progress, str(message))
                return
            self._raise_if_cancel_requested(task)
            if stage not in _STAGE_RANGES:
                return
            start, end = _STAGE_RANGES[stage]
            ratio = 0.0 if total <= 0 else min(1.0, max(0.0, completed / total))
            progress = max(last_progress, start + int((end - start) * ratio))
            state = (stage, progress, str(message))
            if state == last_state:
                return
            self.repository.update_progress(
                task.user_id,
                task.task_id,
                stage,
                progress,
                now=self._now_iso(),
            )
            last_progress = progress
            last_state = state

        return update

    def _try_begin_committing(self, task: ImportTaskRecord) -> bool:
        try:
            return self.repository.try_begin_committing(
                task.user_id, task.task_id, now=self._now_iso()
            )
        except Exception as error:
            raise ImportCommitGateFailure(error) from error

    def _record_failure(self, task: ImportTaskRecord, error: Exception) -> None:
        error_code, retryable, summary = classify_import_failure(error)
        if retryable and task.auto_retry_count < task.max_auto_retries:
            delay = _RETRY_DELAYS[
                min(task.auto_retry_count, len(_RETRY_DELAYS) - 1)
            ]
            next_attempt = self.clock() + timedelta(seconds=delay)
            self.repository.mark_retry_wait(
                task.user_id,
                task.task_id,
                next_attempt_at=_as_utc_iso(next_attempt),
                error_code=error_code,
                error_summary=summary,
                now=self._now_iso(),
            )
        else:
            self.repository.mark_failed(
                task.user_id,
                task.task_id,
                error_code,
                summary,
                now=self._now_iso(),
            )

    def _raise_if_cancel_requested(self, task: ImportTaskRecord) -> None:
        if self.repository.is_cancel_requested(task.user_id, task.task_id):
            raise ImportCancelled()

    def _now_iso(self) -> str:
        return _as_utc_iso(self.clock())

    @staticmethod
    def _remove_empty_batch_dir(path: Path) -> None:
        try:
            path.rmdir()
        except OSError:
            pass


class ImportWorkerPool:
    def __init__(
        self,
        repository: ImportTaskRepository,
        runtime_registry: Any,
        storage: UserStorage,
        *,
        runner: Any = None,
        graph_service=None,
        worker_count: int = 4,
    ) -> None:
        if worker_count < 1:
            raise ValueError("worker_count must be at least 1")
        self.repository = repository
        self.storage = storage
        self.runner = runner or ImportTaskRunner(
            repository, runtime_registry, storage, graph_service=graph_service
        )
        self.worker_count = worker_count
        self._condition = threading.Condition()
        self._stop_event = threading.Event()
        self._blocked_user_ids: set[str] = set()
        self._worker_threads: list[threading.Thread] = []
        self._scheduler_thread: threading.Thread | None = None
        self._task_queue: queue.Queue[ImportTaskRecord | object] = queue.Queue()
        self._active_count = 0
        self._notify_generation = 0

    def start(self) -> None:
        with self._condition:
            if any(thread.is_alive() for thread in self._worker_threads) or (
                self._scheduler_thread is not None
                and self._scheduler_thread.is_alive()
            ):
                return
            self.repository.recover_running(self.storage)
            self._reconcile_terminal_staging()
            self._stop_event.clear()
            self._blocked_user_ids.clear()
            self._active_count = 0
            self._notify_generation = 0
            self._task_queue = queue.Queue()
            self._worker_threads = [
                threading.Thread(
                    target=self._worker_loop,
                    name=f"import-worker-{index + 1}",
                    daemon=False,
                )
                for index in range(self.worker_count)
            ]
            self._scheduler_thread = threading.Thread(
                target=self._scheduler_loop,
                name="import-scheduler",
                daemon=False,
            )
            workers = list(self._worker_threads)
            scheduler = self._scheduler_thread
        for thread in workers:
            thread.start()
        scheduler.start()

    def _reconcile_terminal_staging(self) -> None:
        self.repository.cleanup_succeeded_staging(self.storage)
        conn = connect(self.repository.db_path)
        try:
            rows = conn.execute(
                """
                select id, batch_id, user_id, document_id, file_suffix,
                       staged_relative_path
                from import_tasks where status = 'cancelled'
                """
            ).fetchall()
            self._reconcile_rollback_journals(conn)
        finally:
            conn.close()

        for row in rows:
            try:
                temporary, formal = self.storage.resolve_import_attempt_paths(
                    row["user_id"], row["document_id"], row["file_suffix"]
                )
            except ValueError:
                logger.warning(
                    "could not validate cancelled import attempt path"
                )
            else:
                for attempt_path in (temporary, formal):
                    try:
                        self._unlink_reconciled_path(attempt_path)
                    except OSError:
                        logger.warning(
                            "could not reconcile cancelled import attempt file"
                        )
            try:
                self.storage.remove_staged_import_file(
                    row["user_id"],
                    row["batch_id"],
                    row["id"],
                    row["file_suffix"],
                    row["staged_relative_path"],
                )
            except (OSError, ValueError):
                logger.warning(
                    "could not reconcile cancelled import staging file"
                )

    def _reconcile_rollback_journals(self, conn: Any) -> None:
        try:
            journals = list(self.storage.iter_import_rollback_journals())
        except OSError:
            logger.warning("could not enumerate import rollback markers")
            return
        for user_id, batch_id, marker in journals:
            batch_exists = conn.execute(
                "select 1 from import_batches where id = ? and user_id = ?",
                (batch_id, user_id),
            ).fetchone()
            if batch_exists is not None:
                try:
                    marker.unlink(missing_ok=True)
                except OSError:
                    logger.warning("could not remove stale import rollback marker")
                continue
            try:
                files = self.storage.read_import_rollback_journal(
                    user_id, batch_id, marker
                )
            except (OSError, ValueError):
                logger.warning("could not validate import rollback marker")
                continue

            complete = True
            for task_id, suffix in files:
                try:
                    partial, staged = self.storage.resolve_rollback_staging_paths(
                        user_id, batch_id, task_id, suffix
                    )
                except ValueError:
                    complete = False
                    logger.warning("could not validate import rollback file")
                    continue
                for rollback_path in (partial, staged):
                    try:
                        self._unlink_reconciled_path(rollback_path)
                    except OSError:
                        complete = False
                        logger.warning("could not reconcile import rollback file")
            if not complete:
                continue
            try:
                marker.unlink(missing_ok=True)
            except OSError:
                logger.warning("could not remove import rollback marker")
                continue
            try:
                marker.parent.rmdir()
            except OSError:
                pass

    @staticmethod
    def _unlink_reconciled_path(path: Path) -> None:
        path.unlink(missing_ok=True)
        if path.exists():
            raise OSError("exact import cleanup did not remove its target")

    def stop(self, wait: bool = True) -> None:
        with self._condition:
            self._stop_event.set()
            self._notify_generation += 1
            self._condition.notify_all()
            scheduler = self._scheduler_thread
            workers = list(self._worker_threads)
        if wait and scheduler is not None:
            scheduler.join()
            self._task_queue.join()
            for thread in workers:
                thread.join()

    def notify(self) -> None:
        with self._condition:
            self._notify_generation += 1
            self._condition.notify_all()

    def _scheduler_loop(self) -> None:
        try:
            while not self._stop_event.is_set():
                with self._condition:
                    if self._active_count >= self.worker_count:
                        self._condition.wait(timeout=1.0)
                        continue
                    blocked = set(self._blocked_user_ids)
                    observed_generation = self._notify_generation
                try:
                    task = self.repository.claim_next(blocked)
                except sqlite3.OperationalError:
                    task = None
                except Exception:
                    logger.exception("import scheduler could not claim a task")
                    task = None
                if task is None:
                    with self._condition:
                        self._condition.wait_for(
                            lambda: self._stop_event.is_set()
                            or self._notify_generation != observed_generation,
                            timeout=1.0,
                        )
                    continue
                with self._condition:
                    if self._stop_event.is_set():
                        release_claim = True
                    else:
                        release_claim = False
                        self._blocked_user_ids.add(task.user_id)
                        self._active_count += 1
                        self._task_queue.put(task)
                if release_claim:
                    try:
                        self.repository.release_claim(task.user_id, task.task_id)
                    except Exception:
                        logger.exception(
                            "import scheduler could not release a shutdown claim"
                        )
                    break
        finally:
            for _ in self._worker_threads:
                self._task_queue.put(_STOP)

    def _worker_loop(self) -> None:
        while True:
            item = self._task_queue.get()
            try:
                if item is _STOP:
                    return
                task = item
                try:
                    self.runner.run(task)
                except Exception:
                    logger.exception("import task runner failed unexpectedly")
                    self._record_runner_failure(task)
                finally:
                    with self._condition:
                        self._blocked_user_ids.discard(task.user_id)
                        self._active_count = max(0, self._active_count - 1)
                        self._notify_generation += 1
                        self._condition.notify_all()
            finally:
                self._task_queue.task_done()

    def _record_runner_failure(self, task: ImportTaskRecord) -> None:
        """Finish a task only when its runner left it in ``running``."""

        for delay in (*_RUNNER_FAILURE_RETRY_DELAYS, None):
            try:
                current = self.repository.get_task(task.user_id, task.task_id)
                if current is None or current.status != "running":
                    return
                self.repository.mark_failed(
                    task.user_id,
                    task.task_id,
                    "unexpected_error",
                    "Import worker failed unexpectedly",
                )
                return
            except sqlite3.OperationalError as error:
                if not _is_sqlite_busy(error):
                    logger.exception("could not record import runner failure")
                    return
                if delay is not None:
                    time.sleep(delay)
            except InvalidImportTransition:
                return
            except Exception:
                logger.exception("could not record import runner failure")
                return

        # If a transient writer lock survives all failure writes, release the
        # claim so a later worker can process it rather than stranding it in
        # ``running`` until an application restart.
        for delay in (*_RUNNER_FAILURE_RETRY_DELAYS, None):
            try:
                current = self.repository.get_task(task.user_id, task.task_id)
                if current is None or current.status != "running":
                    return
                self.repository.release_claim(task.user_id, task.task_id)
                return
            except sqlite3.OperationalError as error:
                if not _is_sqlite_busy(error):
                    logger.exception("could not release crashed import task")
                    return
                if delay is not None:
                    time.sleep(delay)
            except InvalidImportTransition:
                return
            except Exception:
                logger.exception("could not release crashed import task")
                return
        logger.error("import task remains running after worker failure")


def _as_utc_iso(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _is_sqlite_busy(error: sqlite3.OperationalError) -> bool:
    return any(marker in str(error).lower() for marker in ("locked", "busy"))
