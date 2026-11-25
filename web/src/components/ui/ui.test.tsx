import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { ThemeProvider } from "../../theme/ThemeProvider";
import { EmptyState, OverlayDialog, PageHeader, Skeleton, StatusBadge, ThemeSwitcher } from "./index";

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
});
