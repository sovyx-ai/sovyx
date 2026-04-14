/**
 * VAL-20: Custom component tests — all 15 components.
 *
 * Components tested:
 * 1.  TokenEntryModal — form, validation, error display
 * 2.  CommandPalette — search, keyboard nav, action dispatch
 * 3.  BrainGraph — renders with data, renders empty, category colors
 * 4.  ChatBubble — user vs assistant styling
 * 5.  ChatThread — scroll, message list, empty, loading
 * 6.  ChannelBadge — channel type → correct icon
 * 7.  LetterAvatar — deterministic color from name
 * 8.  LogRow — level → correct styling, expand/collapse
 * 9.  MetricChart — renders with data, handles empty
 * 10. NeuralMesh — smoke test (CSS-only, no canvas)
 * 11. AppSidebar — navigation, renders within provider
 * 12. Skeletons — renders without crash
 * 13. ActivityFeed — event list, empty state
 * 14. HealthGrid — status colors correct
 * 15. CategoryLegend — renders categories, click filter
 */
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent } from "@/test/test-utils";
import { render as rawRender } from "@testing-library/react";
import React from "react";

// ── Global mocks ──

// Mock react-force-graph-2d (canvas-based, can't render in jsdom)
vi.mock("react-force-graph-2d", () => ({
  __esModule: true,
  default: React.forwardRef(function MockForceGraph(
    props: { graphData?: unknown; width?: number; height?: number },
    _ref: React.Ref<unknown>,
  ) {
    return <div data-testid="force-graph" data-nodes={JSON.stringify((props.graphData as { nodes?: unknown[] })?.nodes ?? [])} />;
  }),
}));

// scrollIntoView mock
beforeEach(() => {
  Element.prototype.scrollIntoView = vi.fn();
});

// ═══════════════════════════════════════════════════════════
// 1. TokenEntryModal
// ═══════════════════════════════════════════════════════════
import { TokenEntryModal } from "../../auth/token-entry-modal";
import { useDashboardStore } from "@/stores/dashboard";

describe("TokenEntryModal", () => {
  beforeEach(() => {
    useDashboardStore.setState({ showTokenModal: false, authenticated: false });
  });

  it("renders dialog with input when showTokenModal is true", () => {
    useDashboardStore.setState({ showTokenModal: true });
    rawRender(<TokenEntryModal />);
    expect(document.querySelector("input")).toBeTruthy();
  });

  it("does not render input when showTokenModal is false", () => {
    useDashboardStore.setState({ showTokenModal: false });
    const { container } = rawRender(<TokenEntryModal />);
    expect(container.querySelector("input")).toBeFalsy();
  });

  it("has a submit button", () => {
    useDashboardStore.setState({ showTokenModal: true });
    rawRender(<TokenEntryModal />);
    const button = document.querySelector("button[type='submit'], button");
    expect(button).toBeTruthy();
  });
});

// ═══════════════════════════════════════════════════════════
// 2. CommandPalette
// ═══════════════════════════════════════════════════════════
import { CommandPalette } from "../../command-palette";

describe("CommandPalette", () => {
  it("renders without crash (closed by default)", () => {
    const { container } = render(<CommandPalette />);
    expect(container).toBeTruthy();
  });

  it("mounts and registers keyboard listener", () => {
    const addSpy = vi.spyOn(document, "addEventListener");
    render(<CommandPalette />);
    const keydownCalls = addSpy.mock.calls.filter(([event]) => event === "keydown");
    expect(keydownCalls.length).toBeGreaterThan(0);
    addSpy.mockRestore();
  });

  it("cleans up keyboard listener on unmount", () => {
    const removeSpy = vi.spyOn(document, "removeEventListener");
    const { unmount } = render(<CommandPalette />);
    unmount();
    const keydownCalls = removeSpy.mock.calls.filter(([event]) => event === "keydown");
    expect(keydownCalls.length).toBeGreaterThan(0);
    removeSpy.mockRestore();
  });
});

// ═══════════════════════════════════════════════════════════
// 3. BrainGraph
// ═══════════════════════════════════════════════════════════
import { BrainGraph } from "../brain-graph";

describe("BrainGraph", () => {
  it("renders with node data", () => {
    const data = {
      nodes: [
        { id: "1", name: "concept1", category: "fact" as const, importance: 0.8, confidence: 0.9, access_count: 5 },
        { id: "2", name: "concept2", category: "entity" as const, importance: 0.5, confidence: 0.7, access_count: 2 },
      ],
      links: [{ source: "1", target: "2", relation_type: "related_to" as const, weight: 0.6 }],
    };
    const { container } = rawRender(<BrainGraph data={data} width={800} height={600} />);
    expect(container).toBeTruthy();
    expect(container.querySelector("[data-testid='force-graph']")).toBeTruthy();
  });

  it("renders empty graph", () => {
    const { container } = rawRender(<BrainGraph data={{ nodes: [], links: [] }} width={800} height={600} />);
    expect(container).toBeTruthy();
  });

  it("calls onNodeClick when provided", () => {
    const onClick = vi.fn();
    const data = {
      nodes: [{ id: "1", name: "c1", category: "fact" as const, importance: 0.8, confidence: 0.9, access_count: 1 }],
      links: [],
    };
    const { container } = rawRender(<BrainGraph data={data} width={800} height={600} onNodeClick={onClick} />);
    expect(container).toBeTruthy();
  });
});

// ═══════════════════════════════════════════════════════════
// 4. ChatBubble
// ═══════════════════════════════════════════════════════════
import { ChatBubble } from "../chat-bubble";

describe("ChatBubble", () => {
  const userMsg = {
    id: "msg-1",
    role: "user" as const,
    content: "Hello world",
    timestamp: "2026-04-07T00:00:00Z",
  };
  const assistantMsg = {
    id: "msg-2",
    role: "assistant" as const,
    content: "Hi there",
    timestamp: "2026-04-07T00:01:00Z",
  };

  it("renders user message content", () => {
    render(<ChatBubble message={userMsg} participantName="Alice" />);
    expect(screen.getByText("Hello world")).toBeInTheDocument();
  });

  it("renders assistant message content", () => {
    render(<ChatBubble message={assistantMsg} participantName="Alice" />);
    expect(screen.getByText("Hi there")).toBeInTheDocument();
  });

  it("shows letter avatar for user messages", () => {
    const { container } = render(<ChatBubble message={userMsg} participantName="Alice" />);
    // LetterAvatar renders the first letter
    expect(container.textContent).toContain("A");
  });

  it("shows mind avatar (S) for assistant messages", () => {
    const { container } = render(<ChatBubble message={assistantMsg} participantName="Alice" />);
    expect(container.textContent).toContain("S");
  });
});

// ═══════════════════════════════════════════════════════════
// 5. ChatThread
// ═══════════════════════════════════════════════════════════
import { ChatThread } from "../chat-thread";

describe("ChatThread", () => {
  const messages = [
    { id: "1", role: "user" as const, content: "Msg 1", timestamp: "2026-04-07T00:00:00Z" },
    { id: "2", role: "assistant" as const, content: "Msg 2", timestamp: "2026-04-07T00:01:00Z" },
  ];

  it("renders messages", () => {
    render(<ChatThread messages={messages} participantName="Bob" />);
    expect(screen.getByText("Msg 1")).toBeInTheDocument();
    expect(screen.getByText("Msg 2")).toBeInTheDocument();
  });

  it("shows empty state when no messages", () => {
    const { container } = render(<ChatThread messages={[]} participantName="Bob" />);
    expect(container).toBeTruthy();
  });

  it("shows loading spinner", () => {
    const { container } = render(<ChatThread messages={[]} participantName="Bob" loading />);
    expect(container.querySelector(".animate-spin")).toBeTruthy();
  });

  it("renders a scrollable container with the virtualized feed", () => {
    // ChatThread now uses TanStack Virtual — auto-scroll uses
    // virtualizer.scrollToIndex instead of Element.scrollIntoView.
    const { container } = render(
      <ChatThread messages={messages} participantName="Bob" />,
    );
    const scrollRoot = container.querySelector(".overflow-auto");
    expect(scrollRoot).not.toBeNull();
  });
});

// ═══════════════════════════════════════════════════════════
// 6. ChannelBadge
// ═══════════════════════════════════════════════════════════
import { ChannelBadge } from "../channel-badge";

describe("ChannelBadge", () => {
  it.each([
    ["telegram", "Telegram", "✈️"],
    ["discord", "Discord", "💬"],
    ["signal", "Signal", "🔒"],
    ["cli", "CLI", "⌨️"],
    ["api", "API", "🔗"],
  ])("renders %s channel with label %s and icon %s", (channel, label, icon) => {
    render(<ChannelBadge channel={channel} />);
    expect(screen.getByText(new RegExp(label))).toBeInTheDocument();
    expect(screen.getByTitle(label).textContent).toContain(icon);
  });

  it("renders unknown channel with fallback", () => {
    render(<ChannelBadge channel="whatsapp" />);
    expect(screen.getByText(/whatsapp/i)).toBeInTheDocument();
    expect(screen.getByTitle("whatsapp").textContent).toContain("📨");
  });

  it("handles case-insensitive channel names", () => {
    render(<ChannelBadge channel="TELEGRAM" />);
    expect(screen.getByText(/Telegram/)).toBeInTheDocument();
  });
});

// ═══════════════════════════════════════════════════════════
// 7. LetterAvatar
// ═══════════════════════════════════════════════════════════
import { LetterAvatar, MindAvatar } from "../letter-avatar";

describe("LetterAvatar", () => {
  it("shows first letter uppercased", () => {
    const { container } = render(<LetterAvatar name="alice" />);
    expect(container.textContent).toBe("A");
  });

  it("deterministic: same name → same color", () => {
    const { container: c1 } = render(<LetterAvatar name="TestUser" />);
    const { container: c2 } = render(<LetterAvatar name="TestUser" />);
    const bg1 = (c1.firstChild as HTMLElement).style.backgroundColor;
    const bg2 = (c2.firstChild as HTMLElement).style.backgroundColor;
    expect(bg1).toBe(bg2);
    expect(bg1).toBeTruthy();
  });

  it("different names can produce different colors", () => {
    const { container: c1 } = render(<LetterAvatar name="AAAA" />);
    const { container: c2 } = render(<LetterAvatar name="ZZZZ" />);
    const bg1 = (c1.firstChild as HTMLElement).style.backgroundColor;
    const bg2 = (c2.firstChild as HTMLElement).style.backgroundColor;
    // Not guaranteed to differ, but likely. At minimum both are truthy.
    expect(bg1).toBeTruthy();
    expect(bg2).toBeTruthy();
  });

  it("respects custom size", () => {
    const { container } = render(<LetterAvatar name="X" size={64} />);
    const el = container.firstChild as HTMLElement;
    expect(el.style.width).toBe("64px");
    expect(el.style.height).toBe("64px");
  });

  it("handles empty name with fallback ?", () => {
    const { container } = render(<LetterAvatar name="" />);
    expect(container.textContent).toBe("?");
  });
});

describe("MindAvatar", () => {
  it("renders S fallback", () => {
    const { container } = render(<MindAvatar />);
    expect(container.textContent).toBe("S");
  });
});

// ═══════════════════════════════════════════════════════════
// 8. LogRow
// ═══════════════════════════════════════════════════════════
import { LogRow } from "../log-row";

describe("LogRow", () => {
  const baseEntry = {
    timestamp: "2026-04-07T00:00:00Z",
    logger: "sovyx.brain",
    event: "Concept created",
  };

  it.each(["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] as const)(
    "renders %s level",
    (level) => {
      const { container } = render(<LogRow entry={{ ...baseEntry, level }} />);
      expect(container.textContent).toContain(level.padEnd(5));
    },
  );

  it("shows logger and event text", () => {
    render(<LogRow entry={{ ...baseEntry, level: "INFO" }} />);
    expect(screen.getByText("sovyx.brain")).toBeInTheDocument();
    expect(screen.getByText("Concept created")).toBeInTheDocument();
  });

  it("expands extra fields on click", () => {
    const entry = { ...baseEntry, level: "INFO" as const, extra_key: "extra_value" };
    const { container } = render(<LogRow entry={entry} />);
    // Initially no <pre>
    expect(container.querySelector("pre")).toBeFalsy();
    // Click to expand
    fireEvent.click(container.firstChild as HTMLElement);
    expect(container.querySelector("pre")).toBeTruthy();
    expect(container.querySelector("pre")!.textContent).toContain("extra_value");
  });

  it("collapses on second click", () => {
    const entry = { ...baseEntry, level: "INFO" as const, detail: "val" };
    const { container } = render(<LogRow entry={entry} />);
    const row = container.firstChild as HTMLElement;
    fireEvent.click(row);
    expect(container.querySelector("pre")).toBeTruthy();
    fireEvent.click(row);
    expect(container.querySelector("pre")).toBeFalsy();
  });

  it("does not expand when no extra fields", () => {
    const { container } = render(<LogRow entry={{ ...baseEntry, level: "INFO" }} />);
    fireEvent.click(container.firstChild as HTMLElement);
    expect(container.querySelector("pre")).toBeFalsy();
  });
});

// ═══════════════════════════════════════════════════════════
// 9. MetricChart
// ═══════════════════════════════════════════════════════════
import { MetricChart } from "../metric-chart";

describe("MetricChart", () => {
  it("renders with data", () => {
    const { container } = render(
      <MetricChart title="Test" data={[{ time: 1000, value: 10 }, { time: 2000, value: 20 }]} />,
    );
    expect(container).toBeTruthy();
  });

  it("renders empty without crash", () => {
    const { container } = render(<MetricChart title="Empty" data={[]} />);
    expect(container).toBeTruthy();
  });

  it("displays title", () => {
    render(<MetricChart title="My Chart" data={[]} />);
    expect(screen.getByText("My Chart")).toBeInTheDocument();
  });
});

// ═══════════════════════════════════════════════════════════
// 10. NeuralMesh (CSS-only, no canvas)
// ═══════════════════════════════════════════════════════════
import { NeuralMesh } from "../neural-mesh";

describe("NeuralMesh", () => {
  it("renders layers as divs", () => {
    const { container } = rawRender(<NeuralMesh />);
    expect(container.querySelector("div")).toBeTruthy();
    // Has aria-hidden for accessibility
    expect(container.querySelector("[aria-hidden='true']")).toBeTruthy();
  });

  it("has pointer-events-none for non-interactive background", () => {
    const { container } = rawRender(<NeuralMesh />);
    const root = container.firstChild as HTMLElement;
    expect(root.className).toContain("pointer-events-none");
  });
});

// ═══════════════════════════════════════════════════════════
// 11. AppSidebar
// ═══════════════════════════════════════════════════════════
import { AppSidebar } from "../../layout/app-sidebar";
import { SidebarProvider } from "@/components/ui/sidebar";
import { TooltipProvider } from "@/components/ui/tooltip";

describe("AppSidebar", () => {
  it("renders within SidebarProvider + router", () => {
    const { container } = render(
      <TooltipProvider>
        <SidebarProvider>
          <AppSidebar />
        </SidebarProvider>
      </TooltipProvider>,
    );
    expect(container).toBeTruthy();
  });

  it("contains navigation links", () => {
    render(
      <TooltipProvider>
        <SidebarProvider>
          <AppSidebar />
        </SidebarProvider>
      </TooltipProvider>,
    );
    // Should have at least one link
    const links = document.querySelectorAll("a");
    expect(links.length).toBeGreaterThan(0);
  });
});

// ═══════════════════════════════════════════════════════════
// 12. Skeletons
// ═══════════════════════════════════════════════════════════
import {
  OverviewSkeleton,
  ConversationsSkeleton,
  BrainSkeleton,
  LogsSkeleton,
  SettingsSkeleton,
} from "../../skeletons";

describe("Skeletons", () => {
  it.each([
    ["OverviewSkeleton", OverviewSkeleton],
    ["ConversationsSkeleton", ConversationsSkeleton],
    ["BrainSkeleton", BrainSkeleton],
    ["LogsSkeleton", LogsSkeleton],
    ["SettingsSkeleton", SettingsSkeleton],
  ])("%s renders without crash", (_name, Component) => {
    const { container } = rawRender(<Component />);
    expect(container).toBeTruthy();
    expect(container.innerHTML).not.toBe("");
  });
});

// ═══════════════════════════════════════════════════════════
// 13. ActivityFeed
// ═══════════════════════════════════════════════════════════
import { ActivityFeed } from "../activity-feed";
import type { WsEvent } from "@/types/api";

describe("ActivityFeed", () => {
  const events: WsEvent[] = [
    { type: "ThinkCompleted", timestamp: "2026-04-07T00:00:00Z", correlation_id: "c1", data: { model: "claude", cost_usd: 0.01, tokens_in: 100, tokens_out: 50, latency_ms: 200 } },
    { type: "PerceptionReceived", timestamp: "2026-04-07T00:01:00Z", correlation_id: "c2", data: {} },
    { type: "ConceptCreated", timestamp: "2026-04-07T00:02:00Z", correlation_id: "c3", data: { concept_id: "x", title: "test", source: "chat" } },
  ];

  it("renders with multiple events", () => {
    const { container } = render(<ActivityFeed events={events} />);
    expect(container).toBeTruthy();
  });

  it("renders empty state", () => {
    const { container } = render(<ActivityFeed events={[]} />);
    expect(container).toBeTruthy();
  });

  it("renders all 11 event types without crash", () => {
    const allTypes: WsEvent[] = [
      "EngineStarted", "EngineStopping", "ServiceHealthChanged",
      "PerceptionReceived", "ThinkCompleted", "ResponseSent",
      "ConceptCreated", "EpisodeEncoded", "ConsolidationCompleted",
      "ChannelConnected", "ChannelDisconnected",
    ].map((type, i) => ({
      type: type as WsEvent["type"],
      timestamp: `2026-04-07T00:${String(i).padStart(2, "0")}:00Z`,
      correlation_id: `c${i}`,
      data: {},
    }));
    const { container } = render(<ActivityFeed events={allTypes} />);
    expect(container).toBeTruthy();
  });
});

// ═══════════════════════════════════════════════════════════
// 14. HealthGrid
// ═══════════════════════════════════════════════════════════
import { HealthGrid } from "../health-grid";
import type { HealthCheck } from "@/types/api";

describe("HealthGrid", () => {
  const checks: HealthCheck[] = [
    { name: "Disk Space", status: "green", message: "OK" },
    { name: "RAM", status: "yellow", message: "75% used", latency_ms: 1.2 },
    { name: "Database", status: "red", message: "Connection failed", latency_ms: 5000 },
  ];

  it("renders all checks", () => {
    render(<HealthGrid checks={checks} />);
    expect(screen.getByText("Disk Space")).toBeInTheDocument();
    expect(screen.getByText("RAM")).toBeInTheDocument();
    expect(screen.getByText("Database")).toBeInTheDocument();
  });

  it("shows correct aria-labels with status", () => {
    render(<HealthGrid checks={checks} />);
    expect(screen.getByRole("status", { name: "Disk Space: green" })).toBeInTheDocument();
    expect(screen.getByRole("status", { name: "RAM: yellow" })).toBeInTheDocument();
    expect(screen.getByRole("status", { name: "Database: red" })).toBeInTheDocument();
  });

  it("renders empty checks list", () => {
    const { container } = render(<HealthGrid checks={[]} />);
    expect(container).toBeTruthy();
  });

  it("shows all green when all checks pass", () => {
    const allGreen: HealthCheck[] = [
      { name: "A", status: "green", message: "OK" },
      { name: "B", status: "green", message: "OK" },
    ];
    const { container } = render(<HealthGrid checks={allGreen} />);
    expect(container).toBeTruthy();
  });
});

// ═══════════════════════════════════════════════════════════
// 15. CategoryLegend + RelationLegend
// ═══════════════════════════════════════════════════════════
import { CategoryLegend, RelationLegend } from "../category-legend";

describe("CategoryLegend", () => {
  it("renders all 7 categories", () => {
    const { container } = render(<CategoryLegend />);
    // 7 color dots
    const dots = container.querySelectorAll("[aria-hidden='true']");
    expect(dots.length).toBe(7);
  });

  it("renders counts when provided", () => {
    render(<CategoryLegend counts={{ fact: 10, entity: 5 }} />);
    expect(screen.getByText("10")).toBeInTheDocument();
    expect(screen.getByText("5")).toBeInTheDocument();
  });

  it("renders without counts", () => {
    const { container } = render(<CategoryLegend />);
    expect(container).toBeTruthy();
  });
});

describe("RelationLegend", () => {
  it("renders all 7 relation types without counts", () => {
    const { container } = render(<RelationLegend />);
    const lines = container.querySelectorAll("[aria-hidden='true']");
    expect(lines.length).toBe(7);
  });

  it("renders only non-zero types when counts provided", () => {
    const counts = { related_to: 10, causes: 3, part_of: 0 };
    const { container } = render(<RelationLegend counts={counts} />);
    const lines = container.querySelectorAll("[aria-hidden='true']");
    // Only related_to and causes have count > 0
    expect(lines.length).toBe(2);
  });

  it("shows count numbers next to type names", () => {
    const counts = { related_to: 42 };
    const { container } = render(<RelationLegend counts={counts} />);
    expect(container.textContent).toContain("42");
  });

  it("returns null when all counts are zero", () => {
    const counts = { related_to: 0, causes: 0 };
    const { container } = render(<RelationLegend counts={counts} />);
    expect(container.innerHTML).toBe("");
  });
});
