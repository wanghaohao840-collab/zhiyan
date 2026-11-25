import { type KeyboardEvent, type PropsWithChildren, useEffect, useRef, useState } from "react";
import { Button } from "../Button/Button";
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
  return <div className="qa-overlay"><button className="qa-overlay__scrim" tabIndex={-1} aria-hidden="true" onClick={onClose} /><div ref={panel} className={`qa-overlay__panel ${className}`} role="dialog" aria-modal="true" aria-label={title} onKeyDown={onKeyDown}><header><h2>{title}</h2><button ref={close} className="qa-icon-button" aria-label={`关闭${title}`} onClick={onClose}>×</button></header>{children}</div></div>;
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
  return <aside className="qa-conversations" aria-label="对话列表"><div className="qa-panel-heading"><h2>学习线程</h2><button className="qa-icon-button" aria-label="新建对话" onClick={onNew}>＋</button></div>{items.length ? <ol>{items.map((item) => <li key={item.conversation_id}><button className="qa-conversation" aria-current={selectedId === item.conversation_id ? "page" : undefined} onClick={() => onSelect(item.conversation_id)}><strong>{item.title}</strong><small>{item.documents.map((doc) => doc.document_name).join("、")}</small></button></li>)}</ol> : <p className="qa-muted">还没有对话</p>}{hasMore && onLoadMore ? <Button hierarchy="secondary" disabled={loadingMore} onClick={onLoadMore}>{loadingMore ? "正在加载更多对话…" : "加载更多对话"}</Button> : null}{onDeleteSelected ? <Button hierarchy="danger" onClick={onDeleteSelected}>删除当前对话</Button> : null}</aside>;
}

type MessageListProps = {
  messages: QaMessage[];
  onSources: (message: QaMessage, trigger: HTMLButtonElement) => void;
  onRetry: (message: QaMessage) => void;
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

const SUMMARY_RETRYABLE_CODES = new Set([
  "QA_ENGINE_UNAVAILABLE", "QA_RAG_CONNECTION", "QA_RAG_TRANSIENT",
  "RAG_CONNECTION_FAILED", "RAG_EMBEDDING_FAILED", "RAG_OPERATION_FAILED",
  "RAG_COLLECTION_FAILED",
]);

function MessageFailure({ message, busy, alreadyRetried, onRetry }: {
  message: QaMessage;
  busy?: boolean;
  alreadyRetried: boolean;
  onRetry: (message: QaMessage) => void;
}) {
  const code = message.safe_error_code ?? "QA_OPERATION_FAILED";
  const isSummary = message.mode === "summary";
  const incomplete = isSummary && code === "QA_SUMMARY_INCOMPLETE";
  const canRetry = !isSummary || (!alreadyRetried && (
    SUMMARY_RETRYABLE_CODES.has(code) || (incomplete && !message.retry_of_message_id)
  ));
  return <div className="qa-message__failure" role="alert">
    <span>{incomplete ? "模型没有生成完整摘要。" : code}</span>
    {canRetry
      ? <Button hierarchy="secondary" disabled={busy} onClick={() => onRetry(message)}>
        {isSummary ? "重试摘要" : "重试回答"}
      </Button>
      : <span>{alreadyRetried
        ? "已发起重试，请查看后续摘要。"
        : incomplete ? "可选择更少的文档，再生成摘要。" : "可使用页面上方的「生成摘要」重新发起。"}</span>}
  </div>;
}

export function MessageList({ messages, onSources, onRetry, busy, hasOlder, loadingOlder, onLoadOlder, notesEnabled = true }: MessageListProps) {
  if (!messages.length) return <div className="qa-welcome"><h2>从一个问题，开始理解</h2><p>提出问题，知研会用当前对话绑定的文档回答，并保留引用。</p></div>;
  const retriedIds = new Set(messages.map((message) => message.retry_of_message_id));
  return <>{hasOlder && onLoadOlder ? <Button hierarchy="secondary" disabled={loadingOlder} onClick={onLoadOlder}>{loadingOlder ? "正在加载更早消息…" : "加载更早消息"}</Button> : null}<ol className="qa-messages" aria-label="问答消息">{messages.map((message) => <li key={message.message_id} className={`qa-message qa-message--${message.role}`} data-status={message.status}><span className="qa-message__role">{message.role === "user" ? "你" : "知研"}</span>{message.status === "pending" ? <p role="status">正在查找并组织答案…</p> : <p>{message.content || (message.status === "failed" ? "回答失败，原问题已保留。" : "")}</p>}{message.status === "failed" ? <MessageFailure message={message} busy={busy} alreadyRetried={retriedIds.has(message.message_id)} onRetry={onRetry} /> : null}{notesEnabled && message.role === "assistant" && message.status === "completed" ? <div className="qa-message__note-actions"><a className="qa-note-action" href={noteDraftHref("qa_answer", message.message_id)}>记为笔记</a>{message.sources.map((source) => <a key={source.citation_id} className="qa-note-action" href={noteDraftHref("qa_citation", message.message_id, source.citation_id)}>记录此引用</a>)}</div> : null}{message.role === "assistant" && message.sources.length ? <button className="qa-source-trigger" onClick={(event) => onSources(message, event.currentTarget)}>引用 {message.sources.length}</button> : null}</li>)}</ol></>;
}

export function QaComposer({ busy, documentCount, onSubmit }: { busy: boolean; documentCount: number; onSubmit: (question: string, mode: QaMode) => void }) {
  const text = useRef<HTMLTextAreaElement>(null);
  const mode = useRef<HTMLSelectElement>(null);
  function submit() { const value = text.current?.value.trim() ?? ""; if (!value) return; onSubmit(value, (mode.current?.value ?? "auto") as QaMode); if (text.current) text.current.value = ""; }
  return <div className="qa-composer"><label htmlFor="qa-question">向这些文档提问</label><textarea ref={text} id="qa-question" maxLength={20000} rows={2} disabled={busy} onKeyDown={(event) => { if (event.key === "Enter" && (event.ctrlKey || event.metaKey) && !event.nativeEvent.isComposing) { event.preventDefault(); submit(); } }} /><div><select ref={mode} aria-label="回答模式" defaultValue="auto" disabled={busy}><option value="auto">自动</option><option value="joint">联合分析</option><option value="compare" disabled={documentCount < 2}>对比（需至少两篇文档）</option></select><Button loading={busy} onClick={submit}>发送</Button></div></div>;
}

export function SourcePanel({ sources }: { sources: QaSource[] }) {
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
  return <aside className="qa-sources" aria-label="引用来源"><h2>引用证据</h2>{sources.length ? <ol>{sources.map((source, index) => <li key={source.citation_id}><span className="qa-source-index">{index + 1}</span><div><small className="qa-citation-id">{source.citation_id}</small><strong>{source.document_name}</strong><small>{[source.page_number ? `第 ${source.page_number} 页` : null, source.section].filter(Boolean).join(" · ")}</small><p>{source.excerpt}</p><code>{source.reference}</code><button className="qa-source-copy" onClick={() => void copyReference(source.reference, index)}>复制引用 {index + 1}</button></div></li>)}</ol> : <p className="qa-muted">选择一条含引用的回答后，在这里核对来源。</p>}<p className="qa-copy-feedback" aria-live="polite">{feedback}</p></aside>;
}

export function SummaryStatus({ job, onCancel }: { job?: QaJob; onCancel: () => void }) {
  if (!job) return null;
  const active = job.status === "queued" || job.status === "running";
  return <section className="qa-summary-status" aria-live="polite"><strong>学习摘要 · {active ? "生成中" : job.status === "completed" ? "已完成" : job.status === "cancelled" ? "已取消" : "失败"}</strong><span>{job.stage} · {job.progress}%{job.safe_error_code ? ` · ${job.safe_error_code}` : ""}</span>{active ? <><span className="qa-summary-status__skeleton" aria-hidden="true" /><Button hierarchy="ghost" onClick={onCancel}>取消生成</Button></> : null}</section>;
}
