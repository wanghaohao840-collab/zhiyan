import { useEffect, useMemo, useRef, useState } from "react";
import { useNavigate } from "react-router-dom";

import { ApiError } from "../api/client";
import { Button } from "../components/Button/Button";
import { DocumentList } from "../components/DocumentList/DocumentList";
import {
  DocumentToolbar,
  type DocumentTypeFilter,
} from "../components/DocumentToolbar/DocumentToolbar";
import {
  ImportBatchPanel,
  importStageText,
} from "../components/ImportBatchPanel/ImportBatchPanel";
import { ImportDialog } from "../components/ImportDialog/ImportDialog";
import { ConstellationMark, OverlayDialog } from "../components/ui";
import {
  useDocumentMutations,
  useDocumentsQuery,
  useImportsQuery,
} from "../features/documents/queries";
import type { Document, ImportBatch } from "../features/documents/types";

function safeMessage(reason: unknown, fallback: string): string {
  return reason instanceof ApiError ? reason.message : fallback;
}

function importAnnouncement(batches: ImportBatch[]): string {
  if (!batches.length) return "";
  const tasks = batches.flatMap((batch) => batch.tasks);
  const counts = tasks.reduce(
    (current, task) => ({ ...current, [task.status]: current[task.status] + 1 }),
    {
      queued: 0,
      running: 0,
      retry_wait: 0,
      succeeded: 0,
      failed: 0,
      cancelled: 0,
    },
  );
  const activeDetails = tasks
    .filter(
      (task) =>
        task.status === "queued" ||
        task.status === "running" ||
        task.status === "retry_wait",
    )
    .map((task) => {
      if (task.cancel_requested_at) {
        return `${task.original_name}：正在取消`;
      }
      if (task.status === "running") {
        const progress = Math.floor(Math.max(0, Math.min(task.progress, 100)) / 10) * 10;
        return `${task.original_name}：${importStageText(task.stage)} · ${progress}%`;
      }
      if (task.status === "retry_wait") {
        return `${task.original_name}：等待重试`;
      }
      return `${task.original_name}：${importStageText(task.stage)}`;
    });
  const summary = `导入任务：排队 ${counts.queued}，处理中 ${counts.running}，等待重试 ${counts.retry_wait}，完成 ${counts.succeeded}，失败 ${counts.failed}，取消 ${counts.cancelled}`;
  return activeDetails.length ? `${summary}。${activeDetails.join("；")}` : summary;
}

type DeleteDialogProps = {
  document: Document;
  error?: string;
  onClose: () => void;
  onConfirm: () => void;
  pending: boolean;
  returnFocusTo: HTMLElement | null;
};

function DeleteDialog({
  document,
  error,
  onClose,
  onConfirm,
  pending,
  returnFocusTo,
}: DeleteDialogProps) {
  const cancelRef = useRef<HTMLButtonElement>(null);
  const errorRef = useRef<HTMLParagraphElement>(null);

  useEffect(() => {
    if (error) errorRef.current?.focus();
  }, [error]);

  return (
    <OverlayDialog
      open
      hideHeader
      canClose={!pending}
      ariaLabel={`删除 ${document.name}`}
      layerClassName="document-dialog-layer"
      scrimClassName="document-dialog-backdrop"
      scrimLabel={`关闭删除 ${document.name} 遮罩`}
      panelClassName="delete-dialog"
      initialFocusRef={cancelRef}
      returnFocusTo={returnFocusTo}
      onClose={onClose}
    >
      <h2 id="delete-dialog-title">删除 {document.name}</h2>
      <p>确认删除“{document.name}”？此操作会删除该文档及相关检索数据。</p>
      {error ? (
        <p ref={errorRef} className="document-error" role="alert" tabIndex={-1}>
          {error}
        </p>
      ) : null}
      <div className="delete-dialog__actions">
        <button
          ref={cancelRef}
          type="button"
          className="button button--secondary button--md"
          disabled={pending}
          onClick={onClose}
        >
          <span>取消</span>
        </button>
        <Button hierarchy="danger" loading={pending} onClick={onConfirm}>
          确认删除
        </Button>
      </div>
    </OverlayDialog>
  );
}

export function DocumentsPage() {
  const navigate = useNavigate();
  const documentsQuery = useDocumentsQuery();
  const importsQuery = useImportsQuery();
  const mutations = useDocumentMutations();
  const [filter, setFilter] = useState("");
  const [typeFilter, setTypeFilter] = useState<DocumentTypeFilter>("all");
  const [importOpen, setImportOpen] = useState(false);
  const [importTrigger, setImportTrigger] = useState<HTMLElement | null>(null);
  const [deleteTarget, setDeleteTarget] = useState<Document>();
  const [deleteTrigger, setDeleteTrigger] = useState<HTMLElement | null>(null);
  const [announcement, setAnnouncement] = useState("");
  const lastAnnouncementRef = useRef("");
  const batches = importsQuery.data ?? [];
  const documents = documentsQuery.data?.items ?? [];

  useEffect(() => {
    const next = importAnnouncement(batches);
    if (next !== lastAnnouncementRef.current) {
      lastAnnouncementRef.current = next;
      setAnnouncement(next);
    }
  }, [batches]);

  const filteredDocuments = useMemo(() => {
    const normalized = filter.trim().toLocaleLowerCase("zh-CN");
    return documents.filter((document) => {
      if (typeFilter !== "all") {
        const suffix = (document.file_suffix || "").toLowerCase();
        const matchesType =
          typeFilter === "md"
            ? suffix === ".md" || suffix === ".markdown"
            : suffix === `.${typeFilter}`;
        if (!matchesType) return false;
      }
      if (!normalized) return true;
      return document.name.toLocaleLowerCase("zh-CN").includes(normalized);
    });
  }, [documents, filter, typeFilter]);

  const initialLoading =
    (documentsQuery.isPending && !documentsQuery.data) ||
    (importsQuery.isPending && !importsQuery.data);
  const queryError = documentsQuery.error ?? importsQuery.error;
  const actionError =
    mutations.retryTask.error ??
    mutations.retryFailed.error ??
    mutations.cancelTask.error;

  function openImport(trigger: HTMLButtonElement) {
    setImportTrigger(trigger);
    setImportOpen(true);
  }

  function openDelete(document: Document, trigger: HTMLButtonElement) {
    mutations.removeDocument.reset();
    setDeleteTrigger(trigger);
    setDeleteTarget(document);
  }

  function closeDelete() {
    if (!mutations.removeDocument.isPending) setDeleteTarget(undefined);
  }

  function confirmDelete() {
    if (!deleteTarget) return;
    mutations.removeDocument.mutate(
      { documentId: deleteTarget.document_id },
      { onSuccess: () => setDeleteTarget(undefined) },
    );
  }

  function reload() {
    void Promise.all([documentsQuery.refetch(), importsQuery.refetch()]).catch(
      () => undefined,
    );
  }

  return (
    <article className="documents-page">
      <DocumentToolbar
        filter={filter}
        onFilterChange={setFilter}
        onOpenImport={openImport}
        showFilter={documents.length > 0}
        typeFilter={typeFilter}
        onTypeFilterChange={setTypeFilter}
      />
      <div
        className="document-live-region"
        role="status"
        aria-label="导入状态"
        aria-live="polite"
        aria-atomic="true"
      >
        {announcement}
      </div>
      {queryError ? (
        <div className="document-query-error" role="alert">
          <p>{safeMessage(queryError, "文档库加载失败，请稍后重试")}</p>
          <Button hierarchy="secondary" onClick={reload}>重新加载</Button>
        </div>
      ) : null}
      {actionError ? (
        <p className="document-error" role="alert">
          {safeMessage(actionError, "操作失败，请稍后重试")}
        </p>
      ) : null}
      {initialLoading ? (
        <div className="document-skeleton" role="status" aria-label="正在加载文档库">
          <span />
          <span />
          <span />
        </div>
      ) : null}
      {!initialLoading && documentsQuery.data ? (
        <>
          {batches.length ? (
            <ImportBatchPanel
              batches={batches}
              retryingTask={mutations.retryTask.isPending ? mutations.retryTask.variables : undefined}
              retryingBatchId={mutations.retryFailed.isPending ? mutations.retryFailed.variables?.batchId : undefined}
              cancellingTask={mutations.cancelTask.isPending ? mutations.cancelTask.variables : undefined}
              onRetry={(batchId, taskId) => mutations.retryTask.mutate({ batchId, taskId })}
              onRetryFailed={(batchId) => mutations.retryFailed.mutate({ batchId })}
              onCancel={(batchId, taskId) => mutations.cancelTask.mutate({ batchId, taskId })}
            />
          ) : null}
          {!documents.length ? (
            <div className="documents-empty-state">
              <section className="documents-empty" aria-labelledby="documents-empty-title">
                <ConstellationMark size="md" />
                <h2 id="documents-empty-title">还没有文档</h2>
                <p>导入 PDF、TXT、Markdown 或 DOCX，开始构建你的知识库。</p>
                <Button hierarchy="secondary" onClick={(event) => openImport(event.currentTarget)}>
                  导入文档
                </Button>
              </section>
              <small className="documents-empty-state__limits">
                每批最多 20 个文件 · 单文件 100 MiB · 每批 500 MiB
              </small>
            </div>
          ) : (
            <DocumentList
              documents={filteredDocuments}
              deletingDocumentId={
                mutations.removeDocument.isPending
                  ? mutations.removeDocument.variables?.documentId
                  : undefined
              }
              onDelete={openDelete}
              onAsk={(document) => navigate(`/qa?documents=${encodeURIComponent(document.document_id)}`)}
            />
          )}
        </>
      ) : null}
      <ImportDialog
        open={importOpen}
        returnFocusTo={importTrigger}
        onClose={() => setImportOpen(false)}
        onImport={async (files) => {
          await mutations.submit.mutateAsync(files);
        }}
      />
      {deleteTarget ? (
        <DeleteDialog
          document={deleteTarget}
          pending={mutations.removeDocument.isPending}
          error={
            mutations.removeDocument.error
              ? safeMessage(mutations.removeDocument.error, "文档删除失败，请重试")
              : undefined
          }
          returnFocusTo={deleteTrigger}
          onClose={closeDelete}
          onConfirm={confirmDelete}
        />
      ) : null}
    </article>
  );
}
