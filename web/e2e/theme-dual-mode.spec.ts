import AxeBuilder from "@axe-core/playwright";
import type { Page } from "@playwright/test";

import {
  escapeRegExp,
  expect,
  normalizeOverviewGreeting,
  notesTest as test,
  openMore,
  registerUser,
  settleVisuals,
} from "./fixtures";

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
    // 验证现代浏览器优先加载 AVIF 主图，不下载大体积兜底 PNG/JPG
    const currentSrc = await page
      .locator(".auth-intro__art")
      .evaluate((image) => (image as HTMLImageElement).currentSrc);
    expect(currentSrc).toMatch(/\.avif($|\?)/);
  }
  await assertMidnightQuality(page, "midnight-login");
  await expect(page).toHaveScreenshot("midnight-login.png");

  // NOTE: 刷新后验证零 FOUC 持久保持 Style B 深色 AI 感
  await page.reload();
  await expect(page.locator("html")).toHaveAttribute("data-theme", "midnight");

  const username = `midnight_user_${testInfo.project.name}`;
  await registerUser(page, appUrl, username);
  await expect(
    page.getByRole("heading", {
      level: 1,
      name: new RegExp(`^(早上好|下午好|晚上好)，${escapeRegExp(username)}$`),
    }),
  ).toBeVisible();
  await expect(page.getByRole("region", { name: "今日学习任务" })).toBeVisible();
  await expect(page.getByText("活跃占比 · 0/30 个活跃日")).toBeVisible();
  // Style B 参考图右版对应「晚上好」，规范化时段前缀消除跨小时像素漂移
  await normalizeOverviewGreeting(page, "晚上好");
  await assertMidnightQuality(page, "midnight-overview");
  await expect(page).toHaveScreenshot("midnight-overview.png");

  // 桌面端验证全局命令面板 (Ctrl+K) 与顶栏快捷检索回车直达 /search 且不写入 URL query
  if (testInfo.project.name === "desktop") {
    await page.keyboard.press("Control+k");
    const palette = page.getByRole("dialog", { name: "命令面板" });
    await expect(palette).toBeVisible();
    await page.keyboard.press("Escape");
    await expect(palette).toBeHidden();

    const quickSearch = page.getByLabel("快捷检索文档或内容");
    await quickSearch.fill("深色 AI 感");
    await quickSearch.press("Enter");
    await expect(page).toHaveURL(`${appUrl}/search`);
    await expect(page.getByLabel("你想从这些资料中找到什么？")).toHaveValue("深色 AI 感");
  }

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
    page.getByText("这是用于 Notes 保存来源验收的确定性回答。"),
  ).toBeVisible({ timeout: 15_000 });
  await expect(page.getByRole("list", { name: "回答处理阶段" })).toBeVisible();
  // 验证 QA 对话头绑定文档范围可展开与收起
  const scopeToggle = page.getByRole("button", { name: /查看范围/ });
  await expect(scopeToggle).toBeVisible();
  await scopeToggle.click();
  const scopeList = page.getByRole("list", { name: "当前对话绑定文档范围" });
  await expect(scopeList).toBeVisible();
  await expect(scopeList).toContainText(filename);
  await page.getByRole("button", { name: /收起范围/ }).click();
  await expect(scopeList).toHaveCount(0);
  await assertMidnightQuality(page, "midnight-qa");
  if (testInfo.project.name !== "tablet") {
    await expect(page).toHaveScreenshot("midnight-qa.png");
  }

  // Style B 学习笔记端到端创建、Markdown 代码块/引用预览与固定像素基线
  await page.goto(`${appUrl}/notes?note=new`);
  await expect(page.getByLabel("概念")).toBeVisible();
  await page.getByLabel("概念").fill("深空玻璃学习笔记");
  await page.getByLabel("笔记标签").fill("深色主题, RAG");
  await page
    .getByLabel("笔记正文")
    .fill(
      "# 深空玻璃学习笔记\n\n> 知识在连接中持续沉淀。\n\n检索相关性得分满足 $s = \\alpha \\cdot v + (1 - \\alpha) \\cdot b$：\n\n```python\nrag_mode = 'hybrid'\n```\n",
    );
  await page.getByRole("tab", { name: "预览" }).click();
  await expect(page.locator(".markdown-preview pre")).toBeVisible();
  await expect(page.locator(".markdown-preview .katex").first()).toBeVisible();
  await expect(page.locator(".markdown-preview__code-lang")).toHaveText("python");
  await expect(page.locator(".markdown-preview__copy")).toBeVisible();
  await expect(page.locator(".markdown-preview .hljs-string")).toHaveText("'hybrid'");
  const noteCreated = page.waitForResponse(
    (response) =>
      response.url() === `${appUrl}/api/v1/notes` && response.request().method() === "POST",
  );
  await page.getByRole("button", { name: "保存笔记" }).click();
  const createdNote = (await (await noteCreated).json()) as { id: string };
  await expect(page).toHaveURL(new RegExp(`note=${createdNote.id}`));
  await expect
    .poll(async () => {
      const response = await page.request.get(`${appUrl}/api/v1/notes/${createdNote.id}`);
      return ((await response.json()) as { projection_state?: string }).projection_state;
    })
    .toBe("ready");
  // NOTE: 与 notes.spec.ts 一致，等投影收敛后重新打开笔记消除列表中的「正在同步记忆」瞬态，并切至预览验证深色 Markdown
  await page.goto(`${appUrl}/notes?note=${createdNote.id}`);
  await expect(page.getByText("正在同步记忆")).toHaveCount(0);
  await page.getByRole("tab", { name: "预览" }).click();
  await expect(page.locator(".markdown-preview pre")).toBeVisible();
  // 规范化笔记列表和编辑器中动态的保存时间，确保固定像素基线零漂移
  await page
    .locator(".notes-list time")
    .evaluateAll((items) => items.forEach((item) => (item.textContent = "最近更新 9/12 14:20")));
  await page
    .locator(".notes-editor__footer > p")
    .evaluateAll((items) => items.forEach((item) => (item.textContent = "最近保存于 9/12 14:20")));
  await assertMidnightQuality(page, "midnight-notes");
  if (testInfo.project.name !== "tablet") {
    await expect(page).toHaveScreenshot("midnight-notes.png");
  }

  // Phase 5 验收：校验 Search / Notes / Insights / Learning 四工作台在 Style B 下均无 Axe 违规与溢出
  for (const [route, label, readyHeading] of [
    ["/search", "midnight-search", "文献检索"],
    ["/insights", "midnight-insights", "学习洞察"],
    ["/learning", "midnight-learning", "学习中心"],
  ] as const) {
    await page.goto(`${appUrl}${route}`);
    await expect(page.getByRole("heading", { level: 1, name: readyHeading })).toBeVisible();
    await assertMidnightQuality(page, label);
  }

  // 移动端极限 320px 窄屏检验 Style B 下四个工作台零横向溢出与 WCAG 2.1 AA 合规
  if (testInfo.project.name === "mobile") {
    await page.setViewportSize({ width: 320, height: 844 });
    for (const [route, label, readyHeading] of [
      ["/search", "midnight-search-320", "文献检索"],
      ["/notes", "midnight-notes-320", "学习笔记"],
      ["/insights", "midnight-insights-320", "学习洞察"],
      ["/learning", "midnight-learning-320", "学习中心"],
    ] as const) {
      await page.goto(`${appUrl}${route}`);
      await expect(page.getByRole("heading", { level: 1, name: readyHeading })).toBeVisible();
      await assertMidnightQuality(page, label);
    }
    await page.setViewportSize({ width: 390, height: 844 });
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
