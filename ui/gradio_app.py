import os
import json
import sys
import atexit
import re
from datetime import datetime
from pathlib import Path

import gradio as gr

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from assistants.document_selection import primary_document_label
from app.database import initialize_database
from app.session import InvalidSessionError, SessionRegistry
from app.storage import UserStorage
from app.import_models import ImportBatchSummary
from app.import_repository import ImportTaskRepository
from app.import_service import ImportTaskService
from app.import_worker import ImportWorkerPool
from hello_agents.memory.rag.errors import sanitize_error_message
from app.migration import LegacyMigrationService
from ui.launch_config import load_launch_config


DATA_ROOT = Path(os.getenv("PDF_ASSISTANT_DATA_DIR", PROJECT_ROOT / "data")).resolve()
session_registry = None
legacy_migration = None
import_repository = None
import_worker_pool = None
_import_workers_started = False
import_service = None


def initialize_app_services() -> None:
    """Create persistent services for the supported script entry point."""

    global session_registry, legacy_migration, import_repository
    global import_worker_pool, import_service
    if session_registry is not None:
        return

    database_path = DATA_ROOT / "app.db"
    initialize_database(database_path)
    session_registry = SessionRegistry(
        db_path=database_path,
        storage=UserStorage(DATA_ROOT),
    )
    legacy_migration = LegacyMigrationService(
        database_path, session_registry.storage, PROJECT_ROOT
    )
    import_repository = ImportTaskRepository(database_path)
    import_worker_pool = ImportWorkerPool(
        import_repository,
        session_registry.runtime_registry,
        session_registry.storage,
    )
    import_service = ImportTaskService(
        session_registry,
        import_repository,
        session_registry.storage,
        import_worker_pool,
    )
    session_registry.runtime_registry.set_import_task_service(import_service)


def _require_assistant(session_token):
    if not session_token:
        raise gr.Error("Please log in first")
    if session_registry is None:
        raise gr.Error(
            "Please log in again; application services are not initialized"
        )
    try:
        return session_registry.get_assistant(session_token)
    except InvalidSessionError as exc:
        raise gr.Error(str(exc))


def _require_session(session_token):
    if not session_token:
        raise gr.Error("Please log in first")
    if session_registry is None:
        raise gr.Error(
            "Please log in again; application services are not initialized"
        )
    try:
        return session_registry.get_session(session_token)
    except InvalidSessionError as exc:
        raise gr.Error(str(exc))


def start_import_workers() -> None:
    """Start the script-owned import workers exactly once.

    Importing this module intentionally only constructs the pool.  The
    supported ``python ui/gradio_app.py`` entry point starts it below, after
    the application module has loaded, and registers its matching shutdown.
    """

    global _import_workers_started
    if _import_workers_started:
        return
    if import_worker_pool is None:
        raise RuntimeError("Import workers have not been initialized")
    import_worker_pool.start()
    atexit.register(import_worker_pool.stop)
    _import_workers_started = True


def _empty_dropdowns():
    return gr.update(choices=[], value=[]), gr.update(choices=[], value=[])


def _empty_learning_dropdown():
    return gr.update(choices=[], value=None)


def _get_current_dropdown_value(session_token):
    """根据 current_document_id 找到下拉框当前值"""

    assistant = _require_assistant(session_token)
    choices = assistant.get_documents()

    current_value = None

    if assistant.current_document_id:
        for choice in choices:
            if choice.endswith(assistant.current_document_id):
                current_value = choice
                break

    return choices, current_value


def register_user(username, password):
    try:
        token = session_registry.register(username or "", password or "")
        session = session_registry.get_session(token)
        choices, current_value = _get_current_dropdown_value(token)
        update = gr.update(choices=choices, value=[current_value] if current_value else [])
        learning_update = gr.update(choices=choices, value=current_value)
        return token, f"Logged in as {session.username}", update, update, learning_update
    except Exception as exc:
        empty_ask, empty_search = _empty_dropdowns()
        return "", f"Register failed: {exc}", empty_ask, empty_search, _empty_learning_dropdown()


def login_user(username, password):
    try:
        token = session_registry.login(username or "", password or "")
        session = session_registry.get_session(token)
        choices, current_value = _get_current_dropdown_value(token)
        update = gr.update(choices=choices, value=[current_value] if current_value else [])
        learning_update = gr.update(choices=choices, value=current_value)
        return token, f"Logged in as {session.username}", update, update, learning_update
    except Exception as exc:
        empty_ask, empty_search = _empty_dropdowns()
        return "", f"Login failed: {exc}", empty_ask, empty_search, _empty_learning_dropdown()


def logout_user(session_token):
    _require_session(session_token)
    session_registry.logout(session_token)
    empty_ask, empty_search = _empty_dropdowns()
    return "", "Logged out", empty_ask, empty_search, _empty_learning_dropdown()


def upload_document(session_token, file):
    """Backward-compatible single-file entry point for asynchronous imports."""

    _require_session(session_token)
    if file is None:
        raise gr.Error("Please select at least one document")
    return submit_import_batch(session_token, [file])


_IMPORT_STATUS_LABELS = {
    "queued": "排队中",
    "running": "执行中",
    "retry_wait": "等待重试",
    "succeeded": "成功",
    "failed": "失败",
}
_IMPORT_STAGE_LABELS = {
    "queued": "排队中",
    "staged": "已暂存",
    "parsing": "解析文档",
    "chunking": "切分文档",
    "embedding": "生成嵌入",
    "persisting": "持久化",
    "committing": "提交结果",
    "succeeded": "已完成",
    "failed": "失败",
}
_CREDENTIAL_ASSIGNMENT_RE = re.compile(
    r"""(?ix)
    ["']?(?:api[_-]?key|access[_-]?token|authorization|auth|token|key|
    password|passwd|pwd|secret|client[_-]?secret|credential)["']?
    \s*[=:]\s*
    (?:"(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*'|[^\s,;}\]\r\n]+)
    """
)
_BEARER_CREDENTIAL_RE = re.compile(
    r"(?i)\b(?:authorization\s*:\s*)?bearer\s+[^\s,;}\]\r\n]+"
)
_URL_USERINFO_RE = re.compile(
    r"(?i)\b([a-z][a-z0-9+.-]*://)[^\s/@:]+:[^\s/@]+@"
)
_WINDOWS_PATH_RE = re.compile(r"(?i)\b[a-z]:[\\/][^\s'\"]+")
_UNIX_PATH_RE = re.compile(r"(?<!\w)/(?:[^/\s'\"]+/)+[^\s'\"]*")


def _format_import_timestamp(value):
    if not value:
        return "-"
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if parsed.tzinfo is not None:
            parsed = parsed.astimezone()
        return parsed.strftime("%Y-%m-%d %H:%M:%S")
    except ValueError:
        return "-"


def _format_import_error(task) -> str:
    private_values = (
        task.user_id,
        task.staged_relative_path,
        task.document_id,
        task.task_id,
        task.batch_id,
    )
    safe = sanitize_error_message(
        task.error_summary or "",
        secrets=private_values,
    )
    for value in private_values:
        if value:
            safe = safe.replace(str(value), "[redacted]")
    safe = _URL_USERINFO_RE.sub(r"\1[credentials-redacted]@", safe)
    safe = _BEARER_CREDENTIAL_RE.sub("[credentials-redacted]", safe)
    safe = _CREDENTIAL_ASSIGNMENT_RE.sub("[credentials-redacted]", safe)
    safe = _WINDOWS_PATH_RE.sub("[路径已脱敏]", safe)
    safe = _UNIX_PATH_RE.sub("[路径已脱敏]", safe)
    return safe[:500]


def format_batch_summary(summary: ImportBatchSummary) -> str:
    return (
        "批量导入进度\n\n"
        f"总数：{summary.total}　排队：{summary.queued}　执行中：{summary.running}　"
        f"等待重试：{summary.retry_wait}　成功：{summary.succeeded}　失败：{summary.failed}"
    )


def format_task_table(summary: ImportBatchSummary) -> list[list[object]]:
    rows: list[list[object]] = []
    for task in summary.tasks:
        rows.append(
            [
                task.original_name,
                _IMPORT_STATUS_LABELS.get(task.status, "未知"),
                _IMPORT_STAGE_LABELS.get(task.stage, "未知"),
                int(task.progress),
                int(task.total_attempt_count),
                _format_import_timestamp(task.next_attempt_at),
                _format_import_error(task),
            ]
        )
    return rows


def _batch_choice(summary: ImportBatchSummary):
    label = (
        f"{_format_import_timestamp(summary.created_at)} - "
        f"{summary.succeeded}/{summary.total} succeeded"
    )
    return (label, summary.batch_id)


def submit_import_batch(session_token, files, progress=gr.Progress()):
    """Stage a batch for background import after authenticating the session."""

    _require_session(session_token)
    if not files:
        raise gr.Error("Please select at least one document")
    try:
        summary = import_service.submit_batch(
            session_token,
            files,
            progress=progress,
        )
    except (ValueError, KeyError) as exc:
        raise gr.Error(str(exc))
    document_update = gr.update()
    learning_update = gr.update()
    try:
        choices, current_value = _get_current_dropdown_value(session_token)
        document_update = gr.update(
            choices=choices,
            value=[current_value] if current_value else [],
        )
        learning_update = gr.update(choices=choices, value=current_value)
    except gr.Error:
        raise
    return (
        summary.batch_id,
        format_batch_summary(summary),
        format_task_table(summary),
        document_update,
        document_update,
        learning_update,
        "",
    )


def refresh_import_batches(session_token):
    if not session_token:
        return gr.update(choices=[], value=None), "", [], ""
    _require_session(session_token)
    batches = import_service.list_batches(session_token, limit=50)
    if not batches:
        return gr.update(choices=[], value=None), "", [], ""
    selected = batches[0]
    return (
        gr.update(
            choices=[_batch_choice(batch) for batch in batches],
            value=selected.batch_id,
        ),
        format_batch_summary(selected),
        format_task_table(selected),
        "",
    )


def clear_import_ui():
    return gr.update(choices=[], value=None), "", [], ""


def refresh_import_batch(session_token, batch_id, selected_task=""):
    if not session_token:
        return "", [], ""
    _require_session(session_token)
    if not batch_id:
        return "", [], ""
    try:
        summary = import_service.get_batch(session_token, batch_id)
    except KeyError as exc:
        raise gr.Error(str(exc))
    return (
        format_batch_summary(summary),
        format_task_table(summary),
        _retain_import_task_selection(summary, batch_id, selected_task),
    )


def select_import_task(session_token, batch_id, evt: gr.SelectData):
    """Resolve a selected failed row to its server-owned opaque task ID."""

    if not session_token:
        return ""
    _require_session(session_token)
    if not batch_id:
        return ""
    try:
        summary = import_service.get_batch(session_token, batch_id)
    except KeyError as exc:
        raise gr.Error(str(exc))
    row_index = evt.index[0] if isinstance(evt.index, (list, tuple)) else evt.index
    if not isinstance(row_index, int) or not 0 <= row_index < len(summary.tasks):
        return ""
    task = summary.tasks[row_index]
    return (batch_id, task.task_id) if task.status == "failed" else ""


def retry_import_task(session_token, batch_id, selected_task=""):
    _require_session(session_token)
    selection = _import_task_selection(selected_task)
    if selection is None:
        raise gr.Error("Please select a failed task")
    selected_batch_id, task_id = selection
    if not batch_id or selected_batch_id != batch_id:
        raise gr.Error("Selected task does not belong to the visible batch")
    try:
        summary = import_service.retry_task(
            session_token, task_id, expected_batch_id=batch_id
        )
    except (ValueError, KeyError) as exc:
        raise gr.Error(str(exc))
    return format_batch_summary(summary), format_task_table(summary), ""


def retry_import_batch_failures(session_token, batch_id):
    _require_session(session_token)
    if not batch_id:
        raise gr.Error("Please select an import batch")
    try:
        summary = import_service.retry_failed_in_batch(session_token, batch_id)
    except (ValueError, KeyError) as exc:
        raise gr.Error(str(exc))
    return format_batch_summary(summary), format_task_table(summary), ""


def _import_task_selection(value):
    if not isinstance(value, (list, tuple)) or len(value) != 2:
        return None
    batch_id, task_id = value
    if not isinstance(batch_id, str) or not isinstance(task_id, str):
        return None
    if not batch_id or not task_id:
        return None
    return batch_id, task_id


def _retain_import_task_selection(summary, batch_id, selected_task):
    selection = _import_task_selection(selected_task)
    if selection is None or selection[0] != batch_id:
        return ""
    selected_task_id = selection[1]
    if any(
        task.task_id == selected_task_id and task.status == "failed"
        for task in summary.tasks
    ):
        return selection
    return ""


def refresh_documents(session_token):
    """刷新 文档 下拉列表"""

    choices, current_value = _get_current_dropdown_value(session_token)

    return gr.update(
        choices=choices,
        value=[current_value] if current_value else []
    )


def select_document(session_token, selected):
    """切换当前 文档"""

    assistant = _require_assistant(session_token)

    try:
        selected = primary_document_label(selected)
    except ValueError:
        return "❌ 切换当前文档只能选择 1 篇文档"

    return assistant.select_document(selected)


def delete_current_pdf(session_token, selected=None):
    """删除当前 文档，并刷新两个下拉框"""

    # 如果用户在下拉框里选了 文档，先切换为当前 文档
    assistant = _require_assistant(session_token)

    if selected:
        try:
            selected = primary_document_label(selected)
        except ValueError:
            return (
                "❌ 删除当前文档只能选择 1 篇文档",
                gr.update(),
                gr.update(),
                gr.update(),
                "",
                "",
            )
        assistant.select_document(selected)

    result = assistant.delete_current_document()

    choices, current_value = _get_current_dropdown_value(session_token)

    dropdown_update = gr.update(
        choices=choices,
        value=[current_value] if current_value else []
    )
    learning_update = gr.update(choices=choices, value=current_value)

    empty_status = ""

    return (
        result,
        dropdown_update,
        dropdown_update,
        learning_update,
        empty_status,
        empty_status,
    )

def clear_all_pdfs(session_token):
    """清空全部 文档，并刷新两个下拉框"""

    assistant = _require_assistant(session_token)
    result = assistant.clear_all_documents()

    # 清空后，下拉框应该为空
    choices, current_value = _get_current_dropdown_value(session_token)

    dropdown_update = gr.update(
        choices=choices,
        value=[current_value] if current_value else []
    )
    learning_update = gr.update(choices=choices, value=current_value)

    empty_status = ""

    return (
        result,
        dropdown_update,
        dropdown_update,
        learning_update,
        empty_status,
        empty_status,
    )


def _select_learning_document(assistant, selected):
    if not isinstance(selected, str) or not selected.strip():
        raise gr.Error("请选择一个学习文档")
    try:
        label = primary_document_label(selected)
    except ValueError as exc:
        raise gr.Error("Please select one learning document") from exc
    try:
        assistant.select_document(label)
    except (ValueError, KeyError) as exc:
        raise gr.Error("学习文档不可用，请刷新后重试") from exc
    return label


def refresh_learning_documents(session_token, selected=None):
    assistant = _require_assistant(session_token)
    choices = assistant.get_documents()
    value = selected if selected in choices else None
    if value is None and assistant.current_document_id:
        value = next(
            (choice for choice in choices if choice.endswith(assistant.current_document_id)),
            None,
        )
    return (gr.update(choices=choices, value=value), *clear_learning_interaction())


def select_learning_document(session_token, selected):
    assistant = _require_assistant(session_token)
    if selected:
        _select_learning_document(assistant, selected)
    return clear_learning_interaction()


def clear_learning_interaction():
    empty = lambda: gr.update(choices=[], value=None)
    return ("", "", "", empty(), empty(), "", "", "", "", "", "",
            "", "", "", "", "", "", [], [], [], [], empty(), empty(),
            "", None, 30, False, empty(), "")


def learning_plan_choices(session_token, selected):
    assistant = _require_assistant(session_token)
    _select_learning_document(assistant, selected)
    plans = assistant.get_learning_plans()
    return gr.update(choices=[(p["title"], p["id"]) for p in plans], value=None), gr.update(choices=[], value=None)


def learning_task_choices(session_token, selected, plan_id):
    assistant = _require_assistant(session_token)
    _select_learning_document(assistant, selected)
    plan = next((p for p in assistant.get_learning_plans() if p["id"] == plan_id), None)
    return gr.update(choices=[(f"{t['due_date']} · {t['title']}", t["id"]) for t in plan["tasks"]] if plan else [], value=None)


def _source_text(sources):
    if not sources:
        return "暂无来源"
    lines = []
    for source in sources:
        location = (
            f"第 {source['page_number']} 页"
            if source.get("page_number") is not None
            else source.get("source_position") or "位置未知"
        )
        lines.append(
            f"- {source.get('document_name', '')} · {location}\n  {source.get('preview', '')}"
        )
    return "\n".join(lines)


def _plan_rows(plans):
    rows = []
    for plan in plans:
        tasks = plan.get("tasks", [])
        completed = sum(bool(task.get("completed")) for task in tasks)
        rows.append([plan["title"], plan["target_date"], plan["daily_minutes"], plan["status"], f"{completed}/{len(tasks)}", plan["id"]])
    return rows


def _task_rows(plans, tasks=None):
    plan_by_task = {
        task["id"]: plan for plan in plans for task in plan.get("tasks", [])
    }
    selected_tasks = tasks if tasks is not None else list(plan_by_task)
    if tasks is None:
        items = [task for plan in plans for task in plan.get("tasks", [])]
    else:
        items = tasks
    return [[task["due_date"], task["phase"], task["title"], task["duration_minutes"], "是" if task.get("completed") else "否", plan_by_task.get(task["id"], {}).get("id", ""), task["id"]] for task in items]


def refresh_learning_dashboard(session_token, selected):
    assistant = _require_assistant(session_token)
    _select_learning_document(assistant, selected)
    dashboard = assistant.get_learning_dashboard()
    summary = (
        f"完成率：{dashboard['completion_rate']:.0%}　"
        f"今日任务：{len(dashboard['today_tasks'])}　"
        f"到期复习：{len(dashboard['due_cards']) + len(dashboard['due_exercises'])}"
    )
    tasks = dashboard["overdue_tasks"] + dashboard["today_tasks"]
    due = [
        ["卡片", item["front"], item["schedule"]["due_at"], item["id"]]
        for item in dashboard["due_cards"]
    ] + [
        ["练习", item["prompt"], item["schedule"]["due_at"], item["id"]]
        for item in dashboard["due_exercises"]
    ]
    return summary, _task_rows(dashboard["plans"], tasks), due


def refresh_learning_plans(session_token, selected):
    assistant = _require_assistant(session_token)
    _select_learning_document(assistant, selected)
    plans = assistant.get_learning_plans()
    return _plan_rows(plans), _task_rows(plans), ""


def create_learning_plan(session_token, selected, title, target_date, daily_minutes):
    assistant = _require_assistant(session_token)
    _select_learning_document(assistant, selected)
    try:
        if isinstance(target_date, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?", target_date):
            target_date = datetime.fromisoformat(target_date).date().isoformat()
        assistant.create_learning_plan(title, target_date, daily_minutes)
        plans = assistant.get_learning_plans()
    except (ValueError, TypeError) as exc:
        raise gr.Error("创建失败，请检查计划名称、目标日期和每日分钟数") from exc
    return "计划已创建", _plan_rows(plans), _task_rows(plans)


def set_learning_task_completed(session_token, selected, plan_id, task_id, completed):
    assistant = _require_assistant(session_token)
    _select_learning_document(assistant, selected)
    try:
        assistant.set_learning_task_completed(plan_id, task_id, bool(completed))
        plans = assistant.get_learning_plans()
    except (ValueError, KeyError) as exc:
        raise gr.Error("任务不可用，请刷新计划后重试") from exc
    return "任务已更新", _plan_rows(plans), _task_rows(plans)


def _card_choices(cards):
    return [(card["front"], card["id"]) for card in cards]


def refresh_learning_cards(session_token, selected):
    assistant = _require_assistant(session_token)
    _select_learning_document(assistant, selected)
    return gr.update(choices=_card_choices(assistant.get_learning_cards()), value=None), "", "", ""


def generate_learning_cards(session_token, selected, count):
    assistant = _require_assistant(session_token)
    _select_learning_document(assistant, selected)
    if type(count) is not int or count not in (3, 5, 10):
        raise gr.Error("生成数量必须是 3、5 或 10")
    try:
        assistant.generate_learning_cards(count)
        cards = assistant.get_learning_cards()
    except (ValueError, TypeError) as exc:
        raise gr.Error("卡片生成失败，请检查文档内容与生成数量后重试") from exc
    return f"已生成 {int(count)} 张卡片", gr.update(choices=_card_choices(cards), value=None), "", "", ""


def select_learning_card(session_token, selected, card_id):
    assistant = _require_assistant(session_token)
    if not card_id:
        return "", "", "", ""
    _select_learning_document(assistant, selected)
    card = next((item for item in assistant.get_learning_cards() if item["id"] == card_id), None)
    if card is None:
        raise gr.Error("Card not found for learning document")
    return card["front"], "", "", ""


def reveal_learning_card(session_token, selected, card_id):
    assistant = _require_assistant(session_token)
    _select_learning_document(assistant, selected)
    card = next((item for item in assistant.get_learning_cards() if item["id"] == card_id), None)
    if card is None:
        raise gr.Error("Card not found for learning document")
    return card["back"], _source_text(card["sources"]), card["id"]


def review_learning_card(session_token, selected, card_id, revealed_card_id, rating):
    assistant = _require_assistant(session_token)
    _select_learning_document(assistant, selected)
    if not card_id or card_id != revealed_card_id:
        raise gr.Error("Reveal this exact card before rating it")
    try:
        card = assistant.review_learning_card(card_id, rating)
    except (ValueError, KeyError) as exc:
        raise gr.Error("卡片评分失败，请重新选择并查看答案") from exc
    return f"下次复习：{card['schedule']['due_at']}", ""


def _exercise_choices(exercises):
    return [(exercise["prompt"], exercise["id"]) for exercise in exercises]


def refresh_learning_exercises(session_token, selected):
    assistant = _require_assistant(session_token)
    _select_learning_document(assistant, selected)
    return gr.update(choices=_exercise_choices(assistant.get_learning_exercises()), value=None), "", gr.update(choices=[], value=None), "", "", ""


def generate_learning_exercises(session_token, selected, count, kind):
    assistant = _require_assistant(session_token)
    _select_learning_document(assistant, selected)
    if type(count) is not int or count not in (3, 5, 10):
        raise gr.Error("生成数量必须是 3、5 或 10")
    try:
        assistant.generate_learning_exercises(count, kind or "mixed")
        exercises = assistant.get_learning_exercises()
    except (ValueError, TypeError) as exc:
        raise gr.Error("练习生成失败，请检查文档内容与生成数量后重试") from exc
    return f"已生成 {int(count)} 道练习", gr.update(choices=_exercise_choices(exercises), value=None), "", gr.update(choices=[], value=None), "", "", ""


def select_learning_exercise(session_token, selected, exercise_id):
    assistant = _require_assistant(session_token)
    if not exercise_id:
        return "", gr.update(choices=[], value=None), "", "", ""
    _select_learning_document(assistant, selected)
    exercise = next((item for item in assistant.get_learning_exercises() if item["id"] == exercise_id), None)
    if exercise is None:
        raise gr.Error("Exercise not found for learning document")
    options = exercise.get("options", []) if exercise["type"] == "multiple_choice" else []
    return exercise["prompt"], gr.update(choices=[(text, index) for index, text in enumerate(options)], value=None), "", "", ""


def submit_learning_multiple_choice(session_token, selected, exercise_id, selected_index):
    assistant = _require_assistant(session_token)
    _select_learning_document(assistant, selected)
    if type(selected_index) is not int:
        raise gr.Error("Please select an answer")
    try:
        result = assistant.answer_learning_multiple_choice(exercise_id, selected_index)
    except (ValueError, TypeError, KeyError) as exc:
        raise gr.Error("答案提交失败，请刷新题目后重试") from exc
    exercise = result["exercise"]
    status = "Correct" if result["correct"] else f"Incorrect; correct answer: {exercise['options'][result['correct_index']]}"
    return f"{status}\n\n{result['explanation']}\n\n{_source_text(exercise['sources'])}\n\nNext review: {exercise['schedule']['due_at']}"


def reveal_learning_short_answer(session_token, selected, exercise_id, user_answer):
    assistant = _require_assistant(session_token)
    _select_learning_document(assistant, selected)
    exercise = next((item for item in assistant.get_learning_exercises() if item["id"] == exercise_id), None)
    if exercise is None or exercise["type"] != "short_answer":
        raise gr.Error("Short-answer exercise not found for learning document")
    if not str(user_answer or "").strip():
        raise gr.Error("Please enter your answer")
    reference = f"参考答案：{exercise['reference_answer']}\n\n关键点：{'、'.join(exercise['key_points'])}\n\n{_source_text(exercise['sources'])}"
    return reference, exercise["id"]


def review_learning_short_answer(session_token, selected, exercise_id, revealed_exercise_id, rating, user_answer):
    assistant = _require_assistant(session_token)
    _select_learning_document(assistant, selected)
    if not exercise_id or exercise_id != revealed_exercise_id:
        raise gr.Error("Reveal this exact reference answer before rating it")
    try:
        exercise = assistant.review_learning_short_answer(exercise_id, rating, user_answer)
    except (ValueError, KeyError) as exc:
        raise gr.Error("练习评分失败，请重新选择并查看参考答案") from exc
    return f"下次复习：{exercise['schedule']['due_at']}", ""


def _legacy_ask_pdf_unused(question, selected_pdf=None):
    """文档 问答：如果下拉框选择了 文档，则先切换当前 文档"""

    if not question or not question.strip():
        return "❌ 请输入问题"

    if selected_pdf:
        assistant.select_document(selected_pdf)

    if not assistant.current_document_id:
        return "❌ 当前没有选择 文档。请先在下拉框中选择一个 文档，并点击“切换当前文档”。"

    try:
        return assistant.ask(question, limit=5)
    except Exception as e:
        return f"❌ 文档 问答失败: {e}"


def _legacy_search_pdf_unused(query, selected_pdf=None):
    """文档检索：如果下拉框选择了 文档，则先切换当前 文档，并返回调试信息"""

    if not query or not query.strip():
        return "❌ 请输入检索内容"

    if selected_pdf:
        select_result = assistant.select_document(selected_pdf)
    else:
        select_result = "⚠️ 当前没有从下拉框传入 文档"

    if not assistant.current_document_id:
        return (
            "❌ 当前没有选择 文档。\n"
            "请先在下拉框中选择一个 文档，并点击“切换当前文档”。\n\n"
            f"当前下拉框值: {selected_pdf}\n"
            f"当前 document_id: {assistant.current_document_id}"
        )

    try:
        result = assistant.search(query, limit=5)

        if result is None:
            result = ""

        result = str(result)

        if not result.strip():
            return (
                "⚠️ 检索函数执行了，但返回内容为空。\n\n"
                f"当前关键词: {query}\n"
                f"当前下拉框值: {selected_pdf}\n"
                f"当前 document_id: {assistant.current_document_id}\n"
                f"切换结果:\n{select_result}\n\n"
                "建议检查 pdf_learning_assistant.py 中的 search() 方法是否有 return。"
            )

        return (
            f"📌 当前检索 文档:\n"
            f"- selected_pdf: {selected_pdf}\n"
            f"- document_id: {assistant.current_document_id}\n\n"
            f"{result}"
        )

    except Exception as e:
        return (
            f"❌ 文档检索失败: {e}\n\n"
            f"当前关键词: {query}\n"
            f"当前下拉框值: {selected_pdf}\n"
            f"当前 document_id: {assistant.current_document_id}"
        )

def ask_pdf(session_token, question, selected_pdf=None, qa_mode="auto"):
    """Document QA for one or more selected documents."""

    assistant = _require_assistant(session_token)

    if not question or not question.strip():
        return "❌ 请输入问题"

    try:
        return assistant.ask(
            question,
            limit=5,
            selected_documents=selected_pdf,
            mode=qa_mode or "auto",
            structured_output=(qa_mode == "compare"),
        )
    except Exception as e:
        return f"❌ 文档问答失败: {e}"


def _format_answer_sources(sources):
    if not sources:
        return "暂无可定位来源"
    grouped = {}
    for source in sources:
        document_id = source.get("document_id") or "unknown"
        grouped.setdefault(document_id, []).append(source)
    lines = []
    for document_id, items in grouped.items():
        lines.append(f"## {document_id}")
        for item in items:
            lines.append(item.get("reference") or f"[{item.get('citation_id', '')}]")
            if item.get("file_name"):
                lines.append(f"文件: {item['file_name']}")
            if item.get("excerpt"):
                lines.append(f"原文片段: {item['excerpt']}")
            lines.append("")
    return "\n".join(lines).strip()


def ask_pdf_with_sources(session_token, question, selected_pdf=None, qa_mode="auto"):
    answer = ask_pdf(session_token, question, selected_pdf, qa_mode)
    try:
        assistant = _require_assistant(session_token)
        data = dict(getattr(assistant.rag_tool, "_last_action_data", {}) or {})
        return answer, _format_answer_sources(data.get("sources") or [])
    except Exception:
        return answer, "暂无可定位来源"


def _format_summary_task(task):
    status = task.get("status", "unknown")
    completed = task.get("completed", 0)
    total = task.get("total", 0)
    stage = task.get("stage", "")
    lines = [
        f"任务状态: {status}",
        f"进度: {completed}/{total}",
        f"阶段: {stage}",
    ]
    if task.get("current_document_id"):
        lines.append(f"当前文档: {task['current_document_id']}")
    if task.get("error"):
        lines.append(f"错误: {task['error']}")
    if task.get("result"):
        lines.extend(["", task["result"]])
    return "\n".join(lines)


def start_summary_pdf(session_token, question, selected_pdf=None):
    assistant = _require_assistant(session_token)
    try:
        task = assistant.start_summary_task(
            question,
            selected_documents=selected_pdf or [],
            limit=5,
        )
        return task["task_id"], _format_summary_task(task)
    except Exception as error:
        return "", f"后台总结启动失败: {error}"


def poll_summary_pdf(session_token, task_id):
    assistant = _require_assistant(session_token)
    if not task_id:
        return "当前没有后台总结任务"
    try:
        return _format_summary_task(assistant.get_summary_task(task_id))
    except Exception as error:
        return f"查询总结任务失败: {error}"


def cancel_summary_pdf(session_token, task_id):
    assistant = _require_assistant(session_token)
    if not task_id:
        return "当前没有可取消的后台总结任务"
    try:
        return _format_summary_task(assistant.cancel_summary_task(task_id))
    except Exception as error:
        return f"取消总结任务失败: {error}"


def start_summary_pdf_auto(session_token, question, selected_pdf=None):
    task_id, output = start_summary_pdf(session_token, question, selected_pdf)
    return task_id, output, gr.update(active=bool(task_id))


def poll_summary_pdf_auto(session_token, task_id):
    assistant = _require_assistant(session_token)
    if not task_id:
        return "当前没有后台总结任务", gr.update(active=False)
    try:
        task = assistant.get_summary_task(task_id)
        active = task.get("status") not in {"completed", "failed", "cancelled"}
        return _format_summary_task(task), gr.update(active=active)
    except Exception as error:
        return f"查询总结任务失败: {error}", gr.update(active=False)


def cancel_summary_pdf_auto(session_token, task_id):
    output = cancel_summary_pdf(session_token, task_id)
    return output, gr.update(active=False)


def search_pdf(session_token, query, selected_pdf=None):
    """Document search for one or more selected documents."""

    assistant = _require_assistant(session_token)

    if not query or not query.strip():
        return "❌ 请输入检索内容"

    try:
        result = assistant.search(
            query,
            limit=5,
            selected_documents=selected_pdf,
        )
        return (
            f"📌 当前检索文档:\n"
            f"- selected_pdf: {selected_pdf}\n\n"
            f"{result}"
        )
    except Exception as e:
        return f"❌ 文档检索失败: {e}"


def generate_citations(session_token, query, selected_pdf=None):
    """根据关键词生成可复制引用格式"""

    assistant = _require_assistant(session_token)

    if not query or not query.strip():
        return "❌ 请输入检索关键词"

    if selected_pdf:
        try:
            selected_pdf = primary_document_label(selected_pdf)
        except ValueError:
            return "❌ 生成引用只能选择 1 篇文档"
        assistant.select_document(selected_pdf)

    if not assistant.current_document_id:
        return "❌ 当前没有选择 文档。请先在下拉框中选择一个 文档。"

    try:
        result = assistant.rag_tool.execute(
            "citation",
            query=query,
            limit=5,
            min_score=0.0,
            document_id=assistant.current_document_id
        )

        return (
            f"📌 当前引用 文档:\n"
            f"- selected_pdf: {selected_pdf}\n"
            f"- document_id: {assistant.current_document_id}\n\n"
            f"{result}"
        )

    except Exception as e:
        return (
            f"❌ 生成引用格式失败: {e}\n\n"
            f"当前关键词: {query}\n"
            f"当前下拉框值: {selected_pdf}\n"
            f"当前 document_id: {assistant.current_document_id}"
        )


def add_note(session_token, note, concept):
    assistant = _require_assistant(session_token)

    if not note or not note.strip():
        return "❌ 请输入学习笔记"

    return assistant.add_note(note=note, concept=concept)


def clear_all_notes(session_token):
    """清空全部学习笔记"""

    assistant = _require_assistant(session_token)

    try:
        return assistant.clear_all_notes()
    except Exception as e:
        return f"❌ 清空学习笔记失败: {e}"


def recall_memory(session_token, query):
    assistant = _require_assistant(session_token)

    if not query or not query.strip():
        return "❌ 请输入要回忆的内容"

    return assistant.recall(query, limit=5)


def show_stats(session_token):
    assistant = _require_assistant(session_token)
    return assistant.get_stats()


def generate_report(session_token):
    assistant = _require_assistant(session_token)
    return assistant.generate_report()

def _report_id(selected):
    if not selected:
        raise gr.Error("Please select a report")
    return str(selected).split("|")[-1].strip()


def list_reports(session_token):
    assistant = _require_assistant(session_token)
    return gr.update(choices=[
        f"{record.created_at} - {record.title} | {record.id}"
        for record in assistant.report_service.list_reports(assistant.user_id)
    ])


def view_report(session_token, selected):
    assistant = _require_assistant(session_token)
    return assistant.report_service.read_report(assistant.user_id, _report_id(selected))


def download_report_markdown(session_token, selected):
    assistant = _require_assistant(session_token)
    return str(assistant.report_service.report_file_path(
        assistant.user_id, _report_id(selected)
    ))


def export_report_docx(session_token, selected=None):
    assistant = _require_assistant(session_token)
    path = assistant.export_report_docx(_report_id(selected) if selected else None)
    return path

def export_report_markdown(session_token):
    assistant = _require_assistant(session_token)
    path = assistant.export_report_markdown()
    return path


def scan_legacy_data(session_token):
    _require_assistant(session_token)
    return json.dumps(legacy_migration.scan(), ensure_ascii=False, indent=2)


def claim_legacy_data(session_token):
    assistant = _require_assistant(session_token)
    return str(legacy_migration.claim(assistant.user_id))


def _get_recovery(session_token):
    """Return the RecoveryService for the authenticated session."""
    assistant = _require_assistant(session_token)
    if assistant.runtime is None:
        raise gr.Error("Recovery not available: no runtime configured")
    return assistant.runtime.recovery


def check_corruption_status(session_token):
    """Inspect History and Memory for corruption."""
    recovery = _get_recovery(session_token)
    history = recovery.check_history()
    memory = recovery.check_memory()
    lines = []
    for r in (history, memory):
        icon = "✅" if r.success else "❌"
        lines.append(f"{icon} {r.target}: {r.message}")
    return "\n\n".join(lines)


def quarantine_history(session_token):
    """Quarantine corrupt History and create a clean active file."""
    recovery = _get_recovery(session_token)
    result = recovery.quarantine_history()
    return f"{'✅' if result.success else '❌'} {result.message}"


def quarantine_memory(session_token):
    """Quarantine corrupt Memory snapshot and create a clean active file."""
    recovery = _get_recovery(session_token)
    result = recovery.quarantine_memory()
    return f"{'✅' if result.success else '❌'} {result.message}"


def list_recovery_backups(session_token):
    """Refresh the backup dropdown with available opaque backup IDs."""
    recovery = _get_recovery(session_token)
    history_backups = recovery.list_history_backups()
    memory_backups = recovery.list_memory_backups()
    all_backups = history_backups + memory_backups
    return gr.update(choices=all_backups, value=[] if not all_backups else None)


def restore_history(session_token, backup_id):
    """Validate and restore a History backup by opaque ID."""
    if not backup_id:
        return "❌ Please select a backup first"
    recovery = _get_recovery(session_token)
    try:
        result = recovery.restore_history(backup_id)
    except (ValueError, FileNotFoundError) as exc:
        return f"❌ Invalid backup: {exc}"
    return f"{'✅' if result.success else '❌'} {result.message}"


def restore_memory(session_token, backup_id):
    """Validate and restore a Memory backup by opaque ID."""
    if not backup_id:
        return "❌ Please select a backup first"
    recovery = _get_recovery(session_token)
    try:
        result = recovery.restore_memory(backup_id)
    except (ValueError, FileNotFoundError) as exc:
        return f"❌ Invalid backup: {exc}"
    return f"{'✅' if result.success else '❌'} {result.message}"

with gr.Blocks(title="文档 智能学习助手") as demo:
    session_token = gr.State("")
    with gr.Tab("登录 / 注册"):
        username_input = gr.Textbox(label="用户名")
        password_input = gr.Textbox(label="密码", type="password")
        with gr.Row():
            login_btn = gr.Button("登录")
            register_btn = gr.Button("注册")
            logout_btn = gr.Button("退出登录")
        auth_status = gr.Textbox(label="登录状态", interactive=False)
    gr.Markdown("# 📘 智能文档学习助手")
    gr.Markdown("支持 文档 / TXT / Markdown / Word 导入、文档问答、学习笔记、记忆回忆和学习统计。")

    # =========================
    # 1. 上传文档
    # =========================
    with gr.Tab("1. 上传文档"):
        import_files = gr.File(
            label="批量上传文档",
            file_count="multiple",
            type="filepath",
            file_types=[".pdf", ".txt", ".md", ".docx"]
        )

        submit_import_btn = gr.Button("提交导入")

        import_batch_dropdown = gr.Dropdown(
            label="最近批次",
            choices=[],
            interactive=True,
        )
        import_summary = gr.Markdown()
        import_tasks = gr.Dataframe(
            headers=["文件名", "状态", "阶段", "进度", "尝试次数", "下次重试", "错误"],
            datatype=["str", "str", "str", "number", "number", "str", "str"],
            interactive=False,
        )
        submitted_import_batch_id = gr.State("")
        selected_import_task_id = gr.State("")
        with gr.Row():
            retry_selected_btn = gr.Button("重试所选失败项")
            retry_batch_btn = gr.Button("重试本批次全部失败项")
            refresh_import_btn = gr.Button("手动刷新")
        import_timer = gr.Timer(value=1, active=True)

    # =========================
    # 2. 文档 问答
    # =========================
    with gr.Tab("2. 文档问答"):
        doc_dropdown_ask = gr.Dropdown(
            label="请选择当前文档",
            choices=[],
            multiselect=True,
            max_choices=10,
            interactive=True
        )

        qa_mode_radio = gr.Radio(
            label="问答模式",
            choices=[
                ("自动", "auto"),
                ("联合问答", "joint"),
                ("对比分析", "compare"),
                ("联合总结", "summary"),
            ],
            value="auto",
            interactive=True,
        )

        select_doc_btn_ask = gr.Button("切换当前文档")

        select_doc_output_ask = gr.Textbox(
            label="当前文档状态",
            lines=4
        )

        refresh_doc_btn_ask = gr.Button("刷新文档列表")

        delete_doc_btn_ask = gr.Button("删除当前文档")

        clear_all_doc_btn_ask = gr.Button("清空全部文档")

        delete_doc_output_ask = gr.Textbox(
            label="删除 / 清空结果",
            lines=8
        )

        question_input = gr.Textbox(
            label="请输入问题",
            lines=3,
            placeholder="例如：这个 文档 主要讲了什么？"
        )

        ask_btn = gr.Button("开始问答")

        answer_output = gr.Textbox(
            label="回答结果",
            lines=15
        )

        answer_sources_output = gr.Textbox(
            label="来源定位与可复制引用",
            lines=10,
        )
        summary_task_id = gr.State("")
        summary_poll_timer = gr.Timer(1.0, active=False)
        with gr.Row():
            start_summary_btn = gr.Button("后台联合总结")
            poll_summary_btn = gr.Button("刷新总结进度")
            cancel_summary_btn = gr.Button("取消后台总结")
        summary_task_output = gr.Textbox(
            label="后台总结任务",
            lines=8,
        )

        select_doc_btn_ask.click(
            fn=select_document,
            inputs=[session_token, doc_dropdown_ask],
            outputs=select_doc_output_ask
        )

        refresh_doc_btn_ask.click(
            fn=refresh_documents,
            inputs=session_token,
            outputs=doc_dropdown_ask
        )

        ask_btn.click(
            fn=ask_pdf_with_sources,
            inputs=[session_token, question_input, doc_dropdown_ask, qa_mode_radio],
            outputs=[answer_output, answer_sources_output]
        )

        start_summary_btn.click(
            fn=start_summary_pdf_auto,
            inputs=[session_token, question_input, doc_dropdown_ask],
            outputs=[summary_task_id, summary_task_output, summary_poll_timer],
        )

        poll_summary_btn.click(
            fn=poll_summary_pdf,
            inputs=[session_token, summary_task_id],
            outputs=summary_task_output,
        )

        cancel_summary_btn.click(
            fn=cancel_summary_pdf_auto,
            inputs=[session_token, summary_task_id],
            outputs=[summary_task_output, summary_poll_timer],
        )

        summary_poll_timer.tick(
            fn=poll_summary_pdf_auto,
            inputs=[session_token, summary_task_id],
            outputs=[summary_task_output, summary_poll_timer],
            show_progress="hidden",
        )

    # =========================
    # 3. 文档检索
    # =========================
    with gr.Tab("3.文献检索"):
        doc_dropdown_search = gr.Dropdown(
            label="请选择当前文档",
            choices=[],
            multiselect=True,
            max_choices=10,
            interactive=True
        )

        select_doc_btn_search = gr.Button("切换当前文档")

        select_doc_output_search = gr.Textbox(
            label="当前 文档 状态",
            lines=4
        )

        refresh_doc_btn_search = gr.Button("刷新文档列表")

        delete_doc_btn_search = gr.Button("删除当前文档")

        clear_all_doc_btn_search = gr.Button("清空全部文档")

        delete_doc_output_search = gr.Textbox(
            label="删除 / 清空结果",
            lines=8
        )

        search_input = gr.Textbox(
            label="搜索关键词",
            lines=2,
            placeholder="例如：自由、LLM、模型下载、SFT"
        )

        search_btn = gr.Button("搜索")

        search_output = gr.Textbox(
            label="检索结果",
            lines=15
        )

        citation_btn = gr.Button("生成引用格式")

        citation_output = gr.Textbox(
            label="可复制引用格式",
            lines=15
        )

        select_doc_btn_search.click(
            fn=select_document,
            inputs=[session_token, doc_dropdown_search],
            outputs=select_doc_output_search
        )

        refresh_doc_btn_search.click(
            fn=refresh_documents,
            inputs=session_token,
            outputs=doc_dropdown_search
        )

        search_btn.click(
            fn=search_pdf,
            inputs=[session_token, search_input, doc_dropdown_search],
            outputs=search_output
        )

        citation_btn.click(
            fn=generate_citations,
            inputs=[session_token, search_input, doc_dropdown_search],
            outputs=citation_output
        )

    # =========================
    # 4. 学习笔记
    # =========================
    with gr.Tab("4.学习笔记"):
        concept_input = gr.Textbox(
            label="概念名称，可选",
            placeholder="例如：RAG、LLM、亲子沟通"
        )

        note_input = gr.Textbox(
            label="学习笔记",
            lines=5,
            placeholder="写下你对这个概念的理解"
        )

        note_btn = gr.Button("保存笔记")

        clear_notes_btn = gr.Button("清空全部学习笔记")

        note_output = gr.Textbox(
            label="保存 / 清空结果",
            lines=8
        )

        note_btn.click(
            fn=add_note,
            inputs=[session_token, note_input, concept_input],
            outputs=note_output
        )

        clear_notes_btn.click(
            fn=clear_all_notes,
            inputs=session_token,
            outputs=note_output
        )

    # =========================
    # 5. 记忆回忆
    # =========================
    with gr.Tab("5.记忆回忆"):
        recall_input = gr.Textbox(
            label="你想回忆什么？",
            lines=2,
            placeholder="例如：RAG 核心、亲子沟通技巧"
        )

        recall_btn = gr.Button("回忆")

        recall_output = gr.Textbox(
            label="回忆结果",
            lines=15
        )

        recall_btn.click(
            fn=recall_memory,
            inputs=[session_token, recall_input],
            outputs=recall_output
        )

    # =========================
    # 6. 学习统计
    # =========================
    with gr.Tab("6.学习统计"):
        stats_btn = gr.Button("查看统计")

        stats_output = gr.Textbox(
            label="统计结果",
            lines=18
        )

        stats_btn.click(
            fn=show_stats,
            inputs=session_token,
            outputs=stats_output
        )

    # =========================
    # 7. 学习报告
    # =========================
    with gr.Tab("7.学习报告"):
        report_btn = gr.Button("学习生成报告")

        report_output = gr.Textbox(
            label="学习报告",
            lines=25
        )

        export_report_md_btn = gr.Button("导出学习报告 Markdown")

        report_md_file = gr.File(
            label="下载 Markdown 学习报告"
        )

        export_report_docx_btn = gr.Button("导出学习报告 Word")

        report_docx_file = gr.File(
            label="下载 Word 学习报告"
        )

        report_history = gr.Dropdown(label="Report history", choices=[])
        refresh_reports_btn = gr.Button("Refresh reports")
        view_report_btn = gr.Button("View snapshot")
        download_snapshot_btn = gr.Button("Download Markdown snapshot")

        report_btn.click(
            fn=generate_report,
            inputs=session_token,
            outputs=report_output
        )

        export_report_md_btn.click(
            fn=export_report_markdown,
            inputs=session_token,
            outputs=report_md_file
        )

        export_report_docx_btn.click(
            fn=export_report_docx,
            inputs=[session_token, report_history],
            outputs=report_docx_file
        )
        refresh_reports_btn.click(
            fn=list_reports, inputs=session_token, outputs=report_history
        )
        view_report_btn.click(
            fn=view_report, inputs=[session_token, report_history], outputs=report_output
        )
        download_snapshot_btn.click(
            fn=download_report_markdown,
            inputs=[session_token, report_history],
            outputs=report_md_file,
        )

        with gr.Accordion("Legacy data migration", open=False):
            migration_output = gr.Textbox(lines=10)
            scan_migration_btn = gr.Button("Scan legacy data")
            claim_migration_btn = gr.Button("Claim and migrate")
            scan_migration_btn.click(
                fn=scan_legacy_data, inputs=session_token, outputs=migration_output
            )
            claim_migration_btn.click(
                fn=claim_legacy_data, inputs=session_token, outputs=migration_output
            )

        with gr.Accordion("🛠️ Data Recovery", open=False):
            recovery_check_btn = gr.Button("Check corruption status")
            recovery_status = gr.Textbox(
                label="Corruption status",
                lines=4,
                interactive=False,
            )
            gr.Markdown("---\n### History recovery")
            with gr.Row():
                quarantine_history_btn = gr.Button("Quarantine corrupt History")
            recovery_history_output = gr.Textbox(
                label="History recovery result",
                lines=3,
                interactive=False,
            )
            gr.Markdown("### Memory recovery")
            with gr.Row():
                quarantine_memory_btn = gr.Button("Quarantine corrupt Memory")
            recovery_memory_output = gr.Textbox(
                label="Memory recovery result",
                lines=3,
                interactive=False,
            )
            gr.Markdown("### Restore from backup")
            recovery_backup_dropdown = gr.Dropdown(
                label="Select backup to restore",
                choices=[],
                interactive=True,
            )
            with gr.Row():
                refresh_backups_btn = gr.Button("Refresh backup list")
                restore_history_btn = gr.Button("Restore History")
                restore_memory_btn = gr.Button("Restore Memory")
            recovery_restore_output = gr.Textbox(
                label="Restore result",
                lines=3,
                interactive=False,
            )

            recovery_check_btn.click(
                fn=check_corruption_status,
                inputs=session_token,
                outputs=recovery_status,
            )
            quarantine_history_btn.click(
                fn=quarantine_history,
                inputs=session_token,
                outputs=recovery_history_output,
            )
            quarantine_memory_btn.click(
                fn=quarantine_memory,
                inputs=session_token,
                outputs=recovery_memory_output,
            )
            refresh_backups_btn.click(
                fn=list_recovery_backups,
                inputs=session_token,
                outputs=recovery_backup_dropdown,
            )
            restore_history_btn.click(
                fn=restore_history,
                inputs=[session_token, recovery_backup_dropdown],
                outputs=recovery_restore_output,
            )
            restore_memory_btn.click(
                fn=restore_memory,
                inputs=[session_token, recovery_backup_dropdown],
                outputs=recovery_restore_output,
            )

    # =========================
    # 8. 学习中心
    # =========================
    with gr.Tab("8. 学习中心"):
        with gr.Row():
            learning_document = gr.Dropdown(
                label="学习文档", choices=[], multiselect=False, interactive=True
            )
            refresh_learning_document_btn = gr.Button("刷新文档")
        learning_document_status = gr.Markdown()
        revealed_card_id = gr.State("")
        revealed_exercise_id = gr.State("")

        with gr.Tabs():
            with gr.Tab("今日任务"):
                refresh_dashboard_btn = gr.Button("刷新今日任务")
                dashboard_summary = gr.Markdown()
                dashboard_tasks = gr.Dataframe(
                    headers=["日期", "阶段", "任务", "分钟", "已完成", "计划 ID", "任务 ID"],
                    datatype=["str", "str", "str", "number", "str", "str", "str"],
                    interactive=False,
                )
                dashboard_due = gr.Dataframe(
                    headers=["类型", "内容", "到期时间", "项目 ID"],
                    datatype=["str", "str", "str", "str"],
                    interactive=False,
                )

            with gr.Tab("学习计划"):
                plan_title = gr.Textbox(label="计划名称", max_lines=1)
                target_date = gr.DateTime(label="目标日期", include_time=False, type="string")
                daily_minutes = gr.Number(label="每日学习分钟数", value=30, precision=0)
                create_plan_btn = gr.Button("创建计划")
                plan_result = gr.Markdown()
                plan_table = gr.Dataframe(
                    headers=["名称", "目标日期", "每日分钟", "状态", "进度", "计划 ID"],
                    datatype=["str", "str", "number", "str", "str", "str"],
                    interactive=False,
                )
                plan_task_table = gr.Dataframe(
                    headers=["日期", "阶段", "任务", "分钟", "已完成", "计划 ID", "任务 ID"],
                    datatype=["str", "str", "str", "number", "str", "str", "str"],
                    interactive=False,
                )
                with gr.Row():
                    task_plan_id = gr.Dropdown(label="选择计划", choices=[])
                    task_id = gr.Dropdown(label="选择任务", choices=[])
                    task_completed = gr.Checkbox(label="标记完成")
                    update_task_btn = gr.Button("更新任务")
                refresh_plans_btn = gr.Button("刷新计划")

            with gr.Tab("知识卡片"):
                card_count = gr.Radio([3, 5, 10], value=5, label="生成数量")
                generate_cards_btn = gr.Button("生成卡片")
                refresh_cards_btn = gr.Button("加载已有卡片")
                card_result = gr.Markdown()
                card_selector = gr.Dropdown(label="选择卡片", choices=[])
                card_front = gr.Markdown()
                reveal_card_btn = gr.Button("显示答案与来源")
                card_back = gr.Markdown()
                card_sources = gr.Markdown()
                with gr.Row():
                    card_forgot_btn = gr.Button("不会")
                    card_fuzzy_btn = gr.Button("模糊")
                    card_mastered_btn = gr.Button("掌握")
                card_review_result = gr.Markdown()

            with gr.Tab("练习题"):
                exercise_kind = gr.Radio(
                    [("混合", "mixed"), ("选择题", "multiple_choice"), ("简答题", "short_answer")],
                    value="mixed", label="题型"
                )
                exercise_count = gr.Radio([3, 5, 10], value=5, label="生成数量")
                generate_exercises_btn = gr.Button("生成练习")
                refresh_exercises_btn = gr.Button("加载已有练习")
                exercise_result = gr.Markdown()
                exercise_selector = gr.Dropdown(label="选择题目", choices=[])
                exercise_prompt = gr.Markdown()
                mcq_choice = gr.Radio(label="选择答案", choices=[])
                submit_mcq_btn = gr.Button("提交选择题")
                mcq_result = gr.Markdown()
                short_answer = gr.Textbox(label="简答作答", lines=4)
                reveal_short_btn = gr.Button("显示参考答案与来源")
                short_reference = gr.Markdown()
                with gr.Row():
                    short_forgot_btn = gr.Button("不会")
                    short_fuzzy_btn = gr.Button("模糊")
                    short_mastered_btn = gr.Button("掌握")
                short_review_result = gr.Markdown()

        learning_reset_outputs = [
            learning_document_status, revealed_card_id, revealed_exercise_id,
            card_selector, exercise_selector,
            card_back, card_sources, card_review_result, mcq_result,
            short_reference, short_review_result,
            card_front, exercise_prompt, short_answer, card_result, exercise_result,
            dashboard_summary, dashboard_tasks, dashboard_due, plan_table, plan_task_table,
            task_plan_id, task_id, plan_title, target_date, daily_minutes, task_completed, mcq_choice, plan_result,
        ]
        session_token.change(fn=clear_learning_interaction, inputs=[], outputs=learning_reset_outputs)
        refresh_learning_document_btn.click(
            fn=refresh_learning_documents,
            inputs=[session_token, learning_document],
            outputs=[learning_document] + learning_reset_outputs,
        )
        learning_document.change(
            fn=select_learning_document,
            inputs=[session_token, learning_document],
            outputs=learning_reset_outputs,
        )
        refresh_dashboard_btn.click(
            fn=refresh_learning_dashboard,
            inputs=[session_token, learning_document],
            outputs=[dashboard_summary, dashboard_tasks, dashboard_due],
        )
        refresh_plans_btn.click(fn=refresh_learning_plans, inputs=[session_token, learning_document], outputs=[plan_table, plan_task_table, plan_result])
        refresh_plans_btn.click(fn=learning_plan_choices, inputs=[session_token, learning_document], outputs=[task_plan_id, task_id])
        task_plan_id.change(fn=learning_task_choices, inputs=[session_token, learning_document, task_plan_id], outputs=task_id)
        create_plan_btn.click(fn=create_learning_plan, inputs=[session_token, learning_document, plan_title, target_date, daily_minutes], outputs=[plan_result, plan_table, plan_task_table]).success(fn=learning_plan_choices, inputs=[session_token, learning_document], outputs=[task_plan_id, task_id])
        update_task_btn.click(fn=set_learning_task_completed, inputs=[session_token, learning_document, task_plan_id, task_id, task_completed], outputs=[plan_result, plan_table, plan_task_table])
        generate_cards_btn.click(fn=generate_learning_cards, inputs=[session_token, learning_document, card_count], outputs=[card_result, card_selector, card_back, card_sources, revealed_card_id])
        refresh_cards_btn.click(fn=refresh_learning_cards, inputs=[session_token, learning_document], outputs=[card_selector, card_back, card_sources, revealed_card_id])
        card_selector.change(fn=select_learning_card, inputs=[session_token, learning_document, card_selector], outputs=[card_front, card_back, card_sources, revealed_card_id])
        card_selector.change(fn=lambda: "", inputs=[], outputs=card_review_result)
        reveal_card_btn.click(fn=reveal_learning_card, inputs=[session_token, learning_document, card_selector], outputs=[card_back, card_sources, revealed_card_id])
        for button, rating in ((card_forgot_btn, "forgot"), (card_fuzzy_btn, "fuzzy"), (card_mastered_btn, "mastered")):
            button.click(fn=lambda token, document, item, revealed, value=rating: review_learning_card(token, document, item, revealed, value), inputs=[session_token, learning_document, card_selector, revealed_card_id], outputs=[card_review_result, revealed_card_id])
        generate_exercises_btn.click(fn=generate_learning_exercises, inputs=[session_token, learning_document, exercise_count, exercise_kind], outputs=[exercise_result, exercise_selector, exercise_prompt, mcq_choice, mcq_result, short_reference, revealed_exercise_id])
        refresh_exercises_btn.click(fn=refresh_learning_exercises, inputs=[session_token, learning_document], outputs=[exercise_selector, exercise_prompt, mcq_choice, mcq_result, short_reference, revealed_exercise_id])
        exercise_selector.change(fn=lambda: ("", ""), inputs=[], outputs=[short_answer, short_review_result])
        exercise_selector.change(fn=select_learning_exercise, inputs=[session_token, learning_document, exercise_selector], outputs=[exercise_prompt, mcq_choice, mcq_result, short_reference, revealed_exercise_id])
        submit_mcq_btn.click(fn=submit_learning_multiple_choice, inputs=[session_token, learning_document, exercise_selector, mcq_choice], outputs=mcq_result)
        reveal_short_btn.click(fn=reveal_learning_short_answer, inputs=[session_token, learning_document, exercise_selector, short_answer], outputs=[short_reference, revealed_exercise_id])
        for button, rating in ((short_forgot_btn, "forgot"), (short_fuzzy_btn, "fuzzy"), (short_mastered_btn, "mastered")):
            button.click(fn=lambda token, document, item, revealed, answer, value=rating: review_learning_short_answer(token, document, item, revealed, value, answer), inputs=[session_token, learning_document, exercise_selector, revealed_exercise_id, short_answer], outputs=[short_review_result, revealed_exercise_id])

        delete_doc_btn_ask.click(
            fn=delete_current_pdf,
            inputs=[session_token, doc_dropdown_ask],
            outputs=[
                delete_doc_output_ask,
                doc_dropdown_ask,
                doc_dropdown_search,
                learning_document,
                select_doc_output_ask,
                select_doc_output_search,
            ],
        )

        delete_doc_btn_search.click(
            fn=delete_current_pdf,
            inputs=[session_token, doc_dropdown_search],
            outputs=[
                delete_doc_output_search,
                doc_dropdown_ask,
                doc_dropdown_search,
                learning_document,
                select_doc_output_ask,
                select_doc_output_search,
            ],
        )

        clear_all_doc_btn_ask.click(
            fn=clear_all_pdfs,
            inputs=session_token,
            outputs=[
                delete_doc_output_ask,
                doc_dropdown_ask,
                doc_dropdown_search,
                learning_document,
                select_doc_output_ask,
                select_doc_output_search,
            ],
        )

        clear_all_doc_btn_search.click(
            fn=clear_all_pdfs,
            inputs=session_token,
            outputs=[
                delete_doc_output_search,
                doc_dropdown_ask,
                doc_dropdown_search,
                learning_document,
                select_doc_output_ask,
                select_doc_output_search,
            ],
        )

        # =========================
        # 上传文档：所有组件都定义完之后再绑定
        # =========================
        login_btn.click(
            fn=login_user,
            inputs=[username_input, password_input],
            outputs=[session_token, auth_status, doc_dropdown_ask, doc_dropdown_search, learning_document],
        ).then(fn=clear_learning_interaction, inputs=[], outputs=learning_reset_outputs).then(
            fn=refresh_import_batches,
            inputs=session_token,
            outputs=[
                import_batch_dropdown,
                import_summary,
                import_tasks,
                selected_import_task_id,
            ],
            queue=False,
        )

        register_btn.click(
            fn=register_user,
            inputs=[username_input, password_input],
            outputs=[session_token, auth_status, doc_dropdown_ask, doc_dropdown_search, learning_document],
        ).then(fn=clear_learning_interaction, inputs=[], outputs=learning_reset_outputs).then(
            fn=refresh_import_batches,
            inputs=session_token,
            outputs=[
                import_batch_dropdown,
                import_summary,
                import_tasks,
                selected_import_task_id,
            ],
            queue=False,
        )

        logout_btn.click(
            fn=logout_user,
            inputs=session_token,
            outputs=[session_token, auth_status, doc_dropdown_ask, doc_dropdown_search, learning_document],
        ).then(fn=clear_learning_interaction, inputs=[], outputs=learning_reset_outputs).then(
            fn=clear_import_ui,
            inputs=None,
            outputs=[
                import_batch_dropdown,
                import_summary,
                import_tasks,
                selected_import_task_id,
            ],
            queue=False,
        )

        submit_import_btn.click(
            fn=submit_import_batch,
            inputs=[session_token, import_files],
            outputs=[
                submitted_import_batch_id,
                import_summary,
                import_tasks,
                doc_dropdown_ask,
                doc_dropdown_search,
                learning_document,
                selected_import_task_id,
            ]
        ).then(
            fn=refresh_import_batches,
            inputs=session_token,
            outputs=[
                import_batch_dropdown,
                import_summary,
                import_tasks,
                selected_import_task_id,
            ],
            queue=False,
        )

        refresh_import_btn.click(
            fn=refresh_import_batches,
            inputs=session_token,
            outputs=[
                import_batch_dropdown,
                import_summary,
                import_tasks,
                selected_import_task_id,
            ],
            queue=False,
        )
        import_batch_dropdown.change(
            fn=refresh_import_batch,
            inputs=[
                session_token,
                import_batch_dropdown,
                selected_import_task_id,
            ],
            outputs=[import_summary, import_tasks, selected_import_task_id],
            queue=False,
        )
        import_timer.tick(
            fn=refresh_import_batch,
            inputs=[
                session_token,
                import_batch_dropdown,
                selected_import_task_id,
            ],
            outputs=[import_summary, import_tasks, selected_import_task_id],
            queue=False,
            show_progress="hidden",
        )
        import_tasks.select(
            fn=select_import_task,
            inputs=[session_token, import_batch_dropdown],
            outputs=selected_import_task_id,
            queue=False,
        )
        retry_selected_btn.click(
            fn=retry_import_task,
            inputs=[
                session_token,
                import_batch_dropdown,
                selected_import_task_id,
            ],
            outputs=[import_summary, import_tasks, selected_import_task_id],
        )
        retry_batch_btn.click(
            fn=retry_import_batch_failures,
            inputs=[session_token, import_batch_dropdown],
            outputs=[import_summary, import_tasks, selected_import_task_id],
        )


def launch_app() -> None:
    """Run the supported script lifecycle and always stop import workers."""

    initialize_app_services()
    try:
        start_import_workers()
        demo.launch(**load_launch_config().as_gradio_kwargs())
    finally:
        if import_worker_pool is not None:
            import_worker_pool.stop(wait=True)


if __name__ == "__main__":
    launch_app()
