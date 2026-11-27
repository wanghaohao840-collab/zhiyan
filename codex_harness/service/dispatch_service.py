"""显式 Harness 派单执行服务：构造契约 Prompt、后台拉起 codex exec、实时解析事件并审计范围。"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from codex_harness.model.entities import DispatchStatus, SandboxMode, TaskType
from codex_harness.repository.dispatch_repository import DispatchRepository
from codex_harness.repository.packet_repository import PacketRepository
from codex_harness.repository.session_repository import derive_project_name, parse_exec_events_file
from codex_harness.schema.contracts import (
    DispatchRequest,
    DispatchResponse,
    DispatchRoundRecord,
    RoutingDecision,
    RoutingRequest,
)
from codex_harness.service.routing_service import RoutingService
from codex_harness.service.scope_service import ScopeService

LOGGER = logging.getLogger(__name__)


def resolve_codex_executable() -> str | None:
    """按 Windows 优先顺序定位本机 Codex CLI 可执行文件。"""
    env_override = os.getenv("CODEX_CLI_PATH")
    if env_override and Path(env_override).exists():
        return env_override

    for candidate in ("codex.cmd", "codex.exe", "codex"):
        found = shutil.which(candidate)
        if found:
            return found

    # 兜底检查 Microsoft Store WindowsApps 安装路径
    winapps_root = Path(r"C:\Program Files\WindowsApps")
    if winapps_root.exists():
        try:
            for folder in sorted(winapps_root.glob("OpenAI.Codex_*"), reverse=True):
                exe = folder / "app" / "resources" / "codex.exe"
                if exe.exists():
                    return str(exe)
        except OSError:
            pass
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
    ) -> None:
        self._default_workspace = default_workspace.resolve()
        self._dispatch_repo = dispatch_repo
        self._routing_service = routing_service
        self._scope_service = scope_service
        self._lock = threading.RLock()
        self._active_procs: dict[str, subprocess.Popen[str]] = {}
        self._cancelled_runs: set[str] = set()

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
                f'model_reasoning_effort="{routing.effort.value}"',
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
            "-c",
            f'model_reasoning_effort="{routing.effort.value}"',
            "-s",
            routing.sandbox.value,
            "-",
        ]

    def dispatch(self, req: DispatchRequest) -> DispatchResponse:
        """执行前置门禁校验、持久化派单记录，并在非 dry_run 模式下后台拉起 Codex。"""
        workspace = Path(req.workspace).resolve() if req.workspace else self._default_workspace
        if not workspace.exists() or not workspace.is_dir():
            raise ValueError(f"Workspace does not exist: {workspace}")

        preflight_warnings: list[str] = []
        allowed_raw = list(req.allowed_files)
        verify_cmds = list(req.verification_commands)
        packet_id: str | None = None
        packet_rel_path: str | None = None

        # 1. 若绑定了任务包，执行依赖就绪门禁并补全白名单与验收命令
        if req.packet_path:
            packet_repo = PacketRepository(workspace)
            packet = packet_repo.find_packet(req.packet_path)
            if packet is None:
                raise ValueError(f"Task packet not found: {req.packet_path!r}")
            packet_id = packet.packet_id
            packet_rel_path = packet.relative_path
            if packet.unmet_dependencies:
                raise ValueError(
                    f"Task packet {packet.packet_id} has unmet dependencies: {', '.join(packet.unmet_dependencies)}"
                )
            if packet.status == "draft":
                preflight_warnings.append(f"任务包 {packet.packet_id} 当前状态为 draft，请确认已完成 REVIEW.md 就绪门禁。")
            if not allowed_raw and packet.allowed_files:
                allowed_raw = list(packet.allowed_files)
            if not verify_cmds and packet.verification_commands:
                verify_cmds = list(packet.verification_commands)

        # 2. 路径边界合法性校验
        allowed_files = self._scope_service.validate_allowed_files(workspace, allowed_raw)
        forbidden_paths = self._scope_service.validate_forbidden_paths(workspace, req.forbidden_paths)

        # 3. 模型与风险路由
        routing = self._routing_service.resolve(
            RoutingRequest(
                task_type=req.task_type,
                involves_ownership_check=req.involves_ownership_check,
                involves_concurrency_or_lease=req.involves_concurrency_or_lease,
                involves_cross_store_or_rollback=req.involves_cross_store_or_rollback,
                is_complex=req.is_complex,
                model_override=req.model_override,
                effort_override=req.effort_override,
                sandbox_override=req.sandbox_override,
            )
        )
        if routing.fallback_note:
            preflight_warnings.append(routing.fallback_note)
        if routing.requires_astra_review and req.task_type != TaskType.REVIEW_CONCURRENCY:
            preflight_warnings.append("本任务命中高风险一致性/并发条件，实施完成后需触发 GPT-6.2 Astra High 独立审查。")

        # 4. 计算会话 ID 与轮次
        session_id = f"harness-{uuid.uuid4().hex[:12]}"
        round_number = 1
        resume_thread_id = req.resume_codex_thread_id
        if req.resume_run_id:
            prev = self._dispatch_repo.get(req.resume_run_id)
            if prev is None:
                raise ValueError(f"Previous run not found: {req.resume_run_id!r}")
            session_id = prev.session_id
            existing_rounds = self._dispatch_repo.list_by_session(session_id)
            round_number = (existing_rounds[-1].round_number + 1) if existing_rounds else (prev.round_number + 1)
            if not resume_thread_id:
                resume_thread_id = prev.codex_thread_id

        compiled_prompt = self.compile_dispatch_prompt(
            req=req,
            routing=routing,
            workspace=workspace,
            allowed_files=allowed_files,
            forbidden_paths=forbidden_paths,
            verification_commands=verify_cmds,
            packet_rel_path=packet_rel_path,
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
            title=req.title.strip(),
            workspace=str(workspace),
            project_name=derive_project_name(str(workspace)),
            source_agent="Antigravity",
            source_model=req.source_model,
            routing=routing,
            packet_id=packet_id,
            packet_path=packet_rel_path,
            allowed_files=allowed_files,
            forbidden_paths=forbidden_paths,
            acceptance_criteria=req.acceptance_criteria,
            verification_commands=verify_cmds,
            compiled_prompt=compiled_prompt,
            status=DispatchStatus.RUNNING,
            codex_thread_id=resume_thread_id,
            started_at=_utc_now_iso(),
            baseline_dirty_files=baseline_dirty,
            events_file=str(events_file),
        )
        self._dispatch_repo.save(record)

        worker = threading.Thread(
            target=self._run_codex_process,
            args=(record, cmd_preview, workspace),
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

    def _run_codex_process(
        self,
        record: DispatchRoundRecord,
        cmd: list[str],
        workspace: Path,
    ) -> None:
        """后台运行 codex exec 子进程，持续落盘事件并刷新统计与范围审计。"""
        start_mono = time.monotonic()
        events_path = self._dispatch_repo.events_path(record.run_id)
        stderr_lines: list[str] = []

        try:
            creation_flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
            proc = subprocess.Popen(
                cmd,
                cwd=str(workspace),
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                creationflags=creation_flags,
            )
            with self._lock:
                self._active_procs[record.run_id] = proc
            record.pid = proc.pid
            self._dispatch_repo.save(record)

            if proc.stdin is not None:
                try:
                    proc.stdin.write(record.compiled_prompt)
                    proc.stdin.close()
                except OSError as exc:
                    LOGGER.warning("Failed writing prompt to stdin for %s: %s", record.run_id, exc)

            def drain_stderr() -> None:
                if proc.stderr is None:
                    return
                for line in proc.stderr:
                    clean = line.rstrip("\r\n")
                    if clean and len(stderr_lines) < 60:
                        stderr_lines.append(clean)

            stderr_thread = threading.Thread(target=drain_stderr, daemon=True)
            stderr_thread.start()

            last_sync = 0.0
            if proc.stdout is not None:
                for raw_line in proc.stdout:
                    self._dispatch_repo.append_event_line(record.run_id, raw_line)
                    now_mono = time.monotonic()
                    if now_mono - last_sync >= 0.8:
                        self._sync_progress_from_events(record, events_path, start_mono)
                        last_sync = now_mono

            exit_code = proc.wait()
            stderr_thread.join(timeout=3.0)

            self._sync_progress_from_events(record, events_path, start_mono)
            record.exit_code = exit_code
            record.completed_at = _utc_now_iso()
            record.duration_ms = int((time.monotonic() - start_mono) * 1000)

            with self._lock:
                was_cancelled = record.run_id in self._cancelled_runs
                self._active_procs.pop(record.run_id, None)
                self._cancelled_runs.discard(record.run_id)

            if was_cancelled:
                record.status = DispatchStatus.CANCELLED
                record.error_message = record.error_message or "已由用户或协调器主动中止。"
            elif exit_code == 0 and not record.error_message:
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
            )
            self._dispatch_repo.save(record)
        except OSError as exc:
            LOGGER.error("Codex dispatch %s failed to spawn: %s", record.run_id, exc)
            record.status = DispatchStatus.FAILED
            record.completed_at = _utc_now_iso()
            record.duration_ms = int((time.monotonic() - start_mono) * 1000)
            record.error_message = str(exc)
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
        """安全中止仍在运行中的派单子进程。"""
        record = self._dispatch_repo.get(run_id)
        if record is None:
            return None
        with self._lock:
            proc = self._active_procs.get(run_id)
            if proc is not None and proc.poll() is None:
                self._cancelled_runs.add(run_id)
                try:
                    proc.terminate()
                    proc.wait(timeout=4.0)
                except subprocess.TimeoutExpired:
                    proc.kill()
                except OSError as exc:
                    LOGGER.warning("Error terminating process for %s: %s", run_id, exc)
            elif record.status == DispatchStatus.RUNNING:
                record.status = DispatchStatus.CANCELLED
                record.completed_at = _utc_now_iso()
                record.error_message = "进程未在当前服务实例中运行，已标记为中止。"
                self._dispatch_repo.save(record)
        return self._dispatch_repo.get(run_id)
