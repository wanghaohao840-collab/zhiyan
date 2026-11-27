"""单向实时监控聚合服务：合并 Harness 显式派单与本机 Codex 会话，生成左侧树与右侧详情。"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from codex_harness.model.entities import DispatchStatus, ScopeAuditStatus
from codex_harness.repository.dispatch_repository import DispatchRepository
from codex_harness.repository.packet_repository import PacketRepository
from codex_harness.repository.session_repository import SessionRepository, parse_exec_events_file
from codex_harness.schema.contracts import (
    DashboardSnapshot,
    DispatchRoundRecord,
    ProjectSessionGroup,
    SessionDetail,
    SessionSummary,
    SessionTurn,
)
from codex_harness.service.dispatch_service import resolve_codex_executable


def _parse_iso_ts(raw: str | None) -> float:
    if not raw:
        return 0.0
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


class MonitorService:
    """聚合本机 Codex 会话、Harness 显式派单记录与仓库任务包状态。"""

    def __init__(
        self,
        workspace: Path,
        session_repo: SessionRepository,
        dispatch_repo: DispatchRepository,
        packet_repo: PacketRepository,
    ) -> None:
        self._workspace = workspace.resolve()
        self._session_repo = session_repo
        self._dispatch_repo = dispatch_repo
        self._packet_repo = packet_repo

    def _delegation_doc_path(self) -> str | None:
        doc = self._workspace / "docs" / "agent-workflow" / "codex-delegation.md"
        return str(doc.resolve()) if doc.exists() else None

    def _build_harness_session_summary(self, rounds: list[DispatchRoundRecord]) -> SessionSummary:
        """将同一 Harness 会话的多轮派单聚合成左侧栏摘要项。"""
        latest = rounds[-1]
        first = rounds[0]
        total_cmds = sum(item.command_count for item in rounds)
        all_files: set[str] = set()
        for item in rounds:
            all_files.update(item.changed_files)

        updated_iso = latest.completed_at or latest.started_at
        scope_status = latest.scope_audit.status if latest.scope_audit else ScopeAuditStatus.UNCHECKED
        return SessionSummary(
            session_id=latest.session_id,
            source_kind="harness",
            project_name=latest.project_name,
            workspace=latest.workspace,
            title=first.title,
            model=latest.routing.model,
            effort=latest.routing.effort.value,
            role=latest.routing.role.value,
            turn_count=len(rounds),
            status=latest.status.value,
            started_at=first.started_at,
            updated_at=updated_iso,
            updated_ts=_parse_iso_ts(updated_iso),
            originator=f"{latest.source_agent} → Codex",
            packet_id=latest.packet_id,
            run_id=latest.run_id,
            codex_thread_id=latest.codex_thread_id,
            command_count=total_cmds,
            file_count=len(all_files),
            scope_status=scope_status,
        )

    def get_dashboard_snapshot(self, query: str | None = None, limit: int = 80) -> DashboardSnapshot:
        """构建左侧项目会话树与顶部同步状态快照。"""
        all_runs = self._dispatch_repo.list_all()
        runs_by_session: dict[str, list[DispatchRoundRecord]] = {}
        linked_codex_threads: set[str] = set()
        running_dispatches = 0

        for run in all_runs:
            runs_by_session.setdefault(run.session_id, []).append(run)
            if run.codex_thread_id:
                linked_codex_threads.add(run.codex_thread_id)
            if run.status == DispatchStatus.RUNNING:
                running_dispatches += 1

        lower_query = query.strip().lower() if query else ""
        merged_summaries: list[SessionSummary] = []

        for rounds in runs_by_session.values():
            rounds.sort(key=lambda item: (item.round_number, item.started_at))
            summary = self._build_harness_session_summary(rounds)
            if lower_query:
                haystack = " ".join(
                    filter(
                        None,
                        [
                            summary.title,
                            summary.project_name,
                            summary.workspace,
                            summary.model,
                            summary.packet_id,
                            summary.session_id,
                        ],
                    )
                ).lower()
                if lower_query not in haystack:
                    continue
            merged_summaries.append(summary)

        codex_sessions = self._session_repo.list_sessions(limit=limit, query=query)
        for item in codex_sessions:
            if item.session_id in linked_codex_threads:
                # 已关联到 Harness 派单的底层线程不重复展示
                continue
            merged_summaries.append(item)

        merged_summaries.sort(key=lambda item: item.updated_ts, reverse=True)

        # 按项目名分组，并保持最近活跃项目排在前面
        groups_map: dict[str, ProjectSessionGroup] = {}
        for item in merged_summaries:
            group = groups_map.get(item.project_name)
            if group is None:
                group = ProjectSessionGroup(
                    project_name=item.project_name,
                    workspace=item.workspace,
                    session_count=0,
                    running_count=0,
                    latest_updated_ts=item.updated_ts,
                    sessions=[],
                )
                groups_map[item.project_name] = group
            group.sessions.append(item)
            group.session_count += 1
            if item.status == "running":
                group.running_count += 1
            if item.updated_ts > group.latest_updated_ts:
                group.latest_updated_ts = item.updated_ts

        project_groups = sorted(groups_map.values(), key=lambda g: g.latest_updated_ts, reverse=True)

        packet_groups = self._packet_repo.list_plan_groups()
        packet_summary = [
            {
                "plan_name": grp.plan_name,
                "done_count": grp.done_count,
                "total_count": grp.total_count,
                "review_verdict": grp.review_verdict,
                "final_review_result": grp.final_review_result,
            }
            for grp in packet_groups[:15]
        ]

        codex_bin = resolve_codex_executable()
        return DashboardSnapshot(
            workspace=str(self._workspace),
            delegation_doc_path=self._delegation_doc_path(),
            codex_cli_available=bool(codex_bin),
            codex_cli_path=codex_bin,
            total_sessions=len(merged_summaries),
            running_dispatches=running_dispatches,
            harness_runs=len(all_runs),
            project_groups=project_groups,
            packet_plans_summary=packet_summary,
            server_time=datetime.now(tz=timezone.utc).isoformat(),
        )

    def get_session_detail(self, session_id: str) -> SessionDetail | None:
        """按 session_id 或 run_id 读取右侧单向监控完整详情。"""
        rounds = self._dispatch_repo.list_by_session(session_id)
        if not rounds:
            single = self._dispatch_repo.get(session_id)
            if single is not None:
                rounds = self._dispatch_repo.list_by_session(single.session_id)

        if rounds:
            summary = self._build_harness_session_summary(rounds)
            turns: list[SessionTurn] = []
            # 若底层 Codex rollout 文件也已落盘，可提取更丰富的 Command/Diff 轨迹作为补充
            rollout_detail = (
                self._session_repo.get_session_detail(summary.codex_thread_id)
                if summary.codex_thread_id
                else None
            )

            for idx, run in enumerate(rounds):
                events_path = Path(run.events_file) if run.events_file else self._dispatch_repo.events_path(run.run_id)
                parsed_exec = parse_exec_events_file(events_path)
                timeline = list(parsed_exec["timeline"])
                command_count = max(run.command_count, parsed_exec["command_count"])
                changed_files = list(dict.fromkeys([*run.changed_files, *parsed_exec["changed_files"]]))
                final_answer = run.final_answer or parsed_exec["final_answer"]

                # 当 codex exec 事件流为空或精简、而对应 rollout 存在同轮轨迹时自动补全
                if not timeline and rollout_detail and idx < len(rollout_detail.turns):
                    rt = rollout_detail.turns[idx]
                    timeline = rt.timeline
                    command_count = max(command_count, rt.command_count)
                    changed_files = list(dict.fromkeys([*changed_files, *rt.changed_files]))
                    final_answer = final_answer or rt.final_answer

                turns.append(
                    SessionTurn(
                        turn_index=run.round_number,
                        turn_id=run.run_id,
                        started_at=run.started_at,
                        completed_at=run.completed_at,
                        duration_ms=run.duration_ms,
                        source_label=run.source_agent,
                        source_model=run.source_model,
                        target_role=run.routing.role.value,
                        target_model=run.routing.model,
                        target_effort=run.routing.effort.value,
                        sandbox=run.routing.sandbox.value,
                        packet_id=run.packet_id,
                        allowed_files=run.allowed_files,
                        prompt=run.compiled_prompt,
                        prompt_line_count=len(run.compiled_prompt.splitlines()),
                        status=run.status.value,
                        command_count=command_count,
                        file_count=len(changed_files),
                        changed_files=changed_files,
                        timeline=timeline,
                        final_answer=final_answer,
                        error_message=run.error_message or parsed_exec["error_message"],
                        scope_audit=run.scope_audit,
                    )
                )

            return SessionDetail(
                summary=summary,
                source_file=str(self._dispatch_repo.record_path(rounds[-1].run_id)),
                delegation_doc_path=self._delegation_doc_path(),
                turns=turns,
                rate_limit_hint=rollout_detail.rate_limit_hint if rollout_detail else None,
            )

        detail = self._session_repo.get_session_detail(session_id)
        if detail is not None:
            detail.delegation_doc_path = self._delegation_doc_path()
        return detail
