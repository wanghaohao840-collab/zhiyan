from contextlib import closing
from dataclasses import replace
import json
from pathlib import Path
import sqlite3
from uuid import UUID

import pytest

from deploy.embedding_json_migrate import (
    BASE_COLLECTION, DEFAULT_DATASET, JsonMigrationFailure,
    candidate_from_evidence, prepare_json_migration,
)
from evals.rag_embedding_acceptance import compare_runtimes, write_report
from evals.rag_retrieval_quality import load_dataset
from hello_agents.memory.rag.embedding_runtime import build_rag_embedding
from hello_agents.memory.rag.json_index_cache import JsonIndexCache
from hello_agents.memory.rag.source_records import SourceInventoryError


PRIVATE = "account data that must stay out of evidence"


class TestEmbeddingRuntime:
    __test__ = False
    batch_size = 1

    def __init__(self, items):
        self.profile = build_rag_embedding({
            "RAG_EMBEDDING_PROVIDER": "siliconflow", "RAG_EMBEDDING_API_KEY": "test-credential",
        }, backend="json").profile
        self.documents = {item["content"]: index for index, item in enumerate(items)}
        self.questions = {item["question"]: index for index, item in enumerate(items)}
        self.embedded = []
        self.fail_on = None

    def _vector(self, index):
        vector = [0.0] * self.profile.dimension
        vector[index] = 1.0
        return vector

    def embed_documents(self, texts):
        if self.fail_on in texts:
            raise RuntimeError("injected transport failure")
        self.embedded.extend(texts)
        return [self._vector(self.documents.get(text, 0)) for text in texts]

    def embed_query(self, text):
        return self._vector(self.questions[text])


@pytest.fixture
def migration(tmp_path):
    root = tmp_path / "data"
    root.mkdir()
    with closing(sqlite3.connect(root / "app.db")) as db:
        db.execute("CREATE TABLE users(id TEXT PRIMARY KEY,status TEXT,password_hash TEXT)")
        db.commit()
    items = load_dataset(DEFAULT_DATASET)
    runtime = TestEmbeddingRuntime(items)
    quality_path = tmp_path / "quality.json"
    quality = compare_runtimes(items, build_rag_embedding({}, backend="json"), runtime)
    write_report(quality_path, quality)
    runtime.embedded.clear()
    return root, runtime, quality_path


def account(root: Path, number: int, *, status="active", initialized=True, documents=()):
    user_id = str(UUID(int=number))
    with closing(sqlite3.connect(root / "app.db")) as db:
        db.execute("INSERT INTO users VALUES(?, ?, ?)", (user_id, status, PRIVATE))
        db.commit()
    user_root = root / "users" / user_id
    if initialized:
        user_root.mkdir(parents=True)
        (user_root / "history.json").write_text(json.dumps({
            "documents": [{"document_id": document} for document in documents],
            "notes": [{"content": PRIVATE}],
        }), encoding="utf-8")
    return user_id, user_root / "rag" / "rag_cache.json"


def write_cache(path, user_id, chunks, *, document_id="shared-document"):
    namespace = f"pdf_{user_id}"
    rows = []
    for index, content in enumerate(chunks):
        chunk_id = f"{document_id}_{index}"
        rows.append({
            "id": chunk_id, "document_id": document_id, "content": content, "vector": [1.0] * 384,
            "metadata": {"memory_id": chunk_id, "document_id": document_id, "content": content,
                         "rag_namespace": namespace, "chunk_index": index, "document_version": 1},
        })
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "collection_name": BASE_COLLECTION, "dimension": 384, "rag_namespace": namespace,
        "updated_at": "2026-11-23T10:00:00", "chunk_count": len(rows), "chunks": rows,
    }), encoding="utf-8")


def prepare(migration):
    root, runtime, quality_path = migration
    return prepare_json_migration(
        root, migration_id="local-bge-m3", candidate_runtime=runtime, quality_report=quality_path,
    )


def evidence_path(root):
    return root / "vector_indexes" / "rag" / "json-migrations" / "local-bge-m3" / "evidence.json"


def test_migration_validates_all_users_and_can_resume(migration):
    root, runtime, _ = migration
    user, legacy = account(root, 1, documents=("shared-document",))
    other, other_legacy = account(root, 2, status="disabled", documents=("shared-document",))
    empty, empty_legacy = account(root, 3)
    never_opened, missing_legacy = account(root, 4, initialized=False)
    write_cache(legacy, user, [PRIVATE])
    write_cache(other_legacy, other, ["other private chunk"])
    before = legacy.read_bytes(), other_legacy.read_bytes()

    record = prepare(migration)
    assert record.validation.chunk_count == 2
    assert record.validation.scope_leaks == 0
    assert record.validation.recall_at_5 == record.validation.mrr_at_5 == 1
    assert candidate_from_evidence(evidence_path(root)) == record
    for account_id, path, contents in [
        (user, legacy, [PRIVATE]), (other, other_legacy, ["other private chunk"]), (empty, empty_legacy, []),
    ]:
        points = JsonIndexCache(path, record.identity, f"pdf_{account_id}").load()
        assert [point.payload["content"] for point in points] == contents
    assert before == (legacy.read_bytes(), other_legacy.read_bytes())
    assert not missing_legacy.parents[1].exists()
    assert not (root / "vector_indexes" / "rag" / "registry.json").exists()
    assert not (root / "vector_indexes" / "rag" / "cutovers").exists()
    evidence_text = evidence_path(root).read_text(encoding="utf-8")
    evidence = json.loads(evidence_text)
    assert evidence["account_count"] == 4
    assert evidence["legacy_cache_count"] == 2
    assert evidence["initialized_empty_count"] == evidence["uninitialized_count"] == 1
    assert all(secret not in evidence_text for secret in [PRIVATE, "test-credential", never_opened])
    assert prepare(migration) == record
    assert runtime.embedded == [PRIVATE, "other private chunk"]


def test_resume_after_partial_embedding_and_detect_source_drift(migration):
    root, runtime, _ = migration
    user, legacy = account(root, 1, documents=("shared-document",))
    write_cache(legacy, user, ["first private chunk", "second private chunk"])
    runtime.fail_on = "second private chunk"
    with pytest.raises(RuntimeError, match="injected transport failure"):
        prepare(migration)
    assert not evidence_path(root).exists()
    runtime.fail_on = None
    record = prepare(migration)
    assert record.validation.chunk_count == 2
    assert runtime.embedded == ["first private chunk", "second private chunk"]

    write_cache(legacy, user, ["changed private chunk", "second private chunk"])
    with pytest.raises(JsonMigrationFailure, match="source_changed"):
        prepare(migration)


def test_missing_cache_with_authoritative_documents_rejects_migration(migration):
    root, runtime, _ = migration
    account(root, 1, documents=("shared-document",))
    with pytest.raises(JsonMigrationFailure, match="missing_source_cache"):
        prepare(migration)
    assert not runtime.embedded
    assert not evidence_path(root).exists()


def test_rejects_orphan_cache_and_quality_from_another_dataset(migration, tmp_path):
    root, runtime, quality = migration
    _, orphan = account(root, 9, initialized=False)
    write_cache(orphan, str(UUID(int=9)), [])
    with closing(sqlite3.connect(root / "app.db")) as db:
        db.execute("DELETE FROM users")
        db.commit()
    with pytest.raises(SourceInventoryError, match="authority_orphan"):
        prepare(migration)

    orphan.unlink()
    orphan.parent.rmdir()
    orphan.parents[1].rmdir()
    items = load_dataset(DEFAULT_DATASET)
    result = compare_runtimes(items, build_rag_embedding({}, backend="json"), runtime)
    wrong_report = tmp_path / "wrong-quality.json"
    write_report(wrong_report, replace(result, dataset_digest="0" * 64))
    with pytest.raises(JsonMigrationFailure, match="quality_changed"):
        prepare((root, runtime, wrong_report))
    assert not evidence_path(root).exists()


def test_existing_empty_legacy_cache_is_validated(migration):
    root, _, _ = migration
    user, legacy = account(root, 1)
    write_cache(legacy, user, [])
    record = prepare(migration)
    assert record.validation.chunk_count == 0
    assert JsonIndexCache(legacy, record.identity, f"pdf_{user}").load() == []

    evidence = json.loads(evidence_path(root).read_text(encoding="utf-8"))
    evidence["candidate_record"]["validation"]["chunk_count"] = 1
    evidence_path(root).write_text(json.dumps(evidence), encoding="utf-8")
    with pytest.raises(JsonMigrationFailure, match="evidence"):
        candidate_from_evidence(evidence_path(root))
