import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen } from "@/test/test-utils";
import { UsageCard } from "./usage-card";
import { useDashboardStore } from "@/stores/dashboard";
import type { DailyStats, StatsTotals, StatsMonth } from "@/types/api";

function makeDays(count: number): DailyStats[] {
  return Array.from({ length: count }, (_, i) => ({
    date: `2026-04-${String(i + 1).padStart(2, "0")}`,
    cost: (i + 1) * 0.10,
    messages: (i + 1) * 10,
    llm_calls: (i + 1) * 4,
    tokens: (i + 1) * 1000,
  }));
}

const defaultTotals: StatsTotals = {
  cost: 12.87,
  messages: 892,
  llm_calls: 3568,
  tokens: 1234000,
  days_active: 14,
};

const defaultMonth: StatsMonth = {
  cost: 4.32,
  messages: 340,
  llm_calls: 1360,
  tokens: 450000,
};

describe("UsageCard", () => {
  beforeEach(() => {
    // Reset store state for each test
    useDashboardStore.setState({
      statsHistory: [],
      statsTotals: null,
      statsMonth: null,
      statsLoading: false,
      statsError: null,
      fetchStatsHistory: vi.fn().mockResolvedValue(undefined),
    });
  });

  it("renders 'no history' when empty", () => {
    render(<UsageCard />);
    expect(screen.getByText("No usage history yet")).toBeInTheDocument();
  });

  it("renders monthly cost and messages", () => {
    useDashboardStore.setState({
      statsHistory: makeDays(10),
      statsTotals: defaultTotals,
      statsMonth: defaultMonth,
    });
    render(<UsageCard />);

    expect(screen.getByTestId("usage-monthly-cost")).toHaveTextContent("$4.32");
    expect(screen.getByTestId("usage-monthly-messages")).toHaveTextContent("340 messages");
  });

  it("renders spark line SVG with data points", () => {
    useDashboardStore.setState({
      statsHistory: makeDays(5),
      statsTotals: defaultTotals,
      statsMonth: defaultMonth,
    });
    render(<UsageCard />);

    const svg = screen.getByTestId("spark-line");
    expect(svg).toBeInTheDocument();
    expect(svg.tagName).toBe("svg");

    const polyline = svg.querySelector("polyline");
    expect(polyline).not.toBeNull();
    expect(polyline?.getAttribute("points")).toBeTruthy();
  });

  it("renders total in secondary text", () => {
    useDashboardStore.setState({
      statsHistory: makeDays(3),
      statsTotals: defaultTotals,
      statsMonth: defaultMonth,
    });
    render(<UsageCard />);

    const total = screen.getByTestId("usage-total");
    expect(total).toHaveTextContent("$12.87");
    expect(total).toHaveTextContent("14 days");
  });

  it("hides spark line when only 1 day of data", () => {
    useDashboardStore.setState({
      statsHistory: makeDays(1),
      statsTotals: defaultTotals,
      statsMonth: defaultMonth,
    });
    render(<UsageCard />);

    // SparkLine returns null when data.length < 2
    expect(screen.queryByTestId("spark-line")).not.toBeInTheDocument();
  });

  it("formats cost with $ and 2 decimal places", () => {
    useDashboardStore.setState({
      statsHistory: makeDays(2),
      statsTotals: { ...defaultTotals, cost: 0.05 },
      statsMonth: { ...defaultMonth, cost: 0.05 },
    });
    render(<UsageCard />);

    expect(screen.getByTestId("usage-monthly-cost")).toHaveTextContent("$0.05");
  });

  it("fills missing days with 0 in spark line", () => {
    // Days 1, 3, 5 — gaps on 2, 4
    const sparse: DailyStats[] = [
      { date: "2026-04-01", cost: 0.1, messages: 1, llm_calls: 1, tokens: 100 },
      { date: "2026-04-03", cost: 0.3, messages: 3, llm_calls: 3, tokens: 300 },
      { date: "2026-04-05", cost: 0.5, messages: 5, llm_calls: 5, tokens: 500 },
    ];

    useDashboardStore.setState({
      statsHistory: sparse,
      statsTotals: defaultTotals,
      statsMonth: defaultMonth,
    });
    render(<UsageCard />);

    // SparkLine should render — 5 points (3 real + 2 filled)
    const svg = screen.getByTestId("spark-line");
    const polyline = svg.querySelector("polyline");
    const points = polyline?.getAttribute("points")?.split(" ") ?? [];
    expect(points.length).toBe(5); // 5 days including gaps
  });

  it("calls fetchStatsHistory on mount", () => {
    const fetchFn = vi.fn().mockResolvedValue(undefined);
    useDashboardStore.setState({ fetchStatsHistory: fetchFn });
    render(<UsageCard />);
    expect(fetchFn).toHaveBeenCalledWith(30);
  });

  it("has correct data-testid", () => {
    useDashboardStore.setState({
      statsHistory: makeDays(2),
      statsTotals: defaultTotals,
      statsMonth: defaultMonth,
    });
    render(<UsageCard />);
    expect(screen.getByTestId("usage-card")).toBeInTheDocument();
  });
});
