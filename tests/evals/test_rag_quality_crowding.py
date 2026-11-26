import json
from hashlib import sha256
from types import SimpleNamespace

import httpx

from evals.rag_quality_crowding import (
    CountedTransport, FROZEN_HASHES, MeasuredEmbedding, compare_case, main, score_retrieval, summarize,
)
from evals.rag_quality_crowding_data import BUCKETS, crowding_fixture_json, make_crowding_dataset
from evals.rag_quality_dataset import load_gold_dataset
from evals.rag_quality_experiment import SimpleEmbedding, build_index
from hello_agents.memory.rag.embedding_client import SiliconFlowEmbedding
from hello_agents.memory.rag.embedding_profile import EmbeddingSettings


def test_fixtures_are_frozen_and_scopes_are_separate(tmp_path):
    partitions = {}
    for split, expected_count in (("dev", 20), ("holdout", 30)):
        fixture = crowding_fixture_json(*make_crowding_dataset(split))
        assert sha256(fixture).hexdigest() == FROZEN_HASHES[split]
        path = tmp_path / f"{split}.json"
        path.write_bytes(fixture)
        chunks, cases = partitions[split] = load_gold_dataset(path)
        assert len(cases) == expected_count
        assert len(chunks) == 25 * expected_count
        assert {case.bucket for case in cases} == set(BUCKETS)
        documents = {chunk.id: chunk.document_id for chunk in chunks}
        for case in cases:
            assert sum(chunk.document_id in case.scope for chunk in chunks) == 24
            if case.bucket == "joint_evidence":
                assert len({documents[id_] for id_ in case.evidence}) == 2
    (dev_chunks, dev_cases), (holdout_chunks, holdout_cases) = partitions.values()
    for field in ("id", "namespace", "document_id", "content"):
        assert {getattr(item, field) for item in dev_chunks}.isdisjoint(
            getattr(item, field) for item in holdout_chunks
        )
    assert {case.question for case in dev_cases}.isdisjoint(case.question for case in holdout_cases)


def test_comparison_uses_same_pool_for_hybrid_and_rerank(tmp_path):
    chunks, cases = make_crowding_dataset("dev")
    case = cases[0]
    fixture = [chunk for chunk in chunks if chunk.document_id in (*case.scope, f"{case.id}-unselected")]
    pipeline = build_index(fixture, tmp_path, SimpleEmbedding())[case.namespace]
    calls = []
    rerank_calls = []

    def track_search(**kwargs):
        calls.append(kwargs)
        return pipeline.search(**kwargs)

    def reverse_rank(query, pool, limit):
        rerank_calls.append((query, [item["id"] for item in pool], limit))
        return pool[::-1]

    rows = compare_case(SimpleNamespace(search=track_search), case, 20, rerank=reverse_rank)
    shared_args = {
        "query": case.question, "document_ids": list(case.scope), "limit": 20, "min_score": 0.12,
        "use_mmr": False, "use_rerank": False,
    }
    assert calls == [
        {"retrieval_mode": "vector", **shared_args}, {"retrieval_mode": "hybrid", **shared_args},
    ]
    vector, hybrid, rerank = rows
    assert rerank_calls == [(case.question, hybrid["pool"], 5)]
    assert rerank["pool"] == hybrid["pool"]
    assert rerank["top"] == hybrid["pool"][::-1][:5]
    assert all(not row["scope_leaks"] for row in rows)
    assert len(vector["top"]) <= 5


def test_failure_attribution_distinguishes_pool_loss_rank_drop_and_scope_leak():
    _, cases = make_crowding_dataset("dev")
    case = next(case for case in cases if case.bucket == "joint_evidence")
    metadata = {"rag_namespace": case.namespace, "document_id": case.scope[0]}
    hits = [{"id": id_, "content": "", "metadata": metadata} for id_ in case.evidence]
    decoy = {"id": "decoy", "content": case.forbidden[0], "metadata": metadata}
    outside = {**decoy, "id": "outside", "metadata": {**metadata, "document_id": "other"}}
    rows = [
        score_retrieval(case, "control", [hits[0], decoy], [hits[0], decoy], 1),
        score_retrieval(case, "control", [*hits, decoy, outside], [hits[0], decoy], 2),
    ]
    assert [row["miss"] for row in rows] == ["pool_miss", "rank_drop"]
    assert [row["scope_leaks"] for row in rows] == [[], ["outside"]]
    metrics = summarize(rows)["overall"]["control"]
    assert metrics["pool_all"] == 0.5
    assert metrics["top_all"] == 0
    assert metrics["forbidden_intrusion"] == 2
    assert metrics["pool_miss"] == metrics["rank_drop"] == 1


def test_provider_retry_is_native_and_reported(monkeypatch):
    responses = iter([503, 200])
    settings = EmbeddingSettings.from_env(
        {"RAG_EMBEDDING_API_KEY": "mock-credential"}, candidate=True,
    )

    def respond(request):
        status = next(responses)
        if status != 200:
            return httpx.Response(status, headers={"Retry-After": "0"})
        return httpx.Response(200, json={
            "model": "BAAI/bge-m3", "data": [{"index": 0, "embedding": [1.0] * 1024}],
        })

    monkeypatch.setattr(httpx, "AsyncHTTPTransport", lambda: httpx.MockTransport(respond))
    transport = CountedTransport()
    wrapper = MeasuredEmbedding(SiliconFlowEmbedding(settings, transport=transport), transport)
    assert len(wrapper.measure(["synthetic"], "query")[0]) == 1024
    stats = wrapper.stages["query"]
    assert stats["configured_batches"] == 1
    assert stats["http_attempts"] == 2
    assert stats["http_retries"] == 1


def test_missing_remote_configuration_writes_diagnosis_without_fallback(tmp_path):
    env = tmp_path / "empty.env"
    env.write_text("", encoding="utf-8")
    output = tmp_path / "output"
    assert main([
        "--split", "dev", "--smoke-limit", "1", "--embedding-env-file", str(env),
        "--output-dir", str(output),
    ]) == 1
    result = json.loads((output / "results.json").read_text(encoding="utf-8"))
    assert result["run"]["status"] == "service_unavailable"
    assert result["run"]["failure_stage"] == "smoke"
    assert not result["run"]["fallback_used"]
    assert result["cases"] == []
