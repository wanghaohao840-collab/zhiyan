import { PanelLeftClose, PanelLeftOpen } from "lucide-react";
import { NavLink } from "react-router-dom";

import { navigationItems } from "../../layout/navigation";
import { BrandMark } from "../BrandMark/BrandMark";
import { Button } from "../Button/Button";
import { NavigationIcon } from "../NavigationIcon/NavigationIcon";

type SidebarProps = {
  collapsed?: boolean;
  isLogoutPending: boolean;
  onLogout: () => void;
  onToggleCollapse?: () => void;
};

export function Sidebar({
  collapsed = false,
  isLogoutPending,
  onLogout,
  onToggleCollapse,
}: SidebarProps) {
  return (
    <aside
      className={`sidebar${collapsed ? " sidebar--collapsed" : ""}`}
      aria-label="应用侧栏"
    >
      <div className="sidebar__header">
        <div className="sidebar__brand">
          <BrandMark />
          <span className="sidebar__brand-copy">
            <strong>知研</strong>
            <small>让知识在时间中沉淀</small>
          </span>
        </div>
        {onToggleCollapse ? (
          <button
            type="button"
            className="sidebar__collapse"
            aria-label={collapsed ? "展开侧栏" : "收起侧栏"}
            aria-expanded={!collapsed}
            title={collapsed ? "展开侧栏" : "收起侧栏"}
            onClick={onToggleCollapse}
          >
            {collapsed ? (
              <PanelLeftOpen size={16} strokeWidth={1.6} aria-hidden="true" />
            ) : (
              <PanelLeftClose size={16} strokeWidth={1.6} aria-hidden="true" />
            )}
          </button>
        ) : null}
      </div>
      <nav className="sidebar__nav" aria-label="主导航">
        {navigationItems.map((item) => (
          <NavLink
            key={item.path}
            to={item.path}
            end
            aria-label={item.label}
            title={collapsed ? item.label : undefined}
            className={({ isActive }) =>
              `sidebar__link${isActive ? " sidebar__link--active" : ""}`
            }
          >
            <NavigationIcon path={item.path} />
            <span className="sidebar__label">{item.label}</span>
          </NavLink>
        ))}
      </nav>
      <Button
        className="sidebar__logout"
        hierarchy="ghost"
        loading={isLogoutPending}
        aria-label="退出登录"
        title={collapsed ? "退出登录" : undefined}
        onClick={onLogout}
      >
        <NavigationIcon path="/logout" />
        <span className="sidebar__label">退出登录</span>
      </Button>
    </aside>
  );
}
