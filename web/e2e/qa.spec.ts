import AxeBuilder from "@axe-core/playwright";
import type { Page } from "@playwright/test";

import { expect, qaTest as test, registerUser, settleVisuals, uniqueUsername } from "./fixtures";

async function createQaConversation(page: Page, appUrl: string, username: string) {
  const filename = `${username}.md`;
  await registerUser(page, appUrl, username);
  await page.goto(`${appUrl}/documents`);
  await page.locator(".document-toolbar").getByRole("button", { name: "导入文档" }).click();
  await page.getByLabel("选择文档").setInputFiles({
    name: filename,
    mimeType: "text/markdown",
    buffer: Buffer.from("# QA evidence\nDeterministic evidence for the product loop."),
  });
  const imported = page.waitForResponse((response) => response.url() === `${appUrl}/api/v1/imports` && response.request().method() === "POST");
  await page.getByRole("button", { name: "开始导入" }).click();
  expect((await imported).status()).toBe(202);
  const row = page.getByRole("listitem", { name: filename });
  await expect(row).toBeVisible({ timeout: 30_000 });
  await row.getByRole("button", { name: "开始问答" }).click();
  const dialog = page.getByRole("dialog", { name: "新建对话" });
  await expect(dialog.getByRole("checkbox", { name: filename })).toBeChecked();
  const created = page.waitForResponse((response) => response.url() === `${appUrl}/api/v1/qa/conversations` && response.request().method() === "POST");
  await dialog.getByRole("button", { name: "创建对话" }).click();
  expect((await created).status()).toBe(201);
  await expect(page).toHaveURL(/\/qa\?conversation=/);
  await expect(page.getByText("基于 1 篇文档")).toBeVisible();
  expect(await page.evaluate(() => window.scrollY)).toBe(0);
  return { filename, conversationId: new URL(page.url()).searchParams.get("conversation")! };
}

async function askAndWait(page: Page, question = "这篇文档的结论是什么？") {
  await page.getByLabel("向这些文档提问").fill(question);
  const answer = page.getByText("这是确定性的可信回答，可通过右侧引用核对来源。");
  await page.getByRole("button", { name: "发送" }).click();
  await expect(answer).toBeVisible({ timeout: 15_000 });
  return answer;
}

async function openSources(page: Page) {
  await page.getByRole("button", { name: "引用 1", exact: true }).click();
  await expect(page.getByText("这是由隔离测试适配器生成的可验证证据片段。").filter({ visible: true })).toBeVisible();
  await expect(page.getByText("[E2E-S1]").filter({ visible: true })).toBeVisible();
}

test("retries a failed summary from the browser and keeps the original instruction", async ({ appUrl, page }) => {
  test.slow();
  const { conversationId } = await createQaConversation(page, appUrl, uniqueUsername("qa_summary_retry"));
  const session = await page.request.get(`${appUrl}/api/v1/auth/session`);
  const { csrf_token } = await session.json();
  const instruction = "[incomplete-once] 请聚焦证据";
  const response = await page.request.post(`${appUrl}/api/v1/qa/conversations/${conversationId}/summary-jobs`, {
    headers: { "X-CSRF-Token": csrf_token },
    data: { instruction, client_request_id: crypto.randomUUID() },
  });
  expect(response.status()).toBe(202);
  const first = await response.json();
  await expect.poll(async () => {
    const result = await page.request.get(`${appUrl}/api/v1/qa/jobs/${first.job_id}`);
    return (await result.json()).status;
  }).toBe("failed");

  await page.reload();
  await expect(page.getByText("模型没有生成完整摘要。")).toBeVisible();
  const next = page.waitForResponse((item) => item.url().endsWith("/summary-retry"));
  await page.getByRole("button", { name: "重试摘要" }).click();
  const accepted = await next;
  expect(accepted.status()).toBe(202);
  const job = await accepted.json();
  expect(job.job_id).not.toBe(first.job_id);
  await expect(page.locator(".qa-summary-status")).toContainText("已完成", { timeout: 15_000 });
  await expect(page.getByText("这是持久化学习摘要，概括了当前固定文档范围。")).toBeVisible();
  const messages = page.getByRole("list", { name: "问答消息" });
  await expect(messages.getByText(instruction, { exact: true })).toHaveCount(2);
  await expect(page.getByText("已发起重试，请查看后续摘要。")).toBeVisible();
});

test("explains summary capacity failure and opens a new document scope", async ({ appUrl, page }, testInfo) => {
  test.slow();
  const { conversationId, filename } = await createQaConversation(page, appUrl, uniqueUsername("qa_capacity"));
  await page.route(`${appUrl}/api/v1/qa/conversations/${conversationId}/summary-jobs`, async (route) => {
    await route.continue({ postData: JSON.stringify({ ...route.request().postDataJSON(), instruction: "[capacity]" }) });
  });
  await page.getByRole("button", { name: "生成摘要" }).click();
  await expect(page.locator(".qa-summary-status")).toContainText("文档超出摘要容量");
  await expect(page.getByRole("alert")).toContainText("请用更少或更短的文档新建对话，再生成摘要。");
  await expect(page.getByRole("button", { name: "重试摘要" })).toHaveCount(0);
  await expect(page.getByText("QA_SUMMARY_CONTEXT_CAPACITY")).toHaveCount(0);
  await page.screenshot({ path: testInfo.outputPath("capacity.png") });

  if (testInfo.project.name !== "desktop") {
    await page.getByRole("button", { name: "对话", exact: true }).click();
  }
  await page.getByRole("button", { name: "新建对话" }).filter({ visible: true }).click();
  const picker = page.getByRole("dialog", { name: "新建对话" });
  await expect(picker.getByRole("checkbox", { name: filename })).toBeVisible();
});

test("shows and copies a summary graph reference", async ({ appUrl, page }, testInfo) => {
  test.slow();
  const { conversationId, filename } = await createQaConversation(page, appUrl, uniqueUsername("qa_graph_reference"));
  await page.context().grantPermissions(["clipboard-read", "clipboard-write"], { origin: appUrl });
  await page.route(`${appUrl}/api/v1/qa/conversations/${conversationId}/summary-jobs`, async (route) => {
    await route.continue({ postData: JSON.stringify({ ...route.request().postDataJSON(), instruction: "[graph-reference]" }) });
  });
  await page.getByRole("button", { name: "生成摘要" }).click();
  await page.getByRole("button", { name: "引用 2", exact: true }).click();
  const evidence = page.getByText("证据链中的图谱实体", { exact: true }).filter({ visible: true });
  await expect(evidence).toBeVisible();
  const source = page.getByRole("listitem").filter({ has: evidence });
  await expect(source).toContainText(`[G-0123456789] ${filename}`);
  await source.getByRole("button", { name: "复制引用 2" }).click();
  await expect(page.getByText("引用 2 已复制").filter({ visible: true })).toBeVisible();
  expect(await page.evaluate(() => navigator.clipboard.readText())).toBe(`[G-0123456789] ${filename}`);
  await page.screenshot({ path: testInfo.outputPath("graph-reference.png") });
});

test("copies a historical summary graph citation after reload", async ({ appUrl, page }, testInfo) => {
  test.slow();
  const { conversationId, filename } = await createQaConversation(page, appUrl, uniqueUsername("qa_history_graph"));
  const { csrf_token } = await (await page.request.get(`${appUrl}/api/v1/auth/session`)).json();
  const started = await page.request.post(`${appUrl}/api/v1/qa/conversations/${conversationId}/summary-jobs`, {
    headers: { "X-CSRF-Token": csrf_token },
    data: { client_request_id: crypto.randomUUID(), instruction: "[historical-graph-reference]" },
  });
  expect(started.status()).toBe(202);
  const { job_id } = await started.json();
  await expect.poll(async () => (await (await page.request.get(`${appUrl}/api/v1/qa/jobs/${job_id}`)).json()).status).toBe("completed");

  await page.reload();
  await page.context().grantPermissions(["clipboard-read", "clipboard-write"], { origin: appUrl });
  await page.getByRole("button", { name: "引用 2", exact: true }).click();
  await expect(page.getByText("这条历史引用没有保存证据片段。", { exact: true }).filter({ visible: true })).toBeVisible();
  await page.getByRole("button", { name: "复制引用 2", exact: true }).filter({ visible: true }).click();
  expect(await page.evaluate(() => navigator.clipboard.readText())).toBe(`[G-0123456789] ${filename}`);
  await page.screenshot({ path: testInfo.outputPath("historical-graph-reference.png") });
});

test("real server preserves answer, isolates users, cancels summary and deletes durably", async ({ appUrl, browser, page }, testInfo) => {
  test.slow();
  const ownerName = uniqueUsername("qa_owner");
  const { conversationId } = await createQaConversation(page, appUrl, ownerName);
  await askAndWait(page);
  await page.reload();
  await expect(page.getByText("这是确定性的可信回答，可通过右侧引用核对来源。")).toHaveCount(1);

  await page.getByRole("button", { name: "生成摘要" }).click();
  const summary = page.locator(".qa-summary-status");
  await expect(summary).toContainText("生成中", { timeout: 15_000 });
  await expect(summary.getByRole("button", { name: "取消生成" })).toBeVisible();

  await page.reload();

  await expect(summary).toContainText("生成中", { timeout: 15_000 });
  await expect(summary.getByRole("button", { name: "取消生成" })).toBeVisible();
  await expect(page.getByLabel("向这些文档提问")).toBeDisabled();
  await expect(page.getByRole("button", { name: "生成摘要" })).toBeDisabled();
  await summary.getByRole("button", { name: "取消生成" }).click();
  await expect(summary).toContainText(/已取消|已完成/, { timeout: 15_000 });

  const otherPage = await browser.newPage();
  try {
    await registerUser(otherPage, appUrl, uniqueUsername("qa_other"));
    const probe = await otherPage.request.get(`${appUrl}/api/v1/qa/conversations/${conversationId}`);
    expect(probe.status()).toBe(404);
    expect(await probe.text()).not.toContain(ownerName);
  } finally {
    await otherPage.close();
  }

  await page.context().grantPermissions(["clipboard-write"], { origin: appUrl });
  await openSources(page);
  await page.getByRole("button", { name: "复制引用 1" }).filter({ visible: true }).click();
  await expect(page.getByText("引用 1 已复制").filter({ visible: true })).toBeVisible();
  if (testInfo.project.name !== "desktop") {
    await page.getByRole("dialog", { name: "引用证据" }).getByRole("button", { name: "关闭引用证据" }).press("Escape");
  }

  if (testInfo.project.name === "desktop") {
    await page.getByRole("button", { name: "删除对话" }).click();
  } else {
    await page.getByRole("button", { name: "对话", exact: true }).click();
    await page.getByRole("dialog", { name: "选择对话" }).getByRole("button", { name: "删除当前对话" }).click();
  }
  const deletion = page.getByRole("dialog", { name: "永久删除对话" });
  await expect(deletion).toContainText("消息、引用、摘要和问答记忆");
  await deletion.getByRole("button", { name: "永久删除", exact: true }).click();
  await expect(deletion).toBeHidden({ timeout: 15_000 });
  await expect(page.getByRole("heading", { name: "从一个问题，开始理解" })).toBeVisible();
});

test("failed answer survives reload and retries without duplicating the turn", async ({ appUrl, page }, testInfo) => {
  test.slow();
  await createQaConversation(page, appUrl, uniqueUsername("qa_failure"));
  await page.getByLabel("向这些文档提问").fill("[fail-once] 请给出安全回答");
  await page.getByRole("button", { name: "发送" }).click();
  await expect(page.getByText("QA_ENGINE_UNAVAILABLE")).toBeVisible({ timeout: 15_000 });
  if (testInfo.project.name === "mobile") {
    await settleVisuals(page);
    await expect(page).toHaveScreenshot("qa-failure.png");
  }
  await page.reload();
  await expect(page.getByText("QA_ENGINE_UNAVAILABLE")).toBeVisible();
  await page.getByRole("button", { name: "重试回答" }).click();
  await expect(page.getByText("这是确定性的可信回答，可通过右侧引用核对来源。")).toHaveCount(1, { timeout: 15_000 });
  await expect(page.locator(".qa-message--user")).toHaveCount(1);
});

test("QA workspace has no serious accessibility violations", async ({ appUrl, page }, testInfo) => {
  test.slow();
  await createQaConversation(page, appUrl, uniqueUsername(`qa_axe_${testInfo.project.name}`));
  await askAndWait(page);
  await openSources(page);
  const results = await new AxeBuilder({ page }).withTags(["wcag2a", "wcag2aa", "wcag21a", "wcag21aa"]).analyze();
  const violations = results.violations.filter((item) => item.impact === "serious" || item.impact === "critical");
  expect(violations, JSON.stringify(violations, null, 2)).toEqual([]);
  if (testInfo.project.name !== "desktop") {
    const dialog = page.getByRole("dialog", { name: "引用证据" });
    await dialog.getByRole("button", { name: "关闭引用证据" }).press("Escape");
    await expect(page.getByRole("button", { name: "引用 1" })).toBeFocused();
  }
  expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(await page.evaluate(() => document.documentElement.clientWidth));
});

test("QA approved visual states", async ({ appUrl, page }, testInfo) => {
  test.slow();
  await createQaConversation(page, appUrl, `qa_visual_${testInfo.project.name}`);
  await askAndWait(page);
  await page.evaluate(() => window.scrollTo(0, 0));
  await expect(page.getByRole("heading", { level: 1, name: "智能问答" })).toBeInViewport();
  await expect(page.getByLabel("向这些文档提问")).toBeInViewport({ ratio: 1 });
  await settleVisuals(page);
  await expect(page).toHaveScreenshot("qa-default.png");

  if (testInfo.project.name === "desktop") {
    await page.getByRole("button", { name: "生成摘要" }).click();
    // The real worker may still be queued when the status bar first appears.
    await expect(page.locator(".qa-summary-status")).toContainText("starting · 0%", { timeout: 15_000 });
    await settleVisuals(page);
    await expect(page).toHaveScreenshot("qa-summary.png");
    await expect(page.locator(".qa-summary-status")).toContainText("已完成", { timeout: 15_000 });
    await page.getByRole("button", { name: "删除对话" }).click();
    await settleVisuals(page);
    await expect(page).toHaveScreenshot("qa-delete.png");
  } else {
    await openSources(page);
    await settleVisuals(page);
    await expect(page).toHaveScreenshot("qa-sources.png");
  }
});
