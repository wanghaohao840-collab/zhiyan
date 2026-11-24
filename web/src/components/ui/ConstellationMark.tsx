export type ConstellationMarkProps = {
  className?: string;
  size?: "sm" | "md";
};

/**
 * 轻量知识星座几何标记，仅用于真实空状态与品牌氛围，不作为业务数据卡片背景装饰。
 */
export function ConstellationMark({
  className,
  size = "md",
}: ConstellationMarkProps) {
  const dimensions = size === "sm" ? { width: 88, height: 48 } : { width: 136, height: 72 };
  return (
    <svg
      viewBox="0 0 136 72"
      width={dimensions.width}
      height={dimensions.height}
      fill="none"
      className={["constellation-mark", className].filter(Boolean).join(" ")}
      aria-hidden="true"
    >
      <g stroke="var(--color-border-control)" strokeWidth="1.2" strokeLinecap="round" opacity="0.65">
        <path d="M18 50 48 26 84 36 116 18" />
        <path d="M48 26 62 56 84 36" strokeDasharray="3 4" />
        <path d="M84 36 106 54" strokeDasharray="3 4" />
      </g>
      <circle cx="18" cy="50" r="3.5" fill="var(--color-surface)" stroke="var(--color-brand-600)" strokeWidth="1.5" />
      <circle cx="48" cy="26" r="4.5" fill="var(--color-surface-brand)" stroke="var(--color-brand-700)" strokeWidth="1.6" />
      <circle cx="62" cy="56" r="3" fill="var(--color-surface)" stroke="var(--color-border-control)" strokeWidth="1.4" />
      <circle cx="84" cy="36" r="5" fill="var(--color-brand-600)" />
      <circle cx="116" cy="18" r="3.5" fill="var(--color-constellation-accent)" />
      <circle cx="106" cy="54" r="3" fill="var(--color-surface)" stroke="var(--color-brand-600)" strokeWidth="1.4" />
    </svg>
  );
}
