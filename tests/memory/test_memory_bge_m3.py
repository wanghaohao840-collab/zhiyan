import json
from types import SimpleNamespace

import httpx
import pytest

from app.memory_repository import MemorySnapshotRepository
from hello_agents.memory.base import MemoryConfig, MemoryItem
from hello_agents.memory.embedding import SimpleEmbedding
from hello_agents.memory.memory_embedding import get_memory_embedder
from hello_agents.memory.manager import MemoryManager
from hello_agents.memory.rag.embedding_profile import EmbeddingFailure
from hello_agents.memory.storage.vector_store import InMemoryVectorStore
from hello_agents.memory.types.episodic import EpisodicMemory
from hello_agents.memory.types.semantic import SemanticMemory


VALUES = {
    "MEMORY_EMBEDDING_PROVIDER": "siliconflow",
    "RAG_EMBEDDING_API_KEY": "test-private-key",
    "RAG_EMBEDDING_MAX_RETRIES": "0",
}


def make_embedder(calls, *, fail=False):
    def handle(request):
        body = json.loads(request.content)
        assert body["model"] == "BAAI/bge-m3"
        calls.extend(body["input"])
        if fail:
            return httpx.Response(402)
        return httpx.Response(200, json={
            "model": "BAAI/bge-m3",
            "data": [{"index": i, "embedding": [1.0] + [0.0] * 1023}
                     for i in range(len(body["input"]))],
        })
    return get_memory_embedder(values=VALUES, transport=httpx.MockTransport(handle))


def test_activation_is_separate_and_long_notes_keep_all_content():
    assert isinstance(get_memory_embedder(values={"RAG_EMBEDDING_PROVIDER": "siliconflow"}), SimpleEmbedding)
    calls = []
    embedder = make_embedder(calls)
    text = "知" * 3001
    vector = embedder.encode(text)
    assert len(vector) == 1024
    assert vector[0] == 1.0
    assert "".join(calls) == text
    assert all(len(part.encode()) <= 6000 for part in calls)


def test_invalid_remote_configuration_is_reported_by_manager(monkeypatch, tmp_path):
    monkeypatch.setenv("MEMORY_EMBEDDING_PROVIDER", "siliconflow")
    monkeypatch.delenv("RAG_EMBEDDING_API_KEY", raising=False)
    monkeypatch.delenv("MEMORY_EMBEDDING_API_KEY", raising=False)
    with pytest.raises(EmbeddingFailure):
        MemoryManager(
            MemoryConfig(database_path=str(tmp_path / "memory.db")),
            enable_episodic=False,
        )


@pytest.mark.parametrize("memory_type", ["episodic", "semantic"])
def test_remote_failure_does_not_become_simple_or_success(monkeypatch, tmp_path, memory_type):
    calls = []
    embedder = make_embedder(calls, fail=True)
    module = f"hello_agents.memory.types.{memory_type}"
    factory = "create_embedding_model_with_fallback" if memory_type == "episodic" else "get_text_embedder"
    monkeypatch.setattr(f"{module}.{factory}", lambda: embedder)
    memory_class = EpisodicMemory if memory_type == "episodic" else SemanticMemory
    memory = memory_class(MemoryConfig(database_path=str(tmp_path / "memory.db")), InMemoryVectorStore())
    try:
        with pytest.raises(EmbeddingFailure) as exc:
            memory.add(MemoryItem(content="private memory", memory_type=memory_type))
        assert exc.value.status_code == 402
        assert memory.count() == 0
        with pytest.raises(EmbeddingFailure):
            memory.retrieve("question")
    finally:
        if hasattr(memory, "close"):
            memory.close()


def test_restart_rebuilds_vectors_and_preserves_other_users(monkeypatch, tmp_path):
    calls = []
    embedder = make_embedder(calls)
    monkeypatch.setattr("hello_agents.memory.types.semantic.get_text_embedder", lambda: embedder)
    monkeypatch.setattr("hello_agents.memory.types.episodic.create_embedding_model_with_fallback", lambda: embedder)
    store = InMemoryVectorStore()
    config = MemoryConfig(database_path=str(tmp_path / "memory.db"))
    semantic = SemanticMemory(config, store)
    episodic = EpisodicMemory(config, store)
    manager = SimpleNamespace(memory_types={"semantic": semantic, "episodic": episodic})
    other = MemoryItem(content="other user's fact", memory_type="semantic", metadata={"user_id": "other"})
    semantic.add(other)
    path = tmp_path / "memories.json"
    records = [
        {"id": kind, "content": f"{kind} fact", "memory_type": kind,
         "importance": 0.8, "timestamp": "2026-11-06T00:00:00",
         "metadata": {"user_id": "owner"}}
        for kind in ["semantic", "episodic"]
    ]
    path.write_text(json.dumps({"user_id": "owner", "memories": records}))
    original = path.read_bytes()
    repo = MemorySnapshotRepository(path, "owner")
    try:
        repo.restore_to_manager(manager)
        assert "__bge_m3_" in semantic.vector_collection
        assert semantic.vector_collection == episodic.vector_collection
        assert len(semantic.retrieve("fact", user_id="owner")) == 1
        assert len(episodic.retrieve("fact", user_id="owner")) == 1
        assert {p.payload["user_id"] for p in store.scroll(semantic.vector_collection)} == {"owner", "other"}
        assert all(len(p.vector) == 1024 for p in store.scroll(semantic.vector_collection, with_vectors=True))
        assert path.read_bytes() == original
        # Restoring an empty snapshot must also remove the owner's stale vectors.
        path.write_text(json.dumps({"user_id": "owner", "memories": []}))
        repo.restore_to_manager(manager)
        assert {p.payload["user_id"] for p in store.scroll(semantic.vector_collection)} == {"other"}
    finally:
        episodic.close()


def test_failed_snapshot_rebuild_clears_partial_vectors(monkeypatch, tmp_path):
    calls = []
    embedder = make_embedder(calls)
    monkeypatch.setattr("hello_agents.memory.types.semantic.get_text_embedder", lambda: embedder)
    memory = SemanticMemory(MemoryConfig(), InMemoryVectorStore())
    manager = SimpleNamespace(memory_types={"semantic": memory})
    path = tmp_path / "memories.json"
    records = [
        {"id": str(i), "content": f"fact {i}", "memory_type": "semantic",
         "importance": 0.8, "timestamp": "2026-11-06T00:00:00",
         "metadata": {"user_id": "owner"}}
        for i in range(2)
    ]
    path.write_text(json.dumps({"user_id": "owner", "memories": records}))
    original = path.read_bytes()
    encode = embedder.encode

    def fail_second(text):
        if text == "fact 1":
            raise EmbeddingFailure("http", status_code=402)
        return encode(text)

    monkeypatch.setattr(embedder, "encode", fail_second)
    with pytest.raises(RuntimeError, match="fail-closed"):
        MemorySnapshotRepository(path, "owner").restore_to_manager(manager)
    assert memory.count() == 0
    assert memory.vector_store.scroll(memory.vector_collection) == []
    assert path.read_bytes() == original


def test_manager_propagates_remote_retrieval_failures(monkeypatch, tmp_path):
    embedder = make_embedder([], fail=True)
    monkeypatch.setattr("hello_agents.memory.types.semantic.get_text_embedder", lambda: embedder)
    manager = MemoryManager(
        MemoryConfig(database_path=str(tmp_path / "memory.db")),
        enable_episodic=False,
    )
    with pytest.raises(EmbeddingFailure):
        manager.retrieve_memories("question", memory_types=["semantic"])
