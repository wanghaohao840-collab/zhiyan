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
