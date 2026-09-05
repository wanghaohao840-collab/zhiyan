"""Tests for the per-user mutation coordination contract.

Covers the invariants listed in ``app/coordination.py`` and the acceptance
criteria in the ``01-user-mutation-coordination`` task packet.
"""

from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from app.coordination import UserMutationCoordinator
from app.history import CorruptHistoryError, HistoryRepository
from app.learning import LearningService
from app.learning_generation import LearningGenerationService
from app.learning_repository import LearningRepository
from assistants.pdf_learning_assistant import PDFLearningAssistant


# ── helpers ────────────────────────────────────────────────────────────────

class FakeTool:
    """Records calls; returns a configurable result per action."""

    def __init__(self, results=None):
        self.calls = []
        self._results = results or {}

    def execute(self, action, **kwargs):
        self.calls.append((action, kwargs))
        result = self._results.get(action)
        if isinstance(result, Exception):
            raise result
        return result or f"{action}-ok"


class BlockingRAGTool(FakeTool):
    """RAG tool whose ``ask`` blocks until an event is set."""

    def __init__(self, block_event: threading.Event):
        super().__init__()
        self.block_event = block_event

    def execute(self, action, **kwargs):
        self.calls.append((action, kwargs))
        if action == "ask":
            self.block_event.wait(timeout=5)
        return f"{action}-ok"


def _make_runtime(tmp_path, user_id="user-1"):
    """Build a minimal runtime namespace with a coordinator."""
    storage_root = tmp_path / "data"
    storage_root.mkdir(parents=True, exist_ok=True)
    user_root = storage_root / "users" / user_id
    for sub in ("documents", "reports", "memory", "rag"):
        (user_root / sub).mkdir(parents=True, exist_ok=True)
    history_path = user_root / "history.json"

    lock = threading.RLock()
    history_repo = HistoryRepository(history_path)
    history_repo.save({
        "documents": [],
        "questions": [],
        "notes": [],
        "sessions": [],
    })
    coordinator = UserMutationCoordinator(
        user_id=user_id,
        lock=lock,
        history=history_repo,
        document_root=user_root / "documents",
    )
    rag_tool = FakeTool()
    learning_repository = LearningRepository(user_root / "learning.json", lock)
    return SimpleNamespace(
        paths=SimpleNamespace(
            root=user_root,
            documents=user_root / "documents",
            history=history_path,
            rag_cache=user_root / "rag" / "rag_cache.json",
            memory_snapshot=user_root / "memory" / "memory_snapshot.json",
        ),
        lock=lock,
        coordinator=coordinator,
        history=history_repo,
        memory_tool=FakeTool(),
        rag_tool=rag_tool,
        reports=None,
        learning_repository=learning_repository,
        learning_service=LearningService(learning_repository),
        learning_generation_service=LearningGenerationService(rag_tool, object()),
    ), user_id


# ── coordinator unit tests ────────────────────────────────────────────────

class TestCoordinatorContract:
    def test_update_history_does_fresh_merge(self, tmp_path):
        """A mutation reloads the latest persisted snapshot first."""
        runtime, user_id = _make_runtime(tmp_path)
        coord = runtime.coordinator

        # Pre-populate via a separate HistoryRepository so coord sees it.
        direct = HistoryRepository(runtime.paths.history)
        direct.update(lambda h: h["notes"].append({"note": "before"}))
        assert len(direct.load()["notes"]) == 1

        # Update through coordinator — must load the latest snapshot.
        coord.update_history(lambda h: h["notes"].append({"note": "after"}))
        assert len(coord.load_history()["notes"]) == 2

    def test_lock_serializes_same_user_writes(self, tmp_path):
        """Concurrent mutations do not lose entries."""
        runtime, user_id = _make_runtime(tmp_path)
        coord = runtime.coordinator
        errors = []

        def append_note(label):
            try:
                coord.update_history(lambda h: h["notes"].append({"note": label}))
            except Exception as exc:
                errors.append(exc)

        with ThreadPoolExecutor(max_workers=4) as pool:
            list(pool.map(append_note, [f"n{i}" for i in range(20)]))

        assert not errors
        assert len(coord.load_history()["notes"]) == 20

    def test_different_users_have_independent_locks(self, tmp_path):
        """User A holding its lock does not block user B."""
        rt_a, _ = _make_runtime(tmp_path, "user-a")
        rt_b, _ = _make_runtime(tmp_path, "user-b")
        ready = threading.Event()
        entered_b = threading.Event()

        def hold_lock_a():
            with rt_a.lock:
                ready.set()
                time.sleep(0.3)

        def enter_lock_b():
            ready.wait()
            with rt_b.lock:
                entered_b.set()

        with ThreadPoolExecutor(max_workers=2) as pool:
            pool.submit(hold_lock_a)
            pool.submit(enter_lock_b)

        assert entered_b.wait(3), "User B should acquire its own lock promptly"

    def test_safe_unlink_rejects_path_outside_root(self, tmp_path):
        """safe_unlink raises ValueError for paths outside document_root."""
        rt_a, _ = _make_runtime(tmp_path, "user-a")
        coord = rt_a.coordinator

        # A path inside root — create it so unlink can succeed
        inside = rt_a.paths.documents / "keep.md"
        inside.write_text("ok", encoding="utf-8")
        coord.safe_unlink(inside)
        assert not inside.exists()

        # A path outside root
        outside = tmp_path / "outside.txt"
        outside.write_text("no", encoding="utf-8")
        with pytest.raises(ValueError, match="outside user root"):
            coord.safe_unlink(outside)
        assert outside.exists()  # untouched

    def test_compensate_rag_add_deletes_document_from_rag(self, tmp_path):
        """Best-effort compensation calls rag.delete_document."""
        rt_a, _ = _make_runtime(tmp_path, "user-a")
        coord = rt_a.coordinator
        rag = FakeTool()
        coord.compensate_rag_add(rag, "doc-to-undo")
        assert ("delete_document", {"document_id": "doc-to-undo"}) in [
            (a, k) for a, k in rag.calls
        ]

    def test_compensate_rag_add_swallows_errors(self, tmp_path):
        """Compensation logs but does not re-raise rag errors."""
        rt_a, _ = _make_runtime(tmp_path, "user-a")
        coord = rt_a.coordinator
        rag = FakeTool({"delete_document": RuntimeError("Qdrant down")})
        # Must not raise
        coord.compensate_rag_add(rag, "doc-x")
        assert len(rag.calls) == 1


# ── Assistant-level tests ──────────────────────────────────────────────────

class TestAssistantCoordination:
    def test_concurrent_notes_merge_without_loss(self, tmp_path):
        """Packet acceptance: two sessions' notes are both retained."""
        runtime, user_id = _make_runtime(tmp_path)
        first = PDFLearningAssistant(user_id=user_id, runtime=runtime)
        second = PDFLearningAssistant(user_id=user_id, runtime=runtime)

        with ThreadPoolExecutor(max_workers=2) as pool:
            list(pool.map(
                lambda args: args[0].add_note(args[1]),
                [(first, "first-note"), (second, "second-note")],
            ))

        notes = runtime.history.load()["notes"]
        assert {item["note"] for item in notes} == {"first-note", "second-note"}

    def test_import_failure_leaves_history_untouched(self, tmp_path):
        """RAG failure on import does not add a History entry."""
        runtime, user_id = _make_runtime(tmp_path)
        rag = FakeTool({"add_document": RuntimeError("import failed")})
        runtime.rag_tool = rag

        assistant = PDFLearningAssistant(user_id=user_id, runtime=runtime)
        src = runtime.paths.documents / "test.md"
        src.write_text("body", encoding="utf-8")

        with pytest.raises(RuntimeError, match="import failed"):
            assistant.load_document(str(src))

        assert runtime.history.load()["documents"] == []

    def test_import_compensates_rag_on_history_failure(self, tmp_path):
        """When History update fails after RAG success, coordinator tries to
        undo the RAG mutation."""
        runtime, user_id = _make_runtime(tmp_path)
        rag = FakeTool()
        runtime.rag_tool = rag
        assistant = PDFLearningAssistant(user_id=user_id, runtime=runtime)
        src = runtime.paths.documents / "test.md"
        src.write_text("body", encoding="utf-8")

        # Corrupt history AFTER construction so only _update_history fails.
        runtime.paths.history.write_text("{not-json", encoding="utf-8")

        with pytest.raises(CorruptHistoryError):
            assistant.load_document(str(src))

        # The corrupt history file must remain unmodified.
        assert runtime.paths.history.read_text(encoding="utf-8") == "{not-json"

        # Compensation should have called delete_document on RAG.
        delete_calls = [
            (a, k) for a, k in rag.calls if a == "delete_document"
        ]
        assert len(delete_calls) >= 1, (
            "coordinator must compensate RAG add after history failure"
        )

    def test_ask_generates_outside_lock(self, tmp_path):
        """LLM generation (rag ask) runs without holding the write lock."""
        block_event = threading.Event()
        rag = BlockingRAGTool(block_event)
        runtime, user_id = _make_runtime(tmp_path)
        runtime.rag_tool = rag

        assistant = PDFLearningAssistant(user_id=user_id, runtime=runtime)

        # Seed a current document so ask doesn't bail early.
        runtime.history.update(
            lambda h: h["documents"].append({
                "document_id": "doc-1",
                "document_name": "one.md",
                "document_path": "/does/not/matter",
            })
        )
        assistant.current_document_id = "doc-1"
        assistant.current_document = "/does/not/matter"
        assistant.history = assistant._load_history()

        note_committed = threading.Event()

        def ask_blocking():
            assistant.ask("question?")

        def commit_note():
            # This should succeed while ask is blocked inside rag_tool.
            assistant.add_note("note-during-ask")
            note_committed.set()

        with ThreadPoolExecutor(max_workers=2) as pool:
            ask_future = pool.submit(ask_blocking)
            # Give ask a moment to enter the blocking rag call.
            time.sleep(0.3)
            pool.submit(commit_note)

            block_event.set()  # unblock the LLM
            ask_future.result(timeout=5)

        assert note_committed.wait(3), (
            "note must commit while LLM is in-flight (lock not held)"
        )

    def test_delete_document_unlinks_source_inside_user_root(self, tmp_path):
        """Delete removes the original source file under user root."""
        runtime, user_id = _make_runtime(tmp_path)
        src = runtime.paths.documents / "doc-1.md"
        src.write_text("content", encoding="utf-8")
        runtime.history.save({
            "documents": [{"document_id": "doc-1", "document_path": str(src)}],
            "questions": [],
            "notes": [],
            "sessions": [],
        })

        assistant = PDFLearningAssistant(user_id=user_id, runtime=runtime)
        assistant.current_document_id = "doc-1"
        assistant.current_document = str(src)
        assistant.delete_current_document()

        assert not src.exists()
        assert runtime.history.load()["documents"] == []

    def test_delete_preserves_out_of_root_source_and_reports_partial(self, tmp_path):
        """An out-of-root History path survives delete and the result notes it."""
        runtime, user_id = _make_runtime(tmp_path)
        out_of_root = tmp_path / "outside" / "doc-1.md"
        out_of_root.parent.mkdir(parents=True, exist_ok=True)
        out_of_root.write_text("do-not-delete", encoding="utf-8")
        runtime.history.save({
            "documents": [{"document_id": "doc-1", "document_path": str(out_of_root)}],
            "questions": [],
            "notes": [],
            "sessions": [],
        })

        assistant = PDFLearningAssistant(user_id=user_id, runtime=runtime)
        assistant.current_document_id = "doc-1"
        assistant.current_document = str(out_of_root)
        result = assistant.delete_current_document()

        # The file lives outside the user document root — must survive.
        assert out_of_root.exists(), "out-of-root file must not be deleted"
        assert out_of_root.read_text(encoding="utf-8") == "do-not-delete"
        # History cleaned up even though the source unlink was skipped.
        assert runtime.history.load()["documents"] == []
        # Must not claim full success.
        assert "outside user root" in result.lower()

    def test_clear_preserves_out_of_root_source_and_reports_partial(self, tmp_path):
        """An out-of-root History path survives clear and the result notes it."""
        runtime, user_id = _make_runtime(tmp_path)
        out_of_root = tmp_path / "outside" / "doc-2.md"
        out_of_root.parent.mkdir(parents=True, exist_ok=True)
        out_of_root.write_text("do-not-delete", encoding="utf-8")
        runtime.history.save({
            "documents": [{"document_id": "doc-2", "document_path": str(out_of_root)}],
            "questions": [],
            "notes": [],
            "sessions": [],
        })

        assistant = PDFLearningAssistant(user_id=user_id, runtime=runtime)
        assistant.current_document_id = "doc-2"
        result = assistant.clear_all_documents()

        assert out_of_root.exists(), "out-of-root file must not be deleted"
        assert out_of_root.read_text(encoding="utf-8") == "do-not-delete"
        assert runtime.history.load()["documents"] == []
        assert "outside user root" in result.lower()

    def test_clear_documents_retains_notes(self, tmp_path):
        """Clear removes documents and questions but keeps notes."""
        runtime, user_id = _make_runtime(tmp_path)
        src = runtime.paths.documents / "doc-1.md"
        src.write_text("content", encoding="utf-8")
        runtime.history.save({
            "documents": [{"document_id": "doc-1", "document_path": str(src)}],
            "questions": [{"q": "Q", "document_id": "doc-1"}],
            "notes": [{"note": "keep-me"}],
            "sessions": [],
        })

        assistant = PDFLearningAssistant(user_id=user_id, runtime=runtime)
        assistant.current_document_id = "doc-1"
        assistant.clear_all_documents()

        loaded = runtime.history.load()
        assert loaded["documents"] == []
        assert loaded["questions"] == []
        assert loaded["notes"] == [{"note": "keep-me"}]
        assert not src.exists()

    def test_delete_document_cascades_learning_records(self, tmp_path):
        runtime, user_id = _make_runtime(tmp_path)
        runtime.history.update(lambda history: history["documents"].append({
            "document_id": "doc-1",
            "document_name": "One.md",
            "document_path": str(runtime.paths.documents / "missing.md"),
        }))
        assistant = PDFLearningAssistant(user_id=user_id, runtime=runtime)
        assistant.current_document_id = "doc-1"
        runtime.learning_service.create_plan(
            "doc-1", "One.md", "Delete me", date.today().isoformat(), 10
        )

        assistant.delete_current_document()
        assistant.current_document_id = "doc-1"
        second_result = assistant.delete_current_document()

        assert runtime.learning_repository.load()["plans"] == []
        assert "history synchronized" in second_result.lower()

    def test_delete_reports_partial_failure_when_learning_cleanup_fails(self, tmp_path):
        runtime, user_id = _make_runtime(tmp_path)
        runtime.learning_service = MagicMock()
        runtime.learning_service.delete_document.side_effect = RuntimeError("disk full")
        runtime.history.update(lambda history: history["documents"].append({
            "document_id": "doc-1",
            "document_name": "One.md",
            "document_path": str(runtime.paths.documents / "missing.md"),
        }))
        assistant = PDFLearningAssistant(user_id=user_id, runtime=runtime)
        assistant.current_document_id = "doc-1"

        result = assistant.delete_current_document()

        assert runtime.history.load()["documents"] == []
        assert "partial failure" in result.lower()
        assert "learning cleanup" in result.lower()

    def test_clear_reports_partial_failure_when_learning_cleanup_fails(self, tmp_path):
        runtime, user_id = _make_runtime(tmp_path)
        runtime.learning_service = MagicMock()
        runtime.learning_service.clear_all.side_effect = RuntimeError("disk full")
        runtime.learning_repository = MagicMock()
        runtime.learning_generation_service = MagicMock()
        runtime.history.update(lambda history: history["documents"].append({
            "document_id": "doc-1",
            "document_name": "One.md",
            "document_path": str(runtime.paths.documents / "missing.md"),
        }))
        assistant = PDFLearningAssistant(user_id=user_id, runtime=runtime)
        assistant.current_document_id = "doc-1"

        result = assistant.clear_all_documents()

        assert runtime.history.load()["documents"] == []
        assert "partial failure" in result.lower()
        assert "learning cleanup" in result.lower()

    def test_clear_cascades_all_learning_records_and_is_idempotent(self, tmp_path):
        runtime, user_id = _make_runtime(tmp_path)
        runtime.history.update(lambda history: history["documents"].append({
            "document_id": "doc-1",
            "document_name": "One.md",
            "document_path": str(runtime.paths.documents / "missing.md"),
        }))
        runtime.learning_service.create_plan(
            "doc-1", "One.md", "Clear me", date.today().isoformat(), 10
        )
        assistant = PDFLearningAssistant(user_id=user_id, runtime=runtime)
        assistant.current_document_id = "doc-1"

        assistant.clear_all_documents()
        second_result = assistant.clear_all_documents()

        assert runtime.learning_repository.load()["plans"] == []
        assert "history synchronized" in second_result.lower()

    def test_active_import_guards_do_not_clean_learning_data(self, tmp_path):
        runtime, user_id = _make_runtime(tmp_path)
        runtime.import_task_service = SimpleNamespace(
            has_active_task_for_document=MagicMock(return_value=True),
            has_active_tasks=MagicMock(return_value=True),
        )
        runtime.learning_service = MagicMock()
        runtime.history.update(lambda history: history["documents"].append({
            "document_id": "doc-1",
            "document_name": "One.md",
            "document_path": str(runtime.paths.documents / "missing.md"),
        }))
        assistant = PDFLearningAssistant(user_id=user_id, runtime=runtime)
        assistant.current_document_id = "doc-1"

        delete_result = assistant.delete_current_document()
        clear_result = assistant.clear_all_documents()

        assert "import is active" in delete_result
        assert "imports are active" in clear_result
        runtime.learning_service.delete_document.assert_not_called()
        runtime.learning_service.clear_all.assert_not_called()

    def test_structured_question_scope_committed_after_generation(self, tmp_path):
        """After ask(), the history question record carries document_ids,
        document_names, and mode."""
        runtime, user_id = _make_runtime(tmp_path)
        runtime.history.update(lambda h: h["documents"].append({
            "document_id": "doc-a",
            "document_name": "Alpha.md",
            "document_path": "/f/a.md",
        }))

        assistant = PDFLearningAssistant(user_id=user_id, runtime=runtime)
        assistant.current_document_id = "doc-a"
        assistant.current_document = "/f/a.md"
        assistant.history = assistant._load_history()

        assistant.ask("hello", selected_documents=["Alpha.md | doc-a"], mode="summary")

        questions = runtime.history.load()["questions"]
        assert len(questions) == 1
        assert questions[0]["document_ids"] == ["doc-a"]
        assert questions[0]["document_names"] == ["Alpha.md"]
        assert questions[0]["mode"] == "summary"
