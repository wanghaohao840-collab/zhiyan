from app.database import initialize_database
from app.runtime import UserRuntimeRegistry
from app.storage import UserStorage


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


def test_runtime_lock_guards_history_writes(tmp_path):
    db_path = tmp_path / "app.db"
    initialize_database(db_path)
    runtime = UserRuntimeRegistry(db_path=db_path, storage=UserStorage(tmp_path / "data")).get_or_create("user-1")

    with runtime.lock:
        runtime.history.add_note({"note": "inside lock", "concept": "runtime"})

    assert runtime.history.load()["notes"][0]["note"] == "inside lock"


def test_user_runtime_shares_learning_services_and_user_lock(tmp_path):
    db_path = tmp_path / "app.db"
    initialize_database(db_path)
    registry = UserRuntimeRegistry(db_path=db_path, storage=UserStorage(tmp_path / "data"))

    runtime = registry.get_or_create("user-1")

    assert runtime.learning_repository.path == runtime.paths.root / "learning.json"
    assert runtime.learning_repository.lock is runtime.lock
    assert runtime.learning_service.repository is runtime.learning_repository
    assert runtime.learning_generation_service.rag_tool is runtime.rag_tool
    assert runtime.learning_generation_service.llm is runtime.rag_tool.llm
