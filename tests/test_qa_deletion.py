from __future__ import annotations

import json
import threading
import time
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

import app.qa_deletion as qa_deletion_module
import app.qa_repository as qa_repository_module
from app.coordination import UserMutationCoordinator
from app.database import connect, initialize_database
from app.document_library import DocumentLibraryService, DocumentNotFoundError
from app.history import HistoryRepository
from app.qa_deletion import (
    QaDeletionRepository,
    QaDeletionService,
    QaDeletionWorker,
)
from app.qa_models import QaDocumentCandidate, QaValidationError
from app.qa_migration import QaLegacyMigrationService
from app.qa_repository import QaRepository
from app.storage import UserStorage, write_json_atomic


OWNER = "owner"
OTHER = "other"


class Sessions:
    def __init__(self, runtime):
        self.runtime_registry = RuntimeRegistry(runtime)
        self.sessions = {
            "token": SimpleNamespace(user_id=OWNER, runtime=runtime),
            "other": SimpleNamespace(user_id=OTHER, runtime=runtime),
        }
        self.cleared = []

    def get_session(self, token):
        return self.sessions[token]

    def clear_document_selection(self, user_id, document_id):
        self.cleared.append((user_id, document_id))
        return 1


class RuntimeRegistry:
    def __init__(self, runtime):
        self.runtime = runtime
        self.acquired = []
        self.released = []

    def acquire_background(self, user_id):
        self.acquired.append(user_id)
        return self.runtime

    def release_background(self, user_id):
        self.released.append(user_id)


class MemoryManager:
    def __init__(self):
        self.removed = []

    def remove_memory(self, memory_id, *, memory_type):
        self.removed.append((memory_id, memory_type))
        return True


class Documents:
    def __init__(self):
        self.deleted = []
        self.items = {OWNER: {"doc"}, OTHER: {"other-doc"}}

    def has_document(self, user_id, document_id):
        return document_id in self.items[user_id]

    def perform_document_delete(
        self,
        user_id,
        runtime,
        document_id,
        *,
        replay_if_missing=False,
    ):
        if document_id not in self.items[user_id]:
            if replay_if_missing:
                return
            raise DocumentNotFoundError()
        self.items[user_id].remove(document_id)
        self.deleted.append((user_id, document_id))


class Wake:
    def __init__(self):
        self.count = 0

    def notify(self):
        self.count += 1


class BeginBlockingConnection:
    def __init__(self, connection, acquired, release):
        self.connection = connection
        self.acquired = acquired
        self.release = release

    def execute(self, statement, *args, **kwargs):
        cursor = self.connection.execute(statement, *args, **kwargs)
        if statement.strip().lower() == "begin immediate":
            self.acquired.set()
            assert self.release.wait(timeout=3)
        return cursor

    def __getattr__(self, name):
        return getattr(self.connection, name)


class BeginObservedConnection:
    def __init__(self, connection, attempted):
        self.connection = connection
        self.attempted = attempted

    def execute(self, statement, *args, **kwargs):
        if statement.strip().lower() == "begin immediate":
            self.attempted.set()
        return self.connection.execute(statement, *args, **kwargs)

    def __getattr__(self, name):
        return getattr(self.connection, name)


class CommitBlockingConnection:
    def __init__(self, connection, ready, release):
        self.connection = connection
        self.ready = ready
        self.release = release

    def commit(self):
        self.ready.set()
        assert self.release.wait(timeout=3)
        return self.connection.commit()

    def __getattr__(self, name):
        return getattr(self.connection, name)


def wait_for(predicate, timeout=3):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.01)
    raise AssertionError("condition was not reached")


def iso(value: datetime) -> str:
    return value.isoformat().replace("+00:00", "Z")


@pytest.fixture
def deletion_parts(tmp_path):
    db_path = tmp_path / "app.db"
    initialize_database(db_path)
    with connect(db_path) as conn:
        conn.executemany(
            """
            insert into users (
                id, username, username_key, password_hash, created_at, updated_at
            ) values (?, ?, ?, 'hash', '2026-08-27T00:00:00Z', '2026-08-27T00:00:00Z')
            """,
            ((OWNER, "Owner", "owner"), (OTHER, "Other", "other")),
        )
    qa = QaRepository(db_path)
    deletion_repository = QaDeletionRepository(db_path)
    memory = MemoryManager()
    runtime = SimpleNamespace(
        memory_tool=SimpleNamespace(memory_manager=memory)
    )
    sessions = Sessions(runtime)
    documents = Documents()
    wake = Wake()
    service = QaDeletionService(
        sessions, documents, qa, deletion_repository, wake
    )
    worker = QaDeletionWorker(
        deletion_repository,
        sessions.runtime_registry,
        documents,
        sessions,
    )
    return qa, deletion_repository, service, worker, memory, documents, sessions


def completed_conversation(qa, *, document_id="doc", memory_id="qa-memory"):
    conversation = qa.create_conversation(
        OWNER, (QaDocumentCandidate(document_id, "文档.pdf", OWNER),)
    )
    turn = qa.create_pending_turn(
        OWNER, conversation.id, "问题", "joint", f"client-{conversation.id}"
    )
    qa.complete_turn(
        OWNER, turn.assistant_message.id, 0, "回答", (), "none", memory_id
    )
    return conversation, turn


def test_document_fence_commits_first_and_creation_observes_it(
    deletion_parts,
    monkeypatch,
) -> None:
    qa, repository, _, _, _, _, _ = deletion_parts
    deletion_ready_to_commit = threading.Event()
    release_deletion_commit = threading.Event()
    creation_begin_attempted = threading.Event()
    original_deletion_connect = qa_deletion_module.connect
    original_repository_connect = qa_repository_module.connect

    def synchronized_deletion_connect(db_path):
        connection = original_deletion_connect(db_path)
        if threading.current_thread().name == "document-delete":
            return CommitBlockingConnection(
                connection,
                deletion_ready_to_commit,
                release_deletion_commit,
            )
        return connection

    def synchronized_repository_connect(db_path):
        connection = original_repository_connect(db_path)
        if threading.current_thread().name == "conversation-create":
            return BeginObservedConnection(connection, creation_begin_attempted)
        return connection

    monkeypatch.setattr(
        qa_deletion_module, "connect", synchronized_deletion_connect
    )
    monkeypatch.setattr(
        qa_repository_module, "connect", synchronized_repository_connect
    )
    deletion_state = {}

    def delete_document():
        try:
            deletion_state["value"] = repository.create_document_deletion(
                OWNER, "doc"
            )
        except BaseException as error:
            deletion_state["error"] = error

    deleter = threading.Thread(target=delete_document, name="document-delete")
    deleter.start()
    if not deletion_ready_to_commit.wait(timeout=1):
        release_deletion_commit.set()
        deleter.join(timeout=3)
        pytest.fail("document deletion never reached its commit boundary")

    creation_state = {}

    def create_conversation():
        try:
            creation_state["value"] = qa.create_conversation(
                OWNER, (QaDocumentCandidate("doc", "文档.pdf", OWNER),)
            )
        except BaseException as error:
            creation_state["error"] = error

    creator = threading.Thread(
        target=create_conversation, name="conversation-create"
    )
    creator.start()
    try:
        assert creation_begin_attempted.wait(timeout=1)
        assert creator.is_alive()
    finally:
        release_deletion_commit.set()

    deleter.join(timeout=3)
    creator.join(timeout=3)
    assert not deleter.is_alive()
    assert not creator.is_alive()
    assert "error" not in deletion_state
    assert isinstance(creation_state.get("error"), QaValidationError)
    assert creation_state["error"].code == "QA_DOCUMENT_DELETING"
    with connect(qa.db_path) as conn:
        count = conn.execute("select count(*) from qa_conversations").fetchone()[0]
        assert count == 0


def test_conversation_immediate_transaction_commits_before_document_snapshot(
    deletion_parts,
    monkeypatch,
) -> None:
    qa, repository, _, worker, _, _, _ = deletion_parts
    begin_acquired = threading.Event()
    release_creation = threading.Event()
    deletion_begin_attempted = threading.Event()
    original_repository_connect = qa_repository_module.connect
    original_deletion_connect = qa_deletion_module.connect

    def synchronized_repository_connect(db_path):
        connection = original_repository_connect(db_path)
        if threading.current_thread().name == "conversation-create":
            return BeginBlockingConnection(
                connection, begin_acquired, release_creation
            )
        return connection

    def synchronized_deletion_connect(db_path):
        connection = original_deletion_connect(db_path)
        if threading.current_thread().name == "document-delete":
            return BeginObservedConnection(connection, deletion_begin_attempted)
        return connection

    monkeypatch.setattr(
        qa_repository_module, "connect", synchronized_repository_connect
    )
    monkeypatch.setattr(
        qa_deletion_module, "connect", synchronized_deletion_connect
    )
    creation_state = {}

    def create_conversation():
        try:
            creation_state["value"] = qa.create_conversation(
                OWNER, (QaDocumentCandidate("doc", "文档.pdf", OWNER),)
            )
        except BaseException as error:
            creation_state["error"] = error

    creator = threading.Thread(
        target=create_conversation, name="conversation-create"
    )
    creator.start()
    if not begin_acquired.wait(timeout=1):
        release_creation.set()
        creator.join(timeout=3)
        pytest.fail("conversation creation never acquired BEGIN IMMEDIATE")

    deletion_state = {}
    def delete_document():
        try:
            deletion_state["value"] = repository.create_document_deletion(
                OWNER, "doc"
            )
        except BaseException as error:
            deletion_state["error"] = error

    deleter = threading.Thread(target=delete_document, name="document-delete")
    deleter.start()
    assert deletion_begin_attempted.wait(timeout=1)
    assert deleter.is_alive()

    release_creation.set()
    creator.join(timeout=3)
    deleter.join(timeout=3)
    assert not creator.is_alive()
    assert not deleter.is_alive()
    assert "error" not in creation_state
    assert "error" not in deletion_state
    created = creation_state["value"]
    deletion = deletion_state["value"]
    assert deletion.affected_conversation_count == 1
    assert qa.get_conversation(OWNER, created.id) is None

    assert worker.run_once("delete-worker")
    assert repository.get_deletion(OWNER, deletion.id).status == "completed"
    with connect(qa.db_path) as conn:
        assert conn.execute(
            "select count(*) from qa_conversations where id = ?",
            (created.id,),
        ).fetchone()[0] == 0


def test_conversation_fence_hides_rows_and_blocks_late_completion(
    deletion_parts,
) -> None:
    qa, repository, service, worker, memory, _, _ = deletion_parts
    conversation = qa.create_conversation(
        OWNER, (QaDocumentCandidate("doc", "文档.pdf", OWNER),)
    )
    pending = qa.create_pending_turn(
        OWNER, conversation.id, "问题", "joint", "client"
    )

    deletion = service.request_conversation("token", conversation.id)
    duplicate = service.request_conversation("token", conversation.id)
    assert duplicate.id == deletion.id
    assert repository.has_active_fence(
        OWNER, "conversation", conversation.id
    )
    assert qa.get_conversation(OWNER, conversation.id) is None
    assert not qa.complete_turn(
        OWNER, pending.assistant_message.id, 0, "late", (), "none", None
    )
    assert service.request_conversation("other", conversation.id) is None

    assert worker.run_once("delete-worker")
    assert repository.get_deletion(OWNER, deletion.id).status == "completed"
    assert service.request_conversation("token", conversation.id).id == deletion.id
    assert memory.removed == []


def test_conversation_deletion_removes_only_linked_memory(deletion_parts) -> None:
    qa, repository, service, worker, memory, _, _ = deletion_parts
    conversation, _ = completed_conversation(qa, memory_id="qa-target")
    other, _ = completed_conversation(qa, memory_id="qa-other")
    deletion = service.request_conversation("token", conversation.id)
    assert worker.run_once("delete-worker")
    assert repository.get_deletion(OWNER, deletion.id).status == "completed"
    assert memory.removed == [("qa-target", "episodic")]
    assert qa.get_conversation(OWNER, other.id) is not None


def test_document_fence_cascades_related_conversations_then_document(
    deletion_parts,
) -> None:
    qa, repository, service, worker, memory, documents, sessions = deletion_parts
    first, _ = completed_conversation(qa, memory_id="qa-one")
    second, _ = completed_conversation(qa, memory_id="qa-two")
    deletion = service.request_document("token", "doc")
    assert deletion.affected_conversation_count == 2
    assert repository.has_active_fence(OWNER, "document", "doc")
    assert qa.list_conversations(OWNER).items == ()

    assert worker.run_once("delete-worker")
    completed = repository.get_deletion(OWNER, deletion.id)
    assert completed.status == "completed"
    assert documents.deleted == [(OWNER, "doc")]
    assert set(memory.removed) == {
        ("qa-one", "episodic"),
        ("qa-two", "episodic"),
    }
    assert sessions.cleared == [(OWNER, "doc")]
    assert sessions.runtime_registry.acquired == [OWNER]
    assert sessions.runtime_registry.released == [OWNER]
    assert qa.get_conversation(OWNER, first.id) is None
    assert qa.get_conversation(OWNER, second.id) is None


def test_failed_stage_keeps_active_retryable_fence(deletion_parts) -> None:
    qa, repository, service, worker, _, documents, _ = deletion_parts
    completed_conversation(qa)
    deletion = service.request_document("token", "doc")

    def fail(*args, **kwargs):
        raise OSError("raw path")

    documents.perform_document_delete = fail
    assert worker.run_once("delete-worker")
    failed = repository.get_deletion(OWNER, deletion.id)
    assert failed.status == "failed"
    assert failed.safe_error_code == "QA_DELETION_FAILED"
    assert "path" not in repr(failed)
    assert repository.has_active_fence(OWNER, "document", "doc")


def test_live_deletion_worker_terminalizes_expired_final_attempt_fence(
    deletion_parts,
    monkeypatch,
) -> None:
    qa, repository, service, _, _, documents, sessions = deletion_parts
    now = datetime(2026, 8, 27, 8, 0, tzinfo=timezone.utc)
    clock = {"value": iso(now)}
    monkeypatch.setattr(qa_deletion_module, "_utc_now", lambda: clock["value"])
    conversation = qa.create_conversation(
        OWNER, (QaDocumentCandidate("doc", "文档.pdf", OWNER),)
    )
    deletion = service.request_document("token", "doc")
    assert qa.get_conversation(OWNER, conversation.id) is None
    for attempt in range(1, 3):
        claimed = repository.claim_next_deletion(
            f"lost-worker-{attempt}", lease_seconds=30
        )
        assert claimed is not None
        repository.fail_or_retry_deletion(
            claimed.id,
            f"lost-worker-{attempt}",
            "QA_DELETION_FAILED",
            None,
        )
    final_claim = repository.claim_next_deletion(
        "lost-worker-3", lease_seconds=30
    )
    assert final_claim is not None
    assert final_claim.attempt_count == 3
    live_worker = QaDeletionWorker(
        repository,
        sessions.runtime_registry,
        documents,
        sessions,
        poll_interval=0.02,
    )

    live_worker.start()
    try:
        assert repository.get_deletion(OWNER, deletion.id).status == "running"
        clock["value"] = iso(now + timedelta(seconds=31))
        terminal = wait_for(
            lambda: (
                current
                if (
                    current := repository.get_deletion(OWNER, deletion.id)
                ).status
                == "failed"
                and current.safe_error_code == "QA_DELETION_INTERRUPTED"
                else None
            )
        )
    finally:
        live_worker.stop()

    assert terminal.attempt_count == 3
    assert not repository.has_active_fence(OWNER, "document", "doc")
    assert qa.get_conversation(OWNER, conversation.id) is not None
    assert not repository.advance_deletion(
        deletion.id, "lost-worker-3", "fenced", "qa_rows_removed"
    )
    assert not repository.complete_deletion(deletion.id, "lost-worker-3")
    with pytest.raises(QaValidationError) as lease_lost:
        repository.fail_or_retry_deletion(
            deletion.id,
            "lost-worker-3",
            "QA_DELETION_FAILED",
            None,
        )
    assert lease_lost.value.code == "QA_DELETION_LEASE_LOST"
    assert documents.deleted == []
    assert sessions.runtime_registry.acquired == []


def test_terminal_processing_failure_can_be_explicitly_requeued_and_resnapshots(
    deletion_parts,
) -> None:
    qa, repository, service, worker, _, documents, _ = deletion_parts
    original_delete = documents.perform_document_delete
    first, _ = completed_conversation(qa, memory_id="qa-first")
    deletion = service.request_document("token", "doc")

    def fail_document_delete(*args, **kwargs):
        raise PermissionError("source is temporarily locked")

    documents.perform_document_delete = fail_document_delete
    for expected_attempt in range(1, 4):
        assert worker.run_once(f"delete-worker-{expected_attempt}")
        current = repository.get_deletion(OWNER, deletion.id)
        assert current.status == "failed"
        assert current.attempt_count == expected_attempt

    with connect(qa.db_path) as conn:
        terminal = conn.execute(
            "select * from qa_deletion_fences where id = ?", (deletion.id,)
        ).fetchone()
    assert terminal["attempt_count"] == 3
    assert terminal["finished_at"] is not None
    assert terminal["safe_error_code"] == "QA_DELETION_FAILED"
    assert terminal["trace_id"] is not None
    assert not repository.has_active_fence(OWNER, "document", "doc")

    late, _ = completed_conversation(qa, memory_id="qa-late")
    assert service.request_document("other", "doc") is None
    retried = service.request_document("token", "doc")

    assert retried.id == deletion.id
    assert retried.status == "queued"
    assert retried.stage == "fenced"
    assert retried.attempt_count == 0
    assert retried.safe_error_code is None
    assert repository.has_active_fence(OWNER, "document", "doc")
    assert repository.get_deletion(OTHER, retried.id) is None
    with connect(qa.db_path) as conn:
        reset = conn.execute(
            "select * from qa_deletion_fences where id = ?", (retried.id,)
        ).fetchone()
    assert reset["created_at"] == terminal["created_at"]
    assert reset["attempt_count"] == 0
    assert reset["finished_at"] is None
    assert reset["safe_error_code"] is None
    assert reset["trace_id"] is None
    assert reset["lease_owner"] is None
    assert reset["lease_expires_at"] is None
    assert set(json.loads(reset["conversation_ids_json"])) == {first.id, late.id}
    assert set(json.loads(reset["memory_ids_json"])) == {"qa-first", "qa-late"}
    with connect(qa.db_path) as conn:
        assert conn.execute(
            """
            select count(*) from qa_deletion_fences
            where user_id = ? and target_type = 'document' and target_id = ?
              and status != 'completed'
            """,
            (OWNER, "doc"),
        ).fetchone()[0] == 1

    documents.perform_document_delete = original_delete
    assert worker.run_once("delete-worker-retry")
    completed = repository.get_deletion(OWNER, retried.id)
    assert completed.status == "completed"
    assert completed.affected_conversation_count == 2
    assert qa.get_conversation(OWNER, first.id) is None
    assert qa.get_conversation(OWNER, late.id) is None
    assert documents.deleted == [(OWNER, "doc")]


def test_document_removal_replay_completes_after_post_delete_failure(
    deletion_parts,
) -> None:
    qa, repository, service, _, memory, documents, sessions = deletion_parts
    conversation, _ = completed_conversation(qa, memory_id="qa-replay")
    deletion = service.request_document("token", "doc")

    class OneShotMigrationFailure:
        def __init__(self):
            self.conversation_calls = []
            self.document_calls = []

        def scrub_conversations(self, user_id, history, conversation_ids):
            self.conversation_calls.append((user_id, tuple(conversation_ids)))

        def scrub_document(self, user_id, history, document_id):
            self.document_calls.append((user_id, document_id))
            if len(self.document_calls) == 1:
                raise OSError("injected after physical deletion")

    migration = OneShotMigrationFailure()
    sessions.runtime_registry.runtime.history = object()
    worker = QaDeletionWorker(
        repository,
        sessions.runtime_registry,
        documents,
        sessions,
        legacy_migration=migration,
    )

    assert worker.run_once("delete-worker-1")
    failed = repository.get_deletion(OWNER, deletion.id)
    assert failed.status == "failed"
    assert failed.stage == "memory_removed"
    assert documents.deleted == [(OWNER, "doc")]
    assert "doc" not in documents.items[OWNER]
    assert sessions.cleared == []
    assert repository.has_active_fence(OWNER, "document", "doc")

    assert worker.run_once("delete-worker-2")
    completed = repository.get_deletion(OWNER, deletion.id)
    assert completed.status == "completed"
    assert completed.stage == "completed"
    assert documents.deleted == [(OWNER, "doc")]
    assert sessions.cleared == [(OWNER, "doc")]
    assert migration.document_calls == [(OWNER, "doc"), (OWNER, "doc")]
    assert memory.removed == [("qa-replay", "episodic")]
    assert qa.get_conversation(OWNER, conversation.id) is None
    assert not repository.has_active_fence(OWNER, "document", "doc")


def test_document_removal_replay_converges_real_resources_and_legacy_backup(
    deletion_parts,
    tmp_path,
) -> None:
    qa, repository, _, _, memory, _, old_sessions = deletion_parts
    storage = UserStorage(tmp_path / "data")
    paths = storage.ensure_user_dirs(OWNER)
    source = paths.documents / "doc.md"
    source.write_text("document body", encoding="utf-8")
    history = HistoryRepository(paths.history)
    history.save(
        {
            "documents": [
                {
                    "document_id": "doc",
                    "document_name": "doc.md",
                    "document_path": str(source),
                    "file_suffix": ".md",
                    "loaded_at": "2026-08-27T00:00:00Z",
                }
            ],
            "questions": [{"document_id": "doc", "question": "legacy"}],
            "notes": [],
            "sessions": [],
        }
    )
    backup_root = storage.data_root / "legacy_backups" / "migration-1"
    backup_history = backup_root / "source" / "legacy-history.json"
    backup_history.parent.mkdir(parents=True)
    HistoryRepository(backup_history).save(history.load())
    write_json_atomic(
        backup_root / "manifest.json",
        {
            "user_id": OWNER,
            "files": [
                {
                    "kind": "history",
                    "relative": "legacy-history.json",
                    "size": backup_history.stat().st_size,
                    "sha256": "before-replay",
                }
            ],
        },
    )

    class StatefulRag:
        def __init__(self):
            self.documents = {"doc"}
            self.calls = []

        def execute(self, action, *, document_id):
            self.calls.append((action, document_id))
            self.documents.discard(document_id)
            return "deleted from rag"

    rag = StatefulRag()
    lock = threading.RLock()
    runtime = old_sessions.runtime_registry.runtime
    runtime.lock = lock
    runtime.paths = paths
    runtime.history = history
    runtime.coordinator = UserMutationCoordinator(
        OWNER, lock, history, document_root=paths.documents
    )
    runtime.rag_tool = rag
    sessions = Sessions(runtime)
    document_library = DocumentLibraryService(
        sessions,
        storage,
        SimpleNamespace(
            has_active_task_for_document=lambda user_id, document_id: False
        ),
    )
    service = QaDeletionService(
        sessions, document_library, qa, repository, Wake()
    )
    delegate = QaLegacyMigrationService(qa.db_path, storage)

    class FailOnceAfterPhysicalDelete:
        def __init__(self):
            self.document_calls = 0

        def scrub_conversations(self, user_id, active_history, conversation_ids):
            return 0

        def scrub_document(self, user_id, active_history, document_id):
            self.document_calls += 1
            if self.document_calls == 1:
                raise OSError("injected after physical deletion")
            return delegate.scrub_document(user_id, active_history, document_id)

    migration = FailOnceAfterPhysicalDelete()
    worker = QaDeletionWorker(
        repository,
        sessions.runtime_registry,
        document_library,
        sessions,
        legacy_migration=migration,
    )
    conversation, _ = completed_conversation(qa, memory_id="qa-real-replay")
    deletion = service.request_document("token", "doc")

    assert worker.run_once("delete-worker-real-1")
    failed = repository.get_deletion(OWNER, deletion.id)
    assert failed.status == "failed"
    assert failed.stage == "memory_removed"
    assert rag.documents == set()
    assert rag.calls == [("delete_document", "doc")]
    assert not source.exists()
    assert history.load()["documents"] == []
    assert history.load()["questions"] == []
    assert HistoryRepository(backup_history).load()["documents"][0][
        "document_id"
    ] == "doc"
    assert repository.has_active_fence(OWNER, "document", "doc")

    assert worker.run_once("delete-worker-real-2")
    completed = repository.get_deletion(OWNER, deletion.id)
    assert completed.status == "completed"
    assert completed.stage == "completed"
    assert rag.calls == [("delete_document", "doc")]
    assert HistoryRepository(backup_history).load()["documents"] == []
    assert HistoryRepository(backup_history).load()["questions"] == []
    assert sessions.cleared == [(OWNER, "doc")]
    assert memory.removed == [("qa-real-replay", "episodic")]
    assert qa.get_conversation(OWNER, conversation.id) is None
    assert not repository.has_active_fence(OWNER, "document", "doc")


def test_document_deletion_retries_graph_cleanup_before_removing_source(deletion_parts):
    _, repository, service, worker, _, documents, _ = deletion_parts
    attempts = []

    def delete_graph(user_id, document_id):
        assert repository.has_active_fence(user_id, "document", document_id)
        assert document_id in documents.items[user_id]
        attempts.append((user_id, document_id))
        if len(attempts) == 1:
            raise OSError("graph offline")

    worker.graph_service = SimpleNamespace(delete_document=delete_graph)
    deletion = service.request_document("token", "doc")

    assert worker.run_once("delete-graph-first")
    assert repository.get_deletion(OWNER, deletion.id).status == "failed"
    assert repository.has_active_fence(OWNER, "document", "doc")
    assert documents.deleted == []

    assert worker.run_once("delete-graph-retry")
    assert repository.get_deletion(OWNER, deletion.id).status == "completed"
    assert attempts == [(OWNER, "doc"), (OWNER, "doc")]
    assert documents.deleted == [(OWNER, "doc")]
    assert documents.items[OTHER] == {"other-doc"}
