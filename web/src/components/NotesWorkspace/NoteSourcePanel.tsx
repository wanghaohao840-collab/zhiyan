import { X } from "lucide-react";

import type { NoteListSource, NoteSource } from "../../features/notes/types";
import { OverlayDialog } from "../ui";
import { SourceCard } from "./SourceCard";

type Source = NoteSource | NoteListSource;

export function NoteSourcePanel({
  concept,
  onOpenQa,
  onOpenSearch,
  sources,
  onClose,
  asDialog = false,
  returnFocusTo,
}: {
  concept?: string;
  onOpenQa?: () => void;
  onOpenSearch?: () => void;
  sources: Source[];
  onClose?: () => void;
  asDialog?: boolean;
  returnFocusTo?: HTMLElement | null;
}) {
  const qaCount = new Set(
    sources
      .filter((source) => source.kind !== "document_chunk" && !source.deleted)
      .map((source) => ("qa_message_id" in source ? source.qa_message_id || source.id : source.id)),
  ).size;

  const content = (
    <>
      <header className="notes-source__heading">
        <div>
          <span className="notes-eyebrow">SOURCES &amp; CONTEXT</span>
          <h2>来源与上下文</h2>
        </div>
        {onClose ? (
          <button
            type="button"
            className="notes-icon-button"
            aria-label="关闭来源"
            onClick={onClose}
          >
            <X size={18} strokeWidth={1.6} aria-hidden="true" />
          </button>
        ) : null}
      </header>
      {qaCount > 0 ? <p className="notes-source-count">来源于 {qaCount} 条问答记录</p> : null}
      {!sources.length ? (
        <div className="notes-source-empty">
          <span className="notes-source-empty__mark" aria-hidden="true">↗</span>
          <h3>让理解有据可循</h3>
          <p>
            这篇笔记当前还没有关联来源。你可以从问答记录或文献证据中补充依据，让笔记更可追溯。
          </p>
          <div className="notes-source-empty__actions">
            {onOpenQa ? (
              <button type="button" onClick={onOpenQa}>
                从 QA 记录导入 ↗
              </button>
            ) : null}
            {onOpenSearch ? (
              <button type="button" onClick={onOpenSearch}>
                从文献检索添加 ↗
              </button>
            ) : null}
          </div>
          <small>从这些入口创建新的来源笔记。</small>
        </div>
      ) : (
        <ol>
          {sources.map((source, index) => (
            <SourceCard key={source.id ?? `${source.kind}-${index}`} source={source} />
          ))}
        </ol>
      )}
      <section className="notes-context">
        <span className="notes-eyebrow">RELATED CONCEPTS</span>
        <h3>关联概念</h3>
        {concept?.trim() ? (
          <p className="notes-concept-chip">{concept}</p>
        ) : (
          <p>记录核心概念，帮助未来的你重新找到这份理解。</p>
        )}
        <h3>记忆线索</h3>
        <p>暂无更多关联线索。保存的笔记会按当前系统的记忆同步状态持续积累。</p>
      </section>
    </>
  );

  if (!asDialog) {
    return (
      <aside className="notes-source" aria-label="笔记来源">
        {content}
      </aside>
    );
  }

  return (
    <OverlayDialog
      open
      hideHeader
      variant="sheet"
      ariaLabel="笔记来源"
      layerClassName="notes-overlay"
      scrimClassName="notes-overlay__scrim"
      scrimLabel="关闭来源遮罩"
      panelClassName="notes-sheet"
      returnFocusTo={returnFocusTo}
      onClose={() => onClose?.()}
    >
      {content}
    </OverlayDialog>
  );
}
