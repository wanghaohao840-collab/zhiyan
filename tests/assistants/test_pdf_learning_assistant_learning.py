from __future__ import annotations

import threading
from datetime import date
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from app.history import HistoryRepository
from app.learning import LearningService
from app.learning_generation import LearningGenerationService
from app.learning_repository import CorruptLearningDataError, LearningRepository
from assistants.pdf_learning_assistant import PDFLearningAssistant


class FakeTool:
    def __init__(self):
        self.calls = []
        self.llm = object()

    def execute(self, action, **kwargs):
        self.calls.append((action, kwargs))
        return f"{action}-ok"


def _runtime(tmp_path):
    root = tmp_path / "runtime"
    for name in ("memory", "rag", "reports", "documents"):
        (root / name).mkdir(parents=True, exist_ok=True)
    lock = threading.RLock()
    history = HistoryRepository(root / "history.json")
    rag = FakeTool()
    repository = LearningRepository(root / "learning.json", lock)
    return SimpleNamespace(
        paths=SimpleNamespace(root=root, documents=root / "documents"),
        lock=lock,
        coordinator=None,
        rag_tool=rag,
        memory_tool=FakeTool(),
        history=history,
        reports=None,
        learning_repository=repository,
        learning_service=LearningService(repository),
        learning_generation_service=LearningGenerationService(rag, rag.llm),
        import_task_service=None,
    )


def _add_document(runtime, document_id="doc-1", document_name="One.md"):
    path = runtime.paths.documents / document_name
    path.write_text("body", encoding="utf-8")
    runtime.history.update(
        lambda history: history["documents"].append(
            {
                "document_id": document_id,
                "document_name": document_name,
                "document_path": str(path),
            }
        )
    )
    return path


def _select(assistant, document_id="doc-1"):
    assistant.current_document_id = document_id
    assistant.current_document = f"/{document_id}.md"


def _card_draft(document_id="doc-1", front="Front"):
    return {
        "front": front,
        "back": "Back",
        "sources": [
            {
                "chunk_id": "chunk-1",
                "document_id": document_id,
                "document_name": "One.md",
                "page_number": None,
                "source_position": 1,
                "preview": "evidence",
            }
        ],
    }


def _exercise_draft(document_id="doc-1", prompt="Question", kind="multiple_choice"):
    draft = {
        "type": kind,
        "prompt": prompt,
        "sources": [
            {
                "chunk_id": "chunk-1",
                "document_id": document_id,
                "document_name": "One.md",
                "page_number": None,
                "source_position": 1,
                "preview": "evidence",
            }
        ],
    }
    if kind == "multiple_choice":
        draft.update(
            options=["A", "B", "C", "D"],
            correct_index=1,
            explanation="Because B is correct.",
        )
    else:
        draft.update(reference_answer="Reference", key_points=["Point"])
    return draft


def test_runtime_sessions_share_learning_data_but_not_document_selection(tmp_path):
    runtime = _runtime(tmp_path)
    _add_document(runtime, "doc-1", "One.md")
    _add_document(runtime, "doc-2", "Two.md")
    first = PDFLearningAssistant(user_id="user-1", runtime=runtime)
    second = PDFLearningAssistant(user_id="user-1", runtime=runtime)
    _select(first, "doc-1")
    _select(second, "doc-2")

    created = first.create_learning_plan(
        "Read one", date.today().isoformat(), 20
    )

    assert first.learning_service is second.learning_service
    assert first.current_document_id == "doc-1"
    assert second.current_document_id == "doc-2"
    assert first.get_learning_plans() == [created]
    assert second.get_learning_plans() == []


def test_standalone_assistant_uses_runtime_root_learning_json(tmp_path):
    assistant = PDFLearningAssistant(user_id="standalone", runtime_dir=tmp_path)

    try:
        assert assistant.learning_repository.path == tmp_path / "learning.json"
        assert assistant.learning_repository.lock is assistant._write_lock
    finally:
        assistant.close()


def test_learning_methods_reject_forged_or_stale_selection_before_services(tmp_path):
    runtime = _runtime(tmp_path)
    assistant = PDFLearningAssistant(user_id="user-1", runtime=runtime)
    _select(assistant, "forged")
    assistant.learning_service = MagicMock()
    assistant.learning_generation_service = MagicMock()

    operations = [
        assistant.get_learning_plans,
        lambda: assistant.create_learning_plan("Plan", date.today().isoformat(), 10),
        lambda: assistant.set_learning_task_completed("plan", "task", True),
        assistant.get_learning_dashboard,
        assistant.get_learning_cards,
        lambda: assistant.generate_learning_cards(3),
        lambda: assistant.review_learning_card("card", "fuzzy"),
        assistant.get_learning_exercises,
        lambda: assistant.generate_learning_exercises(3),
        lambda: assistant.answer_learning_multiple_choice("exercise", 0),
        lambda: assistant.review_learning_short_answer(
            "exercise", "fuzzy", "answer"
        ),
    ]
    for operation in operations:
        with pytest.raises(ValueError, match="current document"):
            operation()

    assistant.learning_service.list_cards.assert_not_called()
    assistant.learning_generation_service.generate_cards.assert_not_called()


def test_generate_cards_releases_lock_and_has_no_ask_memory_or_stat_side_effects(tmp_path):
    runtime = _runtime(tmp_path)
    _add_document(runtime)
    assistant = PDFLearningAssistant(user_id="user-1", runtime=runtime)
    _select(assistant)
    assistant.ask = MagicMock(side_effect=AssertionError("ask must not be called"))
    before_stats = dict(assistant.stats)

    class Generation:
        def generate_cards(self, document_id, document_name, count, existing_fronts=()):
            assert not runtime.lock._is_owned()
            assert (document_id, document_name, count) == ("doc-1", "One.md", 3)
            assert existing_fronts == []
            return [
                _card_draft(front="Front 1"),
                _card_draft(front="Front 2"),
                _card_draft(front="Front 3"),
            ]

    assistant.learning_generation_service = Generation()
    cards = assistant.generate_learning_cards(3)

    assert len(cards) == 3
    assistant.ask.assert_not_called()
    assert runtime.memory_tool.calls == []
    assert assistant.stats == before_stats


def test_generation_revalidates_document_before_atomic_save(tmp_path):
    runtime = _runtime(tmp_path)
    _add_document(runtime)
    assistant = PDFLearningAssistant(user_id="user-1", runtime=runtime)
    _select(assistant)

    class DeletingGeneration:
        def generate_cards(self, *args, **kwargs):
            runtime.history.delete_document("doc-1")
            return [_card_draft(front=f"Front {index}") for index in range(3)]

    assistant.learning_generation_service = DeletingGeneration()

    with pytest.raises(ValueError, match="current document"):
        assistant.generate_learning_cards(3)
    assert runtime.learning_service.list_cards("doc-1") == []


def test_generation_rejects_duplicate_introduced_while_llm_is_running(tmp_path):
    runtime = _runtime(tmp_path)
    _add_document(runtime)
    assistant = PDFLearningAssistant(user_id="user-1", runtime=runtime)
    _select(assistant)

    class RacingGeneration:
        def generate_cards(self, *args, **kwargs):
            runtime.learning_service.save_cards(
                "doc-1", "One.md", [_card_draft(front="Raced front")]
            )
            return [
                _card_draft(front="Fresh front 1"),
                _card_draft(front="Raced front"),
                _card_draft(front="Fresh front 2"),
            ]

    assistant.learning_generation_service = RacingGeneration()

    with pytest.raises(ValueError, match="duplicate card front"):
        assistant.generate_learning_cards(3)
    assert [item["front"] for item in runtime.learning_service.list_cards("doc-1")] == [
        "Raced front"
    ]


def test_exercise_generation_and_review_wrappers_are_document_scoped(tmp_path):
    runtime = _runtime(tmp_path)
    _add_document(runtime)
    assistant = PDFLearningAssistant(user_id="user-1", runtime=runtime)
    _select(assistant)
    assistant.ask = MagicMock(side_effect=AssertionError("ask must not be called"))
    before_stats = dict(assistant.stats)

    class Generation:
        def generate_exercises(
            self,
            document_id,
            document_name,
            count,
            kind="mixed",
            existing_prompts=(),
        ):
            assert not runtime.lock._is_owned()
            assert (document_id, document_name, count, kind) == (
                "doc-1",
                "One.md",
                3,
                "mixed",
            )
            assert existing_prompts == []
            return [
                _exercise_draft(prompt="MC question"),
                _exercise_draft(prompt="Short one", kind="short_answer"),
                _exercise_draft(prompt="Short two", kind="short_answer"),
            ]

    assistant.learning_generation_service = Generation()
    exercises = assistant.generate_learning_exercises(3)

    assert {item["id"] for item in assistant.get_learning_exercises()} == {
        item["id"] for item in exercises
    }
    assert exercises[0]["sources"][0]["preview"] == "evidence"
    multiple_choice = next(item for item in exercises if item["type"] == "multiple_choice")
    short_answer = next(item for item in exercises if item["type"] == "short_answer")
    answer = assistant.answer_learning_multiple_choice(multiple_choice["id"], 1)
    reviewed = assistant.review_learning_short_answer(
        short_answer["id"], "mastered", "My answer"
    )

    assert answer["correct"] is True
    assert reviewed["schedule"]["last_grade"] == 5
    assistant.ask.assert_not_called()
    assert runtime.memory_tool.calls == []
    assert assistant.stats == before_stats


def test_plan_dashboard_and_card_review_wrappers(tmp_path):
    runtime = _runtime(tmp_path)
    _add_document(runtime)
    assistant = PDFLearningAssistant(user_id="user-1", runtime=runtime)
    _select(assistant)

    plan = assistant.create_learning_plan("Plan", date.today().isoformat(), 15)
    completed = assistant.set_learning_task_completed(
        plan["id"], plan["tasks"][0]["id"], True
    )
    card = runtime.learning_service.save_cards(
        "doc-1", "One.md", [_card_draft()]
    )[0]
    reviewed = assistant.review_learning_card(card["id"], "fuzzy")
    dashboard = assistant.get_learning_dashboard()

    assert completed["tasks"][0]["completed"] is True
    assert reviewed["sources"] == card["sources"]
    assert reviewed["schedule"]["last_grade"] == 3
    assert dashboard["completed_tasks"] == 1


def test_cross_document_learning_item_is_rejected(tmp_path):
    runtime = _runtime(tmp_path)
    _add_document(runtime, "doc-1", "One.md")
    _add_document(runtime, "doc-2", "Two.md")
    other = runtime.learning_service.create_plan(
        "doc-2", "Two.md", "Other", date.today().isoformat(), 20
    )
    assistant = PDFLearningAssistant(user_id="user-1", runtime=runtime)
    _select(assistant, "doc-1")

    with pytest.raises(ValueError, match="not found for document"):
        assistant.set_learning_task_completed(
            other["id"], other["tasks"][0]["id"], True
        )

    other_card = runtime.learning_service.save_cards(
        "doc-2", "Two.md", [_card_draft("doc-2")]
    )[0]
    other_exercises = runtime.learning_service.save_exercises(
        "doc-2",
        "Two.md",
        [
            _exercise_draft("doc-2"),
            _exercise_draft("doc-2", "Short", "short_answer"),
        ],
    )
    with pytest.raises(ValueError, match="not found for document"):
        assistant.review_learning_card(other_card["id"], "mastered")
    with pytest.raises(ValueError, match="not found for document"):
        assistant.answer_learning_multiple_choice(other_exercises[0]["id"], 0)
    with pytest.raises(ValueError, match="not found for document"):
        assistant.review_learning_short_answer(
            other_exercises[1]["id"], "fuzzy", "Answer"
        )


def test_corrupt_learning_file_fails_with_user_readable_error(tmp_path):
    runtime = _runtime(tmp_path)
    _add_document(runtime)
    assistant = PDFLearningAssistant(user_id="user-1", runtime=runtime)
    _select(assistant)
    runtime.learning_repository.path.write_text("{broken", encoding="utf-8")

    with pytest.raises(CorruptLearningDataError, match="corrupt and was not modified"):
        assistant.get_learning_cards()


def test_stale_session_filters_orphaned_learning_items(tmp_path):
    runtime = _runtime(tmp_path)
    _add_document(runtime)
    runtime.learning_service.create_plan(
        "doc-1", "One.md", "Orphan", date.today().isoformat(), 20
    )
    assistant = PDFLearningAssistant(user_id="user-1", runtime=runtime)
    _select(assistant)
    runtime.history.delete_document("doc-1")

    with pytest.raises(ValueError, match="current document"):
        assistant.get_learning_plans()
