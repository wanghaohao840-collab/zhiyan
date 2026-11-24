import type { ReactNode } from "react";

export interface PageHeaderProps {
  title: string;
  description?: string;
  eyebrow?: string;
  actions?: ReactNode;
  className?: string;
}

/**
 * 统一业务页头部原语：承载语义 h1 标题、副标题与右侧操作区。
 */
export function PageHeader({
  title,
  description,
  eyebrow,
  actions,
  className,
}: PageHeaderProps) {
  return (
    <header className={className ? `ui-page-header ${className}` : "ui-page-header"}>
      <div className="ui-page-header__copy">
        {eyebrow ? <span className="ui-page-header__eyebrow">{eyebrow}</span> : null}
        <h1>{title}</h1>
        {description ? <p>{description}</p> : null}
      </div>
      {actions ? <div className="ui-page-header__actions">{actions}</div> : null}
    </header>
  );
}
