export interface SkeletonProps {
  label?: string;
  lines?: number;
  className?: string;
}

/**
 * 柔和暖石骨架屏原语：对屏幕阅读器暴露真实状态文案，并响应 prefers-reduced-motion 自动静止。
 */
export function Skeleton({
  label = "正在加载…",
  lines = 3,
  className,
}: SkeletonProps) {
  const safeLines = Math.max(1, Math.min(lines, 6));
  return (
    <div
      className={className ? `ui-skeleton ${className}` : "ui-skeleton"}
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
