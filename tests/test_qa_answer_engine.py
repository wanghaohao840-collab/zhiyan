from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest

from app.qa_answer_engine import (
    QaAnswerRequest,
    QaEngineError,
    RagQaAnswerEngine,
)
from hello_agents.core.llm import HelloAgentsLLM
from hello_agents.memory.rag.contracts import RAGActionResult
from hello_agents.tools.builtin.rag_tool import RAGTool


class FakeResultTool:
    def __init__(self, result: RAGActionResult):
        self.result = result
        self.calls = []

    def execute_result(self, action, **kwargs):
        self.calls.append((action, kwargs))
        return self.result


def request(**overrides) -> QaAnswerRequest:
    values = {
        "question": "当前问题",
        "conversation_context": "用户：上一问\n助手：上一答",
        "document_ids": ("doc-1", "doc-2"),
        "mode": "compare",
        "limit": 7,
        "structured_output": True,
    }
    values.update(overrides)
    return QaAnswerRequest(**values)


def test_answer_engine_projects_request_local_sources_and_comparison() -> None:
    result = RAGActionResult(
        action="ask",
        success=True,
        message="回答",
        data={
            "sources": [
                {
                    "citation_id": "S-1",
                    "document_id": "doc-1",
                    "file_name": "one.pdf",
                    "page_number": 3,
                    "excerpt": "证据",
                    "reference": "[S-1] one.pdf 第3页",
                }
            ],
            "graph_sources": [
                {
                    "citation_id": "G-1",
                    "document_id": "doc-2",
                    "document_name": "two.pdf",
                    "section": "Concept",
                    "excerpt": "关系",
                    "reference": "[G-1] two.pdf",
                }
            ],
            "comparison": {"common_points": []},
            "comparison_format": "structured",
        },
    )
    tool = FakeResultTool(result)
    progress = object()
    cancel = object()

    answer = RagQaAnswerEngine().answer(
        SimpleNamespace(rag_tool=tool),
        request(),
        progress_callback=progress,
        cancel_event=cancel,
    )

    assert answer.answer == "回答"
    assert [source.source_type for source in answer.sources] == ["rag", "graph"]
    assert answer.sources[0].page_number == 3
    assert answer.graph_sources == (answer.sources[1],)
    assert answer.comparison == {"common_points": []}
    assert answer.comparison_format == "structured"
    action, kwargs = tool.calls[0]
    assert action == "ask"
    assert kwargs["query"] == "当前问题"
    assert kwargs["conversation_context"] == "用户：上一问\n助手：上一答"
    assert kwargs["document_ids"] == ["doc-1", "doc-2"]
    assert kwargs["progress_callback"] is progress
    assert kwargs["cancel_event"] is cancel


def test_answer_engine_omits_absent_callbacks_and_raises_typed_failure() -> None:
    tool = FakeResultTool(
        RAGActionResult(
            action="ask",
            success=False,
            message="safe",
            data={},
            error_code="RAG_CONNECTION_FAILED",
            retryable=True,
        )
    )
    with pytest.raises(QaEngineError) as error:
        RagQaAnswerEngine().answer(
            SimpleNamespace(rag_tool=tool),
            request(mode="joint", structured_output=False),
        )
    assert error.value.code == "RAG_CONNECTION_FAILED"
    assert error.value.retryable is True
    assert "progress_callback" not in tool.calls[0][1]
    assert "cancel_event" not in tool.calls[0][1]


def test_execute_result_keeps_action_data_request_local_across_threads() -> None:
    tool = RAGTool.__new__(RAGTool)
    tool.qdrant_api_key = None
    barrier = threading.Barrier(2)

    def execute(action, **kwargs):
        query = kwargs["query"]
        tool._last_action_data = {
            "success": True,
            "sources": [{"document_id": f"doc-{query}"}],
        }
        barrier.wait(timeout=2)
        if query == "one":
            time.sleep(0.05)
        return f"answer-{query}"

    tool.execute = execute
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = {
            query: pool.submit(tool.execute_result, "ask", query=query)
            for query in ("one", "two")
        }
        results = {query: future.result(timeout=2) for query, future in futures.items()}

    assert results["one"].data["sources"] == [{"document_id": "doc-one"}]
    assert results["two"].data["sources"] == [{"document_id": "doc-two"}]


def test_rag_retrieves_only_with_question_but_final_prompt_receives_history() -> None:
    searches = []

    class Pipeline:
        def search(self, **kwargs):
            searches.append(kwargs)
            return [
                {
                    "id": "chunk-1",
                    "content": "检索证据",
                    "score": 0.9,
                    "metadata": {
                        "document_id": "doc-1",
                        "file_name": "one.pdf",
                    },
                }
            ]

    class Llm:
        def __init__(self):
            self.prompts = []

        def estimate_tokens(self, text):
            return len(str(text))

        def generate(self, prompt, **kwargs):
            self.prompts.append(str(prompt))
            return "回答"

    tool = RAGTool.__new__(RAGTool)
    tool.rag_namespace = "test"
    tool._pipelines = {"test": Pipeline()}
    tool.llm = Llm()
    tool.qdrant_api_key = None
    tool.graph_service = None
    tool.graph_configuration_error = None

    result = tool.execute_result(
        "ask",
        query="当前问题",
        conversation_context="用户：历史问题\n助手：历史回答",
        document_ids=["doc-1"],
        mode="joint",
    )

    assert result.success
    assert searches[0]["query"] == "当前问题"
    assert "历史问题" not in searches[0]["query"]
    assert "用户：历史问题" in tool.llm.prompts[-1]
    assert "当前问题" in tool.llm.prompts[-1]


def test_streaming_rag_reports_observed_stages_and_provider_deltas(monkeypatch):
    events = []
    llm = HelloAgentsLLM()
    llm.client = SimpleNamespace()

    def completion(messages, **kwargs):
        assert kwargs["stream"] is True
        assert "[S-abc123]" in messages[-1]["content"]
        yield SimpleNamespace(choices=[SimpleNamespace(
            delta=SimpleNamespace(content="first"), finish_reason=None,
        )])
        assert events[-1] == ("token", "first")
        yield SimpleNamespace(choices=[SimpleNamespace(
            delta=SimpleNamespace(content=" second"), finish_reason="stop",
        )])

    monkeypatch.setattr(llm, "_create_completion", completion)

    def search(**kwargs):
        assert events == [("retrieving", None)]
        assert kwargs["retrieval_mode"] == "hybrid"
        kwargs["rerank_callback"](3)
        return [{
            "id": "chunk-1", "content": "Evidence", "score": 0.9,
            "metadata": {"document_id": "doc-1", "file_name": "one.pdf"},
        }]

    tool = RAGTool.__new__(RAGTool)
    tool.rag_namespace = "test"
    tool._pipelines = {"test": SimpleNamespace(search=search)}
    tool.llm = llm
    tool.qdrant_api_key = None
    tool.graph_service = None
    tool.graph_configuration_error = None
    result = RagQaAnswerEngine(retrieval_mode="hybrid", use_rerank=True).answer(
        SimpleNamespace(rag_tool=tool),
        request(mode="joint", structured_output=False),
        stage_callback=lambda stage, count: events.append((stage, count)),
        token_callback=lambda delta: events.append(("token", delta)),
    )
    assert events[:4] == [
        ("retrieving", None), ("retrieving", 3), ("reranking", 3), ("generating", None),
    ]
    assert "verifying" not in [event for event, _ in events]
    assert result.answer.startswith("".join(value for event, value in events if event == "token"))
    assert result.sources[0].chunk_id == "chunk-1"


def test_verified_evidence_setting_only_applies_to_joint_qa() -> None:
    tool = FakeResultTool(RAGActionResult(action="ask", success=True, message="ok", data={}))
    runtime = SimpleNamespace(rag_tool=tool)
    RagQaAnswerEngine(verify_evidence=True).answer(runtime, request(mode="joint"))
    RagQaAnswerEngine(verify_evidence=True).answer(runtime, request(mode="compare"))
    RagQaAnswerEngine().answer(runtime, request(mode="joint"))
    RagQaAnswerEngine(verify_evidence=True).answer(runtime, request(mode="auto"))
    RagQaAnswerEngine(verify_evidence=True).answer(runtime, request(mode="auto", question="比较这两篇文章"))
    assert tool.calls[0][1]["verify_evidence"] is True
    assert tool.calls[3][1]["verify_evidence"] is True
    assert all("verify_evidence" not in tool.calls[i][1] for i in (1, 2, 4))


def test_single_pass_evidence_strategy_is_explicit_and_only_used_for_joint_qa():
    tool = FakeResultTool(RAGActionResult(action="ask", success=True, message="ok", data={}))
    runtime = SimpleNamespace(rag_tool=tool)
    engine = RagQaAnswerEngine(verify_evidence=True, evidence_strategy="single_pass")
    engine.answer(runtime, request(mode="joint"))
    engine.answer(runtime, request(mode="compare"))
    RagQaAnswerEngine(verify_evidence=True).answer(runtime, request(mode="joint"))
    assert tool.calls[0][1]["verify_evidence"] is True
    assert tool.calls[0][1]["evidence_strategy"] == "single_pass"
    assert "evidence_strategy" not in tool.calls[1][1]
    assert "evidence_strategy" not in tool.calls[2][1]
    with pytest.raises(ValueError, match="RAG_QA_EVIDENCE_STRATEGY"):
        RagQaAnswerEngine(verify_evidence=True, evidence_strategy="invalid")
