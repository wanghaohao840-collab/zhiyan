import json
from dataclasses import replace

import pytest

from evals.rag_quality_experiment import BASELINE, Completion, build_index, evaluate_case
from evals.rag_quality_stress import BUCKETS, stress_cases, summarize_stress
from hello_agents.memory.embedding import SimpleEmbedding
from hello_agents.tools.builtin.rag_context import citation_id


def test_stress_fixture_separates_development_and_validation():
    dev_chunks, dev_cases = stress_cases("dev")
    test_chunks, test_cases = stress_cases("holdout")
    assert len(dev_cases) == 10 and len(test_cases) == 30
    assert {c.namespace for c in dev_chunks}.isdisjoint(c.namespace for c in test_chunks)
    assert {c.id for c in dev_cases}.isdisjoint(c.id for c in test_cases)
    assert {c.bucket for c in test_cases} == set(BUCKETS)
    assert all({c.language for c in test_cases if c.bucket == name} == {"zh", "en"} for name in BUCKETS)
    long_scope = {d for c in test_cases if c.bucket == "long_context" for d in c.scope}
    assert min(len(chunk.content) for chunk in test_chunks if chunk.document_id in long_scope) > 580


def test_stress_summary_preserves_route_tokens_and_protocol_retry(tmp_path):
    chunks, cases = stress_cases("dev")
    case = next(case for case in cases if case.bucket == "entity_conjunction")
    pipeline = build_index(chunks, tmp_path, SimpleEmbedding())[case.namespace]
    hit = {**pipeline.get_document_chunks(case.scope[0])[0], "score": 0.7}
    evidence_id = citation_id(hit)
    pipeline.search = lambda **_: [hit]
    replies = iter([
        Completion("", "length", 560, 1024, 1024),
        Completion(json.dumps({
            "quotes": [{"id": evidence_id, "quote": hit["content"]}],
            "claims": [{"text": f"{case.facts[0]} 秒", "ids": [evidence_id]}],
        }), "stop", 600, 350, 250),
    ])
    variant = replace(BASELINE, name="single_pass", verify_evidence=True, evidence_strategy="single_pass")
    row = evaluate_case(pipeline, case, variant, generate=lambda _: next(replies), repeats=0)
    summary = summarize_stress([row])[case.bucket]["single_pass"]
    assert summary["answer_accuracy"] == 1
    assert summary["single_pass"] == 1 and summary["llm_calls"] == 2
    assert summary["empty_completions"] == summary["length_finishes"] == summary["protocol_retries"] == 1
    assert summary["reasoning_tokens_mean"] == 637
    assert row["visible_chunk_ids"] == [hit["id"]]


def test_adaptive_context_trims_without_silently_dropping_candidates():
    from types import SimpleNamespace

    from hello_agents.tools.builtin.rag_context import ContextCapacityError, fit_context

    chunks = [
        {"id": f"chunk-{i}", "content": f"Direct evidence {i}. " + "padding " * 25,
         "score": 1.0 - i / 10, "metadata": {"document_id": f"doc-{i}"}}
        for i in (1, 2, 3)
    ]
    options = {
        "llm": SimpleNamespace(), "format_source": lambda *_: "",
        "min_truncated_chars": 60, "token_budget": 300,
    }
    baseline = fit_context(chunks, **options)
    preserved = fit_context(chunks, retain_candidates=True, **options)
    assert len(baseline.results) < len(chunks)
    assert len(preserved.results) == len(chunks)
    assert preserved.truncated
    assert len(preserved.context) <= 300
    for chunk, result in zip(chunks, preserved.results, strict=True):
        assert chunk["content"].startswith(result["content"])
        assert len(result["content"]) >= 60
    with pytest.raises(ContextCapacityError, match="minimum semantic content"):
        fit_context(chunks, retain_candidates=True, **{**options, "token_budget": 100})


def test_adaptive_empty_response_uses_one_shorter_retry_for_both_phases():
    from hello_agents.tools.builtin.rag_grounding import EvidenceProtocolError, grounded_answer

    prompts = []
    outputs = iter([
        "",
        '{"quotes":[{"id":"S-a","quote":"The check takes 12 minutes."}]}',
        "",
    ])

    def generate(prompt):
        prompts.append(prompt)
        return next(outputs)

    with pytest.raises(EvidenceProtocolError):
        grounded_answer(
            generate, "How long until the check, and which report is used?", "", "Text",
            [{"citation_id": "S-a", "content": "The check takes 12 minutes."}],
            strategy="two_pass", compact_empty_retry=True,
        )
    assert len(prompts) == 3
    assert len(prompts[1]) < len(prompts[0])
    assert "12 minutes" in prompts[2]


def test_full_context_keeps_headroom_for_first_prompt_and_retry(tmp_path):
    chunks, cases = stress_cases("dev")
    case = next(case for case in cases if case.bucket == "long_context" and case.language == "en")
    pipeline = build_index(chunks, tmp_path, SimpleEmbedding())[case.namespace]
    hits = [
        {**hit, "score": 0.9} for doc in case.scope for hit in pipeline.get_document_chunks(doc)
    ][:5]
    pipeline.search = lambda **_: hits
    observed = []

    def generate(prompt, *, max_tokens):
        observed.append((prompt, max_tokens))
        return "" if len(observed) == 1 else '{"quotes":[]}'

    variant = replace(
        BASELINE, name="adaptive", verify_evidence=True, evidence_strategy="adaptive",
        evidence_max_tokens=4096, use_application_engine=True,
    )
    row = evaluate_case(pipeline, case, variant, generate=generate, repeats=0)
    assert row["evidence_status"] == "insufficient"
    assert row["context_cut"]
    assert row["context_dropped"] == 0
    assert len(row["visible_chunk_ids"]) == len(hits)
    assert len(observed) == 2
    assert all(len(prompt) + cap + 512 < 8192 for prompt, cap in observed)
