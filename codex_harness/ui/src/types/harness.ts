/**
 * Antigravity × Codex Harness 前端数据契约（与后端 Pydantic schema 严格对齐）
 */

export type ScopeAuditStatus = 'unchecked' | 'clean' | 'violation' | 'warning';
export type HarnessViewMode = 'monitor' | 'packets' | 'dispatch';

export interface ScopeAuditResult {
  status: ScopeAuditStatus;
  allowed_files: string[];
  forbidden_paths: string[];
  observed_files: string[];
  out_of_scope_files: string[];
  forbidden_hits: string[];
  summary: string;
}

export interface CommandStep {
  id: string;
  command: string;
  cwd?: string | null;
  exit_code?: number | null;
  duration_ms?: number | null;
  status: string;
  output_preview: string;
  output_truncated: boolean;
}

export interface FileChangeStep {
  path: string;
  change_type: string;
  diff_preview: string;
  diff_truncated: boolean;
}

export interface SubAgentStep {
  id: string;
  kind: string;
  model?: string | null;
  effort?: string | null;
  agent_thread_id?: string | null;
  agent_path?: string | null;
}

export interface TimelineItem {
  id: string;
  kind: 'narration' | 'reasoning' | 'command' | 'file_change' | 'subagent' | 'final_answer' | 'error';
  timestamp?: string | null;
  text?: string | null;
  command?: CommandStep | null;
  file_change?: FileChangeStep | null;
  subagent?: SubAgentStep | null;
}

export interface SessionTurn {
  turn_index: number;
  turn_id?: string | null;
  started_at?: string | null;
  completed_at?: string | null;
  duration_ms?: number | null;
  source_label: string;
  source_model?: string | null;
  target_role?: string | null;
  target_model?: string | null;
  target_effort?: string | null;
  sandbox?: string | null;
  packet_id?: string | null;
  allowed_files: string[];
  prompt: string;
  prompt_line_count: number;
  status: string;
  command_count: number;
  file_count: number;
  changed_files: string[];
  timeline: TimelineItem[];
  final_answer?: string | null;
  error_message?: string | null;
  scope_audit?: ScopeAuditResult | null;
}

export interface SessionSummary {
  session_id: string;
  source_kind: 'harness' | 'codex_session';
  project_name: string;
  workspace: string;
  worktree_name?: string | null;
  title: string;
  model?: string | null;
  effort?: string | null;
  role?: string | null;
  turn_count: number;
  status: string;
  started_at?: string | null;
  updated_at?: string | null;
  updated_ts: number;
  latest_activity_ts?: number;
  originator?: string | null;
  is_subagent: boolean;
  parent_thread_id?: string | null;
  agent_nickname?: string | null;
  packet_id?: string | null;
  packet_path?: string | null;
  run_id?: string | null;
  cancel_requested?: boolean;
  codex_thread_id?: string | null;
  command_count: number;
  file_count: number;
  scope_status: ScopeAuditStatus;
  child_count?: number;
  child_sessions?: SessionSummary[];
}

export interface ProjectSessionGroup {
  project_name: string;
  workspace: string;
  session_count: number;
  total_thread_count?: number;
  running_count: number;
  latest_updated_ts: number;
  sessions: SessionSummary[];
}

export interface SessionDetail {
  summary: SessionSummary;
  source_file?: string | null;
  delegation_doc_path?: string | null;
  turns: SessionTurn[];
  rate_limit_hint?: Record<string, unknown> | null;
}

export interface DashboardSnapshot {
  workspace: string;
  delegation_doc_path?: string | null;
  codex_cli_available: boolean;
  codex_cli_path?: string | null;
  total_sessions: number;
  running_dispatches: number;
  harness_runs: number;
  project_groups: ProjectSessionGroup[];
  packet_plans_summary: Array<{
    plan_name: string;
    done_count: number;
    total_count: number;
    review_verdict?: string | null;
    final_review_result?: string | null;
  }>;
  server_time: string;
}

export interface TaskPacketInfo {
  plan_name: string;
  packet_id: string;
  title: string;
  status: string;
  parallel_safe: boolean;
  depends_on: string[];
  base_commit?: string | null;
  owner?: string | null;
  relative_path: string;
  absolute_path: string;
  goal_excerpt: string;
  allowed_files: string[];
  forbidden_notes: string[];
  verification_commands: string[];
  unmet_dependencies: string[];
  dispatchable: boolean;
}

export interface PacketPlanGroup {
  plan_name: string;
  directory_path: string;
  has_review: boolean;
  review_path?: string | null;
  review_verdict?: string | null;
  has_final_review: boolean;
  final_review_path?: string | null;
  final_review_result?: string | null;
  done_count: number;
  total_count: number;
  packets: TaskPacketInfo[];
}

export interface RoutingDecision {
  task_type: string;
  role: string;
  model: string;
  effort: string;
  sandbox: string;
  requires_astra_review: boolean;
  fallback_note?: string | null;
  rationale: string;
  required_gates: string[];
}

export interface PlanDocSummary {
  title: string;
  relative_path: string;
  absolute_path: string;
  category: 'plan' | 'agent_work' | 'task_packet';
  updated_at: string;
  unchecked_items: number;
  total_items: number;
}

export interface SpecInferenceResult {
  suggested_title: string;
  suggested_task_type: string;
  suggested_involves_ownership_check: boolean;
  suggested_involves_concurrency_or_lease: boolean;
  suggested_involves_cross_store_or_rollback: boolean;
  suggested_is_complex: boolean;
  objective: string;
  acceptance_criteria: string[];
  allowed_files: string[];
  forbidden_paths: string[];
  verification_commands: string[];
  matched_domains: string[];
  matched_plan_docs: string[];
  industry_standards_applied: string[];
  rationale: string;
  routing_preview: RoutingDecision;
}

export interface DispatchFormDraft {
  title: string;
  objective: string | null;
  workspace: string;
  task_type: string;
  involves_ownership_check: boolean | null;
  involves_concurrency_or_lease: boolean | null;
  involves_cross_store_or_rollback: boolean | null;
  is_complex: boolean | null;
  plan_doc_path: string;
  packet_path: string;
  allowed_files_text: string | null;
  verification_text: string | null;
  auto_infer: boolean;
  resume_run_id: string;
  resume_codex_thread_id: string;
}

export interface DispatchResponsePayload {
  run_id: string;
  session_id: string;
  status: string;
  routing: RoutingDecision;
  preflight_warnings: string[];
  compiled_prompt: string;
  codex_command_preview: string[];
  message: string;
}
