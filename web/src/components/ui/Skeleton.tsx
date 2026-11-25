import { useEffect, useRef, useState } from "react";

export interface SkeletonProps {
  label?: string;
  lines?: number;
  delayMs?: number;
  className?: string;
}

/**
 * 延迟加载状态 Hook：防止 <150ms 快接口闪出骨架屏，且一旦显现至少保持 minDurationMs 避免视觉跳闪。
 */
export function useDelayedBusy(active: boolean, delayMs = 150, minDurationMs = 300): boolean {
  const [visible, setVisible] = useState(false);
  const shownAtRef = useRef<number>(0);

  useEffect(() => {
    if (active) {
      const timer = window.setTimeout(() => {
        shownAtRef.current = Date.now();
        setVisible(true);
      }, delayMs);
      return () => window.clearTimeout(timer);
    }
    if (!visible) return undefined;
    const elapsed = Date.now() - shownAtRef.current;
    const remaining = Math.max(0, minDurationMs - elapsed);
    if (remaining === 0) {
      setVisible(false);
      return undefined;
    }
    const timer = window.setTimeout(() => setVisible(false), remaining);
    return () => window.clearTimeout(timer);
  }, [active, delayMs, minDurationMs, visible]);

  return visible;
}

/**
 * 柔和暖石/深空骨架屏原语：立即向读屏器暴露真实状态文案，并通过可配置的视觉显现延迟
 * （默认 150ms）避免高速接口下的瞬间闪动；在 prefers-reduced-motion 下自动静止。
 */
export function Skeleton({
  label = "正在加载…",
  lines = 3,
  delayMs = 150,
  className,
}: SkeletonProps) {
  const safeLines = Math.max(1, Math.min(lines, 6));
  const style = delayMs > 0 ? { animationDelay: `${delayMs}ms` } : undefined;

  return (
    <div
      className={className ? `ui-skeleton ${className}` : "ui-skeleton"}
      style={style}
      role="status"
      aria-live="polite"
    >
      <span className="sr-only">{label}</span>
      {Array.from({ length: safeLines }, (_, index) => (
        <span
          key={index}
          aria-hidden="true"
          className={`ui-skeleton__line${index === safeLines - 1 ? " ui-skeleton__line--short" : ""}`}
        />
      ))}
    </div>
  );
}
