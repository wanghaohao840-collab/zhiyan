import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useEffect, useRef, useState } from "react";
import { useNavigate } from "react-router-dom";

import { ApiError } from "../../api/client";
import { useAuth } from "../../auth/AuthProvider";
import { createNote } from "../../features/notes/api";
import type { SearchResult } from "../../features/search/types";
import { Button } from "../Button/Button";
import { OverlayDialog } from "../ui";
import { errorMessage, sourceLocation } from "./presentation";

export function EvidenceDetail({
  result,
  onClose,
  initialEditing = false,
}: {
  result: SearchResult;
  onClose: () => void;
  initialEditing?: boolean;
}) {
  const closeButtonRef = useRef<HTMLButtonElement>(null);
  const alive = useRef(true);
  const auth = useAuth();
  const navigate = useNavigate();
  const client = useQueryClient();
  const [feedback, setFeedback] = useState("");
  const [editing, setEditing] = useState(initialEditing);
  const [body, setBody] = useState(result.excerpt);
  const dirty = editing && body !== result.excerpt;
  const attempt = useRef<{ body: string; id: string } | null>(null);
  const save = useMutation({
    gcTime: 0,
    retry: false,
    mutationFn: (input: Parameters<typeof createNote>[1]) => createNote(auth.request, input),
  });

  useEffect(() => {
    alive.current = true;
    return () => {
      alive.current = false;
    };
  }, []);

  useEffect(() => {
    const warn = (event: BeforeUnloadEvent) => {
      if (dirty) {
        event.preventDefault();
        event.returnValue = "";
      }
    };
    window.addEventListener("beforeunload", warn);
    return () => window.removeEventListener("beforeunload", warn);
  }, [dirty]);

  async function copy() {
    try {
      if (!navigator.clipboard?.writeText) throw new Error("Clipboard unavailable");
      await navigator.clipboard.writeText(
        `${result.document_name}\n${sourceLocation(result)}\n${result.excerpt}\n文档 ID：${result.document_id}\n片段 ID：${result.locator.chunk_id}`,
      );
      if (alive.current) setFeedback("引用已复制");
    } catch {
      if (alive.current) setFeedback("无法访问剪贴板，请手动选择并复制上方内容。");
    }
  }

  async function saveNote() {
    if (!body.trim() || body.length > 20000 || save.isPending) return;
    if (!attempt.current || attempt.current.body !== body) {
      attempt.current = { body, id: crypto.randomUUID() };
    }
    try {
      const note = await save.mutateAsync({
        body_markdown: body,
        client_request_id: attempt.current.id,
        source: { kind: "document_chunk", locator: result.locator },
      });
      if (!alive.current) return;
      await client.invalidateQueries({ queryKey: ["notes"] });
      if (alive.current) navigate(`/notes?note=${encodeURIComponent(note.id)}`);
    } catch {
      // NOTE: 保留用户输入草稿与幂等请求 ID，便于错误恢复后重试
    }
  }

  function close() {
    if (save.isPending) return;
    if (dirty && !window.confirm("笔记尚未保存，确定关闭吗？")) return;
    onClose();
  }

  function openQa() {
    if (dirty && !window.confirm("笔记尚未保存，确定进入问答吗？")) return;
    navigate(`/qa?documents=${encodeURIComponent(result.document_id)}`);
  }

  const staleSource =
    save.error instanceof ApiError &&
    ["NOTE_SOURCE_NOT_FOUND", "NOTE_SOURCE_DELETING"].includes(save.error.code);

  return (
    <OverlayDialog
      open
      hideHeader
      canClose={!save.isPending}
      ariaLabel="来源详情"
      panelClassName="search-detail"
      initialFocusRef={closeButtonRef}
      onClose={close}
    >
      <header>
        <h2 id="search-detail-title">来源详情</h2>
        <button
          ref={closeButtonRef}
          type="button"
          className="button button--ghost button--md"
          disabled={save.isPending}
          aria-label="关闭来源详情"
          onClick={close}
        >
          关闭
        </button>
      </header>
      <h3>{result.document_name}</h3>
      <p className="search-location">{sourceLocation(result)}</p>
      <p className="search-full-excerpt">{result.excerpt}</p>
      <p className="search-muted">
        显示检索摘录，不代表全文；相关度仅用于排序，不代表答案可信度。
      </p>
      <div className="search-actions">
        <Button hierarchy="secondary" onClick={() => void copy()}>
          复制引用
        </Button>
        <Button hierarchy="secondary" disabled={save.isPending} onClick={openQa}>
          基于此文档问答
        </Button>
        <Button disabled={editing} onClick={() => setEditing(true)}>
          保存为笔记
        </Button>
      </div>
      {feedback ? <p role="status">{feedback}</p> : null}
      {editing ? (
        <form
          className="search-note"
          onSubmit={(event) => {
            event.preventDefault();
            void saveNote();
          }}
        >
          <label htmlFor="search-note-body">笔记正文（保存前可编辑）</label>
          <textarea
            id="search-note-body"
            value={body}
            maxLength={20000}
            rows={7}
            disabled={save.isPending}
            onChange={(event) => {
              setBody(event.target.value);
              save.reset();
            }}
          />
          <p>写下你的理解，保存后可在学习笔记中继续整理。</p>
          {save.error ? (
            <p role="alert">
              {staleSource
                ? "来源已变化或正在删除。请保留正文，关闭详情后重新检索。"
                : errorMessage(save.error, "笔记保存失败，请重试。")}
            </p>
          ) : null}
          <Button
            type="submit"
            loading={save.isPending}
            disabled={!body.trim() || body.length > 20000 || staleSource}
          >
            保存并打开笔记
          </Button>
        </form>
      ) : null}
    </OverlayDialog>
  );
}
