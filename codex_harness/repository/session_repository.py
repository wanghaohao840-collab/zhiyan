"""本机 Codex JSONL 会话与 codex exec 事件流只读扫描解析仓储。"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from codex_harness.schema.contracts import (
    CommandStep,
    FileChangeStep,
    SessionDetail,
    SessionSummary,
    SessionTurn,
    SubAgentStep,
    TimelineItem,
)

LOGGER = logging.getLogger(__name__)

_MAX_OUTPUT_PREVIEW = 4800
_MAX_DIFF_PREVIEW = 5500
_MAX_PROMPT_TITLE = 84
_BOILERPLATE_PREFIXES = (
    "# AGENTS.md instructions",
    "<environment_context>",
    "<skill>",
    "<INSTRUCTIONS>",
)


def _normalize_path_display(raw_path: str | None) -> str:
    """将 file:/// URI 或混用斜杠的路径标准化为易读本地路径。"""
    if not raw_path:
        return ""
    cleaned = raw_path.strip()
    if cleaned.startswith("file:///"):
        cleaned = cleaned[8:]
    return cleaned.replace("/", os.sep)


def derive_worktree_name(workspace: str | None) -> str | None:
    """若工作目录属于 Codex worktree，则提取对应的 worktree 分支/目录名。"""
    cleaned = _normalize_path_display(workspace)
    if not cleaned:
        return None
    parts = [part for part in re.split(r"[\\/]+", cleaned) if part]
    lower_parts = [part.lower() for part in parts]
    if "worktrees" in lower_parts:
        idx = lower_parts.index("worktrees")
        if idx + 2 < len(parts):
            return parts[idx + 1]
    return None


def derive_project_name(workspace: str | None) -> str:
    """根据工作目录派生左侧项目树分组名称，自动将 worktree 归并回主仓库项目名。"""
    cleaned = _normalize_path_display(workspace)
    if not cleaned:
        return "未归类工作区"
    parts = [part for part in re.split(r"[\\/]+", cleaned) if part]
    if not parts:
        return cleaned
    lower_parts = [part.lower() for part in parts]
    if "worktrees" in lower_parts:
        idx = lower_parts.index("worktrees")
        if idx + 2 < len(parts):
            return parts[idx + 2]
    return parts[-1]


def _truncate_text(text: str, limit: int) -> tuple[str, bool]:
    """安全截断过长的命令输出或 Diff 文本，避免撑爆 MCP App 响应体积。"""
    if len(text) <= limit:
        return text, False
    return text[:limit] + "\n... [内容已截断]", True


def _extract_text_blocks(content: Any) -> str:
    """从 Codex 消息 content 列表或字符串中提取纯文本。"""
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for block in content:
        if isinstance(block, str):
            parts.append(block)
        elif isinstance(block, dict):
            text_val = block.get("text")
            if isinstance(text_val, str) and text_val:
                parts.append(text_val)
    return "\n".join(parts).strip()


def _format_duration_ms(duration_obj: Any, started_ms: int | None = None, completed_ms: int | None = None) -> int | None:
    """统一换算 Codex 事件里的耗时字段为毫秒整数。"""
    if isinstance(duration_obj, dict):
        secs = duration_obj.get("secs")
        nanos = duration_obj.get("nanos", 0)
        if isinstance(secs, (int, float)) and isinstance(nanos, (int, float)):
            return int(secs * 1000 + nanos / 1_000_000)
    if isinstance(duration_obj, (int, float)):
        return int(duration_obj)
    if isinstance(started_ms, int) and isinstance(completed_ms, int) and completed_ms >= started_ms:
        return completed_ms - started_ms
    return None


def _extract_command_string(item: dict[str, Any]) -> str:
    """优先从 parsed_cmd 提取人类可读命令，其次回退到原始 command 数组。"""
    parsed_cmd = item.get("parsed_cmd")
    if isinstance(parsed_cmd, list) and parsed_cmd:
        cmds: list[str] = []
        for entry in parsed_cmd:
            if isinstance(entry, dict) and entry.get("cmd"):
                cmds.append(str(entry["cmd"]))
        if cmds:
            return " && ".join(cmds)
    raw_cmd = item.get("command")
    if isinstance(raw_cmd, str):
        return raw_cmd
    if isinstance(raw_cmd, list):
        # Windows PowerShell 包装通常是 [pwsh.exe, -NoProfile, -Command, <actual>]
        if len(raw_cmd) >= 2 and any(flag in ("-Command", "-c") for flag in raw_cmd[:-1]):
            return str(raw_cmd[-1])
        return " ".join(str(part) for part in raw_cmd)
    return ""


def _clean_prompt_text(raw_text: str) -> str:
    """清洗内部通信信封（如 NEW_TASK 加密占位、问题回复 XML），保留可读派单正文。"""
    text = raw_text.strip()
    if not text:
        return ""
    if text.startswith("<send_user_message_question_reply>"):
        # 提取用户交互回复中的可读答案摘要，避免把原始 XML 当作标题
        answers = re.findall(r'"answers"\s*:\s*\[([^\]]+)\]', text)
        if answers:
            cleaned = ", ".join(ans.replace('"', "").strip() for ans in answers)
            return f"交互确认回复：{cleaned}"
        return "交互确认回复"
    if text.startswith("Message Type: NEW_TASK"):
        task_match = re.search(r"Task name:\s*(.+)", text)
        sender_match = re.search(r"Sender:\s*(.+)", text)
        payload_idx = text.find("Payload:")
        tail = text[payload_idx + len("Payload:") :].strip() if payload_idx != -1 else ""
        if tail:
            return tail
        task_name = task_match.group(1).strip() if task_match else "subagent"
        sender = sender_match.group(1).strip() if sender_match else "root"
        return f"来自 {sender} 的子代理派单任务（目标：{task_name}，原始载荷已加密）"
    return text


_SKIP_TITLE_PREFIXES = (
    "inherited context (forked because",
    "antigravity -> codex explicit dispatch",
    "harness role:",
    "sandbox:",
    "workspace:",
    "bound task packet:",
)


def _make_title_from_prompt(prompt: str, fallback: str) -> str:
    """从首条有效提示词生成左侧列表标题，优先提取显式派单 Title 并过滤协议头与尾反斜杠。"""
    cleaned = _clean_prompt_text(prompt)
    # 优先提取显式 Harness 派单信封中的真实 Title 字段
    title_match = re.search(r"(?m)^Title:\s*(.+)$", cleaned)
    if title_match:
        explicit_title = title_match.group(1).strip().rstrip("\\").strip()
        if explicit_title and not explicit_title.lower().startswith(_SKIP_TITLE_PREFIXES):
            return explicit_title[:_MAX_PROMPT_TITLE] + ("..." if len(explicit_title) > _MAX_PROMPT_TITLE else "")

    for raw_line in cleaned.splitlines():
        line = raw_line.strip().lstrip("#").strip().rstrip("\\").strip()
        if not line:
            continue
        if line.startswith(("```", "来自 ", "<")) or line.lower().startswith(_SKIP_TITLE_PREFIXES):
            continue
        return line[:_MAX_PROMPT_TITLE] + ("..." if len(line) > _MAX_PROMPT_TITLE else "")
    return fallback


def _is_boilerplate_user_text(text: str) -> bool:
    stripped = text.strip()
    if not stripped:
        return True
    return any(stripped.startswith(prefix) for prefix in _BOILERPLATE_PREFIXES)


def _iso_from_mtime(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()


def _format_clean_error_message(raw_err: str) -> str:
    """将上游 API 返回的 JSON 错误体提取为精炼人类可读提示。"""
    cleaned = raw_err.strip()
    if cleaned.startswith("{") and cleaned.endswith("}"):
        try:
            parsed = json.loads(cleaned)
            if isinstance(parsed, dict):
                inner = parsed.get("error")
                if isinstance(inner, dict) and inner.get("message"):
                    code = inner.get("code") or inner.get("type")
                    msg = str(inner["message"])
                    return f"{msg} ({code})" if code else msg
                if parsed.get("message"):
                    return str(parsed["message"])
        except json.JSONDecodeError:
            pass
    return cleaned


def parse_exec_events_file(events_path: Path) -> dict[str, Any]:
    """解析 codex exec --json 输出的实时事件流，提取线程 ID、命令、改动文件与时间线。"""
    timeline: list[TimelineItem] = []
    changed_files: list[str] = []
    command_count = 0
    codex_thread_id: str | None = None
    final_answer: str | None = None
    error_message: str | None = None
    seen_errors: set[str] = set()

    if not events_path.exists():
        return {
            "timeline": timeline,
            "changed_files": changed_files,
            "command_count": 0,
            "file_count": 0,
            "codex_thread_id": None,
            "final_answer": None,
            "error_message": None,
        }

    seq = 0
    with events_path.open("r", encoding="utf-8", errors="replace") as handle:
        for raw_line in handle:
            line = raw_line.strip()
            if not line:
                continue
            try:
                evt = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(evt, dict):
                continue

            evt_type = str(evt.get("type") or "")
            ts = evt.get("timestamp") if isinstance(evt.get("timestamp"), str) else None

            if evt_type == "thread.started":
                tid = evt.get("thread_id")
                if isinstance(tid, str) and tid:
                    codex_thread_id = tid
                continue

            if evt_type in ("error", "turn.failed"):
                err = evt.get("message") or (evt.get("error") or {}).get("message")
                if isinstance(err, str) and err.strip():
                    clean_err = _format_clean_error_message(err)
                    error_message = clean_err
                    if clean_err not in seen_errors:
                        seen_errors.add(clean_err)
                        seq += 1
                        timeline.append(TimelineItem(id=f"exec-err-{seq}", kind="error", timestamp=ts, text=clean_err))
                continue

            if evt_type in ("item.completed", "item.updated"):
                if evt_type == "item.updated":
                    # 仅在 completed 时落定条目，避免重复堆叠
                    continue
                item = evt.get("item")
                if not isinstance(item, dict):
                    continue
                item_type = str(item.get("type") or "").lower()
                item_id = str(item.get("id") or f"exec-item-{seq + 1}")

                if item_type == "command_execution":
                    command_count += 1
                    output_raw = str(item.get("aggregated_output") or "")
                    preview, truncated = _truncate_text(output_raw, _MAX_OUTPUT_PREVIEW)
                    cmd_step = CommandStep(
                        id=item_id,
                        command=str(item.get("command") or ""),
                        cwd=item.get("cwd"),
                        exit_code=item.get("exit_code"),
                        duration_ms=item.get("duration_ms"),
                        status=str(item.get("status") or "completed"),
                        output_preview=preview,
                        output_truncated=truncated,
                    )
                    seq += 1
                    timeline.append(TimelineItem(id=f"cmd-{seq}", kind="command", timestamp=ts, command=cmd_step))
                elif item_type == "file_change":
                    changes = item.get("changes")
                    if isinstance(changes, list):
                        for ch in changes:
                            if not isinstance(ch, dict):
                                continue
                            fpath = str(ch.get("path") or "")
                            if fpath and fpath not in changed_files:
                                changed_files.append(fpath)
                            diff_raw = str(ch.get("diff") or ch.get("unified_diff") or "")
                            diff_preview, diff_trunc = _truncate_text(diff_raw, _MAX_DIFF_PREVIEW)
                            seq += 1
                            timeline.append(
                                TimelineItem(
                                    id=f"fc-{seq}",
                                    kind="file_change",
                                    timestamp=ts,
                                    file_change=FileChangeStep(
                                        path=fpath,
                                        change_type=str(ch.get("kind") or ch.get("type") or "update"),
                                        diff_preview=diff_preview,
                                        diff_truncated=diff_trunc,
                                    ),
                                )
                            )
                elif item_type == "reasoning":
                    text_val = str(item.get("text") or "").strip()
                    if text_val:
                        seq += 1
                        timeline.append(TimelineItem(id=f"rs-{seq}", kind="reasoning", timestamp=ts, text=text_val))
                elif item_type == "agent_message":
                    text_val = str(item.get("text") or "").strip()
                    if text_val:
                        final_answer = text_val
                        seq += 1
                        timeline.append(TimelineItem(id=f"msg-{seq}", kind="final_answer", timestamp=ts, text=text_val))

    return {
        "timeline": timeline,
        "changed_files": changed_files,
        "command_count": command_count,
        "file_count": len(changed_files),
        "codex_thread_id": codex_thread_id,
        "final_answer": final_answer,
        "error_message": error_message,
    }


class SessionRepository:
    """只读扫描并缓存本机 ~/.codex/sessions/**/*.jsonl 会话记录。"""

    def __init__(self, session_roots: list[Path] | None = None) -> None:
        if session_roots is None:
            default_roots: list[Path] = []
            codex_home_env = os.getenv("CODEX_HOME")
            if codex_home_env:
                default_roots.append(Path(codex_home_env) / "sessions")
            default_roots.append(Path.home() / ".codex" / "sessions")
            # 去重且保留顺序
            seen: set[str] = set()
            resolved_roots: list[Path] = []
            for root in default_roots:
                key = str(root.resolve()).lower()
                if key not in seen:
                    seen.add(key)
                    resolved_roots.append(root)
            self._roots = resolved_roots
        else:
            self._roots = session_roots

        self._lock = threading.RLock()
        self._summary_cache: dict[str, tuple[int, int, SessionSummary]] = {}
        self._detail_cache: dict[str, tuple[int, int, SessionDetail]] = {}
        self._index_by_id: dict[str, Path] = {}

    def _discover_files(self) -> list[Path]:
        """收集全部现存会话 JSONL 文件，按修改时间倒序排列。"""
        discovered: dict[str, Path] = {}
        for root in self._roots:
            if not root.exists():
                continue
            for path in root.rglob("*.jsonl"):
                if path.is_file():
                    discovered[str(path.resolve()).lower()] = path
        files = list(discovered.values())
        files.sort(key=lambda item: item.stat().st_mtime if item.exists() else 0.0, reverse=True)
        return files

    def _parse_rollout_file(self, path: Path, include_detail: bool) -> tuple[SessionSummary, SessionDetail | None]:
        """解析单个 Codex rollout JSONL 文件，返回轻量摘要与可选完整详情。"""
        stat = path.stat()
        mtime_iso = _iso_from_mtime(stat.st_mtime)

        session_id = path.stem
        workspace = ""
        originator: str | None = None
        is_subagent = False
        parent_thread_id: str | None = None
        agent_nickname: str | None = None
        started_at: str | None = None

        latest_model: str | None = None
        latest_effort: str | None = None
        rate_limit_hint: dict[str, Any] | None = None

        turns: list[SessionTurn] = []
        turn_by_id: dict[str, SessionTurn] = {}
        current_turn: SessionTurn | None = None
        seq = 0

        def ensure_turn(turn_id: str | None, timestamp: str | None) -> SessionTurn:
            nonlocal current_turn
            if turn_id and turn_id in turn_by_id:
                current_turn = turn_by_id[turn_id]
                return current_turn
            if current_turn is None or (turn_id and current_turn.turn_id and current_turn.turn_id != turn_id):
                next_index = len(turns) + 1
                new_turn = SessionTurn(
                    turn_index=next_index,
                    turn_id=turn_id,
                    started_at=timestamp,
                    source_label="User",
                    target_model=latest_model,
                    target_effort=latest_effort,
                    status="running",
                )
                turns.append(new_turn)
                if turn_id:
                    turn_by_id[turn_id] = new_turn
                current_turn = new_turn
            elif turn_id and not current_turn.turn_id:
                current_turn.turn_id = turn_id
                turn_by_id[turn_id] = current_turn
            return current_turn

        with path.open("r", encoding="utf-8", errors="replace") as handle:
            for raw_line in handle:
                line = raw_line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(entry, dict):
                    continue

                entry_type = entry.get("type")
                timestamp = entry.get("timestamp") if isinstance(entry.get("timestamp"), str) else None
                payload = entry.get("payload")
                if not isinstance(payload, dict):
                    continue

                if entry_type == "session_meta":
                    session_id = str(payload.get("id") or payload.get("session_id") or session_id)
                    workspace = _normalize_path_display(payload.get("cwd"))
                    originator = str(payload.get("originator") or "") or None
                    is_subagent = payload.get("thread_source") == "subagent" or bool(payload.get("parent_thread_id"))
                    parent_thread_id = payload.get("parent_thread_id")
                    agent_nickname = payload.get("agent_nickname")
                    started_at = str(payload.get("timestamp") or timestamp or mtime_iso)
                    continue

                if entry_type == "turn_context":
                    turn_id = payload.get("turn_id")
                    if payload.get("model"):
                        latest_model = str(payload["model"])
                    if payload.get("effort"):
                        latest_effort = str(payload["effort"])
                    if payload.get("cwd") and not workspace:
                        workspace = _normalize_path_display(payload.get("cwd"))
                    turn = ensure_turn(turn_id, timestamp)
                    turn.target_model = latest_model
                    turn.target_effort = latest_effort
                    sandbox_policy = payload.get("sandbox_policy")
                    if isinstance(sandbox_policy, dict) and sandbox_policy.get("type"):
                        turn.sandbox = str(sandbox_policy["type"])
                    continue

                if entry_type == "event_msg":
                    ev_type = payload.get("type")
                    turn_id = payload.get("turn_id")

                    if ev_type == "token_count":
                        rl = payload.get("rate_limits")
                        if isinstance(rl, dict):
                            rate_limit_hint = rl
                        continue

                    if ev_type == "task_started":
                        turn = ensure_turn(turn_id, timestamp)
                        if not turn.started_at:
                            turn.started_at = timestamp
                        turn.status = "running"
                        continue

                    if ev_type == "task_complete":
                        turn = ensure_turn(turn_id, timestamp)
                        turn.completed_at = timestamp
                        dur = payload.get("duration_ms")
                        if isinstance(dur, (int, float)):
                            turn.duration_ms = int(dur)
                        err = payload.get("error")
                        if isinstance(err, dict) and err:
                            turn.status = "failed"
                            turn.error_message = str(err.get("message") or err)
                        else:
                            turn.status = "completed"
                        last_msg = payload.get("last_agent_message")
                        if isinstance(last_msg, str) and last_msg.strip() and not turn.final_answer:
                            turn.final_answer = last_msg.strip()
                        continue

                    if ev_type == "user_message":
                        msg_text = _clean_prompt_text(str(payload.get("message") or ""))
                        if msg_text and not _is_boilerplate_user_text(msg_text):
                            turn = ensure_turn(turn_id, timestamp)
                            if not turn.prompt:
                                turn.prompt = msg_text
                                turn.prompt_line_count = len(msg_text.splitlines())
                        continue

                    if ev_type == "item_completed":
                        item = payload.get("item")
                        if not isinstance(item, dict):
                            continue
                        turn = ensure_turn(turn_id, timestamp)
                        item_type = str(item.get("type") or "")
                        started_ms = payload.get("started_at_ms")
                        completed_ms = payload.get("completed_at_ms")

                        if item_type == "UserMessage":
                            msg_text = _clean_prompt_text(_extract_text_blocks(item.get("content")))
                            if msg_text and not _is_boilerplate_user_text(msg_text):
                                turn.prompt = msg_text
                                turn.prompt_line_count = len(msg_text.splitlines())
                        elif item_type == "AgentMessage":
                            msg_text = _extract_text_blocks(item.get("content"))
                            if msg_text:
                                phase = str(item.get("phase") or "commentary")
                                if phase == "final_answer":
                                    turn.final_answer = msg_text
                                if include_detail:
                                    seq += 1
                                    kind = "final_answer" if phase == "final_answer" else "narration"
                                    turn.timeline.append(
                                        TimelineItem(id=f"am-{seq}", kind=kind, timestamp=timestamp, text=msg_text)
                                    )
                        elif item_type == "Reasoning":
                            if include_detail:
                                summary_list = item.get("summary_text")
                                if isinstance(summary_list, list) and summary_list:
                                    # 去除原始 Markdown 粗体标记 **，避免界面直接暴露星号
                                    cleaned_items = [
                                        re.sub(r"\*\*", "", str(s)).strip()
                                        for s in summary_list
                                        if str(s).strip()
                                    ]
                                    joined = " · ".join(cleaned_items)
                                    if joined:
                                        seq += 1
                                        turn.timeline.append(
                                            TimelineItem(id=f"rs-{seq}", kind="reasoning", timestamp=timestamp, text=joined)
                                        )
                        elif item_type == "CommandExecution":
                            turn.command_count += 1
                            if include_detail:
                                seq += 1
                                out_raw = str(item.get("aggregated_output") or "")
                                preview, truncated = _truncate_text(out_raw, _MAX_OUTPUT_PREVIEW)
                                cmd_step = CommandStep(
                                    id=str(item.get("id") or f"cmd-{seq}"),
                                    command=_extract_command_string(item),
                                    cwd=_normalize_path_display(item.get("cwd")),
                                    exit_code=item.get("exit_code"),
                                    duration_ms=_format_duration_ms(item.get("duration"), started_ms, completed_ms),
                                    status=str(item.get("status") or "completed"),
                                    output_preview=preview,
                                    output_truncated=truncated,
                                )
                                turn.timeline.append(
                                    TimelineItem(id=f"ce-{seq}", kind="command", timestamp=timestamp, command=cmd_step)
                                )
                        elif item_type == "FileChange":
                            changes = item.get("changes")
                            if isinstance(changes, dict):
                                for raw_fpath, meta in changes.items():
                                    clean_fp = _normalize_path_display(str(raw_fpath))
                                    if clean_fp and clean_fp not in turn.changed_files:
                                        turn.changed_files.append(clean_fp)
                                        turn.file_count = len(turn.changed_files)
                                    if include_detail and isinstance(meta, dict):
                                        seq += 1
                                        diff_raw = str(meta.get("unified_diff") or "")
                                        diff_preview, diff_trunc = _truncate_text(diff_raw, _MAX_DIFF_PREVIEW)
                                        turn.timeline.append(
                                            TimelineItem(
                                                id=f"fc-{seq}",
                                                kind="file_change",
                                                timestamp=timestamp,
                                                file_change=FileChangeStep(
                                                    path=clean_fp,
                                                    change_type=str(meta.get("type") or "update"),
                                                    diff_preview=diff_preview,
                                                    diff_truncated=diff_trunc,
                                                ),
                                            )
                                        )
                        elif item_type == "SubAgentActivity":
                            if include_detail:
                                sub_kind = str(item.get("kind") or "interacted")
                                sub_path = item.get("agent_path")
                                # NOTE: 过滤高频的父级 /root 心跳交互噪音，仅保留有实质意义的派生/完成/非根协作
                                if sub_kind == "interacted" and sub_path == "/root":
                                    continue
                                seq += 1
                                sub_step = SubAgentStep(
                                    id=str(item.get("id") or f"sub-{seq}"),
                                    kind=sub_kind,
                                    model=item.get("model"),
                                    effort=item.get("reasoning_effort"),
                                    agent_thread_id=item.get("agent_thread_id"),
                                    agent_path=sub_path,
                                )
                                turn.timeline.append(
                                    TimelineItem(id=f"sub-{seq}", kind="subagent", timestamp=timestamp, subagent=sub_step)
                                )
                    continue

                # 兼容不包含 item_completed 的旧版/精简 response_item 事件与子代理初始派单消息
                if entry_type == "response_item":
                    resp_type = payload.get("type")
                    if resp_type == "message" and payload.get("role") == "user":
                        msg_text = _clean_prompt_text(_extract_text_blocks(payload.get("content")))
                        if msg_text and not _is_boilerplate_user_text(msg_text):
                            if current_turn is None or not current_turn.prompt:
                                turn = ensure_turn(None, timestamp)
                                turn.prompt = msg_text
                                turn.prompt_line_count = len(msg_text.splitlines())
                    elif resp_type == "agent_message":
                        msg_text = _clean_prompt_text(_extract_text_blocks(payload.get("content")))
                        if msg_text and not _is_boilerplate_user_text(msg_text):
                            if current_turn is None or not current_turn.prompt:
                                turn = ensure_turn(None, timestamp)
                                turn.prompt = msg_text
                                turn.prompt_line_count = len(msg_text.splitlines())
                                author = payload.get("author")
                                if isinstance(author, str) and author:
                                    turn.source_label = f"Agent ({author})"

        total_cmds = sum(turn.command_count for turn in turns)
        all_files: set[str] = set()
        for turn in turns:
            all_files.update(turn.changed_files)

        first_prompt = next((turn.prompt for turn in turns if turn.prompt), "")
        first_narration = ""
        for turn in turns:
            for item in turn.timeline:
                if item.kind in ("narration", "final_answer") and item.text:
                    first_narration = item.text
                    break
            if first_narration:
                break
        fallback_title = (
            _make_title_from_prompt(first_narration, f"子代理 {agent_nickname}" if agent_nickname else session_id[:16])
            if first_narration
            else (f"子代理 {agent_nickname}" if agent_nickname else session_id[:16])
        )
        title = _make_title_from_prompt(first_prompt, fallback_title)
        if agent_nickname and is_subagent and not title.startswith(f"[{agent_nickname}]"):
            title = f"[{agent_nickname}] {title}"

        # 若最后一条 turn 仍是 running 但文件超过 2 分钟无更新，则视为已结束
        overall_status = "completed"
        if turns:
            last_turn = turns[-1]
            if last_turn.status == "failed":
                overall_status = "failed"
            elif last_turn.status == "running":
                age_sec = max(0.0, datetime.now(tz=timezone.utc).timestamp() - stat.st_mtime)
                if age_sec < 120.0:
                    overall_status = "running"
                else:
                    last_turn.status = "completed"
                    overall_status = "completed"

        summary = SessionSummary(
            session_id=session_id,
            source_kind="codex_session",
            project_name=derive_project_name(workspace),
            workspace=workspace or "未知路径",
            worktree_name=derive_worktree_name(workspace),
            title=title,
            model=latest_model,
            effort=latest_effort,
            role="astra" if (latest_model and "astra" in latest_model.lower()) else ("sol" if (latest_model and "sol" in latest_model.lower()) else None),
            turn_count=max(len(turns), 1 if first_prompt else 0),
            status=overall_status,
            started_at=started_at or mtime_iso,
            updated_at=mtime_iso,
            updated_ts=stat.st_mtime,
            latest_activity_ts=stat.st_mtime,
            originator=originator,
            is_subagent=is_subagent,
            parent_thread_id=parent_thread_id,
            agent_nickname=agent_nickname,
            codex_thread_id=session_id,
            command_count=total_cmds,
            file_count=len(all_files),
        )
        detail = (
            SessionDetail(
                summary=summary,
                source_file=str(path.resolve()),
                turns=turns,
                rate_limit_hint=rate_limit_hint,
            )
            if include_detail
            else None
        )
        return summary, detail

    def list_sessions(self, limit: int = 80, query: str | None = None) -> list[SessionSummary]:
        """返回按更新时间倒序排列的本机 Codex 会话摘要，基于文件 stat 增量缓存。"""
        files = self._discover_files()
        summaries: list[SessionSummary] = []
        lower_query = query.strip().lower() if query else ""

        with self._lock:
            for path in files:
                key = str(path.resolve())
                try:
                    stat = path.stat()
                except OSError:
                    continue
                cached = self._summary_cache.get(key)
                if cached and cached[0] == stat.st_mtime_ns and cached[1] == stat.st_size:
                    summary = cached[2]
                else:
                    try:
                        summary, _ = self._parse_rollout_file(path, include_detail=False)
                        self._summary_cache[key] = (stat.st_mtime_ns, stat.st_size, summary)
                    except (OSError, ValueError) as exc:
                        LOGGER.warning("Failed parsing Codex session %s: %s", path.name, exc)
                        continue
                self._index_by_id[summary.session_id] = path

                if lower_query:
                    haystack = " ".join(
                        filter(
                            None,
                            [
                                summary.title,
                                summary.project_name,
                                summary.workspace,
                                summary.model,
                                summary.agent_nickname,
                                summary.session_id,
                            ],
                        )
                    ).lower()
                    if lower_query not in haystack:
                        continue

                summaries.append(summary)
                if len(summaries) >= limit:
                    break
        return summaries

    def get_session_detail(self, session_id: str) -> SessionDetail | None:
        """按 session_id 读取完整会话详情，带 stat 缓存。"""
        with self._lock:
            target = self._index_by_id.get(session_id)
            if target is None or not target.exists():
                for path in self._discover_files():
                    if session_id in path.stem:
                        target = path
                        self._index_by_id[session_id] = path
                        break
            if target is None or not target.exists():
                # 兜底建立 session_id 索引，避免首次直接按 ID 读取详情时因未扫描目录返回 None
                self.list_sessions(limit=500)
                target = self._index_by_id.get(session_id)
            if target is None or not target.exists():
                return None

            key = str(target.resolve())
            try:
                stat = target.stat()
            except OSError:
                return None
            cached = self._detail_cache.get(key)
            if cached and cached[0] == stat.st_mtime_ns and cached[1] == stat.st_size:
                return cached[2]

            summary, detail = self._parse_rollout_file(target, include_detail=True)
            self._summary_cache[key] = (stat.st_mtime_ns, stat.st_size, summary)
            if detail is not None:
                self._detail_cache[key] = (stat.st_mtime_ns, stat.st_size, detail)
            return detail
