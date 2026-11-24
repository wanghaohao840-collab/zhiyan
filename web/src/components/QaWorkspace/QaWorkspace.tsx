import { Plus, X } from "lucide-react";
import { type KeyboardEvent, type PropsWithChildren, useEffect, useRef, useState } from "react";
import { Button } from "../Button/Button";
import { MarkdownPreview } from "../MarkdownPreview/MarkdownPreview";
import { ConstellationMark } from "../ui/ConstellationMark";
import type { QaConversation, QaJob, QaMessage, QaMode, QaSource } from "../../features/qa/types";

const FOCUSABLE = 'button:not([disabled]), input:not([disabled]), textarea:not([disabled]), select:not([disabled]), [tabindex]:not([tabindex="-1"])';

type OverlayProps = PropsWithChildren<{ title: string; className?: string; onClose: () => void; returnFocusTo?: HTMLElement | null }>;
export function QaOverlay({ title, className = "", onClose, returnFocusTo, children }: OverlayProps) {
  const panel = useRef<HTMLDivElement>(null);
  const close = useRef<HTMLButtonElement>(null);
  useEffect(() => {
    const overflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    close.current?.focus();
    return () => { document.body.style.overflow = overflow; returnFocusTo?.focus(); };
  }, [returnFocusTo]);
  function onKeyDown(event: KeyboardEvent<HTMLDivElement>) {
    if (event.key === "Escape") { event.preventDefault(); onClose(); return; }
    if (event.key !== "Tab") return;
    const items = Array.from(panel.current?.querySelectorAll<HTMLElement>(FOCUSABLE) ?? []);
    const first = items[0]; const last = items.at(-1);
    if (!first || !last) event.preventDefault();
    else if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus(); }
    else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus(); }
  }
  return (
    <div className="qa-overlay">
      <button className="qa-overlay__scrim" tabIndex={-1} aria-hidden="true" onClick={onClose} />
      <div ref={panel} className={`qa-overlay__panel ${className}`} role="dialog" aria-modal="true" aria-label={title} onKeyDown={onKeyDown}>
        <header>
          <h2>{title}</h2>
          <button ref={close} className="qa-icon-button" aria-label={`关闭${title}`} onClick={onClose}>
            <X size={18} strokeWidth={1.6} aria-hidden="true" />
          </button>
        </header>
        {children}
      </div>
    </div>
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

export function ConversationList({ items, selectedId, onSelect, onNew, onDeleteSelected, hasMore, loadingMore, onLoadMore }: ConversationListProps) {
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
        <Button hierarchy="danger" onClick={onDeleteSelected}>删除当前对话</Button>
      ) : null}
    </aside>
  );
}

type MessageListProps = {
  messages: QaMessage[];
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

export function MessageList({
  messages,
  onSources,
  onRetry,
  activeCitationId,
  busy,
  hasOlder,
  loadingOlder,
  onLoadOlder,
  notesEnabled = true,
}: MessageListProps) {
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
    <>
      {hasOlder && onLoadOlder ? (
        <div className="qa-older-action">
          <Button hierarchy="secondary" disabled={loadingOlder} onClick={onLoadOlder}>
            {loadingOlder ? "正在加载更早消息…" : "加载更早消息"}
          </Button>
        </div>
      ) : null}
      <ol className="qa-messages" aria-label="问答消息">
        {messages.map((message) => (
          <li
            key={message.message_id}
            className={`qa-message qa-message--${message.role}`}
            data-status={message.status}
          >
            <span className="qa-message__role">{message.role === "user" ? "你" : "知研"}</span>
            {message.status === "pending" ? (
              <p className="qa-message__pending" role="status">正在查找并组织答案…</p>
            ) : message.role === "assistant" && message.content ? (
              <div className="qa-message__body">
                <MarkdownPreview markdown={message.content} />
              </div>
            ) : (
              <p>{message.content || (message.status === "failed" ? "回答失败，原问题已保留。" : "")}</p>
            )}
            {message.status === "failed" ? (
              <div className="qa-message__failure" role="alert">
                <span>{message.safe_error_code ?? "QA_OPERATION_FAILED"}</span>
                <Button hierarchy="secondary" disabled={busy} onClick={() => onRetry(message)}>重试回答</Button>
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
                    onClick={(event) => onSources(message, event.currentTarget, source.citation_id)}
                    title={[source.document_name, source.page_number ? `第 ${source.page_number} 页` : null, source.section].filter(Boolean).join(" · ")}
                  >
                    <span className="qa-citation-chip__num">[{index + 1}]</span>
                    <span className="qa-citation-chip__name">{source.document_name}</span>
                    {source.page_number ? <span className="qa-citation-chip__page">P.{source.page_number}</span> : null}
                  </button>
                ))}
              </div>
            ) : null}
            {notesEnabled && message.role === "assistant" && message.status === "completed" ? (
              <div className="qa-message__note-actions">
                <a className="qa-note-action" href={noteDraftHref("qa_answer", message.message_id)}>记为笔记</a>
                {message.sources.map((source, index) => (
                  <a
                    key={source.citation_id}
                    className="qa-note-action"
                    href={noteDraftHref("qa_citation", message.message_id, source.citation_id)}
                  >
                    {message.sources.length === 1 ? "记录此引用" : `记录引用 [${index + 1}]`}
                  </a>
                ))}
              </div>
            ) : null}
          </li>
        ))}
      </ol>
    </>
  );
}

export function QaComposer({ busy, documentCount, onSubmit }: { busy: boolean; documentCount: number; onSubmit: (question: string, mode: QaMode) => void }) {
  const text = useRef<HTMLTextAreaElement>(null);
  const mode = useRef<HTMLSelectElement>(null);
  function submit() {
    const value = text.current?.value.trim() ?? "";
    if (!value) return;
    onSubmit(value, (mode.current?.value ?? "auto") as QaMode);
    if (text.current) text.current.value = "";
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
        onKeyDown={(event) => {
          if (event.key === "Enter" && (event.ctrlKey || event.metaKey) && !event.nativeEvent.isComposing) {
            event.preventDefault();
            submit();
          }
        }}
      />
      <div className="qa-composer__controls">
        <select ref={mode} aria-label="回答模式" defaultValue="auto" disabled={busy}>
          <option value="auto">自动</option>
          <option value="joint">联合分析</option>
          <option value="compare" disabled={documentCount < 2}>对比（需至少两篇文档）</option>
        </select>
        <Button loading={busy} onClick={submit}>发送</Button>
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
  const [feedback, setFeedback] = useState("");
  async function copyReference(reference: string, index: number) {
    try {
      if (!navigator.clipboard?.writeText) throw new Error("clipboard unavailable");
      await navigator.clipboard.writeText(reference);
      setFeedback(`引用 ${index + 1} 已复制`);
    } catch {
      setFeedback("复制失败，请手动选择引用文本");
    }
  }
  return (
    <aside className="qa-sources" aria-label="引用来源">
      <div className="qa-sources__header">
        <h2>引用证据</h2>
        {sources.length ? <span className="qa-sources__count tabular-nums">{sources.length} 条</span> : null}
      </div>
      {sources.length ? (
        <ol>
          {sources.map((source, index) => (
            <li
              key={source.citation_id}
              data-citation-id={source.citation_id}
              className={activeCitationId === source.citation_id ? "is-active" : undefined}
            >
              <div className="qa-source-card__top">
                <span className="qa-source-index tabular-nums">{index + 1}</span>
                <small className="qa-citation-id">{source.citation_id}</small>
              </div>
              <div className="qa-source-card__body">
                <strong>{source.document_name}</strong>
                <small>
                  {[source.page_number ? `第 ${source.page_number} 页` : null, source.section]
                    .filter(Boolean)
                    .join(" · ")}
                </small>
                <p>{source.excerpt}</p>
                <code>{source.reference}</code>
                <div className="qa-source-card__actions">
                  <button
                    type="button"
                    className="qa-source-copy"
                    onClick={() => void copyReference(source.reference, index)}
                  >
                    复制引用 {index + 1}
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
          ))}
        </ol>
      ) : (
        <p className="qa-muted">选择一条含引用的回答后，在这里核对来源。</p>
      )}
      <p className="qa-copy-feedback" aria-live="polite">{feedback}</p>
    </aside>
  );
}

export function SummaryStatus({ job, onCancel }: { job?: QaJob; onCancel: () => void }) {
  if (!job) return null;
  const active = job.status === "queued" || job.status === "running";
  return (
    <section className="qa-summary-status" data-status={job.status} aria-live="polite">
      <strong>
        学习摘要 · {active ? "生成中" : job.status === "completed" ? "已完成" : job.status === "cancelled" ? "已取消" : "失败"}
      </strong>
      <span className="tabular-nums">
        {job.stage} · {job.progress}%{job.safe_error_code ? ` · ${job.safe_error_code}` : ""}
      </span>
      {active ? (
        <>
          <span className="qa-summary-status__skeleton" aria-hidden="true" />
          <Button hierarchy="ghost" onClick={onCancel}>取消生成</Button>
        </>
      ) : null}
    </section>
  );
}
