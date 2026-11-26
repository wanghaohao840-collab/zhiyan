import json
from collections import defaultdict

from evals.rag_present_evidence_diag import diagnostic_jobs, replay_one
from evals.rag_present_evidence_samples import supplementary_cases
from evals.rag_quality_crowding_scale import make_scale_dataset
from evals.rag_quality_experiment import SimpleEmbedding, build_index


def test_counterfactual_jobs_keep_scope_and_only_change_requested_evidence():
    chunks, cases = make_scale_dataset("dev")
    rankings = {
        case.id: [chunk.id for chunk in chunks if chunk.document_id in case.scope][:5]
        for case in cases
    }
    groups = defaultdict(dict)
    for case, change, ranks in diagnostic_jobs(chunks, cases, rankings):
        groups[case.id][change] = case, ranks
    assert sum(len(group) for group in groups.values()) == 43
    for group in groups.values():
        if "true_unanswerable" in group:
            continue
        case, baseline = group["baseline"]
        _, reordered = group["gold_first"]
        _, replaced = group["replace_leading_decoy"]
        ablated, removed = group["necessary_evidence_removed"]
        assert set(baseline) == set(reordered)
        assert reordered[0] in case.evidence and baseline[0] not in case.evidence
        assert replaced[1:] == baseline[1:] and replaced[0] not in baseline
        assert ablated.scope == case.scope and ablated.question == case.question
        assert not ablated.evidence and not ablated.facts
        assert set(case.evidence).difference(removed) == {case.evidence[-1]}


def test_raw_response_and_validation_failure_are_distinct_with_one_provider_call(tmp_path):
    chunks, cases = make_scale_dataset("dev")
    case = cases[0]
    fixture = [chunk for chunk in chunks if chunk.document_id in case.scope]
    pipeline = build_index(fixture, tmp_path, SimpleEmbedding())[case.namespace]
    calls = []

    def invalid(prompt, **_):
        calls.append(prompt)
        return json.dumps({
            "quotes": [{"id": "S-missing", "quote": "fabricated"}],
            "claims": [{"text": "fabricated", "ids": ["S-missing"]}],
        })

    row = replay_one(pipeline, case, "baseline", list(case.evidence), invalid)
    assert len(calls) == row["provider_requests"] == 1
    assert row["raw_status"] == "nonempty"
    assert row["evidence_status"] == "invalid"
    assert "not present" in row["evidence_error"]


def test_supplemental_samples_include_two_genuine_negative_cases():
    chunks, cases, rankings = supplementary_cases()
    jobs = list(diagnostic_jobs(chunks, cases, rankings))
    assert len(jobs) == 18
    assert sum(name == "true_unanswerable" for _, name, _ in jobs) == 2
    assert sum(name == "necessary_evidence_removed" for _, name, _ in jobs) == 4


def test_provider_failure_does_not_issue_a_second_request(tmp_path):
    chunks, cases = make_scale_dataset("dev")
    case = cases[0]
    fixture = [chunk for chunk in chunks if chunk.document_id in case.scope]
    pipeline = build_index(fixture, tmp_path, SimpleEmbedding())[case.namespace]
    attempts = []

    def unavailable(prompt, **_):
        attempts.append(prompt)
        raise RuntimeError("synthetic provider error")

    row = replay_one(pipeline, case, "baseline", list(case.evidence), unavailable)
    assert len(attempts) == row["provider_requests"] == 1
    assert row["provider_error"] == "RuntimeError"
    assert row["raw_status"] == "no_response"
