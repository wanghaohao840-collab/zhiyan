"""Prompt-budget and stable-source helpers for RAG generation."""

from __future__ import annotations

import copy
import os
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional

from hello_agents.memory.rag.result_utils import (
    MAX_SELECTED_DOCUMENTS,
    content_digest,
    dedupe_results_by_source,
    normalize_page_number,
)


LLM_CONTEXT_WINDOW_TOKENS = int(os.getenv("LLM_CONTEXT_WINDOW_TOKENS", "8192"))
ANSWER_OUTPUT_TOKEN_RESERVE = int(os.getenv("LLM_OUTPUT_RESERVED_TOKENS", "1024"))
DOCUMENT_SUMMARY_OUTPUT_TOKEN_RESERVE = 384
TOKEN_SAFETY_MARGIN = int(
    os.getenv("LLM_CONTEXT_SAFETY_MARGIN_TOKENS", "512")
)
COMPARE_BASE_CHUNKS_PER_DOC = 2
COMPARE_EXTRA_CHUNKS_PER_DOC = 3
MIN_TRUNCATED_CHARS = 200
SUMMARY_MAX_WORKERS = 3


class ContextCapacityError(ValueError):
    """The minimum valid prompt content cannot fit the configured window."""


@dataclass(frozen=True)
class ContextFit:
    context: str
    results: List[Dict[str, Any]]
    truncated: bool


def estimate_tokens(llm: Any, text: str) -> int:
    estimator = getattr(llm, "estimate_tokens", None)
    if callable(estimator):
        return max(0, int(estimator(str(text or ""))))
    return len(str(text or ""))


def summary_output_tokens(stage: str) -> int:
    key = f"SUMMARY_{stage.upper()}_MAX_OUTPUT_TOKENS"
    raw = os.getenv(key, "").strip()
    try:
        tokens = int(raw) if raw else 1024
    except ValueError:
        raise ContextCapacityError(f"{key} 必须是 256 至 8192 之间的整数") from None
    if not 256 <= tokens <= 8192:
        raise ContextCapacityError(f"{key} 必须是 256 至 8192 之间的整数")
    return tokens


def context_budget(
    llm: Any,
    fixed_prompt: str,
    *,
    output_reserve: int = ANSWER_OUTPUT_TOKEN_RESERVE,
    safety_margin: int = TOKEN_SAFETY_MARGIN,
) -> int:
    window = int(
        getattr(llm, "context_window_tokens", LLM_CONTEXT_WINDOW_TOKENS)
    )
    if window <= output_reserve + safety_margin:
        raise ContextCapacityError(
            "LLM context window must exceed output reserve and safety margin"
        )
    budget = window - output_reserve - safety_margin - estimate_tokens(llm, fixed_prompt)
    if budget <= 0:
        raise ContextCapacityError("fixed prompt exceeds the available input budget")
    return budget


def citation_id(item: Dict[str, Any]) -> str:
    metadata = item.get("metadata", {}) or {}
    identity = "|".join(
        (
            str(metadata.get("document_id") or ""),
            str(normalize_page_number(metadata.get("page_number")) or ""),
            content_digest(item.get("content", "")),
        )
    )
    return f"S-{content_digest(identity)[:12]}"


def prepare_results(results: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Dedupe and copy results before any runtime truncation."""

    deduped = dedupe_results_by_source(results, limit=len(results))
    prepared: List[Dict[str, Any]] = []
    for original in deduped:
        item = copy.copy(original)
        item["metadata"] = copy.copy(original.get("metadata", {}) or {})
        item["content"] = str(original.get("content", ""))
        item["citation_id"] = citation_id(original)
        item["truncated"] = False
        prepared.append(item)
    return prepared


def fit_context(
    results: List[Dict[str, Any]],
    *,
    token_budget: int,
    llm: Any,
    format_source: Callable[[Dict[str, Any], bool], str],
    min_truncated_chars: int = MIN_TRUNCATED_CHARS,
) -> ContextFit:
    """Fit copied results into a budget without producing tiny fragments."""

    if token_budget <= 0:
        raise ContextCapacityError("context budget must be positive")

    included = prepare_results(results)

    def render(item: Dict[str, Any]) -> str:
        source = format_source(
            item.get("metadata", {}) or {}, bool(item.get("truncated"))
        )
        label = item["citation_id"]
        header = f"[{label}{' | ' + source if source else ''}]"
        return f"{header}\n{item.get('content', '')}"

    def render_all(items: List[Dict[str, Any]]) -> str:
        return "\n\n".join(render(item) for item in items)

    while len(included) > 1 and estimate_tokens(llm, render_all(included)) > token_budget:
        removable = [
            (index, item)
            for index, item in enumerate(included)
            if not item.get("_protected")
        ]
        if not removable:
            break
        index, _ = min(
            removable,
            key=lambda pair: (float(pair[1].get("score", 0.0)), -pair[0]),
        )
        included.pop(index)

    if not included:
        raise ContextCapacityError("no complete source can fit the context budget")

    current = render_all(included)
    if estimate_tokens(llm, current) <= token_budget:
        return ContextFit(current, included, False)

    originals = [str(item.get("content", "")) for item in included]
    floors = [min(len(text), min_truncated_chars) for text in originals]

    def apply_ratio(ratio: float) -> str:
        for item, original, floor in zip(included, originals, floors):
            target = floor + int((len(original) - floor) * ratio)
            item["content"] = original[:target]
            item["truncated"] = target < len(original)
        return render_all(included)

    minimum = apply_ratio(0.0)
    if estimate_tokens(llm, minimum) > token_budget:
        raise ContextCapacityError(
            "minimum semantic content for the selected sources exceeds capacity"
        )

    low, high = 0.0, 1.0
    best = minimum
    best_contents = [item["content"] for item in included]
    for _ in range(24):
        middle = (low + high) / 2
        candidate = apply_ratio(middle)
        if estimate_tokens(llm, candidate) <= token_budget:
            low = middle
            best = candidate
            best_contents = [item["content"] for item in included]
        else:
            high = middle

    for item, original, fitted in zip(included, originals, best_contents):
        item["content"] = fitted
        item["truncated"] = len(fitted) < len(original)

    return ContextFit(best, included, any(item["truncated"] for item in included))
