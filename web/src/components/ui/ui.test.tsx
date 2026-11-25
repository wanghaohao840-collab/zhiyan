import { act, fireEvent, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, useLocation } from "react-router-dom";
import { describe, expect, it, vi } from "vitest";

import { ThemeProvider } from "../../theme/ThemeProvider";
import {
  CommandPalette,
  EmptyState,
  ErrorBoundary,
  OfflineBanner,
  OverlayDialog,
  PageHeader,
  PaneResizer,
  Skeleton,
  StatusBadge,
  ThemeSwitcher,
  ToastProvider,
  useToast,
} from "./index";

describe("shared UI primitives", () => {
  it("renders PageHeader with semantic heading, description and actions", () => {
    render(
      <PageHeader
        eyebrow="学习闭环"
        title="学习中心"
        description="把阅读安排成可执行的小任务。"
        actions={<button type="button">新建计划</button>}
      />,
    );

    expect(screen.getByRole("heading", { name: "学习中心", level: 1 })).toBeVisible();
    expect(screen.getByText("把阅读安排成可执行的小任务。")).toBeVisible();
    expect(screen.getByRole("button", { name: "新建计划" })).toBeVisible();
  });

  it("renders EmptyState and Skeleton with accessible status semantics", () => {
    render(
      <>
        <EmptyState title="暂无记录" description="当前没有任务。" />
        <Skeleton label="正在加载任务…" lines={3} />
        <StatusBadge tone="success">已完成</StatusBadge>
      </>,
    );

    expect(screen.getByText("当前没有任务。")).toBeVisible();
    expect(screen.getByRole("status")).toHaveTextContent("正在加载任务…");
    expect(screen.getByText("已完成").closest(".status-badge")).toHaveClass("status-badge--success");
  });

  it("traps focus inside OverlayDialog, closes on Escape, and restores focus", async () => {
    const user = userEvent.setup();
    const onClose = vi.fn();
    const trigger = document.createElement("button");
    trigger.textContent = "打开弹窗";
    document.body.appendChild(trigger);
    trigger.focus();

    const { rerender } = render(
      <OverlayDialog open title="确认操作" onClose={onClose} returnFocusTo={trigger}>
        <button type="button">主要操作</button>
      </OverlayDialog>,
    );

    expect(screen.getByRole("dialog", { name: "确认操作" })).toBeVisible();
    const closeButton = screen.getByRole("button", { name: "关闭确认操作" });
    const actionButton = screen.getByRole("button", { name: "主要操作" });
    expect(closeButton).toHaveFocus();

    await user.tab();
    expect(actionButton).toHaveFocus();
    await user.tab();
    expect(closeButton).toHaveFocus();

    await user.keyboard("{Escape}");
    expect(onClose).toHaveBeenCalledOnce();

    rerender(
      <OverlayDialog open={false} title="确认操作" onClose={onClose} returnFocusTo={trigger}>
        <button type="button">主要操作</button>
      </OverlayDialog>,
    );
    expect(trigger).toHaveFocus();
    trigger.remove();
  });

  it("switches between paper and midnight themes and persists preference cleanly", async () => {
    window.localStorage.removeItem("zhiyan.ui.theme");
    document.documentElement.dataset.theme = "paper";
    const user = userEvent.setup();
    render(
      <ThemeProvider>
        <ThemeSwitcher />
      </ThemeProvider>,
    );

    const paperButton = screen.getByRole("button", { name: "极简知识感" });
    const midnightButton = screen.getByRole("button", { name: "深色 AI 感" });
    expect(paperButton).toHaveAttribute("aria-pressed", "true");
    expect(midnightButton).toHaveAttribute("aria-pressed", "false");

    await user.click(midnightButton);
    expect(document.documentElement.dataset.theme).toBe("midnight");
    expect(window.localStorage.getItem("zhiyan.ui.theme")).toBe("midnight");
    expect(midnightButton).toHaveAttribute("aria-pressed", "true");

    await user.click(paperButton);
    expect(document.documentElement.dataset.theme).toBe("paper");
    expect(window.localStorage.getItem("zhiyan.ui.theme")).toBe("paper");
    window.localStorage.removeItem("zhiyan.ui.theme");
  });

  it("opens CommandPalette, filters commands, and passes quick search via memory state only", async () => {
    const user = userEvent.setup();
    const onClose = vi.fn();
    function LocationProbe() {
      const loc = useLocation();
      return (
        <output data-testid="cmdk-loc">
          {loc.pathname}
          {loc.search}|{JSON.stringify(loc.state)}
        </output>
      );
    }

    render(
      <MemoryRouter initialEntries={["/overview"]}>
        <ThemeProvider>
          <CommandPalette
            open
            onClose={onClose}
            onToggleSidebar={vi.fn()}
            sidebarCollapsed={false}
          />
          <LocationProbe />
        </ThemeProvider>
      </MemoryRouter>,
    );

    const input = screen.getByLabelText("搜索命令或页面");
    await user.type(input, "混合检索");
    await user.keyboard("{Enter}");

    expect(onClose).toHaveBeenCalledOnce();
    expect(screen.getByTestId("cmdk-loc")).toHaveTextContent('/search|{"prefillQuery":"混合检索"}');
  });

  it("queues Toast messages with undo action and supports dismissal", async () => {
    const user = userEvent.setup();
    const onUndo = vi.fn();
    function Trigger() {
      const toast = useToast();
      return (
        <button
          type="button"
          onClick={() =>
            toast.push({
              message: "已完成任务",
              tone: "success",
              action: { label: "撤销", onClick: onUndo },
            })
          }
        >
          触发提示
        </button>
      );
    }

    render(
      <ToastProvider>
        <Trigger />
      </ToastProvider>,
    );

    await user.click(screen.getByRole("button", { name: "触发提示" }));
    expect(screen.getByText("已完成任务")).toBeVisible();
    await user.click(screen.getByRole("button", { name: "撤销" }));
    expect(onUndo).toHaveBeenCalledOnce();
    expect(screen.queryByText("已完成任务")).not.toBeInTheDocument();
  });

  it("catches render exceptions in ErrorBoundary and responds to offline events", () => {
    const spy = vi.spyOn(console, "error").mockImplementation(() => undefined);
    function Crasher(): never {
      throw new Error("boom");
    }
    render(
      <>
        <OfflineBanner />
        <ErrorBoundary>
          <Crasher />
        </ErrorBoundary>
      </>,
    );
    expect(screen.getByRole("heading", { name: "页面暂时遇到意外" })).toBeVisible();
    expect(screen.getByRole("link", { name: "前往旧版" })).toHaveAttribute("href", "/legacy/");

    act(() => {
      window.dispatchEvent(new Event("offline"));
    });
    expect(screen.getByText(/当前网络连接已断开/)).toBeVisible();
    act(() => {
      window.dispatchEvent(new Event("online"));
    });
    expect(screen.getByText("网络已恢复连接。")).toBeVisible();
    spy.mockRestore();
  });

  it("adjusts and resets PaneResizer via keyboard and double-click", () => {
    const onChange = vi.fn();
    const onReset = vi.fn();
    render(
      <PaneResizer
        label="调节栏宽"
        value={240}
        min={180}
        max={340}
        direction={1}
        onChange={onChange}
        onReset={onReset}
      />,
    );
    const sep = screen.getByRole("separator", { name: "调节栏宽" });
    fireEvent.keyDown(sep, { key: "ArrowRight" });
    expect(onChange).toHaveBeenCalledWith(256);
    fireEvent.doubleClick(sep);
    expect(onReset).toHaveBeenCalledOnce();
  });
});
