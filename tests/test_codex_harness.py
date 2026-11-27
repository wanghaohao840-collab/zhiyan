"""Codex Harness 路由、任务包、范围门禁、JSONL 监控与 MCP 协议单元测试。"""

from __future__ import annotations

import json
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from codex_harness.api.mcp_server import APP_MIME_TYPE, APP_RESOURCE_URI, build_default_server
from codex_harness.model.entities import (
    DispatchStatus,
    HarnessRole,
    ReasoningEffort,
    SandboxMode,
    ScopeAuditStatus,
    TaskType,
)
from codex_harness.repository.dispatch_repository import DispatchRepository
from codex_harness.repository.packet_repository import PacketRepository
from codex_harness.repository.session_repository import SessionRepository, parse_exec_events_file
from codex_harness.schema.contracts import DispatchRequest, DispatchRoundRecord, RoutingRequest
from codex_harness.service.dispatch_process import DispatchProcess
from codex_harness.service.dispatch_service import DispatchService
from codex_harness.service.monitor_service import MonitorService
from codex_harness.service.routing_service import RoutingService
from codex_harness.service.scope_service import ScopeService


def test_routing_service_follows_agents_md_matrix() -> None:
    svc = RoutingService()

    tx = svc.resolve(RoutingRequest(task_type=TaskType.IMPLEMENT_TRANSACTION))
    assert tx.role == HarnessRole.SOL
    assert tx.model == "gpt-6.2-sol"
    assert tx.effort == ReasoningEffort.HIGH
    assert tx.sandbox == SandboxMode.WORKSPACE_WRITE

    gen = svc.resolve(RoutingRequest(task_type=TaskType.IMPLEMENT_GENERAL))
    assert gen.effort == ReasoningEffort.MEDIUM
    assert not gen.requires_astra_review

    escalated = svc.resolve(
        RoutingRequest(
            task_type=TaskType.IMPLEMENT_GENERAL,
            involves_ownership_check=True,
            involves_concurrency_or_lease=True,
        )
    )
    assert escalated.effort == ReasoningEffort.HIGH
    assert escalated.requires_astra_review is True

    # 审查角色即使传入 workspace-write 覆盖也必须强制保持 read-only
    review = svc.resolve(
        RoutingRequest(
            task_type=TaskType.REVIEW_CONCURRENCY,
            sandbox_override=SandboxMode.WORKSPACE_WRITE,
        )
    )
    assert review.role == HarnessRole.ASTRA
    assert review.model == "gpt-6.2-astra"
    assert review.effort == ReasoningEffort.HIGH
    assert review.sandbox == SandboxMode.READ_ONLY

    chore_fallback = svc.resolve(RoutingRequest(task_type=TaskType.CHORE, luna_available=False))
    assert chore_fallback.role == HarnessRole.SOL
    assert chore_fallback.fallback_note is not None


def test_scope_service_blocks_traversal_sensitive_and_out_of_scope(tmp_path: Path) -> None:
    svc = ScopeService()
    ws = tmp_path / "repo"
    ws.mkdir()

    with pytest.raises(ValueError, match="escapes workspace"):
        svc.validate_allowed_files(ws, ["../secret.txt"])

    with pytest.raises(ValueError, match="Sensitive file"):
        svc.validate_allowed_files(ws, [".env"])

    with pytest.raises(ValueError, match="Sensitive file"):
        svc.validate_allowed_files(ws, ["auth.json"])

    allowed = svc.validate_allowed_files(ws, ["codex_harness/", "tests/test_*.py"])
    forbidden = svc.validate_forbidden_paths(ws, ["deploy/"])

    clean = svc.audit_scope(ws, allowed, forbidden, ["codex_harness/api/mcp_server.py", "tests/test_codex_harness.py"])
    assert clean.status == ScopeAuditStatus.CLEAN
    assert clean.out_of_scope_files == []

    violation = svc.audit_scope(ws, allowed, forbidden, ["codex_harness/api/mcp_server.py", "app/bootstrap.py", "deploy/run.ps1"])
    assert violation.status == ScopeAuditStatus.VIOLATION
    assert "app/bootstrap.py" in violation.out_of_scope_files
    assert "deploy/run.ps1" in violation.forbidden_hits

    ro_violation = svc.audit_scope(ws, [], [], ["codex_harness/api/mcp_server.py"], read_only_expected=True)
    assert ro_violation.status == ScopeAuditStatus.VIOLATION
    assert "codex_harness/api/mcp_server.py" in ro_violation.out_of_scope_files


def test_packet_repository_parses_dependencies_and_boundaries(tmp_path: Path) -> None:
    ws = tmp_path / "repo"
    plan_dir = ws / "docs" / "agent-workflow" / "task-packets" / "2026-11-27-demo"
    plan_dir.mkdir(parents=True)

    (plan_dir / "REVIEW.md").write_text("# Plan Review\n- Verdict: `accepted`\n", encoding="utf-8")
    (plan_dir / "01-core.md").write_text(
        """---
id: "demo-01"
title: "Core storage"
status: "done"
parallel-safe: true
depends-on: []
base-commit: "abc1234"
owner: "sol"
---
# Task Packet: Core storage

## Goal
Build core storage cleanly.

## Explicit change boundary
### Allowed files
- Create: `app/core.py`
- Test: `tests/test_core.py`
### Forbidden changes
- Do not edit `deploy/`

## Test and verification commands
```powershell
.\\venv\\Scripts\\python.exe -m pytest -q tests/test_core.py
```
""",
        encoding="utf-8",
    )
    (plan_dir / "02-api.md").write_text(
        """---
id: "demo-02"
title: "API layer"
status: "ready"
parallel-safe: false
depends-on: ["demo-01"]
---
## Goal
Expose API.
""",
        encoding="utf-8",
    )
    (plan_dir / "03-ui.md").write_text(
        """---
id: "demo-03"
title: "UI layer"
status: "ready"
parallel-safe: false
depends-on: ["demo-02"]
---
## Goal
Connect UI.
""",
        encoding="utf-8",
    )

    repo = PacketRepository(ws)
    plans = repo.list_plan_groups()
    assert len(plans) == 1
    grp = plans[0]
    assert grp.review_verdict == "accepted"
    assert grp.done_count == 1
    assert grp.total_count == 3

    p1, p2, p3 = grp.packets
    assert p1.allowed_files == ["app/core.py", "tests/test_core.py"]
    assert p1.verification_commands == [".\\venv\\Scripts\\python.exe -m pytest -q tests/test_core.py"]
    assert p2.dispatchable is True
    assert p2.unmet_dependencies == []
    assert p3.dispatchable is False
    assert p3.unmet_dependencies == ["demo-02"]


def test_session_repository_parses_rollout_and_exec_events(tmp_path: Path) -> None:
    sessions_root = tmp_path / "sessions" / "2026" / "11" / "27"
    sessions_root.mkdir(parents=True)
    rollout = sessions_root / "rollout-2026-11-27T12-00-00-thread-demo-001.jsonl"

    lines = [
        {
            "timestamp": "2026-11-27T04:00:00Z",
            "type": "session_meta",
            "payload": {
                "id": "thread-demo-001",
                "cwd": "D:\\python_self_agent",
                "originator": "Codex Desktop",
                "thread_source": "user",
                "timestamp": "2026-11-27T04:00:00Z",
            },
        },
        {
            "timestamp": "2026-11-27T04:00:01Z",
            "type": "turn_context",
            "payload": {
                "turn_id": "turn-1",
                "model": "gpt-6.2-sol",
                "effort": "high",
                "cwd": "D:\\python_self_agent",
                "sandbox_policy": {"type": "workspace-write"},
            },
        },
        {
            "timestamp": "2026-11-27T04:00:01Z",
            "type": "event_msg",
            "payload": {"type": "task_started", "turn_id": "turn-1"},
        },
        {
            "timestamp": "2026-11-27T04:00:02Z",
            "type": "event_msg",
            "payload": {
                "type": "item_completed",
                "turn_id": "turn-1",
                "item": {
                    "type": "UserMessage",
                    "content": [{"type": "text", "text": "Implement codex harness monitor plugin\nSecond line"}],
                },
            },
        },
        {
            "timestamp": "2026-11-27T04:00:03Z",
            "type": "event_msg",
            "payload": {
                "type": "item_completed",
                "turn_id": "turn-1",
                "item": {
                    "type": "AgentMessage",
                    "phase": "commentary",
                    "content": [{"type": "Text", "text": "先核对已有测试和目录结构。"}],
                },
            },
        },
        {
            "timestamp": "2026-11-27T04:00:04Z",
            "type": "event_msg",
            "payload": {
                "type": "item_completed",
                "turn_id": "turn-1",
                "item": {
                    "type": "CommandExecution",
                    "id": "cmd-1",
                    "parsed_cmd": [{"cmd": "rg -n codex_harness"}],
                    "exit_code": 0,
                    "duration": {"secs": 0, "nanos": 120_000_000},
                    "aggregated_output": "codex_harness/api/mcp_server.py:1",
                    "status": "completed",
                },
            },
        },
        {
            "timestamp": "2026-11-27T04:00:05Z",
            "type": "event_msg",
            "payload": {
                "type": "item_completed",
                "turn_id": "turn-1",
                "item": {
                    "type": "FileChange",
                    "changes": {
                        "D:\\python_self_agent\\codex_harness\\api\\mcp_server.py": {
                            "type": "update",
                            "unified_diff": "@@ -1,1 +1,2 @@\n+print('ok')",
                        }
                    },
                },
            },
        },
        {
            "timestamp": "2026-11-27T04:00:08Z",
            "type": "event_msg",
            "payload": {
                "type": "task_complete",
                "turn_id": "turn-1",
                "duration_ms": 7200,
                "last_agent_message": "已完成实现并全部测试通过。",
            },
        },
    ]
    rollout.write_text("\n".join(json.dumps(x, ensure_ascii=False) for x in lines) + "\n", encoding="utf-8")

    repo = SessionRepository(session_roots=[tmp_path / "sessions"])
    summaries = repo.list_sessions()
    assert len(summaries) == 1
    s = summaries[0]
    assert s.session_id == "thread-demo-001"
    assert s.project_name == "python_self_agent"
    assert s.model == "gpt-6.2-sol"
    assert s.effort == "high"
    assert s.command_count == 1
    assert s.file_count == 1
    assert "Implement codex harness monitor plugin" in s.title

    detail = repo.get_session_detail("thread-demo-001")
    assert detail is not None
    assert len(detail.turns) == 1
    turn = detail.turns[0]
    assert turn.duration_ms == 7200
    assert turn.prompt_line_count == 2
    assert turn.final_answer == "已完成实现并全部测试通过。"
    assert any(item.kind == "command" and item.command and item.command.exit_code == 0 for item in turn.timeline)
    assert any(item.kind == "file_change" for item in turn.timeline)

    # 同时验证 codex exec --json 事件流解析
    exec_log = tmp_path / "exec.events.jsonl"
    exec_lines = [
        {"type": "thread.started", "thread_id": "thread-exec-99"},
        {
            "type": "item.completed",
            "item": {
                "id": "item_1",
                "type": "command_execution",
                "command": "pytest -q",
                "exit_code": 0,
                "duration_ms": 450,
                "aggregated_output": "5 passed",
            },
        },
        {
            "type": "item.completed",
            "item": {
                "id": "item_2",
                "type": "agent_message",
                "text": "全部验证通过",
            },
        },
    ]
    exec_log.write_text("\n".join(json.dumps(x, ensure_ascii=False) for x in exec_lines) + "\n", encoding="utf-8")
    parsed_exec = parse_exec_events_file(exec_log)
    assert parsed_exec["codex_thread_id"] == "thread-exec-99"
    assert parsed_exec["command_count"] == 1
    assert parsed_exec["final_answer"] == "全部验证通过"


def test_dispatch_dry_run_and_monitor_dedup_and_mcp_protocol(tmp_path: Path) -> None:
    ws = tmp_path / "repo"
    ws.mkdir()
    data_dir = tmp_path / "plugin_data"
    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir()
    app_html = tmp_path / "app.html"
    app_html.write_text("<!doctype html><html><body>OK</body></html>", encoding="utf-8")

    server = build_default_server(
        workspace=ws,
        data_dir=data_dir,
        app_html_path=app_html,
        session_roots=[sessions_dir],
    )

    # 1. MCP initialize & tools/list & resources/read
    init_resp = server.handle_jsonrpc_message(
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}}
    )
    assert init_resp is not None
    assert init_resp["result"]["protocolVersion"] == "2025-06-18"
    assert "resources" in init_resp["result"]["capabilities"]

    tools_resp = server.handle_jsonrpc_message({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
    assert tools_resp is not None
    tools = {t["name"]: t for t in tools_resp["result"]["tools"]}
    assert "open_codex_harness" in tools
    entrypoints = tools["open_codex_harness"]["_meta"]["com.google.antigravity/ui"]["entrypoints"]
    assert {ep["type"] for ep in entrypoints} == {"aux_pane", "global"}
    assert tools["app_get_dashboard_snapshot"]["_meta"]["ui"]["visibility"] == ["app"]

    res_resp = server.handle_jsonrpc_message(
        {"jsonrpc": "2.0", "id": 3, "method": "resources/read", "params": {"uri": APP_RESOURCE_URI}}
    )
    assert res_resp is not None
    content_item = res_resp["result"]["contents"][0]
    assert content_item["mimeType"] == APP_MIME_TYPE
    assert "OK" in content_item["text"]

    # 2. Entrypoint 默认空参调用 open_codex_harness
    open_resp = server.handle_jsonrpc_message(
        {"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {"name": "open_codex_harness", "arguments": {}}}
    )
    assert open_resp is not None
    assert open_resp["result"]["isError"] is False
    assert "snapshot" in open_resp["result"]["structuredContent"]

    # 3. dry_run 派单预检
    dry_resp = server.handle_jsonrpc_message(
        {
            "jsonrpc": "2.0",
            "id": 5,
            "method": "tools/call",
            "params": {
                "name": "codex_dispatch",
                "arguments": {
                    "title": "审查租约并发边界",
                    "objective": "独立审查 worker 租约过期后的拒写路径。",
                    "task_type": "review_concurrency",
                    "dry_run": True,
                },
            },
        }
    )
    assert dry_resp is not None
    sc = dry_resp["result"]["structuredContent"]
    assert sc["status"] == DispatchStatus.DRY_RUN.value
    assert sc["routing"]["model"] == "gpt-6.2-astra"
    assert sc["routing"]["sandbox"] == "read-only"
    assert "STRICT SCOPE — READ-ONLY REVIEW" in sc["compiled_prompt"]

    # 4. Harness 派单记录与底层 Codex 会话按 codex_thread_id 去重合并
    dispatch_repo = DispatchRepository(data_dir)
    routing = RoutingService().resolve(RoutingRequest(task_type=TaskType.IMPLEMENT_TRANSACTION))
    run = DispatchRoundRecord(
        run_id="run-test-001",
        session_id="harness-sess-001",
        round_number=1,
        title="修复事务竞态",
        workspace=str(ws),
        project_name=ws.name,
        routing=routing,
        allowed_files=["app/worker.py"],
        compiled_prompt="Fix race condition\n",
        status=DispatchStatus.COMPLETED,
        codex_thread_id="linked-thread-777",
        started_at="2026-11-27T05:00:00Z",
        completed_at="2026-11-27T05:01:00Z",
        duration_ms=60000,
    )
    dispatch_repo.save(run)

    rollout = sessions_dir / "rollout-linked-thread-777.jsonl"
    rollout.write_text(
        json.dumps(
            {
                "timestamp": "2026-11-27T05:00:00Z",
                "type": "session_meta",
                "payload": {"id": "linked-thread-777", "cwd": str(ws), "originator": "codex_exec"},
            }
        )
        + "\n",
        encoding="utf-8",
    )

    monitor = MonitorService(
        workspace=ws,
        session_repo=SessionRepository(session_roots=[sessions_dir]),
        dispatch_repo=dispatch_repo,
        packet_repo=PacketRepository(ws),
    )
    snap = monitor.get_dashboard_snapshot()
    assert snap.total_sessions == 1
    assert snap.project_groups[0].sessions[0].source_kind == "harness"


def test_mcp_stdio_subprocess_handshake(tmp_path: Path) -> None:
    """验证真实子进程二进制 UTF-8 stdio JSON-RPC 2.0 通信无 CRLF 污染。"""
    repo_root = Path(__file__).resolve().parents[1]
    proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "codex_harness.api.mcp_server",
            "--workspace",
            str(repo_root),
            "--data-dir",
            str(tmp_path / "data"),
        ],
        cwd=str(repo_root),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        assert proc.stdin is not None and proc.stdout is not None
        req = json.dumps({"jsonrpc": "2.0", "id": 10, "method": "ping", "params": {}}) + "\n"
        proc.stdin.write(req.encode("utf-8"))
        proc.stdin.flush()
        proc.stdin.close()

        raw_out = proc.stdout.readline()
        assert raw_out.endswith(b"\n")
        assert not raw_out.endswith(b"\r\n")
        parsed = json.loads(raw_out.decode("utf-8"))
        assert parsed == {"jsonrpc": "2.0", "id": 10, "result": {}}
        proc.wait(timeout=10)
    finally:
        if proc.poll() is None:
            proc.kill()


def test_session_repository_extracts_subagent_agent_message_prompt(tmp_path: Path) -> None:
    """子代理会话缺少 UserMessage 时，应从 response_item.agent_message 提取初始派单提示。"""
    sessions_root = tmp_path / "sessions"
    sessions_root.mkdir()
    rollout = sessions_root / "rollout-sub-001.jsonl"
    lines = [
        {
            "timestamp": "2026-11-27T04:00:00Z",
            "type": "session_meta",
            "payload": {
                "id": "sub-thread-001",
                "cwd": "D:\\python_self_agent",
                "originator": "Codex Desktop",
                "thread_source": "subagent",
                "parent_thread_id": "root-thread-001",
                "agent_nickname": "Tesla",
            },
        },
        {
            "timestamp": "2026-11-27T04:00:01Z",
            "type": "response_item",
            "payload": {
                "type": "agent_message",
                "author": "root",
                "recipient": "Tesla",
                "content": [{"type": "input_text", "text": "Verify P3 document preview contract and tests"}],
            },
        },
    ]
    rollout.write_text("\n".join(json.dumps(x) for x in lines) + "\n", encoding="utf-8")

    repo = SessionRepository(session_roots=[sessions_root])
    summaries = repo.list_sessions()
    assert len(summaries) == 1
    assert "Verify P3 document preview contract" in summaries[0].title
    detail = repo.get_session_detail("sub-thread-001")
    assert detail is not None
    assert detail.turns[0].source_label == "Agent (root)"


def test_session_repository_filters_root_heartbeat_and_cleans_reasoning_and_xml(tmp_path: Path) -> None:
    """验证过滤 /root 心跳子代理噪音、去除推理摘要星号并清洗 XML/NEW_TASK 标题。"""
    sessions_root = tmp_path / "sessions"
    sessions_root.mkdir()
    rollout = sessions_root / "rollout-noise-001.jsonl"
    lines = [
        {
            "timestamp": "2026-11-27T05:00:00Z",
            "type": "session_meta",
            "payload": {"id": "noise-001", "cwd": "D:\\python_self_agent", "originator": "Codex Desktop"},
        },
        {
            "timestamp": "2026-11-27T05:00:01Z",
            "type": "event_msg",
            "payload": {
                "type": "item_completed",
                "turn_id": "t1",
                "item": {
                    "type": "UserMessage",
                    "content": [{"type": "text", "text": '<send_user_message_question_reply>[{"answers":["确认执行"]}]</send_user_message_question_reply>'}],
                },
            },
        },
        {
            "timestamp": "2026-11-27T05:00:02Z",
            "type": "event_msg",
            "payload": {
                "type": "item_completed",
                "turn_id": "t1",
                "item": {"type": "Reasoning", "summary_text": ["**Checking fence rename**", "**Running tests**"]},
            },
        },
        {
            "timestamp": "2026-11-27T05:00:03Z",
            "type": "event_msg",
            "payload": {
                "type": "item_completed",
                "turn_id": "t1",
                "item": {"type": "SubAgentActivity", "id": "s1", "kind": "interacted", "agent_path": "/root"},
            },
        },
        {
            "timestamp": "2026-11-27T05:00:04Z",
            "type": "event_msg",
            "payload": {
                "type": "item_completed",
                "turn_id": "t1",
                "item": {"type": "SubAgentActivity", "id": "s2", "kind": "started", "agent_path": "/root/ops", "agent_thread_id": "child-1"},
            },
        },
    ]
    rollout.write_text("\n".join(json.dumps(x, ensure_ascii=False) for x in lines) + "\n", encoding="utf-8")

    repo = SessionRepository(session_roots=[sessions_root])
    summaries = repo.list_sessions()
    assert summaries[0].title == "交互确认回复：确认执行"

    detail = repo.get_session_detail("noise-001")
    assert detail is not None
    tl = detail.turns[0].timeline
    reasoning_items = [it for it in tl if it.kind == "reasoning"]
    assert len(reasoning_items) == 1
    assert "**" not in (reasoning_items[0].text or "")
    assert reasoning_items[0].text == "Checking fence rename · Running tests"

    sub_items = [it for it in tl if it.kind == "subagent"]
    assert len(sub_items) == 1
    assert sub_items[0].subagent is not None
    assert sub_items[0].subagent.agent_path == "/root/ops"


def test_monitor_nests_subagents_under_root_conversation_and_merges_worktrees(tmp_path: Path) -> None:
    """同一对话历史窗口的子代理应嵌套在主对话下，且 worktree 会话应归并回主项目名。"""
    ws = tmp_path / "repo"
    ws.mkdir()
    sessions_root = tmp_path / "sessions"
    sessions_root.mkdir()

    root_file = sessions_root / "rollout-root.jsonl"
    root_file.write_text(
        "\n".join(
            json.dumps(x, ensure_ascii=False)
            for x in [
                {
                    "timestamp": "2026-11-27T06:00:00Z",
                    "type": "session_meta",
                    "payload": {
                        "id": "conv-root-1",
                        "cwd": "D:\\python_self_agent",
                        "originator": "Codex Desktop",
                        "thread_source": "user",
                    },
                },
                {
                    "timestamp": "2026-11-27T06:00:01Z",
                    "type": "event_msg",
                    "payload": {
                        "type": "item_completed",
                        "turn_id": "t1",
                        "item": {"type": "UserMessage", "content": [{"type": "text", "text": "一、逐项核对总览\\"}]},
                    },
                },
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    sub_file = sessions_root / "rollout-sub.jsonl"
    sub_file.write_text(
        "\n".join(
            json.dumps(x, ensure_ascii=False)
            for x in [
                {
                    "timestamp": "2026-11-27T06:05:00Z",
                    "type": "session_meta",
                    "payload": {
                        "id": "conv-sub-1",
                        "cwd": "C:\\worktrees\\release13\\python_self_agent",
                        "originator": "Codex Desktop",
                        "thread_source": "subagent",
                        "parent_thread_id": "conv-root-1",
                        "agent_nickname": "Popper",
                    },
                },
                {
                    "timestamp": "2026-11-27T06:05:01Z",
                    "type": "event_msg",
                    "payload": {
                        "type": "item_completed",
                        "turn_id": "t1",
                        "item": {
                            "type": "AgentMessage",
                            "phase": "commentary",
                            "content": [{"type": "Text", "text": "检查备份与恢复脚本"}],
                        },
                    },
                },
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    monitor = MonitorService(
        workspace=ws,
        session_repo=SessionRepository(session_roots=[sessions_root]),
        dispatch_repo=DispatchRepository(tmp_path / "data"),
        packet_repo=PacketRepository(ws),
    )

    snap = monitor.get_dashboard_snapshot()
    assert len(snap.project_groups) == 1
    grp = snap.project_groups[0]
    assert grp.project_name == "python_self_agent"
    assert grp.session_count == 1
    assert grp.total_thread_count == 2
    root_node = grp.sessions[0]
    assert root_node.title == "一、逐项核对总览"
    assert root_node.child_count == 1
    assert root_node.child_sessions[0].agent_nickname == "Popper"
    assert root_node.child_sessions[0].worktree_name == "release13"

    # 搜索子代理关键字时仍保留父对话窗口并展示命中的子代理
    search_snap = monitor.get_dashboard_snapshot(query="Popper")
    assert len(search_snap.project_groups) == 1
    assert search_snap.project_groups[0].sessions[0].session_id == "conv-root-1"
    assert len(search_snap.project_groups[0].sessions[0].child_sessions) == 1


def test_spec_inference_auto_generates_industry_dod_allowlist_and_commands(tmp_path: Path) -> None:
    """验证按方案与行业软件模块完成标准自动推导目标、真实白名单、验收命令与干跑自动补全。"""
    ws = tmp_path / "repo"
    (ws / "docs" / "superpowers" / "plans").mkdir(parents=True)
    (ws / "api" / "routes").mkdir(parents=True)
    (ws / "tests" / "api").mkdir(parents=True)
    (ws / "web" / "src" / "pages").mkdir(parents=True)

    (ws / "api" / "routes" / "documents.py").write_text("# docs route\n", encoding="utf-8")
    (ws / "tests" / "api" / "test_documents_routes.py").write_text("def test_ok(): assert True\n", encoding="utf-8")
    (ws / "web" / "package.json").write_text('{"name":"demo-web"}\n', encoding="utf-8")
    (ws / "web" / "src" / "pages" / "DocumentsPage.tsx").write_text("export function DocumentsPage() { return null }\n", encoding="utf-8")
    (ws / ".env").write_text("SECRET=1\n", encoding="utf-8")

    plan = ws / "docs" / "superpowers" / "plans" / "2026-11-26-p3-preview.md"
    plan.write_text(
        """# P3 原文预览与页面入口计划
- [ ] 补齐 `api/routes/documents.py` 原文预览归属鉴权与 415/404 校验
- [ ] 更新 `web/src/pages/DocumentsPage.tsx` 预览入口，同时忽略 `.env`
""",
        encoding="utf-8",
    )

    server = build_default_server(
        workspace=ws,
        data_dir=tmp_path / "data",
        session_roots=[tmp_path / "sessions"],
    )

    infer_res = server.call_tool(
        "codex_infer_dispatch_spec",
        {
            "title": "续轮派单：续轮派单：核对 P3 计划、原文预览接口和现有页面入口",
            "workspace": str(ws),
        },
    )
    assert infer_res["isError"] is False
    sc = infer_res["structuredContent"]
    assert sc["suggested_title"] == "核对 P3 计划、原文预览接口和现有页面入口"
    assert "docs/superpowers/plans/2026-11-26-p3-preview.md" in sc["matched_plan_docs"]
    assert any("API" in dom for dom in sc["matched_domains"])
    assert any("前端" in dom for dom in sc["matched_domains"])
    assert "api/routes/documents.py" in sc["allowed_files"]
    assert "tests/api/test_documents_routes.py" in sc["allowed_files"]
    assert ".env" not in sc["allowed_files"]
    assert any("pytest" in cmd for cmd in sc["verification_commands"])
    assert any("npm --prefix web run typecheck" in cmd for cmd in sc["verification_commands"])
    assert len(sc["acceptance_criteria"]) >= 3

    # 验证 codex_dispatch 留空 objective/allowed_files/verification_commands 时自动推导补全
    dry_res = server.call_tool(
        "codex_dispatch",
        {
            "title": "续轮派单：核对 P3 计划与原文预览接口",
            "workspace": str(ws),
            "auto_infer": True,
            "dry_run": True,
        },
    )
    assert dry_res["isError"] is False
    dry_sc = dry_res["structuredContent"]
    assert dry_sc["status"] == DispatchStatus.DRY_RUN.value
    assert "api/routes/documents.py" in dry_sc["compiled_prompt"]
    assert "Verification Commands" in dry_sc["compiled_prompt"]


def test_dispatch_command_overrides_codex_home_isolation_and_error_dedup(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """回归：禁用 web_search 防 index_gated_web_access 400、隔离 CODEX_HOME、忽略 skills/.system 误报并去重报错。"""
    ws = tmp_path / "repo"
    ws.mkdir()
    dispatch_svc = DispatchService(
        default_workspace=ws,
        dispatch_repo=DispatchRepository(tmp_path / "data"),
        routing_service=RoutingService(),
        scope_service=ScopeService(),
    )
    routing = RoutingService().resolve(RoutingRequest(task_type=TaskType.REVIEW_CONCURRENCY))

    cmd_fresh = dispatch_svc._build_codex_command("codex.exe", ws, routing, resume_thread_id=None)
    assert 'web_search="disabled"' in cmd_fresh
    assert "mcp_servers.codex-harness.enabled=false" in cmd_fresh
    if sys.platform == "win32":
        assert 'windows.sandbox="unelevated"' in cmd_fresh

    cmd_resume = dispatch_svc._build_codex_command("codex.exe", ws, routing, resume_thread_id="thread-abc")
    assert 'web_search="disabled"' in cmd_resume
    assert 'sandbox_mode="read-only"' in cmd_resume
    if sys.platform == "win32":
        assert 'windows.sandbox="unelevated"' in cmd_resume

    monkeypatch.setenv("CODEX_HOME", str(ws))
    child_env = dispatch_svc._build_child_env(ws)
    assert Path(child_env["CODEX_HOME"]).resolve() != ws.resolve()

    # 只读审查忽略 Codex 运行时自动释放的 skills/.system/*
    audit = ScopeService().audit_scope(
        workspace=ws,
        allowed_files=[],
        forbidden_paths=[],
        observed_files=[
            "skills/.system/plugin-creator/SKILL.md",
            "skills/.system/openai-docs/scripts/resolve-latest-model-info.js",
            "logs_2.sqlite",
        ],
        read_only_expected=True,
    )
    assert audit.status == ScopeAuditStatus.CLEAN
    assert audit.out_of_scope_files == []

    # 去重相同 JSON 错误并提取人类可读消息
    ev = tmp_path / "dup_err.events.jsonl"
    raw_json_err = json.dumps({"error": {"message": "Unknown parameter: 'tools[32].index_gated_web_access'.", "code": "unknown_parameter"}})
    ev.write_text(
        "\n".join(
            [
                json.dumps({"type": "error", "message": raw_json_err}),
                json.dumps({"type": "turn.failed", "error": {"message": raw_json_err}}),
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    parsed = parse_exec_events_file(ev)
    assert len(parsed["timeline"]) == 1
    assert parsed["error_message"] == "Unknown parameter: 'tools[32].index_gated_web_access'. (unknown_parameter)"


def test_dispatch_auto_forks_on_active_writer_thread_conflict(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """续轮目标线程被 Codex Desktop 占用写锁时，应自动继承上下文新开线程并归入原对话树。"""
    ws = tmp_path / "repo"
    ws.mkdir()
    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir()
    data_dir = tmp_path / "data"

    # 准备一个被桌面端锁定的原始 Codex 会话
    locked_id = "locked-thread-001"
    rollout = sessions_dir / "rollout-locked-001.jsonl"
    rollout_lines = [
        {
            "timestamp": "2026-11-27T10:00:00Z",
            "type": "session_meta",
            "payload": {
                "id": locked_id,
                "cwd": str(ws),
                "originator": "Codex Desktop",
                "thread_source": "user",
            },
        },
        {
            "timestamp": "2026-11-27T10:00:01Z",
            "type": "event_msg",
            "payload": {
                "type": "item_completed",
                "turn_id": "t1",
                "item": {
                    "type": "UserMessage",
                    "content": [{"type": "text", "text": "核对 Worker 租约并发拒写边界"}],
                },
            },
        },
        {
            "timestamp": "2026-11-27T10:00:05Z",
            "type": "event_msg",
            "payload": {
                "type": "item_completed",
                "turn_id": "t1",
                "item": {
                    "type": "FileChange",
                    "changes": {str(ws / "app" / "qa_worker.py"): {"type": "update", "unified_diff": "@@ -1 +1 @@"}},
                },
            },
        },
        {
            "timestamp": "2026-11-27T10:00:09Z",
            "type": "event_msg",
            "payload": {
                "type": "task_complete",
                "turn_id": "t1",
                "duration_ms": 8000,
                "last_agent_message": "已完成第一轮租约竞态排查，待补充回归测试。",
            },
        },
    ]
    rollout.write_text("\n".join(json.dumps(x, ensure_ascii=False) for x in rollout_lines) + "\n", encoding="utf-8")

    session_repo = SessionRepository(session_roots=[sessions_dir])
    dispatch_repo = DispatchRepository(data_dir)
    routing_svc = RoutingService()
    scope_svc = ScopeService()
    dispatch_svc = DispatchService(
        default_workspace=ws,
        dispatch_repo=dispatch_repo,
        routing_service=routing_svc,
        scope_service=scope_svc,
        session_repo=session_repo,
    )

    routing = routing_svc.resolve(RoutingRequest(task_type=TaskType.REVIEW_CONCURRENCY))
    record = DispatchRoundRecord(
        run_id="run-lock-001",
        session_id="harness-lock-001",
        round_number=1,
        title="核对 Worker 租约并发拒写边界",
        workspace=str(ws),
        project_name=ws.name,
        routing=routing,
        compiled_prompt="# Antigravity -> Codex Explicit Dispatch\n## Objective\n复查迟到写入拒写路径。\n",
        status=DispatchStatus.RUNNING,
        codex_thread_id=locked_id,
        started_at="2026-11-27T10:27:58Z",
        events_file=str(dispatch_repo.events_path("run-lock-001")),
    )
    dispatch_repo.save(record)

    calls: list[dict[str, object]] = []

    def fake_attempt(
        self: DispatchService,
        record: DispatchRoundRecord,
        cmd: list[str],
        prompt_text: str,
        workspace: Path,
        events_path: Path,
        start_mono: float,
    ) -> tuple[int, list[str], bool]:
        calls.append({"cmd": list(cmd), "prompt": prompt_text})
        if len(calls) == 1:
            return (
                1,
                [
                    f"2026-11-27T10:27:58.512Z ERROR codex_core::session: thread-store conflict: thread {locked_id} already has an active writer",
                    f"Error: thread/resume: thread/resume failed: thread {locked_id} already has an active writer (code -32600)",
                ],
                False,
            )
        dispatch_repo.append_event_line(
            record.run_id,
            json.dumps({"type": "thread.started", "thread_id": "fresh-thread-999"}),
        )
        dispatch_repo.append_event_line(
            record.run_id,
            json.dumps(
                {
                    "type": "item.completed",
                    "item": {"id": "m1", "type": "agent_message", "text": "LOCK_FALLBACK_VERIFIED"},
                }
            ),
        )
        return 0, [], False

    monkeypatch.setattr(DispatchService, "_execute_single_attempt", fake_attempt)
    resume_cmd = dispatch_svc._build_codex_command("codex.exe", ws, routing, resume_thread_id=locked_id)
    dispatch_svc._run_codex_process(
        record=record,
        codex_bin="codex.exe",
        cmd=resume_cmd,
        workspace=ws,
        resume_thread_id=locked_id,
    )

    assert len(calls) == 2
    assert "resume" in calls[0]["cmd"]  # type: ignore[operator]
    assert "resume" not in calls[1]["cmd"]  # type: ignore[operator]
    retry_prompt = str(calls[1]["prompt"])
    assert retry_prompt.startswith("# Antigravity -> Codex Explicit Dispatch")
    assert "## Inherited Context" in retry_prompt
    assert "核对 Worker 租约并发拒写边界" in retry_prompt
    assert "app/qa_worker.py" in retry_prompt or "qa_worker.py" in retry_prompt
    assert "已完成第一轮租约竞态排查" in retry_prompt

    # 验证标题提取跳过 Inherited Context 与显式协议头，并准确生成只读审查 Objective
    from codex_harness.repository.session_repository import _make_title_from_prompt

    assert (
        _make_title_from_prompt(
            "## Inherited Context (forked because thread `abc` is locked)\n# Antigravity -> Codex Explicit Dispatch\nTitle: 核查租约拒写边界\n",
            "fallback",
        )
        == "核查租约拒写边界"
    )
    server = build_default_server(workspace=ws, data_dir=tmp_path / "data2", session_roots=[sessions_dir])
    review_infer = server.call_tool(
        "codex_infer_dispatch_spec",
        {
            "title": "Inherited Context (forked because thread `01a2` is locked)",
            "workspace": str(ws),
            "task_type": "review_concurrency",
        },
    )["structuredContent"]
    assert "Inherited Context" not in review_infer["suggested_title"]
    assert "只读" in review_infer["objective"]
    assert "完成受控实现" not in review_infer["objective"]

    saved = dispatch_repo.get("run-lock-001")
    assert saved is not None
    assert saved.status == DispatchStatus.COMPLETED
    assert saved.codex_thread_id == "fresh-thread-999"
    assert saved.parent_thread_id == locked_id
    assert saved.fallback_from_locked_thread == locked_id
    assert saved.error_message is None
    assert saved.final_answer == "LOCK_FALLBACK_VERIFIED"

    # 事件流中写入了合法 JSON 提示节点，而非报错残留
    parsed_ev = parse_exec_events_file(dispatch_repo.events_path("run-lock-001"))
    assert parsed_ev["error_message"] is None
    assert any(it.kind == "reasoning" and "写入锁" in (it.text or "") for it in parsed_ev["timeline"])

    # 监控侧栏应将该自动新开的 Harness 会话嵌套归入原锁定对话窗口下
    monitor = MonitorService(
        workspace=ws,
        session_repo=session_repo,
        dispatch_repo=dispatch_repo,
        packet_repo=PacketRepository(ws),
    )
    snap = monitor.get_dashboard_snapshot()
    assert len(snap.project_groups) == 1
    roots = snap.project_groups[0].sessions
    assert len(roots) == 1
    assert roots[0].session_id == locked_id
    assert roots[0].child_count == 1
    assert roots[0].child_sessions[0].session_id == "harness-lock-001"


@pytest.mark.parametrize("noisy", [False, True])
def test_other_server_requests_cancellation_until_owner_confirms_exit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, noisy: bool
) -> None:
    data_dir = tmp_path / "data"
    repo = DispatchRepository(data_dir)
    owner = build_default_server(tmp_path, data_dir, session_roots=[])
    other = build_default_server(tmp_path, data_dir, session_roots=[])
    monkeypatch.setattr("codex_harness.service.dispatch_service.resolve_codex_executable", lambda: sys.executable)
    child_code = (
        "import sys,time\n"
        f"while True:\n if {noisy!r}: print('{{}}',flush=True)\n time.sleep(.01)\n"
    )
    proc_command = [sys.executable, "-u", "-c", child_code]
    monkeypatch.setattr(DispatchService, "_build_codex_command", lambda *a, **kw: proc_command)
    signal_started = threading.Event()
    allow_terminate = threading.Event()
    original_stop = DispatchProcess.stop
    observed_procs = []

    def delayed_stop(process_tree):
        observed_procs.append(process_tree.process)
        signal_started.set()
        allow_terminate.wait(5)
        return original_stop(process_tree)

    monkeypatch.setattr(DispatchProcess, "stop", delayed_stop)
    launched = owner.call_tool("codex_dispatch", {
        "title": "Synthetic cancellation test", "objective": "Exercise local cancel", "auto_infer": False,
    })["structuredContent"]
    run_id = launched["run_id"]
    try:
        _wait_for_dispatch(repo, run_id, lambda rec: rec.pid is not None)
        response = other.call_tool("app_cancel_dispatch", {"run_id": run_id})
        assert response["structuredContent"]["status"] == DispatchStatus.RUNNING
        assert signal_started.wait(3)
        assert observed_procs[0].poll() is None
        assert other.call_tool("codex_cancel_dispatch", {"run_id": run_id})["structuredContent"]["status"] == DispatchStatus.RUNNING
        assert repo.get(run_id).status == DispatchStatus.RUNNING
    finally:
        allow_terminate.set()
        owner.call_tool("codex_cancel_dispatch", {"run_id": run_id})

    terminal = _wait_for_dispatch(repo, run_id, lambda rec: rec.completed_at is not None)
    assert terminal.status == DispatchStatus.CANCELLED
    assert terminal.exit_code is not None
    assert observed_procs[0].poll() == terminal.exit_code
    assert len(observed_procs) == 1


def _wait_for_dispatch(repo, run_id, predicate):
    deadline = time.monotonic() + 6
    while time.monotonic() < deadline:
        record = repo.get(run_id)
        if record is not None and predicate(record):
            return record
        time.sleep(0.02)
    pytest.fail(f"Dispatch {run_id} did not reach the expected state")


def test_packet_scope_is_a_hard_limit_for_inference_and_dispatch(tmp_path: Path) -> None:
    packet_dir = tmp_path / "docs" / "agent-workflow" / "task-packets" / "demo-plan"
    packet_dir.mkdir(parents=True)
    (tmp_path / "api" / "routes").mkdir(parents=True)
    (tmp_path / "api" / "routes" / "documents.py").touch()
    (tmp_path / "service" / "inside").mkdir(parents=True)
    (tmp_path / "service" / "inside" / "edit.py").touch()
    packet = packet_dir / "01-boundary.md"
    packet.write_text(
        "---\nid: demo-01\nstatus: ready\n---\n# API receipt\n\n"
        "## Goal\nCompare `api/routes/documents.py` while changing the receipt.\n\n"
        "## Explicit change boundary\n### Allowed files\n"
        "- Modify: `service/inside/`\n- Test: `tests/test_receipt.py`\n",
        encoding="utf-8",
    )
    server = build_default_server(tmp_path, tmp_path / "data", session_roots=[])
    common = {"title": "Update API receipt", "packet_path": packet.relative_to(tmp_path).as_posix()}
    inferred = server.call_tool("app_infer_dispatch_spec", common)
    assert inferred["structuredContent"]["allowed_files"] == ["service/inside/", "tests/test_receipt.py"]
    shrunk = server.call_tool("app_infer_dispatch_spec", {
        **common, "allowed_files": ["service/inside/edit.py"],
    })
    assert shrunk["structuredContent"]["allowed_files"] == ["service/inside/edit.py"]
    dry_request = {**common, "objective": "Update receipt", "dry_run": True}
    for forbidden in ("api/routes/documents.py", "service/", "tests/test_receipt.py/", "tests/test_receipt.py/child", "service/in*"):
        denied = server.call_tool("codex_dispatch", {**dry_request, "allowed_files": [forbidden]})
        assert denied["isError"] is True, forbidden
    dry = server.call_tool("app_create_dispatch", {
        **dry_request, "allowed_files": ["service/inside/edit.py"],
    })["structuredContent"]
    assert "- `service/inside/edit.py`" in dry["compiled_prompt"]
    assert "- `tests/test_receipt.py`" not in dry["compiled_prompt"]

    reviewed = {**dry_request, "allowed_files": [], "sandbox_override": "workspace-write"}
    dry = server.call_tool("codex_dispatch", reviewed)["structuredContent"]
    assert dry["routing"]["sandbox"] == SandboxMode.READ_ONLY
    assert "READ-ONLY REVIEW" in dry["compiled_prompt"]

    empty = packet_dir / "02-empty.md"
    empty.write_text(
        "---\nid: demo-02\nstatus: ready\n---\n# API receipt\n\n"
        "## Goal\nInspect `api/routes/documents.py`.\n",
        encoding="utf-8",
    )
    empty_request = {**dry_request, "packet_path": empty.relative_to(tmp_path).as_posix()}
    preview = server.call_tool("codex_infer_dispatch_spec", empty_request)["structuredContent"]
    assert preview["allowed_files"] == []
    assert preview["routing_preview"]["sandbox"] == SandboxMode.READ_ONLY
    assert server.call_tool("codex_dispatch", empty_request)["structuredContent"]["routing"]["sandbox"] == SandboxMode.READ_ONLY
    assert server.call_tool("codex_dispatch", {
        **empty_request, "allowed_files": ["api/routes/documents.py"],
    })["isError"] is True


def test_inferred_and_explicit_risk_route_same_for_mcp_preview_and_execution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data_dir = tmp_path / "data"
    server = build_default_server(tmp_path, data_dir, session_roots=[])
    # Use a local Python child for the non-dry-run path.
    monkeypatch.setattr("codex_harness.service.dispatch_service.resolve_codex_executable", lambda: sys.executable)
    monkeypatch.setattr(DispatchService, "_build_codex_command", lambda *a, **kw: [sys.executable, "-c", "pass"])
    title = "修复 Worker 租约过期后的迟到写入竞态"
    objective = "验证跨存储一致性与回退安全"
    complete_spec = {
        "title": title, "objective": objective, "allowed_files": ["app/worker.py"],
        "verification_commands": ["python -m pytest"], "acceptance_criteria": ["拒绝旧 Worker 写入"],
    }
    expected = server.call_tool("codex_infer_dispatch_spec", {
        "title": title, "objective_hint": objective,
    })["structuredContent"]
    assert expected["suggested_task_type"] == TaskType.IMPLEMENT_TRANSACTION
    assert expected["suggested_involves_concurrency_or_lease"] is True
    assert expected["routing_preview"]["requires_astra_review"] is True
    for tool_name in ("codex_dispatch", "app_create_dispatch"):
        dry = server.call_tool(tool_name, {**complete_spec, "dry_run": True})["structuredContent"]
        assert dry["routing"] == expected["routing_preview"]

    launched = server.call_tool("codex_dispatch", complete_spec)["structuredContent"]
    record = _wait_for_dispatch(DispatchRepository(data_dir), launched["run_id"], lambda rec: rec.completed_at)
    assert record.routing.model_dump() == expected["routing_preview"]

    overrides = {
        "task_type": "implement_general", "involves_ownership_check": False,
        "involves_concurrency_or_lease": False, "involves_cross_store_or_rollback": False,
        "is_complex": False,
    }
    expected = server.call_tool("app_infer_dispatch_spec", {
        "title": title, "objective_hint": objective, **overrides,
    })["structuredContent"]
    assert expected["suggested_involves_concurrency_or_lease"] is False
    assert expected["routing_preview"]["effort"] == ReasoningEffort.MEDIUM
    dry = server.call_tool("app_create_dispatch", {
        **complete_spec, **overrides, "dry_run": True,
    })["structuredContent"]
    assert dry["routing"] == expected["routing_preview"]


def test_public_inference_and_preview_preserve_followup_scope(tmp_path: Path) -> None:
    root = tmp_path / "docs/agent-workflow/task-packets/sample"
    root.mkdir(parents=True)
    packet = root / "01-empty.md"
    packet.write_text("---\nid: empty\nstatus: ready\n---\n# Review receipt\n", encoding="utf-8")
    data = tmp_path / "data"
    server = build_default_server(tmp_path, data, session_roots=[])
    repo = DispatchRepository(data)
    record = DispatchRoundRecord(
        run_id="run-bounded", session_id="session-bounded", round_number=1,
        title="Receipt", workspace=str(tmp_path), project_name="sample",
        packet_id="empty", packet_path=packet.relative_to(tmp_path).as_posix(),
        routing=RoutingService().resolve(RoutingRequest()), compiled_prompt="Receipt",
        started_at="2026-11-26T12:00:00Z", codex_thread_id="thread-bounded",
        status=DispatchStatus.COMPLETED,
    )
    repo.save(record)
    for link in [{"resume_run_id": record.run_id}, {"resume_codex_thread_id": record.codex_thread_id}]:
        for tool_name in ("codex_infer_dispatch_spec", "app_infer_dispatch_spec", "app_preview_dispatch"):
            reply = server.call_tool(tool_name, {**link, "title": "Continue"})
            assert not reply["isError"]
            preview = reply["structuredContent"]
            assert preview["routing_preview"]["sandbox"] == SandboxMode.READ_ONLY
            assert preview["allowed_files"] == []
        dry = server.call_tool("codex_dispatch", {**link, "title": "Continue", "dry_run": True})
        assert dry["structuredContent"]["routing"] == preview["routing_preview"]
    conflicting = {
        "title": "Continue", "resume_run_id": record.run_id, "resume_codex_thread_id": "another-thread",
    }
    for tool_name in ("codex_infer_dispatch_spec", "app_preview_dispatch", "codex_dispatch"):
        assert server.call_tool(tool_name, {**conflicting, "dry_run": True})["isError"]
    args = {"title": "Update configuration", "allowed_files": ["service/x.py"]}
    for tool_name in ("codex_infer_dispatch_spec", "app_preview_dispatch"):
        assert server.call_tool(tool_name, args)["structuredContent"]["allowed_files"] == args["allowed_files"]


def test_install_plugin_generates_valid_bundle(tmp_path: Path) -> None:
    """验证 install_plugin 生成完整的 plugin.json、mcp_config.json、launcher 与单文件 HTML。"""
    from codex_harness.install_plugin import install_plugin

    repo_root = Path(__file__).resolve().parents[1]
    target = tmp_path / "codex-harness"
    result = install_plugin(repo_root=repo_root, target_dir=target)

    assert (target / "plugin.json").exists()
    assert (target / "mcp_config.json").exists()
    assert (target / "bin" / "run_server.py").exists()
    assert (target / "dist" / "app.html").exists()
    assert (target / "assets" / "logo.svg").exists()
    assert (target / "rules" / "AGENTS.md").exists()
    assert (target / "skills" / "codex-harness" / "SKILL.md").exists()
    assert Path(result["app_html"]).stat().st_size > 10_000

