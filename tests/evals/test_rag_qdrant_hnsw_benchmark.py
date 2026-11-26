from types import SimpleNamespace

import numpy as np
import pytest

from evals.rag_qdrant_hnsw_benchmark import (
    DIM, K, QUERY_COUNT, SIZE, checked_points, fixture, query_filter, recall_at_k, telemetry_counters,
)


def test_synthetic_vectors_include_exact_outside_scope_neighbors():
    matrix, queries, payloads = fixture()
    assert matrix.shape == (SIZE, DIM) and queries.shape == (QUERY_COUNT, DIM)
    np.testing.assert_allclose(np.linalg.norm(matrix, axis=1), 1.0, atol=1e-6)
    np.testing.assert_array_equal(matrix[-QUERY_COUNT:], queries)
    assert all(item["tenant"] == "outside" for item in payloads[-QUERY_COUNT:])
    assert sum(item["tenant"] == "target" and item["doc"] == "focused" for item in payloads) >= K


def test_scope_and_payload_check_detects_namespace_and_page_mismatch():
    expected = {
        index: {"tenant": "target", "doc": "selected", "page": index + 1, "text": f"Text {index}"}
        for index in range(K)
    }
    points = [SimpleNamespace(id=index, payload=payload) for index, payload in expected.items()]
    assert checked_points(points, {"tenant": "target", "doc": "selected"}, expected) == list(range(K))
    with pytest.raises(ValueError, match="escaped"):
        checked_points(points, {"tenant": "other"}, expected)
    points[-1] = SimpleNamespace(id=K - 1, payload={**expected[K - 1], "page": 999})
    with pytest.raises(ValueError, match="original synthetic payload"):
        checked_points(points, {"tenant": "target"}, expected)


def test_recall_is_set_overlap_with_exact_neighbors():
    assert recall_at_k([1, 2, 3, 4], [3, 1, 2, 4]) == 1
    assert recall_at_k([1, 2, 3, 4], [3, 1, 9, 10]) == 0.5
    assert query_filter({}) is None
    assert query_filter({"tenant": "target", "doc": "book"}).must[1].match.value == "book"


def test_telemetry_aggregates_named_collection_search_counters():
    response = {"result": {"collections": {"collections": [{
        "id": "measured", "shards": [{"local": {"segments": [
            {"vector_index_searches": [{"unfiltered_hnsw": {"count": 19, "total_duration_micros": 201}}]},
            {"vector_index_searches": [{"unfiltered_hnsw": {"count": 2, "total_duration_micros": 52}, "filtered_plain": {"count": 3}}]},
            {"vector_index_searches": []},
        ]}}],
    }, {"id": "ignore", "shards": []}]}}}
    counters = telemetry_counters(response, "measured")
    assert counters["unfiltered_hnsw"] == {"count": 21, "duration_us": 253}
    assert counters["filtered_plain"]["count"] == 3
    assert counters["unfiltered_exact"]["count"] == 0
