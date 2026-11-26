from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from api.app import create_api_app
from app.auth import AuthService
from app.database import connect, initialize_database
from app.graph_service import GraphService
from app.session import InvalidSessionError


GRAPH_URL = "/api/v1/graph"
DOC = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
FOREIGN = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"


class Sessions:
    def get_session(self, token):
        if token != "owner":
            raise InvalidSessionError("invalid")
        return SimpleNamespace(token=token, user_id="owner")


class GraphStore:
    def __init__(self):
        self.calls = []
        self.offline = False
        self.while_reading = lambda: None

    def check_database(self):
        if self.offline:
            raise OSError("bolt://private-graph:7687")

    def get_subgraph(self, **params):
        self.check_database()
        self.calls.append(params)
        self.while_reading()
        return {
            "nodes": [
                {
                    "id": "a",
                    "label": "范围内",
                    "type": "Concept",
                    "document_ids": [DOC],
                    "weight": 1,
                },
                {
                    "id": "b",
                    "label": "跨用户",
                    "type": "Concept",
                    "document_ids": [FOREIGN],
                    "weight": 1,
                },
            ],
            "edges": [
                {"source": "a", "target": "b", "relation": "RELATED_TO", "weight": 1}
            ],
            "truncated": False,
        }

    def close(self):
        pass


@pytest.fixture
def graph_parts(monkeypatch):
    monkeypatch.setenv("ZHIYAN_GRAPH_ENABLED", "1")
    store = GraphStore()
    visible = [SimpleNamespace(document_id=DOC)]
    document_library = SimpleNamespace(list_documents=lambda token: tuple(visible))
    services = SimpleNamespace(
        session_registry=Sessions(),
        document_library=document_library,
        graph_service=GraphService(store_factory=lambda **kwargs: store),
        start=lambda: None,
        stop=lambda: None,
    )
    with TestClient(create_api_app(services)) as client:
        yield client, store, visible


@pytest.mark.parametrize("path", ["/capabilities", "/subgraph"])
def test_graph_routes_require_session(graph_parts, path):
    client, _, _ = graph_parts
    assert client.get(GRAPH_URL + path).status_code == 401


def test_capabilities_reflects_graph_setting_and_actual_connectivity(graph_parts, monkeypatch):
    client, store, _ = graph_parts
    client.cookies.set("zhiyan_session", "owner")
    capability = GRAPH_URL + "/capabilities"
    assert client.get(capability).json() == {"enabled": True}

    store.offline = True
    unavailable = client.get(capability)
    assert unavailable.json() == {"enabled": False, "reason": "graph_unavailable"}
    assert unavailable.headers["cache-control"] == "private, no-store"
    error = client.get(GRAPH_URL + "/subgraph")
    assert error.status_code == 503
    assert "private-graph" not in error.text

    monkeypatch.setenv("ZHIYAN_GRAPH_ENABLED", "0")
    assert client.get(capability).json() == {"enabled": False, "reason": "graph_disabled"}


def test_subgraph_passes_owner_scope_and_revalidates_store_projection(graph_parts):
    client, store, _ = graph_parts
    client.cookies.set("zhiyan_session", "owner")
    response = client.get(GRAPH_URL + "/subgraph", params={"query": "图谱", "limit": 7})

    assert response.status_code == 200
    assert response.headers["cache-control"] == "private, no-store"
    assert store.calls == [
        {"rag_namespace": "pdf_owner", "document_ids": [DOC], "query": "图谱", "limit": 7}
    ]
    assert [node["id"] for node in response.json()["nodes"]] == ["a"]
    assert response.json()["edges"] == []

    assert client.get(GRAPH_URL + "/subgraph", params={"document_id": FOREIGN}).status_code == 404
    assert client.get(GRAPH_URL + "/subgraph", params={"limit": 201}).status_code == 422
    assert len(store.calls) == 1


def test_scoped_subgraph_does_not_return_results_after_delete_fence_commits(graph_parts):
    client, store, visible = graph_parts
    client.cookies.set("zhiyan_session", "owner")
    store.while_reading = visible.clear

    response = client.get(GRAPH_URL + "/subgraph", params={"document_id": DOC})

    assert response.status_code == 404
    assert len(store.calls) == 1


def test_graph_cleanup_survives_publish_error_and_the_feature_being_disabled(tmp_path, monkeypatch):
    from hello_agents.memory.graph.extractor import GraphExtractor

    db = tmp_path / "app.db"
    initialize_database(db)
    user = AuthService(db).register("graph_owner", "correct horse battery")
    namespace = f"pdf_{user.id}"
    task = SimpleNamespace(user_id=user.id, document_id=DOC, original_name="example.md")
    pipeline = SimpleNamespace(get_document_chunks=lambda document_id: [])
    runtime = SimpleNamespace(rag_tool=SimpleNamespace(
        rag_namespace=namespace, _get_pipeline=lambda scope: pipeline,
    ))
    monkeypatch.setattr(GraphExtractor, "extract", lambda *args, **kwargs: SimpleNamespace(
        to_store_payload=lambda: {},
    ))
    loaded = []

    def open_store(**kwargs):
        loaded.append(True)
        return store

    def publish(document_id, build_id, payload, *, rag_namespace):
        assert rag_namespace == namespace
        with connect(db) as connection:
            assert connection.execute(
                "select 1 from graph_document_cleanup where user_id = ? and document_id = ?",
                (user.id, document_id),
            ).fetchone() is not None
        raise TimeoutError("publication result unknown")

    deletions = []
    store = SimpleNamespace(
        close=lambda: None, replace_document_graph=publish,
        delete_document=lambda document_id, **kwargs: deletions.append((document_id, kwargs)),
    )
    monkeypatch.setenv("ZHIYAN_GRAPH_ENABLED", "1")
    with pytest.raises(TimeoutError):
        GraphService(db, store_factory=open_store).build_after_import(task, runtime)

    monkeypatch.setenv("ZHIYAN_GRAPH_ENABLED", "0")
    restarted = GraphService(db, store_factory=open_store)
    restarted.delete_document(user.id, FOREIGN)
    restarted.delete_document(user.id, DOC)
    restarted.delete_document(user.id, DOC)
    assert loaded == [True, True]
    assert deletions == [(DOC, {"rag_namespace": namespace})]


def test_graph_service_reuses_pooled_driver_caches_capabilities_and_recovers(monkeypatch):
    monkeypatch.setenv("ZHIYAN_GRAPH_ENABLED", "1")
    monkeypatch.setenv("NEO4J_URI", "neo4j://neo4j:7687")
    created = []
    closed = []
    checks = []
    fail_next = {"value": False}

    class PooledStore:
        def __init__(self, **kwargs):
            self.kwargs = kwargs
            created.append(kwargs)

        def check_database(self):
            checks.append(True)
            if fail_next["value"]:
                fail_next["value"] = False
                raise OSError("connection dropped")

        def close(self):
            closed.append(True)

    service = GraphService(store_factory=PooledStore, capabilities_ttl_seconds=5.0)
    # 多次调用能力探测命中 5s 短 TTL 缓存，仅建立 1 次 Driver 连接池、执行 1 次 check_database
    assert service.capabilities() == (True, None)
    assert service.capabilities() == (True, None)
    assert len(created) == 1
    assert len(checks) == 1

    # 配置变更立即失效缓存并关闭旧池重建
    monkeypatch.setenv("NEO4J_URI", "neo4j://neo4j-v2:7687")
    assert service.capabilities() == (True, None)
    assert len(created) == 2
    assert len(closed) == 1

    # 关闭开关立即失效缓存且优雅关闭连接池
    service.close()
    assert len(closed) == 2

    # 连接失败自动销毁坏连接池，下次自动重建恢复
    no_cache = GraphService(store_factory=PooledStore, capabilities_ttl_seconds=0.0)
    assert no_cache.capabilities() == (True, None)
    fail_next["value"] = True
    assert no_cache.capabilities() == (False, "graph_unavailable")
    assert no_cache.capabilities() == (True, None)
    assert len(created) == 4
    no_cache.close()


def test_async_graph_projection_worker_decouples_import_and_respects_deletion_fence(
    tmp_path, monkeypatch
):
    from threading import RLock
    from app.graph_service import GraphProjectionWorker
    from app.qa_deletion import QaDeletionRepository
    from hello_agents.memory.graph.extractor import GraphExtractor

    db = tmp_path / "async_graph.db"
    initialize_database(db)
    user = AuthService(db).register("async_owner", "correct horse battery")
    namespace = f"pdf_{user.id}"
    published = []
    fenced_doc = "cccccccc-cccc-4ccc-8ccc-cccccccccccc"

    class FakeStore:
        def replace_document_graph(self, document_id, build_id, payload, *, rag_namespace):
            published.append((document_id, rag_namespace))

        def close(self):
            pass

    history_docs = [
        {"document_id": DOC, "document_name": "a.md"},
        {"document_id": fenced_doc, "document_name": "b.md"},
    ]
    runtime = SimpleNamespace(
        lock=RLock(),
        history=SimpleNamespace(load=lambda: {"documents": list(history_docs)}),
        rag_tool=SimpleNamespace(
            rag_namespace=namespace,
            _get_pipeline=lambda scope: SimpleNamespace(get_document_chunks=lambda doc_id: []),
        ),
    )
    runtime_registry = SimpleNamespace(
        acquire_background=lambda uid: runtime,
        release_background=lambda uid: None,
    )
    monkeypatch.setattr(
        GraphExtractor,
        "extract",
        lambda *args, **kwargs: SimpleNamespace(to_store_payload=lambda: {}),
    )
    monkeypatch.setenv("ZHIYAN_GRAPH_ENABLED", "1")
    service = GraphService(db, store_factory=lambda **kwargs: FakeStore())
    worker = GraphProjectionWorker(db, runtime_registry, service, poll_interval=0.05)

    assert service.enqueue_after_import(
        SimpleNamespace(user_id=user.id, document_id=DOC, original_name="a.md")
    )
    assert service.enqueue_after_import(
        SimpleNamespace(user_id=user.id, document_id=fenced_doc, original_name="b.md")
    )
    # 模拟第二个文档在后台建图执行前被删除建栅栏
    QaDeletionRepository(db).create_document_deletion(user.id, fenced_doc)

    assert worker.run_once() is True
    assert worker.run_once() is True
    assert worker.run_once() is False
    # 仅未删除文档发布到 Neo4j，被删除栅栏拦住的文档零发布且任务已清除
    assert published == [(DOC, namespace)]
    with connect(db) as conn:
        rows = conn.execute(
            "select document_id, status from graph_build_jobs where user_id = ?",
            (user.id,),
        ).fetchall()
        assert [(row["document_id"], row["status"]) for row in rows] == [(DOC, "succeeded")]
    service.close()
