import AxeBuilder from "@axe-core/playwright";
import { spawnSync } from "node:child_process";
import { randomUUID } from "node:crypto";
import { resolve } from "node:path";
import type { APIRequestContext, BrowserContext, Page } from "@playwright/test";

import {
  expect,
  notesTest as test,
  registerUser,
  settleVisuals,
  uniqueUsername,
} from "./fixtures";
import { resolveRequiredPythonExecutable } from "./python-runtime";

type Session = { username: string; csrf_token: string };
type Note = {
  id: string;
  body_markdown: string;
  version: number;
  projection_state: "pending" | "ready" | "failed";
  sources: Array<{ deleted: boolean; excerpt_snapshot: string | null }>;
};
type NotePage = { items: Note[]; next_cursor: string | null };
type ImportBatch = { tasks: Array<{ document_id: string }> };

const repositoryRoot = resolve(import.meta.dirname, "../..");
const pythonExecutable = resolveRequiredPythonExecutable(repositoryRoot);

async function session(page: Page, appUrl: string): Promise<Session> {
  const response = await page.request.get(`${appUrl}/api/v1/auth/session`);
  expect(response.status()).toBe(200);
  return response.json() as Promise<Session>;
}

async function createNote(
  request: APIRequestContext,
  appUrl: string,
  csrfToken: string,
  index: number,
) {
  const response = await request.post(`${appUrl}/api/v1/notes`, {
    headers: { "X-CSRF-Token": csrfToken },
    data: {
      body_markdown: `# 分页笔记 ${index}\n\n用于验证不透明游标的正文 ${index}`,
      concept: `分页概念 ${index}`,
      tags: ["分页"],
      client_request_id: randomUUID(),
    },
  });
  expect(response.status()).toBe(201);
  return response.json() as Promise<Note>;
}

async function loginContext(
  context: BrowserContext,
  appUrl: string,
  username: string,
): Promise<Page> {
  const page = await context.newPage();
  await page.goto(`${appUrl}/login`);
  await page.getByLabel("用户名").fill(username);
  await page.getByLabel("密码", { exact: true }).fill("e2e-only-passphrase");
  await page.getByRole("button", { name: "登录", exact: true }).click();
  await expect(page).toHaveURL(`${appUrl}/overview`);
  return page;
}

async function waitForNote(
  request: APIRequestContext,
  appUrl: string,
  noteId: string,
  predicate: (note: Note) => boolean,
) {
  let latest: Note | undefined;
  await expect.poll(async () => {
    const response = await request.get(`${appUrl}/api/v1/notes/${noteId}`);
    if (response.status() !== 200) return false;
    latest = await response.json() as Note;
    return predicate(latest);
  }, { timeout: 20_000 }).toBe(true);
  return latest!;
}

function sqliteNumber(dbPath: string, sql: string, value: string): number {
  const script = [
    "import sqlite3,sys",
    "with sqlite3.connect(sys.argv[1]) as c:",
    " print(c.execute(sys.argv[2], (sys.argv[3],)).fetchone()[0])",
  ].join("\n");
  const result = spawnSync(pythonExecutable, ["-c", script, dbPath, sql, value], {
    cwd: repositoryRoot,
    encoding: "utf8",
    windowsHide: true,
  });
  expect(result.status, result.stderr).toBe(0);
  return Number(result.stdout.trim());
}

async function createManualNote(page: Page, appUrl: string, body = "# 检索目标\n\n**真实 Markdown** 正文") {
  const heading = body.match(/^#\s+(.+)$/m)?.[1] ?? "检索目标";
  await page.goto(`${appUrl}/notes?note=new`);
  await page.getByLabel("概念").fill("检索目标概念");
  await page.getByLabel("笔记标签").fill("核心, 验收");
  await page.getByLabel("笔记正文").fill(body);
  await page.getByRole("tab", { name: "预览" }).click();
  await expect(
    page.getByRole("region", { name: "笔记编辑器" }).getByRole("heading", { level: 1, name: heading }),
  ).toBeVisible();
  const responsePromise = page.waitForResponse((response) =>
    response.url() === `${appUrl}/api/v1/notes` && response.request().method() === "POST",
  );
  await page.getByRole("button", { name: "保存笔记" }).click();
  const response = await responsePromise;
  expect(response.status()).toBe(201);
  const note = await response.json() as Note;
  await expect(page).toHaveURL(new RegExp(`note=${note.id}`));
  return note;
}

async function importAndAsk(page: Page, appUrl: string, username: string) {
  const filename = `${username}-notes-source.md`;
  await page.goto(`${appUrl}/documents`);
  await page.locator(".document-toolbar").getByRole("button", { name: "导入文档" }).click();
  await page.getByLabel("选择文档").setInputFiles({
    name: filename,
    mimeType: "text/markdown",
    buffer: Buffer.from("# Notes source\nDeterministic source for Notes acceptance."),
  });
  const imported = page.waitForResponse((response) =>
    response.url() === `${appUrl}/api/v1/imports` && response.request().method() === "POST",
  );
  await page.getByRole("button", { name: "开始导入" }).click();
  const importResponse = await imported;
  expect(importResponse.status()).toBe(202);
  const batch = await importResponse.json() as ImportBatch;
  const row = page.locator("li.document-row").filter({ hasText: filename });
  await expect(row).toBeVisible({ timeout: 30_000 });
  await row.getByRole("button", { name: "开始问答" }).click();
  const dialog = page.getByRole("dialog", { name: "新建对话" });
  await expect(dialog.getByRole("checkbox", { name: filename })).toBeChecked();
  await dialog.getByRole("button", { name: "创建对话" }).click();
  await page.getByLabel("向这些文档提问").fill("请给出可保存为笔记的结论");
  await page.getByRole("button", { name: "发送" }).click();
  const answer = page.getByText("这是用于 Notes 保存来源验收的确定性回答。");
  await expect(answer).toBeVisible({ timeout: 15_000 });
  return { filename, documentId: batch.tasks[0].document_id, row };
}

test("real Notes lifecycle covers preview, reload, versioning, search, filters, cursor, conflict and tombstones", async ({ appUrl, appServer, browser, page }, testInfo) => {
  test.slow();
  const username = uniqueUsername(`notes_lifecycle_${testInfo.project.name}`);
  await registerUser(page, appUrl, username);
  const auth = await session(page, appUrl);
  const created = await createManualNote(page, appUrl);

  await page.reload();
  await expect(page.getByLabel("笔记正文")).toHaveValue(/真实 Markdown/);
  await page.getByRole("tab", { name: "编辑" }).click();
  await page.getByLabel("笔记正文").fill("# 检索目标\n\n版本二正文");
  const updatedResponse = page.waitForResponse((response) =>
    response.url().endsWith(`/api/v1/notes/${created.id}`) && response.request().method() === "PATCH",
  );
  await page.getByRole("button", { name: "保存笔记" }).click();
  expect((await updatedResponse).status()).toBe(200);
  expect((await page.request.get(`${appUrl}/api/v1/notes/${created.id}`)).status()).toBe(200);

  const secondContext = await browser.newContext();
  try {
    const stalePage = await loginContext(secondContext, appUrl, username);
    await stalePage.goto(`${appUrl}/notes?note=${created.id}`);
    await expect(stalePage.getByLabel("笔记正文")).toHaveValue(/版本二正文/);
    await page.getByLabel("笔记正文").fill("# 检索目标\n\n服务端版本三");
    await page.getByRole("button", { name: "保存笔记" }).click();
    const editor = page.getByRole("region", { name: "笔记编辑器" });
    await expect(editor.getByText(/最近保存于/)).toBeVisible();
    await expect(editor.getByRole("button", { name: "保存笔记" })).toBeDisabled();

    await stalePage.getByLabel("笔记正文").fill("# 检索目标\n\n必须保留的本地草稿");
    const conflictResponse = stalePage.waitForResponse((response) =>
      response.url().endsWith(`/api/v1/notes/${created.id}`) && response.request().method() === "PATCH",
    );
    await stalePage.getByRole("button", { name: "保存笔记" }).click();
    expect((await conflictResponse).status()).toBe(409);
    await expect(stalePage.getByRole("dialog", { name: "笔记已在其他窗口更新" })).toBeVisible();
    await expect(stalePage.getByLabel("笔记正文")).toHaveValue(/必须保留的本地草稿/);
  } finally {
    await secondContext.close();
  }

  for (let index = 0; index < 20; index += 1) {
    await createNote(page.request, appUrl, auth.csrf_token, index);
  }
  const firstPageResponse = await page.request.get(`${appUrl}/api/v1/notes?limit=20&sort=updated_desc`);
  const firstPage = await firstPageResponse.json() as NotePage;
  expect(firstPage.items).toHaveLength(20);
  expect(firstPage.next_cursor).toEqual(expect.any(String));
  expect(firstPage.next_cursor).not.toContain(firstPage.items.at(-1)!.id);

  await page.goto(`${appUrl}/notes`);
  await expect(page.getByRole("button", { name: "加载更多笔记" })).toBeVisible();
  await page.getByRole("button", { name: "加载更多笔记" }).click();
  await expect(page.locator(".notes-list__item")).toHaveCount(21);

  if (testInfo.project.name === "mobile") {
    await page.getByRole("button", { name: /^筛选/ }).click();
    await page.getByLabel("筛选中的搜索笔记").fill("服务端版本三");
    await page.getByRole("button", { name: "完成" }).click();
  } else {
    await page.getByLabel("搜索笔记").fill("服务端版本三");
    await page.getByLabel("搜索笔记").press("Enter");
  }
  await expect(page.locator(".notes-list__item")).toHaveCount(1);
  await expect(page.locator(".notes-list__item")).toContainText("服务端版本三");

  await page.goto(`${appUrl}/notes`);
  if (testInfo.project.name === "mobile") {
    await page.getByRole("button", { name: /^筛选/ }).click();
    await page.getByLabel("筛选中的标签").fill("核心");
    await page.getByRole("button", { name: "完成" }).click();
  } else {
    await page.getByLabel("按标签筛选").fill("核心");
    await page.getByLabel("按标签筛选").press("Enter");
  }
  await expect(page.locator(".notes-list__item")).toHaveCount(1);

  if (testInfo.project.name === "mobile") {
    await expect(
      page.getByRole("button", { name: "清空笔记", exact: true, includeHidden: true }),
    ).toBeHidden();
    const clearResponse = await page.request.post(`${appUrl}/api/v1/notes/clear`, {
      headers: { "X-CSRF-Token": auth.csrf_token },
      data: { confirmation: "清空全部笔记" },
    });
    expect(clearResponse.status()).toBe(200);
    await page.reload();
  } else {
    await page.getByLabel("更多笔记操作").click();
    await page.getByRole("button", { name: "清空笔记" }).click();
    const clearDialog = page.getByRole("dialog", { name: "清空全部笔记" });
    await expect(clearDialog).toContainText("不会删除文档");
    await clearDialog.getByRole("button", { name: "确认清空" }).click();
  }
  await expect(page.getByRole("heading", { name: "没有符合条件的笔记" })).toBeVisible();
  expect(sqliteNumber(appServer.dbPath, "select count(*) from notes where id=? and deleted_at is not null", created.id)).toBe(1);
});

test("completed QA citation saves a server-resolved source that is scrubbed after durable document deletion", async ({ appUrl, page }, testInfo) => {
  test.slow();
  const username = uniqueUsername(`notes_source_${testInfo.project.name}`);
  await registerUser(page, appUrl, username);
  const { filename, row } = await importAndAsk(page, appUrl, username);
  await page.getByRole("link", { name: "记录此引用" }).click();
  await expect.poll(() => page.url(), { timeout: 15_000 }).toMatch(/\/notes\?source_kind=qa_citation/);
  const prefill = new URL(page.url()).searchParams;
  expect(prefill.get("qa_message_id")).toBeTruthy();
  expect(prefill.get("citation_id")).toBe("NOTES-E2E-S1");
  await page.getByLabel("笔记正文").fill("# 来源笔记\n\n用户撰写的内容必须保留。");
  await page.getByLabel("概念").fill("来源保留");
  const savedRequest = page.waitForRequest((request) =>
    request.url() === `${appUrl}/api/v1/notes` && request.method() === "POST",
  );
  const saved = page.waitForResponse((response) =>
    response.url() === `${appUrl}/api/v1/notes` && response.request().method() === "POST",
  );
  await page.getByRole("button", { name: "保存笔记" }).click();
  expect((await savedRequest).postDataJSON()).toMatchObject({
    source: {
      kind: "qa_citation",
      qa_message_id: prefill.get("qa_message_id"),
      citation_id: "NOTES-E2E-S1",
    },
  });
  const note = await (await saved).json() as Note;
  if (testInfo.project.name !== "desktop") {
    await page.getByRole("button", { name: "来源", exact: true }).click();
  }
  await expect(page.getByText(filename).filter({ visible: true })).toBeVisible();
  await expect(page.getByText("这是 Notes 垂直切片的确定性来源片段。").filter({ visible: true })).toBeVisible();
  if (testInfo.project.name !== "desktop") {
    await page.getByRole("button", { name: "关闭来源", exact: true }).click();
  }

  await page.goto(`${appUrl}/documents`);
  const currentRow = page.locator("li.document-row").filter({ hasText: filename });
  await currentRow.getByRole("button", { name: `删除 ${filename}` }).click();
  const deleteResponse = page.waitForResponse((response) =>
    response.url().startsWith(`${appUrl}/api/v1/documents/`) && response.request().method() === "DELETE",
  );
  await page.getByRole("dialog", { name: `删除 ${filename}` }).getByRole("button", { name: "确认删除" }).click();
  expect((await deleteResponse).status()).toBe(202);
  await expect(row).toHaveCount(0);
  await waitForNote(page.request, appUrl, note.id, (value) => value.sources[0]?.deleted === true);

  await page.goto(`${appUrl}/notes?note=${note.id}`);
  await expect(page.getByLabel("笔记正文")).toHaveValue(/用户撰写的内容必须保留/);
  if (testInfo.project.name !== "desktop") {
    await page.getByRole("button", { name: "来源", exact: true }).click();
  }
  await expect(page.getByText("来源已删除").filter({ visible: true })).toBeVisible();
  await expect(page.getByText("这是 Notes 垂直切片的确定性来源片段。").filter({ visible: true })).toHaveCount(0);
});

test("failed projection is non-blocking and explicit retry converges", async ({ appUrl, page }, testInfo) => {
  test.slow();
  await registerUser(page, appUrl, uniqueUsername(`notes_projection_${testInfo.project.name}`));
  const note = await createManualNote(page, appUrl, "# 投影故障\n\n[projection-fail] 笔记仍然成功保存。");
  const failed = await waitForNote(page.request, appUrl, note.id, (value) => value.projection_state === "failed");
  expect(failed.body_markdown).toContain("笔记仍然成功保存");
  await page.reload();
  await expect(page.getByText("记忆投影失败，不影响笔记保存。")).toBeVisible();
  const retried = page.waitForResponse((response) =>
    response.url() === `${appUrl}/api/v1/notes/projections/retry` && response.request().method() === "POST",
  );
  await page.getByRole("button", { name: "重试投影" }).click();
  expect((await retried).status()).toBe(200);
  await waitForNote(page.request, appUrl, note.id, (value) => value.projection_state === "ready");
  await page.reload();
  await expect(page.getByText("记忆投影失败，不影响笔记保存。")).toHaveCount(0);
  await expect(page.getByLabel("笔记正文")).toHaveValue(/笔记仍然成功保存/);
});

test("Notes long content and multiple sources stay within the workspace", async ({ appUrl, page }, testInfo) => {
  await registerUser(page, appUrl, uniqueUsername(`notes_long_${testInfo.project.name}`));
  const title = "长期知识积累与跨文档理解".repeat(12);
  const fixture = {
    id: "layout-note", body_markdown: `# ${title}\n\n${"理解、证据与概念关系。".repeat(800)}`,
    concept: "跨文档理解", tags: ["长期积累", "RAG"], version: 1, projection_state: "ready",
    created_at: "2026-09-13T00:00:00Z", updated_at: "2026-09-13T00:00:00Z", deleted_at: null,
    sources: Array.from({ length: 8 }, (_, index) => ({
      id: `source-${index}`, kind: "document_chunk", deleted: false,
      qa_thread_id: null, qa_message_id: null, citation_id: `citation-${index}-${"abcdef".repeat(15)}`,
      document_id: `document-${index}`, locator: { page: index + 1 },
      title_snapshot: `${"LongDocumentNameWithoutSpaces".repeat(10)}.pdf`,
      excerpt_snapshot: "可追溯的文献证据。".repeat(35),
      created_at: "2026-09-13T00:00:00Z", source_deleted_at: null,
    })),
  };
  await page.route("**/api/v1/notes?*", (route) => route.fulfill({ json: { items: [fixture], next_cursor: null } }));
  await page.route("**/api/v1/notes/layout-note", (route) => route.fulfill({ json: fixture }));
  await page.goto(`${appUrl}/notes?note=layout-note`);
  await expect(page.getByLabel("笔记正文")).toHaveValue(fixture.body_markdown);
  await page.getByRole("tab", { name: "预览" }).click();
  await expect(page.getByRole("region", { name: "笔记编辑器" }).getByRole("heading", { name: title, exact: true })).toBeVisible();
  await page.getByRole("button", { name: "来源", exact: true }).click();
  const sourceCards = page.getByRole("dialog", { name: "笔记来源" }).locator(".notes-source-card");
  await expect(sourceCards).toHaveCount(8);
  expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(
    await page.evaluate(() => document.documentElement.clientWidth),
  );
  const overflow = await sourceCards.evaluateAll((cards) =>
    cards.filter((card) => card.scrollWidth > card.clientWidth + 1).length,
  );
  expect(overflow).toBe(0);
  await page.screenshot({ path: testInfo.outputPath("notes-long-content.png"), fullPage: true });
});

test("Notes approved visuals, accessibility, overflow and mobile primary targets", async ({ appUrl, page }, testInfo) => {
  test.slow();
  await registerUser(page, appUrl, uniqueUsername(`notes_visual_${testInfo.project.name}`));
  const note = await createManualNote(page, appUrl, "# 间隔复习\n\n用 **Markdown** 整理遗忘曲线与复习节奏。");
  // NOTE: 等待后台记忆投影收敛到稳定态，避免截图在 pending 与 ready 之间抖动
  await expect.poll(async () => {
    const response = await page.request.get(`${appUrl}/api/v1/notes/${note.id}`);
    return ((await response.json()) as { projection_state?: string }).projection_state;
  }).toBe("ready");
  await page.goto(`${appUrl}/notes`);
  await expect(page.locator(".notes-list__item")).toHaveCount(1);

  const axe = await new AxeBuilder({ page })
    .withTags(["wcag2a", "wcag2aa", "wcag21a", "wcag21aa"])
    .analyze();
  const violations = axe.violations.filter((item) => item.impact === "serious" || item.impact === "critical");
  expect(violations, JSON.stringify(violations, null, 2)).toEqual([]);
  expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(
    await page.evaluate(() => document.documentElement.clientWidth),
  );

  if (testInfo.project.name === "mobile") {
    const dimensions = await page.locator(".button--primary:visible").evaluateAll((buttons) =>
      buttons.map((button) => {
        const rect = button.getBoundingClientRect();
        return { width: rect.width, height: rect.height, text: button.textContent };
      }),
    );
    expect(dimensions.length).toBeGreaterThan(0);
    expect(dimensions.filter(({ width, height }) => width < 44 || height < 44), JSON.stringify(dimensions)).toEqual([]);
    await settleVisuals(page);
    await page.locator(".notes-list time").evaluateAll((items) => items.forEach((item) => item.textContent = "最近更新 9/12 14:20"));
    await page.locator(".notes-editor__footer > p").evaluateAll((items) => items.forEach((item) => item.textContent = "最近保存于 9/12 14:20"));
    await expect(page).toHaveScreenshot("notes-list.png");
    await page.locator(".notes-list__item").click();
    await expect(page).toHaveURL(new RegExp(`note=${note.id}`));
    await expect(page.getByRole("button", { name: "保存笔记", includeHidden: true })).toHaveCount(1);
    const editor = page.getByRole("region", { name: "笔记编辑器" });
    const saveButton = editor.getByRole("button", { name: "保存笔记" });
    const sourceButton = editor.getByRole("button", { name: "来源", exact: true });
    const deleteButton = editor.getByRole("button", { name: "删除笔记" });
    const tabList = editor.getByRole("tablist", { name: "笔记内容模式" });
    const mobileNav = page.locator(".mobile-nav");
    const [saveBox, sourceBox, deleteBox, tabListBox, navBox] = await Promise.all([
      saveButton.boundingBox(),
      sourceButton.boundingBox(),
      deleteButton.boundingBox(),
      tabList.boundingBox(),
      mobileNav.boundingBox(),
    ]);
    expect(saveBox).not.toBeNull();
    expect(sourceBox).not.toBeNull();
    expect(deleteBox).not.toBeNull();
    expect(tabListBox).not.toBeNull();
    expect(navBox).not.toBeNull();
    expect(saveBox!.y).toBeLessThan(tabListBox!.y);
    expect(saveBox!.y).toBeGreaterThanOrEqual(0);
    expect(saveBox!.x + saveBox!.width).toBeLessThanOrEqual(390);
    expect(sourceBox!.height).toBeGreaterThanOrEqual(44);
    expect(sourceBox!.width).toBeGreaterThanOrEqual(44);
    expect(deleteBox!.height).toBeGreaterThanOrEqual(44);
    expect(deleteBox!.width).toBeGreaterThanOrEqual(44);
    expect(sourceBox!.y + sourceBox!.height).toBeLessThanOrEqual(navBox!.y);
    expect(deleteBox!.y + deleteBox!.height).toBeLessThanOrEqual(navBox!.y);
    await settleVisuals(page);
    await page.locator(".notes-list time").evaluateAll((items) => items.forEach((item) => item.textContent = "最近更新 9/12 14:20"));
    await page.locator(".notes-editor__footer > p").evaluateAll((items) => items.forEach((item) => item.textContent = "最近保存于 9/12 14:20"));
    await expect(page).toHaveScreenshot("notes-editor.png");
    await editor.getByLabel("笔记正文").fill("# 键盘顺序验证");
    await expect(saveButton).toBeEnabled();
    const backButton = editor.getByRole("button", { name: "返回笔记列表" });
    await backButton.focus();
    await page.keyboard.press("Tab");
    await expect(editor.getByLabel("笔记标题")).toBeFocused();
    await page.keyboard.press("Tab");
    await expect(saveButton).toBeFocused();
    await page.keyboard.press("Tab");
    await expect(editor.getByRole("tab", { name: "编辑" })).toBeFocused();
  } else {
    await page.locator(".notes-list__item").click();
    await settleVisuals(page);
    await page.locator(".notes-list time").evaluateAll((items) => items.forEach((item) => item.textContent = "最近更新 9/12 14:20"));
    await page.locator(".notes-editor__footer > p").evaluateAll((items) => items.forEach((item) => item.textContent = "最近保存于 9/12 14:20"));
    await expect(page.locator(".notes-editor__footer > p")).toContainText("最近保存于");
    await settleVisuals(page);
    await page.locator(".notes-list time").evaluateAll((items) => items.forEach((item) => item.textContent = "最近更新 9/12 14:20"));
    await page.locator(".notes-editor__footer > p").evaluateAll((items) => items.forEach((item) => item.textContent = "最近保存于 9/12 14:20"));
    await expect(page).toHaveScreenshot("notes-default.png");
  }
});
