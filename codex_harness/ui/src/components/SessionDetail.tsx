import { useEffect, useMemo, useRef, useState } from 'react';
import {
  AlertTriangle,
  ChevronDown,
  ChevronRight,
  FileCode2,
  FileText,
  GitBranch,
  MessageSquarePlus,
  Plus,
  RefreshCw,
  ShieldAlert,
  ShieldCheck,
  Sparkles,
  Square,
} from 'lucide-react';
import type { SessionDetail as SessionDetailType, SessionTurn, TimelineItem } from '../types/harness';
import {
  formatCommandSeconds,
  formatDurationZh,
  formatShortTimestamp,
  shortPathLabel,
} from '../utils/format';
import { SafeMarkdown } from './SafeMarkdown';

interface SessionDetailProps {
  detail: SessionDetailType | null;
  loading: boolean;
  liveSync: boolean;
  onToggleLiveSync: () => void;
  autoFollow: boolean;
  onToggleAutoFollow: () => void;
  onManualRefresh: () => void;
  onOpenFile: (absPath: string) => void;
  onSelectSession: (sessionId: string) => void;
  onNewDispatch: () => void;
  onFollowUpDispatch: (detail: SessionDetailType) => void;
  onCancelRun: (runId: string) => void;
  delegationDocPath?: string | null;
}

const PREVIEW_HEAD_STEPS = 2;
const PREVIEW_TAIL_STEPS = 4;
const PREVIEW_THRESHOLD = PREVIEW_HEAD_STEPS + PREVIEW_TAIL_STEPS + 1;

/**
 * Antigravity 对话头像徽章（纯内联 SVG）
 */
function AntigravityAvatar() {
  return (
    <span className="ch-avatar ch-avatar-agy" aria-hidden="true">
      <svg width="13" height="13" viewBox="0 0 24 24" fill="none">
        <path
          d="M12 2.8l2.2 6.6 6.6 2.2-6.6 2.2-2.2 6.6-2.2-6.6-6.6-2.2 6.6-2.2L12 2.8z"
          fill="currentColor"
        />
      </svg>
    </span>
  );
}

/**
 * Codex 对话头像徽章（纯内联 SVG）
 */
function CodexAvatar() {
  return (
    <span className="ch-avatar ch-avatar-codex" aria-hidden="true">
      <svg width="14" height="14" viewBox="0 0 24 24" fill="none">
        <circle cx="12" cy="12" r="8.2" stroke="currentColor" strokeWidth="1.8" />
        <path
          d="M9.4 9.6L7 12l2.4 2.4M14.6 9.6L17 12l-2.4 2.4M12.8 8.4l-1.6 7.2"
          stroke="currentColor"
          strokeWidth="1.6"
          strokeLinecap="round"
          strokeLinejoin="round"
        />
      </svg>
    </span>
  );
}

/**
 * 安全渲染 Unified Diff（纯 React 节点，禁止 dangerouslySetInnerHTML）
 */
function SafeDiffView({ diff }: { diff: string }) {
  const lines = diff.split(/\r?\n/);
  return (
    <pre className="ch-cmd-output">
      {lines.map((line, index) => {
        let cls = '';
        if (line.startsWith('+') && !line.startsWith('+++')) cls = 'ch-diff-line-add';
        else if (line.startsWith('-') && !line.startsWith('---')) cls = 'ch-diff-line-del';
        else if (line.startsWith('@@')) cls = 'ch-diff-line-hunk';
        return (
          <span key={index} className={cls}>
            {line}
            {'\n'}
          </span>
        );
      })}
    </pre>
  );
}

function CommandRow({ item }: { item: TimelineItem }) {
  const [open, setOpen] = useState(false);
  const cmd = item.command;
  if (!cmd) return null;
  const isError = typeof cmd.exit_code === 'number' && cmd.exit_code !== 0;
  const durLabel = formatCommandSeconds(cmd.duration_ms);

  return (
    <div className="ch-cmd-card">
      <button type="button" className="ch-cmd-row" onClick={() => setOpen((v) => !v)}>
        <span className="ch-cmd-text" title={cmd.command}>
          <span className="ch-cmd-dollar">$</span> {cmd.command || '(command)'}
        </span>
        <span className={`ch-exit-pill ${isError ? 'is-err' : 'is-ok'}`}>
          {typeof cmd.exit_code === 'number' ? `退出 ${cmd.exit_code}` : cmd.status}
          {durLabel ? ` · ${durLabel}` : ''}
        </span>
      </button>
      {open && (
        <pre className="ch-cmd-output">{cmd.output_preview || '(无标准输出)'}</pre>
      )}
    </div>
  );
}

function FileChangeRow({
  item,
  onOpenFile,
}: {
  item: TimelineItem;
  onOpenFile: (absPath: string) => void;
}) {
  const [open, setOpen] = useState(false);
  const fc = item.file_change;
  if (!fc) return null;

  return (
    <div className="ch-cmd-card">
      <div className="ch-cmd-row" onClick={() => setOpen((v) => !v)} role="button" tabIndex={0}>
        <span className="ch-cmd-text" title={fc.path}>
          <FileCode2 size={12} style={{ verticalAlign: '-2px', marginRight: 5 }} />
          {shortPathLabel(fc.path)}
        </span>
        <span style={{ display: 'inline-flex', alignItems: 'center', gap: 6, flexShrink: 0 }}>
          <span className="ch-pill ch-pill-blue">{fc.change_type}</span>
          <button
            type="button"
            className="ch-btn ch-btn-xs"
            onClick={(e) => {
              e.stopPropagation();
              onOpenFile(fc.path);
            }}
          >
            打开
          </button>
        </span>
      </div>
      {open && fc.diff_preview && <SafeDiffView diff={fc.diff_preview} />}
    </div>
  );
}

function TimelineStepView({
  item,
  onOpenFile,
  onSelectSession,
}: {
  item: TimelineItem;
  onOpenFile: (absPath: string) => void;
  onSelectSession: (sessionId: string) => void;
}) {
  if (item.kind === 'narration' && item.text) {
    return (
      <div className="ch-narration-box">
        <SafeMarkdown text={item.text} />
      </div>
    );
  }
  if (item.kind === 'reasoning' && item.text) {
    return (
      <div className="ch-reasoning-row">
        <Sparkles size={11} />
        <span>{item.text}</span>
      </div>
    );
  }
  if (item.kind === 'command') {
    return <CommandRow item={item} />;
  }
  if (item.kind === 'file_change') {
    return <FileChangeRow item={item} onOpenFile={onOpenFile} />;
  }
  if (item.kind === 'subagent' && item.subagent) {
    const sub = item.subagent;
    return (
      <div className="ch-cmd-card">
        <div className="ch-cmd-row" style={{ cursor: 'default' }}>
          <span className="ch-cmd-text">
            <GitBranch size={12} style={{ verticalAlign: '-2px', marginRight: 5 }} />
            子代理 ({sub.kind}): {sub.agent_path || sub.agent_thread_id || 'subagent'}
            {sub.model ? ` · ${sub.model}` : ''}
          </span>
          {sub.agent_thread_id && (
            <button
              type="button"
              className="ch-btn ch-btn-xs"
              onClick={() => onSelectSession(sub.agent_thread_id!)}
            >
              跳转
            </button>
          )}
        </div>
      </div>
    );
  }
  if (item.kind === 'error' && item.text) {
    return (
      <div className="ch-inline-err">
        <AlertTriangle size={12} style={{ flexShrink: 0, marginTop: 2 }} />
        <span>{item.text}</span>
      </div>
    );
  }
  return null;
}

interface DialogueTurnProps {
  turn: SessionTurn;
  isLatestTurn: boolean;
  isHarnessSession: boolean;
  onOpenFile: (absPath: string) => void;
  onSelectSession: (sessionId: string) => void;
}

/**
 * 单轮 Antigravity（右侧派单气泡）↔ Codex（左侧执行进度卡 + 对话回复气泡）
 */
function DialogueTurn({
  turn,
  isLatestTurn,
  isHarnessSession,
  onOpenFile,
  onSelectSession,
}: DialogueTurnProps) {
  const isRunning = turn.status === 'running';
  const isFailed = turn.status === 'failed';
  const isCancelled = turn.status === 'cancelled';

  const [promptExpanded, setPromptExpanded] = useState(false);
  // 仅最新一轮或运行中轮次默认展开精简进度，历史轮次默认收起为摘要条，突出对话主线
  const [progressExpanded, setProgressExpanded] = useState(isLatestTurn || isRunning);
  const [showAllSteps, setShowAllSteps] = useState(false);
  const [showReasoning, setShowReasoning] = useState(false);

  // 当轮次变为最新或进入运行态时自动展开进度卡
  useEffect(() => {
    if (isRunning) {
      setProgressExpanded(true);
    }
  }, [isRunning]);

  const { primarySteps, reasoningCount, replyText, replyIsFallbackNarration } = useMemo(() => {
    const nonFinal = turn.timeline.filter((item) => item.kind !== 'final_answer');
    const rCount = nonFinal.filter((item) => item.kind === 'reasoning').length;
    const filtered = nonFinal.filter((item) => {
      if (item.kind === 'reasoning' && !showReasoning) return false;
      if (item.kind === 'subagent' && item.subagent?.kind === 'interacted' && item.subagent.agent_path === '/root') {
        return false;
      }
      return true;
    });

    // 若缺少显式 final_answer，在非运行态提取最后一条 Codex 叙述作为对话回复气泡，保证双边对话完整
    let reply = turn.final_answer?.trim() || '';
    let isFallback = false;
    if (!reply && !isRunning) {
      const narrations = nonFinal.filter((item) => item.kind === 'narration' && item.text?.trim());
      if (narrations.length > 0) {
        reply = narrations[narrations.length - 1].text!.trim();
        isFallback = true;
      }
    }
    return {
      primarySteps: filtered,
      reasoningCount: rCount,
      replyText: reply,
      replyIsFallbackNarration: isFallback,
    };
  }, [turn.timeline, turn.final_answer, showReasoning, isRunning]);

  const promptLines = turn.prompt ? turn.prompt.split(/\r?\n/) : [];
  const shouldCollapsePrompt = promptLines.length > 8;
  const visiblePrompt =
    shouldCollapsePrompt && !promptExpanded
      ? promptLines.slice(0, 8).join('\n')
      : turn.prompt;

  const statusText = isRunning
    ? '正在执行'
    : isFailed
      ? '执行中断'
      : isCancelled
        ? '已中止'
        : '已完成执行';
  const statusDot = isRunning
    ? 'ch-dot-blue'
    : isFailed || isCancelled
      ? 'ch-dot-red'
      : 'ch-dot-green';

  const durationText = formatDurationZh(turn.duration_ms);
  const senderDisplayName =
    isHarnessSession || turn.source_label === 'Antigravity' ? 'Antigravity' : turn.source_label || 'Antigravity';
  const senderModelLabel = turn.source_model || (isHarnessSession ? 'gemini-4-neon-high' : null);

  // 对超长步骤做首尾智能预览，避免 40+ 条命令淹没整屏对话
  const shouldTruncateSteps = !showAllSteps && primarySteps.length >= PREVIEW_THRESHOLD;
  const headSteps = shouldTruncateSteps ? primarySteps.slice(0, PREVIEW_HEAD_STEPS) : primarySteps;
  const tailSteps = shouldTruncateSteps ? primarySteps.slice(-PREVIEW_TAIL_STEPS) : [];
  const hiddenStepCount = shouldTruncateSteps
    ? primarySteps.length - PREVIEW_HEAD_STEPS - PREVIEW_TAIL_STEPS
    : 0;

  return (
    <section className="ch-dialogue-turn">
      {/* === 1. 右侧：Antigravity 派单对话气泡 === */}
      <div className="ch-msg-col ch-msg-right">
        <div className="ch-msg-meta ch-msg-meta-right">
          {turn.started_at && <span>{formatShortTimestamp(turn.started_at)}</span>}
          <span className="ch-pill ch-pill-blue">第{turn.turn_index}轮派单</span>
          {turn.packet_id && <span className="ch-pill ch-pill-amber">包 {turn.packet_id}</span>}
          {turn.sandbox && <span className="ch-pill ch-pill-neutral">{turn.sandbox}</span>}
          {senderModelLabel && <span className="ch-meta-model">{senderModelLabel}</span>}
          <strong className="ch-meta-actor">{senderDisplayName}</strong>
          <AntigravityAvatar />
        </div>

        <div className="ch-bubble ch-bubble-antigravity">
          {turn.allowed_files.length > 0 && (
            <div className="ch-dispatch-scopes">
              <span className="ch-pill ch-pill-amber">STRICT SCOPE</span>
              {turn.allowed_files.slice(0, 6).map((file) => (
                <span key={file} className="ch-pill ch-pill-neutral">
                  {file}
                </span>
              ))}
              {turn.allowed_files.length > 6 && (
                <span className="ch-pill ch-pill-neutral">
                  +{turn.allowed_files.length - 6} 项
                </span>
              )}
            </div>
          )}

          {turn.prompt ? (
            <SafeMarkdown text={visiblePrompt} className="ch-prompt-md" />
          ) : (
            <div className="ch-muted-note">（本轮无明文派单内容）</div>
          )}

          {shouldCollapsePrompt && (
            <button
              type="button"
              className="ch-expand-link"
              onClick={() => setPromptExpanded((v) => !v)}
            >
              {promptExpanded ? <ChevronDown size={13} /> : <ChevronRight size={13} />}
              <span>
                {promptExpanded
                  ? '收起派单内容'
                  : `展开完整派单内容 (共 ${turn.prompt_line_count || promptLines.length} 行)`}
              </span>
            </button>
          )}
        </div>
      </div>

      {/* === 2. 左侧：Codex 执行进度卡 + 对话答复气泡 === */}
      <div className="ch-msg-col ch-msg-left">
        <div className="ch-msg-meta ch-msg-meta-left">
          <CodexAvatar />
          <strong className="ch-meta-actor">Codex</strong>
          <span className="ch-meta-model">
            {turn.target_model || 'codex'}
            {turn.target_effort ? ` · ${turn.target_effort}` : ''}
            {turn.target_role ? ` (${turn.target_role})` : ''}
          </span>

          {turn.scope_audit && turn.scope_audit.status !== 'unchecked' && (
            <span
              className={`ch-pill ${
                turn.scope_audit.status === 'violation'
                  ? 'ch-pill-red'
                  : turn.scope_audit.status === 'warning'
                    ? 'ch-pill-amber'
                    : 'ch-pill-green'
              }`}
            >
              {turn.scope_audit.status === 'violation' ? (
                <ShieldAlert size={11} />
              ) : (
                <ShieldCheck size={11} />
              )}
              <span>
                {turn.scope_audit.status === 'violation'
                  ? '范围门禁越界'
                  : turn.scope_audit.status === 'warning'
                    ? '未设白名单'
                    : '范围门禁通过'}
              </span>
            </span>
          )}
        </div>

        {/* 2a. 嵌入在 Codex 气泡列内的紧凑执行进度折叠卡（匹配第一张参考图） */}
        {(primarySteps.length > 0 || turn.command_count > 0 || turn.file_count > 0 || isRunning) && (
          <div className="ch-progress-card">
            <div
              className="ch-progress-bar"
              onClick={() => setProgressExpanded((v) => !v)}
              role="button"
              tabIndex={0}
            >
              <span className="ch-progress-bar-left">
                <span className={`ch-dot ${statusDot}`} />
                <span>
                  {statusText}
                  {' · '}
                  {turn.command_count} 条命令
                  {' · '}
                  {turn.file_count} 个文件
                  {durationText ? ` · ${durationText}` : ''}
                </span>
              </span>

              <span className="ch-progress-bar-right">
                {progressExpanded && reasoningCount > 0 && (
                  <button
                    type="button"
                    className={`ch-mini-chip ${showReasoning ? 'is-on' : ''}`}
                    onClick={(e) => {
                      e.stopPropagation();
                      setShowReasoning((v) => !v);
                    }}
                    title="切换是否显示内部思考摘要"
                  >
                    思考 ({reasoningCount})
                  </button>
                )}
                {progressExpanded ? <ChevronDown size={14} /> : <ChevronRight size={14} />}
              </span>
            </div>

            {progressExpanded && (
              <div className="ch-progress-body">
                {primarySteps.length === 0 ? (
                  <div className="ch-muted-note">
                    {isRunning ? '正在等待 Codex 输出执行步骤...' : '本轮无命令执行步骤。'}
                  </div>
                ) : (
                  <>
                    {headSteps.map((item) => (
                      <TimelineStepView
                        key={item.id}
                        item={item}
                        onOpenFile={onOpenFile}
                        onSelectSession={onSelectSession}
                      />
                    ))}

                    {shouldTruncateSteps && (
                      <button
                        type="button"
                        className="ch-fold-divider"
                        onClick={() => setShowAllSteps(true)}
                      >
                        <ChevronDown size={12} />
                        <span>展开中间省略的 {hiddenStepCount} 条执行步骤</span>
                      </button>
                    )}

                    {tailSteps.map((item) => (
                      <TimelineStepView
                        key={item.id}
                        item={item}
                        onOpenFile={onOpenFile}
                        onSelectSession={onSelectSession}
                      />
                    ))}

                    {showAllSteps && primarySteps.length >= PREVIEW_THRESHOLD && (
                      <button
                        type="button"
                        className="ch-fold-divider"
                        onClick={() => setShowAllSteps(false)}
                      >
                        <ChevronRight size={12} />
                        <span>收起为精简预览（共 {primarySteps.length} 步）</span>
                      </button>
                    )}
                  </>
                )}
              </div>
            )}
          </div>
        )}

        {/* 2b. Codex 正式对话答复气泡 */}
        {replyText && (
          <div className="ch-bubble ch-bubble-codex">
            {replyIsFallbackNarration && (
              <div className="ch-reply-kicker">阶段进展回报</div>
            )}
            <SafeMarkdown text={replyText} />
          </div>
        )}

        {/* 2c. 范围越界或执行异常内联提示（保持在左侧 Codex 对话流内） */}
        {turn.scope_audit && turn.scope_audit.status === 'violation' && (
          <div className="ch-bubble-alert">
            <AlertTriangle size={13} style={{ flexShrink: 0, marginTop: 2 }} />
            <div>
              <strong>{turn.scope_audit.summary}</strong>
              {turn.scope_audit.out_of_scope_files.length > 0 && (
                <div style={{ marginTop: 3 }}>
                  白名单外文件：{turn.scope_audit.out_of_scope_files.join(', ')}
                </div>
              )}
              {turn.scope_audit.forbidden_hits.length > 0 && (
                <div style={{ marginTop: 3 }}>
                  命中禁止/敏感路径：{turn.scope_audit.forbidden_hits.join(', ')}
                </div>
              )}
            </div>
          </div>
        )}

        {turn.error_message && (
          <div className="ch-bubble-alert">
            <AlertTriangle size={13} style={{ flexShrink: 0, marginTop: 2 }} />
            <div>
              <strong>Codex 中断提示：</strong>
              {turn.error_message}
            </div>
          </div>
        )}
      </div>
    </section>
  );
}

/**
 * 右侧 Antigravity ↔ Codex 对话式单向实时监控主舞台
 */
export function SessionDetail({
  detail,
  loading,
  liveSync,
  onToggleLiveSync,
  autoFollow,
  onToggleAutoFollow,
  onManualRefresh,
  onOpenFile,
  onSelectSession,
  onNewDispatch,
  onFollowUpDispatch,
  onCancelRun,
  delegationDocPath,
}: SessionDetailProps) {
  const scrollRef = useRef<HTMLDivElement | null>(null);
  const lastTurnRef = useRef<HTMLDivElement | null>(null);
  const prevSessionIdRef = useRef<string | null>(null);

  const sessionId = detail?.summary.session_id ?? null;
  const turns = detail?.turns ?? [];
  const lastTurn = turns.length > 0 ? turns[turns.length - 1] : null;
  const isRunning = detail?.summary.status === 'running' || lastTurn?.status === 'running';
  const liveEventKey = `${sessionId}-${turns.length}-${lastTurn?.timeline.length ?? 0}-${lastTurn?.status ?? ''}`;

  // 切换会话时聚焦到最新一轮的 Antigravity 气泡顶部；仅在运行中流式更新且开启自动跟随时滚动到底部
  useEffect(() => {
    if (!scrollRef.current || !sessionId) return;
    const switchedSession = prevSessionIdRef.current !== sessionId;
    prevSessionIdRef.current = sessionId;

    if (switchedSession) {
      if (lastTurnRef.current) {
        lastTurnRef.current.scrollIntoView({ block: 'start', behavior: 'auto' });
      } else {
        scrollRef.current.scrollTop = 0;
      }
      return;
    }

    if (autoFollow && isRunning) {
      scrollRef.current.scrollTop = scrollRef.current.scrollHeight;
    }
  }, [sessionId, autoFollow, isRunning, liveEventKey]);

  const docPath = detail?.delegation_doc_path || delegationDocPath;
  const isHarness = detail?.summary.source_kind === 'harness';

  return (
    <div className="ch-main">
      <header className="ch-topbar">
        <div className="ch-topbar-left">
          <div className="ch-topbar-title">
            {detail ? detail.summary.project_name : 'Codex Monitor'}
          </div>
          <div className="ch-topbar-sub">
            {detail
              ? `${isHarness ? 'Antigravity → Codex' : detail.summary.originator || 'Antigravity → Codex'} · ${
                  detail.summary.model || 'codex'
                }${detail.summary.effort ? ` · ${detail.summary.effort}` : ''} · ${
                  detail.summary.turn_count || turns.length
                }轮`
              : 'Antigravity × Codex 对话式单向实时监控'}
          </div>
        </div>

        <div className="ch-topbar-controls">
          <button
            type="button"
            className="ch-sync-pill"
            onClick={onToggleLiveSync}
            title="点击切换实时轮询"
          >
            <span className={`ch-dot ${liveSync ? 'ch-dot-green' : 'ch-dot-muted'}`} />
            <span>{liveSync ? '实时同步' : '已暂停'}</span>
          </button>

          <label className="ch-toggle-label">
            <button
              type="button"
              role="switch"
              aria-checked={autoFollow}
              className={`ch-switch ${autoFollow ? 'is-on' : ''}`}
              onClick={onToggleAutoFollow}
            >
              <span className="ch-switch-thumb" />
            </button>
            <span>自动跟随</span>
          </label>

          {docPath && (
            <button
              type="button"
              className="ch-btn ch-btn-xs"
              onClick={() => onOpenFile(docPath)}
              title="在 Antigravity 侧边栏打开 codex-delegation.md"
            >
              <FileText size={11} />
              <span>契约</span>
            </button>
          )}

          <button
            type="button"
            className="ch-btn ch-btn-xs"
            onClick={onManualRefresh}
            title="立即刷新"
          >
            <RefreshCw size={12} />
          </button>

          {detail?.summary.status === 'running' && detail.summary.run_id && (
            <button
              type="button"
              className="ch-btn ch-btn-xs ch-btn-danger"
              onClick={() => onCancelRun(detail.summary.run_id!)}
            >
              <Square size={10} />
              <span>中止</span>
            </button>
          )}

          <button type="button" className="ch-btn ch-btn-xs ch-btn-primary" onClick={onNewDispatch}>
            <Plus size={12} />
            <span>派单</span>
          </button>
        </div>
      </header>

      <div ref={scrollRef} className="ch-content-scroll ch-chat-canvas">
        {loading && !detail ? (
          <div className="ch-empty">正在同步 Antigravity × Codex 对话轨迹...</div>
        ) : !detail ? (
          <div className="ch-empty">
            <CodexAvatar />
            <div>从左侧选择一个会话查看 Antigravity 与 Codex 的对话与执行进度，或点击右上角「派单」。</div>
          </div>
        ) : turns.length === 0 ? (
          <div className="ch-empty">该会话暂无可展示的对话轮次。</div>
        ) : (
          <div className="ch-chat-stream">
            {turns.map((turn, idx) => {
              const isLast = idx === turns.length - 1;
              return (
                <div
                  key={`${detail.summary.session_id}-turn-${turn.turn_index}`}
                  ref={isLast ? lastTurnRef : undefined}
                >
                  <DialogueTurn
                    turn={turn}
                    isLatestTurn={isLast}
                    isHarnessSession={isHarness}
                    onOpenFile={onOpenFile}
                    onSelectSession={onSelectSession}
                  />
                </div>
              );
            })}

            <div className="ch-followup-strip">
              <button
                type="button"
                className="ch-btn ch-btn-xs"
                onClick={() => onFollowUpDispatch(detail)}
              >
                <MessageSquarePlus size={12} />
                <span>基于此会话发起下一轮 Antigravity → Codex 派单</span>
              </button>
            </div>
          </div>
        )}
      </div>
    </div>
  );
}
