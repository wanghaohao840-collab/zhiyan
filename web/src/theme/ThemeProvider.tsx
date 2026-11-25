import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
  type ReactNode,
} from "react";

import {
  applyThemeToDocument,
  normalizeTheme,
  persistTheme,
  readStoredTheme,
  THEME_STORAGE_KEY,
  type ZhiyanTheme,
} from "./theme";

export interface ThemeContextValue {
  theme: ZhiyanTheme;
  setTheme: (next: ZhiyanTheme) => void;
  toggleTheme: () => void;
}

const ThemeContext = createContext<ThemeContextValue | null>(null);

/**
 * 全局双主题上下文：支持 Style A（paper）与 Style B（midnight）瞬时无闪烁切换与多标签同步。
 */
export function ThemeProvider({ children }: { children: ReactNode }) {
  const [theme, setThemeState] = useState<ZhiyanTheme>(() => readStoredTheme());

  useEffect(() => {
    applyThemeToDocument(theme);
  }, [theme]);

  useEffect(() => {
    function handleStorage(event: StorageEvent) {
      if (event.key !== THEME_STORAGE_KEY) return;
      setThemeState(normalizeTheme(event.newValue));
    }
    window.addEventListener("storage", handleStorage);
    return () => window.removeEventListener("storage", handleStorage);
  }, []);

  const setTheme = useCallback((next: ZhiyanTheme) => {
    const normalized = normalizeTheme(next);
    // NOTE: 切换瞬间临时关闭过渡动画，避免卡片底色与文字颜色错拍闪动
    document.documentElement.classList.add("theme-switching");
    applyThemeToDocument(normalized);
    persistTheme(normalized);
    setThemeState(normalized);
    window.requestAnimationFrame(() => {
      window.requestAnimationFrame(() => {
        document.documentElement.classList.remove("theme-switching");
      });
    });
  }, []);

  const toggleTheme = useCallback(() => {
    setTheme(theme === "midnight" ? "paper" : "midnight");
  }, [setTheme, theme]);

  const value = useMemo(
    () => ({ theme, setTheme, toggleTheme }),
    [setTheme, theme, toggleTheme],
  );

  return <ThemeContext.Provider value={value}>{children}</ThemeContext.Provider>;
}

/**
 * 读取或切换当前主题；若在无 ThemeProvider 的孤立单元测试中调用，自动退化为本地受控状态。
 */
export function useTheme(): ThemeContextValue {
  const context = useContext(ThemeContext);
  const [fallbackTheme, setFallbackTheme] = useState<ZhiyanTheme>(() => readStoredTheme());

  const fallbackSetTheme = useCallback((next: ZhiyanTheme) => {
    const normalized = normalizeTheme(next);
    applyThemeToDocument(normalized);
    persistTheme(normalized);
    setFallbackTheme(normalized);
  }, []);

  const fallbackToggle = useCallback(() => {
    fallbackSetTheme(fallbackTheme === "midnight" ? "paper" : "midnight");
  }, [fallbackSetTheme, fallbackTheme]);

  if (context) {
    return context;
  }

  return {
    theme: fallbackTheme,
    setTheme: fallbackSetTheme,
    toggleTheme: fallbackToggle,
  };
}
