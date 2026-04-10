/**
 * Tests for MindAliveCard component.
 *
 * Covers: metric rendering, skeleton fallback, dismiss action,
 * navigation links, animation prop, edge cases.
 */
import { render, screen, fireEvent } from "@/test/test-utils";
import { MindAliveCard } from "./mind-alive-card";
import { useDashboardStore } from "@/stores/dashboard";
import type { SystemStatus } from "@/types/api";

// ── Helpers ──

function makeStatus(overrides: Partial<SystemStatus> = {}): SystemStatus {
  return {
    version: "0.5.25",
    uptime_seconds: 86400,
    mind_name: "test-mind",
    active_conversations: 3,
    memory_concepts: 47,
    memory_episodes: 12,
    llm_cost_today: 1.5,
    llm_calls_today: 42,
    tokens_today: 50000,
    messages_today: 156,
    ...overrides,
  };
}

const noopDismiss = vi.fn();

beforeEach(() => {
  noopDismiss.mockClear();
  useDashboardStore.setState({
    status: null,
  });
});

// ════════════════════════════════════════════════════════
// RENDERING
// ════════════════════════════════════════════════════════
describe("rendering", () => {
  it("renders metrics from store", () => {
    useDashboardStore.setState({ status: makeStatus() });
    render(<MindAliveCard onDismiss={noopDismiss} />);

    expect(screen.getByTestId("mind-alive-card")).toBeInTheDocument();
    expect(screen.getByText("47")).toBeInTheDocument(); // concepts
    expect(screen.getByText("12")).toBeInTheDocument(); // memories
    expect(screen.getByText("3")).toBeInTheDocument();  // channels
    expect(screen.getByText("156")).toBeInTheDocument(); // messages
  });

  it("shows brain icon", () => {
    useDashboardStore.setState({ status: makeStatus() });
    render(<MindAliveCard onDismiss={noopDismiss} />);

    expect(screen.getByText("Your mind is alive.")).toBeInTheDocument();
  });

  it("shows formatted uptime", () => {
    useDashboardStore.setState({
      status: makeStatus({ uptime_seconds: 86400 }), // 1 day
    });
    render(<MindAliveCard onDismiss={noopDismiss} />);

    // formatUptime(86400) = "1d 0h"
    expect(screen.getByText(/1d 0h/)).toBeInTheDocument();
  });

  it("shows uptime in hours when < 1 day", () => {
    useDashboardStore.setState({
      status: makeStatus({ uptime_seconds: 7200 }), // 2h
    });
    render(<MindAliveCard onDismiss={noopDismiss} />);

    expect(screen.getByText(/2h 0m/)).toBeInTheDocument();
  });
});

// ════════════════════════════════════════════════════════
// SKELETON / NULL STATUS
// ════════════════════════════════════════════════════════
describe("null status", () => {
  it("renders skeleton when status is null", () => {
    useDashboardStore.setState({ status: null });
    render(<MindAliveCard onDismiss={noopDismiss} />);

    expect(screen.getByTestId("mind-alive-card")).toBeInTheDocument();
    // Should NOT show metrics
    expect(screen.queryByText("concepts")).not.toBeInTheDocument();
  });
});

// ════════════════════════════════════════════════════════
// DISMISS
// ════════════════════════════════════════════════════════
describe("dismiss", () => {
  it("calls onDismiss when dismiss button clicked", () => {
    useDashboardStore.setState({ status: makeStatus() });
    render(<MindAliveCard onDismiss={noopDismiss} />);

    fireEvent.click(screen.getByTestId("alive-dismiss"));
    expect(noopDismiss).toHaveBeenCalledTimes(1);
  });

  it("dismiss button has accessible label", () => {
    useDashboardStore.setState({ status: makeStatus() });
    render(<MindAliveCard onDismiss={noopDismiss} />);

    const btn = screen.getByTestId("alive-dismiss");
    expect(btn).toHaveAttribute("aria-label");
  });
});

// ════════════════════════════════════════════════════════
// NAVIGATION
// ════════════════════════════════════════════════════════
describe("navigation", () => {
  it("has link to brain page", () => {
    useDashboardStore.setState({ status: makeStatus() });
    render(<MindAliveCard onDismiss={noopDismiss} />);

    const link = screen.getByText("Explore Brain").closest("a");
    expect(link).toHaveAttribute("href", "/brain");
  });

  it("has link to chat page", () => {
    useDashboardStore.setState({ status: makeStatus() });
    render(<MindAliveCard onDismiss={noopDismiss} />);

    const link = screen.getByText("Open Chat").closest("a");
    expect(link).toHaveAttribute("href", "/chat");
  });
});

// ════════════════════════════════════════════════════════
// ANIMATION
// ════════════════════════════════════════════════════════
describe("animation", () => {
  it("applies glow animation when animate=true", () => {
    useDashboardStore.setState({ status: makeStatus() });
    render(<MindAliveCard animate onDismiss={noopDismiss} />);

    const card = screen.getByTestId("mind-alive-card");
    expect(card.className).toContain("glow-once");
  });

  it("does NOT apply glow animation by default", () => {
    useDashboardStore.setState({ status: makeStatus() });
    render(<MindAliveCard onDismiss={noopDismiss} />);

    const card = screen.getByTestId("mind-alive-card");
    expect(card.className).not.toContain("glow-once");
  });
});

// ════════════════════════════════════════════════════════
// EDGE CASES
// ════════════════════════════════════════════════════════
describe("edge cases", () => {
  it("handles zero metrics gracefully", () => {
    useDashboardStore.setState({
      status: makeStatus({
        memory_concepts: 0,
        memory_episodes: 0,
        active_conversations: 0,
        messages_today: 0,
        uptime_seconds: 0,
      }),
    });
    render(<MindAliveCard onDismiss={noopDismiss} />);

    expect(screen.getByTestId("mind-alive-card")).toBeInTheDocument();
    // formatNumber(0) = "0"
    const zeros = screen.getAllByText("0");
    expect(zeros.length).toBeGreaterThanOrEqual(4);
  });

  it("handles large metrics without overflow", () => {
    useDashboardStore.setState({
      status: makeStatus({
        memory_concepts: 999999,
        memory_episodes: 500000,
        active_conversations: 1000,
        messages_today: 999999,
      }),
    });
    render(<MindAliveCard onDismiss={noopDismiss} />);

    // formatNumber(999999) = "999,999"
    const formatted = screen.getAllByText("999,999");
    expect(formatted.length).toBeGreaterThanOrEqual(2);
  });

  it("metric labels are present", () => {
    useDashboardStore.setState({ status: makeStatus() });
    render(<MindAliveCard onDismiss={noopDismiss} />);

    expect(screen.getByText("concepts")).toBeInTheDocument();
    expect(screen.getByText("memories")).toBeInTheDocument();
    expect(screen.getByText("conversations")).toBeInTheDocument();
    expect(screen.getByText("messages")).toBeInTheDocument();
  });
});
