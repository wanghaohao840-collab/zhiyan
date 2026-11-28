from __future__ import annotations

import logging
import queue
import sqlite3
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from time import monotonic
from typing import Callable, Iterator, Protocol, Sequence
from uuid import uuid4

from app.qa_answer_engine import QaAnswerEngine, QaAnswerRequest, QaEngineError
from app.qa_context import QaContextBuilder
from app.qa_job_repository import QaJobRepository
from app.qa_memory import QaMemoryLinker
from app.qa_models import QaJob, QaMessage
from app.qa_observability import QaTelemetry
from app.qa_repository import QaRepository
from app.qa_service import QaService
from hello_agents.tools.builtin.rag_summary_workflow import (
    SummaryOwnershipCheckError,
    SummaryProgressAborted,
)


logger = logging.getLogger(__name__)


def _is_sqlite_lock_error(error: sqlite3.OperationalError) -> bool:
    code = getattr(error, "sqlite_errorcode", None)
    if code is not None:
        return code & 255 in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED)
    # Python 3.10 does not expose SQLite result codes on exceptions.
    return str(error).startswith(
        ("database is locked", "database table is locked", "database schema is locked")
    )


class QaConversationSummarizer(Protocol):
    def summarize(
        self,
        runtime: object,
        previous_summary: str,
        completed_turns: Sequence[QaMessage],
    ) -> str: ...


class LlmQaConversationSummarizer:
    def summarize(
        self,
        runtime: object,
        previous_summary: str,
        completed_turns: Sequence[QaMessage],
    ) -> str:
        parts = ["请压缩以下学习问答，保留结论、分歧与未解决问题。"]
        if previous_summary:
            parts.append(f"已有摘要：\n{previous_summary}")
        parts.append(
            "\n".join(
                f"{'用户' if item.role == 'user' else '助手'}：{item.content}"
                for item in completed_turns
            )
        )
        return str(runtime.rag_tool.llm.generate("\n\n".join(parts))).strip()


class _LeaseLost(SummaryProgressAborted):
    pass


@dataclass(frozen=True)
class _SummaryCancellation:
    shutdown: threading.Event
    lease_lost: threading.Event
    owns_attempt: Callable[[], bool]

    def is_set(self) -> bool:
        return self.shutdown.is_set() or self.lease_lost.is_set()

    def check_before_model(self) -> None:
        try:
            owns_attempt = not self.lease_lost.is_set() and self.owns_attempt()
        except sqlite3.Error as error:
            retryable = isinstance(error, sqlite3.OperationalError) and _is_sqlite_lock_error(error)
            raise SummaryOwnershipCheckError("无法核对摘要任务状态", retryable=retryable) from None
        if not owns_attempt:
            self.lease_lost.set()
            raise _LeaseLost()


@dataclass(frozen=True)
class _Refresh:
    user_id: str
    conversation_id: str


class QaWorkerPool:
    def __init__(
        self,
        job_repository: QaJobRepository,
        qa_repository: QaRepository,
        runtime_registry,
        answer_engine: QaAnswerEngine,
        context_builder: QaContextBuilder,
        telemetry: QaTelemetry,
        *,
        summarizer: QaConversationSummarizer | None = None,
        memory_linker: QaMemoryLinker | None = None,
        worker_count: int = 2,
        lease_seconds: int = 60,
        poll_interval: float = 0.5,
    ) -> None:
        if worker_count < 1 or lease_seconds < 1 or poll_interval <= 0:
            raise ValueError("invalid QA worker configuration")
        self.job_repository = job_repository
        self.qa_repository = qa_repository
        self.runtime_registry = runtime_registry
        self.answer_engine = answer_engine
        self.context_builder = context_builder
        self.telemetry = telemetry
        self.summarizer = summarizer or LlmQaConversationSummarizer()
        self.memory_linker = memory_linker
        self.worker_count = worker_count
        self.lease_seconds = lease_seconds
        self.poll_interval = poll_interval
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._threads: list[threading.Thread] = []
        self._refresh_queue: queue.Queue[_Refresh] = queue.Queue()
        self._refresh_keys: set[tuple[str, str]] = set()
        self._refresh_lock = threading.Lock()

    def start(self) -> None:
        if any(thread.is_alive() for thread in self._threads):
            return
        self.job_repository.recover_expired()
        self._stop.clear()
        self._threads = [
            threading.Thread(
                target=self._loop,
                args=(f"qa-worker-{index + 1}-{uuid4()}" ,),
                name=f"qa-worker-{index + 1}",
                daemon=False,
            )
            for index in range(self.worker_count)
        ]
        for thread in self._threads:
            thread.start()
        self.notify()

    def stop(self, wait: bool = True) -> None:
        self._stop.set()
        self._wake.set()
        if wait:
            for thread in self._threads:
                thread.join()

    def notify(self) -> None:
        self._wake.set()

    def schedule_summary_refresh(self, user_id: str, conversation_id: str) -> None:
        key = (user_id, conversation_id)
        with self._refresh_lock:
            if key in self._refresh_keys:
                return
            self._refresh_keys.add(key)
            self._refresh_queue.put(_Refresh(*key))
        self.notify()

    def _loop(self, worker_id: str) -> None:
        while not self._stop.is_set():
            try:
                job = self.job_repository.claim_next(
                    worker_id, lease_seconds=self.lease_seconds
                )
                if job is not None:
                    self._run_job(worker_id, job)
                    continue
                if self.memory_linker is not None:
                    memory_message = self.qa_repository.claim_next_memory_sync(
                        worker_id, lease_seconds=self.lease_seconds
                    )
                    if memory_message is not None:
                        self._run_memory(worker_id, memory_message)
                        continue
                try:
                    refresh = self._refresh_queue.get_nowait()
                except queue.Empty:
                    self._wake.wait(self.poll_interval)
                    self._wake.clear()
                    continue
                try:
                    self._run_refresh(refresh)
                finally:
                    self._refresh_queue.task_done()
            except Exception:
                logger.exception("QA worker iteration failed")

    def _run_job(self, worker_id: str, job: QaJob) -> None:
        runtime = None
        attempt = job.attempt_count
        cancel = _SummaryCancellation(
            self._stop,
            threading.Event(),
            lambda: self.job_repository.can_continue_attempt(job.id, worker_id, attempt),
        )
        try:
            with self._renew_summary_lease(worker_id, job, cancel):
                if cancel.is_set():
                    return
                conversation = self.qa_repository.get_conversation(
                    job.user_id, job.conversation_id
                )
                input_message = self.qa_repository.get_message(
                    job.user_id, job.input_message_id
                )
                assistant = self.qa_repository.get_message(
                    job.user_id, job.assistant_message_id
                )
                if conversation is None or input_message is None or assistant is None:
                    return
                runtime = self.runtime_registry.acquire_background(job.user_id)
                if cancel.lease_lost.is_set():
                    raise _LeaseLost()
                if self._stop.is_set():
                    return
                result = self.answer_engine.answer(
                    runtime,
                    QaAnswerRequest(
                        question=input_message.content,
                        conversation_context="",
                        document_ids=tuple(
                            item.document_id for item in conversation.documents
                        ),
                        mode="summary",
                        job_id=job.id,
                        attempt=attempt,
                    ),
                    progress_callback=self._progress_callback(worker_id, job, cancel),
                    cancel_event=cancel,
                )
            if cancel.lease_lost.is_set():
                raise _LeaseLost()
            if self._stop.is_set():
                return
            self.job_repository.commit_answer(
                job.id,
                worker_id,
                assistant.version,
                result.answer,
                result.sources,
                "available" if result.sources else "none",
                attempt=attempt,
                stop_requested=self._stop.is_set,
            )
        except Exception as error:
            if isinstance(error, _LeaseLost) or cancel.lease_lost.is_set():
                # Only the owner of a still-live cancelled attempt can finalize it.
                self.job_repository.complete(job.id, worker_id, attempt=attempt)
                return
            if self._stop.is_set():
                return
            code, retryable = QaService._safe_engine_failure(error)
            try:
                self.job_repository.fail_or_retry(
                    job.id,
                    worker_id,
                    code,
                    str(uuid4()),
                    retryable=retryable,
                    attempt=attempt,
                )
            except Exception:
                logger.warning("QA job failure transition lost", extra={"job_id": job.id})
        finally:
            if runtime is not None:
                self.runtime_registry.release_background(job.user_id)

    @contextmanager
    def _renew_summary_lease(
        self, worker_id: str, job: QaJob, cancel: _SummaryCancellation
    ) -> Iterator[None]:
        done = threading.Event()
        interval = self.lease_seconds / 3

        def renew() -> None:
            next_renewal = monotonic() + interval
            renewal_deadline = monotonic() + self.lease_seconds
            while not cancel.is_set():
                remaining = next_renewal - monotonic()
                if done.wait(min(0.2, max(0, remaining))) or cancel.is_set():
                    return
                if monotonic() < next_renewal:
                    continue
                started = monotonic()
                try:
                    if not self.job_repository.heartbeat(
                        job.id, worker_id, attempt=job.attempt_count
                    ):
                        cancel.lease_lost.set()
                        return
                except sqlite3.OperationalError as error:
                    if _is_sqlite_lock_error(error):
                        if monotonic() < renewal_deadline:
                            next_renewal = min(renewal_deadline, monotonic() + interval / 4)
                            continue
                    cancel.lease_lost.set()
                    logger.warning("QA summary lease renewal failed", extra={"job_id": job.id})
                    return
                except Exception:
                    cancel.lease_lost.set()
                    logger.warning("QA summary lease renewal failed", extra={"job_id": job.id})
                    return
                renewal_deadline = started + self.lease_seconds
                next_renewal = started + interval

        thread = threading.Thread(target=renew, name="qa-summary-renewal")
        thread.start()
        try:
            yield
        finally:
            done.set()
            thread.join()

    def _progress_callback(
        self, worker_id: str, job: QaJob, cancel: _SummaryCancellation
    ):
        def update(
            stage: str,
            completed: int,
            total: int,
            message: str = "",
            *,
            document_id: str | None = None,
        ) -> None:
            if cancel.is_set():
                raise _LeaseLost()
            ratio = 0.0 if total <= 0 else min(1.0, max(0.0, completed / total))
            progress = min(95, max(1, int(ratio * 95)))
            if not self.job_repository.heartbeat(
                job.id,
                worker_id,
                progress=progress,
                stage=str(stage or "generating")[:100],
                attempt=job.attempt_count,
            ):
                cancel.lease_lost.set()
                raise _LeaseLost()

        return update

    def _run_memory(self, worker_id: str, message: QaMessage) -> None:
        runtime = None
        try:
            conversation = self.qa_repository.get_conversation(
                message.user_id, message.conversation_id
            )
            if conversation is None:
                return
            runtime = self.runtime_registry.acquire_background(message.user_id)
            self.memory_linker.sync(
                runtime, conversation, message, worker_id
            )
        except Exception:
            self.qa_repository.fail_memory_sync(
                message.user_id,
                message.id,
                worker_id,
                message.version,
                "QA_MEMORY_WRITE_FAILED",
            )
        finally:
            if runtime is not None:
                self.runtime_registry.release_background(message.user_id)

    def _run_refresh(self, refresh: _Refresh) -> None:
        key = (refresh.user_id, refresh.conversation_id)
        runtime = None
        try:
            conversation = self.qa_repository.get_conversation(*key)
            if conversation is None:
                return
            messages = self._all_messages(*key)
            turns = self.context_builder._complete_turns(
                messages,
                conversation.conversation.summary_through_message_id,
            )
            flattened = tuple(item for turn in turns for item in turn)
            if not flattened:
                return
            runtime = self.runtime_registry.acquire_background(refresh.user_id)
            summary = self.summarizer.summarize(
                runtime,
                conversation.conversation.rolling_summary,
                flattened,
            )
            self.qa_repository.update_rolling_summary(
                refresh.user_id,
                refresh.conversation_id,
                conversation.conversation.version,
                conversation.conversation.summary_version,
                summary,
                flattened[-1].id,
            )
        except Exception:
            self.telemetry.record(
                "qa_summary_fallback", conversation_id=refresh.conversation_id
            )
        finally:
            if runtime is not None:
                self.runtime_registry.release_background(refresh.user_id)
            with self._refresh_lock:
                self._refresh_keys.discard(key)

    def _all_messages(self, user_id: str, conversation_id: str) -> tuple[QaMessage, ...]:
        items: list[QaMessage] = []
        cursor = None
        while True:
            page = self.qa_repository.list_messages(
                user_id, conversation_id, cursor=cursor, limit=200
            )
            items.extend(page.items)
            if page.next_cursor is None:
                return tuple(items)
            cursor = page.next_cursor
