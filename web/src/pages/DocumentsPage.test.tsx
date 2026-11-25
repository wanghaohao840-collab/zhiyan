import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";

import { App } from "../App";
import { AuthProvider } from "../auth/AuthProvider";
import { navigationItems } from "../layout/navigation";

const fetchMock = vi.fn<typeof fetch>();

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

function apiError(message: string, status = 503): Response {
  return jsonResponse(
    { error: { code: "service_unavailable", message, retryable: true, field_errors: {} } },
    status,
  );
}

function documentItem(
  id: string,
  name: string,
  sizeBytes: number | null = 1024,
  loadedAt: string | null = "2026-08-22T10:00:00+00:00",
) {
  return {
    document_id: id,
    name,
    file_suffix: ".md",
    size_bytes: sizeBytes,
    loaded_at: loadedAt,
    status: "ready",
  };
}

function importBatch(status: "running" | "failed" | "succeeded") {
  return {
    batch_id: `batch-${status}`,
    created_at: "2026-08-22T10:00:00+00:00",
    updated_at: "2026-08-22T10:00:00+00:00",
    counts: {
      total: 1,
      queued: 0,
      running: status === "running" ? 1 : 0,
      retry_wait: 0,
      succeeded: status === "succeeded" ? 1 : 0,
      failed: status === "failed" ? 1 : 0,
      cancelled: 0,
    },
    tasks: [
      {
        task_id: `task-${status}`,
        document_id: "document-1",
        original_name: "task-notes.md",
        file_suffix: ".md",
        size_bytes: 12,
        status,
        stage: status === "running" ? "embedding" : status,
        progress: status === "running" ? 68 : 100,
        error_code: status === "failed" ? "import_stage_failed" : null,
        error_summary: status === "failed" ? "索引失败，请稍后重试" : null,
        cancel_requested_at: null,
        created_at: "2026-08-22T10:00:00+00:00",
        started_at: null,
        finished_at: null,
        updated_at: "2026-08-22T10:00:00+00:00",
      },
    ],
  };
}

type StubOptions = {
  actionResponse?: (url: string) => Promise<Response>;
  deleteResponse?: () => Promise<Response>;
  documents?: unknown;
  imports?: unknown;
  documentsResponse?: () => Promise<Response>;
};

function installFetchStub({
  documents = { items: [] },
  imports = [],
  documentsResponse,
  actionResponse,
  deleteResponse,
}: StubOptions = {}) {
  fetchMock.mockImplementation((input, init) => {
    const url = String(input);
    if (url === "/api/v1/auth/session") {
      return Promise.resolve(jsonResponse({ username: "reader", csrf_token: "csrf" }));
    }
    if (url === "/api/v1/qa/capabilities") {
      return Promise.resolve(jsonResponse({ enabled: true }));
    }
    if (url === "/api/v1/qa/conversations?limit=100") {
      return Promise.resolve(jsonResponse({ items: [], next_cursor: null }));
    }
    if (url === "/api/v1/documents" && (init?.method ?? "GET") === "GET") {
      return documentsResponse?.() ?? Promise.resolve(jsonResponse(documents));
    }
    if (url === "/api/v1/imports?limit=20" && (init?.method ?? "GET") === "GET") {
      return Promise.resolve(jsonResponse(imports));
    }
    if (url === "/api/v1/imports" && init?.method === "POST") {
      return Promise.resolve(jsonResponse(importBatch("running"), 202));
    }
    if (url.includes("/retry") || url.includes("/cancel")) {
      return actionResponse?.(url) ?? Promise.resolve(jsonResponse(importBatch("running")));
    }
    if (url.startsWith("/api/v1/documents/") && init?.method === "DELETE") {
      return deleteResponse?.() ?? Promise.resolve(new Response(null, { status: 204 }));
    }
    return Promise.reject(new Error(`Unexpected request: ${url}`));
  });
  vi.stubGlobal("fetch", fetchMock);
}

function renderApp(path = "/documents") {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  const result = render(
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={[path]}>
        <AuthProvider>
          <App />
        </AuthProvider>
      </MemoryRouter>
    </QueryClientProvider>,
  );
  return { client, ...result };
}

describe("DocumentsPage", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
    fetchMock.mockReset();
    document.body.style.overflow = "";
  });

  it("renders real /documents while non-product routes stay migration", async () => {
    installFetchStub();
    const { unmount } = renderApp("/documents");
    expect(await screen.findByRole("heading", { level: 1, name: "文档库" })).toBeVisible();
    expect(screen.queryByText("该能力正在迁移到新版界面")).not.toBeInTheDocument();
    unmount();
    for (const item of navigationItems.filter(({ path }) => !["/overview", "/documents", "/qa", "/notes", "/insights", "/search", "/learning"].includes(path))) {
      const migration = renderApp(item.path);
      expect(
        await screen.findByRole("heading", { level: 1, name: item.heading }),
      ).toBeVisible();
      expect(
        screen.getByText("该能力正在迁移到新版界面，可暂时前往旧版使用。"),
      ).toBeVisible();
      migration.unmount();
    }
    const notes = renderApp("/notes");
    expect(
      await screen.findByRole("heading", { level: 1, name: "学习笔记" }, { timeout: 5000 }),
    ).toBeVisible();
    expect(screen.queryByText("该能力正在迁移到新版界面，可暂时前往旧版使用。")).not.toBeInTheDocument();
    notes.unmount();
    const learning = renderApp("/learning");
    expect(
      await screen.findByRole("heading", { level: 1, name: "学习中心" }, { timeout: 5000 }),
    ).toBeVisible();
    expect(screen.queryByText("该能力正在迁移到新版界面，可暂时前往旧版使用。")).not.toBeInTheDocument();
    learning.unmount();
  }, 15_000);

  it("keeps loading distinct from empty and renders the exact empty copy", async () => {
    let resolveDocuments!: (response: Response) => void;
    installFetchStub({
      documentsResponse: () => new Promise((resolve) => { resolveDocuments = resolve; }),
    });
    renderApp();
    expect(await screen.findByRole("status", { name: "正在加载文档库" })).toBeVisible();
    expect(screen.queryByText("还没有文档")).not.toBeInTheDocument();

    await waitFor(() => expect(resolveDocuments).toBeTypeOf("function"));
    resolveDocuments(jsonResponse({ items: [] }));
    const emptyHeading = await screen.findByRole("heading", {
      level: 2,
      name: "还没有文档",
    });
    expect(emptyHeading).toBeVisible();
    expect.soft(screen.queryByText("每一次阅读，都有迹可循")).toBeVisible();
    expect.soft(screen.queryByLabelText("按名称筛选")).not.toBeInTheDocument();
    expect(
      screen.getByText("导入 PDF、TXT、Markdown 或 DOCX，开始构建你的知识库。"),
    ).toBeVisible();
    const limits = screen.getByText(
      "每批最多 20 个文件 · 单文件 100 MiB · 每批 500 MiB",
    );
    expect(limits).toBeVisible();
    const emptyCard = emptyHeading.closest(".documents-empty");
    expect(emptyCard).not.toBeNull();
    expect.soft(emptyCard).not.toContainElement(limits);
    expect.soft(emptyCard?.querySelector(".documents-empty__icon")).toBeNull();
    expect
      .soft(within(emptyCard as HTMLElement).queryByText("文"))
      .not.toBeInTheDocument();
  });

  it("shows a reloadable query error without misrepresenting it as empty", async () => {
    installFetchStub({ documentsResponse: () => Promise.resolve(apiError("文档服务暂不可用")) });
    renderApp();

    expect(await screen.findByRole("alert")).toHaveTextContent("文档服务暂不可用");
    expect(screen.getByRole("button", { name: "重新加载" })).toBeVisible();
    expect(screen.queryByText("还没有文档")).not.toBeInTheDocument();
  });

  it("preserves successful document data when a background refetch fails", async () => {
    const responses = [
      jsonResponse({ items: [documentItem("doc-1", "kept.md")] }),
      apiError("刷新失败，已保留现有文档"),
    ];
    installFetchStub({
      documentsResponse: () => Promise.resolve(responses.shift()!),
    });
    const { client } = renderApp();
    expect(await screen.findByText("kept.md")).toBeVisible();

    await client.invalidateQueries({ queryKey: ["documents"] });
    expect(await screen.findByRole("alert")).toHaveTextContent("刷新失败，已保留现有文档");
    expect(screen.getByText("kept.md")).toBeVisible();
    expect(screen.queryByText("还没有文档")).not.toBeInTheDocument();
  });

  it("filters case-insensitively, preserves server order and duplicate IDs", async () => {
    installFetchStub({
      documents: {
        items: [
          documentItem("doc-2", "Research.MD", null, null),
          documentItem("doc-1", "research.md"),
          documentItem("doc-3", "other.md"),
        ],
      },
    });
    renderApp();
    const user = userEvent.setup();
    const list = await screen.findByRole("list", { name: "文档列表" });
    expect(within(list).getAllByText(/research\.md/i)).toHaveLength(2);
    expect(within(list).getAllByRole("listitem").map((row) => row.dataset.documentId)).toEqual([
      "doc-2",
      "doc-1",
      "doc-3",
    ]);
    expect(within(list).getByRole("listitem", { name: /Research\.MD/ })).not.toHaveTextContent(
      "0 B",
    );
    expect(within(list).getByRole("listitem", { name: /Research\.MD/ })).not.toHaveTextContent(
      "未知",
    );

    const filter = screen.getByLabelText("按名称筛选");
    expect(filter).toBeVisible();
    await user.type(filter, "RESEARCH");
    expect(within(list).getAllByRole("listitem")).toHaveLength(2);
  });

  it("renders active and partial-failure task actions with qualified names", async () => {
    installFetchStub({
      imports: [
        importBatch("running"),
        importBatch("failed"),
        importBatch("succeeded"),
      ],
    });
    renderApp();

    expect(await screen.findByText("正在生成索引 · 68%")).toBeVisible();
    expect(screen.getByRole("button", { name: "取消 task-notes.md" })).toBeVisible();
    expect(screen.getByText("索引失败，请稍后重试")).toBeVisible();
    expect(screen.getByRole("button", { name: "重试 task-notes.md" })).toBeVisible();
    expect(screen.getByRole("button", { name: "重试全部失败项" })).toBeVisible();
    expect(screen.queryByText("最近导入结果 · 完成 1 · 取消 0")).not.toBeInTheDocument();
  });

  it("uses exact retry, retry-all and cancel routes without unhandled promises", async () => {
    installFetchStub({ imports: [importBatch("running"), importBatch("failed")] });
    renderApp();
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: "取消 task-notes.md" }));
    await user.click(screen.getByRole("button", { name: "重试 task-notes.md" }));
    await user.click(screen.getByRole("button", { name: "重试全部失败项" }));

    await waitFor(() => {
      const urls = fetchMock.mock.calls.map(([url]) => String(url));
      expect(urls).toContain("/api/v1/imports/batch-running/tasks/task-running/cancel");
      expect(urls).toContain("/api/v1/imports/batch-failed/tasks/task-failed/retry");
      expect(urls).toContain("/api/v1/imports/batch-failed/retry-failed");
    });
  });

  it("sends real FormData without a multipart Content-Type override", async () => {
    installFetchStub();
    renderApp();
    const user = userEvent.setup();
    await user.click((await screen.findAllByRole("button", { name: "导入文档" }))[0]!);
    const file = new File(["# notes"], "real.md", { type: "text/markdown" });
    await user.upload(screen.getByLabelText("选择文档"), file);
    await user.click(screen.getByRole("button", { name: "开始导入" }));

    await waitFor(() => {
      const call = fetchMock.mock.calls.find(
        ([url, init]) => url === "/api/v1/imports" && init?.method === "POST",
      );
      expect(call?.[1]?.body).toBeInstanceOf(FormData);
      expect((call?.[1]?.body as FormData).getAll("files")).toEqual([file]);
      expect(new Headers(call?.[1]?.headers).has("Content-Type")).toBe(false);
    });
  });

  it("shows safe action errors without exposing stable error codes", async () => {
    installFetchStub({
      imports: [importBatch("failed")],
      actionResponse: () => Promise.resolve(apiError("该任务当前不可重试", 409)),
    });
    renderApp();
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: "重试 task-notes.md" }));

    expect(await screen.findByRole("alert")).toHaveTextContent("该任务当前不可重试");
    expect(screen.queryByText("service_unavailable")).not.toBeInTheDocument();
  });

  it("opens an accessible delete confirmation and waits for server success", async () => {
    installFetchStub({ documents: { items: [documentItem("doc-1", "notes.md")] } });
    renderApp();
    const user = userEvent.setup();
    const trigger = await screen.findByRole("button", { name: "删除 notes.md" });
    await user.click(trigger);
    const dialog = screen.getByRole("dialog", { name: "删除 notes.md" });
    expect(dialog).toHaveTextContent("notes.md");
    expect(within(dialog).getByRole("button", { name: "取消" })).toHaveFocus();
    await user.click(within(dialog).getByRole("button", { name: "确认删除" }));

    await waitFor(() => expect(screen.queryByRole("dialog", { name: "删除 notes.md" })).not.toBeInTheDocument());
    expect(fetchMock.mock.calls.some(([url, init]) =>
      url === "/api/v1/documents/doc-1" && init?.method === "DELETE",
    )).toBe(true);
  });

  it("keeps delete confirmation recoverable, focuses its safe error, and restores focus", async () => {
    installFetchStub({
      documents: { items: [documentItem("doc-1", "notes.md")] },
      deleteResponse: () => Promise.resolve(apiError("文档删除失败，请重试", 500)),
    });
    renderApp();
    const user = userEvent.setup();
    const trigger = await screen.findByRole("button", { name: "删除 notes.md" });
    await user.click(trigger);
    await user.click(screen.getByRole("button", { name: "确认删除" }));

    const error = await screen.findByRole("alert");
    expect(error).toHaveTextContent("文档删除失败，请重试");
    expect(error).toHaveFocus();
    expect(screen.getByRole("dialog", { name: "删除 notes.md" })).toBeVisible();
    expect(screen.getByText("notes.md")).toBeVisible();
    await user.keyboard("{Escape}");
    expect(screen.queryByRole("dialog", { name: "删除 notes.md" })).not.toBeInTheDocument();
    expect(trigger).toHaveFocus();
  });

  it("uses one deduplicated polite live region and consumes event promises", async () => {
    const initial = importBatch("running");
    initial.tasks[0]!.stage = "embedding";
    initial.tasks[0]!.progress = 41;
    installFetchStub({ imports: [initial] });
    const unhandled: unknown[] = [];
    const record = (event: PromiseRejectionEvent) => {
      event.preventDefault();
      unhandled.push(event.reason);
    };
    window.addEventListener("unhandledrejection", record);
    try {
      const { client } = renderApp();
      const live = await screen.findByRole("status", { name: "导入状态" });
      expect(live).toHaveAttribute("aria-live", "polite");
      expect(document.querySelectorAll('[aria-live="polite"]')).toHaveLength(1);
      await waitFor(() => {
        expect(live).toHaveTextContent("task-notes.md：正在生成索引 · 40%");
      });

      const mutations: MutationRecord[] = [];
      const observer = new MutationObserver((records) => mutations.push(...records));
      observer.observe(live, { childList: true, characterData: true, subtree: true });

      const progressMilestone = {
        ...initial,
        tasks: [{ ...initial.tasks[0]!, progress: 52 }],
      };
      client.setQueryData(["imports", { limit: 20 }], [progressMilestone]);
      await waitFor(() => {
        expect(live).toHaveTextContent("task-notes.md：正在生成索引 · 50%");
      });
      mutations.length = 0;

      client.setQueryData(
        ["imports", { limit: 20 }],
        [{ ...progressMilestone, tasks: [{ ...progressMilestone.tasks[0]! }] }],
      );
      await new Promise((resolve) => setTimeout(resolve, 0));
      expect(mutations).toHaveLength(0);

      client.setQueryData(
        ["imports", { limit: 20 }],
        [
          {
            ...progressMilestone,
            tasks: [{ ...progressMilestone.tasks[0]!, stage: "persisting" }],
          },
        ],
      );
      await waitFor(() => {
        expect(live).toHaveTextContent("task-notes.md：正在保存索引 · 50%");
      });
      observer.disconnect();
      expect(unhandled).toEqual([]);
    } finally {
      window.removeEventListener("unhandledrejection", record);
    }
  });

  it("hands off only the owned document ID to the real QA workspace", async () => {
    installFetchStub({ documents: { items: [documentItem("doc-1", "notes.md")] } });
    renderApp();
    await userEvent.click(await screen.findByRole("button", { name: "开始问答" }));
    expect(await screen.findByRole("heading", { level: 1, name: "智能问答" })).toBeVisible();
    expect(screen.getByRole("dialog", { name: "新建对话" })).toBeVisible();
    expect(screen.getByRole("checkbox", { name: "notes.md" })).toBeChecked();
  });
});
