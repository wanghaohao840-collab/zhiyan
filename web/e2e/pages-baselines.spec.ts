import AxeBuilder from "@axe-core/playwright";
import type { Page } from "@playwright/test";

import {
  escapeRegExp,
  expect,
  normalizeOverviewGreeting,
  notesTest as test,
  registerUser,
  settleVisuals,
} from "./fixtures";

async function assertAccessibleAndNoOverflow(page: Page, label: string) {
  await settleVisuals(page);
  const widths = await page.evaluate(() => ({
    client: document.documentElement.clientWidth,
    scroll: document.documentElement.scrollWidth,
  }));
  expect(widths.scroll, `${label} horizontal overflow`).toBeLessThanOrEqual(widths.client + 1);
  const axe = await new AxeBuilder({ page })
    .withTags(["wcag2a", "wcag2aa", "wcag21a", "wcag21aa"])
    .analyze();
  const severe = axe.violations.filter(
    (item) => item.impact === "serious" || item.impact === "critical",
  );
  expect(severe, JSON.stringify(severe, null, 2)).toEqual([]);
}

test("captures deterministic baselines for overview, search, insights and learning", async ({
  appUrl,
  page,
}, testInfo) => {
  test.slow();
  // NOTE: 每个 E2E 测试运行在独立临时数据库内，使用固定用户名确保视觉快照跨运行确定性
  const username = `pages_base_${testInfo.project.name}`;
  await registerUser(page, appUrl, username);

  await expect(
    page.getByRole("heading", {
      level: 1,
      name: new RegExp(`^(早上好|下午好|晚上好)，${escapeRegExp(username)}$`),
    }),
  ).toBeVisible();
  await expect(page.getByRole("region", { name: "今日学习任务" })).toBeVisible();
  await expect(page.getByText("活跃占比 · 0/30 个活跃日")).toBeVisible();
  await normalizeOverviewGreeting(page, "早上好");
  await assertAccessibleAndNoOverflow(page, "overview");
  await expect(page).toHaveScreenshot("overview-default.png");

  await page.goto(`${appUrl}/search`);
  await expect(page.getByRole("heading", { level: 1, name: "文献检索" })).toBeVisible();
  await assertAccessibleAndNoOverflow(page, "search");
  await expect(page).toHaveScreenshot("search-default.png");

  await page.goto(`${appUrl}/insights`);
  await expect(page.getByRole("heading", { level: 1, name: "学习洞察" })).toBeVisible();
  await expect(page.getByText("近 30 天活动")).toBeVisible();
  await assertAccessibleAndNoOverflow(page, "insights");
  await expect(page).toHaveScreenshot("insights-default.png");

  await page.goto(`${appUrl}/learning`);
  await expect(page.getByRole("heading", { level: 1, name: "学习中心" })).toBeVisible();
  await expect(page.getByText("当前没有任务。", { exact: true })).toBeVisible();
  await assertAccessibleAndNoOverflow(page, "learning");
  await expect(page).toHaveScreenshot("learning-default.png");
});
