import { Eye, RotateCcw, Send, ShieldCheck } from 'lucide-react';
import type {
  DispatchFormDraft,
  DispatchResponsePayload,
  RoutingDecision,
} from '../types/harness';
import { EMPTY_DISPATCH_DRAFT } from '../hooks/useHarnessData';

interface DispatchViewProps {
  draft: DispatchFormDraft;
  onChangeDraft: (next: DispatchFormDraft) => void;
  routingPreview: RoutingDecision | null;
  submitting: boolean;
  lastResult: DispatchResponsePayload | null;
  onSubmit: (dryRun: boolean) => void;
  codexAvailable: boolean;
}

/**
 * 显式 Harness 派单表单：自动按 AGENTS.md 预览模型路由、强制门禁与严格白名单
 */
export function DispatchView({
  draft,
  onChangeDraft,
  routingPreview,
  submitting,
  lastResult,
  onSubmit,
  codexAvailable,
}: DispatchViewProps) {
  const update = <K extends keyof DispatchFormDraft>(key: K, value: DispatchFormDraft[K]) => {
    onChangeDraft({ ...draft, [key]: value });
  };

  return (
    <div className="ch-main">
      <header className="ch-topbar">
        <div className="ch-topbar-left">
          <div className="ch-topbar-title">发起显式 Codex 派单</div>
          <div className="ch-topbar-sub">
            依据 AGENTS.md 自动路由模型与推理强度，执行前后强制审计改动范围白名单
          </div>
        </div>
        <div className="ch-topbar-controls">
          <button
            type="button"
            className="ch-btn ch-btn-xs"
            onClick={() => onChangeDraft(EMPTY_DISPATCH_DRAFT)}
          >
            <RotateCcw size={11} />
            <span>重置</span>
          </button>
        </div>
      </header>

      <div className="ch-content-scroll">
        <div className="ch-form-grid">
          {/* AGENTS.md 实时路由决策预览卡 */}
          {routingPreview && (
            <div className="ch-section-card" style={{ marginBottom: 0 }}>
              <div
                style={{
                  display: 'flex',
                  alignItems: 'center',
                  justifyContent: 'space-between',
                  flexWrap: 'wrap',
                  gap: 8,
                  marginBottom: 6,
                }}
              >
                <div style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
                  <ShieldCheck size={14} style={{ color: 'var(--ch-green)' }} />
                  <strong>AGENTS.md 路由决策预览</strong>
                </div>
                <div style={{ display: 'flex', gap: 6, flexWrap: 'wrap' }}>
                  <span className="ch-pill ch-pill-blue">
                    {routingPreview.role} · {routingPreview.model}
                  </span>
                  <span className="ch-pill ch-pill-amber">effort: {routingPreview.effort}</span>
                  <span className="ch-pill ch-pill-neutral">{routingPreview.sandbox}</span>
                  {routingPreview.requires_astra_review && (
                    <span className="ch-pill ch-pill-red">须 Astra High 复核</span>
                  )}
                </div>
              </div>
              <div style={{ fontSize: 12, color: 'var(--ch-text-secondary)' }}>
                {routingPreview.rationale}
              </div>
              {routingPreview.fallback_note && (
                <div style={{ fontSize: 11.5, color: 'var(--ch-amber)', marginTop: 4 }}>
                  {routingPreview.fallback_note}
                </div>
              )}
              {routingPreview.required_gates.length > 0 && (
                <ul
                  style={{
                    margin: '6px 0 0',
                    paddingLeft: 18,
                    fontSize: 11.5,
                    color: 'var(--ch-text-muted)',
                  }}
                >
                  {routingPreview.required_gates.map((gate) => (
                    <li key={gate}>{gate}</li>
                  ))}
                </ul>
              )}
            </div>
          )}

          <div className="ch-field">
            <label htmlFor="ch-title">派单标题 *</label>
            <input
              id="ch-title"
              className="ch-input"
              placeholder="例如：修复 QaWorker 租约过期后的迟到写入拒写回归"
              value={draft.title}
              onChange={(e) => update('title', e.target.value)}
            />
          </div>

          <div className="ch-field">
            <label htmlFor="ch-task-type">任务类别（决定模型分层与沙箱）</label>
            <select
              id="ch-task-type"
              className="ch-select"
              value={draft.task_type}
              onChange={(e) => update('task_type', e.target.value)}
            >
              <option value="implement_general">一般功能 / 报告下载 / 存储接入 (Sol Medium/High)</option>
              <option value="implement_transaction">事务缺陷修复与回归测试 (Sol High)</option>
              <option value="review_concurrency">并发 / 租约 / 跨存储 / 回退安全独立审查 (Astra High · 只读)</option>
              <option value="chore">日志整理 / 进度更新 / 机械修改 (Luna / Sol 回退)</option>
            </select>
          </div>

          <div className="ch-check-grid">
            <label className="ch-check-card">
              <input
                type="checkbox"
                checked={draft.involves_ownership_check}
                onChange={(e) => update('involves_ownership_check', e.target.checked)}
              />
              <span>涉及用户归属校验</span>
            </label>
            <label className="ch-check-card">
              <input
                type="checkbox"
                checked={draft.involves_concurrency_or_lease}
                onChange={(e) => update('involves_concurrency_or_lease', e.target.checked)}
              />
              <span>涉及租约 / 并发竞争</span>
            </label>
            <label className="ch-check-card">
              <input
                type="checkbox"
                checked={draft.involves_cross_store_or_rollback}
                onChange={(e) => update('involves_cross_store_or_rollback', e.target.checked)}
              />
              <span>涉及跨存储 / 回退安全</span>
            </label>
            <label className="ch-check-card">
              <input
                type="checkbox"
                checked={draft.is_complex}
                onChange={(e) => update('is_complex', e.target.checked)}
              />
              <span>多组件复杂状态推理</span>
            </label>
          </div>

          <div className="ch-field">
            <label htmlFor="ch-objective">目标与可验证完成条件 *</label>
            <textarea
              id="ch-objective"
              rows={4}
              className="ch-textarea"
              placeholder="写清操作顺序和必须成立的观测结果。例如：Worker A 租约过期，Worker B 接管并提交后，A 的迟到写入必须被拒绝，B 的结果仍可读取。"
              value={draft.objective}
              onChange={(e) => update('objective', e.target.value)}
            />
          </div>

          <div className="ch-field">
            <label htmlFor="ch-allowed">允许创建/修改的文件白名单（每行一个相对路径或 glob，严格范围门禁）</label>
            <textarea
              id="ch-allowed"
              rows={3}
              className="ch-textarea"
              placeholder={'app/qa_worker.py\ntests/test_qa_worker.py'}
              value={draft.allowed_files_text}
              onChange={(e) => update('allowed_files_text', e.target.value)}
            />
          </div>

          <div className="ch-field">
            <label htmlFor="ch-verify">验收命令（每行一条）</label>
            <textarea
              id="ch-verify"
              rows={2}
              className="ch-textarea"
              placeholder={'.\\venv\\Scripts\\python.exe -m pytest -q --basetemp=.pytest-tmp-codex tests/test_qa_worker.py'}
              value={draft.verification_text}
              onChange={(e) => update('verification_text', e.target.value)}
            />
          </div>

          {draft.packet_path && (
            <div style={{ fontSize: 12, color: 'var(--ch-text-secondary)' }}>
              已绑定任务包：<code>{draft.packet_path}</code>
            </div>
          )}

          {draft.resume_codex_thread_id && (
            <div style={{ fontSize: 12, color: 'var(--ch-text-secondary)' }}>
              续轮目标线程：<code>{draft.resume_codex_thread_id}</code>
            </div>
          )}

          <div style={{ display: 'flex', alignItems: 'center', gap: 8, flexWrap: 'wrap' }}>
            <button
              type="button"
              className="ch-btn"
              disabled={submitting}
              onClick={() => onSubmit(true)}
            >
              <Eye size={13} />
              <span>干跑预检 (Dry Run)</span>
            </button>
            <button
              type="button"
              className="ch-btn ch-btn-primary"
              disabled={submitting || !codexAvailable}
              onClick={() => onSubmit(false)}
            >
              <Send size={13} />
              <span>{submitting ? '正在派单...' : '正式派单给 Codex'}</span>
            </button>
            {!codexAvailable && (
              <span style={{ fontSize: 11.5, color: 'var(--ch-red)' }}>
                未检测到本机 Codex CLI，仅支持干跑预检
              </span>
            )}
          </div>

          {lastResult && (
            <div className="ch-section-card" style={{ marginTop: 4 }}>
              <div style={{ fontWeight: 600, marginBottom: 6 }}>
                预检结果（{lastResult.status}）
              </div>
              <div style={{ fontSize: 12, color: 'var(--ch-text-secondary)', marginBottom: 8 }}>
                {lastResult.message}
              </div>
              {lastResult.preflight_warnings.length > 0 && (
                <div className="ch-scope-audit-box" style={{ marginTop: 0, marginBottom: 8 }}>
                  {lastResult.preflight_warnings.map((w) => (
                    <div key={w}>• {w}</div>
                  ))}
                </div>
              )}
              <pre className="ch-cmd-output">{lastResult.compiled_prompt}</pre>
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
