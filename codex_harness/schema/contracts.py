"""Codex Harness 请求、响应与监控视图 Pydantic 数据契约。"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from codex_harness.model.entities import (
    DispatchStatus,
    HarnessRole,
    ReasoningEffort,
    SandboxMode,
    ScopeAuditStatus,
    TaskType,
)


class RoutingRequest(BaseModel):
    """模型与风险路由评估入参。"""

    task_type: TaskType = TaskType.IMPLEMENT_GENERAL
    involves_ownership_check: bool = False
    involves_concurrency_or_lease: bool = False
    involves_cross_store_or_rollback: bool = False
    is_complex: bool = False
    luna_available: bool = False
    model_override: str | None = None
    effort_override: ReasoningEffort | None = None
    sandbox_override: SandboxMode | None = None


class RoutingDecision(BaseModel):
    """依据 AGENTS.md 计算得出的显式模型与门禁决策。"""

    task_type: TaskType
    role: HarnessRole
    model: str
    effort: ReasoningEffort
    sandbox: SandboxMode
    requires_astra_review: bool = False
    fallback_note: str | None = None
    rationale: str
    required_gates: list[str] = Field(default_factory=list)


class ScopeAuditResult(BaseModel):
    """派单范围白名单与禁止路径审计结果。"""

    status: ScopeAuditStatus = ScopeAuditStatus.UNCHECKED
    allowed_files: list[str] = Field(default_factory=list)
    forbidden_paths: list[str] = Field(default_factory=list)
    observed_files: list[str] = Field(default_factory=list)
    out_of_scope_files: list[str] = Field(default_factory=list)
    forbidden_hits: list[str] = Field(default_factory=list)
    summary: str = ""


class CommandStep(BaseModel):
    """单条 Shell 命令执行记录。"""

    id: str
    command: str
    cwd: str | None = None
    exit_code: int | None = None
    duration_ms: int | None = None
    status: str = "completed"
    output_preview: str = ""
    output_truncated: bool = False


class FileChangeStep(BaseModel):
    """单次文件变更与精简 Diff。"""

    path: str
    change_type: str = "update"
    diff_preview: str = ""
    diff_truncated: bool = False


class SubAgentStep(BaseModel):
    """子 Agent 派生或交互事件。"""

    id: str
    kind: str
    model: str | None = None
    effort: str | None = None
    agent_thread_id: str | None = None
    agent_path: str | None = None


class TimelineItem(BaseModel):
    """按时间顺序排列的 Codex 执行轨迹节点。"""

    id: str
    kind: Literal["narration", "reasoning", "command", "file_change", "subagent", "final_answer", "error"]
    timestamp: str | None = None
    text: str | None = None
    command: CommandStep | None = None
    file_change: FileChangeStep | None = None
    subagent: SubAgentStep | None = None


class SessionTurn(BaseModel):
    """单轮派单与 Codex 完整执行闭环。"""

    turn_index: int
    turn_id: str | None = None
    started_at: str | None = None
    completed_at: str | None = None
    duration_ms: int | None = None
    source_label: str = "User"
    source_model: str | None = None
    target_role: str | None = None
    target_model: str | None = None
    target_effort: str | None = None
    sandbox: str | None = None
    packet_id: str | None = None
    allowed_files: list[str] = Field(default_factory=list)
    prompt: str = ""
    prompt_line_count: int = 0
    status: str = "completed"
    command_count: int = 0
    file_count: int = 0
    changed_files: list[str] = Field(default_factory=list)
    timeline: list[TimelineItem] = Field(default_factory=list)
    final_answer: str | None = None
    error_message: str | None = None
    scope_audit: ScopeAuditResult | None = None


class SessionSummary(BaseModel):
    """左侧项目会话树的轻量摘要条目（主对话窗口可嵌套子代理列表）。"""

    session_id: str
    source_kind: Literal["harness", "codex_session"]
    project_name: str
    workspace: str
    worktree_name: str | None = None
    title: str
    model: str | None = None
    effort: str | None = None
    role: str | None = None
    turn_count: int = 0
    status: str = "completed"
    started_at: str | None = None
    updated_at: str | None = None
    updated_ts: float = 0.0
    latest_activity_ts: float = 0.0
    originator: str | None = None
    is_subagent: bool = False
    parent_thread_id: str | None = None
    agent_nickname: str | None = None
    packet_id: str | None = None
    packet_path: str | None = None
    run_id: str | None = None
    cancel_requested: bool = False
    codex_thread_id: str | None = None
    command_count: int = 0
    file_count: int = 0
    scope_status: ScopeAuditStatus = ScopeAuditStatus.UNCHECKED
    child_count: int = 0
    child_sessions: list[SessionSummary] = Field(default_factory=list)


SessionSummary.model_rebuild()


class ProjectSessionGroup(BaseModel):
    """左侧栏按工作区聚合的项目分组（session_count 为顶层对话窗口数）。"""

    project_name: str
    workspace: str
    session_count: int
    total_thread_count: int = 0
    running_count: int = 0
    latest_updated_ts: float = 0.0
    sessions: list[SessionSummary] = Field(default_factory=list)


class SessionDetail(BaseModel):
    """右侧主区完整单向实时监控详情。"""

    summary: SessionSummary
    source_file: str | None = None
    delegation_doc_path: str | None = None
    turns: list[SessionTurn] = Field(default_factory=list)
    rate_limit_hint: dict[str, object] | None = None


class TaskPacketInfo(BaseModel):
    """仓库中解析出的单个任务包信息。"""

    plan_name: str
    packet_id: str
    title: str
    status: str = "draft"
    parallel_safe: bool = False
    depends_on: list[str] = Field(default_factory=list)
    base_commit: str | None = None
    owner: str | None = None
    relative_path: str
    absolute_path: str
    goal_excerpt: str = ""
    allowed_files: list[str] = Field(default_factory=list)
    forbidden_notes: list[str] = Field(default_factory=list)
    verification_commands: list[str] = Field(default_factory=list)
    unmet_dependencies: list[str] = Field(default_factory=list)
    dispatchable: bool = False


class PacketPlanGroup(BaseModel):
    """按计划目录聚合的任务包组。"""

    plan_name: str
    directory_path: str
    has_review: bool = False
    review_path: str | None = None
    review_verdict: str | None = None
    has_final_review: bool = False
    final_review_path: str | None = None
    final_review_result: str | None = None
    done_count: int = 0
    total_count: int = 0
    packets: list[TaskPacketInfo] = Field(default_factory=list)


class PlanDocSummary(BaseModel):
    """仓库中可供派单关联的方案文档摘要。"""

    title: str
    relative_path: str
    absolute_path: str
    category: Literal["plan", "agent_work", "task_packet"] = "plan"
    updated_at: str
    unchecked_items: int = 0
    total_items: int = 0


class SpecInferenceRequest(BaseModel):
    """请求后端根据方案、续轮上下文与行业标准自动推导派单规格。"""

    title: str = ""
    objective_hint: str = ""
    workspace: str | None = None
    task_type: TaskType | None = None
    plan_doc_path: str | None = None
    packet_path: str | None = None
    resume_run_id: str | None = None
    resume_codex_thread_id: str | None = None
    involves_ownership_check: bool | None = None
    involves_concurrency_or_lease: bool | None = None
    involves_cross_store_or_rollback: bool | None = None
    is_complex: bool | None = None
    model_override: str | None = None
    effort_override: ReasoningEffort | None = None
    sandbox_override: SandboxMode | None = None
    allowed_files: list[str] | None = None


class SpecInferenceResult(BaseModel):
    """基于方案文档、历史会话与行业功能模块完成标准推导出的派单规格建议。"""

    suggested_title: str
    suggested_task_type: TaskType
    suggested_involves_ownership_check: bool = False
    suggested_involves_concurrency_or_lease: bool = False
    suggested_involves_cross_store_or_rollback: bool = False
    suggested_is_complex: bool = False
    objective: str
    acceptance_criteria: list[str] = Field(default_factory=list)
    allowed_files: list[str] = Field(default_factory=list)
    forbidden_paths: list[str] = Field(default_factory=list)
    verification_commands: list[str] = Field(default_factory=list)
    matched_domains: list[str] = Field(default_factory=list)
    matched_plan_docs: list[str] = Field(default_factory=list)
    industry_standards_applied: list[str] = Field(default_factory=list)
    rationale: str = ""
    routing_preview: RoutingDecision


class DispatchRequest(BaseModel):
    """向 Codex 发起显式派单的请求契约（支持按方案与行业标准自动补全空白字段）。"""

    title: str = Field(default="", max_length=180)
    objective: str = ""
    workspace: str | None = None
    task_type: TaskType | None = None
    involves_ownership_check: bool | None = None
    involves_concurrency_or_lease: bool | None = None
    involves_cross_store_or_rollback: bool | None = None
    is_complex: bool | None = None
    model_override: str | None = None
    effort_override: ReasoningEffort | None = None
    sandbox_override: SandboxMode | None = None
    plan_doc_path: str | None = None
    packet_path: str | None = None
    allowed_files: list[str] | None = None
    forbidden_paths: list[str] = Field(default_factory=list)
    acceptance_criteria: list[str] = Field(default_factory=list)
    verification_commands: list[str] = Field(default_factory=list)
    auto_infer: bool = True
    source_model: str = "gemini-4-neon-high"
    resume_run_id: str | None = None
    resume_codex_thread_id: str | None = None
    dry_run: bool = False


class DispatchRoundRecord(BaseModel):
    """持久化的单轮 Harness 派单记录。"""

    run_id: str
    session_id: str
    round_number: int = 1
    title: str
    workspace: str
    project_name: str
    source_agent: str = "Antigravity"
    source_model: str = "gemini-4-neon-high"
    routing: RoutingDecision
    packet_id: str | None = None
    packet_path: str | None = None
    allowed_files: list[str] = Field(default_factory=list)
    forbidden_paths: list[str] = Field(default_factory=list)
    acceptance_criteria: list[str] = Field(default_factory=list)
    verification_commands: list[str] = Field(default_factory=list)
    compiled_prompt: str
    status: DispatchStatus = DispatchStatus.QUEUED
    pid: int | None = None
    codex_thread_id: str | None = None
    parent_thread_id: str | None = None
    fallback_from_locked_thread: str | None = None
    started_at: str
    completed_at: str | None = None
    duration_ms: int | None = None
    exit_code: int | None = None
    command_count: int = 0
    file_count: int = 0
    changed_files: list[str] = Field(default_factory=list)
    baseline_dirty_files: list[str] = Field(default_factory=list)
    final_answer: str | None = None
    error_message: str | None = None
    events_file: str | None = None
    scope_audit: ScopeAuditResult = Field(default_factory=ScopeAuditResult)


class DispatchResponse(BaseModel):
    """派单接口响应。"""

    run_id: str
    session_id: str
    status: DispatchStatus
    routing: RoutingDecision
    preflight_warnings: list[str] = Field(default_factory=list)
    compiled_prompt: str
    codex_command_preview: list[str] = Field(default_factory=list)
    message: str


class DashboardSnapshot(BaseModel):
    """面板顶部与左侧栏实时同步快照。"""

    workspace: str
    delegation_doc_path: str | None = None
    codex_cli_available: bool = False
    codex_cli_path: str | None = None
    total_sessions: int = 0
    running_dispatches: int = 0
    harness_runs: int = 0
    project_groups: list[ProjectSessionGroup] = Field(default_factory=list)
    packet_plans_summary: list[dict[str, object]] = Field(default_factory=list)
    server_time: str
