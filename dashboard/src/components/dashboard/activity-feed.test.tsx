/**
 * ActivityFeed tests — TASK-304
 *
 * Covers: empty state, event rendering, event types, LIVE/Disconnected badge,
 * event summaries, timestamps, accessibility.
 */

import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen } from "@/test/test-utils";
import { ActivityFeed } from "./activity-feed";
import type { WsEvent, WsEventType } from "@/types/api";

/* ── Mock store ── */

let mockConnected = true;

vi.mock("@/stores/dashboard", () => ({
  useDashboardStore: (selector: (s: Record<string, unknown>) => unknown) =>
    selector({ connected: mockConnected }),
}));

/* ── Mock ScrollArea ── */

vi.mock("@/components/ui/scroll-area", () => ({
  ScrollArea: ({ children, ...props }: { children: React.ReactNode; className?: string }) => (
    <div data-testid="scroll-area" {...props}>{children}</div>
  ),
}));

/* ── Fixtures ── */

function makeEvent(type: WsEventType, data: Record<string, unknown> = {}): WsEvent {
  return {
    type,
    timestamp: new Date().toISOString(),
    data,
  } as WsEvent;
}

beforeEach(() => {
  mockConnected = true;
});

// ════════════════════════════════════════════════════════
// BASIC RENDERING
// ════════════════════════════════════════════════════════
describe("basic rendering", () => {
  it("renders feed title", () => {
    render(<ActivityFeed events={[]} />);
    expect(screen.getByText("Live Feed")).toBeInTheDocument();
  });

  it("renders LIVE badge when connected", () => {
    mockConnected = true;
    render(<ActivityFeed events={[]} />);
    expect(screen.getByText("LIVE")).toBeInTheDocument();
  });

  it("renders Disconnected badge when not connected", () => {
    mockConnected = false;
    render(<ActivityFeed events={[]} />);
    expect(screen.getByText("Disconnected")).toBeInTheDocument();
  });
});

// ════════════════════════════════════════════════════════
// EMPTY STATE
// ════════════════════════════════════════════════════════
describe("empty state", () => {
  it("shows empty message when no events", () => {
    render(<ActivityFeed events={[]} />);
    expect(screen.getByText("Listening for events…")).toBeInTheDocument();
  });

  it("shows hint text in empty state", () => {
    render(<ActivityFeed events={[]} />);
    expect(screen.getByText(/Events appear here in real time/)).toBeInTheDocument();
  });
});

// ════════════════════════════════════════════════════════
// EVENT TYPES (i18n labels from overview.events.*)
// ════════════════════════════════════════════════════════
describe("event types", () => {
  it("renders PerceptionReceived event", () => {
    render(
      <ActivityFeed events={[makeEvent("PerceptionReceived", { source: "telegram", person_id: "user1" })]} />,
    );
    expect(screen.getByText("Perception")).toBeInTheDocument();
    expect(screen.getByText(/from telegram/)).toBeInTheDocument();
  });

  it("renders ThinkCompleted event with model info", () => {
    render(
      <ActivityFeed
        events={[makeEvent("ThinkCompleted", { model: "claude-3.5", tokens_in: 100, tokens_out: 50, cost_usd: 0.001 })]}
      />,
    );
    expect(screen.getByText("Think")).toBeInTheDocument();
    expect(screen.getByText(/claude-3\.5/)).toBeInTheDocument();
  });

  it("renders ResponseSent event", () => {
    render(
      <ActivityFeed events={[makeEvent("ResponseSent", { channel: "telegram", latency_ms: "42" })]} />,
    );
    expect(screen.getByText("Response")).toBeInTheDocument();
    expect(screen.getByText(/via telegram/)).toBeInTheDocument();
  });

  it("renders ConceptCreated event", () => {
    render(
      <ActivityFeed events={[makeEvent("ConceptCreated", { title: "New Concept" })]} />,
    );
    expect(screen.getByText("Concept")).toBeInTheDocument();
    expect(screen.getByText(/Created.*New Concept/)).toBeInTheDocument();
  });

  it("renders EngineStarted event", () => {
    render(<ActivityFeed events={[makeEvent("EngineStarted")]} />);
    expect(screen.getByText("Engine Started")).toBeInTheDocument();
  });

  it("renders ChannelConnected event", () => {
    render(
      <ActivityFeed events={[makeEvent("ChannelConnected", { channel_type: "telegram" })]} />,
    );
    // Event label is "Channel", summary is "telegram connected"
    expect(screen.getByText(/telegram connected/)).toBeInTheDocument();
  });

  it("renders ChannelDisconnected event", () => {
    render(
      <ActivityFeed events={[makeEvent("ChannelDisconnected", { channel_type: "signal", reason: "timeout" })]} />,
    );
    expect(screen.getByText(/signal disconnected.*timeout/)).toBeInTheDocument();
  });

  it("renders EpisodeEncoded event", () => {
    render(
      <ActivityFeed events={[makeEvent("EpisodeEncoded", { importance: 0.85 })]} />,
    );
    expect(screen.getByText("Episode")).toBeInTheDocument();
    expect(screen.getByText(/importance.*0\.85/)).toBeInTheDocument();
  });
});

// ════════════════════════════════════════════════════════
// MULTIPLE EVENTS
// ════════════════════════════════════════════════════════
describe("multiple events", () => {
  it("renders events in reverse order (newest first)", () => {
    const events = [
      { ...makeEvent("EngineStarted"), timestamp: "2026-04-10T10:00:00Z" },
      { ...makeEvent("ConceptCreated", { title: "A" }), timestamp: "2026-04-10T10:01:00Z" },
      { ...makeEvent("ResponseSent", { channel: "tg", latency_ms: "10" }), timestamp: "2026-04-10T10:02:00Z" },
    ];
    render(<ActivityFeed events={events} />);

    const articles = screen.getAllByRole("article");
    expect(articles).toHaveLength(3);
    // Newest first — "Response" event at 10:02
    expect(articles[0]).toHaveAttribute("aria-label", expect.stringContaining("Response"));
  });

  it("renders correct count of events", () => {
    const events = Array.from({ length: 5 }, (_, i) =>
      makeEvent("ConceptCreated", { title: `Concept ${i}` }),
    );
    render(<ActivityFeed events={events} />);
    expect(screen.getAllByRole("article")).toHaveLength(5);
  });
});

// ════════════════════════════════════════════════════════
// ACCESSIBILITY
// ════════════════════════════════════════════════════════
describe("accessibility", () => {
  it("renders log role when events exist", () => {
    render(<ActivityFeed events={[makeEvent("EngineStarted")]} />);
    expect(screen.getByRole("log")).toBeInTheDocument();
  });

  it("log has aria-label", () => {
    render(<ActivityFeed events={[makeEvent("EngineStarted")]} />);
    expect(screen.getByRole("log")).toHaveAttribute("aria-label", "Live feed");
  });

  it("log has aria-live polite", () => {
    render(<ActivityFeed events={[makeEvent("EngineStarted")]} />);
    expect(screen.getByRole("log")).toHaveAttribute("aria-live", "polite");
  });

  it("each event has article role with aria-label", () => {
    render(<ActivityFeed events={[makeEvent("EngineStarted")]} />);
    const article = screen.getByRole("article");
    expect(article).toHaveAttribute("aria-label", expect.stringContaining("Engine Started"));
  });
});

// ════════════════════════════════════════════════════════
// CONNECTION BADGE
// ════════════════════════════════════════════════════════
describe("connection badge", () => {
  it("shows LIVE when connected", () => {
    mockConnected = true;
    render(<ActivityFeed events={[]} />);
    expect(screen.getByText("LIVE")).toBeInTheDocument();
    expect(screen.queryByText("Disconnected")).not.toBeInTheDocument();
  });

  it("shows Disconnected when not connected", () => {
    mockConnected = false;
    render(<ActivityFeed events={[]} />);
    expect(screen.getByText("Disconnected")).toBeInTheDocument();
    expect(screen.queryByText("LIVE")).not.toBeInTheDocument();
  });
});
