import { Trash2 } from "lucide-react";
import type { Document } from "../../features/documents/types";

type DocumentRowProps = {
  deleting: boolean;
  document: Document;
  onDelete: (document: Document, trigger: HTMLButtonElement) => void;
  onAsk: (document: Document) => void;
};

function formatSize(sizeBytes: number): string {
  if (sizeBytes < 1024) {
    return `${sizeBytes} B`;
  }
  if (sizeBytes < 1024 * 1024) {
    return `${Math.round(sizeBytes / 1024)} KiB`;
  }
  const value = sizeBytes / (1024 * 1024);
  return `${Number(value.toFixed(value >= 10 ? 0 : 1))} MiB`;
}

function formatDate(value: string): string | undefined {
  const date = new Date(value);
  if (Number.isNaN(date.valueOf())) {
    return undefined;
  }
  return new Intl.DateTimeFormat("zh-CN", {
    dateStyle: "medium",
    timeStyle: "short",
  }).format(date);
}

export function DocumentRow({ deleting, document, onDelete, onAsk }: DocumentRowProps) {
  const metadata = [
    document.file_suffix.replace(/^\./, "").toUpperCase(),
    document.size_bytes === null ? undefined : formatSize(document.size_bytes),
    document.loaded_at === null ? undefined : formatDate(document.loaded_at),
  ].filter(Boolean);

  return (
    <li
      className="document-row"
      data-document-id={document.document_id}
      data-state={deleting ? "deleting" : "ready"}
      aria-label={document.name}
    >
      <span className="document-row__icon" aria-hidden="true">{document.file_suffix.replace(/^\./, "").toUpperCase()}</span>
      <span className="document-row__body">
        <strong title={document.name}>{document.name}</strong>
        {metadata.length ? <span>{metadata.join(" · ")}</span> : null}
        <span className="document-row__mobile-status">
          {deleting ? "正在删除" : "已导入"}
        </span>
      </span>
      <span className="document-row__status">{deleting ? "正在删除" : "已导入"}</span>
      <button type="button" className="document-row__ask document-action-target" disabled={deleting} onClick={() => onAsk(document)}>开始问答</button>
      <button
        type="button"
        className="document-row__delete document-action-target"
        aria-label={`删除 ${document.name}`}
        disabled={deleting}
        aria-busy={deleting || undefined}
        onClick={(event) => onDelete(document, event.currentTarget)}
      >
        <Trash2 size={16} strokeWidth={1.6} aria-hidden="true" />
      </button>
    </li>
  );
}

type DocumentListProps = {
  deletingDocumentId?: string;
  documents: Document[];
  onDelete: (document: Document, trigger: HTMLButtonElement) => void;
  onAsk: (document: Document) => void;
};

export function DocumentList({
  deletingDocumentId,
  documents,
  onDelete,
  onAsk,
}: DocumentListProps) {
  return (
    <section className="document-list-panel" aria-labelledby="document-list-title">
      <h2 id="document-list-title">全部文档</h2>
      {documents.length ? (
        <ul className="document-list" aria-label="文档列表">
          {documents.map((document) => (
            <DocumentRow
              key={document.document_id}
              document={document}
              deleting={document.document_id === deletingDocumentId}
              onDelete={onDelete}
              onAsk={onAsk}
            />
          ))}
        </ul>
      ) : (
        <p className="document-list-panel__no-results">没有匹配的文档</p>
      )}
    </section>
  );
}
