import json

from evals.rag_quality_crowding_data import make_crowding_dataset
from evals.rag_quality_crowding_qa import CANDIDATE, CONTROL, replay_sample, replay_trials
from evals.rag_quality_experiment import SimpleEmbedding, build_index
from hello_agents.tools.builtin.rag_context import citation_id


def test_replay_uses_saved_scope_order_and_actual_citation_contract(tmp_path):
    chunks, cases = make_crowding_dataset("holdout")
    assert len(replay_sample(cases)) == 10
    case = replay_sample(cases)[0]
    fixture = [chunk for chunk in chunks if chunk.document_id in case.scope]
    pipelines = build_index(fixture, tmp_path, SimpleEmbedding())
    pipeline = pipelines[case.namespace]
    hits = {item["id"]: item for doc in case.scope for item in pipeline.get_document_chunks(doc)}
    gold = hits[case.evidence[0]]
    other = next(hit for id_, hit in hits.items() if id_ != gold["id"])
    id_ = citation_id(gold)
    rankings = {
        (case.id, CONTROL): {"top": [other["id"]], "top_all": False, "retrieval_ms": 5},
        (case.id, CANDIDATE): {"top": [other["id"], gold["id"]], "top_all": True, "retrieval_ms": 7},
    }

    def reply(prompt, *, max_tokens):
        assert max_tokens == 4096
        if id_ not in prompt:
            return '{"quotes":[],"claims":[]}'
        return json.dumps({
            "quotes": [{"id": id_, "quote": gold["content"]}],
            "claims": [{"text": case.facts[0], "ids": [id_]}],
        })

    control, candidate = replay_trials(pipelines, [case], rankings, reply)
    assert control["answer_scores"]["abstained"]
    assert not control["all_evidence_in_ranked_top"]
    assert candidate["ranked_chunks"] == [other["id"], gold["id"]]
    assert candidate["original_retrieval_ms"] == 7
    assert candidate["answer_scores"]["accuracy"]
    assert candidate["citations"]["facts_supported"] == list(case.facts)
    assert not (control["scope_leaks"] or candidate["scope_leaks"])
