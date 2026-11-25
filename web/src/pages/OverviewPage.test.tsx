import { fireEvent, render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { computeActiveRate, getTimeGreeting, OverviewPage } from "./OverviewPage";

const useOverview = vi.fn();
const useTodayLearningSummary = vi.fn();
vi.mock("../auth/AuthProvider", () => ({
  useAuth: () => ({ status: "authenticated", username: "测试读者" }),
}));
vi.mock("../features/insights/queries", () => ({ useOverview: () => useOverview() }));
vi.mock("../features/learning/queries", () => ({
  useTodayLearningSummary: () => useTodayLearningSummary(),
}));

describe("OverviewPage", () => {
  beforeEach(() => {
    useOverview.mockReset();
    useTodayLearningSummary.mockReset().mockReturnValue({
      isPending: false,
      error: null,
      data: {
        todayCount: 0,
        todayHasMore: false,
        completedCount: 0,
        completedHasMore: false,
        overdueCount: 0,
        overdueHasMore: false,
        nextTask: null,
      },
    });
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it("computes real local time greetings and exact active rate", () => {
    expect(getTimeGreeting(8)).toBe("早上好");
    expect(getTimeGreeting(14)).toBe("下午好");
    expect(getTimeGreeting(21)).toBe("晚上好");
    expect(getTimeGreeting(2)).toBe("晚上好");
    expect(computeActiveRate(0, 30)).toBe(0);
    expect(computeActiveRate(6, 30)).toBe(20);
    expect(computeActiveRate(30, 30)).toBe(100);
    expect(computeActiveRate(5, 0)).toBe(0);
  });

  it("renders truthful empty actions, local greeting, active rate and today tasks", () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date(2026, 7, 15, 9, 30, 0));
    useOverview.mockReturnValue({
      isPending: false,
      error: null,
      data: {
        stats: {
          document_count: 0,
          completed_question_count: 0,
          note_count: 0,
          active_days: 0,
          report_count: 0,
          window_days: 30,
          activity: [],
        },
        recent_documents: [],
        recent_questions: [],
      },
    });
    render(
      <MemoryRouter>
        <OverviewPage />
      </MemoryRouter>,
    );
    expect(screen.getByRole("heading", { name: "早上好，测试读者" })).toBeVisible();
    expect(screen.getByText("0%")).toBeVisible();
    expect(screen.getByText("活跃占比 · 0/30 个活跃日")).toBeVisible();
    expect(screen.getByRole("region", { name: "今日学习任务" })).toBeVisible();
    expect(screen.getByText("还没有可学习的文档。")).toBeVisible();
    expect(screen.getByRole("link", { name: "开始问答" })).toHaveAttribute("href", "/qa");
  });

  it("renders returned document, question, active percentage and today task counts", () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date(2026, 7, 15, 20, 15, 0));
    useOverview.mockReturnValue({
      isPending: false,
      error: null,
      data: {
        stats: {
          document_count: 1,
          completed_question_count: 1,
          note_count: 0,
          active_days: 6,
          report_count: 0,
          window_days: 30,
          activity: [],
        },
        recent_documents: [
          { document_id: "doc", name: "真实文档.md", loaded_at: "2026-09-03T00:00:00Z" },
        ],
        recent_questions: [
          { question: "真实问题", asked_at: "2026-09-03T01:00:00Z", document_names: ["真实文档.md"] },
        ],
      },
    });
    useTodayLearningSummary.mockReturnValue({
      isPending: false,
      error: null,
      data: {
        todayCount: 3,
        todayHasMore: false,
        completedCount: 2,
        completedHasMore: false,
        overdueCount: 1,
        overdueHasMore: false,
        nextTask: {
          id: "t1",
          plan_id: "p1",
          document_id: "doc",
          due_date: "2026-09-03",
          phase: "阅读",
          title: "精读第一章核心结论",
          duration_minutes: 25,
          completed: false,
          completed_at: null,
          version: 1,
        },
      },
    });
    render(
      <MemoryRouter>
        <OverviewPage />
      </MemoryRouter>,
    );
    expect(screen.getByRole("heading", { name: "晚上好，测试读者" })).toBeVisible();
    expect(screen.getByText("20%")).toBeVisible();
    expect(screen.getByText("活跃占比 · 6/30 个活跃日")).toBeVisible();
    expect(screen.getByText("下一步：精读第一章核心结论")).toBeVisible();
    expect(screen.getAllByText("真实文档.md")).toHaveLength(2);
    expect(screen.getByText("真实问题")).toBeVisible();
  });

  it("announces loading without showing invented metrics", () => {
    useOverview.mockReturnValue({ isPending: true });
    render(
      <MemoryRouter>
        <OverviewPage />
      </MemoryRouter>,
    );
    expect(screen.getByRole("status")).toHaveTextContent("正在加载学习概览");
    expect(screen.queryByLabelText("学习指标")).not.toBeInTheDocument();
  });

  it("offers a retry after a failed overview request", () => {
    const refetch = vi.fn();
    useOverview.mockReturnValue({ isPending: false, error: new Error("offline"), refetch });
    render(
      <MemoryRouter>
        <OverviewPage />
      </MemoryRouter>,
    );
    expect(screen.getByRole("alert")).toHaveTextContent("概览加载失败");
    fireEvent.click(screen.getByRole("button", { name: "重新加载" }));
    expect(refetch).toHaveBeenCalledOnce();
  });
});
