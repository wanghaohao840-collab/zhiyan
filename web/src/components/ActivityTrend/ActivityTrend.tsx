import { useId } from "react";

import type { ActivityDay } from "../../features/insights/types";
import { ConstellationMark } from "../ui/ConstellationMark";

type Point = { x: number; y: number };

/**
 * 单调三次 Hermite 样条（Fritsch-Carlson）：生成丝滑且绝不发生过冲变形的趋势曲线路径。
 */
function buildMonotonePath(points: Point[]): string {
  if (points.length === 0) return "";
  if (points.length === 1) return `M ${points[0].x} ${points[0].y}`;
  if (points.length === 2) {
    return `M ${points[0].x} ${points[0].y} L ${points[1].x} ${points[1].y}`;
  }

  const count = points.length;
  const slopes: number[] = [];
  for (let i = 0; i < count - 1; i += 1) {
    const dx = points[i + 1].x - points[i].x;
    const dy = points[i + 1].y - points[i].y;
    slopes.push(dx === 0 ? 0 : dy / dx);
  }

  const tangents: number[] = new Array(count).fill(0);
  tangents[0] = slopes[0];
  tangents[count - 1] = slopes[count - 2];
  for (let i = 1; i < count - 1; i += 1) {
    if (slopes[i - 1] * slopes[i] <= 0) {
      tangents[i] = 0;
    } else {
      tangents[i] = (slopes[i - 1] + slopes[i]) / 2;
    }
  }

  // NOTE: 限制切线斜率避免在极值点附近产生越界波动
  for (let i = 0; i < count - 1; i += 1) {
    if (slopes[i] === 0) {
      tangents[i] = 0;
      tangents[i + 1] = 0;
      continue;
    }
    const alpha = tangents[i] / slopes[i];
    const beta = tangents[i + 1] / slopes[i];
    const norm = Math.hypot(alpha, beta);
    if (norm > 3) {
      const scale = 3 / norm;
      tangents[i] = scale * alpha * slopes[i];
      tangents[i + 1] = scale * beta * slopes[i];
    }
  }

  let path = `M ${points[0].x.toFixed(2)} ${points[0].y.toFixed(2)}`;
  for (let i = 0; i < count - 1; i += 1) {
    const p0 = points[i];
    const p1 = points[i + 1];
    const dx = (p1.x - p0.x) / 3;
    const cp1x = p0.x + dx;
    const cp1y = p0.y + tangents[i] * dx;
    const cp2x = p1.x - dx;
    const cp2y = p1.y - tangents[i + 1] * dx;
    path += ` C ${cp1x.toFixed(2)} ${cp1y.toFixed(2)}, ${cp2x.toFixed(2)} ${cp2y.toFixed(2)}, ${p1.x.toFixed(2)} ${p1.y.toFixed(2)}`;
  }
  return path;
}

export function ActivityTrend({ days }: { days: ActivityDay[] }) {
  const gradientId = useId();
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
  const baselineY = 108;
  const points: Point[] = days.map((day, index) => ({
    x: days.length === 1 ? 160 : 12 + (index * 296) / (days.length - 1),
    y: baselineY - (day.total / max) * 86,
  }));
  const curvePath = buildMonotonePath(points);
  const firstPoint = points[0];
  const lastPoint = points.at(-1)!;
  const areaPath =
    points.length > 1
      ? `${curvePath} L ${lastPoint.x.toFixed(2)} ${baselineY} L ${firstPoint.x.toFixed(2)} ${baselineY} Z`
      : "";

  return (
    <div className="activity-trend">
      <svg
        className="activity-trend__svg"
        viewBox="0 0 320 120"
        role="img"
        aria-label={`学习活动趋势，共 ${total} 次活动`}
      >
        <defs>
          <linearGradient id={gradientId} x1="0%" y1="0%" x2="0%" y2="100%">
            <stop offset="0%" stopColor="var(--color-chart-fill-start)" />
            <stop offset="100%" stopColor="var(--color-chart-fill-end)" />
          </linearGradient>
        </defs>
        <path
          d="M12 108H308 M12 65H308 M12 22H308"
          stroke="var(--color-chart-grid)"
          strokeDasharray="3 5"
        />
        {areaPath ? <path d={areaPath} fill={`url(#${gradientId})`} /> : null}
        <path
          className="activity-trend__line"
          d={curvePath}
          fill="none"
          stroke="var(--color-chart-stroke)"
          strokeWidth="2.4"
          strokeLinecap="round"
          strokeLinejoin="round"
        />
        <circle
          cx={lastPoint.x}
          cy={lastPoint.y}
          r="6"
          fill="var(--color-chart-fill-start)"
        />
        <circle
          cx={lastPoint.x}
          cy={lastPoint.y}
          r="3.2"
          fill="var(--color-chart-stroke)"
        />
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
