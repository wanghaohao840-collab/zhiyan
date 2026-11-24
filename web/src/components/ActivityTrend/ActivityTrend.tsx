import { useId } from "react";
import type { ActivityDay } from "../../features/insights/types";
import { ConstellationMark } from "../ui/ConstellationMark";

export function ActivityTrend({ days }: { days: ActivityDay[] }) {
  const gradient = useId();
  const total = days.reduce((sum, day) => sum + day.total, 0);
  if (!days.length || !total) {
    return (
      <div className="trend-empty">
        <ConstellationMark size="sm" />
        <p>还没有学习活动</p>
        <small>导入文档、提问或记录笔记后，积累会在这里显现。</small>
      </div>
    );
  }
  const max = Math.max(1, ...days.map((day) => day.total));
  const points = days.map((day, index) => ({
    x: days.length === 1 ? 160 : 12 + (index * 296) / (days.length - 1),
    y: 108 - (day.total / max) * 92,
  }));
  const line = points.map(({ x, y }) => `${x},${y}`).join(" ");
  return (
    <div className="activity-trend">
      <svg viewBox="0 0 320 120" role="img" aria-label={`学习活动趋势，共 ${total} 次活动`}>
        <defs>
          <linearGradient id={gradient} x1="0%" y1="0%" x2="0%" y2="100%">
            <stop stopColor="var(--color-brand-600)" stopOpacity="0.22" />
            <stop offset="1" stopColor="var(--color-brand-600)" stopOpacity="0" />
          </linearGradient>
        </defs>
        <path d="M12 108H308 M12 62H308 M12 16H308" stroke="var(--color-border-subtle)" strokeDasharray="3 5" />
        <polygon points={`${points[0].x},108 ${line} ${points.at(-1)!.x},108`} fill={`url(#${gradient})`} />
        <polyline points={line} fill="none" stroke="var(--color-brand-600)" strokeWidth="2" strokeLinejoin="round" />
        {points.length === 1 ? (
          <circle cx={points[0].x} cy={points[0].y} r="3.5" fill="var(--color-brand-600)" />
        ) : null}
      </svg>
      <div className="activity-trend__dates tabular-nums">
        <span>{days[0].date.slice(5)}</span>
        <span>{days.at(-1)!.date.slice(5)}</span>
      </div>
      <details>
        <summary>查看每日活动</summary>
        <div className="activity-trend__table">
          <table className="tabular-nums">
            <caption>每日学习活动（UTC）</caption>
            <thead>
              <tr>
                <th>日期</th>
                <th>文档</th>
                <th>问答</th>
                <th>笔记</th>
                <th>合计</th>
              </tr>
            </thead>
            <tbody>
              {days.map((day) => (
                <tr key={day.date}>
                  <th scope="row">{day.date}</th>
                  <td>{day.documents}</td>
                  <td>{day.questions}</td>
                  <td>{day.notes}</td>
                  <td>{day.total}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </details>
    </div>
  );
}
