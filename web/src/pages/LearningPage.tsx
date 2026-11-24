import { useQuery, useQueryClient } from "@tanstack/react-query";
import { CheckCircle2, Circle } from "lucide-react";
import { useEffect, useRef, useState, type FormEvent, type ReactNode } from "react";
import { Link } from "react-router-dom";

import { ApiError } from "../api/client";
import { useAuth } from "../auth/AuthProvider";
import { Button } from "../components/Button/Button";
import { ConstellationMark } from "../components/ui/ConstellationMark";
import { StatusBadge } from "../components/ui/StatusBadge";
import type { DocumentListResponse } from "../features/documents/types";
import { createPlan, updateTask } from "../features/learning/api";
import type {
  CreatePlan,
  LearningPlan,
  LearningTask,
  Page,
  UpdateTask,
} from "../features/learning/types";

function safeMessage(error: unknown) {
  return error instanceof ApiError ? error.message : "请求未完成，请稍后重试。";
}

export function LearningPage() {
  const auth = useAuth();
  if (auth.status !== "authenticated") return null;
  return <LearningWorkspace key={`${auth.username}:${auth.csrfToken}`} username={auth.username} />;
}

function ResourcePage<T>({
  username,
  path,
  renderItem,
  empty,
}: {
  username: string;
  path: string;
  renderItem: (item: T) => ReactNode;
  empty: string;
}) {
  const auth = useAuth();
  const [cursor, setCursor] = useState<string | null>(null);
  const query = useQuery({
    queryKey: ["learning", username, path, cursor],
    gcTime: 0,
    retry: false,
    queryFn: ({ signal }) =>
      auth.request<Page<T>>(
        `${path}${path.includes("?") ? "&" : "?"}limit=20${cursor ? `&cursor=${encodeURIComponent(cursor)}` : ""}`,
        { signal, cache: "no-store" },
      ),
  });
  return (
    <>
      {query.isPending ? (
        <p className="learning-muted" role="status">正在加载…</p>
      ) : query.error ? (
        <div className="learning-alert" role="alert">
          <p>{safeMessage(query.error)}</p>
          <Button
            hierarchy="secondary"
            onClick={() => {
              if (cursor) setCursor(null);
              else void query.refetch();
            }}
          >
            重新加载
          </Button>
        </div>
      ) : (
        <>
          {query.data?.items.length ? (
            <ul className="learning-list">{query.data.items.map(renderItem)}</ul>
          ) : (
            <div className="learning-empty">
              <ConstellationMark size="sm" />
              <p>{empty}</p>
            </div>
          )}
          <div className="learning-pagination">
            <Button hierarchy="ghost" disabled={!cursor} onClick={() => setCursor(null)}>
              回到第一页
            </Button>
            <Button
              hierarchy="secondary"
              disabled={!query.data?.next_cursor || query.isFetching}
              onClick={() => setCursor(query.data!.next_cursor)}
            >
              下一页
            </Button>
          </div>
        </>
      )}
    </>
  );
}

function TaskItem({ task, username }: { task: LearningTask; username: string }) {
  const auth = useAuth();
  const client = useQueryClient();
  const alive = useRef(true);
  const pending = useRef(false);
  const attempt = useRef<UpdateTask | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);

  useEffect(() => {
    alive.current = true;
    return () => {
      alive.current = false;
    };
  }, []);

  async function toggle() {
    if (pending.current) return;
    if (
      !attempt.current ||
      attempt.current.expected_version !== task.version ||
      attempt.current.completed === task.completed
    ) {
      attempt.current = {
        request_id: crypto.randomUUID(),
        expected_version: task.version,
        completed: !task.completed,
      };
    }
    pending.current = true;
    setBusy(true);
    setError(null);
    try {
      await updateTask(auth.request, task.id, attempt.current);
      if (alive.current) {
        attempt.current = null;
        await client.invalidateQueries({ queryKey: ["learning", username] });
      }
    } catch (failure) {
      if (alive.current) setError(failure);
    } finally {
      pending.current = false;
      if (alive.current) setBusy(false);
    }
  }

  return (
    <li className={`learning-item${task.completed ? " learning-item--completed" : ""}`}>
      <div className="learning-item__content">
        <div className="learning-item__title-row">
          <h3>{task.title}</h3>
          <StatusBadge
            tone={task.completed ? "success" : "warning"}
            icon={
              task.completed ? (
                <CheckCircle2 size={13} strokeWidth={1.8} />
              ) : (
                <Circle size={13} strokeWidth={1.8} />
              )
            }
          >
            {task.completed ? "已完成" : "未完成"}
          </StatusBadge>
        </div>
        <p className="learning-item__meta tabular-nums">
          {task.due_date} · 计划 {task.duration_minutes} 分钟 · {task.completed ? "已完成" : "未完成"}
        </p>
      </div>
      <Button hierarchy="secondary" size="sm" loading={busy} onClick={() => void toggle()}>
        {task.completed ? "恢复未完成" : "标记完成"}
      </Button>
      {error ? (
        <div className="learning-item__error" role="alert">
          <p>{safeMessage(error)}</p>
          <Button
            hierarchy="ghost"
            size="sm"
            onClick={() => {
              attempt.current = null;
              setError(null);
              void client.invalidateQueries({ queryKey: ["learning", username] });
            }}
          >
            刷新任务
          </Button>
        </div>
      ) : null}
    </li>
  );
}

function LearningWorkspace({ username }: { username: string }) {
  const auth = useAuth();
  const client = useQueryClient();
  const alive = useRef(true);
  const submitting = useRef(false);

  useEffect(() => {
    alive.current = true;
    return () => {
      alive.current = false;
    };
  }, []);

  const [section, setSection] = useState<"today" | "plans">("today");
  const [bucket, setBucket] = useState("today");
  const [selected, setSelected] = useState<LearningPlan | null>(null);
  const [documentId, setDocumentId] = useState("");
  const [title, setTitle] = useState("");
  const [days, setDays] = useState("7");
  const [minutes, setMinutes] = useState("30");
  const [zone] = useState(() => Intl.DateTimeFormat().resolvedOptions().timeZone);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const [feedback, setFeedback] = useState("");
  const attempt = useRef<{ signature: string; input: CreatePlan } | null>(null);

  const documents = useQuery({
    queryKey: ["learning", username, "documents"],
    gcTime: 0,
    retry: false,
    queryFn: ({ signal }) =>
      auth.request<DocumentListResponse>("/api/v1/documents", { signal, cache: "no-store" }),
  });

  const valid =
    !!documentId &&
    !!title.trim() &&
    title.trim().length <= 100 &&
    Number.isInteger(Number(days)) &&
    +days >= 1 &&
    +days <= 365 &&
    Number.isInteger(Number(minutes)) &&
    +minutes >= 5 &&
    +minutes <= 480 &&
    !!zone;

  async function submit(event: FormEvent) {
    event.preventDefault();
    if (!valid || submitting.current) return;
    const input = {
      document_id: documentId,
      title: title.trim(),
      days: +days,
      daily_minutes: +minutes,
      timezone: zone,
    };
    const signature = JSON.stringify(input);
    if (attempt.current?.signature !== signature) {
      attempt.current = {
        signature,
        input: { ...input, request_id: crypto.randomUUID() },
      };
    }
    submitting.current = true;
    setBusy(true);
    setError(null);
    setFeedback("");
    try {
      const result = await createPlan(auth.request, attempt.current!.input);
      if (!alive.current) return;
      attempt.current = null;
      setSelected(result.plan);
      setTitle("");
      setFeedback("计划已创建。");
      await client.invalidateQueries({ queryKey: ["learning", username] });
    } catch (failure) {
      if (alive.current) setError(failure);
    } finally {
      submitting.current = false;
      if (alive.current) setBusy(false);
    }
  }

  return (
    <article className="learning-page">
      <header className="learning-header">
        <div>
          <h1>学习中心</h1>
          <p>把阅读安排成可执行的小任务。计划分钟数不代表实际学习时长。</p>
        </div>
      </header>
      <nav className="learning-actions" aria-label="学习中心视图">
        <Button
          hierarchy={section === "today" ? "primary" : "secondary"}
          aria-pressed={section === "today"}
          onClick={() => setSection("today")}
        >
          今日任务
        </Button>
        <Button
          hierarchy={section === "plans" ? "primary" : "secondary"}
          aria-pressed={section === "plans"}
          onClick={() => setSection("plans")}
        >
          学习计划
        </Button>
      </nav>
      {section === "today" ? (
        <section className="learning-card">
          <div className="learning-card__header">
            <div>
              <h2>任务清单</h2>
              <p className="learning-muted">日期按各计划创建时保存的时区计算。</p>
            </div>
            <label className="learning-bucket-filter">
              <span>任务范围</span>
              <select value={bucket} onChange={(event) => setBucket(event.target.value)}>
                <option value="today">今日未完成</option>
                <option value="overdue">逾期未完成</option>
                <option value="completed">已完成</option>
              </select>
            </label>
          </div>
          <ResourcePage<LearningTask>
            key={bucket}
            username={username}
            path={`/api/v1/learning/today?bucket=${bucket}`}
            empty="当前没有任务。"
            renderItem={(task) => <TaskItem key={task.id} task={task} username={username} />}
          />
        </section>
      ) : (
        <div className="learning-grid">
          <section className="learning-card">
            <h2>创建学习计划</h2>
            {documents.isPending ? (
              <p className="learning-muted" role="status">正在加载文档…</p>
            ) : documents.error ? (
              <div className="learning-alert" role="alert">
                <p>{safeMessage(documents.error)}</p>
                <Button hierarchy="secondary" onClick={() => void documents.refetch()}>
                  重新加载文档
                </Button>
              </div>
            ) : !documents.data?.items.length ? (
              <p className="learning-muted">
                还没有可学习的文档。<Link to="/documents">前往文档库导入</Link>
              </p>
            ) : null}
            <form onSubmit={(event) => void submit(event)}>
              <fieldset disabled={busy}>
                <label>
                  学习文档
                  <select
                    value={documentId}
                    required
                    onChange={(event) => setDocumentId(event.target.value)}
                  >
                    <option value="">请选择文档</option>
                    {documents.data?.items.map((doc) => (
                      <option key={doc.document_id} value={doc.document_id}>
                        {doc.name}
                      </option>
                    ))}
                  </select>
                </label>
                <label>
                  计划名称
                  <input
                    required
                    maxLength={100}
                    placeholder="例如：精读 RAG 检索架构论文…"
                    value={title}
                    onChange={(event) => setTitle(event.target.value)}
                  />
                </label>
                <div className="learning-numbers">
                  <label>
                    周期（天）
                    <input
                      type="number"
                      required
                      min={1}
                      max={365}
                      step={1}
                      value={days}
                      onChange={(event) => setDays(event.target.value)}
                    />
                  </label>
                  <label>
                    每日计划（分钟）
                    <input
                      type="number"
                      required
                      min={5}
                      max={480}
                      step={1}
                      value={minutes}
                      onChange={(event) => setMinutes(event.target.value)}
                    />
                  </label>
                </div>
                <p className="learning-timezone">
                  计划时区：{zone || "无法识别时区，请检查浏览器设置"}
                </p>
                <Button type="submit" disabled={!valid} loading={busy}>
                  创建计划
                </Button>
              </fieldset>
            </form>
            {error ? <p className="learning-alert" role="alert">{safeMessage(error)}</p> : null}
            {feedback ? <p className="learning-feedback" role="status">{feedback}</p> : null}
          </section>
          <section className="learning-card">
            <h2>我的学习计划</h2>
            <ResourcePage<LearningPlan>
              username={username}
              path="/api/v1/learning/plans"
              empty="还没有学习计划。"
              renderItem={(plan) => {
                const progressRatio = plan.task_count
                  ? Math.round((plan.completed_count / plan.task_count) * 100)
                  : 0;
                return (
                  <li key={plan.id} className="learning-item learning-plan-item">
                    <div className="learning-item__content">
                      <div className="learning-item__title-row">
                        <h3>{plan.title}</h3>
                        <StatusBadge
                          tone={plan.completed_count === plan.task_count && plan.task_count > 0 ? "success" : "brand"}
                        >
                          <span className="tabular-nums">
                            {plan.completed_count}/{plan.task_count}
                          </span>
                        </StatusBadge>
                      </div>
                      <p className="learning-item__meta tabular-nums">
                        {plan.document_name} · {plan.completed_count}/{plan.task_count} 项已完成
                      </p>
                      <p className="learning-item__submeta tabular-nums">
                        {plan.start_date} — {plan.target_date} · {plan.timezone}
                      </p>
                      <div className="learning-progress" aria-hidden="true">
                        <span style={{ width: `${progressRatio}%` }} />
                      </div>
                    </div>
                    <Button hierarchy="secondary" size="sm" onClick={() => setSelected(plan)}>
                      查看任务
                    </Button>
                  </li>
                );
              }}
            />
          </section>
          {selected ? (
            <section className="learning-card learning-detail">
              <div className="learning-card__header">
                <h2>{selected.title} · 任务</h2>
                <Button hierarchy="ghost" size="sm" onClick={() => setSelected(null)}>
                  关闭计划详情
                </Button>
              </div>
              <ResourcePage<LearningTask>
                key={selected.id}
                username={username}
                path={`/api/v1/learning/plans/${encodeURIComponent(selected.id)}/tasks`}
                empty="该计划没有可显示的任务。"
                renderItem={(task) => <TaskItem key={task.id} task={task} username={username} />}
              />
            </section>
          ) : null}
        </div>
      )}
    </article>
  );
}
