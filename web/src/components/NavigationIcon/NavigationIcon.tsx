import {
  CalendarCheck2,
  Files,
  LayoutDashboard,
  LayoutGrid,
  LineChart,
  LogOut,
  MessageSquareText,
  NotebookPen,
  Search,
} from "lucide-react";

type NavigationIconProps = {
  path: string;
  size?: number;
};

/**
 * 统一使用 Lucide 线性图标（strokeWidth=1.6），保持全站导航视觉一致。
 */
export function NavigationIcon({ path, size = 18 }: NavigationIconProps) {
  const iconProps = {
    size,
    strokeWidth: 1.6,
    "aria-hidden": true as const,
    className: "nav-svg-icon",
  };

  switch (path) {
    case "/overview":
      return <LayoutDashboard {...iconProps} />;
    case "/documents":
      return <Files {...iconProps} />;
    case "/qa":
      return <MessageSquareText {...iconProps} />;
    case "/search":
      return <Search {...iconProps} />;
    case "/notes":
      return <NotebookPen {...iconProps} />;
    case "/insights":
      return <LineChart {...iconProps} />;
    case "/learning":
      return <CalendarCheck2 {...iconProps} />;
    case "/more":
      return <LayoutGrid {...iconProps} />;
    case "/logout":
    default:
      return <LogOut {...iconProps} />;
  }
}
