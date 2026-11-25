import AxeBuilder from "@axe-core/playwright";
import type { Page } from "@playwright/test";

import { expect, openMore, qaTest as test, registerUser, settleVisuals } from "./fixtures";

async function assertMidnightQuality(page: Page, label: string) {
  await settleVisuals(page);
  const state = await page.evaluate(() => ({
    theme: document.documentElement.dataset.theme,
    metaColor: document.querySelector('meta[name="theme-color"]')?.getAttribute("content"),
    client: document.documentElement.clientWidth,
    scroll: document.documentElement.scrollWidth,
  }));
  expect(state.theme, `${label} theme`).toBe("midnight");
  expect(state.metaColor, `${label} theme-color`).toBe("#070C15");
  expect(state.scroll, `${label} horizontal overflow`).toBeLessThanOrEqual(state.client + 1);
  const axe = await new AxeBuilder({ page })
    .withTags(["wcag2a", "wcag2aa", "wcag21a", "wcag21aa"])
    .analyze();
  const severe = axe.violations.filter(
    (item) => item.impact === "serious" || item.impact === "critical",
  );
  expect(severe, JSON.stringify(severe, null, 2)).toEqual([]);
}

test("switches cleanly between Style A (paper) and Style B (midnight) with zero FOUC and full accessibility", async ({
  appUrl,
  page,
}, testInfo) => {
  test.slow();
  await page.goto(`${appUrl}/login`);
  await expect(page.locator("html")).toHaveAttribute("data-theme", "paper");

  await page.getByRole("button", { name: "深色 AI 感" }).click();
  await expect(page.locator("html")).toHaveAttribute("data-theme", "midnight");
  if (testInfo.project.name !== "mobile") {
    await expect(page.locator(".auth-intro__art")).toBeVisible();
    await expect
      .poll(() =>
        page
          .locator(".auth-intro__art")
          .evaluate((image) => (image as HTMLImageElement).naturalWidth),
      )
      .toBeGreaterThan(0);
  }
  await assertMidnightQuality(page, "midnight-login");
  await expect(page).toHaveScreenshot("midnight-login.png");

  // NOTE: 刷新后验证零 FOUC 持久保持 Style B 深色 AI 感
  await page.reload();
  await expect(page.locator("html")).toHaveAttribute("data-theme", "midnight");

  const username = `midnight_user_${testInfo.project.name}`;
  await registerUser(page, appUrl, username);
  await expect(page.getByRole("heading", { level: 1, name: `你好，${username}` })).toBeVisible();
  await assertMidnightQuality(page, "midnight-overview");
  await expect(page).toHaveScreenshot("midnight-overview.png");

  await page.goto(`${appUrl}/documents`);
  await page.locator(".document-toolbar").getByRole("button", { name: "导入文档" }).click();
  const filename = `midnight-${testInfo.project.name}.md`;
  await page.getByLabel("选择文档").setInputFiles({
    name: filename,
    mimeType: "text/markdown",
    buffer: Buffer.from("# 深色 AI 感知识库\n\n通过 Memory 与 RAG 形成可追溯的长期知识沉淀。"),
  });
  await page.getByRole("button", { name: "开始导入" }).click();
  const row = page.locator("li.document-row").filter({ hasText: filename });
  await expect(row).toBeVisible({ timeout: 30_000 });
  // 校验文档库新增的真实类型分段筛选
  await page.getByRole("button", { name: "PDF", exact: true }).click();
  await expect(page.locator("li.document-row")).toHaveCount(0);
  await page.getByRole("button", { name: "全部", exact: true }).click();
  await expect(row).toBeVisible();
  // NOTE: 与 visual.spec.ts 一致，仅规范化实时上传时间戳片段，确保无更新模式回归零像素漂移
  await row
    .locator(".document-row__body > span:not(.document-row__mobile-status)")
    .evaluate((metadata) => {
      metadata.textContent =
        metadata.textContent?.replace(/ · [^·]+$/, " · 2026年8月15日 09:32") ?? "";
    });
  await assertMidnightQuality(page, "midnight-documents");
  if (testInfo.project.name !== "tablet") {
    await expect(page).toHaveScreenshot("midnight-documents.png");
  }

  await row.getByRole("button", { name: "开始问答" }).click();
  await page.getByRole("dialog", { name: "新建对话" }).getByRole("button", { name: "创建对话" }).click();
  await page.getByLabel("向这些文档提问").fill("这篇文档的结论是什么？");
  await page.getByRole("button", { name: "发送", exact: true }).click();
  await expect(
    page.getByText("这是确定性的可信回答，可通过右侧引用核对来源。"),
  ).toBeVisible({ timeout: 15_000 });
  await assertMidnightQuality(page, "midnight-qa");
  if (testInfo.project.name !== "tablet") {
    await expect(page).toHaveScreenshot("midnight-qa.png");
  }

  // 从应用内一键切回 Style A（极简知识感）
  if (testInfo.project.name === "mobile") {
    await openMore(page, testInfo.project.name);
    await page.getByRole("dialog", { name: "更多" }).getByRole("button", { name: "极简知识感" }).click();
  } else {
    await page.getByRole("button", { name: "极简" }).click();
  }
  await expect(page.locator("html")).toHaveAttribute("data-theme", "paper");
  expect(
    await page.evaluate(() =>
      document.querySelector('meta[name="theme-color"]')?.getAttribute("content"),
    ),
  ).toBe("#F7F6F2");
});
