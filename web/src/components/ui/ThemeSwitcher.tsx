import { Sparkles, SunMedium } from "lucide-react";

import { useTheme } from "../../theme/ThemeProvider";

export interface ThemeSwitcherProps {
  compact?: boolean;
  className?: string;
}

/**
 * 双气质主题实时切换控件：支持在 Style A（极简知识感）与 Style B（深色 AI 感）之间一键无刷新切换。
 */
export function ThemeSwitcher({ compact = false, className }: ThemeSwitcherProps) {
  const { theme, setTheme } = useTheme();

  return (
    <div
      className={[
        "theme-switcher",
        compact ? "theme-switcher--compact" : "",
        className ?? "",
      ]
        .filter(Boolean)
        .join(" ")}
      role="group"
      aria-label="界面风格切换"
    >
      <button
        type="button"
        className={`theme-switcher__option${theme === "paper" ? " is-active" : ""}`}
        aria-pressed={theme === "paper"}
        title="切换为 Style A · 极简知识感"
        onClick={() => setTheme("paper")}
      >
        <SunMedium size={15} strokeWidth={1.6} aria-hidden="true" />
        <span>{compact ? "极简" : "极简知识感"}</span>
      </button>
      <button
        type="button"
        className={`theme-switcher__option${theme === "midnight" ? " is-active" : ""}`}
        aria-pressed={theme === "midnight"}
        title="切换为 Style B · 深色 AI 感"
        onClick={() => setTheme("midnight")}
      >
        <Sparkles size={15} strokeWidth={1.6} aria-hidden="true" />
        <span>{compact ? "深色 AI" : "深色 AI 感"}</span>
      </button>
    </div>
  );
}
