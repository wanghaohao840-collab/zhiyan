import { type MouseEvent, useRef, useState } from "react";
import { Outlet } from "react-router-dom";

import { useAuth } from "../auth/AuthProvider";
import { MobileBottomNav } from "../components/MobileBottomNav/MobileBottomNav";
import { MoreDrawer } from "../components/MoreDrawer/MoreDrawer";
import { Sidebar } from "../components/Sidebar/Sidebar";
import { TopBar } from "../components/TopBar/TopBar";
import "../styles/app-shell.css";

export function AppShell() {
  const auth = useAuth();
  const mainRef = useRef<HTMLElement>(null);
  const [moreOpen, setMoreOpen] = useState(false);
  const [sidebarCollapsed, setSidebarCollapsed] = useState(false);
  const [drawerTrigger, setDrawerTrigger] = useState<HTMLButtonElement | null>(null);
  const username = auth.status === "authenticated" ? auth.username : "用户";

  function openMore(event: MouseEvent<HTMLButtonElement>) {
    setDrawerTrigger(event.currentTarget);
    setMoreOpen(true);
  }

  function closeMore() {
    setMoreOpen(false);
  }

  function logout() {
    void auth.logout().catch(() => undefined);
  }

  function skipToMain(event: MouseEvent<HTMLAnchorElement>) {
    event.preventDefault();
    mainRef.current?.focus();
  }

  return (
    <div className={`app-shell${sidebarCollapsed ? " app-shell--collapsed" : ""}`}>
      <a className="skip-link" href="#main-content" onClick={skipToMain}>
        跳到主要内容
      </a>
      <Sidebar
        collapsed={sidebarCollapsed}
        isLogoutPending={auth.isLogoutPending}
        onLogout={logout}
        onToggleCollapse={() => setSidebarCollapsed((current) => !current)}
      />
      <MobileBottomNav moreOpen={moreOpen} onOpenMore={openMore} />
      <div className="app-shell__body">
        <TopBar username={username} moreOpen={moreOpen} onOpenMore={openMore} />
        <main id="main-content" ref={mainRef} className="app-shell__main" tabIndex={-1}>
          <div className="app-shell__content">
            <Outlet />
          </div>
        </main>
      </div>
      <MoreDrawer
        open={moreOpen}
        returnFocusTo={drawerTrigger}
        username={username}
        isLogoutPending={auth.isLogoutPending}
        onClose={closeMore}
        onLogout={logout}
      />
    </div>
  );
}
