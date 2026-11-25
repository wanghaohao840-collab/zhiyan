import { Menu, Search } from "lucide-react";
import type { MouseEvent } from "react";
import { Link, useLocation } from "react-router-dom";

import { navigationItems } from "../../layout/navigation";
import { ThemeSwitcher } from "../ui";

type TopBarProps = {
  moreOpen: boolean;
  onOpenMore: (event: MouseEvent<HTMLButtonElement>) => void;
  username: string;
};

export function TopBar({ moreOpen, onOpenMore, username }: TopBarProps) {
  const location = useLocation();
  const currentItem = navigationItems.find((item) => item.path === location.pathname);
  const contextLabel = currentItem?.heading ?? "学习工作台";
  const avatarLabel = username.trim().slice(0, 1).toUpperCase() || "研";

  return (
    <header className="topbar">
      <span className="topbar__desktop-title">{contextLabel}</span>
      <strong className="topbar__mobile-title">知研</strong>
      <div className="topbar__actions">
        <Link
          to="/search"
          className="topbar__search-trigger"
          aria-label="快捷前往文献检索"
        >
          <Search size={15} strokeWidth={1.6} aria-hidden="true" />
          <span>搜索文档、提问或内容…</span>
        </Link>
        <ThemeSwitcher compact className="topbar__theme-switcher" />
        <button
          type="button"
          className="topbar__more"
          aria-label="更多操作"
          aria-expanded={moreOpen}
          aria-controls="more-drawer"
          onClick={onOpenMore}
        >
          <Menu size={18} strokeWidth={1.6} aria-hidden="true" />
        </button>
        <span className="topbar__user" title={`当前账户：${username}`}>
          <span className="topbar__avatar" aria-hidden="true">
            {avatarLabel}
          </span>
          <span className="sr-only">当前账户：{username}</span>
        </span>
      </div>
    </header>
  );
}
