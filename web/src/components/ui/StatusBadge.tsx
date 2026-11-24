import type { ReactNode } from "react";

export type StatusTone = "neutral" | "brand" | "success" | "info" | "warning" | "danger";

export type StatusBadgeProps = {
  children: ReactNode;
  className?: string;
  icon?: ReactNode;
  tone?: StatusTone;
};

/**
 * 统一语义状态徽章：始终结合色阶与文本/图标传递含义，避免仅靠颜色区分状态。
 */
export function StatusBadge({
  children,
  className,
  icon,
  tone = "neutral",
}: StatusBadgeProps) {
  return (
    <span className={["status-badge", `status-badge--${tone}`, className].filter(Boolean).join(" ")}>
      {icon ? <span className="status-badge__icon" aria-hidden="true">{icon}</span> : null}
      <span>{children}</span>
    </span>
  );
}
