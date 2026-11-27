"""单向实时监控聚合服务：合并 Harness 显式派单与本机 Codex 会话，生成左侧树与右侧详情。"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from codex_harness.model.entities import DispatchStatus, ScopeAuditStatus
from codex_harness.repository.dispatch_repository import DispatchRepository
from codex_harness.repository.packet_repository import PacketRepository
from codex_harness.repository.session_repository import (
    SessionRepository,
    derive_worktree_name,
    parse_exec_events_file,
)
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
        for rel in (
            Path("docs") / "agent-workflow" / "codex-delegation.md",
            Path("docs") / "codex-delegation.md",
        ):
            doc = self._workspace / rel
            if doc.exists():
                return str(doc.resolve())
        return None

    def _build_harness_session_summary(self, rounds: list[DispatchRoundRecord]) -> SessionSummary:
        """将同一 Harness 会话的多轮派单聚合成左侧栏摘要项。"""
        latest = rounds[-1]
        first = rounds[0]
        total_cmds = sum(item.command_count for item in rounds)
        all_files: set[str] = set()
        for item in rounds:
            all_files.update(item.changed_files)

        updated_iso = latest.completed_at or latest.started_at
        updated_ts = _parse_iso_ts(updated_iso)
        scope_status = latest.scope_audit.status if latest.scope_audit else ScopeAuditStatus.UNCHECKED
        effective_parent = next((r.parent_thread_id for r in reversed(rounds) if r.parent_thread_id), None)
        return SessionSummary(
            session_id=latest.session_id,
            source_kind="harness",
            project_name=latest.project_name,
            workspace=latest.workspace,
            worktree_name=derive_worktree_name(latest.workspace),
            title=first.title,
            model=latest.routing.model,
            effort=latest.routing.effort.value,
            role=latest.routing.role.value,
            turn_count=len(rounds),
            status=latest.status.value,
            started_at=first.started_at,
            updated_at=updated_iso,
            updated_ts=updated_ts,
            latest_activity_ts=updated_ts,
            originator=f"{latest.source_agent} → Codex",
            is_subagent=bool(effective_parent),
            parent_thread_id=effective_parent,
            packet_id=latest.packet_id,
            packet_path=latest.packet_path if latest.packet_id is not None else None,
            run_id=latest.run_id,
            cancel_requested=(
                latest.status in (DispatchStatus.QUEUED, DispatchStatus.RUNNING)
                and self._dispatch_repo.cancellation_requested(latest.run_id)
            ),
            codex_thread_id=latest.codex_thread_id,
            command_count=total_cmds,
            file_count=len(all_files),
            scope_status=scope_status,
        )

    @staticmethod
    def _matches_query(item: SessionSummary, lower_query: str) -> bool:
        if not lower_query:
            return True
        haystack = " ".join(
            filter(
                None,
                [
                    item.title,
                    item.project_name,
                    item.workspace,
                    item.worktree_name,
                    item.model,
                    item.agent_nickname,
                    item.packet_id,
                    item.session_id,
                ],
            )
        ).lower()
        return lower_query in haystack

    def get_dashboard_snapshot(self, query: str | None = None, limit: int = 120) -> DashboardSnapshot:
        """构建左侧按项目 → 主对话窗口（嵌套子代理）组织的实时同步快照。"""
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

        # 全量加载会话摘要（stat 缓存保障毫秒级响应），以便精确建立父子对话窗口归属
        codex_sessions = self._session_repo.list_sessions(limit=max(limit * 3, 240), query=None)
        codex_by_id: dict[str, SessionSummary] = {item.session_id: item for item in codex_sessions}

        # 建立平铺的去重会话集合（复制避免污染缓存对象）
        all_nodes: list[SessionSummary] = []
        for rounds in runs_by_session.values():
            rounds.sort(key=lambda item: (item.round_number, item.started_at))
            summary = self._build_harness_session_summary(rounds)
            # 若该 Harness 续轮派单关联到了某个 Codex 子代理，继承其 parent_thread_id 以便归入同一对话窗口
            if summary.codex_thread_id and summary.codex_thread_id in codex_by_id:
                linked = codex_by_id[summary.codex_thread_id]
                if linked.parent_thread_id:
                    summary.parent_thread_id = linked.parent_thread_id
                    summary.is_subagent = linked.is_subagent
            all_nodes.append(summary)

        for item in codex_sessions:
            if item.session_id in linked_codex_threads:
                continue
            all_nodes.append(item.model_copy(update={"child_sessions": [], "child_count": 0, "latest_activity_ts": item.updated_ts}))

        node_by_id: dict[str, SessionSummary] = {node.session_id: node for node in all_nodes}
        # 也允许通过 codex_thread_id 查到对应的 Harness 节点
        for node in all_nodes:
            if node.codex_thread_id and node.codex_thread_id not in node_by_id:
                node_by_id[node.codex_thread_id] = node

        def resolve_root_id(node: SessionSummary) -> str | None:
            """向上沿 parent_thread_id 追溯，定位该子代理所属的最顶层主对话窗口。"""
            visited: set[str] = {node.session_id}
            curr_parent = node.parent_thread_id
            last_valid_parent: str | None = None
            while curr_parent and curr_parent not in visited:
                parent_node = node_by_id.get(curr_parent)
                if parent_node is None:
                    break
                visited.add(parent_node.session_id)
                last_valid_parent = parent_node.session_id
                curr_parent = parent_node.parent_thread_id
            return last_valid_parent

        root_nodes: list[SessionSummary] = []
        children_by_root: dict[str, list[SessionSummary]] = {}
        for node in all_nodes:
            root_id = resolve_root_id(node)
            if root_id and root_id != node.session_id:
                children_by_root.setdefault(root_id, []).append(node)
            else:
                root_nodes.append(node)

        lower_query = query.strip().lower() if query else ""
        filtered_roots: list[SessionSummary] = []

        for root in root_nodes:
            kids = children_by_root.get(root.session_id, [])
            kids.sort(key=lambda item: item.updated_ts, reverse=True)

            if lower_query:
                root_hit = self._matches_query(root, lower_query)
                matched_kids = [k for k in kids if self._matches_query(k, lower_query)]
                if not root_hit and not matched_kids:
                    continue
                visible_kids = kids if root_hit else matched_kids
            else:
                visible_kids = kids

            latest_ts = max([root.updated_ts, *(k.updated_ts for k in visible_kids)])
            any_running = root.status == "running" or any(k.status == "running" for k in visible_kids)
            root_copy = root.model_copy(
                update={
                    "child_sessions": visible_kids,
                    "child_count": len(visible_kids),
                    "latest_activity_ts": latest_ts,
                    "status": "running" if any_running else root.status,
                }
            )
            filtered_roots.append(root_copy)

        filtered_roots.sort(key=lambda item: item.latest_activity_ts, reverse=True)
        filtered_roots = filtered_roots[:limit]

        # 按项目名分组，并将各项目内所有 worktree 会话收敛在同一主仓库下
        groups_map: dict[str, ProjectSessionGroup] = {}
        total_threads = 0
        for root in filtered_roots:
            group = groups_map.get(root.project_name)
            if group is None:
                group = ProjectSessionGroup(
                    project_name=root.project_name,
                    workspace=root.workspace,
                    session_count=0,
                    total_thread_count=0,
                    running_count=0,
                    latest_updated_ts=root.latest_activity_ts,
                    sessions=[],
                )
                groups_map[root.project_name] = group
            group.sessions.append(root)
            group.session_count += 1
            thread_inc = 1 + root.child_count
            group.total_thread_count += thread_inc
            total_threads += thread_inc
            if root.status == "running":
                group.running_count += 1
            if root.latest_activity_ts > group.latest_updated_ts:
                group.latest_updated_ts = root.latest_activity_ts

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
            total_sessions=total_threads,
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
