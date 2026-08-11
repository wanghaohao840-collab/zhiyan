import json

import pytest

from app.storage import UnsafePathError, UserStorage, read_json, write_json_atomic


USER_ID = "11111111-1111-1111-1111-111111111111"
DOC_ID = "22222222-2222-2222-2222-222222222222"


def test_ensure_user_dirs_creates_expected_layout(tmp_path):
    storage = UserStorage(tmp_path / "data")

    paths = storage.ensure_user_dirs(USER_ID)

    assert paths.root == tmp_path / "data" / "users" / USER_ID
    assert paths.documents.is_dir()
    assert paths.rag_cache == paths.root / "rag" / "rag_cache.json"
    assert paths.history == paths.root / "history.json"
    assert paths.learning == paths.root / "learning.json"
    assert paths.memory_snapshot == paths.root / "memory" / "memories.json"
    assert paths.reports.is_dir()


def test_document_path_uses_uuid_name_and_validated_suffix(tmp_path):
    storage = UserStorage(tmp_path / "data")

    path = storage.document_path(USER_ID, DOC_ID, ".PDF")

    assert path.name == f"{DOC_ID}.pdf"
    assert path.parent.name == "documents"


def test_rejects_path_traversal(tmp_path):
    storage = UserStorage(tmp_path / "data")
    storage.ensure_user_dirs(USER_ID)

    with pytest.raises(UnsafePathError):
        storage.assert_within_user(
            USER_ID,
            tmp_path / "data" / "users" / USER_ID / ".." / "other",
        )


def test_rejects_unsupported_suffix(tmp_path):
    storage = UserStorage(tmp_path / "data")

    with pytest.raises(ValueError, match="Unsupported document type"):
        storage.validate_suffix(".exe")


def test_atomic_json_round_trip(tmp_path):
    target = tmp_path / "state.json"

    write_json_atomic(target, {"ok": True, "items": [1, 2, 3]})

    assert json.loads(target.read_text(encoding="utf-8")) == {
        "ok": True,
        "items": [1, 2, 3],
    }
    assert read_json(target, default={}) == {"ok": True, "items": [1, 2, 3]}
    assert read_json(tmp_path / "missing.json", default={"empty": True}) == {
        "empty": True
    }
