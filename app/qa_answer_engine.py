from __future__ import annotations

from dataclasses import dataclass
from pathlib import PurePath
from typing import Any, Literal, Mapping, Protocol

from app.qa_models import QaMode, QaSourceDraft
from hello_agents.tools.builtin.rag_grounding import EVIDENCE_STRATEGIES


@dataclass(frozen=True)
class QaAnswerRequest:
    question: str
    conversation_context: str
    document_ids: tuple[str, ...]
    mode: QaMode
    limit: int = 5
    structured_output: bool = False


@dataclass(frozen=True)
class QaAnswerResult:
    answer: str
    sources: tuple[QaSourceDraft, ...]
    mode: QaMode
    comparison: Mapping[str, Any] | None = None
    comparison_format: str | None = None
    graph_sources: tuple[QaSourceDraft, ...] = ()


class QaEngineError(RuntimeError):
    def __init__(self, code: str, retryable: bool) -> None:
        super().__init__(code)
        self.code = code
        self.retryable = retryable


class QaAnswerEngine(Protocol):
    def answer(
        self,
        runtime: object,
        request: QaAnswerRequest,
        *,
        progress_callback: object | None = None,
        cancel_event: object | None = None,
    ) -> QaAnswerResult: ...


class RagQaAnswerEngine:
    def __init__(
        self,
        *,
        enable_summary_workflow: bool = False,
        verify_evidence: bool = False,
        evidence_strategy: str = "two_pass",
        evidence_max_tokens: int | str | None = None,
    ) -> None:
        self.enable_summary_workflow = enable_summary_workflow
        if verify_evidence and evidence_strategy not in EVIDENCE_STRATEGIES:
            raise ValueError("RAG_QA_EVIDENCE_STRATEGY must be two_pass, single_pass or adaptive")
        if verify_evidence and evidence_max_tokens not in (None, ""):
            if type(evidence_max_tokens) not in (int, str):
                raise ValueError("RAG_QA_EVIDENCE_MAX_TOKENS must be an integer between 1 and 8192")
            try:
                evidence_max_tokens = int(evidence_max_tokens)
            except ValueError:
                raise ValueError("RAG_QA_EVIDENCE_MAX_TOKENS must be an integer between 1 and 8192") from None
            if not 1 <= evidence_max_tokens <= 8192:
                raise ValueError("RAG_QA_EVIDENCE_MAX_TOKENS must be an integer between 1 and 8192")
        else:
            evidence_max_tokens = None
        self.evidence_max_tokens = evidence_max_tokens
        self.verify_evidence = verify_evidence
        self.evidence_strategy = evidence_strategy

    def answer(
        self,
        runtime: object,
        request: QaAnswerRequest,
        *,
        progress_callback: object | None = None,
        cancel_event: object | None = None,
    ) -> QaAnswerResult:
        kwargs: dict[str, Any] = {
            "query": request.question,
            "conversation_context": request.conversation_context,
            "document_ids": list(request.document_ids),
            "mode": request.mode,
            "limit": request.limit,
            "min_score": 0.12,
            "structured_output": request.structured_output,
        }
        if self.verify_evidence and request.mode == "joint":
            kwargs["verify_evidence"] = True
            if self.evidence_max_tokens is not None:
                kwargs["evidence_max_tokens"] = self.evidence_max_tokens
            if self.evidence_strategy != "two_pass":
                kwargs["evidence_strategy"] = self.evidence_strategy
        if progress_callback is not None:
            kwargs["progress_callback"] = progress_callback
        if cancel_event is not None:
            kwargs["cancel_event"] = cancel_event

        if (
            self.enable_summary_workflow
            and request.mode == "summary"
            and len(request.document_ids) > 1
        ):
            kwargs["use_summary_workflow"] = True

        result = runtime.rag_tool.execute_result("ask", **kwargs)
        if not result.success:
            raise QaEngineError(
                str(result.error_code or "QA_ENGINE_UNAVAILABLE"),
                bool(result.retryable),
            )

        rag_sources = tuple(
            _source_draft(item, "rag")
            for item in _mapping_items(result.data.get("sources"))
        )
        graph_sources = tuple(
            _graph_source_draft(item, request.document_ids)
            for item in _mapping_items(result.data.get("graph_sources"))
        )
        comparison = result.data.get("comparison")
        return QaAnswerResult(
            answer=str(result.message),
            sources=(*rag_sources, *graph_sources),
            mode=request.mode,
            comparison=comparison if isinstance(comparison, Mapping) else None,
            comparison_format=_optional_text(result.data.get("comparison_format")),
            graph_sources=graph_sources,
        )


def _mapping_items(value: object) -> tuple[Mapping[str, Any], ...]:
    if not isinstance(value, (list, tuple)):
        return ()
    return tuple(item for item in value if isinstance(item, Mapping))


def _graph_source_draft(
    raw: Mapping[str, Any], document_ids: tuple[str, ...]
) -> QaSourceDraft:
    members = raw.get("member_document_ids")
    if raw.get("kind") == "canonical_entity" and isinstance(members, (list, tuple)):
        anchors = sorted(doc_id for doc_id in document_ids if doc_id in members)
        if anchors:
            # Storage requires one real document per citation. Anchor the shared
            # citation to a contributing document without changing its stable ID.
            raw = {
                **raw,
                "document_id": anchors[0],
                "document_name": "跨文档图谱",
                "excerpt": raw.get("name"),
            }
    return _source_draft(raw, "graph")


def _source_draft(
    raw: Mapping[str, Any], source_type: Literal["rag", "graph"]
) -> QaSourceDraft:
    page = raw.get("page_number")
    raw_name = str(raw.get("file_name") or raw.get("document_name") or "")
    document_name = PurePath(raw_name.replace("\\", "/")).name
    return QaSourceDraft(
        citation_id=str(raw.get("citation_id") or "")[:100],
        document_id=str(raw.get("document_id") or "")[:200],
        document_name=document_name[:300],
        page_number=page if isinstance(page, int) and not isinstance(page, bool) else None,
        section=_optional_text(raw.get("section"), limit=300),
        excerpt=str(raw.get("excerpt") or "")[:2000],
        reference=str(raw.get("reference") or "")[:1000],
        truncated=bool(raw.get("truncated")),
        source_type=source_type,
    )


def _optional_text(value: object, *, limit: int = 100) -> str | None:
    text = str(value or "").strip()
    return text[:limit] if text else None
