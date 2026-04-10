/**
 * CognitiveTimeline tests — TASK-304
 *
 * Covers: loading state, empty state, entries display, time grouping,
 * entry types, concept chips, role badges, importance dots.
 */

import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen } from "@/test/test-utils";
import { CognitiveTimeline } from "./cognitive-timeline";
import type { TimelineEntry } from "@/types/api";

/* ── Mock store ── */

const mockFetchTimeline = vi.fn(() => Promise.resolve());
let mockStoreState: Record<string, unknown> = {};

vi.mock("@/stores/dashboard", () => ({
  useDashboardStore: (selector: (s: Record<string, unknown>) => unknown) => {
    if (typeof selector === "function") return selector(mockStoreState);
    return mockStoreState;
  },
}));

/* ── Mock ScrollArea to passthrough ── */

vi.mock("@/components/ui/scroll-area", () => ({
  ScrollArea: ({ children, ...props }: { children: React.ReactNode; className?: string }) => (
    <div data-testid="scroll-area" {...props}>{children}</div>
  ),
}));

/* ── Fixtures ── */

function makeEntry(overrides: Partial<TimelineEntry> & { type: TimelineEntry["type"] }): TimelineEntry {
  return {
    timestamp: new Date().toISOString(),
    data: {},
    ...overrides,
  } as TimelineEntry;
}

const NOW = new Date();
const TWO_MIN_AGO = new Date(NOW.getTime() - 2 * 60_000).toISOString();
const FIVE_HOURS_AGO = new Date(NOW.getTime() - 5 * 3_600_000).toISOString();
const YESTERDAY = new Date(NOW.getTime() - 30 * 3_600_000).toISOString();

function setStore(entries: TimelineEntry[], loading = false, connected = true) {
  mockStoreState = {
    timelineEntries: entries,
    isLoadingTimeline: loading,
    fetchTimeline: mockFetchTimeline,
    connected,
  };
}

beforeEach(() => {
  mockFetchTimeline.mockReset();
  setStore([]);
});

// ════════════════════════════════════════════════════════
// BASIC RENDERING
// ════════════════════════════════════════════════════════
describe("basic rendering", () => {
  it("renders timeline title", () => {
    setStore([]);
    render(<CognitiveTimeline />);
    expect(screen.getByText("Cognitive Timeline")).toBeInTheDocument();
  });

  it("calls fetchTimeline on mount", () => {
    setStore([]);
    render(<CognitiveTimeline />);
    expect(mockFetchTimeline).toHaveBeenCalled();
  });
});

// ════════════════════════════════════════════════════════
// LOADING STATE
// ════════════════════════════════════════════════════════
describe("loading state", () => {
  it("shows skeleton when loading", () => {
    setStore([], true);
    const { container } = render(<CognitiveTimeline />);
    const pulsingElements = container.querySelectorAll(".animate-pulse");
    expect(pulsingElements.length).toBeGreaterThan(0);
  });
});

// ════════════════════════════════════════════════════════
// EMPTY STATE
// ════════════════════════════════════════════════════════
describe("empty state", () => {
  it("shows empty message when no entries", () => {
    setStore([]);
    render(<CognitiveTimeline />);
    expect(screen.getByText("No cognitive activity yet")).toBeInTheDocument();
  });

  it("shows hint text in empty state", () => {
    setStore([]);
    render(<CognitiveTimeline />);
    expect(screen.getByText(/Start a conversation to see/)).toBeInTheDocument();
  });
});

// ════════════════════════════════════════════════════════
// ENTRIES DISPLAY
// ════════════════════════════════════════════════════════
describe("entries display", () => {
  it("renders conversation entry", () => {
    setStore([
      makeEntry({
        type: "conversation",
        timestamp: TWO_MIN_AGO,
        data: { channel: "telegram", message_count: 5 },
      }),
    ]);
    render(<CognitiveTimeline />);
    expect(screen.getByText(/telegram/)).toBeInTheDocument();
  });

  it("renders message entry with preview", () => {
    setStore([
      makeEntry({
        type: "message",
        timestamp: TWO_MIN_AGO,
        data: { preview: "Hello world", role: "user" },
      }),
    ]);
    render(<CognitiveTimeline />);
    expect(screen.getByText("Hello world")).toBeInTheDocument();
  });

  it("renders concepts_learned with concept names", () => {
    setStore([
      makeEntry({
        type: "concepts_learned",
        timestamp: TWO_MIN_AGO,
        data: { names: ["React", "TypeScript", "Vitest"], count: 3 },
      }),
    ]);
    render(<CognitiveTimeline />);
    expect(screen.getByText("React")).toBeInTheDocument();
    expect(screen.getByText("TypeScript")).toBeInTheDocument();
    expect(screen.getByText("Vitest")).toBeInTheDocument();
  });

  it("renders episode_encoded entry", () => {
    setStore([
      makeEntry({
        type: "episode_encoded",
        timestamp: TWO_MIN_AGO,
        data: { importance: 0.85 },
      }),
    ]);
    render(<CognitiveTimeline />);
    // importance is formatted to 1 decimal: 0.85 → "0.8"
    expect(screen.getByText(/importance.*0\.8/)).toBeInTheDocument();
  });

  it("renders consolidation entry", () => {
    setStore([
      makeEntry({
        type: "consolidation",
        timestamp: TWO_MIN_AGO,
        data: { merged: 3, pruned: 1, strengthened: 5 },
      }),
    ]);
    render(<CognitiveTimeline />);
    // The summary contains the consolidation stats
    expect(screen.getByText(/3.*1.*5/)).toBeInTheDocument();
  });
});

// ════════════════════════════════════════════════════════
// TIME GROUPING
// ════════════════════════════════════════════════════════
describe("time grouping", () => {
  it("shows Just now group for recent entries", () => {
    setStore([
      makeEntry({
        type: "message",
        timestamp: TWO_MIN_AGO,
        data: { preview: "Recent message", role: "user" },
      }),
    ]);
    render(<CognitiveTimeline />);
    expect(screen.getByText("Just now")).toBeInTheDocument();
  });

  it("shows Earlier today for older entries", () => {
    setStore([
      makeEntry({
        type: "message",
        timestamp: FIVE_HOURS_AGO,
        data: { preview: "Old message", role: "user" },
      }),
    ]);
    render(<CognitiveTimeline />);
    expect(screen.getByText("Earlier today")).toBeInTheDocument();
  });

  it("shows Yesterday for day-old entries", () => {
    setStore([
      makeEntry({
        type: "message",
        timestamp: YESTERDAY,
        data: { preview: "Yesterday msg", role: "user" },
      }),
    ]);
    render(<CognitiveTimeline />);
    expect(screen.getByText("Yesterday")).toBeInTheDocument();
  });
});

// ════════════════════════════════════════════════════════
// ROLE BADGES
// ════════════════════════════════════════════════════════
describe("role badges", () => {
  it("shows YOU badge for user messages", () => {
    setStore([
      makeEntry({
        type: "message",
        timestamp: TWO_MIN_AGO,
        data: { preview: "User said hi", role: "user" },
      }),
    ]);
    render(<CognitiveTimeline />);
    expect(screen.getByText("YOU")).toBeInTheDocument();
  });

  it("shows AI badge for assistant messages", () => {
    setStore([
      makeEntry({
        type: "message",
        timestamp: TWO_MIN_AGO,
        data: { preview: "AI response", role: "assistant" },
      }),
    ]);
    render(<CognitiveTimeline />);
    expect(screen.getByText("AI")).toBeInTheDocument();
  });
});

// ════════════════════════════════════════════════════════
// ARIA / ACCESSIBILITY
// ════════════════════════════════════════════════════════
describe("accessibility", () => {
  it("renders feed role when entries exist", () => {
    setStore([
      makeEntry({
        type: "message",
        timestamp: TWO_MIN_AGO,
        data: { preview: "Test", role: "user" },
      }),
    ]);
    render(<CognitiveTimeline />);
    expect(screen.getByRole("feed")).toBeInTheDocument();
  });

  it("feed has aria-label", () => {
    setStore([
      makeEntry({
        type: "message",
        timestamp: TWO_MIN_AGO,
        data: { preview: "Test" },
      }),
    ]);
    render(<CognitiveTimeline />);
    expect(screen.getByRole("feed")).toHaveAttribute("aria-label", "Cognitive timeline");
  });
});

// ════════════════════════════════════════════════════════
// MODEL & COST DISPLAY
// ════════════════════════════════════════════════════════
describe("model and cost display", () => {
  it("shows model name for messages with model data", () => {
    setStore([
      makeEntry({
        type: "message",
        timestamp: TWO_MIN_AGO,
        data: { preview: "Response", role: "assistant", model: "claude-3.5-sonnet", cost_usd: 0.0015 },
      }),
    ]);
    render(<CognitiveTimeline />);
    // Model and cost are in the same span: "claude-3.5-sonnet · $0.0015"
    expect(screen.getByText(/claude-3\.5-sonnet.*\$0\.0015/)).toBeInTheDocument();
  });
});
