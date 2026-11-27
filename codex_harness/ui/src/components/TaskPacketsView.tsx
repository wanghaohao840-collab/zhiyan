import { useState } from 'react';
import {
  CheckCircle2,
  ChevronDown,
  ChevronRight,
  FileText,
  Lock,
  RefreshCw,
  Send,
} from 'lucide-react';
import type { PacketPlanGroup, TaskPacketInfo } from '../types/harness';

interface TaskPacketsViewProps {
  plans: PacketPlanGroup[];
  onRefresh: () => void;
  onOpenFile: (absPath: string) => void;
  onDispatchPacket: (packet: TaskPacketInfo) => void;
}

function statusPillClass(status: string): string {
  if (status === 'done') return 'ch-pill-green';
  if (status === 'ready') return 'ch-pill-blue';
  if (status === 'blocked') return 'ch-pill-red';
  if (status === 'in_progress') return 'ch-pill-amber';
  return 'ch-pill-neutral';
}

/**
 * 仓库任务包与门禁状态视图（直接映射 docs/agent-workflow/task-packets/）
 */
export function TaskPacketsView({
  plans,
  onRefresh,
  onOpenFile,
  onDispatchPacket,
}: TaskPacketsViewProps) {
  const [expandedPlan, setExpandedPlan] = useState<string | null>(
    plans[0]?.plan_name ?? null,
  );

  return (
    <div className="ch-main">
      <header className="ch-topbar">
        <div className="ch-topbar-left">
          <div className="ch-topbar-title">任务包与门禁状态机</div>
          <div className="ch-topbar-sub">
            扫描 docs/agent-workflow/task-packets/ · 共 {plans.length} 个计划目录
          </div>
        </div>
        <div className="ch-topbar-controls">
          <button type="button" className="ch-btn ch-btn-xs" onClick={onRefresh}>
            <RefreshCw size={12} />
            <span>重新扫描</span>
          </button>
        </div>
      </header>

      <div className="ch-content-scroll">
        {plans.length === 0 ? (
          <div className="ch-empty">未在当前工作区发现任务包目录。</div>
        ) : (
          plans.map((plan) => {
            const open = expandedPlan === plan.plan_name;
            return (
              <div key={plan.plan_name} className="ch-section-card">
                <div
                  style={{
                    display: 'flex',
                    alignItems: 'center',
                    justifyContent: 'space-between',
                    gap: 8,
                    cursor: 'pointer',
                  }}
                  onClick={() =>
                    setExpandedPlan(open ? null : plan.plan_name)
                  }
                  role="button"
                  tabIndex={0}
                >
                  <div style={{ display: 'flex', alignItems: 'center', gap: 7, minWidth: 0 }}>
                    {open ? <ChevronDown size={14} /> : <ChevronRight size={14} />}
                    <strong style={{ fontSize: 13 }}>{plan.plan_name}</strong>
                    <span className="ch-pill ch-pill-neutral">
                      {plan.done_count}/{plan.total_count} done
                    </span>
                    {plan.review_verdict && (
                      <span className="ch-pill ch-pill-blue">
                        REVIEW: {plan.review_verdict}
                      </span>
                    )}
                    {plan.final_review_result && (
                      <span
                        className={`ch-pill ${
                          plan.final_review_result === 'accepted'
                            ? 'ch-pill-green'
                            : 'ch-pill-amber'
                        }`}
                      >
                        FINAL: {plan.final_review_result}
                      </span>
                    )}
                  </div>

                  <div style={{ display: 'flex', gap: 6, flexShrink: 0 }}>
                    {plan.review_path && (
                      <button
                        type="button"
                        className="ch-btn ch-btn-xs"
                        onClick={(e) => {
                          e.stopPropagation();
                          onOpenFile(plan.review_path!);
                        }}
                      >
                        <FileText size={11} />
                        <span>REVIEW</span>
                      </button>
                    )}
                    {plan.final_review_path && (
                      <button
                        type="button"
                        className="ch-btn ch-btn-xs"
                        onClick={(e) => {
                          e.stopPropagation();
                          onOpenFile(plan.final_review_path!);
                        }}
                      >
                        <CheckCircle2 size={11} />
                        <span>FINAL</span>
                      </button>
                    )}
                  </div>
                </div>

                {open && (
                  <div
                    style={{
                      marginTop: 10,
                      paddingTop: 10,
                      borderTop: '1px solid var(--ch-border)',
                      display: 'flex',
                      flexDirection: 'column',
                      gap: 8,
                    }}
                  >
                    {plan.packets.map((pkt) => (
                      <div
                        key={pkt.relative_path}
                        style={{
                          padding: '9px 11px',
                          borderRadius: 8,
                          border: '1px solid var(--ch-border)',
                          background: 'var(--ch-bg)',
                        }}
                      >
                        <div
                          style={{
                            display: 'flex',
                            alignItems: 'center',
                            justifyContent: 'space-between',
                            gap: 8,
                          }}
                        >
                          <div style={{ display: 'flex', alignItems: 'center', gap: 6, flexWrap: 'wrap' }}>
                            <span className={`ch-pill ${statusPillClass(pkt.status)}`}>
                              {pkt.status}
                            </span>
                            <strong style={{ fontSize: 12.5 }}>
                              {pkt.packet_id} · {pkt.title}
                            </strong>
                            {pkt.unmet_dependencies.length > 0 && (
                              <span className="ch-pill ch-pill-red">
                                <Lock size={10} />
                                <span>待前置: {pkt.unmet_dependencies.join(', ')}</span>
                              </span>
                            )}
                          </div>
                          <div style={{ display: 'flex', gap: 6, flexShrink: 0 }}>
                            <button
                              type="button"
                              className="ch-btn ch-btn-xs"
                              onClick={() => onOpenFile(pkt.absolute_path)}
                            >
                              打开文档
                            </button>
                            <button
                              type="button"
                              className="ch-btn ch-btn-xs ch-btn-primary"
                              disabled={pkt.unmet_dependencies.length > 0}
                              onClick={() => onDispatchPacket(pkt)}
                            >
                              <Send size={11} />
                              <span>派单</span>
                            </button>
                          </div>
                        </div>

                        {pkt.goal_excerpt && (
                          <div
                            style={{
                              marginTop: 5,
                              fontSize: 12,
                              color: 'var(--ch-text-secondary)',
                            }}
                          >
                            {pkt.goal_excerpt}
                          </div>
                        )}

                        {pkt.allowed_files.length > 0 && (
                          <div className="ch-dispatch-scopes" style={{ marginTop: 6, marginBottom: 0 }}>
                            {pkt.allowed_files.slice(0, 5).map((f) => (
                              <span key={f} className="ch-pill ch-pill-neutral">
                                {f}
                              </span>
                            ))}
                            {pkt.allowed_files.length > 5 && (
                              <span className="ch-pill ch-pill-neutral">
                                +{pkt.allowed_files.length - 5}
                              </span>
                            )}
                          </div>
                        )}
                      </div>
                    ))}
                  </div>
                )}
              </div>
            );
          })
        )}
      </div>
    </div>
  );
}
