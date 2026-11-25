import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";
import { AuthProvider } from "../auth/AuthProvider";
import type { QaJob } from "../features/qa/types";
import { QaPage } from "./QaPage";

const fetchMock = vi.fn<typeof fetch>();
const response = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
const conversation = { conversation_id: "11111111-1111-4111-8111-111111111111", title: "研究对话", origin: "product", rolling_summary: "", summary_version: 0, created_at: "now", updated_at: "now", last_message_at: "now", documents: [{ document_id: "doc-1", document_name: "研究.md", position: 0 }] };
const completedMessage = { message_id: "message-complete", conversation_id: conversation.conversation_id, turn_id: "turn-complete", role: "assistant", status: "completed", mode: "auto", content: "回答", source_state: "available", retry_of_message_id: null, safe_error_code: null, trace_id: null, created_at: "now", updated_at: "now", completed_at: "now", sources: [{ citation_id: "S-1", document_id: "doc-1", document_name: "研究.md", page_number: 3, section: "方法", excerpt: "服务器证据", reference: "[S-1]", truncated: false, source_type: "rag" }] };

const runningJob = (jobId: string, conversationId: string): QaJob => ({
  job_id: jobId,
  conversation_id: conversationId,
  input_message_id: "input-active",
  assistant_message_id: "assistant-active",
  status: "running" as const,
  stage: "summarizing",
  progress: 40,
  cancel_requested_at: null,
  attempt_count: 1,
  max_attempts: 3,
  safe_error_code: null,
  trace_id: null,
  created_at: "now",
  started_at: "now",
  finished_at: null,
  updated_at: "running",
});

type RenderPageOptions = {
  activeJob?: QaJob | null;
  jobResult?: QaJob;
  conversations?: (typeof conversation)[];
  notesCapability?: "enabled" | "disabled" | "pending" | "error";
};

function renderPage(
  enabled: boolean,
  path = "/qa",
  messages: unknown[] = [],
  options: RenderPageOptions = {},
) {
  const conversationItems = options.conversations ?? [conversation];
  let activeSummaryRequests = 0;
  fetchMock.mockImplementation((input, init) => {
    const url = String(input);
    if (url === "/api/v1/auth/session") return Promise.resolve(response({ username: "reader", csrf_token: "csrf" }));
    if (url === "/api/v1/qa/capabilities") return Promise.resolve(response({ enabled }));
    if (url === "/api/v1/notes/capabilities") {
      if (options.notesCapability === "pending") return new Promise<Response>(() => undefined);
      if (options.notesCapability === "error") return Promise.resolve(response({ error: { code: "notes_unavailable", message: "Notes unavailable", retryable: false, field_errors: {} } }, 503));
      return Promise.resolve(response({ enabled: options.notesCapability !== "disabled" }));
    }
    if (url === "/api/v1/documents") return Promise.resolve(response({ items: [{ document_id: "doc-1", name: "研究.md", file_suffix: ".md", size_bytes: 1, loaded_at: "now", status: "ready" }] }));
    if (url === "/api/v1/qa/conversations?limit=20") return Promise.resolve(response({ items: conversationItems, next_cursor: null }));
    const selectedConversation = conversationItems.find(
      (item) => url === `/api/v1/qa/conversations/${item.conversation_id}`,
    );
    if (selectedConversation && (init?.method ?? "GET") === "GET") return Promise.resolve(response(selectedConversation));
    if (url === `/api/v1/qa/conversations/${conversation.conversation_id}` && init?.method === "DELETE") return Promise.resolve(response({ deletion_id: "deletion-1", target_type: "conversation", target_id: conversation.conversation_id, status: "queued", stage: "queued", affected_conversation_count: 1, attempt_count: 0, safe_error_code: null, trace_id: null, created_at: "now", updated_at: "queued" }, 202));
    if (url.includes("/messages?limit=50")) return Promise.resolve(response({ items: messages, next_cursor: null }));
    if (url.endsWith("/summary-jobs/active") && (init?.method ?? "GET") === "GET") {
      activeSummaryRequests += 1;
      const selected = conversationItems.find((item) => url.includes(item.conversation_id));
      const active = activeSummaryRequests === 1
        && options.activeJob?.conversation_id === selected?.conversation_id
        ? options.activeJob
        : null;
      return Promise.resolve(response({ job: active }));
    }
    if (options.jobResult && url === `/api/v1/qa/jobs/${options.jobResult.job_id}`) {
      return Promise.resolve(response(options.jobResult));
    }
    if (url.endsWith("/messages") && init?.method === "POST") return Promise.resolve(response({ message_id: "message-1", conversation_id: conversation.conversation_id, turn_id: "turn-1", role: "assistant", status: "pending", mode: "auto", content: "", source_state: "none", retry_of_message_id: null, safe_error_code: null, trace_id: null, created_at: "now", updated_at: "now", completed_at: null, sources: [] }, 202));
    if (url.endsWith("/summary-jobs") && init?.method === "POST") return Promise.resolve(response({ job_id: "job-1", conversation_id: conversation.conversation_id, input_message_id: "input-1", assistant_message_id: "assistant-1", status: "running", stage: "summarizing", progress: 40, cancel_requested_at: null, attempt_count: 1, max_attempts: 3, safe_error_code: null, trace_id: null, created_at: "now", started_at: "now", finished_at: null, updated_at: "running" }, 202));
    if (url === "/api/v1/qa/jobs/job-1") return Promise.resolve(response({ job_id: "job-1", conversation_id: conversation.conversation_id, input_message_id: "input-1", assistant_message_id: "assistant-1", status: "completed", stage: "completed", progress: 100, cancel_requested_at: null, attempt_count: 1, max_attempts: 3, safe_error_code: null, trace_id: null, created_at: "now", started_at: "now", finished_at: "now", updated_at: "done" }));
    if (url === "/api/v1/qa/deletions/deletion-1") return Promise.resolve(response({ deletion_id: "deletion-1", target_type: "conversation", target_id: conversation.conversation_id, status: "completed", stage: "completed", affected_conversation_count: 1, attempt_count: 1, safe_error_code: null, trace_id: null, created_at: "now", updated_at: "done" }));
    return Promise.reject(new Error(`Unexpected request ${url}`));
  });
  vi.stubGlobal("fetch", fetchMock);
  const client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
  return render(<QueryClientProvider client={client}><MemoryRouter initialEntries={[path]}><AuthProvider><QaPage /></AuthProvider></MemoryRouter></QueryClientProvider>);
}

describe("QaPage", () => {
  afterEach(() => { vi.unstubAllGlobals(); fetchMock.mockReset(); document.body.style.overflow = ""; });
  it("shows an explicit migration state when the route is disabled", async () => {
    renderPage(false);
    expect(await screen.findByRole("heading", { name: "智能问答正在迁移" })).toBeVisible();
    expect(screen.getByText(/历史数据不会被修改/)).toBeVisible();
  });
  it("submits a real question inside a fixed-scope conversation", async () => {
    renderPage(true, `/qa?conversation=${conversation.conversation_id}`);
    expect(await screen.findByRole("heading", { name: "智能问答" })).toBeVisible();
    await userEvent.type(screen.getByLabelText("向这些文档提问"), "研究结论是什么？");
    await userEvent.click(screen.getByRole("button", { name: "发送" }));
    expect(fetchMock).toHaveBeenCalledWith(expect.stringContaining("/messages"), expect.objectContaining({ method: "POST", body: expect.stringContaining("研究结论是什么？") }));
  });

  it("shows the latest completed answer sources without a citation click", async () => {
    renderPage(true, `/qa?conversation=${conversation.conversation_id}`, [completedMessage]);
    expect(await screen.findByText("服务器证据")).toBeVisible();
    expect(screen.getByRole("button", { name: "复制引用 1" })).toBeVisible();
    expect(screen.getByRole("link", { name: "记为笔记" })).toHaveAttribute(
      "href",
      "/notes?source_kind=qa_answer&qa_message_id=message-complete",
    );
    expect(screen.queryByRole("link", { name: "记录此引用" })).not.toBeInTheDocument();
    expect(screen.getByRole("link", { name: "存入笔记" })).toHaveAttribute(
      "href",
      "/notes?source_kind=qa_citation&qa_message_id=message-complete&citation_id=S-1",
    );
  });

  it("hides note actions when the Notes route is disabled", async () => {
    renderPage(true, `/qa?conversation=${conversation.conversation_id}`, [completedMessage], { notesCapability: "disabled" });
    expect(await screen.findByText("服务器证据")).toBeVisible();
    expect(screen.queryByRole("link", { name: "记为笔记" })).not.toBeInTheDocument();
    expect(screen.queryByRole("link", { name: "存入笔记" })).not.toBeInTheDocument();
  });

  it.each(["pending", "error"] as const)("fails closed while Notes capability is %s", async (notesCapability) => {
    renderPage(true, `/qa?conversation=${conversation.conversation_id}`, [completedMessage], { notesCapability });
    expect(await screen.findByText("服务器证据")).toBeVisible();
    expect(screen.queryByRole("link", { name: "记为笔记" })).not.toBeInTheDocument();
    expect(screen.queryByRole("link", { name: "存入笔记" })).not.toBeInTheDocument();
  });

  it("renders durable summary completion and deletion confirmation", async () => {
    renderPage(true, `/qa?conversation=${conversation.conversation_id}`);
    await screen.findByRole("heading", { name: "智能问答" });
    await userEvent.click(screen.getByRole("button", { name: "生成摘要" }));
    expect(await screen.findByText(/学习摘要 · 已完成/)).toBeVisible();

    await userEvent.click(screen.getByRole("button", { name: "删除对话" }));
    expect(screen.getByRole("dialog", { name: "永久删除对话" })).toHaveTextContent("消息、引用、摘要和问答记忆");
    await userEvent.click(screen.getByRole("button", { name: "永久删除" }));
    expect(fetchMock).toHaveBeenCalledWith(`/api/v1/qa/conversations/${conversation.conversation_id}`, expect.objectContaining({ method: "DELETE" }));
  });

  it("recovers an active summary after reload and blocks conflicting actions", async () => {
    const active = runningJob("job-active", conversation.conversation_id);
    renderPage(
      true,
      `/qa?conversation=${conversation.conversation_id}`,
      [],
      { activeJob: active, jobResult: active },
    );

    expect(await screen.findByText(/学习摘要 · 生成中/)).toBeVisible();
    expect(screen.getByRole("button", { name: "取消生成" })).toBeEnabled();
    expect(screen.getByLabelText("向这些文档提问")).toBeDisabled();
    expect(screen.getByRole("button", { name: "生成摘要" })).toBeDisabled();
  });

  it("reconciles recovered summary completion and releases actions", async () => {
    const active = runningJob("job-active", conversation.conversation_id);
    const completed = {
      ...active,
      status: "completed" as const,
      stage: "completed",
      progress: 100,
      finished_at: "done",
      updated_at: "done",
    };
    renderPage(
      true,
      `/qa?conversation=${conversation.conversation_id}`,
      [],
      { activeJob: active, jobResult: completed },
    );

    const composer = await screen.findByLabelText("向这些文档提问");
    await waitFor(() => expect(composer).toBeEnabled());
    expect(screen.getByRole("button", { name: "生成摘要" })).toBeEnabled();
    await waitFor(() => expect(
      fetchMock.mock.calls.filter(([input]) => String(input).endsWith("/summary-jobs/active")).length,
    ).toBeGreaterThanOrEqual(2));
    await new Promise((resolve) => setTimeout(resolve, 0));
    expect(screen.getByText(/学习摘要 · 已完成/)).toBeVisible();
  });

  it("does not carry a just-created summary identity into another conversation", async () => {
    const second = {
      ...conversation,
      conversation_id: "22222222-2222-4222-8222-222222222222",
      title: "第二个对话",
    };
    renderPage(
      true,
      `/qa?conversation=${conversation.conversation_id}`,
      [],
      { conversations: [conversation, second] },
    );
    await screen.findByRole("heading", { name: "智能问答" });
    await userEvent.click(screen.getByRole("button", { name: "生成摘要" }));
    await userEvent.click(screen.getByRole("button", { name: "对话" }));
    await userEvent.click(within(screen.getByRole("dialog", { name: "选择对话" })).getByRole("button", { name: /第二个对话/ }));

    await waitFor(() => expect(screen.queryByText(/学习摘要/)).not.toBeInTheDocument());
  });

  it("reaches the existing deletion confirmation from the conversation drawer", async () => {
    renderPage(true, `/qa?conversation=${conversation.conversation_id}`);
    await screen.findByRole("heading", { name: "智能问答" });
    await userEvent.click(screen.getByRole("button", { name: "对话" }));
    await userEvent.click(screen.getByRole("button", { name: "删除当前对话" }));
    expect(screen.getByRole("dialog", { name: "永久删除对话" })).toHaveTextContent(
      "消息、引用、摘要和问答记忆",
    );
    await userEvent.click(screen.getByRole("button", { name: "永久删除" }));
    expect(fetchMock).toHaveBeenCalledWith(
      `/api/v1/qa/conversations/${conversation.conversation_id}`,
      expect.objectContaining({ method: "DELETE" }),
    );
  });
});
