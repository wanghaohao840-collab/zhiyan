import { fireEvent, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { ConversationList, MessageList, QaComposer, QaOverlay, SourcePanel, SummaryStatus } from "./QaWorkspace";
import type { QaJob, QaMessage, QaSource } from "../../features/qa/types";

const source: QaSource = { citation_id: "citation-1", document_id: "doc-1", document_name: "研究.md", page_number: 3, section: "方法", excerpt: "证据片段", reference: "研究.md#方法", truncated: false, source_type: "document" };
const failed: QaMessage = { message_id: "message-1", conversation_id: "conversation-1", turn_id: "turn-1", role: "assistant", status: "failed", mode: "auto", content: "", source_state: "none", retry_of_message_id: null, safe_error_code: "QA_ENGINE_UNAVAILABLE", trace_id: null, created_at: "now", updated_at: "now", completed_at: null, sources: [] };

describe("QA workspace components", () => {
  it("exposes explicit disabled loading controls only when another page exists", async () => {
    const loadConversations = vi.fn();
    const loadMessages = vi.fn();
    const { rerender } = render(<>
      <ConversationList
        items={[]}
        onSelect={vi.fn()}
        onNew={vi.fn()}
        hasMore
        loadingMore={false}
        onLoadMore={loadConversations}
      />
      <MessageList
        messages={[failed]}
        onSources={vi.fn()}
        onRetry={vi.fn()}
        hasOlder
        loadingOlder
        onLoadOlder={loadMessages}
      />
    </>);

    await userEvent.click(screen.getByRole("button", { name: "加载更多对话" }));
    expect(loadConversations).toHaveBeenCalledOnce();
    expect(screen.getByRole("button", { name: "正在加载更早消息…" })).toBeDisabled();

    rerender(<MessageList messages={[failed]} onSources={vi.fn()} onRetry={vi.fn()} />);
    expect(screen.queryByRole("button", { name: /加载更早消息/ })).not.toBeInTheDocument();
  });

  it("requires two documents and submits only on a non-composing shortcut", async () => {
    const submit = vi.fn();
    render(<QaComposer busy={false} documentCount={1} onSubmit={submit} />);
    expect(screen.getByRole("option", { name: "对比（需至少两篇文档）" })).toBeDisabled();
    const input = screen.getByLabelText("向这些文档提问");
    await userEvent.type(input, "什么是结论{enter}");
    expect(submit).not.toHaveBeenCalled();
    fireEvent.keyDown(input, { key: "Enter", ctrlKey: true, isComposing: true });
    expect(submit).not.toHaveBeenCalled();
    fireEvent.keyDown(input, { key: "Enter", ctrlKey: true });
    expect(submit).toHaveBeenCalledWith("什么是结论", "auto");
  });

  it("renders safe failure/retry and complete citation evidence", async () => {
    const retry = vi.fn();
    const writeText = vi.fn().mockResolvedValue(undefined);
    Object.defineProperty(window.navigator, "clipboard", { configurable: true, value: { writeText } });
    render(<><MessageList messages={[failed]} onSources={vi.fn()} onRetry={retry} /><SourcePanel sources={[source]} /></>);
    expect(screen.getByRole("alert")).toHaveTextContent("QA_ENGINE_UNAVAILABLE");
    await userEvent.click(screen.getByRole("button", { name: "重试回答" }));
    expect(retry).toHaveBeenCalledWith(failed);
    expect(screen.getByText("研究.md#方法")).toBeVisible();
    await userEvent.click(screen.getByRole("button", { name: "复制引用 1" }));
    expect(writeText).toHaveBeenCalledWith("研究.md#方法");
    expect(screen.getByText("引用 1 已复制")).toBeVisible();
    writeText.mockRejectedValueOnce(new Error("private browser failure"));
    await userEvent.click(screen.getByRole("button", { name: "复制引用 1" }));
    expect(screen.getByText("复制失败，请手动选择引用文本")).toBeVisible();
    expect(document.body).not.toHaveTextContent("private browser failure");
  });

  it("blocks retry while another QA action is busy", () => {
    render(<MessageList messages={[failed]} onSources={vi.fn()} onRetry={vi.fn()} busy />);
    expect(screen.getByRole("button", { name: "重试回答" })).toBeDisabled();
  });

  it("offers identifier-only answer and citation note actions only for completed answers", () => {
    const completed: QaMessage = {
      ...failed,
      message_id: "answer & 1",
      status: "completed",
      content: "答案正文不应进入 URL",
      sources: [source],
    };
    const pending: QaMessage = { ...failed, message_id: "pending-1", status: "pending", sources: [source] };
    render(
      <>
        <MessageList messages={[completed, failed, pending]} onSources={vi.fn()} onRetry={vi.fn()} />
        <SourcePanel sources={[source]} messageId={completed.message_id} notesEnabled />
      </>,
    );

    expect(screen.getByRole("link", { name: "记为笔记" })).toHaveAttribute(
      "href",
      "/notes?source_kind=qa_answer&qa_message_id=answer+%26+1",
    );
    expect(screen.queryByRole("link", { name: "记录此引用" })).not.toBeInTheDocument();
    expect(screen.getByRole("link", { name: "存入笔记" })).toHaveAttribute(
      "href",
      "/notes?source_kind=qa_citation&qa_message_id=answer+%26+1&citation_id=citation-1",
    );
    expect(screen.getByRole("link", { name: "记为笔记" })).not.toHaveAttribute("href", expect.stringContaining("答案正文"));
    expect(screen.queryAllByRole("link", { name: "记为笔记" })).toHaveLength(1);
  });

  it("hides note actions when the Notes route is disabled", () => {
    const completed: QaMessage = { ...failed, status: "completed", content: "答案", sources: [source] };
    render(
      <>
        <MessageList messages={[completed]} notesEnabled={false} onSources={vi.fn()} onRetry={vi.fn()} />
        <SourcePanel sources={[source]} messageId={completed.message_id} notesEnabled={false} />
      </>,
    );
    expect(screen.queryByRole("link", { name: "记为笔记" })).not.toBeInTheDocument();
    expect(screen.queryByRole("link", { name: "存入笔记" })).not.toBeInTheDocument();
  });

  it("renders a decorative cue only while summary work is active", () => {
    const job: QaJob = { job_id: "job-1", conversation_id: "conversation-1", input_message_id: "input-1", assistant_message_id: "assistant-1", status: "running", stage: "summarizing", progress: 40, cancel_requested_at: null, attempt_count: 1, max_attempts: 3, safe_error_code: null, trace_id: null, created_at: "now", started_at: "now", finished_at: null, updated_at: "now" };
    const { rerender } = render(<SummaryStatus job={job} onCancel={vi.fn()} />);
    expect(document.querySelector(".qa-summary-status__skeleton")).toBeInTheDocument();
    rerender(<SummaryStatus job={{ ...job, status: "completed", progress: 100 }} onCancel={vi.fn()} />);
    expect(document.querySelector(".qa-summary-status__skeleton")).not.toBeInTheDocument();
  });

  it("renders markdown safely and numbers multi-citation chips and actions", async () => {
    const secondSource: QaSource = {
      ...source,
      citation_id: "citation-2",
      document_name: "实验.pdf",
      page_number: 7,
      reference: "实验.pdf#P7",
    };
    const onSources = vi.fn();
    const rich: QaMessage = {
      ...failed,
      message_id: "rich-1",
      status: "completed",
      content: "### 核心结论\n\n- 支持 **Markdown** 渲染\n- <script>alert(1)</script>",
      sources: [source, secondSource],
    };
    render(
      <>
        <MessageList messages={[rich]} activeCitationId="citation-2" onSources={onSources} onRetry={vi.fn()} />
        <SourcePanel sources={[source, secondSource]} activeCitationId="citation-2" messageId="rich-1" notesEnabled />
      </>,
    );

    expect(screen.getByRole("heading", { level: 3, name: "核心结论" })).toBeVisible();
    expect(screen.getByRole("list", { name: "回答处理阶段" })).toBeVisible();
    expect(screen.getByText("证据引用 (2)")).toBeVisible();
    expect(document.querySelector("script")).not.toBeInTheDocument();
    expect(screen.queryByRole("link", { name: /记录引用/ })).not.toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: /\[2\]/ }));
    expect(onSources).toHaveBeenCalledWith(rich, expect.any(HTMLButtonElement), "citation-2");
    expect(document.querySelector('li[data-citation-id="citation-2"]')).toHaveClass("is-active");
    const saveLinks = screen.getAllByRole("link", { name: "存入笔记" });
    expect(saveLinks).toHaveLength(2);
    expect(saveLinks[0]).toHaveAttribute(
      "href",
      "/notes?source_kind=qa_citation&qa_message_id=rich-1&citation_id=citation-1",
    );
    expect(saveLinks[1]).toHaveAttribute(
      "href",
      "/notes?source_kind=qa_citation&qa_message_id=rich-1&citation_id=citation-2",
    );
  });

  it("traps focus, closes on Escape and returns focus", async () => {
    const close = vi.fn();
    const trigger = document.createElement("button");
    document.body.append(trigger); trigger.focus();
    const { unmount } = render(<QaOverlay title="引用来源" returnFocusTo={trigger} onClose={close}><button>内部操作</button></QaOverlay>);
    expect(screen.getByRole("button", { name: "关闭引用来源" })).toHaveFocus();
    await userEvent.keyboard("{Escape}");
    expect(close).toHaveBeenCalled();
    unmount();
    expect(trigger).toHaveFocus();
    trigger.remove();
  });

  it("links inline citation markers in prose and scrolls active source card into view", async () => {
    const scrollIntoView = vi.fn();
    HTMLElement.prototype.scrollIntoView = scrollIntoView;
    const onSources = vi.fn();
    const cited: QaMessage = {
      ...failed,
      message_id: "cited-1",
      status: "completed",
      content: "核心结论来源于实验报告 [1]。",
      sources: [source],
    };

    render(
      <>
        <MessageList messages={[cited]} activeCitationId="citation-1" onSources={onSources} onRetry={vi.fn()} />
        <SourcePanel sources={[source]} activeCitationId="citation-1" messageId="cited-1" notesEnabled />
      </>,
    );

    const inlineButton = screen.getByRole("button", { name: "查看正文引用 1" });
    await userEvent.click(inlineButton);
    expect(onSources).toHaveBeenCalledWith(cited, inlineButton, "citation-1");
    expect(scrollIntoView).toHaveBeenCalled();
  });
});
