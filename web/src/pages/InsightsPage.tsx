import { useEffect, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { ApiError } from "../api/client";
import { Button } from "../components/Button/Button";
import { MarkdownPreview } from "../components/MarkdownPreview/MarkdownPreview";
import { useCreateReport, useReport, useReports, useStats } from "../features/insights/queries";

export function InsightsPage() {
  const [params, setParams] = useSearchParams();
  const tab = params.get("tab") === "reports" ? "reports" : "stats";
  const [selectedId, setSelectedId] = useState<string>();
  const stats = useStats(); const reports = useReports(); const selected = useReport(selectedId); const create = useCreateReport();
  useEffect(() => { if (!selectedId && reports.data?.[0]) setSelectedId(reports.data[0].id); }, [reports.data, selectedId]);
  const switchTab = (next: "stats" | "reports") => setParams(next === "stats" ? {} : { tab: next }, { replace: true });
  const max = Math.max(1, ...(stats.data?.activity.map((day) => day.total) ?? [1]));
  return <article className="insights-page"><header className="insight-heading"><div><h1>学习洞察</h1><p>只统计系统可验证的学习活动</p></div></header>
    <div className="insight-tabs" role="tablist" aria-label="洞察视图"><button role="tab" aria-selected={tab === "stats"} onClick={() => switchTab("stats")}>学习统计</button><button role="tab" aria-selected={tab === "reports"} onClick={() => switchTab("reports")}>学习报告</button></div>
    {tab === "stats" ? <section role="tabpanel">
      {stats.isPending ? <div className="insight-state" role="status">正在加载统计…</div> : stats.error || !stats.data ? <div className="insight-state" role="alert"><p>{stats.error instanceof ApiError ? stats.error.message : "统计加载失败"}</p><Button hierarchy="secondary" onClick={() => void stats.refetch()}>重新加载</Button></div> : <>
        <section className="metric-grid" aria-label="学习统计"><article><strong>{stats.data.document_count}</strong><span>文档</span></article><article><strong>{stats.data.completed_question_count}</strong><span>已完成问答</span></article><article><strong>{stats.data.note_count}</strong><span>笔记</span></article><article><strong>{stats.data.report_count}</strong><span>报告</span></article></section>
        <section className="insight-panel"><header><h2>近 30 天活动</h2><span>{stats.data.active_days} 个活跃日</span></header><div className="activity-chart" role="region" tabIndex={0} aria-label="每日活动次数">{stats.data.activity.map((day) => <div className="activity-day" key={day.date} title={`${day.date}：${day.total} 次`}><span style={{ height: `${Math.max(day.total ? 10 : 2, day.total / max * 100)}%` }} /><small>{day.date.slice(8)}</small><i className="sr-only">{day.date} 共 {day.total} 次活动</i></div>)}</div><p className="insight-footnote">活动包含文档导入、已完成问答和新建笔记；不推断阅读时长或掌握度。</p></section>
      </>}
    </section> : <section className="report-layout" role="tabpanel">
      <aside className="insight-panel report-list"><header><h2>报告历史</h2><Button loading={create.isPending} onClick={() => void create.mutateAsync().then((value) => setSelectedId(value.id)).catch(() => undefined)}>生成报告</Button></header>{create.error ? <p role="alert">报告生成失败，请重试。</p> : null}{reports.isPending ? <p role="status">正在加载报告…</p> : reports.error ? <p role="alert">报告历史加载失败。</p> : reports.data?.length ? <ul>{reports.data.map((report) => <li key={report.id}><button aria-pressed={selectedId === report.id} onClick={() => setSelectedId(report.id)}><strong>{report.title}</strong><span>{new Date(report.created_at).toLocaleString("zh-CN")}</span></button></li>)}</ul> : <div className="insight-empty"><p>尚未生成学习报告。</p></div>}</aside>
      <section className="insight-panel report-preview"><header><h2>报告内容</h2>{selectedId ? <div className="report-downloads"><a href={`/api/v1/insights/reports/${selectedId}/download?format=md`}>Markdown</a><a href={`/api/v1/insights/reports/${selectedId}/download?format=docx`}>Word</a></div> : null}</header>{selectedId && selected.isPending ? <p role="status">正在打开报告…</p> : selected.error ? <p role="alert">报告加载失败。</p> : selected.data ? <MarkdownPreview markdown={selected.data.content} /> : <div className="insight-empty"><p>选择或生成一份报告进行查看。</p><Link to="/overview">返回概览</Link></div>}</section>
    </section>}
  </article>;
}
