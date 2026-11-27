import { Eye, FileText, RotateCcw, Send, ShieldCheck, Sparkles } from 'lucide-react';
import type {
  DispatchFormDraft,
  DispatchResponsePayload,
  PlanDocSummary,
  RoutingDecision,
  SpecInferenceResult,
} from '../types/harness';

const RISK_OPTIONS = [
  ['involves_ownership_check', '涉及用户归属校验'],
  ['involves_concurrency_or_lease', '涉及租约或并发竞争'],
  ['involves_cross_store_or_rollback', '涉及跨存储或回退安全'],
  ['is_complex', '涉及多组件复杂状态'],
] as const;

interface DispatchViewProps {
  draft: DispatchFormDraft;
  onChangeDraft: (next: DispatchFormDraft) => void;
  planDocs: PlanDocSummary[];
  routingPreview: RoutingDecision | null;
  inferredSpec: SpecInferenceResult | null;
  specError?: string;
  inferring: boolean;
  onInferSpec: () => void;
  onResetDraft: () => void;
  submitting: boolean;
  lastResult: DispatchResponsePayload | null;
  onSubmit: (dryRun: boolean) => void;
  codexAvailable: boolean;
}

/**
 * 显式 Harness 派单表单：支持按方案文档、续轮上下文与行业软件工程完成标准自动推导目标、白名单与验收命令
 */
export function DispatchView({
  draft,
  onChangeDraft,
  planDocs,
  routingPreview,
  inferredSpec,
  specError,
  inferring,
  onInferSpec,
  onResetDraft,
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
            结合方案文档与行业功能模块完成标准，自动推导目标、改动白名单与验收命令
          </div>
        </div>
        <div className="ch-topbar-controls">
          <button
            type="button"
            className="ch-btn ch-btn-xs ch-btn-primary"
            disabled={inferring}
            onClick={onInferSpec}
            title="根据当前输入补全规格，保留已手动填写的内容"
          >
            <Sparkles size={11} />
            <span>{inferring ? '推导中...' : '智能生成规格'}</span>
          </button>
          <button
            type="button"
            className="ch-btn ch-btn-xs"
            onClick={onResetDraft}
          >
            <RotateCcw size={11} />
            <span>重置</span>
          </button>
        </div>
      </header>

      <div className="ch-content-scroll">
        <div className="ch-form-grid">
          {specError && <div className="ch-banner-err" role="alert">{specError}</div>}
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

          {/* 行业功能模块完成标准推导依据卡 */}
          {inferredSpec?.rationale && (
            <div className="ch-section-card" style={{ marginBottom: 0 }}>
              <div
                style={{
                  display: 'flex',
                  alignItems: 'center',
                  justifyContent: 'space-between',
                  flexWrap: 'wrap',
                  gap: 6,
                  marginBottom: 6,
                }}
              >
                <div style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
                  <Sparkles size={13} style={{ color: 'var(--ch-accent)' }} />
                  <strong>已套用行业模块完成标准</strong>
                </div>
                <div style={{ display: 'flex', gap: 5, flexWrap: 'wrap' }}>
                  {inferredSpec.matched_domains.map((dom) => (
                    <span key={dom} className="ch-pill ch-pill-green">
                      {dom}
                    </span>
                  ))}
                </div>
              </div>
              <div style={{ fontSize: 12, color: 'var(--ch-text-secondary)' }}>
                {inferredSpec.rationale}
              </div>
              {inferredSpec.matched_plan_docs.length > 0 && (
                <div
                  style={{
                    marginTop: 6,
                    display: 'flex',
                    alignItems: 'center',
                    gap: 5,
                    flexWrap: 'wrap',
                    fontSize: 11.5,
                    color: 'var(--ch-text-muted)',
                  }}
                >
                  <FileText size={11} />
                  <span>关联方案：</span>
                  {inferredSpec.matched_plan_docs.map((doc) => (
                    <span key={doc} className="ch-pill ch-pill-neutral">
                      {doc}
                    </span>
                  ))}
                </div>
              )}
            </div>
          )}

          {/* 关联仓库方案文档选择器 */}
          <div className="ch-field">
            <label htmlFor="ch-plan-doc">
              关联实施计划 / 方案文档
            </label>
            <select
              id="ch-plan-doc"
              className="ch-select"
              value={draft.plan_doc_path}
              onChange={(e) => update('plan_doc_path', e.target.value)}
            >
              <option value="">自动匹配（依据标题与续轮历史自动检索仓库方案）</option>
              {planDocs.map((doc) => (
                <option key={doc.relative_path} value={doc.relative_path}>
                  {doc.title} ({doc.relative_path}
                  {doc.unchecked_items > 0 ? ` · 待办 ${doc.unchecked_items}` : ''})
                </option>
              ))}
            </select>
          </div>

          <div className="ch-field">
            <label htmlFor="ch-title">派单标题 *</label>
            <input
              id="ch-title"
              className="ch-input"
              placeholder={inferredSpec?.suggested_title || '例如：核对 P3 计划、原文预览接口和现有页面入口'}
              value={draft.title}
              maxLength={180}
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
              <option value="">自动判断</option>
              <option value="implement_general">一般功能 / 报告下载 / 存储接入 (Sol Medium/High)</option>
              <option value="implement_transaction">事务缺陷修复与回归测试 (Sol High)</option>
              <option value="review_concurrency">并发 / 租约 / 跨存储 / 回退安全独立审查 (Astra High · 只读)</option>
              <option value="chore">日志整理 / 进度更新 / 机械修改 (Luna / Sol 回退)</option>
            </select>
          </div>

          <div className="ch-check-grid">
            {RISK_OPTIONS.map(([key, label]) => (
              <div className="ch-field" key={key}>
                <label htmlFor={`ch-${key}`}>{label}</label>
                <select
                  id={`ch-${key}`}
                  className="ch-select"
                  value={draft[key] === null ? '' : String(draft[key])}
                  onChange={(e) => update(key, e.target.value === '' ? null : e.target.value === 'true')}
                >
                  <option value="">自动判断</option>
                  <option value="true">是</option>
                  <option value="false">否</option>
                </select>
              </div>
            ))}
          </div>

          <div className="ch-field">
            <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 5 }}>
              <label htmlFor="ch-objective" style={{ marginBottom: 0 }}>
                目标与可验证完成条件（留空将在派单时按方案与行业标准自动推导）
              </label>
              <button
                type="button"
                className="ch-btn ch-btn-xs"
                disabled={inferring}
                onClick={onInferSpec}
              >
                <Sparkles size={11} />
                <span>{inferring ? '推导中...' : '重新按行业标准生成'}</span>
              </button>
            </div>
            <textarea
              id="ch-objective"
              rows={6}
              className="ch-textarea"
              placeholder="点击「智能生成规格」或直接派单，系统会自动依据方案、历史轨迹及行业软件功能模块完成标准生成完整目标与验收条目；也可在此直接微调。"
              value={draft.objective ?? inferredSpec?.objective ?? ''}
              onChange={(e) => update('objective', e.target.value)}
            />
          </div>

          <div className="ch-field">
            <label htmlFor="ch-allowed">
              允许创建或修改的文件（绑定任务包时可缩小范围）
            </label>
            <textarea
              id="ch-allowed"
              rows={4}
              className="ch-textarea"
              placeholder={draft.allowed_files_text === null
                ? '按任务包或方案推导可修改的文件'
                : draft.packet_path ? '已清空范围，本轮将使用只读沙箱' : '未指定文件范围'}
              value={draft.allowed_files_text ?? inferredSpec?.allowed_files.join('\n') ?? ''}
              onChange={(e) => update('allowed_files_text', e.target.value)}
            />
            {draft.allowed_files_text !== null && (
              <button type="button" className="ch-btn ch-btn-xs" onClick={() => update('allowed_files_text', null)}>
                恢复默认改动范围
              </button>
            )}
          </div>

          <div className="ch-field">
            <label htmlFor="ch-verify">
              验收命令（自动结合仓库 venv / pytest / 前端构建脚本推导，每行一条）
            </label>
            <textarea
              id="ch-verify"
              rows={3}
              className="ch-textarea"
              placeholder="留空时由系统根据命中模块自动生成可执行 pytest / typecheck / build 验收命令"
              value={draft.verification_text ?? inferredSpec?.verification_commands.join('\n') ?? ''}
              onChange={(e) => update('verification_text', e.target.value)}
            />
          </div>

          <label className="ch-check-card" style={{ width: 'fit-content' }}>
            <input
              type="checkbox"
              checked={draft.auto_infer}
              onChange={(e) => update('auto_infer', e.target.checked)}
            />
            <span>自动判断任务风险并补全未指定的规格</span>
          </label>

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
              disabled={submitting || inferring || Boolean(specError)}
              onClick={() => onSubmit(true)}
            >
              <Eye size={13} />
              <span>干跑预检 (Dry Run)</span>
            </button>
            <button
              type="button"
              className="ch-btn ch-btn-primary"
              disabled={submitting || inferring || Boolean(specError) || !codexAvailable}
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
