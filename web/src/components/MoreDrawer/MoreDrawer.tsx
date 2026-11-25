import { X } from "lucide-react";
import { useRef } from "react";
import { NavLink } from "react-router-dom";

import { navigationItems } from "../../layout/navigation";
import { Button } from "../Button/Button";
import { NavigationIcon } from "../NavigationIcon/NavigationIcon";
import { OverlayDialog, ThemeSwitcher } from "../ui";

type MoreDrawerProps = {
  isLogoutPending: boolean;
  onClose: () => void;
  onLogout: () => void;
  open: boolean;
  returnFocusTo: HTMLButtonElement | null;
  username: string;
};

export function MoreDrawer({
  isLogoutPending,
  onClose,
  onLogout,
  open,
  returnFocusTo,
  username,
}: MoreDrawerProps) {
  const firstActionRef = useRef<HTMLAnchorElement>(null);

  return (
    <OverlayDialog
      open={open}
      hideHeader
      variant="drawer"
      panelId="more-drawer"
      ariaLabel="更多"
      layerClassName="drawer-layer"
      scrimClassName="drawer-backdrop"
      scrimLabel="关闭更多"
      panelClassName="more-drawer"
      initialFocusRef={firstActionRef}
      returnFocusTo={returnFocusTo}
      onClose={onClose}
    >
      <header className="more-drawer__header">
        <h2 id="more-drawer-title">更多</h2>
        <button type="button" aria-label="关闭更多" onClick={onClose}>
          <X size={18} strokeWidth={1.6} aria-hidden="true" />
        </button>
      </header>
      <nav className="more-drawer__nav" aria-label="更多导航">
        {navigationItems.slice(4).map((item, index) => (
          <NavLink
            key={item.path}
            ref={index === 0 ? firstActionRef : undefined}
            to={item.path}
            end
            onClick={onClose}
            className={({ isActive }) =>
              `more-drawer__item${isActive ? " more-drawer__item--active" : ""}`
            }
          >
            <NavigationIcon path={item.path} />
            <span>{item.label}</span>
          </NavLink>
        ))}
      </nav>
      <div className="more-drawer__theme">
        <span className="more-drawer__section-label">界面风格</span>
        <ThemeSwitcher />
      </div>
      <div className="more-drawer__account">
        <p>
          <strong>{username}</strong>
        </p>
        <Button hierarchy="ghost" onClick={onClose}>
          账户
        </Button>
        <Button hierarchy="danger" loading={isLogoutPending} onClick={onLogout}>
          退出登录
        </Button>
      </div>
    </OverlayDialog>
  );
}
