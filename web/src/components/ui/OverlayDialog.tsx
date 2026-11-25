import { X } from "lucide-react";
import { useEffect, useId, useRef, type ReactNode, type RefObject } from "react";

export interface OverlayDialogProps {
  open: boolean;
  title?: string;
  ariaLabel?: string;
  onClose: () => void;
  children: ReactNode;
  variant?: "dialog" | "drawer" | "sheet";
  closeLabel?: string;
  closeDisabled?: boolean;
  canClose?: boolean;
  dismissible?: boolean;
  hideHeader?: boolean;
  initialFocusRef?: RefObject<HTMLElement | null>;
  returnFocusTo?: HTMLElement | null;
  className?: string;
  layerClassName?: string;
  scrimClassName?: string;
  scrimLabel?: string;
  panelClassName?: string;
  panelId?: string;
}

const FOCUSABLE_SELECTOR =
  'button:not([disabled]), [href], input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])';

/**
 * 统一轻量遮罩弹窗/抽屉原语：内建焦点陷阱、Escape 关闭、滚动锁定与焦点归还，
 * 全站所有弹窗与抽屉统一复用此原语以保证一致的无障碍行为与双主题外观。
 */
export function OverlayDialog({
  open,
  title,
  ariaLabel,
  onClose,
  children,
  variant = "dialog",
  closeLabel,
  closeDisabled = false,
  canClose = true,
  dismissible = true,
  hideHeader = false,
  initialFocusRef,
  returnFocusTo,
  className,
  layerClassName,
  scrimClassName,
  scrimLabel,
  panelClassName,
  panelId,
}: OverlayDialogProps) {
  const titleId = useId();
  const panelRef = useRef<HTMLElement>(null);
  const onCloseRef = useRef(onClose);
  const canCloseRef = useRef(canClose);
  const returnFocusRef = useRef(returnFocusTo);
  onCloseRef.current = onClose;
  canCloseRef.current = canClose;
  returnFocusRef.current = returnFocusTo;

  useEffect(() => {
    if (!open) return;
    const priorOverflow = document.body.style.overflow;
    const fallbackFocus =
      document.activeElement instanceof HTMLElement ? document.activeElement : null;
    document.body.style.overflow = "hidden";

    const initialTarget =
      initialFocusRef?.current ??
      panelRef.current?.querySelector<HTMLElement>(FOCUSABLE_SELECTOR);
    initialTarget?.focus();

    function handleKeyDown(event: KeyboardEvent) {
      if (event.key === "Escape") {
        event.preventDefault();
        if (canCloseRef.current) {
          onCloseRef.current();
        }
        return;
      }
      if (event.key !== "Tab") return;
      const focusable = Array.from(
        panelRef.current?.querySelectorAll<HTMLElement>(FOCUSABLE_SELECTOR) ?? [],
      ).filter((element) => !element.hasAttribute("disabled"));
      const first = focusable[0];
      const last = focusable.at(-1);
      if (!first || !last) {
        event.preventDefault();
        return;
      }
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
      const target =
        returnFocusRef.current !== undefined ? returnFocusRef.current : fallbackFocus;
      target?.focus();
    };
  }, [initialFocusRef, open]);

  if (!open) return null;

  const resolvedTitle = title ?? ariaLabel ?? "对话框";
  const resolvedCloseLabel = closeLabel ?? `关闭${resolvedTitle}`;
  const usesOwnTitle = Boolean(title) && !hideHeader;
  const resolvedScrimClass = scrimClassName
    ? `ui-overlay__scrim ${scrimClassName}`
    : "ui-overlay__scrim";

  return (
    <div
      className={layerClassName ? `ui-overlay ${layerClassName}` : "ui-overlay"}
      role="presentation"
    >
      {/* NOTE: 遮罩仅承担指针点击关闭，键盘关闭由 Escape 与显式关闭按钮负责，避免暴露多余 button 角色干扰读屏器 */}
      <div
        aria-hidden="true"
        data-scrim-label={scrimLabel}
        className={resolvedScrimClass}
        onClick={
          dismissible
            ? () => {
                if (canClose) onClose();
              }
            : undefined
        }
      />
      <section
        id={panelId}
        ref={panelRef}
        role="dialog"
        aria-modal="true"
        aria-labelledby={usesOwnTitle ? titleId : undefined}
        aria-label={usesOwnTitle ? undefined : (ariaLabel ?? title)}
        className={[
          "ui-overlay__panel",
          `ui-overlay__panel--${variant}`,
          panelClassName ?? "",
          className ?? "",
        ]
          .filter(Boolean)
          .join(" ")}
      >
        {!hideHeader && title ? (
          <header className="ui-overlay__header">
            <h2 id={titleId}>{title}</h2>
            <button
              type="button"
              className="ui-overlay__close"
              aria-label={resolvedCloseLabel}
              disabled={closeDisabled || !canClose}
              onClick={onClose}
            >
              <X size={18} strokeWidth={1.6} aria-hidden="true" />
            </button>
          </header>
        ) : null}
        {hideHeader ? children : <div className="ui-overlay__body">{children}</div>}
      </section>
    </div>
  );
}
