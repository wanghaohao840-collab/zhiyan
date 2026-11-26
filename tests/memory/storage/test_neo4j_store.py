from __future__ import annotations

import pytest

from hello_agents.memory.storage.neo4j_store import (
    Neo4jConfigError,
    Neo4jGraphStore,
)
from hello_agents.memory.storage import neo4j_store


class FakeResult:
    def __init__(self, records=None):
        self._records = list(records or [])

    def data(self):
        return list(self._records)

    def single(self):
        return self._records[0] if self._records else None


class RecordingTransaction:
    def __init__(self, responses=None, fail_on=None):
        self.calls = []
        self.responses = responses or {}
        self.fail_on = fail_on

    def run(self, query, **parameters):
        self.calls.append((query, parameters))
        if self.fail_on and self.fail_on in query:
            raise RuntimeError("transaction failed")
        for marker, records in self.responses.items():
            if marker in query:
                return FakeResult(records)
        return FakeResult()


class RecordingSession:
    def __init__(self, driver):
        self.driver = driver

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def execute_write(self, callback):
        tx = RecordingTransaction(self.driver.responses, self.driver.fail_on)
        self.driver.transactions.append(tx)
        return callback(tx)

    def execute_read(self, callback):
        tx = RecordingTransaction(self.driver.responses)
        self.driver.transactions.append(tx)
        return callback(tx)

    def run(self, query, **parameters):
        tx = RecordingTransaction(self.driver.responses)
        self.driver.transactions.append(tx)
        return tx.run(query, **parameters)


class RecordingDriver:
    def __init__(self, responses=None, fail_on=None):
        self.responses = responses or {}
        self.fail_on = fail_on
        self.transactions = []
        self.session_databases = []
        self.closed = False

    def session(self, database=None):
        self.session_databases.append(database)
        return RecordingSession(self)

    def close(self):
        self.closed = True


def sample_graph():
    return {
        "document": {"name": "安全文档", "source": "fixture"},
        "chapters": [
            {
                "chapter_id": "doc-1:chapter:0",
                "title": "第一章",
                "level": 1,
                "order": 0,
                "heading_path": ["第一章"],
                "parent_id": None,
            }
        ],
        "chunks": [
            {
                "chunk_id": "chunk-1",
                "content": "Alice 解释图数据库",
                "page_number": 1,
                "chunk_index": 0,
                "chapter_id": "doc-1:chapter:0",
            }
        ],
        "concepts": [
            {
                "concept_id": "concept-1",
                "name": "图数据库",
                "normalized_name": "图数据库",
                "description": "图结构存储",
            }
        ],
        "knowledge_points": [],
        "persons": [],
        "relations": [
            {
                "source_id": "chunk-1",
                "target_id": "concept-1",
                "type": "MENTIONS",
                "properties": {
                    "chunk_id": "chunk-1",
                    "evidence": "解释图数据库",
                    "confidence": 0.9,
                },
            }
        ],
    }


def test_requires_complete_configuration_without_injected_driver():
    with pytest.raises(Neo4jConfigError):
        Neo4jGraphStore(uri=None, username="neo4j", password=None)


def test_failed_connectivity_closes_the_created_driver(monkeypatch):
    class FailingDriver:
        def __init__(self):
            self.closed = False

        def verify_connectivity(self):
            raise RuntimeError("offline")

        def close(self):
            self.closed = True

    driver = FailingDriver()

    class FakeGraphDatabase:
        @staticmethod
        def driver(*args, **kwargs):
            return driver

    monkeypatch.setattr(neo4j_store, "GraphDatabase", FakeGraphDatabase)

    with pytest.raises(Neo4jConfigError, match="connection failed"):
        Neo4jGraphStore(
            uri="neo4j://localhost:7687",
            username="neo4j",
            password="wrong",
        )

    assert driver.closed is True


def test_rejects_empty_namespace_before_a_graph_operation():
    store = Neo4jGraphStore(driver=RecordingDriver())

    with pytest.raises(ValueError, match="rag_namespace"):
        store.delete_document("doc-1", rag_namespace="")


def test_representation_and_public_state_do_not_expose_credentials():
    driver = RecordingDriver()
    store = Neo4jGraphStore(
        driver=driver,
        database="neo4j",
        uri="neo4j://secret-host",
        username="secret-user",
        password="secret-password",
    )

    rendered = f"{store!r} {store!s} {store.__dict__}"
    assert "secret-password" not in rendered
    assert "secret-user" not in rendered
    assert "secret-host" not in rendered
    assert "config" not in store.__dict__


def test_schema_initialization_is_idempotent_per_instance():
    driver = RecordingDriver()
    store = Neo4jGraphStore(driver=driver)

    store.initialize_schema()
    first_count = sum(len(tx.calls) for tx in driver.transactions)
    store.initialize_schema()

    assert first_count >= 6
    assert sum(len(tx.calls) for tx in driver.transactions) == first_count


def test_schema_initialization_includes_canonical_entity_uniqueness():
    driver = RecordingDriver()
    store = Neo4jGraphStore(driver=driver)

    store.initialize_schema()

    queries = [query for tx in driver.transactions for query, _ in tx.calls]
    canonical = next(
        query for query in queries if "graph_canonical_entity" in query
    )
    assert "CanonicalEntity" in canonical
    assert "rag_namespace" in canonical
    assert "entity_type" in canonical
    assert "normalized_name" in canonical


def test_replace_document_graph_uses_one_transaction_and_scoped_parameters():
    driver = RecordingDriver()
    store = Neo4jGraphStore(driver=driver)

    result = store.replace_document_graph(
        document_id="doc-1",
        build_id="build-1",
        graph=sample_graph(),
    )

    write_tx = driver.transactions[-1]
    assert result["node_count"] == 4
    assert result["relation_count"] >= 3
    assert len(driver.transactions) >= 2  # schema plus one write transaction
    assert any("DETACH DELETE" in query for query, _ in write_tx.calls)
    assert all(
        params.get("document_id") == "doc-1"
        for query, params in write_tx.calls
        if "document_id" in query
    )
    assert all("安全文档" not in query for query, _ in write_tx.calls)


def test_replace_links_canonical_entities_and_cleans_namespace_orphans():
    driver = RecordingDriver()
    store = Neo4jGraphStore(driver=driver)

    store.replace_document_graph(
        "doc-1",
        "build-1",
        sample_graph(),
        rag_namespace="tenant-a",
    )

    write_tx = driver.transactions[-1]
    link_query, link_params = next(
        call for call in write_tx.calls if "GRAPH_LINK_CANONICAL" in call[0]
    )
    cleanup_query, cleanup_params = next(
        call for call in write_tx.calls if "GRAPH_CLEAN_CANONICAL" in call[0]
    )
    assert "REFERS_TO" in link_query
    assert link_params["rag_namespace"] == "tenant-a"
    assert link_params["document_id"] == "doc-1"
    assert len(link_params["rows"]) == 1
    assert link_params["rows"][0]["graph_id"] == "concept-1"
    assert link_params["rows"][0]["entity_type"] == "Concept"
    assert len(link_params["rows"][0]["canonical_id"]) == 24
    assert "document_id" not in cleanup_query
    assert cleanup_params == {"rag_namespace": "tenant-a"}


def test_replace_failure_propagates_without_a_second_write_transaction():
    driver = RecordingDriver(fail_on="GRAPH_WRITE_DOCUMENT")
    store = Neo4jGraphStore(driver=driver)

    with pytest.raises(RuntimeError, match="transaction failed"):
        store.replace_document_graph("doc-1", "build-1", sample_graph())

    write_transactions = [
        tx for tx in driver.transactions
        if any("GRAPH_REPLACE_DELETE" in query for query, _ in tx.calls)
    ]
    assert len(write_transactions) == 1


def test_build_lookup_and_delete_are_document_scoped():
    driver = RecordingDriver(
        responses={
            "GRAPH_GET_BUILD": [
                {"build_id": "build-1", "graph_status": "ready"}
            ],
            "GRAPH_DELETE_COUNT": [
                {"nodes_removed": 4, "relations_removed": 3}
            ],
        }
    )
    store = Neo4jGraphStore(driver=driver)

    assert store.get_document_build("doc-1") == {
        "build_id": "build-1",
        "graph_status": "ready",
    }
    assert store.delete_document("doc-1") == {
        "nodes_removed": 4,
        "relations_removed": 3,
    }

    calls = [call for tx in driver.transactions for call in tx.calls]
    scoped = [
        params for query, params in calls
        if "GRAPH_GET_BUILD" in query or "GRAPH_DELETE" in query
    ]
    assert scoped
    assert all(params["document_id"] == "doc-1" for params in scoped)


def test_graph_query_uses_independent_cursors_and_excludes_chunk_content():
    driver = RecordingDriver(
        responses={
            "GRAPH_QUERY_NODES": [
                {
                    "id": "chunk-1",
                    "type": "Chunk",
                    "properties": {
                        "chunk_id": "chunk-1",
                        "content": "hidden",
                        "document_id": "doc-1",
                    },
                },
                {"id": "concept-1", "type": "Concept", "properties": {}},
            ],
            "GRAPH_QUERY_RELATIONS": [
                {
                    "source_id": "chunk-1",
                    "target_id": "concept-1",
                    "type": "MENTIONS",
                    "properties": {"confidence": 0.9},
                    "source": {"id": "chunk-1", "type": "Chunk", "name": None},
                    "target": {
                        "id": "concept-1",
                        "type": "Concept",
                        "name": "图数据库",
                    },
                },
                {
                    "source_id": "concept-1",
                    "target_id": "concept-2",
                    "type": "RELATED_TO",
                    "properties": {},
                    "source": {"id": "concept-1", "type": "Concept", "name": "A"},
                    "target": {"id": "concept-2", "type": "Concept", "name": "B"},
                },
            ],
        }
    )
    store = Neo4jGraphStore(driver=driver)

    result = store.get_document_graph(
        "doc-1",
        node_cursor="5",
        relation_cursor="9",
        node_limit=1,
        relation_limit=1,
    )

    assert "content" not in result["nodes"][0]["properties"]
    assert result["relations"][0]["target"]["name"] == "图数据库"
    assert result["page"]["next_node_cursor"] == "6"
    assert result["page"]["next_relation_cursor"] == "10"
    calls = [call for tx in driver.transactions for call in tx.calls]
    node_params = next(params for query, params in calls if "GRAPH_QUERY_NODES" in query)
    rel_params = next(params for query, params in calls if "GRAPH_QUERY_RELATIONS" in query)
    assert node_params["skip"] == 5
    assert rel_params["skip"] == 9


def test_graph_context_is_scoped_parameterized_and_bounded():
    driver = RecordingDriver(
        responses={
            "GRAPH_QUERY_CONTEXT": [
                {
                    "entity": {
                        "id": "concept-1",
                        "type": "Concept",
                        "name": "Neo4j",
                        "properties": {"normalized_name": "neo4j"},
                    },
                    "neighbor": {
                        "id": "person-1",
                        "type": "Person",
                        "name": "Alice",
                        "properties": {"content": "must-not-leak"},
                    },
                    "relation": {
                        "source_id": "concept-1",
                        "target_id": "person-1",
                        "type": "RELATED_TO",
                        "properties": {"evidence": "Neo4j by Alice"},
                    },
                }
            ]
        }
    )
    store = Neo4jGraphStore(driver=driver)

    result = store.get_graph_context(
        "doc-1",
        query_terms=["neo4j", "alice"],
        rag_namespace="tenant-a",
        node_limit=2,
        relation_limit=3,
    )

    assert result["entities"][0]["id"] == "concept-1"
    assert result["entities"][0]["type"] == "Concept"
    assert result["entities"][1]["id"] == "person-1"
    assert "content" not in result["entities"][1]["properties"]
    assert result["relations"][0]["type"] == "RELATED_TO"
    calls = [
        call
        for tx in driver.transactions
        for call in tx.calls
        if "GRAPH_QUERY_CONTEXT" in call[0]
    ]
    assert len(calls) == 1
    query, parameters = calls[0]
    assert "neo4j" not in query
    assert parameters["document_id"] == "doc-1"
    assert parameters["rag_namespace"] == "tenant-a"
    assert parameters["query_terms"] == ["neo4j", "alice"]
    assert parameters["node_limit"] == 2
    assert parameters["relation_limit"] == 3


def test_cross_document_entities_are_scoped_bounded_and_sanitized():
    driver = RecordingDriver(
        responses={
            "GRAPH_QUERY_CROSS_DOCUMENT_ENTITIES": [
                {
                    "canonical_id": "canonical-neo4j",
                    "entity_type": "Concept",
                    "normalized_name": "neo4j",
                    "name": "Neo4j",
                    "members": [
                        {
                            "document_id": "doc-2",
                            "id": "doc-2:concept:neo4j",
                            "type": "Concept",
                            "name": "Neo4j",
                            "properties": {
                                "description": "Graph database",
                                "content": "must-not-leak",
                            },
                        },
                        {
                            "document_id": "doc-1",
                            "id": "doc-1:concept:neo4j",
                            "type": "Concept",
                            "name": "Neo4j",
                            "properties": {},
                        },
                    ],
                }
            ]
        }
    )
    store = Neo4jGraphStore(driver=driver)

    result = store.get_cross_document_entities(
        ["doc-2", "doc-1", "doc-2"],
        query_terms=["Neo4j", "neo4j", ""],
        rag_namespace="tenant-a",
        entity_limit=3,
        evidence_limit=5,
    )

    assert result["entities"][0]["canonical_id"] == "canonical-neo4j"
    assert [
        member["document_id"]
        for member in result["entities"][0]["members"]
    ] == ["doc-1", "doc-2"]
    assert "content" not in result["entities"][0]["members"][1]["properties"]
    query, parameters = next(
        call
        for tx in driver.transactions
        for call in tx.calls
        if "GRAPH_QUERY_CROSS_DOCUMENT_ENTITIES" in call[0]
    )
    assert "neo4j" not in query.lower().replace("canonicalentity", "")
    assert parameters == {
        "document_ids": ["doc-2", "doc-1"],
        "rag_namespace": "tenant-a",
        "query_terms": ["neo4j"],
        "entity_labels": ["Concept", "KnowledgePoint", "Person"],
        "entity_limit": 3,
        "evidence_limit": 5,
    }


@pytest.mark.parametrize(
    "document_ids, message",
    [
        (["doc-1"], "between 2 and 10"),
        ([f"doc-{index}" for index in range(11)], "between 2 and 10"),
        (["doc-1", ""], "document_id"),
    ],
)
def test_cross_document_entities_validate_document_scope(document_ids, message):
    store = Neo4jGraphStore(driver=RecordingDriver())

    with pytest.raises(ValueError, match=message):
        store.get_cross_document_entities(
            document_ids,
            query_terms=["neo4j"],
        )


def test_delete_cleans_only_namespace_canonical_orphans():
    driver = RecordingDriver()
    store = Neo4jGraphStore(driver=driver)

    store.delete_document("doc-1", rag_namespace="tenant-a")

    write_tx = driver.transactions[-1]
    cleanup_query, cleanup_params = next(
        call for call in write_tx.calls if "GRAPH_CLEAN_CANONICAL" in call[0]
    )
    assert "CanonicalEntity" in cleanup_query
    assert "REFERS_TO" in cleanup_query
    assert "$rag_namespace" in cleanup_query
    assert cleanup_params == {"rag_namespace": "tenant-a"}


def test_namespace_is_part_of_every_document_scope():
    driver = RecordingDriver()
    store = Neo4jGraphStore(driver=driver)

    store.replace_document_graph(
        "shared-doc",
        "build-a",
        sample_graph(),
        rag_namespace="tenant-a",
    )
    store.delete_document("shared-doc", rag_namespace="tenant-b")

    scoped_calls = [
        parameters
        for tx in driver.transactions
        for query, parameters in tx.calls
        if "$document_id" in query
    ]
    assert any(params.get("rag_namespace") == "tenant-a" for params in scoped_calls)
    assert any(params.get("rag_namespace") == "tenant-b" for params in scoped_calls)


def test_canvas_queries_use_authorized_docs_bounded_rows_and_in_scope_endpoints():
    driver = RecordingDriver(responses={
        "GRAPH_CANVAS_NODES": [
            {
                "element_id": "local", "document_ids": ["owned"], "type": "Concept",
                "label": "Graph", "graph_id": "concept", "scope_id": "owned", "weight": None,
            },
            {
                "element_id": "global", "document_ids": ["owned"], "type": "CanonicalEntity",
                "label": "Graph", "graph_id": "canonical", "scope_id": "", "weight": 5.0,
            },
            {
                "element_id": "extra", "document_ids": ["owned"], "type": "Document",
                "label": "More", "graph_id": "doc", "scope_id": "owned", "weight": 1.0,
            },
        ],
        "GRAPH_CANVAS_EDGES": [
            {"source": "local", "target": "global", "relation": "REFERS_TO", "weight": 2.0}
        ],
    })
    store = Neo4jGraphStore(driver=driver)

    graph = store.get_subgraph(
        rag_namespace="pdf_owner", document_ids=["owned"], query="GRAPH", limit=2,
    )

    ((nodes_query, node_params), (edges_query, edge_params)) = driver.transactions[-1].calls
    assert nodes_query.count("rag_namespace: $rag_namespace") == 3
    assert nodes_query.count("document_id IN $document_ids") == 2
    assert edges_query.count("rag_namespace: $rag_namespace") == 2
    assert "r.rag_namespace" in edges_query
    assert "elementId(target) IN $element_ids" in edges_query
    assert "labels(n)[0]" not in nodes_query
    assert "toString(coalesce(" in nodes_query
    assert "any(lbl IN labels(source)" in edges_query
    assert node_params["document_ids"] == ["owned"]
    assert node_params["search_term"] == "graph"
    assert node_params["rag_namespace"] == edge_params["rag_namespace"] == "pdf_owner"
    assert node_params["limit"] == edge_params["limit"] == 3
    assert edge_params["element_ids"] == ["local", "global"]
    assert graph["truncated"]
    assert graph["edges"] == [
        {
            "source": "Concept:owned:concept", "target": "CanonicalEntity::canonical",
            "relation": "REFERS_TO", "weight": 2.0,
        }
    ]
    assert len(graph["nodes"]) == 2
    with pytest.raises(ValueError):
        store.get_subgraph(rag_namespace="pdf_owner", document_ids=["owned"], limit=201)
