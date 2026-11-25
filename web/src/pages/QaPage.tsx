import { ChevronDown, ChevronUp, FileText } from "lucide-react";
import { type CSSProperties, useEffect, useMemo, useState } from "react";
import { useSearchParams } from "react-router-dom";
import { ApiError } from "../api/client";
import { Button } from "../components/Button/Button";
import {
  ConversationList,
  MessageList,
  QaComposer,
  QaOverlay,
  SourcePanel,
  SummaryStatus,
} from "../components/QaWorkspace/QaWorkspace";
import { QaDeleteDialog } from "../components/QaWorkspace/QaDeleteDialog";
import { QaDrawer } from "../components/QaWorkspace/QaDrawer";
import { PaneResizer, useResizableSplit } from "../components/ui";
import { useDocumentsQuery } from "../features/documents/queries";
import { useNotesCapabilities } from "../features/notes/queries";
import {
  newClientRequestId,
  useQaActiveSummary,
  useQaCapabilities,
  useQaConversation,
  useQaConversations,
  useQaDeletion,
  useQaJob,
  useQaMessages,
  useQaMutations,
} from "../features/qa/queries";
import type { QaMessage, QaSource } from "../features/qa/types";

const message = (reason: unknown, fallback: string) =>
  reason instanceof ApiError ? reason.message : fallback;

export function QaPage() {
  const [params, setParams] = useSearchParams();
  const selectedId = params.get("conversation") ?? undefined;
  const handoffIds = useMemo(
    () => (params.get("documents") ?? "").split(",").filter(Boolean),
    [params],
  );
  const capabilities = useQaCapabilities();
  const notesCapabilities = useNotesCapabilities();
  const enabled = capabilities.data?.enabled !== false;
  const notesEnabled = notesCapabilities.isSuccess && notesCapabilities.data.enabled === true;
  const conversations = useQaConversations(enabled);
  const conversation = useQaConversation(enabled ? selectedId : undefined);
  const messages = useQaMessages(enabled ? selectedId : undefined);
  const documents = useDocumentsQuery();
  const mutations = useQaMutations();
  const split = useResizableSplit({
    storageKey: "zhiyan.ui.qa.split",
    defaultLeft: 224,
    defaultRight: 292,
    minLeft: 180,
    maxLeft: 340,
    minRight: 240,
    maxRight: 440,
  });
  const [createOpen, setCreateOpen] = useState(handoffIds.length > 0);
  const [selectedDocuments, setSelectedDocuments] = useState<Set<string>>(new Set(handoffIds));
  const [scopeExpanded, setScopeExpanded] = useState(false);
  const [sources, setSources] = useState<QaSource[]>([]);
  const [sourceMessageId, setSourceMessageId] = useState<string>();
  const [activeCitationId, setActiveCitationId] = useState<string>();
  const [sourceOpen, setSourceOpen] = useState(false);
  const [conversationOpen, setConversationOpen] = useState(false);
  const [conversationTrigger, setConversationTrigger] = useState<HTMLElement | null>(null);
  const [sourceTrigger, setSourceTrigger] = useState<HTMLElement | null>(null);
  const [startedSummary, setStartedSummary] = useState<{
    conversationId: string;
    jobId: string;
  }>();
  const [deletionId, setDeletionId] = useState<string>();
  const [deleteOpen, setDeleteOpen] = useState(false);
  const activeSummary = useQaActiveSummary(enabled ? selectedId : undefined);
  const startedJobId =
    startedSummary && startedSummary.conversationId === selectedId
      ? startedSummary.jobId
      : undefined;
  const summaryJobId = startedJobId ?? activeSummary.data?.job?.job_id;
  const job = useQaJob(summaryJobId);
  const deletion = useQaDeletion(deletionId);
  const discoveredSummary = activeSummary.data?.job;

  useEffect(() => {
    setStartedSummary(undefined);
    setScopeExpanded(false);
  }, [selectedId]);

  useEffect(() => {
    if (!selectedId || discoveredSummary?.conversation_id !== selectedId) return;
    setStartedSummary((current) =>
      current?.conversationId === selectedId && current.jobId === discoveredSummary.job_id
        ? current
        : { conversationId: selectedId, jobId: discoveredSummary.job_id },
    );
  }, [discoveredSummary, selectedId]);

  useEffect(() => {
    if (deletion.data?.status === "completed") {
      setDeletionId(undefined);
      setDeleteOpen(false);
      setParams({});
    }
  }, [deletion.data?.status, setParams]);

  useEffect(() => {
    const items = messages.data?.items;
    if (!items) return;
    const latest = [...items]
      .reverse()
      .find((item) => item.role === "assistant" && item.status === "completed");
    setSources(latest?.sources ?? []);
    setSourceMessageId(latest?.message_id);
    setActiveCitationId(undefined);
  }, [messages.data?.items, selectedId]);

  function selectConversation(id: string) {
    setParams({ conversation: id });
    setConversationOpen(false);
    setSources([]);
    setSourceMessageId(undefined);
    setActiveCitationId(undefined);
  }
  function createConversation() {
    mutations.create.mutate([...selectedDocuments], {
      onSuccess: (item) => {
        setCreateOpen(false);
        setParams({ conversation: item.conversation_id });
        setSelectedDocuments(new Set());
      },
    });
  }
  function showSources(item: QaMessage, trigger: HTMLButtonElement, citationId?: string) {
    setSources(item.sources);
    setSourceMessageId(item.message_id);
    setActiveCitationId(citationId);
    setSourceTrigger(trigger);
    if (window.innerWidth < 1200) setSourceOpen(true);
  }

  if (capabilities.isPending)
    return (
      <div className="qa-state" role="status">
        正在加载智能问答…
      </div>
    );
  if (capabilities.error)
    return (
      <div className="qa-state" role="alert">
        {message(capabilities.error, "智能问答状态加载失败")}
      </div>
    );
  if (!enabled)
    return (
      <section className="qa-state">
        <h1>智能问答正在迁移</h1>
        <p>此部署尚未启用新版问答路由。你的历史数据不会被修改；启用后可继续使用持久对话与引用。</p>
      </section>
    );

  const items = conversations.items;
  const currentMessages = messages.items;
  const currentJob = job.data ?? activeSummary.data?.job ?? undefined;
  const summaryActive = currentJob?.status === "queued" || currentJob?.status === "running";
  const busy =
    mutations.ask.isPending ||
    mutations.summarize.isPending ||
    currentMessages.some((item) => item.status === "pending") ||
    summaryActive;
  const queryError =
    conversations.error ?? conversation.error ?? messages.error ?? activeSummary.error;
  const actionError =
    mutations.ask.error ??
    mutations.retry.error ??
    mutations.summarize.error ??
    mutations.cancel.error;
  const deletionActive = deletion.data?.status === "queued" || deletion.data?.status === "running";
  const boundDocs = conversation.data?.documents ?? [];
  const workspaceStyle = {
    "--pane-left-width": `${split.leftWidth}px`,
    "--pane-right-width": `${split.rightWidth}px`,
  } as CSSProperties;

  return (
    <article className="qa-page">
      <header className="qa-header">
        <div className="qa-header__info">
          <h1>智能问答</h1>
          <div className="qa-scope-row">
            <p>
              {conversation.data
                ? `基于 ${boundDocs.length} 篇文档`
                : "选择固定文档范围，开始可追溯问答"}
            </p>
            {boundDocs.length > 0 ? (
              <button
                type="button"
                className="qa-scope-toggle"
                aria-expanded={scopeExpanded}
                onClick={() => setScopeExpanded((open) => !open)}
              >
                <span>{scopeExpanded ? "收起范围" : "查看范围"}</span>
                {scopeExpanded ? (
                  <ChevronUp size={13} strokeWidth={1.6} aria-hidden="true" />
                ) : (
                  <ChevronDown size={13} strokeWidth={1.6} aria-hidden="true" />
                )}
              </button>
            ) : null}
          </div>
          {scopeExpanded && boundDocs.length > 0 ? (
            <ul className="qa-scope-pills" aria-label="当前对话绑定文档范围">
              {boundDocs.map((doc) => (
                <li key={doc.document_id} className="qa-scope-pill">
                  <FileText size={12} strokeWidth={1.6} aria-hidden="true" />
                  <span>{doc.document_name}</span>
                </li>
              ))}
            </ul>
          ) : null}
        </div>
        <div className="qa-header__actions">
          <Button
            hierarchy="secondary"
            className="qa-conversation-trigger"
            onClick={(event) => {
              setConversationTrigger(event.currentTarget);
              setConversationOpen(true);
            }}
          >
            对话
          </Button>
          {selectedId ? (
            <>
              <Button
                hierarchy="secondary"
                disabled={busy}
                onClick={() =>
                  mutations.summarize.mutate(
                    { conversationId: selectedId, instruction: "", clientRequestId: newClientRequestId() },
                    { onSuccess: (value) => setStartedSummary({ conversationId: selectedId, jobId: value.job_id }) },
                  )
                }
              >
                生成摘要
              </Button>
              <Button hierarchy="ghost" onClick={() => setDeleteOpen(true)}>
                删除对话
              </Button>
            </>
          ) : null}
        </div>
      </header>
      {queryError ? (
        <p className="qa-error" role="alert">
          {message(queryError, "问答数据加载失败，请重试")}
        </p>
      ) : null}
      {actionError ? (
        <p className="qa-error" role="alert">
          {message(actionError, "问答操作失败，请重试")}
        </p>
      ) : null}
      {conversation.data && conversation.data.origin !== "product" ? (
        <p className="qa-legacy-note">
          这是迁移后的历史对话；旧来源可能无法恢复，但新消息将使用当前文档范围。
        </p>
      ) : null}
      <SummaryStatus
        job={currentJob}
        onCancel={() => summaryJobId && mutations.cancel.mutate(summaryJobId)}
      />
      <div className="qa-workspace" style={workspaceStyle}>
        {conversations.isPending ? (
          <aside className="qa-conversations" role="status">
            正在加载对话…
          </aside>
        ) : (
          <ConversationList
            items={items}
            selectedId={selectedId}
            onSelect={selectConversation}
            onNew={() => setCreateOpen(true)}
            hasMore={Boolean(conversations.hasNextPage)}
            loadingMore={conversations.isFetchingNextPage}
            onLoadMore={() => void conversations.fetchNextPage()}
          />
        )}
        <PaneResizer
          label="调节学习线程栏宽度"
          value={split.leftWidth}
          min={180}
          max={340}
          direction={1}
          onChange={split.updateLeft}
          onReset={split.resetLeft}
        />
        <main className="qa-thread">
          {selectedId ? (
            <>
              {messages.isPending ? (
                <div className="qa-welcome" role="status">
                  正在加载消息…
                </div>
              ) : (
                <MessageList
                  messages={currentMessages}
                  activeCitationId={activeCitationId}
                  onSources={showSources}
                  onRetry={(item) =>
                    mutations.retry.mutate({
                      messageId: item.message_id,
                      conversationId: item.conversation_id,
                      clientRequestId: newClientRequestId(),
                    })
                  }
                  busy={busy}
                  hasOlder={Boolean(messages.hasNextPage)}
                  loadingOlder={messages.isFetchingNextPage}
                  onLoadOlder={() => void messages.fetchNextPage()}
                  notesEnabled={enabled && notesEnabled}
                />
              )}
              <QaComposer
                busy={busy}
                documentCount={boundDocs.length}
                onSubmit={(question, mode) =>
                  mutations.ask.mutate({
                    conversationId: selectedId,
                    question,
                    mode,
                    clientRequestId: newClientRequestId(),
                  })
                }
              />
            </>
          ) : (
            <div className="qa-welcome">
              <h2>从一个问题，开始理解</h2>
              <p>文档范围创建后保持固定，避免后续回答悄悄改变证据边界。</p>
              <Button onClick={() => setCreateOpen(true)}>选择文档</Button>
            </div>
          )}
        </main>
        <PaneResizer
          label="调节引用证据栏宽度"
          value={split.rightWidth}
          min={240}
          max={440}
          direction={-1}
          onChange={split.updateRight}
          onReset={split.resetRight}
        />
        <SourcePanel
          sources={sources}
          activeCitationId={activeCitationId}
          messageId={sourceMessageId}
          notesEnabled={enabled && notesEnabled}
        />
      </div>
      {conversationOpen ? (
        <QaDrawer
          title="选择对话"
          className="qa-overlay__panel--conversations"
          returnFocusTo={conversationTrigger}
          onClose={() => setConversationOpen(false)}
        >
          <ConversationList
            items={items}
            selectedId={selectedId}
            onSelect={selectConversation}
            onNew={() => {
              setConversationOpen(false);
              setCreateOpen(true);
            }}
            onDeleteSelected={
              selectedId
                ? () => {
                    setConversationOpen(false);
                    setDeleteOpen(true);
                  }
                : undefined
            }
            hasMore={Boolean(conversations.hasNextPage)}
            loadingMore={conversations.isFetchingNextPage}
            onLoadMore={() => void conversations.fetchNextPage()}
          />
        </QaDrawer>
      ) : null}
      {sourceOpen ? (
        <QaDrawer
          title="引用证据"
          className="qa-overlay__panel--sources"
          returnFocusTo={sourceTrigger}
          onClose={() => setSourceOpen(false)}
        >
          <SourcePanel
            sources={sources}
            activeCitationId={activeCitationId}
            messageId={sourceMessageId}
            notesEnabled={enabled && notesEnabled}
          />
        </QaDrawer>
      ) : null}
      {createOpen ? (
        <QaOverlay title="新建对话" onClose={() => setCreateOpen(false)}>
          <div className="qa-document-picker">
            <p>选择 1–10 篇文档。创建后范围固定；需要更换范围时请新建对话。</p>
            {documents.isPending ? (
              <p role="status">正在加载文档…</p>
            ) : (
              <ul>
                {(documents.data?.items ?? []).map((doc) => (
                  <li key={doc.document_id}>
                    <label>
                      <input
                        type="checkbox"
                        checked={selectedDocuments.has(doc.document_id)}
                        disabled={
                          !selectedDocuments.has(doc.document_id) && selectedDocuments.size >= 10
                        }
                        onChange={(event) =>
                          setSelectedDocuments((current) => {
                            const next = new Set(current);
                            if (event.target.checked) next.add(doc.document_id);
                            else next.delete(doc.document_id);
                            return next;
                          })
                        }
                      />
                      <span>{doc.name}</span>
                    </label>
                  </li>
                ))}
              </ul>
            )}
            <footer>
              <Button hierarchy="secondary" onClick={() => setCreateOpen(false)}>
                取消
              </Button>
              <Button
                disabled={!selectedDocuments.size}
                loading={mutations.create.isPending}
                onClick={createConversation}
              >
                创建对话
              </Button>
            </footer>
            {mutations.create.error ? (
              <p className="qa-error" role="alert">
                {message(mutations.create.error, "创建失败")}
              </p>
            ) : null}
          </div>
        </QaOverlay>
      ) : null}
      {deleteOpen && conversation.data ? (
        <QaDeleteDialog title="永久删除对话" onClose={() => setDeleteOpen(false)}>
          <div className="qa-delete-copy">
            <p>
              将永久删除“{conversation.data.title}”的消息、引用、摘要和问答记忆。此操作不可撤销。
            </p>
            <footer>
              <Button hierarchy="secondary" onClick={() => setDeleteOpen(false)}>
                取消
              </Button>
              <Button
                hierarchy="danger"
                loading={mutations.remove.isPending || deletionActive}
                onClick={() =>
                  mutations.remove.mutate(conversation.data!.conversation_id, {
                    onSuccess: (item) => setDeletionId(item.deletion_id),
                  })
                }
              >
                永久删除
              </Button>
            </footer>
            {deletion.data ? (
              <p role="status">
                {deletion.data.stage} · {deletion.data.status}
              </p>
            ) : null}
            {mutations.remove.error ? (
              <p className="qa-error" role="alert">
                {message(mutations.remove.error, "删除失败，请重试")}
              </p>
            ) : null}
          </div>
        </QaDeleteDialog>
      ) : null}
    </article>
  );
}
