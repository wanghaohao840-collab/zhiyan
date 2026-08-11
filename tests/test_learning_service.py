from __future__ import annotations

from copy import deepcopy
from datetime import date, datetime, timedelta, timezone
from threading import RLock

import pytest

from app.learning import LearningService
from app.learning_repository import EMPTY_LEARNING, LearningRepository


NOW = datetime(2026, 8, 11, 9, 30, tzinfo=timezone.utc)
TODAY = date(2026, 8, 11)


@pytest.fixture
def repository(tmp_path):
    return LearningRepository(tmp_path / "learning.json", RLock())


@pytest.fixture
def service(repository):
    return LearningService(repository, now=lambda: NOW, today=lambda: TODAY)


def source(document_id="doc-a", document_name="Alpha", **extra):
    return {
        "chunk_id": "chunk-1",
        "document_id": document_id,
        "document_name": document_name,
        "page_number": 2,
        "source_position": None,
        "preview": "Grounded source",
        **extra,
    }


def card_draft(front="Question", document_id="doc-a", **overrides):
    draft = {
        "front": front,
        "back": "Answer",
        "sources": [source(document_id=document_id)],
    }
    draft.update(overrides)
    return draft


def mcq_draft(prompt="Choose one", document_id="doc-a", **overrides):
    draft = {
        "type": "multiple_choice",
        "prompt": prompt,
        "options": ["A", "B", "C", "D"],
        "correct_index": 2,
        "explanation": "C is grounded in the source.",
        "sources": [source(document_id=document_id)],
    }
    draft.update(overrides)
    return draft


def short_draft(prompt="Explain it", document_id="doc-a", **overrides):
    draft = {
        "type": "short_answer",
        "prompt": prompt,
        "reference_answer": "A grounded answer.",
        "key_points": ["first", "second"],
        "sources": [source(document_id=document_id)],
    }
    draft.update(overrides)
    return draft


@pytest.mark.parametrize(
    ("days", "expected_phases"),
    [
        (1, ["review"]),
        (2, ["reading", "review"]),
        (3, ["reading", "cards", "review"]),
        (4, ["reading", "cards", "exercises", "review"]),
        (5, ["reading", "cards", "cards", "exercises", "review"]),
        (
            10,
            [
                "reading",
                "reading",
                "reading",
                "cards",
                "cards",
                "cards",
                "exercises",
                "exercises",
                "review",
                "review",
            ],
        ),
        (
            12,
            [
                "reading",
                "reading",
                "reading",
                "cards",
                "cards",
                "cards",
                "cards",
                "exercises",
                "exercises",
                "exercises",
                "review",
                "review",
            ],
        ),
    ],
)
def test_create_plan_allocates_inclusive_phase_days(service, days, expected_phases):
    target = TODAY + timedelta(days=days - 1)

    plan = service.create_plan("doc-a", "Alpha", "Learn Alpha", target.isoformat(), 25)

    assert [task["phase"] for task in plan["tasks"]] == expected_phases
    assert [task["due_date"] for task in plan["tasks"]] == [
        (TODAY + timedelta(days=offset)).isoformat() for offset in range(days)
    ]
    assert {task["duration_minutes"] for task in plan["tasks"]} == {25}
    assert all(task["completed"] is False and task["completed_at"] is None for task in plan["tasks"])
    assert plan["target_date"] == target.isoformat()
    assert plan["status"] == "active"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("document_id", " "),
        ("document_name", ""),
        ("title", None),
        ("title", " "),
        ("target_date", "2026-08-10"),
        ("target_date", "2027-08-11"),
        ("target_date", "not-a-date"),
        ("daily_minutes", 4),
        ("daily_minutes", 481),
        ("daily_minutes", 1.5),
        ("daily_minutes", True),
        ("title", "x" * 101),
    ],
)
def test_create_plan_validates_boundaries(service, field, value):
    arguments = {
        "document_id": "doc-a",
        "document_name": "Alpha",
        "title": "Learn Alpha",
        "target_date": TODAY.isoformat(),
        "daily_minutes": 20,
    }
    arguments[field] = value

    with pytest.raises(ValueError):
        service.create_plan(**arguments)


@pytest.mark.parametrize(
    ("days_after_today", "daily_minutes", "raw_title", "expected_title"),
    [(0, 5, "  x  ", "x"), (364, 480, f"  {'x' * 100}  ", "x" * 100)],
)
def test_create_plan_accepts_exact_date_and_minutes_boundaries(
    service, days_after_today, daily_minutes, raw_title, expected_title
):
    target = TODAY + timedelta(days=days_after_today)

    plan = service.create_plan(
        "doc-a", "Alpha", raw_title, target.isoformat(), daily_minutes
    )

    assert plan["title"] == expected_title
    assert plan["daily_minutes"] == daily_minutes
    assert plan["target_date"] == target.isoformat()


def test_list_plans_is_sorted_and_defensive(service):
    second = service.create_plan("doc-a", "Alpha", "Second", TODAY.isoformat(), 10)
    first = service.create_plan("doc-a", "Alpha", "First", TODAY.isoformat(), 10)

    plans = service.list_plans("doc-a")
    assert [plan["id"] for plan in plans] == sorted([second["id"], first["id"]])

    plans[0]["title"] = "mutated"
    assert all(plan["title"] != "mutated" for plan in service.list_plans("doc-a"))


def test_task_completion_updates_task_timestamp_and_plan_status(service):
    plan = service.create_plan("doc-a", "Alpha", "One day", TODAY.isoformat(), 15)
    task_id = plan["tasks"][0]["id"]

    completed = service.set_task_completed("doc-a", plan["id"], task_id, True)
    assert completed["tasks"][0]["completed"] is True
    assert completed["tasks"][0]["completed_at"] == NOW.isoformat()
    assert completed["status"] == "completed"
    assert completed["updated_at"] == NOW.isoformat()

    reopened = service.set_task_completed("doc-a", plan["id"], task_id, False)
    assert reopened["tasks"][0]["completed"] is False
    assert reopened["tasks"][0]["completed_at"] is None
    assert reopened["status"] == "active"


def test_task_completion_rejects_bool_and_cross_document_opaque_ids(service):
    plan = service.create_plan("doc-a", "Alpha", "One day", TODAY.isoformat(), 15)

    with pytest.raises(ValueError):
        service.set_task_completed("doc-a", plan["id"], plan["tasks"][0]["id"], 1)
    with pytest.raises(ValueError):
        service.set_task_completed("doc-b", plan["id"], plan["tasks"][0]["id"], True)
    with pytest.raises(ValueError):
        service.set_task_completed("doc-a", plan["id"], "other-task", True)


def test_new_cards_and_exercises_are_immediately_due_and_preserve_sources(service):
    grounded_source = source(citation_id="citation-7")
    cards = service.save_cards(
        "doc-a", "Alpha", [card_draft(sources=[grounded_source])]
    )
    exercises = service.save_exercises(
        "doc-a", "Alpha", [mcq_draft(), short_draft(prompt="Short")]
    )

    assert cards[0]["sources"] == [grounded_source]
    for item in cards + exercises:
        assert item["schedule"] == {
            "repetitions": 0,
            "interval_days": 0,
            "ease_factor": 2.5,
            "due_at": NOW.isoformat(),
            "last_reviewed_at": None,
            "last_grade": None,
        }
    mcq = next(item for item in exercises if item["type"] == "multiple_choice")
    short = next(item for item in exercises if item["type"] == "short_answer")
    assert mcq["reference_answer"] is None
    assert mcq["key_points"] == []
    assert short["options"] == []
    assert short["correct_index"] is None
    assert short["explanation"] is None


@pytest.mark.parametrize(
    "draft",
    [
        {"front": "Q", "back": "A"},
        card_draft(front=" "),
        card_draft(back=""),
        card_draft(sources=[]),
        card_draft(sources=[source(document_id="doc-b")]),
    ],
)
def test_save_cards_validates_required_grounded_fields_atomically(service, repository, draft):
    before = repository.load()
    with pytest.raises(ValueError):
        service.save_cards("doc-a", "Alpha", [card_draft("Valid"), draft])
    assert repository.load() == before


@pytest.mark.parametrize(
    "draft",
    [
        mcq_draft(options=["A", "B", "C"]),
        mcq_draft(options=["A", " a ", "C", "D"]),
        mcq_draft(correct_index=True),
        mcq_draft(correct_index=4),
        mcq_draft(explanation=" "),
        short_draft(reference_answer=""),
        short_draft(key_points=[]),
        short_draft(key_points=["same", "same"]),
        short_draft(sources=[source(document_id="doc-b")]),
        {"type": "unknown", "prompt": "P", "sources": [source()]},
    ],
)
def test_save_exercises_validates_type_specific_fields_atomically(service, repository, draft):
    before = repository.load()
    with pytest.raises(ValueError):
        service.save_exercises("doc-a", "Alpha", [mcq_draft("Valid"), draft])
    assert repository.load() == before


def test_save_batches_reject_normalized_duplicates_against_existing_and_batch(service, repository):
    service.save_cards("doc-a", "Alpha", [card_draft("Caf\u00e9  Facts")])
    service.save_exercises("doc-a", "Alpha", [mcq_draft("Prior Prompt")])
    before = repository.load()

    with pytest.raises(ValueError, match="duplicate"):
        service.save_cards("doc-a", "Alpha", [card_draft("unique"), card_draft(" CAFE\u0301 facts ")])
    assert repository.load() == before

    with pytest.raises(ValueError, match="duplicate"):
        service.save_exercises(
            "doc-a", "Alpha", [short_draft("New  Prompt"), short_draft(" new prompt ")]
        )
    assert repository.load() == before


def test_duplicate_normalization_is_scoped_to_document(service):
    service.save_cards("doc-a", "Alpha", [card_draft("Same")])
    saved = service.save_cards(
        "doc-b", "Beta", [card_draft(" same ", document_id="doc-b")]
    )
    assert len(saved) == 1


def test_existing_texts_and_lists_are_document_scoped_sorted_and_defensive(service):
    cards = service.save_cards("doc-a", "Alpha", [card_draft("B"), card_draft("A")])
    service.save_cards("doc-b", "Beta", [card_draft("Hidden", document_id="doc-b")])
    exercises = service.save_exercises(
        "doc-a", "Alpha", [short_draft("B prompt"), mcq_draft("A prompt")]
    )
    service.save_exercises(
        "doc-b", "Beta", [mcq_draft("Hidden", document_id="doc-b")]
    )

    assert service.existing_card_fronts("doc-a") == [
        card["front"] for card in service.list_cards("doc-a")
    ]
    assert service.existing_exercise_prompts("doc-a") == [
        item["prompt"] for item in service.list_exercises("doc-a")
    ]
    listed_cards = service.list_cards("doc-a")
    listed_exercises = service.list_exercises("doc-a")
    listed_cards[0]["front"] = "changed"
    listed_exercises[0]["prompt"] = "changed"
    assert all(item["front"] != "changed" for item in service.list_cards("doc-a"))
    assert all(item["prompt"] != "changed" for item in service.list_exercises("doc-a"))


def test_card_sm2_transitions_and_review_log_content(service, repository):
    card = service.save_cards("doc-a", "Alpha", [card_draft()])[0]

    first = service.review_card("doc-a", card["id"], "mastered")
    assert first["schedule"] == {
        "repetitions": 1,
        "interval_days": 1,
        "ease_factor": pytest.approx(2.6),
        "due_at": (NOW + timedelta(days=1)).isoformat(),
        "last_reviewed_at": NOW.isoformat(),
        "last_grade": 5,
    }
    second = service.review_card("doc-a", card["id"], "mastered")
    assert second["schedule"]["repetitions"] == 2
    assert second["schedule"]["interval_days"] == 6
    assert second["schedule"]["ease_factor"] == pytest.approx(2.7)
    third = service.review_card("doc-a", card["id"], "fuzzy")
    assert third["schedule"]["repetitions"] == 3
    assert third["schedule"]["interval_days"] == 15
    assert third["schedule"]["ease_factor"] == pytest.approx(2.56)

    log = repository.load()["review_logs"][-1]
    assert log["item_type"] == "card"
    assert log["item_id"] == card["id"]
    assert log["document_id"] == "doc-a"
    assert log["rating"] == "fuzzy"
    assert log["grade"] == 3
    assert log["result"] == "fuzzy"
    assert log["reviewed_at"] == NOW.isoformat()
    assert log["previous_due_at"] == second["schedule"]["due_at"]
    assert log["next_due_at"] == third["schedule"]["due_at"]
    assert log["old_schedule"] == second["schedule"]
    assert log["new_schedule"] == third["schedule"]


def test_sm2_failure_resets_repetitions_and_enforces_ease_floor(service):
    card = service.save_cards("doc-a", "Alpha", [card_draft()])[0]
    service.review_card("doc-a", card["id"], "mastered")
    service.review_card("doc-a", card["id"], "mastered")

    failed = service.review_card("doc-a", card["id"], "forgot")
    assert failed["schedule"]["repetitions"] == 0
    assert failed["schedule"]["interval_days"] == 1
    for _ in range(10):
        failed = service.review_card("doc-a", card["id"], "forgot")
    assert failed["schedule"]["ease_factor"] == 1.3


@pytest.mark.parametrize("rating", [0, True, "correct", "FORGOT", ""])
def test_card_review_rejects_invalid_ratings(service, rating):
    card = service.save_cards("doc-a", "Alpha", [card_draft()])[0]
    with pytest.raises(ValueError):
        service.review_card("doc-a", card["id"], rating)


def test_mcq_grades_correct_and_wrong_answers_and_logs_automatic_result(service, repository):
    correct_item, wrong_item = service.save_exercises(
        "doc-a", "Alpha", [mcq_draft("Correct"), mcq_draft("Wrong")]
    )

    correct = service.answer_multiple_choice("doc-a", correct_item["id"], 2)
    wrong = service.answer_multiple_choice("doc-a", wrong_item["id"], 0)

    assert correct["correct"] is True
    assert correct["exercise"]["schedule"]["last_grade"] == 4
    assert wrong["correct"] is False
    assert wrong["exercise"]["schedule"]["last_grade"] == 1
    logs = repository.load()["review_logs"]
    assert logs[-2]["result"] == "correct"
    assert logs[-2]["selected_index"] == 2
    assert logs[-2]["correct"] is True
    assert logs[-2]["user_answer"] == 2
    assert logs[-1]["result"] == "incorrect"
    assert logs[-1]["selected_index"] == 0
    assert logs[-1]["correct"] is False


@pytest.mark.parametrize("selected_index", [-1, 4, 1.0, True])
def test_mcq_answer_rejects_invalid_indices(service, selected_index):
    exercise = service.save_exercises("doc-a", "Alpha", [mcq_draft()])[0]
    with pytest.raises(ValueError):
        service.answer_multiple_choice("doc-a", exercise["id"], selected_index)


def test_exercise_review_type_guards_and_cross_document_ids(service):
    exercises = service.save_exercises(
        "doc-a", "Alpha", [mcq_draft(), short_draft()]
    )
    mcq = next(item for item in exercises if item["type"] == "multiple_choice")
    short = next(item for item in exercises if item["type"] == "short_answer")
    with pytest.raises(ValueError):
        service.review_short_answer("doc-a", mcq["id"], "mastered", "answer")
    with pytest.raises(ValueError):
        service.answer_multiple_choice("doc-a", short["id"], 0)
    with pytest.raises(ValueError):
        service.answer_multiple_choice("doc-b", mcq["id"], 0)
    with pytest.raises(ValueError):
        service.review_short_answer("doc-b", short["id"], "fuzzy", "answer")


def test_short_answer_reschedules_only_with_explicit_rating_and_logs_answer(service, repository):
    exercise = service.save_exercises("doc-a", "Alpha", [short_draft()])[0]

    reviewed = service.review_short_answer(
        "doc-a", exercise["id"], "fuzzy", "My own answer"
    )

    assert reviewed["schedule"]["last_grade"] == 3
    log = repository.load()["review_logs"][-1]
    assert log["rating"] == "fuzzy"
    assert log["user_answer"] == "My own answer"
    assert log["result"] == "fuzzy"

    before = repository.load()
    with pytest.raises(ValueError):
        service.review_short_answer("doc-a", exercise["id"], None, "answer")
    with pytest.raises(ValueError):
        service.review_short_answer("doc-a", exercise["id"], "mastered", " ")
    assert repository.load() == before


def test_card_review_rejects_cross_document_opaque_id(service):
    card = service.save_cards("doc-a", "Alpha", [card_draft()])[0]
    with pytest.raises(ValueError):
        service.review_card("doc-b", card["id"], "mastered")


def test_dashboard_reports_task_progress_and_document_scoped_due_items(repository):
    current = {"now": NOW, "today": TODAY}
    service = LearningService(
        repository, now=lambda: current["now"], today=lambda: current["today"]
    )
    plan = service.create_plan(
        "doc-a", "Alpha", "Three days", (TODAY + timedelta(days=2)).isoformat(), 20
    )
    service.set_task_completed("doc-a", plan["id"], plan["tasks"][1]["id"], True)
    card = service.save_cards("doc-a", "Alpha", [card_draft()])[0]
    exercise = service.save_exercises("doc-a", "Alpha", [mcq_draft()])[0]
    service.save_cards("doc-b", "Beta", [card_draft("Hidden", document_id="doc-b")])

    current["today"] = TODAY + timedelta(days=1)
    current["now"] = NOW + timedelta(days=1)
    dashboard = service.dashboard("doc-a")

    assert set(dashboard) == {
        "plans",
        "completed_tasks",
        "total_tasks",
        "completion_rate",
        "today_tasks",
        "overdue_tasks",
        "due_cards",
        "due_exercises",
    }
    assert dashboard["completed_tasks"] == 1
    assert dashboard["total_tasks"] == 3
    assert dashboard["completion_rate"] == pytest.approx(1 / 3)
    assert dashboard["today_tasks"] == []
    assert [task["id"] for task in dashboard["overdue_tasks"]] == [plan["tasks"][0]["id"]]
    assert [item["id"] for item in dashboard["due_cards"]] == [card["id"]]
    assert [item["id"] for item in dashboard["due_exercises"]] == [exercise["id"]]


def test_empty_dashboard_and_due_sorting(service, repository):
    assert service.dashboard("empty")["completion_rate"] == 0.0
    cards = service.save_cards(
        "doc-a", "Alpha", [card_draft("B"), card_draft("A")]
    )
    expected = sorted(cards, key=lambda item: (item["schedule"]["due_at"], item["created_at"], item["id"]))
    assert [item["id"] for item in service.dashboard("doc-a")["due_cards"]] == [
        item["id"] for item in expected
    ]

    dashboard = service.dashboard("doc-a")
    dashboard["due_cards"][0]["front"] = "changed"
    assert all(item["front"] != "changed" for item in repository.load()["cards"])


class DelegatingRepository:
    def __init__(self):
        self.deleted = []
        self.clear_count = 0

    def delete_document(self, document_id):
        self.deleted.append(document_id)

    def clear_all(self):
        self.clear_count += 1


def test_cascade_operations_delegate_to_repository():
    repository = DelegatingRepository()
    service = LearningService(repository, now=lambda: NOW, today=lambda: TODAY)

    service.delete_document("doc-a")
    service.clear_all()

    assert repository.deleted == ["doc-a"]
    assert repository.clear_count == 1


class CountingRepository:
    def __init__(self):
        self.data = deepcopy(EMPTY_LEARNING)
        self.update_count = 0

    def load(self):
        return deepcopy(self.data)

    def update(self, mutator):
        self.update_count += 1
        result = mutator(self.data)
        return deepcopy(result)


def test_each_batch_and_review_mutation_uses_one_atomic_update():
    repository = CountingRepository()
    service = LearningService(repository, now=lambda: NOW, today=lambda: TODAY)

    card = service.save_cards("doc-a", "Alpha", [card_draft()])[0]
    exercise = service.save_exercises("doc-a", "Alpha", [mcq_draft()])[0]
    plan = service.create_plan("doc-a", "Alpha", "Plan", TODAY.isoformat(), 10)
    service.set_task_completed("doc-a", plan["id"], plan["tasks"][0]["id"], True)
    service.review_card("doc-a", card["id"], "mastered")
    service.answer_multiple_choice("doc-a", exercise["id"], 2)

    assert repository.update_count == 6
