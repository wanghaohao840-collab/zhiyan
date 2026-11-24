import { X } from "lucide-react";
import { useEffect, useId, useRef, type ReactNode } from "react";

export interface OverlayDialogProps {
  open: boolean;
  title: string;
  onClose: () => void;
  children: ReactNode;
  variant?: "dialog" | "drawer";
  closeLabel?: string;
  returnFocusTo?: HTMLElement | null;
  className?: string;
}

const FOCUSABLE_SELECTOR =
  'button:not([disabled]), [href], input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])';

/**
 * 统一轻量遮罩弹窗/抽屉原语：内建焦点陷阱、Escape 关闭、滚动锁定与焦点归还。
 */
export function OverlayDialog({
  open,
  title,
  onClose,
  children,
  variant = "dialog",
  closeLabel,
  returnFocusTo,
  className,
}: OverlayDialogProps) {
  const titleId = useId();
  const panelRef = useRef<HTMLElement>(null);
  const onCloseRef = useRef(onClose);
  onCloseRef.current = onClose;

  useEffect(() => {
    if (!open) return;
    const priorOverflow = document.body.style.overflow;
    const fallbackFocus =
      document.activeElement instanceof HTMLElement ? document.activeElement : null;
    document.body.style.overflow = "hidden";
    panelRef.current?.querySelector<HTMLElement>(FOCUSABLE_SELECTOR)?.focus();

    function handleKeyDown(event: KeyboardEvent) {
      if (event.key === "Escape") {
        event.preventDefault();
        onCloseRef.current();
        return;
      }
      if (event.key !== "Tab") return;
      const focusable = Array.from(
        panelRef.current?.querySelectorAll<HTMLElement>(FOCUSABLE_SELECTOR) ?? [],
      );
      const first = focusable[0];
      const last = focusable.at(-1);
      if (!first || !last) return;
      if (event.shiftKey && document.activeElement === first) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault();
        first.focus();
      }
    }

    window.addEventListener("keydown", handleKeyDown);
    return () => {
      window.removeEventListener("keydown", handleKeyDown);
      document.body.style.overflow = priorOverflow;
      (returnFocusTo ?? fallbackFocus)?.focus();
    };
  }, [open, returnFocusTo]);

  if (!open) return null;
  const resolvedCloseLabel = closeLabel ?? `关闭${title}`;

  return (
    <div className="ui-overlay" role="presentation">
      <button
        type="button"
        className="ui-overlay__scrim"
        aria-label={`${resolvedCloseLabel}遮罩`}
        onClick={onClose}
      />
      <section
        ref={panelRef}
        role="dialog"
        aria-modal="true"
        aria-labelledby={titleId}
        className={`ui-overlay__panel ui-overlay__panel--${variant}${className ? ` ${className}` : ""}`}
      >
        <header className="ui-overlay__header">
          <h2 id={titleId}>{title}</h2>
          <button
            type="button"
            className="ui-overlay__close"
            aria-label={resolvedCloseLabel}
            onClick={onClose}
          >
            <X size={18} strokeWidth={1.6} aria-hidden="true" />
          </button>
        </header>
        <div className="ui-overlay__body">{children}</div>
      </section>
    </div>
  );
}
