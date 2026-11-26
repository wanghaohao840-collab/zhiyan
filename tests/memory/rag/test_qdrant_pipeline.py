from datetime import date
from types import SimpleNamespace

import pytest

from hello_agents.memory.rag.contracts import DocumentSegment
from hello_agents.memory.rag.errors import (
    RAGAuthenticationError,
    RAGCollectionError,
    RAGDocumentTooLargeError,
    RAGEmbeddingError,
    RAGOperationError,
)
from hello_agents.memory.rag.pipeline import create_rag_pipeline
from hello_agents.memory.rag.prepare import qdrant_point_id
from hello_agents.memory.rag.qdrant_pipeline import QdrantRAGPipeline


@pytest.fixture(autouse=True)
def isolate_qdrant_collection_env(monkeypatch):
    monkeypatch.delenv("QDRANT_COLLECTION", raising=False)


class FakeQdrantClient:
    def __init__(self):
        self.collections = set()
        self.points = {}
        self.scroll_calls = []
        self.upsert_calls = []
        self.payload_index_calls = []

    def collection_exists(self, collection_name):
        return collection_name in self.collections

    def create_collection(self, collection_name, vectors_config):
        self.collections.add(collection_name)
        self.vectors_config = vectors_config

    def get_collection(self, collection_name):
        return SimpleNamespace(
            config=SimpleNamespace(
                params=SimpleNamespace(
                    vectors=SimpleNamespace(size=384, distance="Cosine")
                )
            )
        )

    def create_payload_index(
        self,
        collection_name,
        field_name,
        field_schema,
        wait=True,
    ):
        self.payload_index_calls.append(
            {
                "collection_name": collection_name,
                "field_name": field_name,
                "field_schema": getattr(field_schema, "value", field_schema),
                "wait": wait,
            }
        )

    def upsert(self, collection_name, points, wait=True):
        assert wait is True
        self.upsert_calls.append(list(points))
        for point in points:
            self.points[str(point.id)] = {
                "vector": list(point.vector),
                "payload": dict(point.payload),
            }

    def query_points(self, collection_name, query, query_filter=None, limit=5, with_payload=True):
        scored = []
        for point_id, point in self.points.items():
            if self._matches(point["payload"], query_filter):
                scored.append(SimpleNamespace(id=point_id, score=1.0, payload=point["payload"]))
        return SimpleNamespace(points=scored[:limit])

    def scroll(self, collection_name, scroll_filter=None, limit=256, offset=None, with_payload=True, with_vectors=False):
        self.scroll_calls.append(
            {
                "collection_name": collection_name,
                "with_payload": with_payload,
                "with_vectors": with_vectors,
            }
        )
        matches = [
            SimpleNamespace(id=point_id, payload=point["payload"])
            for point_id, point in self.points.items()
            if self._matches(point["payload"], scroll_filter)
        ]
        start = int(offset or 0)
        batch = matches[start:start + limit]
        next_offset = start + limit if start + limit < len(matches) else None
        return batch, next_offset

    def count(self, collection_name, count_filter=None, exact=True):
        value = sum(1 for point in self.points.values() if self._matches(point["payload"], count_filter))
        return SimpleNamespace(count=value)

    def delete(self, collection_name, points_selector, wait=True):
        assert wait is True
        if hasattr(points_selector, "points"):
            to_delete = [str(point_id) for point_id in points_selector.points]
        else:
            to_delete = [
                point_id
                for point_id, point in self.points.items()
                if self._matches(point["payload"], points_selector.filter)
            ]
        for point_id in to_delete:
            self.points.pop(point_id)

    def _matches(self, payload, filter_obj):
        if filter_obj is None:
            return True
        for condition in getattr(filter_obj, "must", []) or []:
            key = condition.key
            if hasattr(condition.match, "any"):
                if payload.get(key) not in condition.match.any:
                    return False
            else:
                expected = condition.match.value
                if payload.get(key) != expected:
                    return False
        should = getattr(filter_obj, "should", []) or []
        if should:
            matched_any = False
            for condition in should:
                key = condition.key
                expected = condition.match.value
                if payload.get(key) == expected:
                    matched_any = True
                    break
            if not matched_any:
                return False
        for condition in getattr(filter_obj, "must_not", []) or []:
            key = condition.key
            expected = condition.match.value
            if payload.get(key) == expected:
                return False
        return True


def test_qdrant_backend_writes_payload_and_searches_with_filters(monkeypatch):
    client = FakeQdrantClient()
    monkeypatch.setenv("RAG_BACKEND", "qdrant")
    monkeypatch.setenv("QDRANT_URL", "http://localhost:6333")

    pipeline = create_rag_pipeline(
        collection_name="from_constructor",
        rag_namespace="ns",
        qdrant_client=client,
    )
    result = pipeline.replace_document(
        "doc-1",
        [
            DocumentSegment("alpha", {"page_number": 1, "file_name": "a.pdf"}),
            DocumentSegment("beta", {"page_number": 2, "file_name": "a.pdf"}),
        ],
    )

    assert result["success"] is True
    assert result["chunks_added"] == 2
    point_id = qdrant_point_id("ns", "doc-1", 0)
    payload = client.points[point_id]["payload"]
    assert payload["content"] == "alpha"
    assert payload["document_id"] == "doc-1"
    assert payload["rag_namespace"] == "ns"
    assert payload["chunk_index"] == 0
    assert payload["metadata"]["page_number"] == 1
    assert "content" not in payload["metadata"]
    assert "document_id" not in payload["metadata"]
    assert "rag_namespace" not in payload["metadata"]
    assert "chunk_index" not in payload["metadata"]

    results = pipeline.search("alpha", document_id="doc-1")
    assert [item["metadata"]["document_id"] for item in results] == ["doc-1", "doc-1"]


def test_qdrant_pipeline_ensures_filter_payload_indexes():
    client = FakeQdrantClient()

    QdrantRAGPipeline(
        collection_name="indexed",
        rag_namespace="ns",
        qdrant_client=client,
        retry_delays=(),
    )

    assert {
        call["field_name"]: call["field_schema"]
        for call in client.payload_index_calls
    } == {
        "rag_namespace": "keyword",
        "document_id": "keyword",
        "chunk_index": "integer",
    }
    assert all(call["wait"] is True for call in client.payload_index_calls)


def test_qdrant_hybrid_search_recalls_lexical_candidate(monkeypatch):
    client = FakeQdrantClient()
    monkeypatch.setenv("RAG_BACKEND", "qdrant")
    monkeypatch.setenv("QDRANT_URL", "http://localhost:6333")
    pipeline = create_rag_pipeline(rag_namespace="hybrid", qdrant_client=client)
    for index in range(6):
        pipeline.replace_document(
            f"doc-{index}",
            [DocumentSegment(f"generic content {index}", {})],
        )
    pipeline.replace_document(
        "doc-rare",
        [DocumentSegment("rare needle lexical term", {})],
    )

    result = pipeline.search(
        "needle",
        limit=1,
        retrieval_mode="hybrid",
        vector_weight=0.2,
    )

    assert result[0]["metadata"]["document_id"] == "doc-rare"


def test_qdrant_source_dedupe_preserves_distinct_unpaged_chunks(monkeypatch):
    client = FakeQdrantClient()
    monkeypatch.setenv("RAG_BACKEND", "qdrant")
    monkeypatch.setenv("QDRANT_URL", "http://localhost:6333")
    pipeline = create_rag_pipeline(rag_namespace="ns", qdrant_client=client)
    pipeline.replace_document(
        "doc-1",
        [DocumentSegment("first", {}), DocumentSegment("second", {})],
    )

    results = pipeline.search("anything", document_id="doc-1", limit=10)

    assert {item["content"] for item in results} == {"first", "second"}


def test_qdrant_delete_clear_and_stats_are_namespace_scoped(monkeypatch):
    client = FakeQdrantClient()
    monkeypatch.setenv("RAG_BACKEND", "qdrant")
    monkeypatch.setenv("QDRANT_URL", "http://localhost:6333")

    ns_a = create_rag_pipeline(rag_namespace="ns-a", qdrant_client=client)
    ns_b = create_rag_pipeline(rag_namespace="ns-b", qdrant_client=client)
    ns_a.replace_document("doc-a", [DocumentSegment("alpha", {})])
    ns_b.replace_document("doc-b", [DocumentSegment("beta", {})])

    assert ns_a.stats()["document_count"] == 1
    assert ns_a.delete_document("doc-a")["chunks_removed"] == 1
    assert ns_a.stats()["chunk_count"] == 0
    assert ns_b.stats()["chunk_count"] == 1

    ns_b.clear()
    assert ns_b.stats()["chunk_count"] == 0


def test_qdrant_replace_document_deletes_orphan_chunks(monkeypatch):
    client = FakeQdrantClient()
    monkeypatch.setenv("RAG_BACKEND", "qdrant")
    monkeypatch.setenv("QDRANT_URL", "http://localhost:6333")

    pipeline = create_rag_pipeline(rag_namespace="ns", qdrant_client=client)
    pipeline.replace_document(
        "doc-1",
        [
            DocumentSegment("first", {}),
            DocumentSegment("second", {}),
            DocumentSegment("third", {}),
        ],
    )

    result = pipeline.replace_document("doc-1", [DocumentSegment("only", {})])

    assert result["success"] is True
    assert pipeline.stats()["chunk_count"] == 1
    payloads = [point["payload"] for point in client.points.values()]
    assert [payload["content"] for payload in payloads] == ["only"]


def test_qdrant_preview_scopes_chunks_and_preserves_exact_source_context(monkeypatch):
    client = FakeQdrantClient()
    monkeypatch.setenv("RAG_BACKEND", "qdrant")
    monkeypatch.setenv("QDRANT_URL", "http://localhost:6333")

    owner = create_rag_pipeline(rag_namespace="owner", qdrant_client=client)
    outsider = create_rag_pipeline(rag_namespace="outsider", qdrant_client=client)
    page = " ".join(f"word-{number:04d}" for number in range(300))
    owner.replace_document("doc-1", [DocumentSegment(page, {"page_number": 1})])
    outsider.replace_document("doc-1", [DocumentSegment("foreign text", {})])
    target_id = qdrant_point_id("owner", "doc-1", 1)
    client.scroll_calls.clear()

    previous, target, following = owner.get_document_preview_chunks("doc-1", target_id)

    assert len(client.scroll_calls) == 2
    assert [item["metadata"]["chunk_index"] for item in (previous, target, following)] == [0, 1, 2]
    metadata = target["metadata"]
    context = metadata["preview_before"] + target["content"] + metadata["preview_after"]
    assert context in page
    assert len(metadata["preview_before"]) == len(metadata["preview_after"]) == 350
    assert outsider.get_document_preview_chunks("doc-1", target_id) == (None, None, None)


def test_qdrant_search_filters_to_selected_document_ids(monkeypatch):
    client = FakeQdrantClient()
    monkeypatch.setenv("RAG_BACKEND", "qdrant")
    monkeypatch.setenv("QDRANT_URL", "http://localhost:6333")

    pipeline = create_rag_pipeline(rag_namespace="ns", qdrant_client=client)
    pipeline.replace_document("doc-1", [DocumentSegment("alpha one", {})])
    pipeline.replace_document("doc-2", [DocumentSegment("alpha two", {})])
    pipeline.replace_document("doc-3", [DocumentSegment("alpha three", {})])

    results = pipeline.search("alpha", document_ids=["doc-1", "doc-2"], limit=10)

    assert [item["metadata"]["document_id"] for item in results] == ["doc-1", "doc-2"]
    assert pipeline._scope_filter(document_ids=["doc-1", "doc-2"]) == {
        "rag_namespace": "ns",
        "document_id": ["doc-1", "doc-2"],
    }


def test_qdrant_source_dedupe_keeps_distinct_unpaged_chunks(monkeypatch):
    client = FakeQdrantClient()
    monkeypatch.setenv("RAG_BACKEND", "qdrant")
    pipeline = create_rag_pipeline(rag_namespace="ns", qdrant_client=client)
    pipeline.replace_document(
        "doc-1",
        [
            DocumentSegment("alpha same prefix " + "A" * 200, {}),
            DocumentSegment("alpha same prefix " + "B" * 200, {}),
        ],
    )

    results = pipeline.search("alpha", document_ids=["doc-1"], limit=10)

    assert len(results) == 2


def test_qdrant_source_dedupe_does_not_merge_identical_text_across_documents(monkeypatch):
    client = FakeQdrantClient()
    monkeypatch.setenv("RAG_BACKEND", "qdrant")
    pipeline = create_rag_pipeline(rag_namespace="ns", qdrant_client=client)
    pipeline.replace_document("doc-1", [DocumentSegment("alpha identical", {})])
    pipeline.replace_document("doc-2", [DocumentSegment("alpha identical", {})])

    results = pipeline.search(
        "alpha", document_ids=["doc-1", "doc-2"], limit=10
    )

    assert [item["metadata"]["document_id"] for item in results] == [
        "doc-1",
        "doc-2",
    ]


def test_qdrant_empty_replace_preserves_existing_document_unless_explicit(monkeypatch):
    client = FakeQdrantClient()
    monkeypatch.setenv("RAG_BACKEND", "qdrant")
    pipeline = create_rag_pipeline(rag_namespace="ns", qdrant_client=client)
    pipeline.replace_document("doc-1", [DocumentSegment("alpha", {})])

    rejected = pipeline.replace_document("doc-1", [DocumentSegment("  ", {})])
    assert rejected["success"] is False
    assert pipeline.stats()["chunk_count"] == 1

    accepted = pipeline.replace_document("doc-1", [], allow_empty=True)
    assert accepted["success"] is True
    assert pipeline.stats()["chunk_count"] == 0


def test_qdrant_replace_preserves_created_at_and_increments_version(monkeypatch):
    client = FakeQdrantClient()
    monkeypatch.setenv("RAG_BACKEND", "qdrant")
    pipeline = create_rag_pipeline(rag_namespace="ns", qdrant_client=client)
    pipeline.replace_document("doc-1", [DocumentSegment("alpha", {})])
    first = next(iter(client.points.values()))["payload"]

    pipeline.replace_document("doc-1", [DocumentSegment("beta", {})])
    second = next(iter(client.points.values()))["payload"]

    assert first["created_at"].endswith("Z")
    assert second["created_at"] == first["created_at"]
    assert second["document_version"] == 2
    assert second["updated_at"].endswith("Z")


def test_qdrant_search_rejects_empty_document_ids(monkeypatch):
    client = FakeQdrantClient()
    monkeypatch.setenv("RAG_BACKEND", "qdrant")
    monkeypatch.setenv("QDRANT_URL", "http://localhost:6333")

    pipeline = create_rag_pipeline(rag_namespace="ns", qdrant_client=client)
    pipeline.replace_document("doc-1", [DocumentSegment("alpha one", {})])

    try:
        pipeline.search("alpha", document_ids=[])
    except ValueError as error:
        assert "document_ids cannot be empty" in str(error)
    else:
        raise AssertionError("empty document_ids should not search the whole namespace")


def test_qdrant_existing_collection_rejects_incompatible_vector_size(monkeypatch):
    client = FakeQdrantClient()
    client.collections.add("rag_knowledge_base")
    client.get_collection = lambda collection_name: SimpleNamespace(
        config=SimpleNamespace(
            params=SimpleNamespace(
                vectors=SimpleNamespace(size=128, distance="Cosine")
            )
        )
    )
    monkeypatch.setenv("RAG_BACKEND", "qdrant")
    monkeypatch.setenv("QDRANT_URL", "http://localhost:6333")

    with pytest.raises(RAGCollectionError, match="incompatible"):
        create_rag_pipeline(collection_name="rag_knowledge_base", rag_namespace="ns", qdrant_client=client)


def test_qdrant_existing_collection_rejects_incompatible_distance(monkeypatch):
    client = FakeQdrantClient()
    client.collections.add("rag_knowledge_base")
    client.get_collection = lambda collection_name: SimpleNamespace(
        config=SimpleNamespace(
            params=SimpleNamespace(
                vectors=SimpleNamespace(size=384, distance="Dot")
            )
        )
    )
    monkeypatch.setenv("RAG_BACKEND", "qdrant")
    monkeypatch.setenv("QDRANT_URL", "http://localhost:6333")

    with pytest.raises(RAGCollectionError, match="Cosine"):
        create_rag_pipeline(collection_name="rag_knowledge_base", rag_namespace="ns", qdrant_client=client)


def test_qdrant_retries_transient_search_errors(monkeypatch):
    client = FakeQdrantClient()
    calls = {"query": 0}

    def flaky_query_points(**kwargs):
        calls["query"] += 1
        if calls["query"] < 3:
            raise RuntimeError("HTTP 503 temporary outage")
        return SimpleNamespace(points=[])

    client.query_points = flaky_query_points
    monkeypatch.setenv("RAG_BACKEND", "qdrant")
    monkeypatch.setenv("QDRANT_URL", "http://localhost:6333")
    pipeline = create_rag_pipeline(
        rag_namespace="ns",
        qdrant_client=client,
        retry_delays=(0, 0, 0),
    )

    assert pipeline.search("alpha") == []
    assert calls["query"] == 3


def test_qdrant_does_not_retry_client_errors(monkeypatch):
    client = FakeQdrantClient()
    calls = {"query": 0}

    def client_error_query_points(**kwargs):
        calls["query"] += 1
        raise RuntimeError("HTTP 400 bad request")

    client.query_points = client_error_query_points
    monkeypatch.setenv("RAG_BACKEND", "qdrant")
    monkeypatch.setenv("QDRANT_URL", "http://localhost:6333")
    pipeline = create_rag_pipeline(
        rag_namespace="ns",
        qdrant_client=client,
        retry_delays=(0, 0, 0),
    )

    with pytest.raises(RAGOperationError):
        pipeline.search("alpha")
    assert calls["query"] == 1


def test_qdrant_errors_are_sanitized(monkeypatch):
    client = FakeQdrantClient()

    def failing_query_points(**kwargs):
        raise RuntimeError("GET http://localhost:6333?api_key=secret-token failed")

    client.query_points = failing_query_points
    monkeypatch.setenv("RAG_BACKEND", "qdrant")
    monkeypatch.setenv("QDRANT_URL", "http://localhost:6333?api_key=secret-token")
    pipeline = create_rag_pipeline(
        rag_namespace="ns",
        qdrant_client=client,
        retry_delays=(0, 0, 0),
    )

    with pytest.raises(RAGOperationError) as error:
        pipeline.search("alpha")

    assert "secret-token" not in str(error.value)
    assert "api_key=%2A%2A%2A" in str(error.value) or "api_key=***" in str(error.value)


def test_qdrant_summary_rejects_documents_over_chunk_limit(monkeypatch):
    client = FakeQdrantClient()
    monkeypatch.setenv("RAG_BACKEND", "qdrant")
    monkeypatch.setenv("QDRANT_URL", "http://localhost:6333")
    pipeline = create_rag_pipeline(rag_namespace="ns", qdrant_client=client)
    pipeline._count = lambda document_id=None: 10001

    with pytest.raises(RAGDocumentTooLargeError, match="10000"):
        pipeline.get_document_summary_context("doc-too-large")


def test_qdrant_stats_only_requests_document_id_payload(monkeypatch):
    client = FakeQdrantClient()
    monkeypatch.setenv("RAG_BACKEND", "qdrant")
    monkeypatch.setenv("QDRANT_URL", "http://localhost:6333")
    pipeline = create_rag_pipeline(rag_namespace="ns", qdrant_client=client)
    pipeline.replace_document("doc-1", [DocumentSegment("alpha", {"extra": "value"})])

    stats = pipeline.stats()

    assert stats["document_count"] == 1
    assert client.scroll_calls[-1]["with_payload"] == ["document_id"]


@pytest.mark.parametrize(
    ("collection_name", "rag_namespace"),
    [("", "ns"), ("   ", "ns"), ("collection", ""), ("collection", "   ")],
)
def test_qdrant_pipeline_rejects_empty_collection_or_namespace(collection_name, rag_namespace):
    with pytest.raises(Exception, match="cannot be empty"):
        QdrantRAGPipeline(
            collection_name=collection_name,
            rag_namespace=rag_namespace,
            qdrant_client=FakeQdrantClient(),
        )


def test_qdrant_embedding_dimension_mismatch_fails_explicitly(monkeypatch):
    client = FakeQdrantClient()
    monkeypatch.setenv("RAG_BACKEND", "qdrant")
    pipeline = create_rag_pipeline(rag_namespace="ns", qdrant_client=client, retry_delays=())
    pipeline.embedder = SimpleNamespace(encode=lambda text: [1.0, 2.0])

    with pytest.raises(RAGEmbeddingError, match=r"dimension 2.*expected 384"):
        pipeline.search("alpha")


@pytest.mark.parametrize(
    ("status_code", "expected_error"),
    [
        (429, RAGOperationError),
        (400, RAGOperationError),
        (401, RAGAuthenticationError),
        (403, RAGAuthenticationError),
    ],
)
def test_qdrant_does_not_retry_rate_limit_or_client_errors(
    monkeypatch, status_code, expected_error
):
    client = FakeQdrantClient()
    calls = 0

    def fail(**kwargs):
        nonlocal calls
        calls += 1
        error = RuntimeError(f"HTTP {status_code} rejected")
        error.status_code = status_code
        raise error

    client.query_points = fail
    monkeypatch.setenv("RAG_BACKEND", "qdrant")
    pipeline = create_rag_pipeline(
        rag_namespace="ns", qdrant_client=client, retry_delays=(0, 0)
    )

    with pytest.raises(expected_error):
        pipeline.search("alpha")
    assert calls == 1


@pytest.mark.parametrize(
    "error_factory",
    [
        lambda: TimeoutError("timed out"),
        lambda: ConnectionError("connection reset"),
        lambda: type("GatewayError", (RuntimeError,), {"status_code": 502})("bad gateway"),
    ],
)
def test_qdrant_retries_transient_transport_errors(monkeypatch, error_factory):
    client = FakeQdrantClient()
    calls = 0

    def flaky(**kwargs):
        nonlocal calls
        calls += 1
        if calls < 3:
            raise error_factory()
        return SimpleNamespace(points=[])

    client.query_points = flaky
    monkeypatch.setenv("RAG_BACKEND", "qdrant")
    pipeline = create_rag_pipeline(
        rag_namespace="ns", qdrant_client=client, retry_delays=(0, 0)
    )

    assert pipeline.search("alpha") == []
    assert calls == 3


def test_qdrant_does_not_retry_plain_value_error(monkeypatch):
    client = FakeQdrantClient()
    calls = 0

    def fail(**kwargs):
        nonlocal calls
        calls += 1
        raise ValueError("programming error")

    client.query_points = fail
    monkeypatch.setenv("RAG_BACKEND", "qdrant")
    pipeline = create_rag_pipeline(
        rag_namespace="ns", qdrant_client=client, retry_delays=(0, 0)
    )

    with pytest.raises(RAGOperationError, match="programming error"):
        pipeline.search("alpha")
    assert calls == 1


def test_qdrant_api_key_is_redacted_from_nested_errors_and_urls(monkeypatch):
    api_key = "nested-secret-token"
    client = FakeQdrantClient()

    def fail(**kwargs):
        try:
            raise ValueError(f"Authorization: Bearer {api_key}")
        except ValueError as cause:
            raise RuntimeError(
                f"request http://localhost:6333?api_key={api_key} failed"
            ) from cause

    client.query_points = fail
    monkeypatch.setenv("RAG_BACKEND", "qdrant")
    pipeline = create_rag_pipeline(
        rag_namespace="ns",
        qdrant_url=f"http://localhost:6333?api_key={api_key}",
        qdrant_api_key=api_key,
        qdrant_client=client,
        retry_delays=(),
    )

    with pytest.raises(RAGOperationError) as captured:
        pipeline.search("alpha")

    rendered = repr(captured.value) + str(captured.value)
    assert api_key not in rendered
    assert api_key not in repr(captured.value.__cause__)


def test_qdrant_summary_samples_document_start_middle_and_end(monkeypatch):
    client = FakeQdrantClient()
    monkeypatch.setenv("RAG_BACKEND", "qdrant")
    pipeline = create_rag_pipeline(rag_namespace="ns", qdrant_client=client)
    pipeline.replace_document(
        "doc-1",
        [DocumentSegment(f"chunk-{index}", {"page_number": index + 1}) for index in range(9)],
    )

    results = pipeline.get_document_summary_context("doc-1", limit=3)

    assert [result["content"] for result in results] == ["chunk-0", "chunk-4", "chunk-8"]


def test_qdrant_metadata_converts_non_json_values(monkeypatch):
    client = FakeQdrantClient()
    monkeypatch.setenv("RAG_BACKEND", "qdrant")
    pipeline = create_rag_pipeline(rag_namespace="ns", qdrant_client=client)

    pipeline.replace_document(
        "doc-1",
        [
            DocumentSegment(
                "alpha",
                {
                    "published": date(2026, 6, 30),
                    "tags": {"rag", "qdrant"},
                    "nested": {"value": complex(1, 2)},
                },
            )
        ],
    )
    metadata = next(iter(client.points.values()))["payload"]["metadata"]

    assert metadata["published"] == "2026-06-30"
    assert isinstance(metadata["tags"], str)
    assert metadata["nested"]["value"] == "(1+2j)"


def test_qdrant_upsert_batches_never_exceed_100_points(monkeypatch):
    client = FakeQdrantClient()
    monkeypatch.setenv("RAG_BACKEND", "qdrant")
    pipeline = create_rag_pipeline(rag_namespace="ns", qdrant_client=client)

    result = pipeline.replace_document(
        "doc-1",
        [DocumentSegment(f"chunk-{index}", {}) for index in range(205)],
    )

    assert result["chunks_added"] == 205
    assert [len(batch) for batch in client.upsert_calls] == [100, 100, 5]


def test_qdrant_relevance_rerank_respects_selected_documents():
    class RankedClient(FakeQdrantClient):
        def query_points(self, *args, **kwargs):
            result = super().query_points(*args, **kwargs)
            for point in result.points:
                point.score = 0.81 if point.payload["content"] == "frost policy network" else 0.80
            return result

    client = RankedClient()
    pipeline = QdrantRAGPipeline(
        collection_name="rerank", rag_namespace="ns", qdrant_client=client,
    )
    pipeline.add_text("frost policy network", document_id="chosen")
    pipeline.add_text("frost network policy", document_id="chosen", replace_existing=False)
    pipeline.add_text("frost network policy outside scope", document_id="other")

    query = "frost network policy"
    original = pipeline.search(query, limit=1, document_ids=["chosen"])
    reranked = pipeline.search(query, limit=1, document_ids=["chosen"], use_rerank=True)
    assert original[0]["content"] == "frost policy network"
    assert reranked[0]["content"] == "frost network policy"
    assert reranked[0]["metadata"]["document_id"] == "chosen"
