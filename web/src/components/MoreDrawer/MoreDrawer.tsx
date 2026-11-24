import { X } from "lucide-react";
import { NavigationIcon } from "../NavigationIcon/NavigationIcon";
import { type KeyboardEvent, useEffect, useRef } from "react";
import { NavLink } from "react-router-dom";

import { navigationItems } from "../../layout/navigation";
import { Button } from "../Button/Button";

type MoreDrawerProps = {
  isLogoutPending: boolean;
  onClose: () => void;
  onLogout: () => void;
  open: boolean;
  returnFocusTo: HTMLButtonElement | null;
  username: string;
};

const focusableSelector = "a[href], button:not([disabled])";

export function MoreDrawer({
  isLogoutPending,
  onClose,
  onLogout,
  open,
  returnFocusTo,
  username,
}: MoreDrawerProps) {
  const drawerRef = useRef<HTMLDivElement>(null);
  const firstActionRef = useRef<HTMLAnchorElement>(null);
  const wasOpenRef = useRef(false);

  useEffect(() => {
    if (open) {
      wasOpenRef.current = true;
      firstActionRef.current?.focus();
      const previousOverflow = document.body.style.overflow;
      document.body.style.overflow = "hidden";
      return () => {
        document.body.style.overflow = previousOverflow;
      };
    }

    if (wasOpenRef.current) {
      wasOpenRef.current = false;
      returnFocusTo?.focus();
    }
  }, [open, returnFocusTo]);

  if (!open) {
    return null;
  }

  function handleKeyDown(event: KeyboardEvent<HTMLDivElement>) {
    if (event.key === "Escape") {
      event.preventDefault();
      onClose();
      return;
    }
    if (event.key !== "Tab") {
      return;
    }

    const focusable = Array.from(
      drawerRef.current?.querySelectorAll<HTMLElement>(focusableSelector) ?? [],
    );
    const first = focusable[0];
    const last = focusable.at(-1);
    if (!first || !last) {
      event.preventDefault();
      return;
    }
    if (event.shiftKey && document.activeElement === first) {
      event.preventDefault();
      last.focus();
    } else if (!event.shiftKey && document.activeElement === last) {
      event.preventDefault();
      first.focus();
    }
  }

  return (
    <div className="drawer-layer">
      <button
        type="button"
        className="drawer-backdrop"
        aria-label="关闭更多"
        tabIndex={-1}
        onClick={onClose}
      />
      <div
        id="more-drawer"
        ref={drawerRef}
        className="more-drawer"
        role="dialog"
        aria-modal="true"
        aria-labelledby="more-drawer-title"
        onKeyDown={handleKeyDown}
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
        <div className="more-drawer__account">
          <p><strong>{username}</strong></p>
          <Button hierarchy="ghost" onClick={onClose}>账户</Button>
          <Button
            hierarchy="danger"
            loading={isLogoutPending}
            onClick={onLogout}
          >
            退出登录
          </Button>
        </div>
      </div>
    </div>
  );
}
