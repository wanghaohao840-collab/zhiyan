import { X } from "lucide-react";
import {
  type ChangeEvent,
  type DragEvent,
  useId,
  useRef,
  useState,
} from "react";

import { ApiError } from "../../api/client";
import { Button } from "../Button/Button";
import { OverlayDialog } from "../ui";

const ACCEPTED_SUFFIXES = [".pdf", ".txt", ".md", ".markdown", ".docx"];
const MAX_FILES = 20;
const MAX_FILE_BYTES = 100 * 1024 * 1024;
const MAX_BATCH_BYTES = 500 * 1024 * 1024;

type SelectedFile = {
  file: File;
  key: string;
};

type FilePickerProps = {
  error?: string;
  inputId: string;
  onChange: (event: ChangeEvent<HTMLInputElement>) => void;
  onDragLeave: () => void;
  onDragOver: (event: DragEvent<HTMLDivElement>) => void;
  onDrop: (event: DragEvent<HTMLDivElement>) => void;
  state: "idle" | "drag-active" | "invalid";
};

export function FilePicker({
  error,
  inputId,
  onChange,
  onDragLeave,
  onDragOver,
  onDrop,
  state,
}: FilePickerProps) {
  const helpId = `${inputId}-help`;
  const errorId = error ? `${inputId}-error` : undefined;
  return (
    <div
      className="file-picker"
      data-state={state}
      data-testid="file-picker"
      onDragOver={onDragOver}
      onDragLeave={onDragLeave}
      onDrop={onDrop}
    >
      <strong>选择或拖入文档</strong>
      <p id={helpId}>支持 PDF、TXT、Markdown、DOCX；单文件不超过 100 MiB</p>
      <label className="file-picker__browse" htmlFor={inputId}>选择文件</label>
      <input
        id={inputId}
        className="file-picker__input"
        type="file"
        multiple
        accept={ACCEPTED_SUFFIXES.join(",")}
        aria-label="选择文档"
        aria-describedby={[helpId, errorId].filter(Boolean).join(" ")}
        aria-invalid={Boolean(error)}
        onChange={onChange}
      />
      {error ? <p id={errorId} className="file-picker__error" role="alert">{error}</p> : null}
    </div>
  );
}

type ImportDialogProps = {
  onClose: () => void;
  onImport: (files: File[]) => Promise<void>;
  open: boolean;
  returnFocusTo: HTMLElement | null;
};

function suffix(name: string): string {
  const index = name.lastIndexOf(".");
  return index >= 0 ? name.slice(index).toLowerCase() : "";
}

function validationError(existing: SelectedFile[], files: File[]): string | undefined {
  if (existing.length + files.length > MAX_FILES) {
    return "每批最多选择 20 个文件";
  }
  const unsupported = files.find((file) => !ACCEPTED_SUFFIXES.includes(suffix(file.name)));
  if (unsupported) {
    return `${unsupported.name} 的文件类型不支持`;
  }
  const oversized = files.find((file) => file.size > MAX_FILE_BYTES);
  if (oversized) {
    return `${oversized.name} 超过单文件 100 MiB 限制`;
  }
  const totalBytes = [...existing.map(({ file }) => file), ...files].reduce(
    (total, file) => total + file.size,
    0,
  );
  if (totalBytes > MAX_BATCH_BYTES) {
    return "所选文件超过每批 500 MiB 限制";
  }
  return undefined;
}

export function ImportDialog({
  onClose,
  onImport,
  open,
  returnFocusTo,
}: ImportDialogProps) {
  const inputId = `document-files-${useId()}`;
  const closeRef = useRef<HTMLButtonElement>(null);
  const localKeyRef = useRef(0);
  const [selected, setSelected] = useState<SelectedFile[]>([]);
  const [dragActive, setDragActive] = useState(false);
  const [error, setError] = useState<string>();
  const [submitting, setSubmitting] = useState(false);

  function addFiles(files: File[]) {
    setDragActive(false);
    const nextError = validationError(selected, files);
    setError(nextError);
    if (nextError) {
      return;
    }
    const additions = files.map((file) => ({
      file,
      key: `${localKeyRef.current++}-${file.name}-${file.size}-${file.lastModified}`,
    }));
    setSelected((current) => [...current, ...additions]);
  }

  function handleChange(event: ChangeEvent<HTMLInputElement>) {
    addFiles(Array.from(event.currentTarget.files ?? []));
    event.currentTarget.value = "";
  }

  function handleDragOver(event: DragEvent<HTMLDivElement>) {
    event.preventDefault();
    setDragActive(true);
  }

  function handleDrop(event: DragEvent<HTMLDivElement>) {
    event.preventDefault();
    addFiles(Array.from(event.dataTransfer.files));
  }

  function removeFile(key: string) {
    setSelected((current) => current.filter((entry) => entry.key !== key));
    setError(undefined);
  }

  async function startImport() {
    if (!selected.length) {
      setError("请至少选择一个文件");
      return;
    }
    setSubmitting(true);
    setError(undefined);
    try {
      await onImport(selected.map(({ file }) => file));
      setSelected([]);
      onClose();
    } catch (reason) {
      setError(
        reason instanceof ApiError
          ? reason.message
          : "导入失败，请稍后重试",
      );
    } finally {
      setSubmitting(false);
    }
  }

  const pickerState = error ? "invalid" : dragActive ? "drag-active" : "idle";

  return (
    <OverlayDialog
      open={open}
      hideHeader
      canClose={!submitting}
      ariaLabel="导入文档"
      layerClassName="document-dialog-layer"
      scrimClassName="document-dialog-backdrop"
      scrimLabel="关闭导入遮罩"
      panelClassName="import-dialog"
      initialFocusRef={closeRef}
      returnFocusTo={returnFocusTo}
      onClose={onClose}
    >
      <header className="import-dialog__header">
        <h2 id="import-dialog-title">导入文档</h2>
        <button
          ref={closeRef}
          type="button"
          className="import-dialog__close document-action-target"
          aria-label="关闭导入文档"
          disabled={submitting}
          onClick={onClose}
        >
          <X size={18} strokeWidth={1.6} aria-hidden="true" />
        </button>
      </header>
      <p className="import-dialog__intro">选择文件后立即创建持久任务；不同文件独立成功或失败。</p>
      <FilePicker
        inputId={inputId}
        state={pickerState}
        error={error}
        onChange={handleChange}
        onDragOver={handleDragOver}
        onDragLeave={() => setDragActive(false)}
        onDrop={handleDrop}
      />
      {selected.length ? (
        <ul className="selected-files" aria-label="已选择文档">
          {selected.map(({ file, key }) => (
            <li key={key}>
              <span>{file.name}</span>
              <button
                type="button"
                className="document-action-target"
                aria-label={`移除 ${file.name}`}
                disabled={submitting}
                onClick={() => removeFile(key)}
              >
                <X size={16} strokeWidth={1.6} aria-hidden="true" />
              </button>
            </li>
          ))}
        </ul>
      ) : null}
      <p className="import-dialog__limits">每批最多 20 个文件 · 总计不超过 500 MiB</p>
      <div className="import-dialog__actions">
        <Button
          className="import-dialog__primary"
          loading={submitting}
          onClick={() => void startImport()}
        >
          开始导入
        </Button>
        <Button hierarchy="secondary" disabled={submitting} onClick={onClose}>
          取消
        </Button>
      </div>
    </OverlayDialog>
  );
}
