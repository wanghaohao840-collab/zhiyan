export type ZhiyanTheme = "paper" | "midnight";

export const THEME_STORAGE_KEY = "zhiyan.ui.theme";

export const THEME_META_COLOR: Record<ZhiyanTheme, string> = {
  paper: "#F7F6F2",
  midnight: "#070C15",
};

/**
 * 归一化主题标识，默认保持 Style A（paper）以保证首屏与基线可控。
 */
export function normalizeTheme(value: unknown): ZhiyanTheme {
  return value === "midnight" ? "midnight" : "paper";
}

/**
 * 安全读取本地界面主题偏好，不涉及任何用户认证或敏感信息。
 */
export function readStoredTheme(): ZhiyanTheme {
  try {
    return normalizeTheme(window.localStorage.getItem(THEME_STORAGE_KEY));
  } catch {
    return "paper";
  }
}

/**
 * 将主题状态同步应用到 DOM 根节点、color-scheme 与移动端 theme-color 元标签。
 */
export function applyThemeToDocument(theme: ZhiyanTheme): void {
  if (typeof document === "undefined") return;
  const root = document.documentElement;
  root.dataset.theme = theme;
  root.style.colorScheme = theme === "midnight" ? "dark" : "light";
  const meta = document.querySelector('meta[name="theme-color"]');
  if (meta) {
    meta.setAttribute("content", THEME_META_COLOR[theme]);
  }
}

/**
 * 持久化界面主题偏好到本地存储，隐私模式或存储受限时静默降级。
 */
export function persistTheme(theme: ZhiyanTheme): void {
  try {
    window.localStorage.setItem(THEME_STORAGE_KEY, theme);
  } catch {
    // NOTE: 存储不可用时保留当前会话内存态，不影响页面正常切换
  }
}
