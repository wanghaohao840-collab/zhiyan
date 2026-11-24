from app.database import initialize_database
from app.runtime import UserRuntimeRegistry
from app.storage import UserStorage


def test_new_user_bootstraps_managed_json_but_missing_old_cache_fails(tmp_path, monkeypatch):
    import pytest
    from hello_agents.memory.rag.embedding_runtime import build_rag_embedding
    from hello_agents.memory.rag.index_identity import IndexIdentity
    from hello_agents.memory.rag.index_registry import IndexRecord, IndexRegistry, IndexValidation
    from hello_agents.memory.rag.json_index_cache import JsonIndexCache, JsonIndexCacheError

    monkeypatch.setenv("RAG_BACKEND", "json")
    monkeypatch.setenv("RAG_EMBEDDING_PROVIDER", "siliconflow")
    monkeypatch.setenv("RAG_EMBEDDING_API_KEY", "test-key")
    import os
    runtime = build_rag_embedding(dict(os.environ), backend="json")
    identity = IndexIdentity("json", "pdf_learning_collection", runtime.profile)
    IndexRegistry(tmp_path).put(IndexRecord(
        identity, "pdf_learning_collection", "test-new-user", IndexValidation(
            True, "2026-11-23T00:00:00Z", 0, "a" * 64, 0,
        ), "active",
    ))
    db_path = tmp_path / "app.db"
    initialize_database(db_path)
    storage = UserStorage(tmp_path)
    registry = UserRuntimeRegistry(db_path, storage)
    original_write = JsonIndexCache.write

    def interrupted_write(cache, points):
        original_write(cache, points)
        raise OSError("injected first-login interruption")

    with monkeypatch.context() as fault:
        fault.setattr(JsonIndexCache, "write", interrupted_write)
        with pytest.raises(OSError, match="interruption"):
            registry.get_or_create("new-user")
    assert not storage.user_paths("new-user").root.exists()
    first = registry.get_or_create("new-user")
    assert first.rag_tool._get_pipeline().dimension == 1024
    assert first.rag_tool._get_pipeline().chunks == []
    assert first.paths.history.exists()
    cache = first.rag_tool._get_pipeline().cache_path
    registry.release_if_unused("new-user")
    cache.unlink()
    with pytest.raises(JsonIndexCacheError, match="missing"):
        UserRuntimeRegistry(db_path, storage).get_or_create("new-user")


def test_product_runtime_does_not_connect_optional_graph(tmp_path, monkeypatch):
    from hello_agents.tools.builtin.rag_tool import RAGTool

    monkeypatch.setenv("NEO4J_URI", "neo4j://unavailable.invalid:7687")
    monkeypatch.setenv("NEO4J_USERNAME", "neo4j")
    monkeypatch.setenv("NEO4J_PASSWORD", "test-only-password")
    attempts = []
    monkeypatch.setattr(RAGTool, "_configure_graph_service", lambda *a, **kw: attempts.append(kw))
    db_path = tmp_path / "app.db"
    initialize_database(db_path)
    runtime = UserRuntimeRegistry(db_path=db_path, storage=UserStorage(tmp_path / "data")).get_or_create("user-1")

    assert attempts == []
    assert runtime.rag_tool.graph_service is None


def test_user_runtime_registry_reuses_runtime_per_user(tmp_path):
    db_path = tmp_path / "app.db"
    initialize_database(db_path)
    registry = UserRuntimeRegistry(db_path=db_path, storage=UserStorage(tmp_path / "data"))

    first = registry.get_or_create("user-1")
    second = registry.get_or_create("user-1")
    other = registry.get_or_create("user-2")

    assert first is second
    assert first is not other
    assert first.lock is second.lock
    assert first.paths.history == tmp_path / "data" / "users" / "user-1" / "history.json"
    assert first.active_session_count == 0
    assert first.active_background_count == 0
    assert first.rag_tool.data_root == (tmp_path / "data").resolve()
    assert (first.memory_tool.memory_manager.memory_types["semantic"].vector_collection
            != other.memory_tool.memory_manager.memory_types["semantic"].vector_collection)


def test_runtime_lock_guards_history_writes(tmp_path):
    db_path = tmp_path / "app.db"
    initialize_database(db_path)
    runtime = UserRuntimeRegistry(db_path=db_path, storage=UserStorage(tmp_path / "data")).get_or_create("user-1")

    with runtime.lock:
        runtime.history.add_note({"note": "inside lock", "concept": "runtime"})

    assert runtime.history.load()["notes"][0]["note"] == "inside lock"
