import type { ReactNode } from "react";

import { ConstellationMark } from "./ConstellationMark";

export interface EmptyStateProps {
  title?: string;
  description: string;
  action?: ReactNode;
  markSize?: "sm" | "md";
  className?: string;
}

/**
 * 统一真实空状态原语：内置轻量 Knowledge Constellation SVG 标识，禁止用于虚构占位数据。
 */
export function EmptyState({
  title,
  description,
  action,
  markSize = "sm",
  className,
}: EmptyStateProps) {
  return (
    <div className={className ? `ui-empty-state ${className}` : "ui-empty-state"}>
      <ConstellationMark size={markSize} />
      {title ? <h3>{title}</h3> : null}
      <p>{description}</p>
      {action ? <div className="ui-empty-state__action">{action}</div> : null}
    </div>
  );
}
