from __future__ import annotations

from copy import deepcopy
from datetime import date, datetime, timedelta, timezone
from typing import Any, Callable
from unicodedata import normalize
from uuid import uuid4


_PHASES = ("reading", "cards", "exercises", "review")
_PHASE_WEIGHTS = (25, 30, 25, 20)
_PHASE_TITLES = {
    "reading": "Read and understand the document",
    "cards": "Review learning cards",
    "exercises": "Practice exercises",
    "review": "Comprehensive review",
}
_RATINGS = {"forgot": 1, "fuzzy": 3, "mastered": 5}


class LearningService:
    def __init__(
        self,
        repository,
        *,
        now: Callable[[], datetime] | None = None,
        today: Callable[[], date] | None = None,
    ):
        self.repository = repository
        self._now_clock = now or (lambda: datetime.now(timezone.utc))
        self._today_clock = today or date.today

    def create_plan(
        self,
        document_id,
        document_name,
        title,
        target_date,
        daily_minutes,
    ) -> dict:
        document_id = _required_string(document_id, "document_id")
        document_name = _required_string(document_name, "document_name")
        title = _plan_title(title)
        target = _iso_date(target_date, "target_date")
        minutes = _bounded_int(daily_minutes, "daily_minutes", 5, 480)
        today = self._today()
        if target < today:
            raise ValueError("target_date cannot be before today")
        if target > today + timedelta(days=364):
            raise ValueError("target_date cannot be more than 364 days after today")
        timestamp = self._timestamp()
        phases = _allocate_phases((target - today).days + 1)
        plan = {
            "id": str(uuid4()),
            "document_id": document_id,
            "document_name": document_name,
            "title": title,
            "target_date": target.isoformat(),
            "daily_minutes": minutes,
            "status": "active",
            "tasks": [
                {
                    "id": str(uuid4()),
                    "due_date": (today + timedelta(days=offset)).isoformat(),
                    "phase": phase,
                    "title": _PHASE_TITLES[phase],
                    "duration_minutes": minutes,
                    "completed": False,
                    "completed_at": None,
                }
                for offset, phase in enumerate(phases)
            ],
            "created_at": timestamp,
            "updated_at": timestamp,
        }

        def add(data):
            data["plans"].append(plan)
            return deepcopy(plan)

        return self.repository.update(add)

    def list_plans(self, document_id) -> list[dict]:
        document_id = _required_string(document_id, "document_id")
        plans = [
            plan
            for plan in self.repository.load()["plans"]
            if plan["document_id"] == document_id
        ]
        return deepcopy(sorted(plans, key=_created_key))

    def set_task_completed(
        self, document_id, plan_id, task_id, completed
    ) -> dict:
        document_id = _required_string(document_id, "document_id")
        plan_id = _required_string(plan_id, "plan_id")
        task_id = _required_string(task_id, "task_id")
        if not isinstance(completed, bool):
            raise ValueError("completed must be a boolean")
        timestamp = self._timestamp()

        def update(data):
            plan = _find_scoped(data["plans"], document_id, plan_id, "plan")
            task = next((item for item in plan["tasks"] if item["id"] == task_id), None)
            if task is None:
                raise ValueError("task not found for document plan")
            task["completed"] = completed
            task["completed_at"] = timestamp if completed else None
            plan["status"] = (
                "completed" if all(item["completed"] for item in plan["tasks"]) else "active"
            )
            plan["updated_at"] = timestamp
            return deepcopy(plan)

        return self.repository.update(update)

    def dashboard(self, document_id) -> dict:
        document_id = _required_string(document_id, "document_id")
        data = self.repository.load()
        plans = sorted(
            (plan for plan in data["plans"] if plan["document_id"] == document_id),
            key=_created_key,
        )
        tasks = [task for plan in plans for task in plan["tasks"]]
        completed_tasks = sum(task["completed"] for task in tasks)
        today = self._today().isoformat()
        now = self._timestamp()
        today_tasks = sorted(
            (
                task
                for task in tasks
                if not task["completed"] and task["due_date"] == today
            ),
            key=_task_key,
        )
        overdue_tasks = sorted(
            (
                task
                for task in tasks
                if not task["completed"] and task["due_date"] < today
            ),
            key=_task_key,
        )
        due_cards = _due_items(data["cards"], document_id, now)
        due_exercises = _due_items(data["exercises"], document_id, now)
        total_tasks = len(tasks)
        return deepcopy(
            {
                "plans": plans,
                "completed_tasks": completed_tasks,
                "total_tasks": total_tasks,
                "completion_rate": completed_tasks / total_tasks if total_tasks else 0.0,
                "today_tasks": today_tasks,
                "overdue_tasks": overdue_tasks,
                "due_cards": due_cards,
                "due_exercises": due_exercises,
            }
        )

    def existing_card_fronts(self, document_id) -> list[str]:
        return [card["front"] for card in self.list_cards(document_id)]

    def existing_exercise_prompts(self, document_id) -> list[str]:
        return [exercise["prompt"] for exercise in self.list_exercises(document_id)]

    def save_cards(self, document_id, document_name, drafts) -> list[dict]:
        document_id = _required_string(document_id, "document_id")
        document_name = _required_string(document_name, "document_name")
        drafts = _draft_list(drafts)
        timestamp = self._timestamp()

        def save(data):
            seen = {
                _normalized(item["front"])
                for item in data["cards"]
                if item["document_id"] == document_id
            }
            cards = []
            for draft in drafts:
                validated = _validate_card_draft(draft, document_id)
                key = _normalized(validated["front"])
                if key in seen:
                    raise ValueError("duplicate card front")
                seen.add(key)
                cards.append(
                    {
                        "id": str(uuid4()),
                        "document_id": document_id,
                        "document_name": document_name,
                        "front": validated["front"],
                        "back": validated["back"],
                        "sources": validated["sources"],
                        "schedule": _new_schedule(timestamp),
                        "created_at": timestamp,
                        "updated_at": timestamp,
                    }
                )
            data["cards"].extend(cards)
            return deepcopy(cards)

        return self.repository.update(save)

    def save_exercises(self, document_id, document_name, drafts) -> list[dict]:
        document_id = _required_string(document_id, "document_id")
        document_name = _required_string(document_name, "document_name")
        drafts = _draft_list(drafts)
        timestamp = self._timestamp()

        def save(data):
            seen = {
                _normalized(item["prompt"])
                for item in data["exercises"]
                if item["document_id"] == document_id
            }
            exercises = []
            for draft in drafts:
                validated = _validate_exercise_draft(draft, document_id)
                key = _normalized(validated["prompt"])
                if key in seen:
                    raise ValueError("duplicate exercise prompt")
                seen.add(key)
                common = {
                    "id": str(uuid4()),
                    "document_id": document_id,
                    "document_name": document_name,
                    "type": validated["type"],
                    "prompt": validated["prompt"],
                    "sources": validated["sources"],
                    "schedule": _new_schedule(timestamp),
                    "created_at": timestamp,
                    "updated_at": timestamp,
                }
                if validated["type"] == "multiple_choice":
                    common.update(
                        {
                            "options": validated["options"],
                            "correct_index": validated["correct_index"],
                            "explanation": validated["explanation"],
                            "reference_answer": None,
                            "key_points": [],
                        }
                    )
                else:
                    common.update(
                        {
                            "options": [],
                            "correct_index": None,
                            "explanation": None,
                            "reference_answer": validated["reference_answer"],
                            "key_points": validated["key_points"],
                        }
                    )
                exercises.append(common)
            data["exercises"].extend(exercises)
            return deepcopy(exercises)

        return self.repository.update(save)

    def list_cards(self, document_id) -> list[dict]:
        document_id = _required_string(document_id, "document_id")
        cards = [
            item
            for item in self.repository.load()["cards"]
            if item["document_id"] == document_id
        ]
        return deepcopy(sorted(cards, key=_created_key))

    def list_exercises(self, document_id) -> list[dict]:
        document_id = _required_string(document_id, "document_id")
        exercises = [
            item
            for item in self.repository.load()["exercises"]
            if item["document_id"] == document_id
        ]
        return deepcopy(sorted(exercises, key=_created_key))

    def review_card(self, document_id, card_id, rating) -> dict:
        document_id = _required_string(document_id, "document_id")
        card_id = _required_string(card_id, "card_id")
        grade = _rating_grade(rating)
        timestamp = self._timestamp()

        def review(data):
            card = _find_scoped(data["cards"], document_id, card_id, "card")
            _reschedule_and_log(
                data,
                card,
                item_type="card",
                grade=grade,
                result=rating,
                timestamp=timestamp,
                rating=rating,
                user_answer=None,
            )
            return deepcopy(card)

        return self.repository.update(review)

    def answer_multiple_choice(
        self, document_id, exercise_id, selected_index
    ) -> dict:
        document_id = _required_string(document_id, "document_id")
        exercise_id = _required_string(exercise_id, "exercise_id")
        if not _is_int(selected_index) or not 0 <= selected_index <= 3:
            raise ValueError("selected_index must be an integer in 0..3")
        timestamp = self._timestamp()

        def review(data):
            exercise = _find_scoped(
                data["exercises"], document_id, exercise_id, "exercise"
            )
            if exercise["type"] != "multiple_choice":
                raise ValueError("exercise is not multiple choice")
            correct = selected_index == exercise["correct_index"]
            _reschedule_and_log(
                data,
                exercise,
                item_type="exercise",
                grade=4 if correct else 1,
                result="correct" if correct else "incorrect",
                timestamp=timestamp,
                user_answer=selected_index,
                selected_index=selected_index,
                correct=correct,
            )
            return {
                "exercise": deepcopy(exercise),
                "selected_index": selected_index,
                "correct": correct,
                "correct_index": exercise["correct_index"],
                "explanation": exercise["explanation"],
            }

        return self.repository.update(review)

    def review_short_answer(
        self, document_id, exercise_id, rating, user_answer
    ) -> dict:
        document_id = _required_string(document_id, "document_id")
        exercise_id = _required_string(exercise_id, "exercise_id")
        grade = _rating_grade(rating)
        user_answer = _required_string(user_answer, "user_answer")
        timestamp = self._timestamp()

        def review(data):
            exercise = _find_scoped(
                data["exercises"], document_id, exercise_id, "exercise"
            )
            if exercise["type"] != "short_answer":
                raise ValueError("exercise is not short answer")
            _reschedule_and_log(
                data,
                exercise,
                item_type="exercise",
                grade=grade,
                result=rating,
                timestamp=timestamp,
                rating=rating,
                user_answer=user_answer,
            )
            return deepcopy(exercise)

        return self.repository.update(review)

    def delete_document(self, document_id) -> None:
        document_id = _required_string(document_id, "document_id")
        self.repository.delete_document(document_id)

    def clear_all(self) -> None:
        self.repository.clear_all()

    def _timestamp(self) -> str:
        value = self._now_clock()
        if not isinstance(value, datetime) or value.tzinfo is None:
            raise ValueError("now clock must return a timezone-aware datetime")
        return value.astimezone(timezone.utc).isoformat()

    def _today(self) -> date:
        value = self._today_clock()
        if not isinstance(value, date) or isinstance(value, datetime):
            raise ValueError("today clock must return a date")
        return value


def _allocate_phases(day_count: int) -> list[str]:
    if day_count == 1:
        return ["review"]
    if day_count == 2:
        return ["reading", "review"]
    if day_count == 3:
        return ["reading", "cards", "review"]

    quotas = [day_count * weight / sum(_PHASE_WEIGHTS) for weight in _PHASE_WEIGHTS]
    floors = [int(quota) for quota in quotas]
    counts = [max(1, value) for value in floors]
    fractions = [quota - floor for quota, floor in zip(quotas, floors)]
    while sum(counts) < day_count:
        order = sorted(range(len(_PHASES)), key=lambda index: (-fractions[index], index))
        for index in order:
            if sum(counts) == day_count:
                break
            counts[index] += 1
    while sum(counts) > day_count:
        order = sorted(range(len(_PHASES)), key=lambda index: (fractions[index], index))
        for index in order:
            if sum(counts) == day_count:
                break
            if counts[index] > 1:
                counts[index] -= 1
    return [
        phase
        for phase, count in zip(_PHASES, counts)
        for _ in range(count)
    ]


def _new_schedule(timestamp: str) -> dict:
    return {
        "repetitions": 0,
        "interval_days": 0,
        "ease_factor": 2.5,
        "due_at": timestamp,
        "last_reviewed_at": None,
        "last_grade": None,
    }


def _next_schedule(schedule: dict, grade: int, timestamp: str) -> dict:
    ease_delta = 0.1 - (5 - grade) * (0.08 + (5 - grade) * 0.02)
    ease_factor = round(max(1.3, schedule["ease_factor"] + ease_delta), 10)
    if grade < 3:
        repetitions = 0
        interval_days = 1
    else:
        repetitions = schedule["repetitions"] + 1
        if repetitions == 1:
            interval_days = 1
        elif repetitions == 2:
            interval_days = 6
        else:
            interval_days = max(1, round(schedule["interval_days"] * ease_factor))
    reviewed_at = datetime.fromisoformat(timestamp)
    return {
        "repetitions": repetitions,
        "interval_days": interval_days,
        "ease_factor": ease_factor,
        "due_at": (reviewed_at + timedelta(days=interval_days)).isoformat(),
        "last_reviewed_at": timestamp,
        "last_grade": grade,
    }


def _reschedule_and_log(
    data: dict,
    item: dict,
    *,
    item_type: str,
    grade: int,
    result: str,
    timestamp: str,
    user_answer: str | int | None,
    rating: str | None = None,
    selected_index: int | None = None,
    correct: bool | None = None,
) -> None:
    old_schedule = deepcopy(item["schedule"])
    new_schedule = _next_schedule(old_schedule, grade, timestamp)
    item["schedule"] = new_schedule
    item["updated_at"] = timestamp
    log = {
        "id": str(uuid4()),
        "item_type": item_type,
        "item_id": item["id"],
        "document_id": item["document_id"],
        "grade": grade,
        "quality": grade,
        "result": result,
        "reviewed_at": timestamp,
        "previous_due_at": old_schedule["due_at"],
        "next_due_at": new_schedule["due_at"],
        "old_schedule": old_schedule,
        "new_schedule": deepcopy(new_schedule),
        "user_answer": user_answer,
    }
    if rating is not None:
        log["rating"] = rating
    if selected_index is not None:
        log["selected_index"] = selected_index
    if correct is not None:
        log["correct"] = correct
    data["review_logs"].append(log)


def _validate_card_draft(draft: Any, document_id: str) -> dict:
    draft = _mapping(draft, "card draft")
    return {
        "front": _required_string(draft.get("front"), "card front"),
        "back": _required_string(draft.get("back"), "card back"),
        "sources": _sources(draft.get("sources"), document_id),
    }


def _validate_exercise_draft(draft: Any, document_id: str) -> dict:
    draft = _mapping(draft, "exercise draft")
    exercise_type = draft.get("type")
    prompt = _required_string(draft.get("prompt"), "exercise prompt")
    sources = _sources(draft.get("sources"), document_id)
    if exercise_type == "multiple_choice":
        options = draft.get("options")
        if (
            not isinstance(options, list)
            or len(options) != 4
            or not all(isinstance(option, str) and option.strip() for option in options)
            or len({_normalized(option) for option in options}) != 4
        ):
            raise ValueError("multiple-choice options must be four unique non-empty strings")
        correct_index = draft.get("correct_index")
        if not _is_int(correct_index) or not 0 <= correct_index <= 3:
            raise ValueError("correct_index must be an integer in 0..3")
        return {
            "type": exercise_type,
            "prompt": prompt,
            "options": deepcopy(options),
            "correct_index": correct_index,
            "explanation": _required_string(
                draft.get("explanation"), "multiple-choice explanation"
            ),
            "sources": sources,
        }
    if exercise_type == "short_answer":
        key_points = draft.get("key_points")
        if (
            not isinstance(key_points, list)
            or not 1 <= len(key_points) <= 8
            or not all(isinstance(point, str) and point.strip() for point in key_points)
            or len({_normalized(point) for point in key_points}) != len(key_points)
        ):
            raise ValueError("key_points must be 1..8 unique non-empty strings")
        return {
            "type": exercise_type,
            "prompt": prompt,
            "reference_answer": _required_string(
                draft.get("reference_answer"), "reference_answer"
            ),
            "key_points": deepcopy(key_points),
            "sources": sources,
        }
    raise ValueError("exercise type must be multiple_choice or short_answer")


def _sources(value: Any, document_id: str) -> list[dict]:
    if not isinstance(value, list) or not value:
        raise ValueError("sources must contain at least one source")
    validated = []
    for raw_source in value:
        item = _mapping(raw_source, "source")
        for field in ("chunk_id", "document_id", "document_name", "preview"):
            _required_string(item.get(field), f"source {field}")
        if item["document_id"] != document_id:
            raise ValueError("source belongs to another document")
        for field in ("page_number", "source_position"):
            if field not in item:
                raise ValueError(f"source {field} is required")
            value = item[field]
            if value is not None and not _is_int(value):
                raise ValueError(f"source {field} must be an integer or null")
        validated.append(deepcopy(item))
    return validated


def _find_scoped(items: list[dict], document_id: str, item_id: str, name: str) -> dict:
    item = next(
        (
            candidate
            for candidate in items
            if candidate["id"] == item_id and candidate["document_id"] == document_id
        ),
        None,
    )
    if item is None:
        raise ValueError(f"{name} not found for document")
    return item


def _due_items(items: list[dict], document_id: str, now: str) -> list[dict]:
    return sorted(
        (
            item
            for item in items
            if item["document_id"] == document_id and item["schedule"]["due_at"] <= now
        ),
        key=lambda item: (
            item["schedule"]["due_at"],
            item["created_at"],
            item["id"],
        ),
    )


def _required_string(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")
    return value


def _mapping(value: Any, name: str) -> dict:
    if not isinstance(value, dict):
        raise ValueError(f"{name} must be an object")
    return value


def _draft_list(value: Any) -> list[dict]:
    if not isinstance(value, list) or not value:
        raise ValueError("drafts must be a non-empty list")
    return value


def _bounded_int(value: Any, name: str, minimum: int, maximum: int) -> int:
    if not _is_int(value) or not minimum <= value <= maximum:
        raise ValueError(f"{name} must be an integer in {minimum}..{maximum}")
    return value


def _plan_title(value: Any) -> str:
    title = _required_string(value, "title").strip()
    if len(title) > 100:
        raise ValueError("title must contain at most 100 characters")
    return title


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _iso_date(value: Any, name: str) -> date:
    if not isinstance(value, str) or len(value) != 10:
        raise ValueError(f"{name} must be an ISO date")
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"{name} must be an ISO date") from exc


def _rating_grade(value: Any) -> int:
    if not isinstance(value, str) or value not in _RATINGS:
        raise ValueError("rating must be forgot, fuzzy, or mastered")
    return _RATINGS[value]


def _normalized(value: str) -> str:
    return " ".join(normalize("NFKC", value).casefold().split())


def _created_key(item: dict) -> tuple[str, str]:
    return item["created_at"], item["id"]


def _task_key(item: dict) -> tuple[str, str]:
    return item["due_date"], item["id"]
