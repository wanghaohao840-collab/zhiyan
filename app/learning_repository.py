from __future__ import annotations

from copy import deepcopy
from datetime import date, datetime, timedelta
from math import isfinite
from pathlib import Path
from threading import RLock
from typing import Any, Callable, TypeVar

from app.storage import read_json, write_json_atomic


EMPTY_LEARNING = {
    "version": 1,
    "plans": [],
    "cards": [],
    "exercises": [],
    "review_logs": [],
}

_ROOT_KEYS = frozenset(EMPTY_LEARNING)
_PHASES = frozenset({"reading", "cards", "exercises", "review"})
_PLAN_STATUSES = frozenset({"active", "completed"})
_EXERCISE_TYPES = frozenset({"multiple_choice", "short_answer"})
_LOG_ITEM_TYPES = frozenset({"card", "exercise"})
_LOG_RESULTS = frozenset({"forgot", "fuzzy", "mastered", "incorrect", "correct"})

T = TypeVar("T")


class CorruptLearningDataError(ValueError):
    """Raised when learning data cannot be safely read or persisted."""


class LearningRepository:
    def __init__(self, path: Path, lock: RLock):
        self.path = Path(path)
        self.lock = lock

    def load(self) -> dict:
        with self.lock:
            return deepcopy(self._load_unlocked())

    def save(self, data: dict) -> None:
        with self.lock:
            self._ensure_existing_valid_unlocked()
            self._validate_or_raise(data)
            write_json_atomic(self.path, data)

    def update(self, mutator: Callable[[dict], T]) -> T:
        with self.lock:
            data = self._load_unlocked()
            result = mutator(data)
            self._validate_or_raise(data)
            write_json_atomic(self.path, data)
            return result

    def delete_document(self, document_id: str) -> None:
        self.update(
            lambda data: _delete_document(data, document_id)
        )

    def clear_all(self) -> None:
        self.update(
            lambda data: data.update(deepcopy(EMPTY_LEARNING))
        )

    def _ensure_existing_valid_unlocked(self) -> None:
        if self.path.exists():
            self._load_unlocked()

    def _load_unlocked(self) -> dict:
        try:
            data = read_json(self.path, default=deepcopy(EMPTY_LEARNING))
        except Exception as exc:
            raise CorruptLearningDataError(
                f"Learning data is corrupt and was not modified: {self.path}"
            ) from exc
        self._validate_or_raise(data)
        return data

    def _validate_or_raise(self, data: Any) -> None:
        try:
            _validate_root(data)
        except Exception as exc:
            if isinstance(exc, CorruptLearningDataError):
                raise
            raise CorruptLearningDataError(
                f"Learning data has an invalid structure and was not modified: {self.path}"
            ) from exc


def _delete_document(data: dict, document_id: str) -> None:
    for collection in ("plans", "cards", "exercises", "review_logs"):
        data[collection] = [
            item for item in data[collection] if item["document_id"] != document_id
        ]


def _validate_root(data: Any) -> None:
    if not isinstance(data, dict) or set(data) != _ROOT_KEYS:
        raise ValueError("root must contain exactly version, plans, cards, exercises, and review_logs")
    if not _is_int(data["version"]) or data["version"] != 1:
        raise ValueError("version must be integer 1")
    for collection in ("plans", "cards", "exercises", "review_logs"):
        if not isinstance(data[collection], list):
            raise ValueError(f"{collection} must be a list")
    for plan in data["plans"]:
        _validate_plan(plan)
    for card in data["cards"]:
        _validate_card(card)
    for exercise in data["exercises"]:
        _validate_exercise(exercise)
    for log in data["review_logs"]:
        _validate_review_log(log)


def _validate_plan(plan: Any) -> None:
    _require_mapping(plan, "plan")
    _require_strings(
        plan,
        "plan",
        "id",
        "document_id",
        "document_name",
        "title",
        "target_date",
        "created_at",
        "updated_at",
    )
    _require_date(plan["target_date"], "plan.target_date")
    _require_timestamp(plan["created_at"], "plan.created_at")
    _require_timestamp(plan["updated_at"], "plan.updated_at")
    _require_int(plan, "daily_minutes", "plan")
    if plan.get("status") not in _PLAN_STATUSES:
        raise ValueError("plan.status is invalid")
    if not isinstance(plan.get("tasks"), list):
        raise ValueError("plan.tasks must be a list")
    for task in plan["tasks"]:
        _validate_task(task)


def _validate_task(task: Any) -> None:
    _require_mapping(task, "plan task")
    _require_strings(task, "plan task", "id", "due_date", "title")
    _require_date(task["due_date"], "plan task.due_date")
    if task.get("phase") not in _PHASES:
        raise ValueError("plan task.phase is invalid")
    _require_int(task, "duration_minutes", "plan task")
    if not isinstance(task.get("completed"), bool):
        raise ValueError("plan task.completed must be a boolean")
    _require_key(task, "completed_at", "plan task")
    _require_optional_timestamp(task.get("completed_at"), "plan task.completed_at")


def _validate_card(card: Any) -> None:
    _require_mapping(card, "card")
    _require_strings(
        card,
        "card",
        "id",
        "document_id",
        "document_name",
        "front",
        "back",
        "created_at",
        "updated_at",
    )
    _require_timestamp(card["created_at"], "card.created_at")
    _require_timestamp(card["updated_at"], "card.updated_at")
    _validate_sources(card, "card")
    _validate_schedule(card.get("schedule"), "card.schedule")


def _validate_exercise(exercise: Any) -> None:
    _require_mapping(exercise, "exercise")
    _require_strings(
        exercise,
        "exercise",
        "id",
        "document_id",
        "document_name",
        "prompt",
        "created_at",
        "updated_at",
    )
    exercise_type = exercise.get("type")
    if exercise_type not in _EXERCISE_TYPES:
        raise ValueError("exercise.type is invalid")
    _require_timestamp(exercise["created_at"], "exercise.created_at")
    _require_timestamp(exercise["updated_at"], "exercise.updated_at")
    _validate_sources(exercise, "exercise")
    _validate_schedule(exercise.get("schedule"), "exercise.schedule")
    if exercise_type == "multiple_choice":
        _validate_multiple_choice_options(exercise.get("options"))
        _require_key(exercise, "correct_index", "multiple-choice exercise")
        correct_index = exercise.get("correct_index")
        if not _is_int(correct_index) or not 0 <= correct_index <= 3:
            raise ValueError("multiple-choice correct_index must be in 0..3")
        _require_key(exercise, "reference_answer", "multiple-choice exercise")
        if exercise.get("reference_answer") is not None:
            raise ValueError("multiple-choice reference_answer must be null")
        _require_key(exercise, "key_points", "multiple-choice exercise")
        if exercise.get("key_points") != []:
            raise ValueError("multiple-choice key_points must be empty")
        _require_key(exercise, "explanation", "multiple-choice exercise")
        if not isinstance(exercise.get("explanation"), str):
            raise ValueError("multiple-choice explanation must be a string")
    else:
        if exercise.get("options") != []:
            raise ValueError("short-answer options must be empty")
        _require_key(exercise, "correct_index", "short-answer exercise")
        if exercise.get("correct_index") is not None:
            raise ValueError("short-answer correct_index must be null")
        _require_key(exercise, "reference_answer", "short-answer exercise")
        _require_non_empty_string(exercise.get("reference_answer"), "short-answer reference_answer")
        _validate_short_answer_key_points(exercise.get("key_points"))
        _require_key(exercise, "explanation", "short-answer exercise")
        if exercise.get("explanation") is not None:
            raise ValueError("short-answer explanation must be null")


def _validate_sources(item: dict, name: str) -> None:
    sources = item.get("sources")
    if not isinstance(sources, list):
        raise ValueError(f"{name}.sources must be a list")
    for source in sources:
        _require_mapping(source, f"{name} source")
        _require_strings(
            source,
            f"{name} source",
            "chunk_id",
            "document_id",
            "document_name",
            "preview",
        )
        if source["document_id"] != item["document_id"]:
            raise ValueError(f"{name} source belongs to another document")
        for field in ("page_number", "source_position"):
            _require_key(source, field, f"{name} source")
            value = source.get(field)
            if value is not None and not _is_int(value):
                raise ValueError(f"{name} source.{field} must be an integer or null")


def _validate_schedule(schedule: Any, name: str) -> None:
    _require_mapping(schedule, name)
    for field in ("repetitions", "interval_days"):
        _require_int(schedule, field, name)
        if schedule[field] < 0:
            raise ValueError(f"{name}.{field} must be nonnegative")
    ease_factor = schedule.get("ease_factor")
    if (
        not isinstance(ease_factor, (int, float))
        or isinstance(ease_factor, bool)
        or not isfinite(ease_factor)
        or ease_factor < 1.3
    ):
        raise ValueError(f"{name}.ease_factor must be a finite number at least 1.3")
    _require_timestamp(schedule.get("due_at"), f"{name}.due_at")
    _require_key(schedule, "last_reviewed_at", name)
    _require_optional_timestamp(schedule.get("last_reviewed_at"), f"{name}.last_reviewed_at")
    _require_key(schedule, "last_grade", name)
    last_grade = schedule.get("last_grade")
    if last_grade is not None and (not _is_int(last_grade) or not 0 <= last_grade <= 5):
        raise ValueError(f"{name}.last_grade must be an integer in 0..5 or null")


def _validate_review_log(log: Any) -> None:
    _require_mapping(log, "review log")
    _require_strings(
        log,
        "review log",
        "id",
        "item_id",
        "document_id",
        "reviewed_at",
        "previous_due_at",
        "next_due_at",
    )
    if log.get("item_type") not in _LOG_ITEM_TYPES:
        raise ValueError("review log.item_type is invalid")
    _require_int(log, "grade", "review log")
    if log.get("result") not in _LOG_RESULTS:
        raise ValueError("review log.result is invalid")
    _require_timestamp(log["reviewed_at"], "review log.reviewed_at")
    _require_timestamp(log["previous_due_at"], "review log.previous_due_at")
    _require_timestamp(log["next_due_at"], "review log.next_due_at")
    _require_key(log, "user_answer", "review log")
    answer = log.get("user_answer")
    if answer is not None and not isinstance(answer, str) and not _is_int(answer):
        raise ValueError("review log.user_answer has an invalid type")


def _require_mapping(value: Any, name: str) -> None:
    if not isinstance(value, dict):
        raise ValueError(f"{name} must be an object")


def _require_key(data: dict, field: str, name: str) -> None:
    if field not in data:
        raise ValueError(f"{name}.{field} is required")


def _require_strings(data: dict, name: str, *fields: str) -> None:
    for field in fields:
        _require_non_empty_string(data.get(field), f"{name}.{field}")


def _require_non_empty_string(value: Any, name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")


def _require_int(data: dict, field: str, name: str) -> None:
    if not _is_int(data.get(field)):
        raise ValueError(f"{name}.{field} must be an integer")


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _validate_multiple_choice_options(value: Any) -> None:
    if (
        not isinstance(value, list)
        or len(value) != 4
        or not all(isinstance(option, str) and option.strip() for option in value)
        or len(set(value)) != 4
    ):
        raise ValueError("multiple-choice options must be four unique non-empty strings")


def _validate_short_answer_key_points(value: Any) -> None:
    if (
        not isinstance(value, list)
        or not 1 <= len(value) <= 8
        or not all(isinstance(point, str) and point.strip() for point in value)
        or len(set(value)) != len(value)
    ):
        raise ValueError("short-answer key_points must be 1..8 unique non-empty strings")


def _require_date(value: str, name: str) -> None:
    try:
        if len(value) != 10:
            raise ValueError
        date.fromisoformat(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be an ISO date") from exc


def _require_timestamp(value: Any, name: str) -> None:
    if not isinstance(value, str):
        raise ValueError(f"{name} must be an ISO UTC timestamp")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"{name} must be an ISO UTC timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() != timedelta(0):
        raise ValueError(f"{name} must be an ISO UTC timestamp")


def _require_optional_timestamp(value: Any, name: str) -> None:
    if value is not None:
        _require_timestamp(value, name)
