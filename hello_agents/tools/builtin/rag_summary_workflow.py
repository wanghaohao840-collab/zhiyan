from __future__ import annotations

import logging
from collections.abc import Callable
from time import perf_counter
from typing import Any, Literal, TypedDict
from uuid import uuid4


logger = logging.getLogger(__name__)


class SummaryCancelled(RuntimeError):
    """The owning process requested cooperative cancellation."""


class SummaryProgressAborted(RuntimeError):
    """The progress consumer withdrew permission to continue the workflow."""


class SummaryState(TypedDict, total=False):
    document_count: int
    graph_by_document: dict[str, dict[str, Any]]
    shared_graph_contexts: list[dict[str, Any]]
    completed: dict[str, dict[str, Any]]
    failures: dict[str, str]
    mapped: list[dict[str, Any]]
    retryable: bool
    incomplete_output: bool
    error: str
    answer: str
    truncated: bool


SummaryStage = Callable[[SummaryState], SummaryState]


def run_summary_workflow(
    prepare: SummaryStage,
    map_documents: SummaryStage,
    collect: SummaryStage,
    reduce: SummaryStage,
    *,
    use_langgraph: bool,
    document_count: int,
) -> SummaryState:
    stages = (
        ("prepare", prepare),
        ("map_documents", map_documents),
        ("collect", collect),
        ("reduce", reduce),
    )
    initial: SummaryState = {"document_count": document_count}
    def run_direct() -> SummaryState:
        for _, stage in stages:
            initial.update(stage(initial))
            if "error" in initial:
                break
        return initial

    if not use_langgraph:
        return run_direct()

    from langchain_core.globals import get_debug
    from langgraph.graph import END, START, StateGraph
    from langsmith import tracing_context

    # LangChain's global debug console can print inputs even with tracing off.
    if get_debug():
        logger.warning("qa_summary_workflow_disabled reason=langchain_debug")
        with tracing_context(enabled=False):
            return run_direct()

    run_id = uuid4().hex

    def traced(name: str, stage: SummaryStage) -> SummaryStage:
        def node(state: SummaryState) -> SummaryState:
            started = perf_counter()
            status = "error"
            update: SummaryState = {}
            try:
                update = stage(state)
                status = "error" if "error" in update else "ok"
                return update
            except (SummaryCancelled, SummaryProgressAborted):
                status = "cancelled"
                raise
            finally:
                elapsed_ms = round((perf_counter() - started) * 1000, 1)
                mapped = update.get("mapped", state.get("mapped", []))
                failed = len(update.get("failures", state.get("failures", {})))
                if "completed" in update:
                    mapped = [
                        item for item in update["completed"].values()
                        if item["status"] == "ok"
                    ]
                    failed = document_count - len(mapped)
                cache_hits = sum(bool(item.get("cache_hit")) for item in mapped)
                logger.info(
                    "qa_summary stage=%s status=%s duration_ms=%s run_id=%s "
                    "documents=%s mapped=%s failed=%s cache_hits=%s truncated=%s",
                    name,
                    status,
                    elapsed_ms,
                    run_id,
                    document_count,
                    len(mapped),
                    failed,
                    cache_hits,
                    update.get("truncated", False),
                    extra={
                        "qa_summary_stage": name,
                        "qa_summary_status": status,
                        "qa_summary_run_id": run_id,
                    },
                )

        return node

    def route(state: SummaryState) -> Literal["continue", "stop"]:
        return "stop" if "error" in state else "continue"

    graph = StateGraph(SummaryState)
    for name, stage in stages:
        graph.add_node(name, traced(name, stage))
    graph.add_edge(START, "prepare")
    graph.add_conditional_edges(
        "prepare", route, {"continue": "map_documents", "stop": END}
    )
    graph.add_edge("map_documents", "collect")
    graph.add_conditional_edges("collect", route, {"continue": "reduce", "stop": END})
    graph.add_edge("reduce", END)

    # Invocation data includes document text. Keep tracing local even if
    # LangSmith tracing was enabled elsewhere in this process.
    with tracing_context(enabled=False):
        return graph.compile().invoke(initial)
