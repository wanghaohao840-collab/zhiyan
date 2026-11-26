import { ArrowDown, ArrowUp, Check, CheckCircle2, Copy, Plus } from "lucide-react";
import { type PropsWithChildren, useEffect, useRef, useState } from "react";
import { Button } from "../Button/Button";
import { MarkdownPreview } from "../MarkdownPreview/MarkdownPreview";
import { ConstellationMark, OverlayDialog, useToast } from "../ui";
import { qaStages, type QaLiveAnswer } from "../../features/qa/stream";
import type { QaConversation, QaJob, QaMessage, QaMode, QaSource } from "../../features/qa/types";

type OverlayProps = PropsWithChildren<{
  title: string;
  className?: string;
  onClose: () => void;
  returnFocusTo?: HTMLElement | null;
}>;
export function QaOverlay({ title, className = "", onClose, returnFocusTo, children }: OverlayProps) {
  return (
    <OverlayDialog
      open
      title={title}
      closeLabel={`关闭${title}`}
      layerClassName="qa-overlay"
      scrimClassName="qa-overlay__scrim"
      panelClassName={`qa-overlay__panel ${className}`.trim()}
      returnFocusTo={returnFocusTo}
      onClose={onClose}
    >
      {children}
    </OverlayDialog>
  );
}

type ConversationListProps = {
  items: QaConversation[];
  selectedId?: string;
  onSelect: (id: string) => void;
  onNew: () => void;
  onDeleteSelected?: () => void;
  hasMore?: boolean;
  loadingMore?: boolean;
  onLoadMore?: () => void;
};

export function ConversationList({
  items,
  selectedId,
  onSelect,
  onNew,
  onDeleteSelected,
  hasMore,
  loadingMore,
  onLoadMore,
}: ConversationListProps) {
  return (
    <aside className="qa-conversations" aria-label="对话列表">
      <div className="qa-panel-heading">
        <h2>学习线程</h2>
        <button className="qa-icon-button" aria-label="新建对话" onClick={onNew}>
          <Plus size={18} strokeWidth={1.6} aria-hidden="true" />
        </button>
      </div>
      {items.length ? (
        <ol>
          {items.map((item) => (
            <li key={item.conversation_id}>
              <button
                className="qa-conversation"
                aria-current={selectedId === item.conversation_id ? "page" : undefined}
                onClick={() => onSelect(item.conversation_id)}
              >
                <strong>{item.title}</strong>
                <small>{item.documents.map((doc) => doc.document_name).join("、")}</small>
              </button>
            </li>
          ))}
        </ol>
      ) : (
        <p className="qa-muted">还没有对话</p>
      )}
      {hasMore && onLoadMore ? (
        <Button hierarchy="secondary" disabled={loadingMore} onClick={onLoadMore}>
          {loadingMore ? "正在加载更多对话…" : "加载更多对话"}
        </Button>
      ) : null}
      {onDeleteSelected ? (
        <Button hierarchy="danger" onClick={onDeleteSelected}>
          删除当前对话
        </Button>
      ) : null}
    </aside>
  );
}

type MessageListProps = {
  messages: QaMessage[];
  liveAnswer?: QaLiveAnswer;
  onSources: (message: QaMessage, trigger: HTMLButtonElement, citationId?: string) => void;
  onRetry: (message: QaMessage) => void;
  activeCitationId?: string;
  busy?: boolean;
  hasOlder?: boolean;
  loadingOlder?: boolean;
  onLoadOlder?: () => void;
  notesEnabled?: boolean;
};

function noteDraftHref(kind: "qa_answer" | "qa_citation", messageId: string, citationId?: string) {
  const params = new URLSearchParams({ source_kind: kind, qa_message_id: messageId });
  if (citationId) params.set("citation_id", citationId);
  return `/notes?${params.toString()}`;
}

type StepState = "done" | "active" | "idle" | "error";

interface ProgressStepItem {
  label: string;
  state: StepState;
}

/**
 * 基于真实服务端消息/任务状态的横向 4 步胶囊流程条（不编造未由后端返回的逐字思考日志）。
 */
function QaStepTrack({ steps, ariaLabel }: { steps: ProgressStepItem[]; ariaLabel: string }) {
  return (
    <ol className="qa-step-track" aria-label={ariaLabel}>
      {steps.map((step, index) => (
        <li
          key={step.label}
          className={`qa-step-track__item qa-step-track__item--${step.state}`}
          aria-current={step.state === "active" ? "step" : undefined}
        >
          <span className="qa-step-track__badge tabular-nums" aria-hidden="true">
            {index + 1}
          </span>
          <span className="qa-step-track__label">{step.label}</span>
          {index < steps.length - 1 ? (
            <span className="qa-step-track__arrow" aria-hidden="true">
              →
            </span>
          ) : null}
        </li>
      ))}
    </ol>
  );
}

function buildLiveMessageSteps(message: QaMessage, live: QaLiveAnswer): ProgressStepItem[] {
  const labels = {
    retrieving: live.retrievedCount === undefined
      ? "文档检索"
      : `文档检索（${live.retrievedCount} 条）`,
    reranking: "相关性重排",
    verifying: "证据核验",
    generating: "生成回答",
  };
  return qaStages.map((stage) => {
    const label = labels[stage];
    if (!live.stages.includes(stage)) return { label, state: "idle" };
    if (stage === live.currentStage) {
      if (message.status === "failed" || message.status === "cancelled") {
        return { label, state: "error" };
      }
      if (message.status === "pending" && live.status !== "done") {
        return { label, state: live.status === "receiving" ? "active" : "idle" };
      }
    }
    return { label, state: "done" };
  });
}

function buildPersistedMessageSteps(message: QaMessage): ProgressStepItem[] {
  if (message.status === "pending") {
    return [
      { label: "接收问题", state: "done" },
      { label: "文档检索", state: "active" },
      { label: "引用整理", state: "idle" },
      { label: "生成回答", state: "idle" },
    ];
  }
  if (message.status === "failed" || message.status === "cancelled") {
    return [
      { label: "接收问题", state: "done" },
      { label: "文档检索", state: "error" },
      { label: "引用整理", state: "idle" },
      { label: "生成回答", state: "idle" },
    ];
  }
  const hasSources = message.sources.length > 0;
  return [
    { label: "接收问题", state: "done" },
    { label: "文档检索", state: "done" },
    { label: hasSources ? `证据引用 (${message.sources.length})` : "证据核对", state: "done" },
    { label: "生成回答", state: "done" },
  ];
}

function buildSummarySteps(job: QaJob): ProgressStepItem[] {
  if (job.status === "completed") {
    return [
      { label: "任务排队", state: "done" },
      { label: "聚合对话", state: "done" },
      { label: "提炼摘要", state: "done" },
      { label: "保存结果", state: "done" },
    ];
  }
  if (job.status === "failed" || job.status === "cancelled") {
    return [
      { label: "任务排队", state: "done" },
      { label: "聚合对话", state: "error" },
      { label: "提炼摘要", state: "idle" },
      { label: "保存结果", state: "idle" },
    ];
  }
  const progress = job.progress;
  const activeIndex = job.status === "queued" ? 0 : progress < 40 ? 1 : progress < 80 ? 2 : 3;
  const labels = ["任务排队", "聚合对话", "提炼摘要", "保存结果"];
  return labels.map((label, index) => ({
    label,
    state: index < activeIndex ? "done" : index === activeIndex ? "active" : "idle",
  }));
}

export function MessageList({
  messages,
  liveAnswer,
  onSources,
  onRetry,
  activeCitationId,
  busy,
  hasOlder,
  loadingOlder,
  onLoadOlder,
  notesEnabled = true,
}: MessageListProps) {
  const viewportRef = useRef<HTMLDivElement>(null);
  const stickToBottomRef = useRef(true);
  const [showScrollBottom, setShowScrollBottom] = useState(false);

  const displayMessages = messages.map((message) =>
    message.message_id === liveAnswer?.messageId && message.status === "pending" && liveAnswer.content
      ? { ...message, content: liveAnswer.content, sources: liveAnswer.sources }
      : message,
  );
  const lastMessage = displayMessages.at(-1);
  const lastMessageKey = lastMessage
    ? `${lastMessage.message_id}:${lastMessage.status}:${lastMessage.content.length}`
    : "";

  useEffect(() => {
    const el = viewportRef.current;
    if (!el || !stickToBottomRef.current) return;
    if (typeof el.scrollTo === "function") {
      el.scrollTo({ top: el.scrollHeight, behavior: "smooth" });
    } else {
      el.scrollTop = el.scrollHeight;
    }
  }, [lastMessageKey]);

  function handleScroll() {
    const el = viewportRef.current;
    if (!el) return;
    const distanceFromBottom = el.scrollHeight - el.scrollTop - el.clientHeight;
    const atBottom = distanceFromBottom <= 72;
    stickToBottomRef.current = atBottom;
    setShowScrollBottom(!atBottom);
  }

  function scrollToBottom() {
    const el = viewportRef.current;
    if (!el) return;
    stickToBottomRef.current = true;
    setShowScrollBottom(false);
    if (typeof el.scrollTo === "function") {
      el.scrollTo({ top: el.scrollHeight, behavior: "smooth" });
    } else {
      el.scrollTop = el.scrollHeight;
    }
  }

  if (!messages.length) {
    return (
      <div className="qa-welcome">
        <ConstellationMark size="sm" />
        <h2>从一个问题，开始理解</h2>
        <p>提出问题，知研会用当前对话绑定的文档回答，并保留引用。</p>
      </div>
    );
  }
  return (
    <div className="qa-messages-shell">
      <div ref={viewportRef} className="qa-messages-viewport" onScroll={handleScroll}>
        {hasOlder && onLoadOlder ? (
          <div className="qa-older-action">
            <Button hierarchy="secondary" disabled={loadingOlder} onClick={onLoadOlder}>
              {loadingOlder ? "正在加载更早消息…" : "加载更早消息"}
            </Button>
          </div>
        ) : null}
        <ol className="qa-messages" aria-label="问答消息">
          {displayMessages.map((message) => (
            <li
              key={message.message_id}
              className={`qa-message qa-message--${message.role}`}
              data-status={message.status}
            >
              <span className="qa-message__role">{message.role === "user" ? "你" : "知研"}</span>
              {message.role === "assistant" && message.status === "completed" ? (
                <div className="qa-answer-status">
                  <CheckCircle2 size={14} strokeWidth={1.6} aria-hidden="true" />
                  <span>
                    {message.sources.length
                      ? `已完成文档检索与组织 · 参考来源 ${message.sources.length} 条`
                      : "已完成回答"}
                  </span>
                </div>
              ) : null}
              {message.status === "pending" && !message.content ? (
                <p className="qa-message__pending" role="status">
                  正在查找并组织答案…
                </p>
              ) : message.role === "assistant" && message.content ? (
                <div className="qa-message__body">
                  <MarkdownPreview
                    markdown={message.content}
                    citations={message.sources.map((source, index) => ({
                      index: index + 1,
                      citationId: source.citation_id,
                      label: source.document_name,
                    }))}
                    onCitationClick={(citationId, trigger) =>
                      onSources(message, trigger, citationId)
                    }
                  />
                </div>
              ) : (
                <p>
                  {message.content ||
                    (message.status === "failed" ? "回答失败，原问题已保留。" : "")}
                </p>
              )}
              {message.role === "assistant" ? (
                <QaStepTrack
                  steps={
                    message.status === "pending" &&
                    liveAnswer?.messageId === message.message_id &&
                    liveAnswer.stages.length > 0
                      ? buildLiveMessageSteps(message, liveAnswer)
                      : buildPersistedMessageSteps(message)
                  }
                  ariaLabel="回答处理阶段"
                />
              ) : null}
              {message.status === "failed" ? (
                <div className="qa-message__failure" role="alert">
                  <span>{message.safe_error_code ?? "QA_OPERATION_FAILED"}</span>
                  <Button hierarchy="secondary" disabled={busy} onClick={() => onRetry(message)}>
                    重试回答
                  </Button>
                </div>
              ) : null}
              {message.role === "assistant" && message.sources.length ? (
                <div className="qa-citation-bar" aria-label="来源引用">
                  <button
                    type="button"
                    className="qa-source-trigger"
                    onClick={(event) => onSources(message, event.currentTarget)}
                  >
                    引用 {message.sources.length}
                  </button>
                  {message.sources.map((source, index) => (
                    <button
                      key={source.citation_id}
                      type="button"
                      className={`qa-citation-chip${activeCitationId === source.citation_id ? " is-active" : ""}`}
                      onClick={(event) =>
                        onSources(message, event.currentTarget, source.citation_id)
                      }
                      title={[
                        source.document_name,
                        source.page_number ? `第 ${source.page_number} 页` : null,
                        source.section,
                      ]
                        .filter(Boolean)
                        .join(" · ")}
                    >
                      <span className="qa-citation-chip__num">[{index + 1}]</span>
                      <span className="qa-citation-chip__name">{source.document_name}</span>
                      {source.page_number ? (
                        <span className="qa-citation-chip__page">P.{source.page_number}</span>
                      ) : null}
                    </button>
                  ))}
                </div>
              ) : null}
              {notesEnabled && message.role === "assistant" && message.status === "completed" ? (
                <div className="qa-message__note-actions">
                  <a
                    className="qa-note-action"
                    href={noteDraftHref("qa_answer", message.message_id)}
                  >
                    记为笔记
                  </a>
                </div>
              ) : null}
            </li>
          ))}
        </ol>
      </div>
      {showScrollBottom ? (
        <button
          type="button"
          className="qa-scroll-bottom"
          aria-label="回到底部查看最新回答"
          onClick={scrollToBottom}
        >
          <ArrowDown size={14} strokeWidth={1.8} aria-hidden="true" />
          <span>回到底部</span>
        </button>
      ) : null}
    </div>
  );
}

export function QaComposer({
  busy,
  documentCount,
  onSubmit,
}: {
  busy: boolean;
  documentCount: number;
  onSubmit: (question: string, mode: QaMode) => void;
}) {
  const text = useRef<HTMLTextAreaElement>(null);
  const mode = useRef<HTMLSelectElement>(null);

  function autoResize() {
    const el = text.current;
    if (!el) return;
    el.style.height = "auto";
    el.style.height = `${Math.min(180, Math.max(52, el.scrollHeight))}px`;
  }

  function submit() {
    const value = text.current?.value.trim() ?? "";
    if (!value) return;
    onSubmit(value, (mode.current?.value ?? "auto") as QaMode);
    if (text.current) {
      text.current.value = "";
      text.current.style.height = "";
    }
  }

  return (
    <div className="qa-composer">
      <label htmlFor="qa-question">向这些文档提问</label>
      <textarea
        ref={text}
        id="qa-question"
        maxLength={20000}
        rows={2}
        placeholder="输入问题，按 Ctrl + Enter 发送…"
        disabled={busy}
        onInput={autoResize}
        onKeyDown={(event) => {
          if (
            event.key === "Enter" &&
            (event.ctrlKey || event.metaKey) &&
            !event.nativeEvent.isComposing
          ) {
            event.preventDefault();
            submit();
          }
        }}
      />
      <div className="qa-composer__controls">
        <select ref={mode} aria-label="回答模式" defaultValue="auto" disabled={busy}>
          <option value="auto">自动</option>
          <option value="joint">联合分析</option>
          <option value="compare" disabled={documentCount < 2}>
            对比（需至少两篇文档）
          </option>
        </select>
        <Button loading={busy} onClick={submit}>
          {busy ? null : <ArrowUp size={15} strokeWidth={1.6} aria-hidden="true" />}
          发送
        </Button>
      </div>
    </div>
  );
}

export function SourcePanel({
  sources,
  activeCitationId,
  messageId,
  notesEnabled = false,
}: {
  sources: QaSource[];
  activeCitationId?: string;
  messageId?: string;
  notesEnabled?: boolean;
}) {
  const toast = useToast();
  const listRef = useRef<HTMLOListElement>(null);
  const [feedback, setFeedback] = useState("");
  const [copiedId, setCopiedId] = useState<string | null>(null);
  const [pulsingId, setPulsingId] = useState<string | null>(null);

  useEffect(() => {
    if (!activeCitationId || !listRef.current) return;
    const escaped =
      typeof CSS !== "undefined" && typeof CSS.escape === "function"
        ? CSS.escape(activeCitationId)
        : activeCitationId.replace(/"/g, '\\"');
    const card = listRef.current.querySelector<HTMLElement>(`li[data-citation-id="${escaped}"]`);
    if (card) {
      card.scrollIntoView?.({ behavior: "smooth", block: "nearest" });
      setPulsingId(activeCitationId);
      const timer = window.setTimeout(() => setPulsingId(null), 900);
      return () => window.clearTimeout(timer);
    }
    return undefined;
  }, [activeCitationId]);

  async function copyReference(source: QaSource, index: number) {
    try {
      if (!navigator.clipboard?.writeText) throw new Error("clipboard unavailable");
      await navigator.clipboard.writeText(source.reference);
      const msg = `引用 ${index + 1} 已复制`;
      setFeedback(msg);
      setCopiedId(source.citation_id);
      toast.push({ message: `已复制引用 ${index + 1} 到剪贴板`, tone: "success" });
      window.setTimeout(() => setCopiedId((id) => (id === source.citation_id ? null : id)), 1600);
    } catch {
      const msg = "复制失败，请手动选择引用文本";
      setFeedback(msg);
      toast.push({ message: msg, tone: "danger" });
    }
  }

  return (
    <aside className="qa-sources" aria-label="引用来源">
      <div className="qa-sources__header">
        <h2>引用证据</h2>
        {sources.length ? (
          <span className="qa-sources__count tabular-nums">{sources.length} 条</span>
        ) : null}
      </div>
      {sources.length ? (
        <ol ref={listRef}>
          {sources.map((source, index) => {
            const isCopied = copiedId === source.citation_id;
            const itemClasses = [
              activeCitationId === source.citation_id ? "is-active" : "",
              pulsingId === source.citation_id ? "is-pulsing" : "",
            ]
              .filter(Boolean)
              .join(" ");
            return (
              <li
                key={source.citation_id}
                data-citation-id={source.citation_id}
                className={itemClasses || undefined}
              >
                <div className="qa-source-card__top">
                  <span className="qa-source-index tabular-nums">{index + 1}</span>
                  <small className="qa-citation-id">{source.citation_id}</small>
                </div>
                <div className="qa-source-card__body">
                  <strong>{source.document_name}</strong>
                  <small>
                    {[
                      source.page_number ? `第 ${source.page_number} 页` : null,
                      source.section,
                    ]
                      .filter(Boolean)
                      .join(" · ")}
                  </small>
                  <p>{source.excerpt}</p>
                  <code>{source.reference}</code>
                  <div className="qa-source-card__actions">
                    <button
                      type="button"
                      className={`qa-source-copy${isCopied ? " is-copied" : ""}`}
                      onClick={() => void copyReference(source, index)}
                    >
                      {isCopied ? (
                        <Check size={13} strokeWidth={1.8} aria-hidden="true" />
                      ) : (
                        <Copy size={13} strokeWidth={1.6} aria-hidden="true" />
                      )}
                      <span>复制引用 {index + 1}</span>
                    </button>
                    {notesEnabled && messageId ? (
                      <a
                        className="qa-source-note-link"
                        href={noteDraftHref("qa_citation", messageId, source.citation_id)}
                      >
                        存入笔记
                      </a>
                    ) : null}
                  </div>
                </div>
              </li>
            );
          })}
        </ol>
      ) : (
        <p className="qa-muted">选择一条含引用的回答后，在这里核对来源。</p>
      )}
      <p className="qa-copy-feedback" aria-live="polite">
        {feedback}
      </p>
    </aside>
  );
}

export function SummaryStatus({ job, onCancel }: { job?: QaJob; onCancel: () => void }) {
  if (!job) return null;
  const active = job.status === "queued" || job.status === "running";
  return (
    <section className="qa-summary-status" data-status={job.status} aria-live="polite">
      <div className="qa-summary-status__head">
        <strong>
          学习摘要 ·{" "}
          {active
            ? "生成中"
            : job.status === "completed"
              ? "已完成"
              : job.status === "cancelled"
                ? "已取消"
                : "失败"}
        </strong>
        <span className="tabular-nums">
          {job.stage} · {job.progress}%{job.safe_error_code ? ` · ${job.safe_error_code}` : ""}
        </span>
      </div>
      <QaStepTrack steps={buildSummarySteps(job)} ariaLabel="摘要任务阶段" />
      {active ? (
        <div className="qa-summary-status__actions">
          <span className="qa-summary-status__skeleton" aria-hidden="true" />
          <Button hierarchy="ghost" onClick={onCancel}>
            取消生成
          </Button>
        </div>
      ) : null}
    </section>
  );
}
