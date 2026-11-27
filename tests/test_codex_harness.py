"""Codex Harness 路由、任务包、范围门禁、JSONL 监控与 MCP 协议单元测试。"""

from __future__ import annotations

import json
import subprocess
import sys
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

