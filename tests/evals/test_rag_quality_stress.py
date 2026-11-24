import json
from dataclasses import replace

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
