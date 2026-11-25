import { render, screen } from "@testing-library/react";
import { expect, it } from "vitest";
import { ActivityTrend } from "./ActivityTrend";

it("shows an honest empty state", () => {
  render(<ActivityTrend days={[]} />);
  expect(screen.getByText("还没有学习活动")).toBeVisible();
  expect(screen.queryByRole("img")).not.toBeInTheDocument();
});

it("exposes the original UTC date and single day count", () => {
  render(
    <ActivityTrend
      days={[{ date: "2026-09-09", documents: 1, questions: 2, notes: 0, total: 3 }]}
    />,
  );
  expect(screen.getByRole("img", { name: "学习活动趋势，共 3 次活动" })).toBeVisible();
  expect(screen.getByText("2026-09-09")).toBeInTheDocument();
});

it("renders smooth monotone curve from real daily totals without invented mastery scores", () => {
  render(
    <ActivityTrend
      days={[
        { date: "2026-09-08", documents: 1, questions: 0, notes: 0, total: 1 },
        { date: "2026-09-09", documents: 0, questions: 2, notes: 0, total: 2 },
      ]}
    />,
  );
  expect(screen.getByRole("img", { name: "学习活动趋势，共 3 次活动" })).toBeVisible();
  expect(screen.queryByText("87%")).not.toBeInTheDocument();
});
