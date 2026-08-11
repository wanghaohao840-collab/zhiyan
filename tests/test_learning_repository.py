from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from threading import RLock
import json

import pytest

from app.learning_repository import (
    EMPTY_LEARNING,
    CorruptLearningDataError,
    LearningRepository,
)


DOCUMENT_ID = "doc-1"


def _source(document_id=DOCUMENT_ID):
    return {
        "chunk_id": "chunk-1",
        "document_id": document_id,
        "document_name": "guide.pdf",
        "page_number": 1,
        "source_position": None,
        "preview": "source preview",
    }


def _schedule():
    return {
        "repetitions": 0,
        "interval_days": 0,
        "ease_factor": 2.5,
        "due_at": "2026-08-11T08:00:00+00:00",
        "last_reviewed_at": None,
        "last_grade": None,
    }


def _data():
    return {
        "version": 1,
        "plans": [
            {
                "id": "plan-1",
                "document_id": DOCUMENT_ID,
                "document_name": "guide.pdf",
                "title": "Study guide",
                "target_date": "2026-08-12",
                "daily_minutes": 30,
                "status": "active",
                "tasks": [
                    {
                        "id": "task-1",
                        "due_date": "2026-08-11",
                        "phase": "reading",
                        "title": "Read",
                        "duration_minutes": 30,
                        "completed": False,
                        "completed_at": None,
                    }
                ],
                "created_at": "2026-08-11T08:00:00+00:00",
                "updated_at": "2026-08-11T08:00:00+00:00",
            }
        ],
        "cards": [
            {
                "id": "card-1",
                "document_id": DOCUMENT_ID,
                "document_name": "guide.pdf",
                "front": "Question",
                "back": "Answer",
                "sources": [_source()],
                "schedule": _schedule(),
                "created_at": "2026-08-11T08:00:00+00:00",
                "updated_at": "2026-08-11T08:00:00+00:00",
            }
        ],
        "exercises": [
            {
                "id": "exercise-1",
                "document_id": DOCUMENT_ID,
                "document_name": "guide.pdf",
                "type": "multiple_choice",
                "prompt": "Pick one",
                "options": ["A", "B", "C", "D"],
                "correct_index": 1,
                "reference_answer": None,
                "key_points": [],
                "explanation": "Because B",
                "sources": [_source()],
                "schedule": _schedule(),
                "created_at": "2026-08-11T08:00:00+00:00",
                "updated_at": "2026-08-11T08:00:00+00:00",
            }
        ],
        "review_logs": [
            {
                "id": "log-1",
                "item_type": "card",
                "item_id": "card-1",
                "document_id": DOCUMENT_ID,
                "grade": 5,
                "result": "mastered",
                "user_answer": None,
                "reviewed_at": "2026-08-11T08:10:00+00:00",
                "previous_due_at": "2026-08-11T08:00:00+00:00",
                "next_due_at": "2026-08-12T08:10:00+00:00",
            }
        ],
    }


def _repo(tmp_path):
    path = tmp_path / "learning.json"
    return LearningRepository(path, RLock()), path


def test_missing_file_returns_fresh_empty_root_without_writing(tmp_path):
    repo, path = _repo(tmp_path)

    first = repo.load()
    first["plans"].append({"not": "persisted"})
    second = repo.load()

    assert first is not second
    assert second == EMPTY_LEARNING
    assert not path.exists()


def test_save_round_trip_is_defensive_and_atomic(tmp_path):
    repo, path = _repo(tmp_path)
    data = _data()

    repo.save(data)
    data["cards"][0]["front"] = "changed outside repository"
    loaded = repo.load()
    loaded["cards"][0]["front"] = "changed copy"

    assert repo.load()["cards"][0]["front"] == "Question"
    assert '"version": 1' in path.read_text(encoding="utf-8")


def test_update_serializes_the_supplied_lock_and_does_not_lose_updates(tmp_path):
    lock = RLock()
    repo = LearningRepository(tmp_path / "learning.json", lock)

    def add(number):
        plan = _data()["plans"][0]
        plan["id"] = f"plan-{number}"
        return repo.update(lambda data: data["plans"].append(plan))

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(add, range(40)))

    assert {item["id"] for item in repo.load()["plans"]} == {
        f"plan-{number}" for number in range(40)
    }


def test_delete_document_cascades_all_collections_and_is_idempotent(tmp_path):
    repo, _ = _repo(tmp_path)
    data = _data()
    other = deepcopy(data)
    for collection in ("plans", "cards", "exercises", "review_logs"):
        other[collection][0]["document_id"] = "doc-2"
        if collection in {"cards", "exercises"}:
            other[collection][0]["sources"][0]["document_id"] = "doc-2"
        data[collection].extend(other[collection])
    repo.save(data)

    repo.delete_document(DOCUMENT_ID)
    repo.delete_document(DOCUMENT_ID)

    assert all(
        item["document_id"] == "doc-2"
        for collection in ("plans", "cards", "exercises", "review_logs")
        for item in repo.load()[collection]
    )


def test_clear_all_is_idempotent(tmp_path):
    repo, _ = _repo(tmp_path)
    repo.save(_data())

    repo.clear_all()
    repo.clear_all()

    assert repo.load() == EMPTY_LEARNING


@pytest.mark.parametrize(
    "data",
    [
        {"version": 2, "plans": [], "cards": [], "exercises": [], "review_logs": []},
        {"version": 1.0, "plans": [], "cards": [], "exercises": [], "review_logs": []},
        {"version": 1, "plans": [], "cards": [], "exercises": []},
        {**EMPTY_LEARNING, "unknown": []},
        {**EMPTY_LEARNING, "plans": {}},
        {**EMPTY_LEARNING, "cards": [{"id": "missing-fields"}]},
        {**EMPTY_LEARNING, "cards": [{**_data()["cards"][0], "schedule": {}}]},
        {**EMPTY_LEARNING, "exercises": [{**_data()["exercises"][0], "type": "essay"}]},
        {**EMPTY_LEARNING, "review_logs": [{**_data()["review_logs"][0], "item_type": "plan"}]},
    ],
)
def test_invalid_existing_data_fails_closed_and_preserves_bytes(tmp_path, data):
    repo, path = _repo(tmp_path)
    path.write_text(json.dumps(data), encoding="utf-8")
    before = path.read_bytes()

    with pytest.raises(CorruptLearningDataError, match="Learning data"):
        repo.load()
    with pytest.raises(CorruptLearningDataError):
        repo.update(lambda root: root["plans"].append({}))

    assert path.read_bytes() == before


def test_invalid_json_fails_closed_and_preserves_bytes(tmp_path):
    repo, path = _repo(tmp_path)
    path.write_bytes(b'{"version":')
    before = path.read_bytes()

    with pytest.raises(CorruptLearningDataError, match="Learning data") as error:
        repo.load()

    assert error.value.__cause__ is not None
    assert path.read_bytes() == before


def test_save_does_not_overwrite_existing_corrupt_bytes(tmp_path):
    repo, path = _repo(tmp_path)
    path.write_bytes(b"not json")
    before = path.read_bytes()

    with pytest.raises(CorruptLearningDataError):
        repo.save(_data())

    assert path.read_bytes() == before


@pytest.mark.parametrize(
    "mutate",
    [
        lambda data: data["exercises"][0].update({"options": []}),
        lambda data: data["exercises"][0].update(
            {"options": ["A", "B", "C", "C"]}
        ),
        lambda data: data["exercises"][0].update({"correct_index": 4}),
        lambda data: data["exercises"][0].update(
            {
                "type": "short_answer",
                "options": [],
                "correct_index": None,
                "reference_answer": "",
                "key_points": ["point"],
                "explanation": None,
            }
        ),
        lambda data: data["exercises"][0].update(
            {
                "type": "short_answer",
                "options": [],
                "correct_index": None,
                "reference_answer": "Reference answer",
                "key_points": [],
                "explanation": None,
            }
        ),
        lambda data: data["cards"][0]["schedule"].update(
            {
                "repetitions": -1,
            }
        ),
        lambda data: data["cards"][0]["schedule"].update({"interval_days": -1}),
        lambda data: data["cards"][0]["schedule"].update({"ease_factor": 1.2}),
        lambda data: data["cards"][0]["schedule"].update({"last_grade": 99}),
    ],
)
def test_save_rejects_unusable_exercises_and_schedules(tmp_path, mutate):
    repo, _ = _repo(tmp_path)
    data = _data()
    mutate(data)

    with pytest.raises(CorruptLearningDataError):
        repo.save(data)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda data: data["exercises"][0].update({"explanation": ""}),
        lambda data: data["exercises"][0].update(
            {"options": ["A", " a ", "B", "C"]}
        ),
        lambda data: data["cards"][0].update({"sources": []}),
        lambda data: data["exercises"][0].update({"sources": []}),
        lambda data: data["exercises"][0].update(
            {
                "type": "short_answer",
                "options": [],
                "correct_index": None,
                "reference_answer": "Reference answer",
                "key_points": ["point"],
                "explanation": None,
                "sources": [],
            }
        ),
    ],
)
def test_save_rejects_empty_sources_and_unusable_multiple_choice_fields(tmp_path, mutate):
    repo, _ = _repo(tmp_path)
    data = _data()
    mutate(data)

    with pytest.raises(CorruptLearningDataError):
        repo.save(data)
