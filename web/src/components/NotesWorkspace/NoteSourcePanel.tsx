import { X } from "lucide-react";
import { useEffect, useRef } from "react";
import type { NoteSource, NoteListSource } from "../../features/notes/types";

import { SourceCard } from "./SourceCard";

type Source = NoteSource | NoteListSource;
export function NoteSourcePanel({ concept, onOpenQa, onOpenSearch, sources, onClose, asDialog = false, returnFocusTo }: { concept?: string; onOpenQa?: () => void; onOpenSearch?: () => void; sources: Source[]; onClose?: () => void; asDialog?: boolean; returnFocusTo?: HTMLElement | null }) {
  const panel = useRef<HTMLElement>(null);
  const closeRef = useRef(onClose);
  closeRef.current = onClose;
  useEffect(() => { if (!asDialog) return; const prior = document.body.style.overflow; document.body.style.overflow = "hidden"; panel.current?.querySelector<HTMLElement>("button")?.focus(); const keyDown = (event: KeyboardEvent) => { if (event.key === "Escape") { event.preventDefault(); closeRef.current?.(); return; } if (event.key !== "Tab") return; const focusable = Array.from(panel.current?.querySelectorAll<HTMLElement>('button:not([disabled]), a[href], input:not([disabled]), textarea:not([disabled])') ?? []); const first = focusable[0]; const last = focusable.at(-1); if (!first || !last) return; if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus(); } else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus(); } }; window.addEventListener("keydown", keyDown); return () => { window.removeEventListener("keydown", keyDown); document.body.style.overflow = prior; returnFocusTo?.focus(); }; }, [asDialog, returnFocusTo]);
  const qaCount = new Set(sources.filter((source) => source.kind !== "document_chunk" && !source.deleted).map((source) => "qa_message_id" in source ? source.qa_message_id || source.id : source.id)).size;
  const content = <>
    <header className="notes-source__heading"><div><span className="notes-eyebrow">SOURCES & CONTEXT</span><h2>来源与上下文</h2></div>{onClose ? <button type="button" className="notes-icon-button" aria-label="关闭来源" onClick={onClose}><X size={18} strokeWidth={1.6} aria-hidden="true" /></button> : null}</header>
    {qaCount > 0 ? <p className="notes-source-count">来源于 {qaCount} 条问答记录</p> : null}
    {!sources.length ? <div className="notes-source-empty"><span className="notes-source-empty__mark" aria-hidden="true">↗</span><h3>让理解有据可循</h3><p>这篇笔记当前还没有关联来源。你可以从问答记录或文献证据中补充依据，让笔记更可追溯。</p><div className="notes-source-empty__actions">{onOpenQa ? <button type="button" onClick={onOpenQa}>从 QA 记录导入 ↗</button> : null}{onOpenSearch ? <button type="button" onClick={onOpenSearch}>从文献检索添加 ↗</button> : null}</div><small>从这些入口创建新的来源笔记。</small></div> : <ol>{sources.map((source, index) => <SourceCard key={source.id ?? `${source.kind}-${index}`} source={source} />)}</ol>}
    <section className="notes-context"><span className="notes-eyebrow">RELATED CONCEPTS</span><h3>关联概念</h3>{concept?.trim() ? <p className="notes-concept-chip">{concept}</p> : <p>记录核心概念，帮助未来的你重新找到这份理解。</p>}<h3>记忆线索</h3><p>暂无更多关联线索。保存的笔记会按当前系统的记忆同步状态持续积累。</p></section>
  </>;
  if (!asDialog) return <aside className="notes-source" aria-label="笔记来源">{content}</aside>;
  return <div className="notes-overlay" role="presentation"><button className="notes-overlay__scrim" aria-label="关闭来源遮罩" onClick={onClose} /><section ref={panel} className="notes-sheet" role="dialog" aria-modal="true" aria-label="笔记来源">{content}</section></div>;
}
