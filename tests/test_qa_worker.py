from __future__ import annotations

import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

import app.qa_job_repository as qa_job_repository_module
from app.bootstrap import ApplicationServices
from app.database import connect, initialize_database
from app.qa_answer_engine import (
    QaAnswerRequest,
    QaAnswerResult,
    QaEngineError,
    RagQaAnswerEngine,
)
from app.qa_context import QaContextBuilder
from app.qa_job_repository import QaJobRepository
from app.qa_memory import QaMemoryLinker
from app.qa_models import QaDocumentCandidate, QaSourceDraft
from app.qa_observability import InProcessQaTelemetry
from app.qa_repository import QaRepository
from app.qa_worker import QaWorkerPool
from hello_agents.core.llm import HelloAgentsLLM
from hello_agents.tools.builtin.rag_context import ContextCapacityError
from hello_agents.tools.builtin.rag_tool import RAGTool


OWNER = "owner"


class RuntimeRegistry:
    def __init__(self):
        self.runtime = SimpleNamespace(name="runtime")
        self.acquired = []
        self.released = []

    def acquire_background(self, user_id):
        self.acquired.append(user_id)
        return self.runtime

    def release_background(self, user_id):
        self.released.append(user_id)


class Engine:
    def __init__(self, outcomes=None, *, block=False):
        self.outcomes = list(outcomes or [])
        self.calls = []
        self.started = threading.Event()
        self.release = threading.Event()
        if not block:
            self.release.set()

    def answer(self, runtime, request, **kwargs):
        self.calls.append((runtime, request, kwargs))
        self.started.set()
        assert self.release.wait(timeout=3)
        callback = kwargs.get("progress_callback")
        if callback:
            callback("generating", 1, 2, "safe")
        outcome = self.outcomes.pop(0) if self.outcomes else None
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome or QaAnswerResult(
            "总结回答",
            (QaSourceDraft("S-1", "doc", "文档.pdf", excerpt="证据"),),
            "summary",
        )


@pytest.fixture
def worker_parts(tmp_path):
    db_path = tmp_path / "app.db"
    initialize_database(db_path)
    with connect(db_path) as conn:
        conn.execute(
            """
            insert into users (
                id, username, username_key, password_hash, created_at, updated_at
            ) values ('owner', 'Owner', 'owner', 'hash',
                      '2026-08-26T00:00:00Z', '2026-08-26T00:00:00Z')
            """
        )
    qa = QaRepository(db_path)
    jobs = QaJobRepository(db_path)
    conversation = qa.create_conversation(
        OWNER, (QaDocumentCandidate("doc", "文档.pdf", OWNER),)
    )
    return qa, jobs, conversation


def wait_for(predicate, timeout=4):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.01)
    raise AssertionError("condition was not reached")


def make_pool(parts, engine, runtime_registry=None, summarizer=None):
    qa, jobs, _ = parts
    registry = runtime_registry or RuntimeRegistry()
    return QaWorkerPool(
        jobs,
        qa,
        registry,
        engine,
        QaContextBuilder(2000),
        InProcessQaTelemetry(),
        summarizer=summarizer,
        worker_count=1,
        lease_seconds=5,
        poll_interval=0.02,
    ), registry


def test_summary_worker_commits_answer_and_releases_runtime(worker_parts) -> None:
    qa, jobs, conversation = worker_parts
    enqueue = jobs.create_summary_turn_and_job(
        OWNER, conversation.id, "总结", "client-1"
    )
    pool, registry = make_pool(worker_parts, Engine())
    pool.start()
    try:
        pool.notify()
        completed = wait_for(
            lambda: (
                job
                if (job := jobs.get(OWNER, enqueue.job.id)).status == "completed"
                else None
            )
        )
    finally:
        pool.stop()
    message = qa.get_message(OWNER, enqueue.pending.assistant_message.id)
    assert completed.progress == 100
    assert message.status == "completed"
    assert message.content == "总结回答"
    assert [source.citation_id for source in message.sources] == ["S-1"]
    assert registry.acquired == registry.released == [OWNER]


def test_cancel_after_model_return_wins_before_atomic_commit(worker_parts) -> None:
    qa, jobs, conversation = worker_parts
    enqueue = jobs.create_summary_turn_and_job(
        OWNER, conversation.id, "总结", "client-1"
    )
    engine = Engine(block=True)
    pool, _ = make_pool(worker_parts, engine)
    pool.start()
    try:
        pool.notify()
        assert engine.started.wait(timeout=3)
        jobs.request_cancel(OWNER, enqueue.job.id)
        engine.release.set()
        wait_for(lambda: jobs.get(OWNER, enqueue.job.id).status == "cancelled")
    finally:
        pool.stop()
    assert qa.get_message(OWNER, enqueue.pending.assistant_message.id).status == "cancelled"


def test_retryable_engine_failure_is_requeued_then_succeeds(worker_parts) -> None:
    qa, jobs, conversation = worker_parts
    enqueue = jobs.create_summary_turn_and_job(
        OWNER, conversation.id, "总结", "client-1", max_attempts=2
    )
    engine = Engine([QaEngineError("rag_connection", True), None])
    pool, _ = make_pool(worker_parts, engine)
    pool.start()
    try:
        pool.notify()
        job = wait_for(
            lambda: (
                current
                if (current := jobs.get(OWNER, enqueue.job.id)).status == "completed"
                else None
            )
        )
    finally:
        pool.stop()
    assert job.attempt_count == 2
    assert len(engine.calls) == 2
    assert qa.get_message(OWNER, enqueue.pending.assistant_message.id).status == "completed"


def test_rolling_summary_refresh_is_deduplicated_and_compare_and_set(
    worker_parts,
) -> None:
    qa, jobs, conversation = worker_parts
    turn = qa.create_pending_turn(
        OWNER, conversation.id, "问题", "joint", "client-1"
    )
    qa.complete_turn(OWNER, turn.assistant_message.id, 0, "回答", (), "none", None)

    class Summarizer:
        def __init__(self):
            self.calls = []

        def summarize(self, runtime, previous_summary, completed_turns):
            self.calls.append((runtime, previous_summary, completed_turns))
            return "滚动摘要"

    summarizer = Summarizer()
    pool, _ = make_pool(worker_parts, Engine(), summarizer=summarizer)
    pool.schedule_summary_refresh(OWNER, conversation.id)
    pool.schedule_summary_refresh(OWNER, conversation.id)
    pool.start()
    try:
        updated = wait_for(
            lambda: (
                item
                if (item := qa.get_conversation(OWNER, conversation.id)).conversation.rolling_summary
                else None
            )
        )
    finally:
        pool.stop()
    assert updated.conversation.rolling_summary == "滚动摘要"
    assert len(summarizer.calls) == 1


def test_worker_processes_deterministic_memory_sync(worker_parts) -> None:
    qa, jobs, conversation = worker_parts
    turn = qa.create_pending_turn(
        OWNER, conversation.id, "问题", "joint", "client-memory"
    )
    qa.complete_turn(OWNER, turn.assistant_message.id, 0, "回答", (), "none", None)

    class Manager:
        def __init__(self):
            self.ids = []

        def add_memory(self, content, **kwargs):
            self.ids.append(kwargs["memory_id"])
            return kwargs["memory_id"]

        def remove_memory(self, memory_id, *, memory_type):
            return True

    registry = RuntimeRegistry()
    manager = Manager()
    registry.runtime.memory_tool = SimpleNamespace(memory_manager=manager)
    pool = QaWorkerPool(
        jobs,
        qa,
        registry,
        Engine(),
        QaContextBuilder(2000),
        InProcessQaTelemetry(),
        memory_linker=QaMemoryLinker(qa),
        worker_count=1,
        lease_seconds=5,
        poll_interval=0.02,
    )
    pool.start()
    try:
        linked = wait_for(
            lambda: (
                message
                if (message := qa.get_message(OWNER, turn.assistant_message.id)).memory_sync_status
                == "completed"
                else None
            )
        )
    finally:
        pool.stop()
    assert linked.memory_id == manager.ids[0]


def test_live_worker_reconciles_final_attempt_expiry_without_notification(
    worker_parts,
    monkeypatch,
) -> None:
    qa, jobs, conversation = worker_parts
    now = datetime(2026, 8, 27, 8, 0, tzinfo=timezone.utc)
    clock = {"value": now.isoformat().replace("+00:00", "Z")}
    monkeypatch.setattr(
        qa_job_repository_module, "_utc_now", lambda: clock["value"]
    )
    enqueued = jobs.create_summary_turn_and_job(
        OWNER,
        conversation.id,
        "总结",
        "client-live-expiry",
        max_attempts=1,
    )
    claimed = jobs.claim_next("lost-worker", lease_seconds=30)
    assert claimed is not None
    engine = Engine()
    pool, _ = make_pool(worker_parts, engine)

    pool.start()
    try:
        assert jobs.get(OWNER, enqueued.job.id).status == "running"
        clock["value"] = (now + timedelta(seconds=31)).isoformat().replace(
            "+00:00", "Z"
        )
        terminal = wait_for(
            lambda: (
                current
                if (current := jobs.get(OWNER, enqueued.job.id)).status == "failed"
                else None
            )
        )
    finally:
        pool.stop()

    assistant = qa.get_message(OWNER, enqueued.pending.assistant_message.id)
    assert terminal.safe_error_code == "QA_JOB_INTERRUPTED"
    assert assistant.status == "failed"
    assert assistant.safe_error_code == "QA_JOB_INTERRUPTED"
    assert jobs.get_active_for_conversation(OWNER, conversation.id) is None
    assert engine.calls == []


def test_live_worker_reconciles_cancel_requested_expiry_without_notification(
    worker_parts,
    monkeypatch,
) -> None:
    qa, jobs, conversation = worker_parts
    now = datetime(2026, 8, 27, 8, 0, tzinfo=timezone.utc)
    clock = {"value": now.isoformat().replace("+00:00", "Z")}
    monkeypatch.setattr(
        qa_job_repository_module, "_utc_now", lambda: clock["value"]
    )
    enqueued = jobs.create_summary_turn_and_job(
        OWNER,
        conversation.id,
        "总结",
        "client-live-cancelled-expiry",
        max_attempts=1,
    )
    claimed = jobs.claim_next("lost-worker", lease_seconds=30)
    assert claimed is not None
    requested = jobs.request_cancel(
        OWNER,
        claimed.id,
        now=(now + timedelta(seconds=1)).isoformat().replace("+00:00", "Z"),
    )
    assert requested.status == "running"
    engine = Engine()
    pool, _ = make_pool(worker_parts, engine)

    pool.start()
    try:
        clock["value"] = (now + timedelta(seconds=31)).isoformat().replace(
            "+00:00", "Z"
        )
        terminal = wait_for(
            lambda: (
                current
                if (current := jobs.get(OWNER, enqueued.job.id)).status
                == "cancelled"
                else None
            )
        )
    finally:
        pool.stop()

    assistant = qa.get_message(OWNER, enqueued.pending.assistant_message.id)
    assert terminal.safe_error_code is None
    assert assistant.status == "cancelled"
    assert assistant.safe_error_code is None
    assert jobs.get_active_for_conversation(OWNER, conversation.id) is None
    assert engine.calls == []

class SummaryPipeline:
    def __init__(self, read_error=None, *, ids_with_errors=()):
        self.reads = []
        self.ids_with_errors = frozenset(ids_with_errors)
        self.read_error = read_error

    def get_document_summary_context(self, document_id, limit):
        self.reads.append(document_id)
        if document_id in self.ids_with_errors:
            raise self.read_error
        return [
            {
                "id": f"chunk-{owner}",
                "content": f"PRIVATE_CONTENT_{owner}",
                "score": 0.9,
                "metadata": {
                    "document_id": owner,
                    "file_name": f"PRIVATE_FILE_{owner}.pdf",
                    "page_number": 3,
                },
            }
            for owner in (document_id, "outside-scope")
        ]


class SummaryLlm:
    def __init__(self):
        self.prompts = []

    def estimate_tokens(self, text):
        return len(text)

    def generate(self, prompt):
        self.prompts.append(prompt)
        return "PRIVATE_ANSWER_资料不存在其他差异"


def summary_runtime(pipeline):
    tool = RAGTool.__new__(RAGTool)
    tool.rag_namespace = "test-summary"
    tool._pipelines = {tool.rag_namespace: pipeline}
    tool._document_summary_cache = {}
    tool._document_summary_cache_lock = threading.RLock()
    tool.llm = SummaryLlm()
    tool.qdrant_api_key = None
    tool.graph_service = None
    return SimpleNamespace(rag_tool=tool)


def start_multi_summary(parts, pipeline, *, enable, count=2):
    qa, jobs, _ = parts
    documents = tuple(
        QaDocumentCandidate(f"doc-{index}", f"{index}.pdf", OWNER)
        for index in range(count)
    )
    conversation = qa.create_conversation(OWNER, documents)
    queued = jobs.create_summary_turn_and_job(
        OWNER, conversation.id, "PRIVATE_QUESTION", "real-summary"
    )
    registry = RuntimeRegistry()
    registry.runtime = summary_runtime(pipeline)
    pool, _ = make_pool(
        parts, RagQaAnswerEngine(enable_summary_workflow=enable), registry
    )
    return pool, registry, queued


@pytest.mark.parametrize("enable", [False, True])
def test_real_summary_worker_projects_sources_and_persists_progress(
    worker_parts, monkeypatch, enable
):
    qa, jobs, _ = worker_parts
    pipeline = SummaryPipeline()
    pool, registry, queued = start_multi_summary(worker_parts, pipeline, enable=enable)
    tool = registry.runtime.rag_tool
    tool.graph_service = SimpleNamespace(
        get_cross_document_entities=lambda document_ids, query, **kwargs: {
            "success": True,
            "data": {
                "entities": [
                    {
                        "canonical_id": "common",
                        "name": "shared term",
                        "members": [{"document_id": doc_id} for doc_id in document_ids],
                    }
                ]
            },
        }
    )
    valid_ref = tool._graph_citation_id("doc-0|doc-1", "canonical_entity", "common")
    unknown_ref = "G-0123456789"
    generate = tool.llm.generate

    def generate_with_citations(prompt):
        answer = generate(prompt)
        if "请给出联合总结：" in prompt:
            return f"{answer} [{valid_ref}] [{unknown_ref}]"
        return answer

    monkeypatch.setattr(tool.llm, "generate", generate_with_citations)
    heartbeat = jobs.heartbeat
    progress = []

    def record(*args, **kwargs):
        if "progress" in kwargs:
            progress.append((kwargs["progress"], kwargs["stage"]))
        return heartbeat(*args, **kwargs)

    monkeypatch.setattr(jobs, "heartbeat", record)
    pool.lease_seconds = 60
    claimed = jobs.claim_next("test-worker", lease_seconds=60)
    pool._run_job("test-worker", claimed)

    job = jobs.get(OWNER, queued.job.id)
    message = qa.get_message(OWNER, queued.pending.assistant_message.id)
    assert job.status == message.status == "completed"
    assert f"PRIVATE_ANSWER_资料不存在其他差异 [{valid_ref}] [未核实引用]" in message.content
    assert unknown_ref not in message.content
    assert all(source.citation_id != unknown_ref for source in message.sources)
    rag_sources = [source for source in message.sources if source.source_type == "rag"]
    assert [(source.document_id, source.page_number) for source in rag_sources] == [
        ("doc-0", 3),
        ("doc-1", 3),
    ]
    graph_source, = [source for source in message.sources if source.source_type == "graph"]
    assert graph_source.document_id == "doc-0"
    assert graph_source.citation_id == valid_ref
    assert graph_source.excerpt == "shared term"
    assert [stage for _, stage in progress] == [
        "mapping", "mapping", "mapping", "mapping", "reducing", "completed",
    ]
    assert [value for value, _ in progress] == sorted(value for value, _ in progress)
    assert registry.acquired == registry.released == [OWNER]
    assert set(pipeline.reads) == {"doc-0", "doc-1"}


@pytest.mark.parametrize("enable", [False, True])
def test_summary_persists_readable_graph_references(worker_parts, enable):
    qa, jobs, _ = worker_parts
    pool, registry, queued = start_multi_summary(worker_parts, SummaryPipeline(), enable=enable)
    pool.lease_seconds = 60
    tool = registry.runtime.rag_tool
    evidence = "保留的图谱证据。" + "甲" * 240 + "不进入模型的尾部"
    tool.graph_service = SimpleNamespace(
        get_graph_context=lambda document_id, query, **kwargs: {"success": True, "data": {
            "entities": [{"id": "root", "name": f"图谱实体 {document_id}", "type": "Concept"}],
            "relations": [
                {"source_id": "root", "target_id": "analysis", "type": "SUPPORTS", "properties": {"evidence": evidence}},
                {"source_id": "root", "target_id": "result", "type": "MENTIONS"},
            ],
        }},
    )
    pool._run_job("test-worker", jobs.claim_next("test-worker", lease_seconds=60))

    job = jobs.get(OWNER, queued.job.id)
    message = qa.get_message(OWNER, queued.pending.assistant_message.id)
    assert job.status == message.status == "completed"
    sources = {source.citation_id: source for source in message.sources if source.source_type == "graph"}
    assert len(sources) == 6
    for document_id in ("doc-0", "doc-1"):
        for kind, graph_id, excerpt in (
            ("entity", "root", f"图谱实体 {document_id}"),
            ("relation", "root|analysis|SUPPORTS", evidence[:240]),
            ("relation", "root|result|MENTIONS", "MENTIONS（暂无图谱证据片段）"),
        ):
            citation_id = tool._graph_citation_id(document_id, kind, graph_id)
            source = sources[citation_id]
            assert source.document_id == document_id
            assert source.document_name == f"PRIVATE_FILE_{document_id}.pdf"
            assert source.excerpt == excerpt
            assert source.reference == f"[{citation_id}] {source.document_name}"


@pytest.mark.parametrize("enable", [False, True])
def test_real_summary_cancellation_stops_replenishing_map(
    worker_parts, monkeypatch, enable
):
    qa, jobs, _ = worker_parts
    pipeline = SummaryPipeline()
    pool, registry, queued = start_multi_summary(
        worker_parts, pipeline, enable=enable, count=6
    )
    heartbeat = jobs.heartbeat

    def cancel_after_first_map(*args, **kwargs):
        if kwargs.get("stage") == "mapping" and kwargs["progress"] > 1:
            jobs.request_cancel(OWNER, queued.job.id)
        return heartbeat(*args, **kwargs)

    monkeypatch.setattr(jobs, "heartbeat", cancel_after_first_map)
    claimed = jobs.claim_next("test-worker", lease_seconds=60)
    pool._run_job("test-worker", claimed)

    job = jobs.get(OWNER, queued.job.id)
    message = qa.get_message(OWNER, queued.pending.assistant_message.id)
    assert job.status == message.status == "cancelled"
    assert not message.sources
    assert set(pipeline.reads) <= {"doc-0", "doc-1", "doc-2"}
    assert all("请给出联合总结" not in prompt for prompt in registry.runtime.rag_tool.llm.prompts)
    assert registry.acquired == registry.released == [OWNER]


@pytest.mark.parametrize("enable", [False, True])
@pytest.mark.parametrize("cancel_at", ["doc-0", "shared"])
@pytest.mark.parametrize("graph_failure", [False, True])
def test_summary_graph_stops_after_sqlite_cancellation(
    worker_parts, caplog, enable, cancel_at, graph_failure,
):
    qa, jobs, _ = worker_parts
    pipeline = SummaryPipeline()
    pool, registry, queued = start_multi_summary(worker_parts, pipeline, enable=enable, count=3)
    pool.lease_seconds = 60
    tool = registry.runtime.rag_tool
    calls = []

    def graph_response(step):
        calls.append(step)
        if step == cancel_at:
            assert jobs.request_cancel(OWNER, queued.job.id).cancel_requested_at
            if graph_failure:
                raise RuntimeError("PRIVATE_GRAPH_FAILURE")
        return {"success": True, "data": {"entities": [{"id": step, "name": step}]}}

    tool.graph_service = SimpleNamespace(
        get_graph_context=lambda document_id, query, **kwargs: graph_response(document_id),
        get_cross_document_entities=lambda document_ids, query, **kwargs: graph_response("shared"),
    )
    claimed = jobs.claim_next("test-worker", lease_seconds=60)
    with caplog.at_level("INFO", logger="hello_agents.tools.builtin.rag_summary_workflow"):
        pool._run_job("test-worker", claimed)

    assert calls == (["doc-0"] if cancel_at == "doc-0" else ["doc-0", "doc-1", "doc-2", "shared"])
    assert not pipeline.reads and not tool.llm.prompts
    job = jobs.get(OWNER, queued.job.id)
    message = qa.get_message(OWNER, queued.pending.assistant_message.id)
    assert job.status == message.status == "cancelled"
    assert job.attempt_count == 1 and job.safe_error_code is None
    assert not message.content and not message.sources
    assert registry.acquired == registry.released == [OWNER]
    records = [record for record in caplog.records if hasattr(record, "qa_summary_stage")]
    assert [(record.qa_summary_stage, record.qa_summary_reason) for record in records] == [
        ("prepare", "progress_aborted"),
    ]
    assert "PRIVATE_GRAPH_FAILURE" not in caplog.text


def test_summary_graph_calls_each_tool_once_and_uses_job_retry(worker_parts):
    qa, jobs, _ = worker_parts
    pipeline = SummaryPipeline(TimeoutError("PRIVATE_PROVIDER"), ids_with_errors=("doc-0", "doc-1"))
    pool, registry, queued = start_multi_summary(worker_parts, pipeline, enable=True)
    claimed = jobs.claim_next("test-worker", lease_seconds=60)
    pool._run_job("test-worker", claimed)

    job = jobs.get(OWNER, queued.job.id)
    assert job.status == "queued"
    assert job.safe_error_code == "QA_RAG_TRANSIENT"
    assert sorted(pipeline.reads) == ["doc-0", "doc-1"]
    assert registry.runtime.rag_tool.llm.prompts == []
    assert registry.acquired == registry.released == [OWNER]


@pytest.mark.parametrize("enable", [False, True])
@pytest.mark.parametrize("change", ["cancel", "reclaim"])
def test_summary_checks_sqlite_ownership_before_map_model_calls(
    worker_parts, monkeypatch, caplog, enable, change,
):
    qa, jobs, _ = worker_parts
    pipeline = SummaryPipeline()
    pool, registry, queued = start_multi_summary(worker_parts, pipeline, enable=enable)
    pool.lease_seconds = 60
    tool = registry.runtime.rag_tool
    read = pipeline.get_document_summary_context
    state_changed = threading.Event()
    clock = datetime.now(timezone.utc)
    monkeypatch.setattr(qa_job_repository_module, "_utc_now", lambda: clock.isoformat())

    def read_after_state_change(document_id, limit):
        nonlocal clock
        if document_id == "doc-0":
            if change == "cancel":
                assert jobs.request_cancel(OWNER, queued.job.id).cancel_requested_at
            else:
                clock += timedelta(seconds=61)
                replacement = jobs.claim_next("test-worker", lease_seconds=60)
                assert replacement.id == queued.job.id and replacement.attempt_count == 2
            state_changed.set()
        assert state_changed.wait(timeout=5)
        return read(document_id, limit)

    monkeypatch.setattr(pipeline, "get_document_summary_context", read_after_state_change)
    claimed = jobs.claim_next("test-worker", lease_seconds=60)
    with caplog.at_level("INFO", logger="hello_agents.tools.builtin.rag_summary_workflow"):
        pool._run_job("test-worker", claimed)

    assert not tool.llm.prompts and not tool._document_summary_cache
    job = jobs.get(OWNER, queued.job.id)
    answer = qa.get_message(OWNER, queued.pending.assistant_message.id)
    if change == "cancel":
        assert (job.status, job.attempt_count, answer.status) == ("cancelled", 1, "cancelled")
    else:
        assert (job.status, job.attempt_count, answer.status) == ("running", 2, "pending")
    assert not answer.content and not answer.sources
    records = [record for record in caplog.records if hasattr(record, "qa_summary_stage")]
    assert [record.qa_summary_stage for record in records] == ["prepare", "map_documents"]
    assert records[-1].qa_summary_reason in {"cancelled", "progress_aborted"}
    assert registry.acquired == registry.released == [OWNER]


@pytest.mark.parametrize("enable", [False, True])
@pytest.mark.parametrize("db_error", ["database is locked", "PRIVATE_DATABASE_ERROR"])
def test_summary_does_not_publish_partial_answers_when_ownership_checks_fail(
    worker_parts, monkeypatch, caplog, enable, db_error,
):
    qa, jobs, _ = worker_parts
    pool, registry, queued = start_multi_summary(worker_parts, SummaryPipeline(), enable=enable)
    pool.lease_seconds = 60
    tool = registry.runtime.rag_tool
    other_document_generated = threading.Event()
    first_check = threading.Lock()
    owns_attempt = jobs.can_continue_attempt
    generate = tool.llm.generate

    def probe(*args):
        if first_check.acquire(blocking=False):
            return owns_attempt(*args)
        assert other_document_generated.wait(timeout=5)
        raise sqlite3.OperationalError(db_error)

    def model(prompt):
        result = generate(prompt)
        other_document_generated.set()
        return result

    monkeypatch.setattr(jobs, "can_continue_attempt", probe)
    monkeypatch.setattr(tool.llm, "generate", model)
    claimed = jobs.claim_next("test-worker", lease_seconds=60)
    with caplog.at_level("INFO", logger="hello_agents.tools.builtin.rag_summary_workflow"):
        pool._run_job("test-worker", claimed)

    job = jobs.get(OWNER, queued.job.id)
    expected = ("queued", "QA_RAG_TRANSIENT") if db_error == "database is locked" else ("failed", "QA_ENGINE_FAILED")
    assert (job.status, job.safe_error_code) == expected
    answer = qa.get_message(OWNER, queued.pending.assistant_message.id)
    assert answer.status == ("pending" if job.status == "queued" else "failed")
    assert not answer.content and not answer.sources
    assert len(tool.llm.prompts) == 1 and "请给出联合总结" not in tool.llm.prompts[0]
    records = [record for record in caplog.records if hasattr(record, "qa_summary_stage")]
    assert records[-1].qa_summary_reason == "ownership_check_failed"
    assert "PRIVATE_DATABASE_ERROR" not in caplog.text


def test_summary_provider_failure_is_neither_a_cached_summary_nor_a_nested_retry(
    monkeypatch,
):
    from httpx import Request
    from openai import APIConnectionError

    monkeypatch.delenv("LLM_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    runtime = summary_runtime(SummaryPipeline())
    llm = HelloAgentsLLM(max_retries=2, retry_backoff=0)
    calls = []

    def create(**kwargs):
        calls.append(kwargs)
        raise APIConnectionError(
            message="PRIVATE_TRANSPORT",
            request=Request("POST", "https://provider.example/v1/chat/completions"),
        )

    llm.client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    runtime.rag_tool.llm = llm
    with pytest.raises(QaEngineError) as raised:
        RagQaAnswerEngine(enable_summary_workflow=True).answer(
            runtime, QaAnswerRequest("PRIVATE_QUESTION", "", ("doc-0", "doc-1"), "summary")
        )

    assert raised.value.retryable
    assert raised.value.code == "rag_operation"
    assert len(calls) == 2
    assert runtime.rag_tool._document_summary_cache == {}


@pytest.mark.parametrize("enable", [False, True])
@pytest.mark.parametrize("failing_stage", ["map", "reduce"])
def test_summary_transport_does_not_retry_after_persisted_cancellation(
    worker_parts, enable, failing_stage,
):
    from httpx import Request
    from openai import APIConnectionError

    qa, jobs, _ = worker_parts
    pool, registry, queued = start_multi_summary(worker_parts, SummaryPipeline(), enable=enable)
    pool.lease_seconds = 60
    llm = HelloAgentsLLM(max_retries=2, retry_backoff=0)
    failing_calls = []
    both_maps_started = threading.Barrier(2, action=lambda: jobs.request_cancel(OWNER, queued.job.id))

    def create(messages, **kwargs):
        assert "before_retry" not in kwargs
        prompt = messages[-1]["content"]
        stage = "reduce" if "请给出联合总结：" in prompt else "map"
        if stage != failing_stage:
            return SimpleNamespace(choices=[SimpleNamespace(
                finish_reason="stop", message=SimpleNamespace(content="合成文档摘要"),
            )])
        failing_calls.append(prompt)
        if stage == "map" and len(failing_calls) <= 2:
            both_maps_started.wait(timeout=5)
        elif stage == "reduce" and len(failing_calls) == 1:
            assert jobs.request_cancel(OWNER, queued.job.id).cancel_requested_at
        raise APIConnectionError(request=Request("POST", "https://provider.example/v1/chat/completions"))

    llm.client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    registry.runtime.rag_tool.llm = llm
    pool._run_job("test-worker", jobs.claim_next("test-worker", lease_seconds=60))

    assert len(failing_calls) == (2 if failing_stage == "map" else 1)
    job = jobs.get(OWNER, queued.job.id)
    answer = qa.get_message(OWNER, queued.pending.assistant_message.id)
    assert job.status == answer.status == "cancelled"
    assert job.attempt_count == 1 and not answer.content and not answer.sources


def test_summary_does_not_send_internal_retry_check_to_custom_transport(worker_parts):
    qa, jobs, _ = worker_parts
    pool, registry, queued = start_multi_summary(worker_parts, SummaryPipeline(), enable=False)
    pool.lease_seconds = 60

    class CustomTransport(HelloAgentsLLM):
        def _chat_with_metadata(self, prompt, *, strict, max_tokens):
            return SimpleNamespace(text="自定义模型摘要", finish_reason="stop")

    registry.runtime.rag_tool.llm = CustomTransport()
    pool._run_job("test-worker", jobs.claim_next("test-worker", lease_seconds=60))

    job = jobs.get(OWNER, queued.job.id)
    answer = qa.get_message(OWNER, queued.pending.assistant_message.id)
    assert job.status == answer.status == "completed"
    assert "自定义模型摘要" in answer.content


def test_summary_graph_keeps_partial_result_and_logs_only_metadata(caplog, monkeypatch):
    monkeypatch.setenv("LANGSMITH_TRACING", "true")
    monkeypatch.setenv("LANGCHAIN_TRACING_V2", "true")
    from langsmith import Client
    from langsmith.utils import tracing_is_enabled

    def fail_if_traced(*args, **kwargs):
        pytest.fail("summary data must never be sent to LangSmith")

    monkeypatch.setattr(Client, "create_run", fail_if_traced)
    monkeypatch.setattr(Client, "update_run", fail_if_traced)
    runtime = summary_runtime(
        SummaryPipeline(RuntimeError("PRIVATE_EXCEPTION"), ids_with_errors=("missing",))
    )
    engine = RagQaAnswerEngine(enable_summary_workflow=True)
    request = QaAnswerRequest("PRIVATE_QUESTION", "", ("doc-0", "missing"), "summary")
    llm = runtime.rag_tool.llm
    generate = llm.generate

    def check_tracing(prompt):
        # Map workers and the reduce node must both see disabled tracing.
        assert not tracing_is_enabled()
        return generate(prompt)

    llm.generate = check_tracing
    with caplog.at_level("INFO", logger="hello_agents.tools.builtin.rag_summary_workflow"):
        first = engine.answer(runtime, request)
        second = engine.answer(runtime, request)

    assert first == second
    assert [source.document_id for source in first.sources] == ["doc-0"]
    assert "以下文档未纳入本次总结：missing" in first.answer
    assert len(llm.prompts) == 3  # first map + two reductions
    assert all("PRIVATE_EXCEPTION" not in prompt for prompt in llm.prompts)
    records = [record for record in caplog.records if hasattr(record, "qa_summary_stage")]
    assert [record.qa_summary_stage for record in records] == [
        "prepare", "map_documents", "collect", "reduce",
    ] * 2
    assert len({record.qa_summary_run_id for record in records}) == 2
    assert all("PRIVATE_" not in str(record.__dict__) for record in records)
    assert "cache_hits=1" in records[-1].getMessage()


def test_concurrent_graph_invocations_do_not_mix_sources():
    pipeline = SummaryPipeline()
    runtime = summary_runtime(pipeline)
    engine = RagQaAnswerEngine(enable_summary_workflow=True)
    barrier = threading.Barrier(2)
    groups = (("aa", "ab"), ("ba", "bb"))

    def answer(documents):
        barrier.wait(timeout=5)
        return engine.answer(
            runtime, QaAnswerRequest("PRIVATE_QUESTION", "", documents, "summary")
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = tuple(executor.map(answer, groups))

    assert [tuple(source.document_id for source in item.sources) for item in results] == list(groups)
    assert set(pipeline.reads) == {doc for group in groups for doc in group}


@pytest.mark.parametrize("flag", ["", "langgraph"])
def test_bootstrap_switch_only_applies_to_background_summary(tmp_path, monkeypatch, flag):
    monkeypatch.setenv("ZHIYAN_MULTI_SUMMARY_WORKFLOW", flag)
    services = ApplicationServices.create(tmp_path / "default")
    assert not services.qa_service.answer_engine.enable_summary_workflow
    assert services.qa_worker_pool.answer_engine.enable_summary_workflow == bool(flag)

    injected = Engine()
    services = ApplicationServices.create(tmp_path / "injected", qa_answer_engine=injected)
    assert services.qa_worker_pool.answer_engine is services.qa_service.answer_engine is injected


def test_summary_graph_avoids_debug_console_output(capsys, monkeypatch, caplog):
    from langchain_core import globals as langchain_globals

    monkeypatch.setattr(langchain_globals, "_debug", True)
    runtime = summary_runtime(SummaryPipeline())
    with caplog.at_level("INFO", logger="hello_agents.tools.builtin.rag_summary_workflow"):
        answer = RagQaAnswerEngine(enable_summary_workflow=True).answer(
            runtime, QaAnswerRequest("PRIVATE_QUESTION", "", ("doc-0", "doc-1"), "summary")
        )

    assert "PRIVATE_ANSWER" in answer.answer
    stdout, stderr = capsys.readouterr()
    assert "PRIVATE_" not in stdout + stderr
    records = [record for record in caplog.records if hasattr(record, "qa_summary_stage")]
    assert [record.qa_summary_stage for record in records] == [
        "prepare", "map_documents", "collect", "reduce",
    ]
    assert {record.qa_summary_engine for record in records} == {"langgraph_debug_fallback"}
    assert {record.qa_summary_job_id for record in records} == {None}
    assert "PRIVATE_" not in caplog.text


def test_summary_job_rejects_late_result_after_lease_takeover(
    worker_parts, monkeypatch
):
    qa, jobs, _ = worker_parts
    pool, registry, queued = start_multi_summary(
        worker_parts, SummaryPipeline(), enable=True
    )
    now = datetime.now(timezone.utc)
    monkeypatch.setattr(qa_job_repository_module, "_utc_now", lambda: now.isoformat())
    llm = registry.runtime.rag_tool.llm
    generate = llm.generate

    def take_over_during_reduce(prompt):
        nonlocal now
        if "请给出联合总结" in prompt:
            now += timedelta(seconds=61)
            replacement = jobs.claim_next("new-worker", lease_seconds=60)
            assert replacement is not None and replacement.id == queued.job.id
            assistant = qa.get_message(OWNER, queued.pending.assistant_message.id)
            jobs.commit_answer(
                replacement.id, "new-worker", assistant.version, "new result", (), "none"
            )
        return generate(prompt)

    llm.generate = take_over_during_reduce
    claimed = jobs.claim_next("old-worker", lease_seconds=60)
    pool._run_job("old-worker", claimed)

    job = jobs.get(OWNER, queued.job.id)
    message = qa.get_message(OWNER, queued.pending.assistant_message.id)
    assert job.status == message.status == "completed"
    assert message.content == "new result"
    assert not message.sources
    assert registry.acquired == registry.released == [OWNER]


def test_slow_summary_keeps_lease_without_progress_events(worker_parts, monkeypatch):
    qa, jobs, conversation = worker_parts
    queued = jobs.create_summary_turn_and_job(OWNER, conversation.id, "总结", "slow")
    now = datetime.now(timezone.utc)
    monkeypatch.setattr(qa_job_repository_module, "_utc_now", lambda: now.isoformat())
    engine = Engine(block=True)
    pool, _ = make_pool(worker_parts, engine)
    pool.lease_seconds = 1
    claimed = jobs.claim_next("slow-worker", lease_seconds=1)
    renewed = threading.Event()
    heartbeat = jobs.heartbeat

    def observe(*args, **kwargs):
        accepted = heartbeat(*args, **kwargs)
        if accepted and kwargs.get("progress") is None:
            renewed.set()
        return accepted

    monkeypatch.setattr(jobs, "heartbeat", observe)
    worker = threading.Thread(target=pool._run_job, args=("slow-worker", claimed))
    worker.start()
    try:
        assert engine.started.wait(2)
        now += timedelta(milliseconds=750)
        assert renewed.wait(1.5), "slow call did not renew its lease"
        now += timedelta(milliseconds=500)
        assert jobs.claim_next("other-worker", lease_seconds=1) is None
    finally:
        engine.release.set()
        worker.join(4)
    assert not worker.is_alive()
    job = jobs.get(OWNER, queued.job.id)
    assert job.status == "completed"
    assert job.attempt_count == 1
    assert qa.get_message(OWNER, queued.pending.assistant_message.id).status == "completed"



def test_summary_renewal_retries_contention_without_losing_progress(worker_parts, monkeypatch):
    _, jobs, conversation = worker_parts
    queued = jobs.create_summary_turn_and_job(OWNER, conversation.id, "总结", "busy")
    engine = Engine(block=True)
    pool, _ = make_pool(worker_parts, engine)
    pool.lease_seconds = 2
    claimed = jobs.claim_next("test-worker", lease_seconds=2)
    assert jobs.heartbeat(
        claimed.id, "test-worker", progress=39, stage="mapping", attempt=1
    )
    busy = threading.Event()
    renewed = threading.Event()
    heartbeat = jobs.heartbeat

    def observe(*args, **kwargs):
        try:
            accepted = heartbeat(*args, **kwargs)
        except sqlite3.OperationalError:
            busy.set()
            raise
        if accepted and "progress" not in kwargs:
            renewed.set()
        return accepted

    monkeypatch.setattr(jobs, "heartbeat", observe)
    blocker = connect(jobs.db_path)
    worker = threading.Thread(target=pool._run_job, args=("test-worker", claimed))
    worker.start()
    try:
        assert engine.started.wait(2)
        blocker.execute("begin immediate")
        try:
            assert busy.wait(2)
        finally:
            blocker.rollback()
        assert renewed.wait(2)
        job = jobs.get(OWNER, queued.job.id)
        assert (job.progress, job.stage, job.attempt_count) == (39, "mapping", 1)
    finally:
        blocker.close()
        engine.release.set()
        worker.join(4)
    assert not worker.is_alive()
    assert jobs.get(OWNER, queued.job.id).status == "completed"


def test_renewal_observes_running_cancel_without_new_progress(worker_parts):
    qa, jobs, conversation = worker_parts
    queued = jobs.create_summary_turn_and_job(OWNER, conversation.id, "总结", "cancel")
    engine_entered = threading.Event()
    engine_stopped = threading.Event()

    class CancelAwareEngine:
        def answer(self, runtime, request, *, cancel_event, **kwargs):
            engine_entered.set()
            try:
                wait_for(cancel_event.is_set, timeout=3)
            finally:
                engine_stopped.set()
            return QaAnswerResult("must not commit", (), "summary")

    pool, _ = make_pool(worker_parts, CancelAwareEngine())
    pool.lease_seconds = 1
    claimed = jobs.claim_next("test-worker", lease_seconds=1)
    worker = threading.Thread(target=pool._run_job, args=("test-worker", claimed))
    worker.start()
    try:
        assert engine_entered.wait(2)
        jobs.request_cancel(OWNER, queued.job.id)
        assert engine_stopped.wait(2)
    finally:
        worker.join(4)
    assert not worker.is_alive()
    assert jobs.get(OWNER, queued.job.id).status == "cancelled"
    assert qa.get_message(OWNER, queued.pending.assistant_message.id).status == "cancelled"


def test_summary_shutdown_does_not_commit_inflight_answer(worker_parts):
    qa, jobs, conversation = worker_parts
    queued = jobs.create_summary_turn_and_job(OWNER, conversation.id, "总结", "shutdown")
    engine_entered = threading.Event()
    release = threading.Event()

    class ClosingEngine:
        def answer(self, runtime, request, *, cancel_event, **kwargs):
            engine_entered.set()
            assert release.wait(4)
            assert cancel_event.is_set()
            return QaAnswerResult("must not commit", (), "summary")

    pool, registry = make_pool(worker_parts, ClosingEngine())
    pool.lease_seconds = 60
    pool.start()
    try:
        assert engine_entered.wait(3)
        renewal, = [t for t in threading.enumerate() if t.name == "qa-summary-renewal"]
        pool.stop(wait=False)
        renewal.join(1.5)
        assert not renewal.is_alive()
    finally:
        release.set()
        pool.stop()
    job = jobs.get(OWNER, queued.job.id)
    assert job.status == "running"
    assert qa.get_message(OWNER, queued.pending.assistant_message.id).status == "pending"
    assert jobs.recover_expired(now=job.lease_expires_at) == 1
    assert jobs.get(OWNER, job.id).status == "queued"
    assert registry.acquired == registry.released == [OWNER]


@pytest.mark.parametrize("enable", [False, True])
@pytest.mark.parametrize(("stage", "finish_reason", "category"), [
    ("map", "length", "output_truncated"),
    ("reduce", "length", "output_truncated"),
    ("reduce", "content_filter", "safety_blocked"),
    ("reduce", "tool_calls", "abnormal_finish"),
    ("reduce", "function_call", "abnormal_finish"),
    ("reduce", "PRIVATE_FINISH_REASON", "abnormal_finish"),
    ("reduce", "stop", "empty_output"),
])
def test_summary_does_not_save_incomplete_model_output(
    worker_parts, monkeypatch, caplog, enable, stage, finish_reason, category
):
    qa, jobs, _ = worker_parts
    pool, registry, queued = start_multi_summary(
        worker_parts, SummaryPipeline(), enable=enable
    )
    monkeypatch.setenv("SUMMARY_MAP_MAX_OUTPUT_TOKENS", "640")
    monkeypatch.setenv("SUMMARY_REDUCE_MAX_OUTPUT_TOKENS", "1280")
    llm = HelloAgentsLLM(max_retries=2, retry_backoff=0)
    calls = []

    def create(messages, max_tokens, **kwargs):
        current = "map" if "请仅根据以下资料总结文档" in messages[-1]["content"] else "reduce"
        calls.append((current, max_tokens))
        invalid = current == stage
        content = None if invalid and (stage == "map" or finish_reason == "stop") else (
            "TRUNCATED_OUTPUT_SENTINEL" if invalid else "模型摘要已生成。"
        )
        return SimpleNamespace(choices=[SimpleNamespace(
            finish_reason=finish_reason if invalid else "stop",
            message=SimpleNamespace(content=content, reasoning_content="PRIVATE_REASONING"),
        )])

    llm.client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    tool = registry.runtime.rag_tool
    tool.llm = llm
    reserves = []
    context_budget = tool._context_budget

    def budget(fixed_prompt, *, output_reserve=1024):
        reserves.append(output_reserve)
        return context_budget(fixed_prompt, output_reserve=output_reserve)

    monkeypatch.setattr(tool, "_context_budget", budget)
    claimed = jobs.claim_next("test-worker", lease_seconds=60)
    pool.lease_seconds = 60
    with caplog.at_level("INFO", logger="hello_agents.tools.builtin.rag_summary_workflow"):
        pool._run_job("test-worker", claimed)

    job = jobs.get(OWNER, queued.job.id)
    answer = qa.get_message(OWNER, queued.pending.assistant_message.id)
    assert job.status == answer.status == "failed"
    assert job.safe_error_code == answer.safe_error_code == "QA_SUMMARY_INCOMPLETE"
    assert job.attempt_count == 1
    assert not answer.content and not answer.sources
    assert calls == ([("map", 640)] * 2 + ([("reduce", 1280)] if stage == "reduce" else []))
    assert reserves == ([640] * 2 + ([1280] if stage == "reduce" else []))
    assert len(tool._document_summary_cache) == (2 if stage == "reduce" else 0)
    records = [item for item in caplog.records if hasattr(item, "qa_summary_stage")]
    assert records
    assert len({item.qa_summary_run_id for item in records}) == 1
    assert {item.qa_summary_job_id for item in records} == {job.id}
    assert {item.qa_summary_attempt for item in records} == {1}
    assert {item.qa_summary_engine for item in records} == {
        "langgraph" if enable else "direct"
    }
    if stage == "map":
        assert [item.qa_summary_stage for item in records] == ["prepare", "map_documents", "collect"]
        assert records[1].qa_summary_output_issues == {category: 2}
    else:
        assert [item.qa_summary_stage for item in records] == [
            "prepare", "map_documents", "collect", "reduce",
        ]
        assert records[-1].qa_summary_reason == category
        assert records[-1].qa_summary_status == "error"

    logs = "\n".join(str(item.__dict__) + item.getMessage() for item in records)
    for value in ("TRUNCATED_OUTPUT_SENTINEL", "PRIVATE_", OWNER, "doc-0"):
        assert value not in logs


@pytest.mark.parametrize("enable", [False, True])
@pytest.mark.parametrize(("scenario", "status", "code", "model_calls"), [
    ("reduce", "failed", "QA_SUMMARY_CONTEXT_CAPACITY", 2),
    ("all_maps", "failed", "QA_SUMMARY_CONTEXT_CAPACITY", 0),
    ("mixed_transient", "queued", "QA_RAG_TRANSIENT", 0),
    ("mixed_unknown", "failed", "QA_ENGINE_FAILED", 0),
    ("partial", "completed", None, 2),
    ("config", "failed", "QA_ENGINE_FAILED", 0),
])
def test_summary_reports_only_confirmed_capacity_failures(
    worker_parts, monkeypatch, caplog, enable, scenario, status, code, model_calls,
):
    qa, jobs, _ = worker_parts
    pipeline = SummaryPipeline(
        TimeoutError("PRIVATE_PROVIDER") if scenario == "mixed_transient" else RuntimeError("PRIVATE_ERROR"),
        ids_with_errors=("doc-1",) if scenario.startswith("mixed_") else (),
    )
    pool, registry, queued = start_multi_summary(worker_parts, pipeline, enable=enable)
    tool = registry.runtime.rag_tool

    def budget(prompt, **kwargs):
        if "单篇文档摘要：" in prompt:
            return 30 if scenario == "reduce" else 4096
        if scenario == "all_maps" or (
            scenario in {"mixed_transient", "mixed_unknown", "partial"} and "doc-0" in prompt
        ):
            raise ContextCapacityError("PRIVATE_BUDGET_DETAIL")
        return 4096

    monkeypatch.setattr(tool, "_context_budget", budget)
    if scenario == "config":
        monkeypatch.setenv("SUMMARY_MAP_MAX_OUTPUT_TOKENS", "invalid")
    claimed = jobs.claim_next("test-worker", lease_seconds=60)
    with caplog.at_level("INFO", logger="hello_agents.tools.builtin.rag_summary_workflow"):
        pool._run_job("test-worker", claimed)

    job = jobs.get(OWNER, queued.job.id)
    answer = qa.get_message(OWNER, queued.pending.assistant_message.id)
    assert (job.status, job.safe_error_code, job.attempt_count) == (status, code, 1)
    assert len(tool.llm.prompts) == model_calls
    if status == "failed":
        assert (answer.status, answer.safe_error_code) == (status, code)
        assert not answer.content and not answer.sources
    elif status == "completed":
        assert "以下文档未纳入本次总结：doc-0" in answer.content
        assert {source.document_id for source in answer.sources} == {"doc-1"}
    capacity_stages = [
        record.qa_summary_stage for record in caplog.records
        if getattr(record, "qa_summary_reason", None) == "context_capacity"
    ]
    expected_stages = {"reduce": ["reduce"], "all_maps": ["collect"]}
    assert capacity_stages == expected_stages.get(scenario, [])
    assert "PRIVATE_BUDGET_DETAIL" not in caplog.text


@pytest.mark.parametrize("enable", [False, True])
@pytest.mark.parametrize("client_kind", ["legacy", "metadata"])
def test_summary_can_discuss_context_capacity(worker_parts, enable, client_kind):
    qa, jobs, _ = worker_parts
    pool, registry, queued = start_multi_summary(worker_parts, SummaryPipeline(), enable=enable)
    pool.lease_seconds = 60
    tool = registry.runtime.rag_tool
    generate = tool.llm.generate
    map_text = "文档解释 context window 对检索的影响。"
    reduce_text = "两篇资料都讨论了上下文长度和 token limit。"

    def answer(prompt):
        generate(prompt)
        return reduce_text if "请给出联合总结：" in prompt else map_text

    def metadata(prompt, **kwargs):
        return SimpleNamespace(text=answer(prompt), finish_reason="stop")

    if client_kind == "metadata":
        tool.llm.generate_with_metadata = metadata
    else:
        tool.llm.generate = answer
    pool._run_job("test-worker", jobs.claim_next("test-worker", lease_seconds=60))

    job = jobs.get(OWNER, queued.job.id)
    message = qa.get_message(OWNER, queued.pending.assistant_message.id)
    assert job.status == message.status == "completed"
    assert len(tool.llm.prompts) == 3 and tool.llm.prompts[-1].count(map_text) == 2
    assert reduce_text in message.content and len(message.sources) == 2


@pytest.mark.parametrize("enable", [False, True])
@pytest.mark.parametrize(("failure", "status", "code"), [
    ("legacy_capacity", "failed", "QA_SUMMARY_CONTEXT_CAPACITY"),
    ("legacy_other", "failed", "QA_SUMMARY_INCOMPLETE"),
    ("http_capacity", "failed", "QA_SUMMARY_CONTEXT_CAPACITY"),
    ("http_other", "failed", "QA_ENGINE_FAILED"),
])
def test_summary_keeps_explicit_provider_capacity_failures_distinct(
    worker_parts, caplog, enable, failure, status, code,
):
    from httpx import Request, Response
    from openai import BadRequestError

    qa, jobs, _ = worker_parts
    pool, registry, queued = start_multi_summary(worker_parts, SummaryPipeline(), enable=enable)
    pool.lease_seconds = 60
    tool = registry.runtime.rag_tool
    generate = tool.llm.generate
    detail = "maximum context length exceeded" if failure.endswith("capacity") else "unsupported option"

    def legacy(prompt):
        text = generate(prompt)
        return f"[LLM调用失败] {detail} PRIVATE_PROVIDER" if "请给出联合总结：" in prompt else text

    def metadata(prompt, **kwargs):
        text = generate(prompt)
        if "请给出联合总结：" in prompt:
            request = Request("POST", "https://provider.example/v1/chat/completions")
            raise BadRequestError(f"{detail} PRIVATE_PROVIDER", response=Response(400, request=request), body=None)
        return SimpleNamespace(text=text, finish_reason="stop")

    if failure.startswith("legacy"):
        tool.llm.generate = legacy
    else:
        tool.llm.generate_with_metadata = metadata
    with caplog.at_level("INFO", logger="hello_agents.tools.builtin.rag_summary_workflow"):
        pool._run_job("test-worker", jobs.claim_next("test-worker", lease_seconds=60))

    job = jobs.get(OWNER, queued.job.id)
    message = qa.get_message(OWNER, queued.pending.assistant_message.id)
    assert (job.status, job.safe_error_code) == (status, code)
    assert (message.status, message.safe_error_code) == (status, code)
    assert not message.content and not message.sources
    assert len(tool.llm.prompts) == 3 and "PRIVATE_PROVIDER" not in caplog.text


@pytest.mark.parametrize("reason", ["stop", None])
def test_summary_reuses_only_completed_maps_at_the_same_budget(monkeypatch, reason):
    monkeypatch.delenv("SUMMARY_MAP_MAX_OUTPUT_TOKENS", raising=False)
    monkeypatch.delenv("SUMMARY_REDUCE_MAX_OUTPUT_TOKENS", raising=False)
    runtime = summary_runtime(SummaryPipeline())
    llm = HelloAgentsLLM()
    calls = []

    def create(messages, max_tokens, **kwargs):
        prompt = messages[-1]["content"]
        calls.append(max_tokens)
        is_bad_map = "请仅根据以下资料总结文档 doc-1。" in prompt
        choice = SimpleNamespace(message=SimpleNamespace(content="前半句"))
        if reason:
            choice.finish_reason = reason
        if is_bad_map:
            choice.finish_reason = "length"
        return SimpleNamespace(choices=[choice])

    llm.client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    runtime.rag_tool.llm = llm
    engine = RagQaAnswerEngine(enable_summary_workflow=True)
    request = QaAnswerRequest("概括", "", ("doc-0", "doc-1"), "summary")

    for expected_calls in (3, 2):
        calls.clear()
        answer = engine.answer(runtime, request)
        assert calls == [1024] * expected_calls
        assert {source.document_id for source in answer.sources} == {"doc-0"}
        assert "以下文档未纳入本次总结：doc-1" in answer.answer

    monkeypatch.setenv("SUMMARY_MAP_MAX_OUTPUT_TOKENS", "640")
    calls.clear()
    engine.answer(runtime, request)
    assert calls == [640, 640, 1024]
    assert len(runtime.rag_tool._document_summary_cache) == 2


@pytest.mark.parametrize("limit", ["invalid", "0", "8193", "4096"])
def test_summary_rejects_invalid_budget_before_model_calls(monkeypatch, limit):
    monkeypatch.setenv("SUMMARY_MAP_MAX_OUTPUT_TOKENS", limit)
    runtime = summary_runtime(SummaryPipeline())
    llm = runtime.rag_tool.llm
    llm.context_window_tokens = 4096
    request = QaAnswerRequest("概括", "", ("doc-0", "doc-1"), "summary")

    with pytest.raises(QaEngineError) as failure:
        RagQaAnswerEngine().answer(runtime, request)

    assert not failure.value.retryable
    assert not llm.prompts


@pytest.mark.parametrize("enable", [False, True])
def test_summary_model_override_is_snapshotted_and_cache_is_model_specific(
    monkeypatch, enable,
):
    monkeypatch.setenv("SUMMARY_MODEL_ID", "   ")
    runtime = summary_runtime(SummaryPipeline())
    llm = HelloAgentsLLM(model="global-model")
    calls = []

    def create(model, messages, **kwargs):
        prompt = messages[-1]["content"]
        stage = "map" if "请仅根据以下资料总结文档" in prompt else "reduce"
        calls.append((stage, model))
        if stage == "map" and model == "summary-model":
            monkeypatch.setenv("SUMMARY_MODEL_ID", "next-model")
        return SimpleNamespace(choices=[SimpleNamespace(
            finish_reason="stop", message=SimpleNamespace(content="完整摘要"),
        )])

    llm.client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    runtime.rag_tool.llm = llm
    engine = RagQaAnswerEngine(enable_summary_workflow=enable)
    request = QaAnswerRequest("概括", "", ("doc-0", "doc-1"), "summary")
    engine.answer(runtime, request)
    assert calls == [("map", "global-model")] * 2 + [("reduce", "global-model")]

    monkeypatch.setenv("SUMMARY_MODEL_ID", " summary-model ")
    calls.clear()
    engine.answer(runtime, request)
    assert calls == [("map", "summary-model")] * 2 + [("reduce", "summary-model")]

    monkeypatch.setenv("SUMMARY_MODEL_ID", "summary-model")
    calls.clear()
    engine.answer(runtime, request)
    assert calls == [("reduce", "summary-model")]
    assert llm.generate("普通问答") == "完整摘要"
    assert calls[-1][1] == llm.model == "global-model"


def test_explicit_summary_model_requires_client_support(monkeypatch):
    monkeypatch.setenv("SUMMARY_MODEL_ID", "different-model")
    runtime = summary_runtime(SummaryPipeline())
    request = QaAnswerRequest("概括", "", ("doc-0", "doc-1"), "summary")

    with pytest.raises(QaEngineError) as failure:
        RagQaAnswerEngine().answer(runtime, request)

    assert failure.value.code == "rag_config"
    assert not failure.value.retryable
    assert not runtime.rag_tool.llm.prompts


def test_invalid_summary_model_is_not_retried_or_replaced(monkeypatch):
    monkeypatch.setenv("SUMMARY_MODEL_ID", "missing")
    runtime = summary_runtime(SummaryPipeline())
    llm = HelloAgentsLLM(model="global-model", max_retries=2, retry_backoff=0)
    models = []

    class ModelRejected(Exception):
        status_code = 400

    def create(model, **kwargs):
        models.append(model)
        raise ModelRejected("502 upstream detail must not trigger a retry")

    llm.client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    runtime.rag_tool.llm = llm
    request = QaAnswerRequest("概括", "", ("doc-0", "doc-1"), "summary")

    with pytest.raises(QaEngineError) as failure:
        RagQaAnswerEngine().answer(runtime, request)

    assert not failure.value.retryable
    assert models == ["missing", "missing"]
    assert llm.model == "global-model"


def test_summary_retry_logs_are_separated_by_attempt(worker_parts, caplog):
    qa, jobs, _ = worker_parts
    pipeline = SummaryPipeline(
        TimeoutError("PRIVATE_TIMEOUT"), ids_with_errors=("doc-0", "doc-1"),
    )
    pool, _, queued = start_multi_summary(worker_parts, pipeline, enable=True)
    pool.lease_seconds = 60

    with caplog.at_level("INFO", logger="hello_agents.tools.builtin.rag_summary_workflow"):
        first = jobs.claim_next("same-worker", lease_seconds=60)
        pool._run_job("same-worker", first)
        assert jobs.get(OWNER, queued.job.id).status == "queued"

        pipeline.ids_with_errors = frozenset()
        second = jobs.claim_next("same-worker", lease_seconds=60)
        pool._run_job("same-worker", second)

    assert jobs.get(OWNER, queued.job.id).status == "completed"
    assert qa.get_message(OWNER, queued.pending.assistant_message.id).sources
    records = [item for item in caplog.records if hasattr(item, "qa_summary_stage")]
    assert {item.qa_summary_job_id for item in records} == {queued.job.id}
    runs = {
        index: {item.qa_summary_run_id for item in records if item.qa_summary_attempt == index}
        for index in (1, 2)
    }
    assert len(runs[1]) == len(runs[2]) == 1
    assert runs[1].isdisjoint(runs[2])
    assert "PRIVATE_TIMEOUT" not in caplog.text
