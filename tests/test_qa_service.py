from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from app.database import connect, initialize_database
from app.document_library import DocumentLibraryItem
from app.qa_answer_engine import QaAnswerResult, QaEngineError
from app.qa_context import QaContextBuilder
from app.qa_models import QaSourceDraft, QaValidationError
from app.qa_observability import InProcessQaTelemetry
from app.qa_repository import QaRepository
from app.qa_service import (
    QaBusyError,
    QaEngineUnavailableError,
    QaNotFoundError,
    QaRetryNotAllowedError,
    QaService,
)


OWNER = "owner"
OTHER = "other"
TOKEN = "owner-token"


class Sessions:
    def __init__(self):
        self.sessions = {
            TOKEN: SimpleNamespace(
                user_id=OWNER,
                runtime=SimpleNamespace(name="owner", lock=threading.RLock()),
            ),
            "other-token": SimpleNamespace(
                user_id=OTHER,
                runtime=SimpleNamespace(name="other", lock=threading.RLock()),
            ),
        }

    def get_session(self, token):
        return self.sessions[token]


class Library:
    def __init__(self):
        self.items = {
            TOKEN: (
                DocumentLibraryItem("doc-1", "一.pdf", ".pdf", 1, None),
                DocumentLibraryItem("doc-2", "二.pdf", ".pdf", 1, None),
            ),
            "other-token": (
                DocumentLibraryItem("other-doc", "他.pdf", ".pdf", 1, None),
            ),
        }

    def list_documents(self, token):
        return self.items[token]


class Engine:
    def __init__(self, outcomes=None):
        self.outcomes = list(outcomes or [])
        self.calls = []

    def answer(self, runtime, request, **kwargs):
        self.calls.append((runtime, request, kwargs))
        outcome = self.outcomes.pop(0) if self.outcomes else None
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome or QaAnswerResult(
            answer="回答",
            sources=(
                QaSourceDraft(
                    "S-1", "doc-1", "一.pdf", excerpt="证据", reference="[S-1]"
                ),
            ),
            mode=request.mode,
        )


@pytest.fixture
def service_parts(tmp_path):
    db_path = tmp_path / "app.db"
    initialize_database(db_path)
    with connect(db_path) as conn:
        conn.executemany(
            """
            insert into users (
                id, username, username_key, password_hash, created_at, updated_at
            ) values (?, ?, ?, 'hash', '2026-08-26T00:00:00Z', '2026-08-26T00:00:00Z')
            """,
            ((OWNER, "Owner", "owner"), (OTHER, "Other", "other")),
        )
    repository = QaRepository(db_path)
    sessions = Sessions()
    library = Library()
    telemetry = InProcessQaTelemetry()
    return db_path, repository, sessions, library, telemetry


def make_service(parts, engine=None):
    _, repository, sessions, library, telemetry = parts
    return QaService(
        sessions,
        library,
        repository,
        engine or Engine(),
        QaContextBuilder(max_input_tokens=2000),
        telemetry,
    )


class Jobs:
    def __init__(self, result=None):
        self.result = result
        self.calls = []

    def create_summary_turn_and_job(self, *args, **kwargs):
        self.calls.append(("create", args, kwargs))
        return self.result

    def get(self, *args, **kwargs):
        self.calls.append(("get", args, kwargs))
        return self.result.job if self.result else None

    def request_cancel(self, *args, **kwargs):
        self.calls.append(("cancel", args, kwargs))
        return self.result.job if self.result else None


class Worker:
    def __init__(self):
        self.notifications = 0

    def notify(self):
        self.notifications += 1

    def schedule_summary_refresh(self, user_id, conversation_id):
        pass


def test_create_conversation_preserves_verified_scope_and_is_user_scoped(
    service_parts,
) -> None:
    service = make_service(service_parts)
    created = service.create_conversation(TOKEN, ("doc-2", "doc-1"))
    assert [item.document_id for item in created.documents] == ["doc-2", "doc-1"]
    with pytest.raises(QaNotFoundError):
        service.get_conversation("other-token", created.id)
    with pytest.raises(QaNotFoundError):
        service.create_conversation(TOKEN, ("other-doc",))


def test_create_conversation_waits_for_document_removal_before_scope_snapshot(
    service_parts,
) -> None:
    service = make_service(service_parts)
    _, _, sessions, library, _ = service_parts
    runtime = sessions.sessions[TOKEN].runtime
    creation_lock_attempted = threading.Event()

    class ObservedRuntimeLock:
        def __init__(self) -> None:
            self.lock = threading.RLock()

        def __enter__(self):
            if threading.current_thread().name == "conversation-create":
                creation_lock_attempted.set()
            self.lock.acquire()
            return self

        def __exit__(self, *_args):
            self.lock.release()

    runtime.lock = ObservedRuntimeLock()
    deletion_holds_runtime = threading.Event()
    release_deletion = threading.Event()
    creation_finished = threading.Event()
    creation_state = {}

    def finish_document_removal() -> None:
        with runtime.lock:
            deletion_holds_runtime.set()
            assert release_deletion.wait(timeout=3)
            library.items[TOKEN] = ()

    def create_conversation() -> None:
        try:
            creation_state["value"] = service.create_conversation(
                TOKEN, ("doc-1",)
            )
        except BaseException as error:
            creation_state["error"] = error
        finally:
            creation_finished.set()

    deleter = threading.Thread(
        target=finish_document_removal, name="document-remove"
    )
    creator = threading.Thread(
        target=create_conversation, name="conversation-create"
    )
    deleter.start()
    assert deletion_holds_runtime.wait(timeout=1)
    creator.start()
    try:
        assert creation_lock_attempted.wait(timeout=1)
        assert not creation_finished.is_set()
    finally:
        release_deletion.set()

    deleter.join(timeout=3)
    creator.join(timeout=3)
    assert not deleter.is_alive()
    assert not creator.is_alive()
    assert isinstance(creation_state.get("error"), QaNotFoundError)
    assert "value" not in creation_state


def test_create_conversation_holds_runtime_lock_through_repository_commit(
    service_parts,
    monkeypatch,
) -> None:
    service = make_service(service_parts)
    _, repository, sessions, _, _ = service_parts
    runtime = sessions.sessions[TOKEN].runtime
    deletion_lock_attempted = threading.Event()
    deletion_lock_acquired = threading.Event()
    repository_entered = threading.Event()
    release_repository = threading.Event()
    creation_state = {}

    class ObservedRuntimeLock:
        def __init__(self) -> None:
            self.lock = threading.RLock()

        def __enter__(self):
            if threading.current_thread().name == "document-remove":
                deletion_lock_attempted.set()
            self.lock.acquire()
            return self

        def __exit__(self, *_args):
            self.lock.release()

    runtime.lock = ObservedRuntimeLock()
    original_create = repository.create_conversation

    def hold_repository_commit(*args, **kwargs):
        repository_entered.set()
        assert release_repository.wait(timeout=3)
        return original_create(*args, **kwargs)

    monkeypatch.setattr(repository, "create_conversation", hold_repository_commit)

    def create_conversation() -> None:
        try:
            creation_state["value"] = service.create_conversation(
                TOKEN, ("doc-1",)
            )
        except BaseException as error:
            creation_state["error"] = error

    def wait_to_remove_document() -> None:
        with runtime.lock:
            deletion_lock_acquired.set()

    creator = threading.Thread(
        target=create_conversation, name="conversation-create"
    )
    deleter = threading.Thread(
        target=wait_to_remove_document, name="document-remove"
    )
    creator.start()
    assert repository_entered.wait(timeout=1)
    deleter.start()
    try:
        assert deletion_lock_attempted.wait(timeout=1)
        assert not deletion_lock_acquired.is_set()
    finally:
        release_repository.set()

    creator.join(timeout=3)
    deleter.join(timeout=3)
    assert not creator.is_alive()
    assert not deleter.is_alive()
    assert "error" not in creation_state
    assert deletion_lock_acquired.is_set()


def test_legacy_gradio_submission_uses_shared_qa_domain_once(
    service_parts,
) -> None:
    service = make_service(service_parts)
    conversation = service.create_legacy_single_turn_conversation(
        TOKEN, ["一.pdf | doc-1"]
    )
    message = service.ask(
        TOKEN, conversation.id, "问题", "joint", "gradio-request"
    )

    assert conversation.conversation.origin == "legacy_gradio"
    assert message.content == "回答"
    report_turns = service.report_turns(TOKEN)
    assert len(report_turns) == 1
    assert report_turns[0].question == "问题"
    assert report_turns[0].answer == "回答"


def test_duplicate_request_calls_engine_once_and_returns_persisted_answer(
    service_parts,
) -> None:
    engine = Engine()
    service = make_service(service_parts, engine)
    conversation = service.create_conversation(TOKEN, ("doc-1",))
    first = service.ask(TOKEN, conversation.id, "问题", "joint", "client-1")
    duplicate = service.ask(TOKEN, conversation.id, "ignored", "joint", "client-1")
    assert duplicate.id == first.id
    assert duplicate.status == "completed"
    assert len(engine.calls) == 1
    assert engine.calls[0][1].question == "问题"
    assert engine.calls[0][1].document_ids == ("doc-1",)
    assert service.telemetry.snapshot()["qa_idempotent_hit"] == 1


def test_concurrent_stream_duplicate_shares_in_flight_reservation(
    service_parts, monkeypatch
) -> None:
    service = make_service(service_parts)
    conversation = service.create_conversation(TOKEN, ("doc-1",))
    reserved = threading.Event()
    release_first_request = threading.Event()
    duplicate_arrived = threading.Event()
    reserve_count = 0

    def reserve():
        nonlocal reserve_count
        reserve_count += 1
        if reserve_count != 1:
            return False
        reserved.set()
        assert release_first_request.wait(5)
        return True

    release_slot = Mock()
    commit_slot = Mock(return_value=True)
    service.worker_pool = SimpleNamespace(
        reserve_question_slot=reserve,
        release_question_slot=release_slot,
        commit_reserved_question=commit_slot,
    )
    service.session_registry.runtime_registry = SimpleNamespace()
    create_turn = service.repository.create_pending_turn

    def observed_create_turn(*args, **kwargs):
        if reserved.is_set():
            duplicate_arrived.set()
        return create_turn(*args, **kwargs)

    monkeypatch.setattr(service.repository, "create_pending_turn", observed_create_turn)

    def ask():
        return service.ask(
            TOKEN, conversation.id, "问题", "joint", "duplicate", stream=True
        )

    def repeat():
        try:
            return ask()
        finally:
            duplicate_arrived.set()

    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(ask)
        try:
            assert reserved.wait(5)
            duplicate = executor.submit(repeat)
            assert duplicate_arrived.wait(5)
        finally:
            release_first_request.set()

    assert duplicate.result().id == first.result().id
    assert reserve_count == 1
    commit_slot.assert_called_once()
    release_slot.assert_not_called()
    assert len(service.list_messages(TOKEN, conversation.id).items) == 2


def test_stream_conversation_conflict_releases_only_its_reservation(service_parts) -> None:
    service = make_service(service_parts)
    conversation = service.create_conversation(TOKEN, ("doc-1",))
    pending = service.repository.create_pending_turn(
        OWNER, conversation.id, "原问题", "joint", "original"
    )
    worker = SimpleNamespace(
        reserve_question_slot=Mock(return_value=True),
        release_question_slot=Mock(),
        commit_reserved_question=Mock(),
    )
    service.worker_pool = worker

    with pytest.raises(QaBusyError):
        service.ask(TOKEN, conversation.id, "另一条问题", "joint", "new", stream=True)

    duplicate = service.ask(TOKEN, conversation.id, "原问题", "joint", "original", stream=True)
    assert duplicate.id == pending.assistant_message.id
    worker.reserve_question_slot.assert_called_once()
    worker.release_question_slot.assert_called_once()
    worker.commit_reserved_question.assert_not_called()
    assert len(service.list_messages(TOKEN, conversation.id).items) == 2


def test_compare_and_missing_fixed_scope_fail_before_engine(service_parts) -> None:
    engine = Engine()
    service = make_service(service_parts, engine)
    conversation = service.create_conversation(TOKEN, ("doc-1",))
    with pytest.raises(QaValidationError) as compare:
        service.ask(TOKEN, conversation.id, "比较", "compare", "client-1")
    assert compare.value.code == "QA_COMPARE_REQUIRES_MULTIPLE_DOCUMENTS"

    service.document_library.items[TOKEN] = ()
    with pytest.raises(QaNotFoundError):
        service.ask(TOKEN, conversation.id, "问题", "joint", "client-2")
    assert engine.calls == []


def test_engine_failure_persists_only_safe_code_and_trace(service_parts) -> None:
    engine = Engine([RuntimeError("C:/secret/raw prompt")])
    service = make_service(service_parts, engine)
    conversation = service.create_conversation(TOKEN, ("doc-1",))
    with pytest.raises(QaEngineUnavailableError) as failure:
        service.ask(TOKEN, conversation.id, "敏感问题", "joint", "client-1")
    messages = service.list_messages(TOKEN, conversation.id).items
    assistant = next(item for item in messages if item.role == "assistant")
    assert assistant.status == "failed"
    assert assistant.safe_error_code == "QA_ENGINE_UNAVAILABLE"
    assert assistant.trace_id == failure.value.trace_id
    assert "secret" not in repr(assistant)


def test_lowercase_rag_error_is_mapped_to_retryable_safe_domain_code(
    service_parts,
) -> None:
    engine = Engine([QaEngineError("rag_connection", retryable=True)])
    service = make_service(service_parts, engine)
    conversation = service.create_conversation(TOKEN, ("doc-1",))
    with pytest.raises(QaEngineUnavailableError) as failure:
        service.ask(TOKEN, conversation.id, "问题", "joint", "client-1")
    assistant = next(
        item
        for item in service.list_messages(TOKEN, conversation.id).items
        if item.role == "assistant"
    )
    assert failure.value.code == "QA_RAG_CONNECTION"
    assert assistant.safe_error_code == "QA_RAG_CONNECTION"


def test_retry_reuses_question_and_links_new_assistant(service_parts) -> None:
    engine = Engine(
        [
            QaEngineError("RAG_CONNECTION_FAILED", retryable=True),
            QaAnswerResult("恢复回答", (), "joint"),
        ]
    )
    service = make_service(service_parts, engine)
    conversation = service.create_conversation(TOKEN, ("doc-1",))
    with pytest.raises(QaEngineUnavailableError):
        service.ask(TOKEN, conversation.id, "原问题", "joint", "client-1")
    failed = next(
        item
        for item in service.list_messages(TOKEN, conversation.id).items
        if item.role == "assistant"
    )

    retried = service.retry(TOKEN, failed.id, "client-2")
    duplicate = service.retry(TOKEN, failed.id, "client-2")
    assert retried.status == "completed"
    assert duplicate.id == retried.id
    assert retried.retry_of_message_id == failed.id
    assert engine.calls[-1][1].question == "原问题"
    assert len(engine.calls) == 2
    messages = service.list_messages(TOKEN, conversation.id).items
    assert [message.role for message in messages].count("user") == 1
    assert [message.role for message in messages].count("assistant") == 2
    report_turns = service.report_turns(TOKEN)
    assert [(turn.question, turn.answer) for turn in report_turns] == [
        ("原问题", "恢复回答")
    ]

    with pytest.raises(QaRetryNotAllowedError):
        service.retry(TOKEN, retried.id, "client-3")


def test_engine_runs_after_pending_transaction_has_committed(service_parts) -> None:
    db_path = service_parts[0]

    class LockCheckingEngine(Engine):
        def answer(self, runtime, request, **kwargs):
            with connect(db_path) as conn:
                conn.execute("begin immediate")
                conn.rollback()
            return super().answer(runtime, request, **kwargs)

    service = make_service(service_parts, LockCheckingEngine())
    conversation = service.create_conversation(TOKEN, ("doc-1",))
    assert service.ask(
        TOKEN, conversation.id, "问题", "joint", "client-1"
    ).status == "completed"


def test_summary_service_methods_are_user_scoped_and_notify_worker(
    service_parts,
) -> None:
    from app.qa_job_repository import QaJobRepository

    db_path, repository, sessions, library, telemetry = service_parts
    real_jobs = QaJobRepository(db_path)
    worker = Worker()
    service = QaService(
        sessions,
        library,
        repository,
        Engine(),
        QaContextBuilder(2000),
        telemetry,
        job_repository=real_jobs,
        worker_pool=worker,
    )
    conversation = service.create_conversation(TOKEN, ("doc-1",))
    job = service.start_summary(TOKEN, conversation.id, "", "summary-1")
    assert job.status == "queued"
    assert worker.notifications == 1
    assert service.get_job(TOKEN, job.id).id == job.id
    assert service.get_job("other-token", job.id) is None
    active = service.get_active_job(TOKEN, conversation.id)
    assert active is not None
    assert active.id == job.id
    with pytest.raises(QaNotFoundError):
        service.get_active_job("other-token", conversation.id)
    assert service.cancel_job(TOKEN, job.id).status == "cancelled"


def test_product_message_listing_uses_recent_repository_read(
    service_parts, monkeypatch
) -> None:
    service = make_service(service_parts)
    _, repository, _sessions, _library, _telemetry = service_parts
    conversation = service.create_conversation(TOKEN, ["doc-1"])
    recent = repository.list_recent_messages
    recent_spy = Mock(wraps=recent)
    monkeypatch.setattr(repository, "list_recent_messages", recent_spy)

    service.list_messages(TOKEN, conversation.id, limit=7)

    recent_spy.assert_called_once_with(
        OWNER, conversation.id, cursor=None, limit=7
    )
