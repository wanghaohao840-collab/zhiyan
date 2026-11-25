import { Menu, Search } from "lucide-react";
import { useState, type FormEvent, type MouseEvent } from "react";
import { useLocation, useNavigate } from "react-router-dom";

import { navigationItems } from "../../layout/navigation";
import { ThemeSwitcher } from "../ui";

type TopBarProps = {
  moreOpen: boolean;
  onOpenMore: (event: MouseEvent<HTMLButtonElement>) => void;
  username: string;
};

export function TopBar({ moreOpen, onOpenMore, username }: TopBarProps) {
  const location = useLocation();
  const navigate = useNavigate();
  const [quickQuery, setQuickQuery] = useState("");
  const currentItem = navigationItems.find((item) => item.path === location.pathname);
  const contextLabel = currentItem?.heading ?? "学习工作台";
  const avatarLabel = username.trim().slice(0, 1).toUpperCase() || "研";

  function handleQuickSearch(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const trimmed = quickQuery.trim();
    setQuickQuery("");
    // NOTE: 遵循文献检索安全契约，仅经由 React Router 内存 state 传递预填词，不写入 URL 或持久存储，也不自动提交
    navigate("/search", trimmed ? { state: { prefillQuery: trimmed } } : undefined);
  }

  return (
    <header className="topbar">
      <span className="topbar__desktop-title">{contextLabel}</span>
      <strong className="topbar__mobile-title">知研</strong>
      <div className="topbar__actions">
        <form
          className="topbar__search-trigger"
          role="search"
          aria-label="顶栏快捷检索"
          onSubmit={handleQuickSearch}
        >
          <Search size={15} strokeWidth={1.6} aria-hidden="true" />
          <input
            type="search"
            value={quickQuery}
            onChange={(event) => setQuickQuery(event.target.value)}
            placeholder="搜索文档、提问或内容…"
            aria-label="快捷检索文档或内容"
          />
        </form>
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
