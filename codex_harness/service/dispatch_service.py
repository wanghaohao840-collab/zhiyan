"""显式 Harness 派单执行服务：构造契约 Prompt、后台拉起 codex exec、实时解析事件并审计范围。"""

from __future__ import annotations

import json
import logging
import os
import shutil
import threading
import time
import uuid
from collections import deque
from datetime import datetime, timezone
from pathlib import Path

from codex_harness.model.entities import DispatchStatus, SandboxMode, TaskType
from codex_harness.repository.dispatch_repository import DispatchRepository
from codex_harness.repository.packet_repository import PacketRepository
from codex_harness.repository.session_repository import (
    SessionRepository,
    derive_project_name,
    parse_exec_events_file,
)
from codex_harness.schema.contracts import (
    DispatchRequest,
    DispatchResponse,
    DispatchRoundRecord,
    RoutingDecision,
    RoutingRequest,
    SpecInferenceRequest,
    SpecInferenceResult,
    TaskPacketInfo,
)
from codex_harness.service.dispatch_process import DispatchProcess
from codex_harness.service.routing_service import RoutingService
from codex_harness.service.scope_service import ScopeService
from codex_harness.service.spec_inference_service import SpecInferenceService, strip_followup_prefix

LOGGER = logging.getLogger(__name__)


def resolve_codex_executable() -> str | None:
    """按优先顺序定位本机 Codex CLI，优先使用与桌面运行时同步的新版原生 codex.exe。"""
    env_override = os.getenv("CODEX_CLI_PATH")
    if env_override and Path(env_override).exists():
        return env_override

    # NOTE: 优先使用 Codex Desktop 运行时附带的原生 codex.exe（支持最新模型元数据与缓存格式），
    # 避免旧版 npm global codex.cmd 在解析 models cache 时降级并多消耗大量 token。
    desktop_plugin_exe = Path.home() / ".codex" / "plugins" / ".plugin-appserver" / "codex.exe"
    if desktop_plugin_exe.exists():
        return str(desktop_plugin_exe)

    winapps_root = Path(r"C:\Program Files\WindowsApps")
    if winapps_root.exists():
        try:
            for folder in sorted(winapps_root.glob("OpenAI.Codex_*"), reverse=True):
                exe = folder / "app" / "resources" / "codex.exe"
                if exe.exists():
                    return str(exe)
        except OSError:
            pass

    for candidate in ("codex.exe", "codex.cmd", "codex"):
        found = shutil.which(candidate)
        if found:
            return found

    return None


def _utc_now_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


class DispatchService:
    """负责预检门禁、编译显式派单 Prompt、异步拉起 Codex 子进程与生命周期管理。"""

    def __init__(
        self,
        default_workspace: Path,
        dispatch_repo: DispatchRepository,
        routing_service: RoutingService,
        scope_service: ScopeService,
        spec_inference_service: SpecInferenceService | None = None,
        session_repo: SessionRepository | None = None,
    ) -> None:
        self._default_workspace = default_workspace.resolve()
        self._dispatch_repo = dispatch_repo
        self._routing_service = routing_service
        self._scope_service = scope_service
        self._spec_inference_service = spec_inference_service
        self._session_repo = session_repo

    def compile_dispatch_prompt(
        self,
        req: DispatchRequest,
        routing: RoutingDecision,
        workspace: Path,
        allowed_files: list[str],
        forbidden_paths: list[str],
        verification_commands: list[str],
        packet_rel_path: str | None,
    ) -> str:
        """按显式 Harness 规范构造发往 Codex 的结构化派单内容。"""
        lines: list[str] = [
            f"# {req.title}",
            "",
            f"Workspace: `{workspace}`",
            f"Harness Role: `{routing.role.value}` | Model: `{routing.model}` | Effort: `{routing.effort.value}` | Sandbox: `{routing.sandbox.value}`",
        ]
        if packet_rel_path:
            lines.append(f"Bound Task Packet: `{packet_rel_path}`")

        lines.extend(["", "## Objective", req.objective.strip()])

        if routing.sandbox == SandboxMode.READ_ONLY:
            lines.extend(
                [
                    "",
                    "## STRICT SCOPE — READ-ONLY REVIEW",
                    "Do NOT modify any repository files. Report concrete findings with `path:line`, trigger sequence, impact, and verification steps.",
                ]
            )
        elif allowed_files:
            lines.extend(["", f"## STRICT SCOPE — ONLY create/edit files inside `{workspace}`:"])
            for path in allowed_files:
                lines.append(f"- `{path}`")
            lines.append("If achieving the goal requires edits outside this allowlist, STOP and report a Reality-Conflict blocker instead of expanding scope.")

        if forbidden_paths:
            lines.extend(["", "## FORBIDDEN PATHS / BOUNDARIES"])
            for path in forbidden_paths:
                lines.append(f"- `{path}`")

        if req.acceptance_criteria:
            lines.extend(["", "## Acceptance Criteria"])
            for item in req.acceptance_criteria:
                lines.append(f"- {item}")

        if verification_commands:
            lines.extend(["", "## Verification Commands"])
            for cmd in verification_commands:
                lines.append(f"- `{cmd}`")

        if routing.required_gates:
            lines.extend(["", "## Harness Gates (AGENTS.md)"])
            for gate in routing.required_gates:
                lines.append(f"- {gate}")

        lines.extend(
            [
                "",
                "## Handoff Output",
                "Conclude with: changed files, exact verification commands and pass/fail summary, deviations, and residual risks.",
            ]
        )
        return "\n".join(lines).strip() + "\n"

    def _build_codex_command(
        self,
        codex_bin: str,
        workspace: Path,
        routing: RoutingDecision,
        resume_thread_id: str | None,
    ) -> list[str]:
        """构造非交互 codex exec（或 resume）命令行参数，Prompt 通过 stdin（`-`）传入。"""
        # NOTE: 显式关闭 web_search（规避 ~/.codex/config.toml 中 web_search="indexed" 在
        # openai-http 接口上序列化不支持字段 tools[N].index_gated_web_access 的 400 报错），
        # 并禁用子进程递归拉起本插件自身 MCP 服务；在 Windows 无窗口后台子进程下固定使用
        # windows.sandbox="unelevated"，避免本机配置为 elevated 时 codex-windows-sandbox-setup.exe
        # 因无法交互提权报错 orchestrator_helper_exit_nonzero (exit -1)。
        common_overrides = [
            "-c",
            f'model_reasoning_effort="{routing.effort.value}"',
            "-c",
            'web_search="disabled"',
            "-c",
            "mcp_servers.codex-harness.enabled=false",
        ]
        if os.name == "nt":
            common_overrides.extend(["-c", 'windows.sandbox="unelevated"'])
        if resume_thread_id:
            return [
                codex_bin,
                "exec",
                "resume",
                resume_thread_id,
                "--json",
                "-m",
                routing.model,
                "-c",
                f'sandbox_mode="{routing.sandbox.value}"',
                *common_overrides,
                "-",
            ]
        return [
            codex_bin,
            "exec",
            "--json",
            "-C",
            str(workspace),
            "-m",
            routing.model,
            "-s",
            routing.sandbox.value,
            *common_overrides,
            "-",
        ]

    @staticmethod
    def _build_child_env(workspace: Path) -> dict[str, str]:
        """构造子进程环境变量，强制将 CODEX_HOME 隔离到 ~/.codex 避免在目标仓库内解压 skills/.system。"""
        env = dict(os.environ)
        default_codex_home = (Path.home() / ".codex").resolve()
        ws_resolved = workspace.resolve()
        raw_home = env.get("CODEX_HOME")
        if not raw_home:
            env["CODEX_HOME"] = str(default_codex_home)
        else:
            try:
                Path(raw_home).resolve().relative_to(ws_resolved)
                # CODEX_HOME 落在目标仓库内部会导致 Codex 自动向仓库写入 skills/.system/ 与 sqlite
                env["CODEX_HOME"] = str(default_codex_home)
            except (OSError, ValueError):
                pass
        return env

    def _dispatch_context(self, req: DispatchRequest) -> tuple[Path, DispatchRoundRecord | None]:
        previous = self._dispatch_repo.get(req.resume_run_id) if req.resume_run_id else None
        if req.resume_run_id and previous is None:
            raise ValueError(f"Previous run not found: {req.resume_run_id!r}")
        if req.resume_codex_thread_id:
            if previous is not None and previous.codex_thread_id != req.resume_codex_thread_id:
                raise ValueError("The requested Codex thread does not match the previous run")
            if previous is None:
                # Desktop callers can resume by thread alone. Use the latest round
                # of that thread so its packet and workspace remain in effect.
                previous = next(
                    (run for run in self._dispatch_repo.list_all()
                     if run.codex_thread_id == req.resume_codex_thread_id),
                    None,
                )
        workspace_path = req.workspace or (previous.workspace if previous else None)
        workspace = Path(workspace_path).resolve() if workspace_path else self._default_workspace
        if not workspace.exists() or not workspace.is_dir():
            raise ValueError(f"Workspace does not exist: {workspace}")
        return workspace, previous

    def _resolve_spec(
        self, req: DispatchRequest, workspace: Path, previous: DispatchRoundRecord | None,
    ) -> tuple[SpecInferenceResult, TaskPacketInfo | None, list[str]]:
        preflight_warnings: list[str] = []
        requested_files = req.allowed_files
        forbidden_raw = list(req.forbidden_paths)
        acceptance_list = list(req.acceptance_criteria)
        verify_cmds = list(req.verification_commands)
        objective_text = req.objective.strip()
        clean_title = strip_followup_prefix(req.title)
        packet = None
        packet_path = req.packet_path
        if previous is not None and previous.packet_id is not None:
            if workspace != Path(previous.workspace).resolve() or not previous.packet_path:
                raise ValueError("A task packet follow-up must use its original workspace and task packet")
            packet_path = packet_path or previous.packet_path
            if requested_files is None:
                requested_files = previous.allowed_files

        if packet_path:
            packet_repo = PacketRepository(workspace)
            packet = packet_repo.find_packet(packet_path)
            if packet is None:
                raise ValueError(f"Task packet not found: {packet_path!r}")
            if previous is not None and previous.packet_id is not None and (
                packet.packet_id != previous.packet_id or packet.relative_path != previous.packet_path
            ):
                raise ValueError("A task packet follow-up cannot change its bound task packet")
            if packet.unmet_dependencies:
                raise ValueError(
                    f"Task packet {packet.packet_id} has unmet dependencies: {', '.join(packet.unmet_dependencies)}"
                )
            if packet.status == "draft":
                preflight_warnings.append(f"任务包 {packet.packet_id} 当前状态为 draft，请确认已完成 REVIEW.md 就绪门禁。")
            if not verify_cmds and packet.verification_commands:
                verify_cmds = list(packet.verification_commands)

        # 风险路由独立于规格补全，即使目标与验收已填写，也应推导未指定的风险。
        inferred = None
        if req.auto_infer and self._spec_inference_service is not None:
            inferred = self._spec_inference_service.infer(
                SpecInferenceRequest(
                    title=clean_title,
                    objective_hint=objective_text,
                    workspace=str(workspace),
                    task_type=req.task_type,
                    plan_doc_path=req.plan_doc_path,
                    packet_path=packet.relative_path if packet is not None else None,
                    resume_run_id=req.resume_run_id,
                    resume_codex_thread_id=req.resume_codex_thread_id,
                    involves_ownership_check=req.involves_ownership_check,
                    involves_concurrency_or_lease=req.involves_concurrency_or_lease,
                    involves_cross_store_or_rollback=req.involves_cross_store_or_rollback,
                    is_complex=req.is_complex,
                    model_override=req.model_override,
                    effort_override=req.effort_override,
                    sandbox_override=req.sandbox_override,
                    allowed_files=requested_files,
                ),
                packet=packet,
            )
            if not objective_text:
                objective_text = inferred.objective
            if not acceptance_list:
                acceptance_list = list(inferred.acceptance_criteria)
            if not forbidden_raw:
                forbidden_raw = list(inferred.forbidden_paths)
            if not verify_cmds:
                verify_cmds = list(inferred.verification_commands)
            preflight_warnings.append(f"已按方案与行业功能模块标准推导派单规格：{inferred.rationale}")

        if packet is not None:
            allowed_files = (
                inferred.allowed_files
                if inferred is not None
                else self._scope_service.constrain_packet_allowed_files(workspace, packet.allowed_files, requested_files)
            )
        else:
            if requested_files is None:
                requested_files = inferred.allowed_files if inferred is not None else []
            allowed_files = self._scope_service.validate_allowed_files(workspace, requested_files)
        forbidden_paths = self._scope_service.validate_forbidden_paths(workspace, forbidden_raw)
        empty_packet_scope = packet is not None and not allowed_files
        if empty_packet_scope:
            preflight_warnings.append("任务包的有效改动范围为空，本轮使用只读沙箱。")

        routing = inferred.routing_preview if inferred is not None else self._routing_service.resolve(
            RoutingRequest(
                task_type=req.task_type or TaskType.IMPLEMENT_GENERAL,
                involves_ownership_check=bool(req.involves_ownership_check),
                involves_concurrency_or_lease=bool(req.involves_concurrency_or_lease),
                involves_cross_store_or_rollback=bool(req.involves_cross_store_or_rollback),
                is_complex=bool(req.is_complex),
                model_override=req.model_override,
                effort_override=req.effort_override,
                sandbox_override=SandboxMode.READ_ONLY if empty_packet_scope else req.sandbox_override,
            )
        )
        if routing.fallback_note:
            preflight_warnings.append(routing.fallback_note)
        if routing.requires_astra_review and routing.task_type != TaskType.REVIEW_CONCURRENCY:
            preflight_warnings.append("本任务命中高风险一致性/并发条件，实施完成后需触发 GPT-6.2 Astra High 独立审查。")

        if not clean_title:
            if inferred is not None:
                clean_title = inferred.suggested_title
            elif packet is not None:
                clean_title = packet.title
            elif previous is not None:
                clean_title = previous.title
            else:
                clean_title = req.plan_doc_path or "按方案执行工程实现与回归验收"

        spec = SpecInferenceResult(
            suggested_title=clean_title,
            suggested_task_type=routing.task_type,
            suggested_involves_ownership_check=inferred.suggested_involves_ownership_check if inferred else bool(req.involves_ownership_check),
            suggested_involves_concurrency_or_lease=inferred.suggested_involves_concurrency_or_lease if inferred else bool(req.involves_concurrency_or_lease),
            suggested_involves_cross_store_or_rollback=inferred.suggested_involves_cross_store_or_rollback if inferred else bool(req.involves_cross_store_or_rollback),
            suggested_is_complex=inferred.suggested_is_complex if inferred else bool(req.is_complex),
            objective=objective_text,
            acceptance_criteria=acceptance_list,
            allowed_files=allowed_files,
            forbidden_paths=forbidden_paths,
            verification_commands=verify_cmds,
            matched_domains=inferred.matched_domains if inferred else [],
            matched_plan_docs=inferred.matched_plan_docs if inferred else [],
            industry_standards_applied=inferred.industry_standards_applied if inferred else [],
            rationale=inferred.rationale if inferred else "",
            routing_preview=routing,
        )
        return spec, packet, preflight_warnings

    def preview_spec(self, req: DispatchRequest) -> SpecInferenceResult:
        """Resolve the same effective specification used by dry runs and execution."""
        workspace, previous = self._dispatch_context(req)
        spec, _, _ = self._resolve_spec(req, workspace, previous)
        return spec

    def dispatch(self, req: DispatchRequest) -> DispatchResponse:
        """执行前置门禁校验、持久化派单记录，并在非 dry_run 模式下后台拉起 Codex。"""
        if not any((strip_followup_prefix(req.title), req.plan_doc_path, req.packet_path, req.resume_run_id, req.resume_codex_thread_id)):
            raise ValueError("请至少填写派单标题或选择一份仓库方案文档。")
        workspace, previous = self._dispatch_context(req)
        spec, packet, preflight_warnings = self._resolve_spec(req, workspace, previous)
        if not spec.objective:
            raise ValueError("缺少派单目标（objective），请填写目标描述或启用 auto_infer 自动推导。")

        effective_req = req.model_copy(update={
            "title": spec.suggested_title,
            "objective": spec.objective,
            "acceptance_criteria": spec.acceptance_criteria,
        })
        routing = spec.routing_preview
        session_id = f"harness-{uuid.uuid4().hex[:12]}"
        round_number = 1
        resume_thread_id = req.resume_codex_thread_id
        inherited_parent_thread_id: str | None = None
        if previous is not None:
            session_id = previous.session_id
            existing_rounds = self._dispatch_repo.list_by_session(session_id)
            round_number = (existing_rounds[-1].round_number + 1) if existing_rounds else (previous.round_number + 1)
            inherited_parent_thread_id = previous.parent_thread_id or next(
                (r.parent_thread_id for r in reversed(existing_rounds) if r.parent_thread_id),
                None,
            )
            if not resume_thread_id:
                resume_thread_id = previous.codex_thread_id

        packet_path = packet.relative_path if packet is not None else req.plan_doc_path
        compiled_prompt = self.compile_dispatch_prompt(
            req=effective_req,
            routing=routing,
            workspace=workspace,
            allowed_files=spec.allowed_files,
            forbidden_paths=spec.forbidden_paths,
            verification_commands=spec.verification_commands,
            packet_rel_path=packet_path,
        )

        codex_bin = resolve_codex_executable()
        cmd_preview = self._build_codex_command(
            codex_bin=codex_bin or "codex",
            workspace=workspace,
            routing=routing,
            resume_thread_id=resume_thread_id,
        )

        run_id = f"run-{datetime.now(tz=timezone.utc).strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}"
        baseline_dirty = self._scope_service.list_git_dirty_files(workspace)

        if req.dry_run:
            return DispatchResponse(
                run_id=run_id,
                session_id=session_id,
                status=DispatchStatus.DRY_RUN,
                routing=routing,
                preflight_warnings=preflight_warnings,
                compiled_prompt=compiled_prompt,
                codex_command_preview=cmd_preview,
                message="干跑预检通过：未实际启动 Codex 进程，请核对路由与派单契约后下发。",
            )

        if not codex_bin:
            raise RuntimeError("未在本机找到 Codex CLI（codex / codex.cmd / codex.exe），请先安装并完成 codex login。")

        events_file = self._dispatch_repo.events_path(run_id)
        record = DispatchRoundRecord(
            run_id=run_id,
            session_id=session_id,
            round_number=round_number,
            title=spec.suggested_title,
            workspace=str(workspace),
            project_name=derive_project_name(str(workspace)),
            source_agent="Antigravity",
            source_model=req.source_model,
            routing=routing,
            packet_id=packet.packet_id if packet is not None else None,
            packet_path=packet_path,
            allowed_files=spec.allowed_files,
            forbidden_paths=spec.forbidden_paths,
            acceptance_criteria=spec.acceptance_criteria,
            verification_commands=spec.verification_commands,
            compiled_prompt=compiled_prompt,
            status=DispatchStatus.RUNNING,
            codex_thread_id=resume_thread_id,
            parent_thread_id=inherited_parent_thread_id,
            started_at=_utc_now_iso(),
            baseline_dirty_files=baseline_dirty,
            events_file=str(events_file),
        )
        self._dispatch_repo.save(record)

        worker = threading.Thread(
            target=self._run_codex_process,
            args=(record, codex_bin, cmd_preview, workspace, resume_thread_id),
            name=f"codex-dispatch-{run_id}",
            daemon=True,
        )
        worker.start()

        return DispatchResponse(
            run_id=run_id,
            session_id=session_id,
            status=DispatchStatus.RUNNING,
            routing=routing,
            preflight_warnings=preflight_warnings,
            compiled_prompt=compiled_prompt,
            codex_command_preview=cmd_preview,
            message=f"已向 Codex ({routing.model} · {routing.effort.value}) 派发第 {round_number} 轮任务，可在右侧 Codex Harness 面板实时监控。",
        )

    @staticmethod
    def _is_thread_writer_conflict(text: str) -> bool:
        """检测 Codex 线程是否因被 Codex Desktop 打开而触发单写入者租约冲突。"""
        lower = text.lower()
        return "already has an active writer" in lower or "thread-store conflict" in lower

    def _build_inherited_context_block(self, thread_id: str) -> str:
        """当原线程被 Codex Desktop 占用时，从只读会话文件提炼上下文交接摘要，确保新线程无缝接续。"""
        lines: list[str] = [
            f"## Inherited Context (forked because thread `{thread_id}` is locked by an active Codex Desktop writer)",
        ]
        if self._session_repo is None:
            lines.append("Continue the objective below independently in this fresh thread without relying on in-memory history.")
            return "\n".join(lines) + "\n\n"

        detail = self._session_repo.get_session_detail(thread_id)
        if detail is None:
            lines.append("Continue the objective below independently in this fresh thread.")
            return "\n".join(lines) + "\n\n"

        lines.append(f"- Original conversation title: {detail.summary.title}")
        recent_files: list[str] = []
        recent_notes: list[str] = []
        for turn in detail.turns[-3:]:
            for fpath in turn.changed_files:
                if fpath not in recent_files:
                    recent_files.append(fpath)
            if turn.final_answer:
                recent_notes.append(f"Turn {turn.turn_index} reply: {turn.final_answer[:450]}")
            else:
                narrs = [it.text for it in turn.timeline if it.kind == "narration" and it.text]
                if narrs:
                    recent_notes.append(f"Turn {turn.turn_index} latest update: {narrs[-1][:450]}")
        if recent_files:
            lines.append(f"- Recently touched files: {', '.join(recent_files[:10])}")
        for note in recent_notes[-3:]:
            lines.append(f"- {note}")
        return "\n".join(lines) + "\n\n"

    def _execute_single_attempt(
        self,
        record: DispatchRoundRecord,
        cmd: list[str],
        prompt_text: str,
        workspace: Path,
        events_path: Path,
        start_mono: float,
    ) -> tuple[int | None, list[str], bool]:
        """执行单次 codex exec 子进程调用并流式落盘 stdout 事件。"""
        if self._dispatch_repo.cancellation_requested(record.run_id):
            return None, [], True

        # 使用滑动尾部窗口保留最新 stderr，防止早期诊断日志淹没末尾核心错误（如 thread-store conflict）
        stderr_tail: deque[str] = deque(maxlen=120)
        process_tree = DispatchProcess(cmd, workspace, self._build_child_env(workspace))
        proc = process_tree.process
        record.pid = proc.pid
        stdout_received = threading.Event()
        stdout_error: list[OSError] = []
        output_lock = threading.Lock()
        accept_output = True

        def write_prompt() -> None:
            if proc.stdin is not None:
                try:
                    with proc.stdin:
                        proc.stdin.write(prompt_text)
                except OSError as exc:
                    LOGGER.warning("Failed writing prompt to stdin for %s: %s", record.run_id, exc)

        def drain_stdout() -> None:
            if proc.stdout is not None:
                try:
                    with proc.stdout:
                        for line in proc.stdout:
                            with output_lock:
                                if not accept_output:
                                    return
                                self._dispatch_repo.append_event_line(record.run_id, line)
                            stdout_received.set()
                except OSError as exc:
                    stdout_error.append(exc)

        def drain_stderr() -> None:
            if proc.stderr is not None:
                with proc.stderr:
                    for line in proc.stderr:
                        clean = line.rstrip("\r\n")
                        if clean:
                            with output_lock:
                                if not accept_output:
                                    return
                                stderr_tail.append(clean)

        io_threads = [
            threading.Thread(target=target, daemon=True)
            for target in (drain_stdout, drain_stderr, write_prompt)
        ]
        started_threads: list[threading.Thread] = []
        tree_exited = False
        cancelled = False
        try:
            self._dispatch_repo.save(record)
            for thread in io_threads:
                thread.start()
                started_threads.append(thread)

            # Wait independently of pipe I/O so quiet or busy children can both be cancelled.
            last_sync = 0.0
            while process_tree.is_active():
                if self._dispatch_repo.cancellation_requested(record.run_id):
                    cancelled = process_tree.stop()
                    break
                if stdout_error:
                    raise stdout_error[0]
                now_mono = time.monotonic()
                if stdout_received.is_set() and now_mono - last_sync >= 0.8:
                    stdout_received.clear()
                    self._sync_progress_from_events(record, events_path, start_mono)
                    last_sync = now_mono
                time.sleep(0.2)
            tree_exited = True
        finally:
            try:
                if not tree_exited:
                    process_tree.stop()
                process_tree.wait()
            finally:
                process_tree.close()
                if not tree_exited:
                    with output_lock:
                        accept_output = False
                for thread in started_threads:
                    thread.join(timeout=None if tree_exited else 1.0)
                streams = (proc.stdout, proc.stderr, proc.stdin)
                for stream in streams[len(started_threads):]:
                    if stream is not None:
                        stream.close()

        if stdout_error and not cancelled:
            raise stdout_error[0]
        return proc.returncode, list(stderr_tail), cancelled

    def _run_codex_process(
        self,
        record: DispatchRoundRecord,
        codex_bin: str,
        cmd: list[str],
        workspace: Path,
        resume_thread_id: str | None = None,
    ) -> None:
        """后台运行 codex exec 子进程；若 resume 遇桌面端写锁冲突则自动继承上下文新开线程重试一次。"""
        start_mono = time.monotonic()
        events_path = self._dispatch_repo.events_path(record.run_id)

        try:
            exit_code, stderr_lines, was_cancelled = self._execute_single_attempt(
                record=record,
                cmd=cmd,
                prompt_text=record.compiled_prompt,
                workspace=workspace,
                events_path=events_path,
                start_mono=start_mono,
            )
            self._sync_progress_from_events(record, events_path, start_mono)

            combined_err = "\n".join([*stderr_lines, record.error_message or ""])
            if (
                not was_cancelled
                and exit_code != 0
                and resume_thread_id
                and self._is_thread_writer_conflict(combined_err)
            ):
                if self._dispatch_repo.cancellation_requested(record.run_id):
                    was_cancelled = True
                else:
                    exit_code, stderr_lines, was_cancelled = self._retry_locked_thread(
                        record, codex_bin, workspace, resume_thread_id, events_path, start_mono
                    )
                    self._sync_progress_from_events(record, events_path, start_mono)

            record.exit_code = exit_code
            self._finish_run(record, workspace, start_mono, was_cancelled, stderr_lines)
        except OSError as exc:
            LOGGER.error("Codex dispatch %s failed: %s", record.run_id, exc)
            record.status = DispatchStatus.FAILED
            record.completed_at = _utc_now_iso()
            record.duration_ms = int((time.monotonic() - start_mono) * 1000)
            record.error_message = str(exc)
            self._dispatch_repo.save(record)

    def _retry_locked_thread(
        self,
        record: DispatchRoundRecord,
        codex_bin: str,
        workspace: Path,
        resume_thread_id: str,
        events_path: Path,
        start_mono: float,
    ) -> tuple[int | None, list[str], bool]:
        LOGGER.info(
            "Thread %s locked by active writer; forking fresh codex exec with inherited context for %s",
            resume_thread_id,
            record.run_id,
        )
        # 清空被锁拒绝的旧事件与错误态，保留父线程归属并注入上下文交接摘要
        try:
            if events_path.exists():
                events_path.unlink()
        except OSError:
            pass
        record.parent_thread_id = resume_thread_id
        record.fallback_from_locked_thread = resume_thread_id
        record.codex_thread_id = None
        record.error_message = None
        record.command_count = 0
        record.changed_files = []
        record.file_count = 0

        notice_event = json.dumps(
            {
                "type": "item.completed",
                "item": {
                    "id": "harness-fork-note",
                    "type": "reasoning",
                    "text": (
                        f"原 Codex 线程 {resume_thread_id[:12]} 正在被 Codex Desktop 占用写入锁，"
                        "已自动继承该会话上下文并新开独立线程继续执行。"
                    ),
                },
            },
            ensure_ascii=False,
        )
        self._dispatch_repo.append_event_line(record.run_id, notice_event)
        self._dispatch_repo.save(record)

        inherited_block = self._build_inherited_context_block(resume_thread_id).strip()
        # 将上下文交接块插入 Handoff 段之前（而非 Prompt 首行），避免污染会话首行标题
        handoff_marker = "\n## Handoff Output"
        if handoff_marker in record.compiled_prompt:
            head, tail = record.compiled_prompt.split(handoff_marker, 1)
            inherited_prompt = f"{head.rstrip()}\n\n{inherited_block}\n{handoff_marker}{tail}"
        else:
            inherited_prompt = f"{record.compiled_prompt.rstrip()}\n\n{inherited_block}\n"
        fresh_cmd = self._build_codex_command(
            codex_bin=codex_bin,
            workspace=workspace,
            routing=record.routing,
            resume_thread_id=None,
        )
        return self._execute_single_attempt(
            record=record,
            cmd=fresh_cmd,
            prompt_text=inherited_prompt,
            workspace=workspace,
            events_path=events_path,
            start_mono=start_mono,
        )

    def _finish_run(
        self,
        record: DispatchRoundRecord,
        workspace: Path,
        start_mono: float,
        was_cancelled: bool,
        stderr_lines: list[str],
    ) -> None:
        record.completed_at = _utc_now_iso()
        record.duration_ms = int((time.monotonic() - start_mono) * 1000)

        if was_cancelled:
            record.status = DispatchStatus.CANCELLED
            record.error_message = record.error_message or "已由用户或协调器主动中止。"
        elif record.exit_code == 0 and not record.error_message:
            record.status = DispatchStatus.COMPLETED
        else:
            record.status = DispatchStatus.FAILED
            if not record.error_message and stderr_lines:
                record.error_message = "\n".join(stderr_lines[-8:])

        # 合并事件流中的改动文件与派单前后新增的 Git 脏文件，执行范围门禁审计
        after_dirty = self._scope_service.list_git_dirty_files(workspace)
        baseline_set = set(record.baseline_dirty_files)
        newly_dirty = [path for path in after_dirty if path not in baseline_set]
        observed_files = list(dict.fromkeys([*record.changed_files, *newly_dirty]))
        record.changed_files = observed_files
        record.file_count = len(observed_files)
        record.scope_audit = self._scope_service.audit_scope(
            workspace=workspace,
            allowed_files=record.allowed_files,
            forbidden_paths=record.forbidden_paths,
            observed_files=observed_files,
            read_only_expected=record.routing.sandbox == SandboxMode.READ_ONLY,
            packet_scope=record.packet_id is not None,
        )
        self._dispatch_repo.save(record)

    def _sync_progress_from_events(
        self,
        record: DispatchRoundRecord,
        events_path: Path,
        start_mono: float,
    ) -> None:
        """从已落盘的事件流同步实时计数与 Codex thread_id。"""
        parsed = parse_exec_events_file(events_path)
        record.command_count = parsed["command_count"]
        record.changed_files = parsed["changed_files"]
        record.file_count = parsed["file_count"]
        record.duration_ms = int((time.monotonic() - start_mono) * 1000)
        if parsed["codex_thread_id"]:
            record.codex_thread_id = parsed["codex_thread_id"]
        if parsed["final_answer"]:
            record.final_answer = parsed["final_answer"]
        if parsed["error_message"]:
            record.error_message = parsed["error_message"]
        self._dispatch_repo.save(record)

    def cancel(self, run_id: str) -> DispatchRoundRecord | None:
        """提交取消请求，由持有子进程句柄的派单线程确认退出。"""
        return self._dispatch_repo.request_cancel(run_id)
