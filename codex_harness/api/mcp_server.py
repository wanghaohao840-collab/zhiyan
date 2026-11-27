"""Antigravity MCP App + Codex Harness 标准 stdio JSON-RPC 2.0 服务端。"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from codex_harness import __version__
from codex_harness.model.entities import DispatchStatus, SandboxMode
from codex_harness.repository.dispatch_repository import DispatchRepository
from codex_harness.repository.packet_repository import PacketRepository
from codex_harness.repository.session_repository import SessionRepository
from codex_harness.schema.contracts import DispatchRequest, RoutingRequest, SpecInferenceRequest
from codex_harness.service.dispatch_service import DispatchService
from codex_harness.service.monitor_service import MonitorService
from codex_harness.service.routing_service import RoutingService
from codex_harness.service.scope_service import ScopeService
from codex_harness.service.spec_inference_service import SpecInferenceService

LOGGER = logging.getLogger(__name__)

APP_RESOURCE_URI = "ui://codex-harness/app.html"
APP_MIME_TYPE = "text/html;profile=mcp-app"
SUPPORTED_PROTOCOL_VERSIONS: tuple[str, ...] = (
    "2025-11-25",
    "2025-06-18",
    "2025-03-26",
    "2024-11-05",
)

PANE_META: dict[str, Any] = {
    "ui": {"resourceUri": APP_RESOURCE_URI},
    "com.google.antigravity/ui": {
        "entrypoints": [
            {"type": "aux_pane", "title": "Codex Monitor"},
            {"type": "global", "title": "Codex Monitor"},
        ]
    },
}
INLINE_UI_META: dict[str, Any] = {"ui": {"resourceUri": APP_RESOURCE_URI}}
APP_ONLY_META: dict[str, Any] = {"ui": {"visibility": ["app"]}}


def _default_app_html_paths() -> list[Path]:
    """按优先级返回单文件 MCP App HTML 候选路径。"""
    candidates: list[Path] = []
    env_html = os.getenv("CODEX_HARNESS_APP_HTML")
    if env_html:
        candidates.append(Path(env_html))
    pkg_dir = Path(__file__).resolve().parent.parent
    candidates.append(pkg_dir / "ui" / "dist" / "index.html")
    candidates.append(pkg_dir / "dist" / "app.html")
    return candidates


class CodexHarnessMcpServer:
    """无额外第三方 MCP SDK 依赖的确定性 JSON-RPC 2.0 stdio 服务。"""

    def __init__(
        self,
        workspace: Path,
        data_dir: Path,
        session_repo: SessionRepository,
        dispatch_repo: DispatchRepository,
        packet_repo: PacketRepository,
        routing_service: RoutingService,
        scope_service: ScopeService,
        dispatch_service: DispatchService,
        monitor_service: MonitorService,
        spec_inference_service: SpecInferenceService,
        app_html_path: Path | None = None,
    ) -> None:
        self._workspace = workspace.resolve()
        self._data_dir = data_dir.resolve()
        self._session_repo = session_repo
        self._dispatch_repo = dispatch_repo
        self._packet_repo = packet_repo
        self._routing_service = routing_service
        self._scope_service = scope_service
        self._dispatch_service = dispatch_service
        self._monitor_service = monitor_service
        self._spec_inference_service = spec_inference_service
        self._app_html_path = app_html_path

    def _load_app_html(self) -> str:
        """读取单文件前端 HTML，支持环境变量热覆盖以便 UI 调试。"""
        paths: list[Path] = []
        if self._app_html_path is not None:
            paths.append(self._app_html_path)
        paths.extend(_default_app_html_paths())
        for candidate in paths:
            if candidate.exists() and candidate.is_file():
                text = candidate.read_text(encoding="utf-8")
                if text.strip():
                    return text
        return (
            "<!doctype html><html lang='zh-CN'><head><meta charset='utf-8'/>"
            "<title>Codex Monitor</title></head>"
            "<body style='font-family:system-ui,sans-serif;padding:24px'>"
            "<h3>Codex Harness UI 尚未构建</h3>"
            "<p>请在 <code>codex_harness/ui</code> 目录执行 <code>npm install && npm run build</code>。</p>"
            "</body></html>"
        )

    def list_tools(self) -> list[dict[str, Any]]:
        """返回面向 Antigravity 模型与前端 App 的全部 MCP 工具元数据。"""
        return [
            {
                "name": "open_codex_harness",
                "title": "Codex Monitor",
                "description": "打开 Antigravity × Codex 显式 Harness 与单向实时监控侧边栏面板。",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "session_id": {"type": "string", "description": "可选：初始聚焦的会话或派单 ID"},
                        "view": {
                            "type": "string",
                            "enum": ["monitor", "packets", "dispatch"],
                            "description": "可选：初始标签视图",
                        },
                    },
                },
                "_meta": PANE_META,
            },
            {
                "name": "codex_route_policy",
                "title": "路由 Codex 模型与门禁",
                "description": "依据 AGENTS.md 模型分工规则，根据任务类型与风险标志返回推荐角色、模型、推理强度、沙箱和强制门禁。",
                "inputSchema": RoutingRequest.model_json_schema(),
            },
            {
                "name": "codex_infer_dispatch_spec",
                "title": "按方案与行业标准智能推导派单规格",
                "description": "根据仓库方案文档、续轮会话上下文与行业软件功能模块完成标准，自动推导目标与可验证完成条件、允许改动文件白名单、验收命令与推荐风险路由。",
                "inputSchema": SpecInferenceRequest.model_json_schema(),
            },
            {
                "name": "codex_list_task_packets",
                "title": "列出仓库任务包与门禁状态",
                "description": "扫描 docs/agent-workflow/task-packets/，返回各计划的 REVIEW 结论、任务包依赖就绪状态及允许改动文件边界。",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "plan_filter": {"type": "string", "description": "可选：按计划目录名子串过滤"},
                    },
                },
            },
            {
                "name": "codex_dispatch",
                "title": "向 Codex 发起显式 Harness 派单",
                "description": "按显式契约（目标、模型路由、严格改动白名单、验证命令、任务包依赖门禁）向本机 codex exec 派发任务并开始实时追踪。建议先用 dry_run=true 预检。",
                "inputSchema": DispatchRequest.model_json_schema(),
                "_meta": INLINE_UI_META,
            },
            {
                "name": "codex_dispatch_status",
                "title": "查询 Codex 派单实时状态与审计结果",
                "description": "查询指定 run_id / session_id 的执行进度、命令数、改动文件、范围门禁审计与最终答复；不传参数时返回最近派单列表。",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "run_id": {"type": "string", "description": "可选：指定派单 run_id 或 session_id"},
                    },
                },
            },
            {
                "name": "codex_cancel_dispatch",
                "title": "中止运行中的 Codex 派单",
                "description": "请求中止指定 run_id，由持有后台 codex exec 进程的服务确认退出后更新状态。",
                "inputSchema": {
                    "type": "object",
                    "required": ["run_id"],
                    "properties": {
                        "run_id": {"type": "string", "description": "要中止的派单 run_id"},
                    },
                },
            },
            {
                "name": "codex_verify_scope",
                "title": "校验改动范围白名单门禁",
                "description": "比对当前工作区 Git 改动或指定文件列表与 allowed_files / forbidden_paths，检测越界修改与敏感文件触碰。",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "run_id": {"type": "string", "description": "可选：直接审计指定派单记录"},
                        "allowed_files": {"type": "array", "items": {"type": "string"}},
                        "forbidden_paths": {"type": "array", "items": {"type": "string"}},
                        "observed_files": {"type": "array", "items": {"type": "string"}},
                        "read_only_expected": {"type": "boolean"},
                    },
                },
            },
            # === 仅供 MCP App 前端调用的只读/交互工具（不暴露给模型轨迹） ===
            {
                "name": "app_get_dashboard_snapshot",
                "title": "App 获取左侧项目会话树快照",
                "description": "前端面板轮询接口：返回按项目分组的 Codex 与 Harness 会话摘要。",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string"},
                        "limit": {"type": "integer", "minimum": 1, "maximum": 200},
                    },
                },
                "_meta": APP_ONLY_META,
            },
            {
                "name": "app_get_session_detail",
                "title": "App 获取会话完整监控轨迹",
                "description": "前端面板详情接口：按 session_id 返回轮次、派单内容、命令、改动文件与门禁审计。",
                "inputSchema": {
                    "type": "object",
                    "required": ["session_id"],
                    "properties": {
                        "session_id": {"type": "string"},
                    },
                },
                "_meta": APP_ONLY_META,
            },
            {
                "name": "app_get_task_packets",
                "title": "App 获取任务包列表",
                "description": "前端面板任务包视图接口。",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "plan_filter": {"type": "string"},
                    },
                },
                "_meta": APP_ONLY_META,
            },
            {
                "name": "app_preview_routing",
                "title": "App 预览模型路由决策",
                "description": "前端派单表单实时预览 AGENTS.md 路由结果。",
                "inputSchema": RoutingRequest.model_json_schema(),
                "_meta": APP_ONLY_META,
            },
            {
                "name": "app_list_plan_docs",
                "title": "App 获取仓库方案文档列表",
                "description": "前端派单表单加载 docs/superpowers/plans 与 docs/agent-work 方案清单。",
                "inputSchema": {"type": "object", "properties": {}},
                "_meta": APP_ONLY_META,
            },
            {
                "name": "app_infer_dispatch_spec",
                "title": "App 智能推导目标、白名单与验收命令",
                "description": "前端派单表单基于方案、续轮上下文与行业功能模块完成标准自动生成完整规格。",
                "inputSchema": SpecInferenceRequest.model_json_schema(),
                "_meta": APP_ONLY_META,
            },
            {
                "name": "app_create_dispatch",
                "title": "App 发起显式派单",
                "description": "前端派单表单提交接口。",
                "inputSchema": DispatchRequest.model_json_schema(),
                "_meta": APP_ONLY_META,
            },
            {
                "name": "app_preview_dispatch",
                "title": "App 预览有效派单规格",
                "description": "按正式派单的规则计算当前表单的目标、改动范围、验收命令和风险路由。",
                "inputSchema": DispatchRequest.model_json_schema(),
                "_meta": APP_ONLY_META,
            },
            {
                "name": "app_cancel_dispatch",
                "title": "App 中止派单",
                "description": "前端面板中止正在运行的派单。",
                "inputSchema": {
                    "type": "object",
                    "required": ["run_id"],
                    "properties": {"run_id": {"type": "string"}},
                },
                "_meta": APP_ONLY_META,
            },
        ]

    def list_resources(self) -> list[dict[str, Any]]:
        """声明可供 Antigravity 宿主加载的 ui:// MCP App 资源。"""
        return [
            {
                "uri": APP_RESOURCE_URI,
                "name": "Codex Monitor",
                "description": "Antigravity × Codex 显式 Harness 与单向实时监控面板",
                "mimeType": APP_MIME_TYPE,
                "_meta": {"ui": {"prefersBorder": False}},
            }
        ]

    def read_resource(self, uri: str) -> dict[str, Any]:
        """读取指定资源内容。"""
        if uri != APP_RESOURCE_URI:
            raise ValueError(f"Unknown resource URI: {uri}")
        return {
            "contents": [
                {
                    "uri": APP_RESOURCE_URI,
                    "mimeType": APP_MIME_TYPE,
                    "text": self._load_app_html(),
                    "_meta": {"ui": {"prefersBorder": False}},
                }
            ]
        }

    def call_tool(self, name: str, arguments: dict[str, Any] | None) -> dict[str, Any]:
        """分发工具调用，统一输出 content（给模型）与 structuredContent（给前端 App）。"""
        args = arguments or {}
        try:
            if name == "open_codex_harness":
                snapshot = self._monitor_service.get_dashboard_snapshot(limit=40)
                initial_session_id = args.get("session_id")
                if not initial_session_id and snapshot.project_groups:
                    first_sessions = snapshot.project_groups[0].sessions
                    if first_sessions:
                        initial_session_id = first_sessions[0].session_id
                payload = {
                    "view": args.get("view") or "monitor",
                    "selected_session_id": initial_session_id,
                    "snapshot": snapshot.model_dump(),
                }
                summary_text = (
                    f"Codex Monitor 面板已就绪：共发现 {snapshot.total_sessions} 条会话"
                    f"（Harness 派单 {snapshot.harness_runs} 轮，运行中 {snapshot.running_dispatches} 个），"
                    f"工作区 {snapshot.workspace}。"
                )
                return self._ok(summary_text, payload)

            if name in ("codex_route_policy", "app_preview_routing"):
                req = RoutingRequest.model_validate(args)
                decision = self._routing_service.resolve(req)
                text = (
                    f"推荐角色: {decision.role.value} | 模型: {decision.model} ({decision.effort.value}) | "
                    f"沙箱: {decision.sandbox.value} | 需 Astra 复核: {'是' if decision.requires_astra_review else '否'}\n"
                    f"依据: {decision.rationale}"
                )
                return self._ok(text, decision.model_dump())

            if name in ("codex_infer_dispatch_spec", "app_infer_dispatch_spec"):
                req = SpecInferenceRequest.model_validate(args)
                dispatch_req = DispatchRequest(
                    **req.model_dump(exclude={"objective_hint"}), objective=req.objective_hint,
                )
                spec = self._dispatch_service.preview_spec(dispatch_req)
                text = (
                    f"已推导派单规格：{spec.rationale}\n"
                    f"白名单 ({len(spec.allowed_files)}): {', '.join(spec.allowed_files[:6])}\n"
                    f"验收命令 ({len(spec.verification_commands)}): {' ; '.join(spec.verification_commands[:3])}"
                )
                return self._ok(text, spec.model_dump())

            if name == "app_list_plan_docs":
                docs = self._spec_inference_service.list_plan_docs()
                return self._ok("ok", {"plans": [d.model_dump() for d in docs]})

            if name in ("codex_list_task_packets", "app_get_task_packets"):
                plan_filter = args.get("plan_filter")
                groups = self._packet_repo.list_plan_groups(
                    plan_filter=str(plan_filter) if plan_filter else None
                )
                payload = {"plans": [grp.model_dump() for grp in groups]}
                ready_count = sum(
                    1 for grp in groups for pkt in grp.packets if pkt.dispatchable
                )
                text = f"共扫描到 {len(groups)} 个任务包计划目录，其中可立即派单任务包 {ready_count} 个。"
                return self._ok(text, payload)

            if name in ("codex_dispatch", "app_create_dispatch"):
                req = DispatchRequest.model_validate(args)
                res = self._dispatch_service.dispatch(req)
                return self._ok(res.message, res.model_dump())

            if name == "app_preview_dispatch":
                req = DispatchRequest.model_validate(args)
                spec = self._dispatch_service.preview_spec(req)
                return self._ok("已计算有效派单规格。", spec.model_dump())

            if name == "codex_dispatch_status":
                run_id = args.get("run_id")
                if run_id:
                    detail = self._monitor_service.get_session_detail(str(run_id))
                    if detail is None:
                        return self._err(f"未找到派单或会话记录: {run_id}")
                    s = detail.summary
                    text = (
                        f"会话 {s.session_id} [{s.status}] | 模型: {s.model or '-'} ({s.effort or '-'}) | "
                        f"轮次: {s.turn_count} | 命令: {s.command_count} | 改动文件: {s.file_count} | "
                        f"范围审计: {s.scope_status.value}"
                    )
                    return self._ok(text, detail.model_dump())
                runs = self._dispatch_repo.list_all()[:20]
                text = f"共有 {len(runs)} 条近期 Harness 派单记录。"
                return self._ok(text, {"runs": [r.model_dump() for r in runs]})

            if name in ("codex_cancel_dispatch", "app_cancel_dispatch"):
                run_id = str(args.get("run_id") or "").strip()
                if not run_id:
                    return self._err("缺少必填参数 run_id")
                updated = self._dispatch_service.cancel(run_id)
                if updated is None:
                    return self._err(f"未找到派单记录: {run_id}")
                if updated.status in (DispatchStatus.QUEUED, DispatchStatus.RUNNING):
                    text = f"已提交派单 {run_id} 的中止请求，正在等待执行进程确认退出。"
                    cancel_requested = True
                else:
                    text = f"派单 {run_id} 当前状态: {updated.status.value}"
                    cancel_requested = False
                return self._ok(text, {**updated.model_dump(), "cancel_requested": cancel_requested})

            if name == "codex_verify_scope":
                run_id = args.get("run_id")
                if run_id:
                    rec = self._dispatch_repo.get(str(run_id))
                    if rec is None:
                        return self._err(f"未找到派单记录: {run_id}")
                    ws = Path(rec.workspace)
                    after_dirty = self._scope_service.list_git_dirty_files(ws)
                    baseline_set = set(rec.baseline_dirty_files)
                    newly_dirty = [p for p in after_dirty if p not in baseline_set]
                    observed = list(dict.fromkeys([*rec.changed_files, *newly_dirty]))
                    audit = self._scope_service.audit_scope(
                        workspace=ws,
                        allowed_files=rec.allowed_files,
                        forbidden_paths=rec.forbidden_paths,
                        observed_files=observed,
                        read_only_expected=rec.routing.sandbox == SandboxMode.READ_ONLY,
                        packet_scope=rec.packet_id is not None,
                    )
                    return self._ok(audit.summary, audit.model_dump())

                allowed = self._scope_service.validate_allowed_files(
                    self._workspace, [str(x) for x in (args.get("allowed_files") or [])]
                )
                forbidden = self._scope_service.validate_forbidden_paths(
                    self._workspace, [str(x) for x in (args.get("forbidden_paths") or [])]
                )
                observed = (
                    [str(x) for x in args["observed_files"]]
                    if args.get("observed_files") is not None
                    else self._scope_service.list_git_dirty_files(self._workspace)
                )
                audit = self._scope_service.audit_scope(
                    workspace=self._workspace,
                    allowed_files=allowed,
                    forbidden_paths=forbidden,
                    observed_files=observed,
                    read_only_expected=bool(args.get("read_only_expected", False)),
                )
                return self._ok(audit.summary, audit.model_dump())

            if name == "app_get_dashboard_snapshot":
                query = args.get("query")
                limit = int(args.get("limit") or 80)
                snapshot = self._monitor_service.get_dashboard_snapshot(
                    query=str(query) if query else None,
                    limit=max(1, min(limit, 200)),
                )
                return self._ok("ok", snapshot.model_dump())

            if name == "app_get_session_detail":
                session_id = str(args.get("session_id") or "").strip()
                if not session_id:
                    return self._err("缺少 session_id")
                detail = self._monitor_service.get_session_detail(session_id)
                if detail is None:
                    return self._err(f"未找到会话: {session_id}")
                return self._ok("ok", detail.model_dump())

            return self._err(f"未知工具名称: {name}")
        except (ValidationError, ValueError, RuntimeError, OSError) as exc:
            LOGGER.warning("Tool %s failed: %s", name, exc)
            return self._err(str(exc))

    def handle_jsonrpc_message(self, message: dict[str, Any]) -> dict[str, Any] | None:
        """处理单条 JSON-RPC 2.0 请求，通知类消息返回 None。"""
        req_id = message.get("id")
        method = str(message.get("method") or "")
        params = message.get("params") or {}

        # 通知（无 id）不回包
        if req_id is None and method.startswith("notifications/"):
            return None

        try:
            if method == "initialize":
                client_version = str(params.get("protocolVersion") or "")
                negotiated = (
                    client_version
                    if client_version in SUPPORTED_PROTOCOL_VERSIONS
                    else SUPPORTED_PROTOCOL_VERSIONS[1]
                )
                result = {
                    "protocolVersion": negotiated,
                    "capabilities": {
                        "tools": {"listChanged": False},
                        "resources": {"subscribe": False, "listChanged": False},
                    },
                    "serverInfo": {
                        "name": "codex-harness",
                        "version": __version__,
                    },
                }
            elif method == "ping":
                result = {}
            elif method == "tools/list":
                result = {"tools": self.list_tools()}
            elif method == "tools/call":
                tool_name = str(params.get("name") or "")
                tool_args = params.get("arguments")
                result = self.call_tool(tool_name, tool_args if isinstance(tool_args, dict) else {})
            elif method == "resources/list":
                result = {"resources": self.list_resources()}
            elif method == "resources/read":
                uri = str(params.get("uri") or "")
                result = self.read_resource(uri)
            elif method == "prompts/list":
                result = {"prompts": []}
            else:
                return {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "error": {"code": -32601, "message": f"Method not found: {method}"},
                }

            return {"jsonrpc": "2.0", "id": req_id, "result": result}
        except ValueError as exc:
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "error": {"code": -32602, "message": str(exc)},
            }
        except (OSError, RuntimeError) as exc:
            LOGGER.exception("Internal RPC error for %s", method)
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "error": {"code": -32603, "message": str(exc)},
            }

    def serve_stdio(self) -> None:
        """以二进制 UTF-8 换行分隔 JSON-RPC 循环服务 stdin/stdout，彻底规避 Windows CRLF 问题。"""
        stdin_buf = sys.stdin.buffer
        stdout_buf = sys.stdout.buffer
        LOGGER.info("codex-harness MCP server started (workspace=%s, data_dir=%s)", self._workspace, self._data_dir)

        while True:
            raw_line = stdin_buf.readline()
            if not raw_line:
                break
            line = raw_line.decode("utf-8", errors="replace").strip()
            if not line:
                continue
            try:
                message = json.loads(line)
            except json.JSONDecodeError as exc:
                err_resp = {
                    "jsonrpc": "2.0",
                    "id": None,
                    "error": {"code": -32700, "message": f"Parse error: {exc}"},
                }
                self._write_frame(stdout_buf, err_resp)
                continue

            if not isinstance(message, dict):
                continue
            response = self.handle_jsonrpc_message(message)
            if response is not None:
                self._write_frame(stdout_buf, response)

    @staticmethod
    def _write_frame(stdout_buf: Any, payload: dict[str, Any]) -> None:
        encoded = (json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
        stdout_buf.write(encoded)
        stdout_buf.flush()

    @staticmethod
    def _ok(text: str, structured: dict[str, Any]) -> dict[str, Any]:
        return {
            "content": [{"type": "text", "text": text}],
            "structuredContent": structured,
            "isError": False,
        }

    @staticmethod
    def _err(message: str) -> dict[str, Any]:
        return {
            "content": [{"type": "text", "text": message}],
            "structuredContent": {"error": message},
            "isError": True,
        }


def build_default_server(
    workspace: Path,
    data_dir: Path,
    app_html_path: Path | None = None,
    session_roots: list[Path] | None = None,
) -> CodexHarnessMcpServer:
    """装配默认分层依赖并构造 MCP 服务端实例。"""
    session_repo = SessionRepository(session_roots=session_roots)
    dispatch_repo = DispatchRepository(data_dir=data_dir)
    packet_repo = PacketRepository(workspace=workspace)
    routing_service = RoutingService()
    scope_service = ScopeService()
    spec_inference_service = SpecInferenceService(
        workspace=workspace,
        session_repo=session_repo,
        dispatch_repo=dispatch_repo,
        packet_repo=packet_repo,
        routing_service=routing_service,
        scope_service=scope_service,
    )
    dispatch_service = DispatchService(
        default_workspace=workspace,
        dispatch_repo=dispatch_repo,
        routing_service=routing_service,
        scope_service=scope_service,
        spec_inference_service=spec_inference_service,
        session_repo=session_repo,
    )
    monitor_service = MonitorService(
        workspace=workspace,
        session_repo=session_repo,
        dispatch_repo=dispatch_repo,
        packet_repo=packet_repo,
    )
    return CodexHarnessMcpServer(
        workspace=workspace,
        data_dir=data_dir,
        session_repo=session_repo,
        dispatch_repo=dispatch_repo,
        packet_repo=packet_repo,
        routing_service=routing_service,
        scope_service=scope_service,
        dispatch_service=dispatch_service,
        monitor_service=monitor_service,
        spec_inference_service=spec_inference_service,
        app_html_path=app_html_path,
    )


def main(argv: list[str] | None = None) -> int:
    """CLI 入口：解析参数并启动 stdio MCP 服务。"""
    logging.basicConfig(
        stream=sys.stderr,
        level=os.getenv("CODEX_HARNESS_LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    parser = argparse.ArgumentParser(description="Antigravity × Codex Harness MCP Server")
    parser.add_argument(
        "--workspace",
        default=os.getenv("CODEX_HARNESS_WORKSPACE", str(Path(__file__).resolve().parents[2])),
        help="默认目标仓库根目录",
    )
    parser.add_argument(
        "--data-dir",
        default=os.getenv("CODEX_HARNESS_DATA_DIR", str(Path.home() / ".gemini" / "config" / "plugin_data" / "codex-harness")),
        help="Harness 派单持久化数据目录",
    )
    parser.add_argument(
        "--app-html",
        default=os.getenv("CODEX_HARNESS_APP_HTML"),
        help="可选：单文件 MCP App HTML 路径",
    )
    parsed = parser.parse_args(argv)

    server = build_default_server(
        workspace=Path(parsed.workspace),
        data_dir=Path(parsed.data_dir),
        app_html_path=Path(parsed.app_html) if parsed.app_html else None,
    )
    server.serve_stdio()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
