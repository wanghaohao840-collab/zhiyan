import { AlertCircle, CheckCircle2, Clock3 } from "lucide-react";
import { Link } from "react-router-dom";

import { ApiError } from "../api/client";
import { useAuth } from "../auth/AuthProvider";
import { ActivityTrend } from "../components/ActivityTrend/ActivityTrend";
import { Button } from "../components/Button/Button";
import { NavigationIcon } from "../components/NavigationIcon/NavigationIcon";
import { useOverview } from "../features/insights/queries";
import { useTodayLearningSummary } from "../features/learning/queries";
import { useTheme } from "../theme/ThemeProvider";

const formatDate = (value: string | null) =>
  value
    ? new Intl.DateTimeFormat("zh-CN", { dateStyle: "medium" }).format(new Date(value))
    : "时间未知";

/**
 * 根据本地真实小时返回问候语：05:00-11:59 早上好，12:00-17:59 下午好，其余晚上好。
 */
export function getTimeGreeting(hour = new Date().getHours()): "早上好" | "下午好" | "晚上好" {
  if (hour >= 5 && hour < 12) return "早上好";
  if (hour >= 12 && hour < 18) return "下午好";
  return "晚上好";
}

/**
 * 精确计算真实活跃占比（仅基于服务端返回的 active_days / window_days）。
 */
export function computeActiveRate(activeDays: number, windowDays: number): number {
  if (windowDays <= 0 || activeDays <= 0) return 0;
  return Math.min(100, Math.round((activeDays / windowDays) * 100));
}

function formatBucketCount(count: number, hasMore: boolean): string {
  return hasMore ? `${count}+` : String(count);
}

export function OverviewPage() {
  const auth = useAuth();
  const { theme } = useTheme();
  const query = useOverview();
  const todayLearning = useTodayLearningSummary();
  const isMidnight = theme === "midnight";

  if (query.isPending) {
    return (
      <section className="insight-state" role="status">
        正在加载学习概览…
      </section>
    );
  }
  if (query.error || !query.data) {
    return (
      <section className="insight-state" role="alert">
        <h1>学习概览</h1>
        <p>{query.error instanceof ApiError ? query.error.message : "概览加载失败"}</p>
        <Button hierarchy="secondary" onClick={() => void query.refetch()}>
          重新加载
        </Button>
      </section>
    );
  }

  const { stats, recent_documents: documents, recent_questions: questions } = query.data;
  const totalActions = stats.activity.reduce((sum, day) => sum + day.total, 0);
  const activeRate = computeActiveRate(stats.active_days, stats.window_days);
  const greeting = getTimeGreeting();
  const todayData = todayLearning.data;
  const assets = [
    { count: stats.document_count, label: "文档", path: "/documents" },
    { count: stats.completed_question_count, label: "已完成问答", path: "/qa" },
    { count: stats.note_count, label: "学习笔记", path: "/notes" },
    { count: stats.report_count, label: "学习报告", path: "/insights" },
  ];

  return (
    <article className="overview-page">
      <header className="insight-heading overview-heading">
        <div>
          <p className="overview-eyebrow">
            {isMidnight
              ? "智能工作台 · DEEP KNOWLEDGE SPACE"
              : "学习概览 · YOUR KNOWLEDGE, GROWING"}
          </p>
          <h1>
            {greeting}，{auth.status === "authenticated" ? auth.username : "学习者"}
          </h1>
          <p>
            {isMidnight
              ? "今天也是积累的一天，在深邃中连接你的文档与思考。"
              : "继续学习，让知识在时间中沉淀。"}
          </p>
        </div>
        <Link className="button button--primary button--md" to="/documents">
          导入文档
        </Link>
      </header>
      <div className="overview-highlights">
        <section className="insight-panel overview-trend">
          <header>
            <h2>近 {stats.window_days} 天学习活动</h2>
            <span className="overview-dot tabular-nums">
              {totalActions > 0 ? `累计 ${totalActions} 次` : "持续积累"}
            </span>
          </header>
          <div className="overview-activity-value">
            <strong className="tabular-nums">{activeRate}%</strong>
            <span className="tabular-nums">
              活跃占比 · {stats.active_days}/{stats.window_days} 个活跃日
            </span>
          </div>
          <ActivityTrend days={stats.activity} />
        </section>

        <section className="insight-panel overview-today" aria-label="今日学习任务">
          <header>
            <h2>今日学习任务</h2>
            <Link to="/learning">学习中心 →</Link>
          </header>
          {todayLearning.isPending ? (
            <p className="overview-today__status" role="status">
              正在读取今日任务…
            </p>
          ) : todayLearning.error || !todayData ? (
            <div className="overview-today__fallback">
              <p>今日任务暂时不可用，请前往学习中心查看。</p>
              <Link to="/learning">打开学习中心</Link>
            </div>
          ) : (
            <>
              <ul className="overview-today__metrics">
                <li>
                  <span className="overview-today__icon overview-today__icon--brand" aria-hidden="true">
                    <Clock3 size={15} strokeWidth={1.6} />
                  </span>
                  <div className="overview-today__copy">
                    <strong className="tabular-nums">
                      {formatBucketCount(todayData.todayCount, todayData.todayHasMore)}
                    </strong>
                    <span>今日待完成</span>
                  </div>
                </li>
                <li>
                  <span className="overview-today__icon overview-today__icon--success" aria-hidden="true">
                    <CheckCircle2 size={15} strokeWidth={1.6} />
                  </span>
                  <div className="overview-today__copy">
                    <strong className="tabular-nums">
                      {formatBucketCount(todayData.completedCount, todayData.completedHasMore)}
                    </strong>
                    <span>今日已完成</span>
                  </div>
                </li>
                <li>
                  <span className="overview-today__icon overview-today__icon--warning" aria-hidden="true">
                    <AlertCircle size={15} strokeWidth={1.6} />
                  </span>
                  <div className="overview-today__copy">
                    <strong className="tabular-nums">
                      {formatBucketCount(todayData.overdueCount, todayData.overdueHasMore)}
                    </strong>
                    <span>逾期未完成</span>
                  </div>
                </li>
              </ul>
              <p className="overview-today__next">
                {todayData.nextTask
                  ? `下一步：${todayData.nextTask.title}`
                  : "今日暂无待办，可在学习中心按文档制定学习计划。"}
              </p>
            </>
          )}
        </section>

        <section className="insight-panel overview-assets" aria-label="学习指标">
          <header>
            <h2>你的知识资产</h2>
            <span className="overview-kicker">每一份积累，都有价值</span>
          </header>
          <div className="overview-assets__grid">
            {assets.map((item) => (
              <Link key={item.path} to={item.path}>
                <span className="overview-asset-icon">
                  <NavigationIcon path={item.path} />
                </span>
                <strong className="tabular-nums">{item.count}</strong>
                <span>{item.label}</span>
              </Link>
            ))}
          </div>
        </section>

        <aside className="overview-reflection">
          <span className="overview-kicker">留一点时间，给理解</span>
          <span className="overview-quote-mark" aria-hidden="true">
            “
          </span>
          {isMidnight ? (
            <p>
              知识的价值，
              <br />
              在于被不断地使用与连接。
            </p>
          ) : (
            <p>
              积累不是存储，
              <br />
              而是让思想生长。
            </p>
          )}
          <span className="overview-reflection__line" />
          <small>
            从一个问题开始，
            <br />
            把阅读连接成自己的知识。
          </small>
          <Link to="/qa">
            开始一次探索 <span aria-hidden="true">→</span>
          </Link>
        </aside>
      </div>
      <div className="overview-columns">
        <section className="insight-panel">
          <header>
            <h2>最近文档</h2>
            <Link to="/documents">查看全部 →</Link>
          </header>
          {documents.length ? (
            <ul className="insight-list">
              {documents.map((item) => (
                <li key={item.document_id}>
                  <Link to={`/qa?documents=${encodeURIComponent(item.document_id)}`}>
                    <NavigationIcon path="/documents" />
                    {item.name}
                  </Link>
                  <span className="tabular-nums">{formatDate(item.loaded_at)}</span>
                </li>
              ))}
            </ul>
          ) : (
            <div className="insight-empty">
              <span className="overview-empty-icon" aria-hidden="true">
                <NavigationIcon path="/documents" />
              </span>
              <p>还没有可学习的文档。</p>
              <Link to="/documents">导入第一篇文档</Link>
            </div>
          )}
        </section>
        <section className="insight-panel">
          <header>
            <h2>最近提问</h2>
            <Link to="/qa">进入问答 →</Link>
          </header>
          {questions.length ? (
            <ul className="insight-list">
              {questions.map((item, index) => (
                <li key={`${item.asked_at}-${index}`}>
                  <span>{item.question}</span>
                  <small>{item.document_names.join("、") || "未关联文档"}</small>
                </li>
              ))}
            </ul>
          ) : (
            <div className="insight-empty">
              <span className="overview-empty-icon" aria-hidden="true">
                <NavigationIcon path="/qa" />
              </span>
              <p>完成一次基于文档的问答后，会在这里继续。</p>
              <Link to="/qa">开始问答</Link>
            </div>
          )}
        </section>
      </div>
      <footer className="overview-footer">
        <span>让知识在时间中沉淀</span>
        <Link to="/insights">查看完整学习洞察与报告 →</Link>
      </footer>
    </article>
  );
}
