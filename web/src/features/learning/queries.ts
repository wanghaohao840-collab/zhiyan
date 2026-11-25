import { useQuery } from "@tanstack/react-query";

import { useAuth } from "../../auth/AuthProvider";
import type { LearningTask, Page } from "./types";

export interface TodayLearningSummary {
  todayCount: number;
  todayHasMore: boolean;
  completedCount: number;
  completedHasMore: boolean;
  overdueCount: number;
  overdueHasMore: boolean;
  nextTask: LearningTask | null;
}

/**
 * 读取后端真实 `/api/v1/learning/today` 三个桶的今日任务摘要，供概览页展示，
 * 绝不编造未由服务端返回的指标或记忆片段计数。
 */
export function useTodayLearningSummary() {
  const auth = useAuth();
  const username = auth.status === "authenticated" ? auth.username : "anonymous";

  return useQuery<TodayLearningSummary>({
    queryKey: ["learning", username, "today-summary"],
    enabled: auth.status === "authenticated",
    gcTime: 0,
    retry: false,
    queryFn: async ({ signal }) => {
      const [todayPage, completedPage, overduePage] = await Promise.all([
        auth.request<Page<LearningTask>>("/api/v1/learning/today?bucket=today&limit=20", {
          signal,
          cache: "no-store",
        }),
        auth.request<Page<LearningTask>>("/api/v1/learning/today?bucket=completed&limit=20", {
          signal,
          cache: "no-store",
        }),
        auth.request<Page<LearningTask>>("/api/v1/learning/today?bucket=overdue&limit=20", {
          signal,
          cache: "no-store",
        }),
      ]);

      return {
        todayCount: todayPage.items.length,
        todayHasMore: Boolean(todayPage.next_cursor),
        completedCount: completedPage.items.length,
        completedHasMore: Boolean(completedPage.next_cursor),
        overdueCount: overduePage.items.length,
        overdueHasMore: Boolean(overduePage.next_cursor),
        nextTask: todayPage.items[0] ?? overduePage.items[0] ?? null,
      };
    },
  });
}
