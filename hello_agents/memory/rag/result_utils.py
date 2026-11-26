"""Utilities for document-scoped RAG result handling."""

from __future__ import annotations

from collections import Counter
import hashlib
import math
import re
import unicodedata
from typing import Any, Optional


MAX_SELECTED_DOCUMENTS = 10
QA_MODES = frozenset({"auto", "joint", "compare", "summary"})
RETRIEVAL_MODES = frozenset({"vector", "hybrid"})
_LEXICAL_TOKEN_RE = re.compile(r"[A-Za-z0-9_]+|[\u4e00-\u9fff]")


def normalize_document_scope(
    document_id: Optional[str] = None,
    document_ids: Optional[list[str]] = None,
) -> Optional[list[str]]:
    """Normalize legacy and multi-document scope parameters."""

    if document_id is not None and document_ids is not None:
        raise ValueError("document_id and document_ids cannot be provided together")

    if document_id is not None:
        doc_id = str(document_id).strip()
        return [doc_id] if doc_id else []

    if document_ids is None:
        return None

    normalized: list[str] = []
    seen: set[str] = set()

    for raw_id in document_ids:
        doc_id = str(raw_id).strip()
        if not doc_id or doc_id in seen:
            continue
        normalized.append(doc_id)
        seen.add(doc_id)

    return normalized


def normalize_page_number(value: Any) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def resolve_qa_mode(
    query: str,
    mode: str = "auto",
    summary_mode: bool = False,
) -> str:
    """Resolve explicit or keyword-based QA mode at every public boundary."""

    if summary_mode:
        return "summary"

    selected = str(mode or "auto").strip().lower()
    if selected not in QA_MODES:
        raise ValueError(f"不支持的问答模式: {mode}")
    if selected != "auto":
        return selected

    query_text = str(query or "")
    compare_words = ("对比", "比较", "区别", "差异", "异同", "共同点")
    summary_words = ("总结", "概括", "综述", "主要内容", "核心内容", "全文")
    if any(word in query_text for word in compare_words):
        return "compare"
    if any(word in query_text for word in summary_words):
        return "summary"
    return "joint"


def _normalize_content_for_digest(content: str) -> str:
    text = unicodedata.normalize("NFKC", content or "")
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    return text.strip()


def content_digest(content: str) -> str:
    normalized = _normalize_content_for_digest(content)
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def dedupe_results_by_source(results: list[dict], limit: int) -> list[dict]:
    deduped: list[dict] = []
    seen: set[tuple[Optional[str], Optional[str], str]] = set()

    for result in results:
        metadata = result.get("metadata") or {}
        key = (
            metadata.get("document_id"),
            normalize_page_number(metadata.get("page_number")),
            content_digest(result.get("content", "")),
        )
        if key in seen:
            continue
        seen.add(key)
        deduped.append(result)
        if len(deduped) >= limit:
            break

    return deduped


def lexical_tokens(text: str) -> set[str]:
    """Return deterministic word/character tokens for lightweight lexical recall."""

    return {token.lower() for token in _LEXICAL_TOKEN_RE.findall(str(text or ""))}


def lexical_overlap_score(query: str, content: str) -> float:
    """Score query-term coverage in ``content`` without external tokenizers."""

    query_tokens = lexical_tokens(query)
    if not query_tokens:
        return 0.0
    return len(query_tokens & lexical_tokens(content)) / len(query_tokens)


def _token_similarity(left: set[str], right: set[str]) -> float:
    if not left or not right:
        return 0.0
    intersection = len(left & right)
    return intersection / (len(left) + len(right) - intersection)


def hybrid_rank_results(
    query: str,
    vector_results: list[dict],
    lexical_results: list[dict],
    limit: int,
    vector_weight: float = 0.7,
) -> list[dict]:
    """Merge vector and lexical candidates with a stable weighted score."""

    weight = min(1.0, max(0.0, float(vector_weight)))
    merged: dict[str, dict] = {}
    for result in [*vector_results, *lexical_results]:
        key = str(result.get("id") or content_digest(result.get("content", "")))
        current = merged.get(key)
        if current is None:
            current = dict(result)
            merged[key] = current
        else:
            current_score = float(current.get("_vector_score", current.get("score", 0.0)))
            candidate_score = float(result.get("_vector_score", result.get("score", 0.0)))
            if candidate_score > current_score:
                current.update(result)

    ranked: list[dict] = []
    for result in merged.values():
        vector_score = max(
            0.0,
            min(1.0, float(result.get("_vector_score", result.get("score", 0.0)))),
        )
        lexical_score = lexical_overlap_score(query, result.get("content", ""))
        result["score"] = weight * vector_score + (1.0 - weight) * lexical_score
        ranked.append(result)

    ranked.sort(
        key=lambda item: (
            -float(item.get("score", 0.0)),
            str(item.get("id", "")),
        )
    )
    for result in ranked:
        result.pop("_vector_score", None)
    return ranked[: max(0, limit)]


RERANK_CANDIDATE_LIMIT = 24


def relevance_rerank_results(query: str, results: list[dict], limit: int) -> list[dict]:
    """Rerank a bounded prefix with rare query terms and adjacent token matches."""
    query_sequence = _ranking_tokens(query)
    if not query_sequence or len(results) < 2:
        return results

    candidates = results[:RERANK_CANDIDATE_LIMIT]
    sequences = [_ranking_tokens(item.get("content", "")) for item in candidates]
    token_sets = [set(sequence) for sequence in sequences]
    frequency = Counter(token for tokens in token_sets for token in tokens)
    weights = {
        token: 1 + math.log((1 + len(candidates)) / (1 + frequency[token]))
        for token in dict.fromkeys(query_sequence)
    }
    total_weight = sum(weights.values())
    query_pairs = set(zip(query_sequence, query_sequence[1:]))
    ranked = []
    for item, sequence, tokens in zip(candidates, sequences, token_sets):
        coverage = sum(weight for token, weight in weights.items() if token in tokens) / total_weight
        adjacency = (
            len(query_pairs.intersection(zip(sequence, sequence[1:]))) / len(query_pairs)
            if query_pairs else coverage
        )
        original_score = max(0.0, min(1.0, float(item["score"])))
        score = 0.5 * original_score + 0.3 * coverage + 0.2 * adjacency
        ranked.append({**item, "score": score})
    ranked.sort(key=lambda item: -item["score"])
    return ranked + results[RERANK_CANDIDATE_LIMIT:max(RERANK_CANDIDATE_LIMIT, limit)]


def _ranking_tokens(text: str) -> list[str]:
    return _LEXICAL_TOKEN_RE.findall(unicodedata.normalize("NFKC", text).lower())


def mmr_select(
    results: list[dict],
    limit: int,
    lambda_mult: float = 0.75,
) -> list[dict]:
    """Select high-scoring but non-redundant chunks using maximal marginal relevance."""

    if limit <= 0 or not results:
        return []
    if len(results) <= limit:
        return list(results)

    tradeoff = min(1.0, max(0.0, float(lambda_mult)))
    remaining = list(range(len(results)))
    tokens = [lexical_tokens(item.get("content", "")) for item in results]
    max_similarity = [0.0] * len(results)
    latest_tokens: set[str] = set()
    selected: list[dict] = []
    while remaining and len(selected) < limit:
        best_index = 0
        best_value = float("-inf")
        for index, result_index in enumerate(remaining):
            candidate = results[result_index]
            relevance = max(0.0, min(1.0, float(candidate.get("score", 0.0))))
            redundancy = max(
                max_similarity[result_index], _token_similarity(tokens[result_index], latest_tokens),
            )
            max_similarity[result_index] = redundancy
            value = tradeoff * relevance - (1.0 - tradeoff) * redundancy
            tie_breaker = str(candidate.get("id", ""))
            best_tie_breaker = str(results[remaining[best_index]].get("id", ""))
            if value > best_value or (
                value == best_value and tie_breaker < best_tie_breaker
            ):
                best_value = value
                best_index = index
        chosen = remaining.pop(best_index)
        selected.append(results[chosen])
        latest_tokens = tokens[chosen]
    return selected


def sample_evenly(items: list[Any], limit: int) -> list[Any]:
    if limit <= 0 or not items:
        return []
    if len(items) <= limit:
        return list(items)
    if limit == 1:
        return [items[0]]

    last = len(items) - 1
    indexes = [round(position * last / (limit - 1)) for position in range(limit)]
    return [items[index] for index in indexes]
