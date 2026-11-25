import { useRef } from "react";

import { Button } from "../Button/Button";
import { OverlayDialog } from "../ui";

export function NoteClearDialog({
  pending,
  onClose,
  onConfirm,
  returnFocusTo,
}: {
  pending: boolean;
  onClose: () => void;
  onConfirm: () => void;
  returnFocusTo?: HTMLElement | null;
}) {
  const cancel = useRef<HTMLButtonElement>(null);

  return (
    <OverlayDialog
      open
      hideHeader
      canClose={!pending}
      ariaLabel="清空全部笔记"
      layerClassName="notes-overlay"
      scrimClassName="notes-overlay__scrim"
      scrimLabel="关闭清空确认遮罩"
      panelClassName="notes-dialog"
      initialFocusRef={cancel}
      returnFocusTo={returnFocusTo}
      onClose={onClose}
    >
      <h2>清空全部笔记</h2>
      <p>这会删除当前账号的所有笔记，但不会删除文档。</p>
      <div className="notes-dialog__actions">
        <button ref={cancel} type="button" disabled={pending} onClick={onClose}>
          取消
        </button>
        <Button hierarchy="danger" loading={pending} onClick={onConfirm}>
          确认清空
        </Button>
      </div>
    </OverlayDialog>
  );
}
