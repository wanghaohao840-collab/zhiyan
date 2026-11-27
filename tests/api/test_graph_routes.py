from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from threading import Event, RLock
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from api.app import create_api_app
from app.auth import AuthService
from app.database import connect, initialize_database
from app.graph_service import GraphProjectionWorker, GraphService
from app.qa_deletion import QaDeletionRepository, QaDeletionWorker
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


@pytest.fixture
def graph_build_and_deletion(tmp_path, monkeypatch):
    from hello_agents.memory.graph.extractor import GraphExtractor

    db = tmp_path / "graph_deletion.db"
    initialize_database(db)
    auth = AuthService(db)
    user = auth.register("graph_owner", "correct horse battery")
    other_user = auth.register("other_owner", "correct horse battery")
    task = SimpleNamespace(user_id=user.id, document_id=DOC, original_name="example.md")
    namespace = f"pdf_{user.id}"
    other_namespace = f"pdf_{other_user.id}"
    history_docs = [{"document_id": DOC}]
    graph_documents = {(other_namespace, DOC), (namespace, FOREIGN)}
    graph_changes = []
    hooks = SimpleNamespace(during_extract=lambda: None, before_publish=lambda: None)

    def extract(*args, **kwargs):
        hooks.during_extract()
        return SimpleNamespace(to_store_payload=lambda: {})

    def publish(document_id, build_id, payload, *, rag_namespace):
        hooks.before_publish()
        graph_changes.append(("publish", rag_namespace, document_id))
        graph_documents.add((rag_namespace, document_id))

    def remove(document_id, *, rag_namespace):
        graph_changes.append(("delete", rag_namespace, document_id))
        graph_documents.discard((rag_namespace, document_id))

    monkeypatch.setattr(GraphExtractor, "extract", extract)
    monkeypatch.setenv("ZHIYAN_GRAPH_ENABLED", "1")
    store = SimpleNamespace(replace_document_graph=publish, delete_document=remove, close=lambda: None)
    service = GraphService(db, store_factory=lambda **kwargs: store)
    runtime = SimpleNamespace(
        lock=RLock(),
        history=SimpleNamespace(load=lambda: {"documents": list(history_docs)}),
        rag_tool=SimpleNamespace(
            rag_namespace=namespace,
            _get_pipeline=lambda scope: SimpleNamespace(get_document_chunks=lambda doc_id: []),
        ),
        memory_tool=SimpleNamespace(memory_manager=object()),
    )
    runtime_registry = SimpleNamespace(
        acquire_background=lambda uid: runtime,
        release_background=lambda uid: None,
    )
    projection = GraphProjectionWorker(db, runtime_registry, service)
    deletion_repository = QaDeletionRepository(db)
    deletion_worker = QaDeletionWorker(
        deletion_repository,
        runtime_registry,
        SimpleNamespace(perform_document_delete=lambda *args, **kwargs: history_docs.clear()),
        SimpleNamespace(clear_document_selection=lambda *args: None),
        graph_service=service,
    )

    def fence_document():
        return deletion_repository.create_document_deletion(user.id, DOC)

    def delete():
        deletion = fence_document()
        assert deletion_worker.run_once("graph-deletion-test")
        assert deletion_repository.get_deletion(user.id, deletion.id).status == "completed"

    yield SimpleNamespace(
        db=db, user=user, other_user=other_user, task=task, namespace=namespace,
        other_namespace=other_namespace, graph_documents=graph_documents,
        graph_changes=graph_changes, hooks=hooks, service=service, projection=projection,
        delete=delete, fence_document=fence_document,
    )
    service.close()


def test_projection_cannot_publish_after_deletion_completes_during_extraction(graph_build_and_deletion):
    parts = graph_build_and_deletion
    parts.hooks.during_extract = parts.delete

    assert parts.service.enqueue_after_import(parts.task)
    assert parts.projection.run_once()

    assert parts.graph_changes == [("delete", parts.namespace, DOC)]
    assert parts.graph_documents == {(parts.other_namespace, DOC), (parts.namespace, FOREIGN)}
    assert not parts.service.enqueue_after_import(parts.task)
    with connect(parts.db) as conn:
        assert conn.execute("select 1 from graph_document_cleanup").fetchall() == []
        assert conn.execute("select 1 from graph_build_jobs").fetchall() == []


def test_projection_blocked_during_extraction_is_not_recorded_as_succeeded(graph_build_and_deletion):
    parts = graph_build_and_deletion
    parts.hooks.during_extract = parts.fence_document

    assert parts.service.enqueue_after_import(parts.task)
    assert parts.projection.run_once()

    assert parts.graph_changes == []
    with connect(parts.db) as conn:
        assert conn.execute("select 1 from graph_build_jobs").fetchall() == []


def test_deletion_waits_for_publication_of_only_the_same_document(graph_build_and_deletion, monkeypatch):
    parts = graph_build_and_deletion
    publishing = Event()
    allow_publication = Event()
    deleting = Event()
    delete_document = parts.service.delete_document

    def pause_publication():
        publishing.set()
        assert allow_publication.wait(timeout=10)

    def observe_deletion(user_id, document_id):
        if (user_id, document_id) == (parts.user.id, DOC):
            deleting.set()
        return delete_document(user_id, document_id)

    def finish_unrelated_operations():
        delete_document(parts.other_user.id, DOC)
        assert parts.service.enqueue_after_import(SimpleNamespace(
            user_id=parts.user.id, document_id=FOREIGN, original_name="unrelated.md",
        ))

    parts.hooks.before_publish = pause_publication
    monkeypatch.setattr(parts.service, "delete_document", observe_deletion)
    assert parts.service.enqueue_after_import(parts.task)
    with ThreadPoolExecutor(max_workers=3) as executor:
        build = executor.submit(parts.projection.run_once)
        try:
            assert publishing.wait(timeout=5)
            deletion = executor.submit(parts.delete)
            assert deleting.wait(timeout=5)
            executor.submit(finish_unrelated_operations).result(timeout=5)
            with pytest.raises(TimeoutError):
                deletion.result(timeout=0.25)
        finally:
            allow_publication.set()
        assert build.result(timeout=5)
        deletion.result(timeout=5)

    assert parts.graph_changes == [
        ("delete", parts.other_namespace, DOC),
        ("publish", parts.namespace, DOC),
        ("delete", parts.namespace, DOC),
    ]
    assert parts.graph_documents == {(parts.namespace, FOREIGN)}
    with connect(parts.db) as conn:
        assert conn.execute("select 1 from graph_document_cleanup").fetchall() == []


def test_graph_recovery_finishes_exhausted_attempt_and_requeues_remaining_attempt(
    graph_build_and_deletion, monkeypatch
):
    parts = graph_build_and_deletion
    monkeypatch.setattr("app.graph_service._utc_after", lambda delay: "2000-01-01T00:00:00Z")
    assert parts.service.enqueue_after_import(parts.task)
    for attempt in (1, 2):
        claimed = parts.projection._claim_next()
        assert claimed.attempt_count == attempt
        parts.projection._fail_or_retry_job(claimed, RuntimeError())
    assert parts.projection._claim_next().attempt_count == 3
    assert parts.service.enqueue_after_import(SimpleNamespace(
        user_id=parts.user.id, document_id=FOREIGN, original_name="retry.md",
    ))
    assert parts.projection._claim_next().attempt_count == 1

    restarted = GraphProjectionWorker(parts.db, parts.projection.runtime_registry, parts.service)
    assert restarted.recover_interrupted() == 2
    with connect(parts.db) as conn:
        rows = conn.execute(
            "select document_id, status, attempt_count, next_attempt_at, last_error "
            "from graph_build_jobs order by document_id"
        ).fetchall()
    assert [tuple(row) for row in rows] == [
        (DOC, "failed", 3, None, "GraphBuildInterrupted"),
        (FOREIGN, "queued", 1, None, None),
    ]
    retried = restarted._claim_next()
    assert (retried.document_id, retried.attempt_count) == (FOREIGN, 2)
    assert restarted._claim_next() is None
