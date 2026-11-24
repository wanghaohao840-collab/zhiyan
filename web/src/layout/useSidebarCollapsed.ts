import { useCallback, useState } from "react";

const SIDEBAR_COLLAPSED_STORAGE_KEY = "zhiyan.ui.sidebarCollapsed";

function readInitialSidebarCollapsed(): boolean {
  try {
    return window.localStorage.getItem(SIDEBAR_COLLAPSED_STORAGE_KEY) === "true";
  } catch {
    return false;
  }
}

/**
 * 管理桌面端侧栏 228px / 68px 折叠状态，仅保存纯界面布尔偏好，不涉及任何认证或敏感信息。
 */
export function useSidebarCollapsed() {
  const [collapsed, setCollapsed] = useState<boolean>(readInitialSidebarCollapsed);

  const toggle = useCallback(() => {
    setCollapsed((current) => {
      const next = !current;
      try {
        window.localStorage.setItem(SIDEBAR_COLLAPSED_STORAGE_KEY, String(next));
      } catch {
        // NOTE: 隐私模式或存储受限时静默回退到内存态，不影响页面交互
      }
      return next;
    });
  }, []);

  return { collapsed, toggle };
}
