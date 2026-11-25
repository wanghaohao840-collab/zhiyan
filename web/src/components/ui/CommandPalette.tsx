import {
  FilePlus2,
  MessageSquarePlus,
  PanelLeft,
  Search,
  Sparkles,
  SunMedium,
} from "lucide-react";
import {
  useEffect,
  useMemo,
  useRef,
  useState,
  type KeyboardEvent,
  type ReactNode,
} from "react";
import { useNavigate } from "react-router-dom";

import { navigationItems } from "../../layout/navigation";
import { prefetchRoute } from "../../pages/routeLoaders";
import { useTheme } from "../../theme/ThemeProvider";
import { NavigationIcon } from "../NavigationIcon/NavigationIcon";

export interface CommandPaletteProps {
  open: boolean;
  onClose: () => void;
  onToggleSidebar: () => void;
  sidebarCollapsed: boolean;
}

interface CommandItem {
  id: string;
  group: "快捷检索" | "前往工作区" | "快捷操作";
  label: string;
  keywords: string;
  hint?: string;
  icon: ReactNode;
  onSelect: () => void;
  onHighlight?: () => void;
}

/**
 * 全局命令面板（⌘K / Ctrl+K）：遵循 Emil Kowalski 高频键盘动作零入场动画法则（0ms 瞬开瞬关），
 * 支持模糊过滤、键盘上下导航、切页、切双主题、折叠侧栏以及纯内存预填跳转文献检索。
 */
export function CommandPalette({
  open,
  onClose,
  onToggleSidebar,
  sidebarCollapsed,
}: CommandPaletteProps) {
  const navigate = useNavigate();
  const { theme, setTheme } = useTheme();
  const [query, setQuery] = useState("");
  const [activeIndex, setActiveIndex] = useState(0);
  const inputRef = useRef<HTMLInputElement>(null);
  const previousFocusRef = useRef<HTMLElement | null>(null);

  useEffect(() => {
    if (!open) return;
    previousFocusRef.current =
      document.activeElement instanceof HTMLElement ? document.activeElement : null;
    setQuery("");
    setActiveIndex(0);
    inputRef.current?.focus();
    const priorOverflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    return () => {
      document.body.style.overflow = priorOverflow;
      previousFocusRef.current?.focus();
    };
  }, [open]);

  const trimmed = query.trim();

  const commands = useMemo<CommandItem[]>(() => {
    const items: CommandItem[] = [];
    if (trimmed) {
      items.push({
        id: "quick-search",
        group: "快捷检索",
        label: `在文献检索中查找“${trimmed}”`,
        keywords: trimmed,
        hint: "Enter",
        icon: <Search size={16} strokeWidth={1.6} aria-hidden="true" />,
        onHighlight: () => prefetchRoute("/search"),
        onSelect: () => {
          navigate("/search", { state: { prefillQuery: trimmed } });
          onClose();
        },
      });
    }

    for (const nav of navigationItems) {
      items.push({
        id: `nav-${nav.path}`,
        group: "前往工作区",
        label: nav.label,
        keywords: `${nav.label} ${nav.heading} ${nav.path}`,
        hint: nav.path,
        icon: <NavigationIcon path={nav.path} />,
        onHighlight: () => prefetchRoute(nav.path),
        onSelect: () => {
          navigate(nav.path);
          onClose();
        },
      });
    }

    items.push(
      {
        id: "action-new-qa",
        group: "快捷操作",
        label: "进入智能问答",
        keywords: "新建 对话 提问 qa",
        icon: <MessageSquarePlus size={16} strokeWidth={1.6} aria-hidden="true" />,
        onHighlight: () => prefetchRoute("/qa"),
        onSelect: () => {
          navigate("/qa");
          onClose();
        },
      },
      {
        id: "action-new-note",
        group: "快捷操作",
        label: "新建学习笔记",
        keywords: "新建 笔记 markdown note",
        icon: <FilePlus2 size={16} strokeWidth={1.6} aria-hidden="true" />,
        onHighlight: () => prefetchRoute("/notes"),
        onSelect: () => {
          navigate("/notes?note=new");
          onClose();
        },
      },
      {
        id: "action-theme-paper",
        group: "快捷操作",
        label: "切换为 Style A · 极简知识感",
        keywords: "主题 浅色 宣纸 极简 paper light",
        hint: theme === "paper" ? "当前" : undefined,
        icon: <SunMedium size={16} strokeWidth={1.6} aria-hidden="true" />,
        onSelect: () => {
          setTheme("paper");
          onClose();
        },
      },
      {
        id: "action-theme-midnight",
        group: "快捷操作",
        label: "切换为 Style B · 深色 AI 感",
        keywords: "主题 深色 夜间 暗色 midnight dark ai",
        hint: theme === "midnight" ? "当前" : undefined,
        icon: <Sparkles size={16} strokeWidth={1.6} aria-hidden="true" />,
        onSelect: () => {
          setTheme("midnight");
          onClose();
        },
      },
      {
        id: "action-toggle-sidebar",
        group: "快捷操作",
        label: sidebarCollapsed ? "展开桌面侧栏" : "收起桌面侧栏为图标轨",
        keywords: "侧栏 折叠 展开 sidebar",
        icon: <PanelLeft size={16} strokeWidth={1.6} aria-hidden="true" />,
        onSelect: () => {
          onToggleSidebar();
          onClose();
        },
      },
    );

    if (!trimmed) return items;
    const needle = trimmed.toLowerCase();
    return items.filter(
      (item) =>
        item.id === "quick-search" ||
        item.label.toLowerCase().includes(needle) ||
        item.keywords.toLowerCase().includes(needle),
    );
  }, [navigate, onClose, onToggleSidebar, setTheme, sidebarCollapsed, theme, trimmed]);

  const safeIndex = commands.length ? Math.min(activeIndex, commands.length - 1) : 0;

  useEffect(() => {
    commands[safeIndex]?.onHighlight?.();
  }, [commands, safeIndex]);

  if (!open) return null;

  function handleKeyDown(event: KeyboardEvent<HTMLDivElement>) {
    if (event.key === "Escape") {
      event.preventDefault();
      onClose();
      return;
    }
    if (!commands.length) return;
    if (event.key === "ArrowDown") {
      event.preventDefault();
      setActiveIndex((index) => (index + 1) % commands.length);
    } else if (event.key === "ArrowUp") {
      event.preventDefault();
      setActiveIndex((index) => (index - 1 + commands.length) % commands.length);
    } else if (event.key === "Enter") {
      event.preventDefault();
      commands[safeIndex]?.onSelect();
    }
  }

  return (
    <div className="cmdk-layer" role="presentation" onKeyDown={handleKeyDown}>
      <div className="cmdk-scrim" aria-hidden="true" onClick={onClose} />
      <section
        className="cmdk-panel"
        role="dialog"
        aria-modal="true"
        aria-label="全局命令面板"
      >
        <div className="cmdk-input-row">
          <Search size={16} strokeWidth={1.6} aria-hidden="true" />
          <input
            ref={inputRef}
            type="search"
            value={query}
            placeholder="输入页面名称、命令或要检索的关键词…"
            aria-label="搜索命令或页面"
            aria-autocomplete="list"
            aria-controls="cmdk-listbox"
            onChange={(event) => {
              setQuery(event.target.value);
              setActiveIndex(0);
            }}
          />
          <kbd className="cmdk-kbd">Esc</kbd>
        </div>
        <ul id="cmdk-listbox" className="cmdk-list" role="listbox" aria-label="可执行命令">
          {commands.length ? (
            commands.map((item, index) => (
              <li
                key={item.id}
                role="option"
                aria-selected={index === safeIndex}
                className={`cmdk-item${index === safeIndex ? " is-active" : ""}`}
                onMouseEnter={() => setActiveIndex(index)}
                onClick={item.onSelect}
              >
                <span className="cmdk-item__icon" aria-hidden="true">
                  {item.icon}
                </span>
                <span className="cmdk-item__label">{item.label}</span>
                <span className="cmdk-item__meta">{item.hint ?? item.group}</span>
              </li>
            ))
          ) : (
            <li className="cmdk-empty">未找到匹配命令</li>
          )}
        </ul>
      </section>
    </div>
  );
}
