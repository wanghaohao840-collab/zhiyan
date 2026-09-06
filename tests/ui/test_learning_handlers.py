from __future__ import annotations

from types import SimpleNamespace

import gradio as gr
import pytest

from ui import gradio_app as app


SOURCE = {
    "document_id": "doc-1",
    "document_name": "One.pdf",
    "page_number": 2,
    "source_position": None,
    "preview": "grounded evidence",
}


class Assistant:
    current_document_id = "doc-1"

    def __init__(self):
        self.cards = [{"id": "card-1", "front": "Front", "back": "Back", "sources": [SOURCE], "schedule": {"due_at": "tomorrow"}}]
        self.exercises = [
            {"id": "mcq-1", "type": "multiple_choice", "prompt": "Pick", "options": ["A", "B", "C", "D"], "correct_index": 1, "explanation": "Because B", "sources": [SOURCE], "schedule": {"due_at": "tomorrow"}},
            {"id": "short-1", "type": "short_answer", "prompt": "Explain", "options": [], "reference_answer": "Reference", "key_points": ["one", "two"], "sources": [SOURCE], "schedule": {"due_at": "tomorrow"}},
        ]

    def get_documents(self): return ["One.pdf | doc-1", "Two.md | doc-2"]
    def select_document(self, label): self.current_document_id = label.rsplit(" | ", 1)[1]
    def get_learning_plans(self): return []
    def get_learning_dashboard(self):
        return {"plans": [], "completed_tasks": 0, "total_tasks": 0, "completion_rate": 0, "today_tasks": [], "overdue_tasks": [], "due_cards": self.cards, "due_exercises": self.exercises}
    def create_learning_plan(self, title, target_date, daily_minutes): return {"id": "plan"}
    def set_learning_task_completed(self, plan, task, completed): return {}
    def get_learning_cards(self): return self.cards if self.current_document_id == "doc-1" else []
    def generate_learning_cards(self, count): return self.cards
    def review_learning_card(self, card_id, rating): return self.cards[0]
    def get_learning_exercises(self): return self.exercises if self.current_document_id == "doc-1" else []
    def generate_learning_exercises(self, count, kind): return self.exercises
    def answer_learning_multiple_choice(self, exercise_id, index):
        exercise = self.exercises[0]
        exercise["schedule"]["due_at"] = "tomorrow"
        return {"exercise": exercise, "selected_index": index, "correct": index == 1, "correct_index": 1, "explanation": "Because B"}
    def review_learning_short_answer(self, exercise_id, rating, answer): return self.exercises[1]


@pytest.fixture
def assistant(monkeypatch):
    value = Assistant()
    monkeypatch.setattr(app, "_require_assistant", lambda token: value)
    return value


def test_learning_dropdown_is_scalar_and_refresh_preserves_valid_selection(assistant):
    update, *clears = app.refresh_learning_documents("token", "One.pdf | doc-1")
    assert update["value"] == "One.pdf | doc-1"
    assert update["choices"] == assistant.get_documents()
    assert clears[:3] == ["", "", ""]
    assert clears[3]["value"] is None and clears[4]["value"] is None
    assert tuple(clears) == app.clear_learning_interaction()


def test_document_change_clears_reveal_and_display_state(assistant):
    result = app.select_learning_document("token", "Two.md | doc-2")
    assert result[:3] == ("", "", "")
    assert result[3]["value"] is None and result[4]["value"] is None
    assert result == app.clear_learning_interaction()


def test_dashboard_and_plan_handlers_return_list_tables(assistant):
    summary, tasks, due = app.refresh_learning_dashboard("token", "One.pdf | doc-1")
    assert "完成率" in summary and isinstance(tasks, list)
    assert [row[0] for row in due] == ["卡片", "练习", "练习"]
    result, plans, plan_tasks = app.create_learning_plan("token", "One.pdf | doc-1", "Plan", "2026-09-05", 30)
    assert result == "计划已创建" and isinstance(plans, list) and isinstance(plan_tasks, list)


def test_card_must_be_revealed_and_exact_selection_must_match(assistant):
    with pytest.raises(gr.Error, match="exact card"):
        app.review_learning_card("token", "One.pdf | doc-1", "card-1", "", "mastered")
    answer, sources, revealed = app.reveal_learning_card("token", "One.pdf | doc-1", "card-1")
    assert (answer, revealed) == ("Back", "card-1")
    assert "One.pdf" in sources and "第 2 页" in sources and "grounded evidence" in sources
    status, cleared = app.review_learning_card("token", "One.pdf | doc-1", "card-1", revealed, "mastered")
    assert "下次复习" in status and cleared == ""


def test_stale_card_and_cross_document_ids_are_rejected(assistant):
    with pytest.raises(gr.Error, match="exact card"):
        app.review_learning_card("token", "One.pdf | doc-1", "card-1", "old-card", "fuzzy")
    with pytest.raises(gr.Error, match="not found"):
        app.reveal_learning_card("token", "Two.md | doc-2", "card-1")


def test_mcq_is_auto_graded_and_displays_sources(assistant):
    result = app.submit_learning_multiple_choice("token", "One.pdf | doc-1", "mcq-1", 1)
    assert "Correct" in result and "Because B" in result and "grounded evidence" in result


def test_short_answer_requires_exact_reveal_before_self_review(assistant):
    reference, revealed = app.reveal_learning_short_answer("token", "One.pdf | doc-1", "short-1", "Mine")
    assert "Reference" in reference and "one、two" in reference
    with pytest.raises(gr.Error, match="exact reference"):
        app.review_learning_short_answer("token", "One.pdf | doc-1", "short-1", "mcq-1", "fuzzy", "Mine")
    status, cleared = app.review_learning_short_answer("token", "One.pdf | doc-1", "short-1", revealed, "fuzzy", "Mine")
    assert "下次复习" in status and cleared == ""


def test_component_contracts_are_present():
    source = app.Path(app.__file__).read_text(encoding="utf-8")
    assert 'with gr.Tab("8. 学习中心")' in source
    for tab in ("今日任务", "学习计划", "知识卡片", "练习题"):
        assert f'with gr.Tab("{tab}")' in source
    assert 'include_time=False, type="string"' in source
    assert source.count("gr.Radio([3, 5, 10], value=5") == 2
    assert "from app.learning_repository" not in source


def _binding(name):
    return [fn for fn in app.demo.fns.values() if getattr(fn.fn, "__name__", "") == name]


def test_actual_auth_batch_and_delete_bindings_have_exact_component_contracts():
    for name in ("login_user", "register_user", "logout_user"):
        binding = _binding(name)[0]
        assert len(binding.outputs) == 5
        assert binding.outputs[2].multiselect is True
        assert binding.outputs[3].multiselect is True
        assert binding.outputs[4].multiselect is False
    batch = _binding("submit_import_batch")[0]
    assert len(batch.outputs) == 7
    assert batch.outputs[3].multiselect is True
    assert batch.outputs[4].multiselect is True
    assert batch.outputs[5].multiselect is False
    for name in ("delete_current_pdf", "clear_all_pdfs"):
        for binding in _binding(name):
            assert len(binding.outputs) == 6
            assert binding.outputs[1].multiselect is True
            assert binding.outputs[2].multiselect is True
            assert binding.outputs[3].multiselect is False


def test_learning_document_binding_clears_selectors_answers_and_reveal_states():
    binding = _binding("select_learning_document")[0]
    assert len(binding.inputs) == 2 and binding.inputs[1].multiselect is False
    assert len(binding.outputs) == len(app.clear_learning_interaction())
    assert binding.outputs[3].label == "选择卡片"
    assert binding.outputs[4].label == "选择题目"


@pytest.mark.parametrize("name,args", [
    ("refresh_learning_documents", ("token", "One.pdf | doc-1")),
    ("select_learning_document", ("token", "One.pdf | doc-1")),
    ("refresh_learning_cards", ("token", "One.pdf | doc-1")),
    ("refresh_learning_exercises", ("token", "One.pdf | doc-1")),
    ("generate_learning_cards", ("token", "One.pdf | doc-1", 3)),
    ("generate_learning_exercises", ("token", "One.pdf | doc-1", 3, "mixed")),
    ("learning_plan_choices", ("token", "One.pdf | doc-1")),
    ("learning_task_choices", ("token", "One.pdf | doc-1", None)),
    ("clear_learning_interaction", ()),
])
def test_actual_learning_binding_output_arity(assistant, name, args):
    bindings = _binding(name)
    assert bindings
    result = getattr(app, name)(*args)
    for binding in bindings:
        assert len(binding.outputs) == (len(result) if isinstance(result, tuple) else 1)


def test_learning_rejects_multiple_documents(assistant):
    with pytest.raises(gr.Error):
        app.refresh_learning_cards("token", assistant.get_documents())


def test_plan_selectors_have_readable_labels_and_opaque_values(assistant):
    assistant.get_learning_plans = lambda: [{"id": "opaque-plan", "title": "复习计划", "tasks": [{"id": "opaque-task", "title": "阅读", "due_date": "2026-09-05"}]}]
    plans, tasks = app.learning_plan_choices("token", "One.pdf | doc-1")
    assert plans["choices"] == [("复习计划", "opaque-plan")]
    assert tasks["value"] is None
    assert app.learning_task_choices("token", "One.pdf | doc-1", "opaque-plan")["choices"] == [("2026-09-05 · 阅读", "opaque-task")]


def test_mcq_labels_are_answers_not_indices(assistant):
    result = app.select_learning_exercise("token", "One.pdf | doc-1", "mcq-1")
    assert result[1]["choices"] == [("A", 0), ("B", 1), ("C", 2), ("D", 3)]


@pytest.mark.parametrize("value", [True, 3.5, "3", None])
def test_generation_does_not_coerce_invalid_counts(assistant, value):
    with pytest.raises(gr.Error):
        app.generate_learning_cards("token", "One.pdf | doc-1", value)
    with pytest.raises(gr.Error):
        app.generate_learning_exercises("token", "One.pdf | doc-1", value, "mixed")


@pytest.mark.parametrize("value", [True, 1.5, "1", None])
def test_mcq_does_not_coerce_invalid_indices(assistant, value):
    with pytest.raises(gr.Error):
        app.submit_learning_multiple_choice("token", "One.pdf | doc-1", "mcq-1", value)


@pytest.mark.parametrize("value", ["2026-09-05", "2026-09-05T00:00:00", "2026-09-05 00:00:00"])
def test_datetime_component_payload_normalizes_to_date(assistant, value):
    received = []
    assistant.create_learning_plan = lambda title, date, minutes: received.append(date)
    app.create_learning_plan("token", "One.pdf | doc-1", "Plan", value, 30)
    assert received == ["2026-09-05"]
