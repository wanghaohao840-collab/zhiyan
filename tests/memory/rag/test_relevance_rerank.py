
from hello_agents.memory.rag.result_utils import RERANK_CANDIDATE_LIMIT, relevance_rerank_results


def test_relevance_rerank_promotes_ordered_phrase_and_preserves_input():
    query = "frost network policy"
    candidates = [
        {"id": "weak", "score": 0.81, "content": "frost policy network"},
        {"id": "strong", "score": 0.80, "content": "frost network policy"},
    ]
    ranked = relevance_rerank_results(query, candidates, limit=1)
    assert [item["id"] for item in ranked] == ["strong", "weak"]
    assert candidates[0]["score"] == 0.81


def test_relevance_rerank_limits_work_to_the_candidate_prefix():
    candidates = [
        {"id": str(number), "score": 0.2, "content": "distractor"}
        for number in range(RERANK_CANDIDATE_LIMIT)
    ]
    tail = {"id": "outside-pool", "score": 1, "content": "frost network policy"}
    ranked = relevance_rerank_results("frost network policy", [*candidates, tail], limit=5)
    assert [item["id"] for item in ranked] == [item["id"] for item in candidates]
    expanded = relevance_rerank_results(
        "frost network policy", [*candidates, tail], limit=RERANK_CANDIDATE_LIMIT + 1,
    )
    assert expanded[-1] is tail
