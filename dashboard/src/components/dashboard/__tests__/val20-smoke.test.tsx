/**
 * VAL-20: Smoke + behavior tests for untested dashboard components.
 */
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";

beforeEach(() => {
  Element.prototype.scrollIntoView = vi.fn();
});

import { ChannelBadge } from "../channel-badge";
describe("ChannelBadge", () => {
  it("renders known channel", () => {
    const { container } = render(<ChannelBadge channel="telegram" />);
    expect(container.textContent).toContain("Telegram");
  });
  it("renders unknown channel with fallback", () => {
    const { container } = render(<ChannelBadge channel="sms" />);
    expect(container.textContent).toContain("sms");
  });
  it("is case-insensitive", () => {
    const { container } = render(<ChannelBadge channel="DISCORD" />);
    expect(container.textContent).toContain("Discord");
  });
});

import { LetterAvatar, MindAvatar } from "../letter-avatar";
describe("LetterAvatar", () => {
  it("renders first letter uppercase", () => {
    const { container } = render(<LetterAvatar name="alice" />);
    expect(container.textContent).toBe("A");
  });
  it("same name = same color (deterministic)", () => {
    const { container: c1 } = render(<LetterAvatar name="bob" />);
    const { container: c2 } = render(<LetterAvatar name="bob" />);
    expect((c1.firstChild as HTMLElement).style.backgroundColor)
      .toBe((c2.firstChild as HTMLElement).style.backgroundColor);
  });
  it("handles empty name", () => {
    const { container } = render(<LetterAvatar name="" />);
    expect(container.textContent).toBe("?");
  });
});

describe("MindAvatar", () => {
  it("renders S", () => {
    const { container } = render(<MindAvatar />);
    expect(container.textContent).toBe("S");
  });
});

import { ChatBubble } from "../chat-bubble";
describe("ChatBubble", () => {
  it("renders user message", () => {
    render(<ChatBubble message={{ id: "msg-user", role: "user", content: "Hello", timestamp: "2026-04-07T00:00:00Z" }} participantName="Alice" />);
    expect(screen.getByText("Hello")).toBeTruthy();
  });
  it("renders assistant message", () => {
    render(<ChatBubble message={{ id: "msg-assistant", role: "assistant", content: "Hi!", timestamp: "2026-04-07T00:00:01Z" }} participantName="Alice" />);
    expect(screen.getByText("Hi!")).toBeTruthy();
  });
});

import { ChatThread } from "../chat-thread";
describe("ChatThread", () => {
  it("renders messages", () => {
    const msgs = [
      { id: "m1", role: "user" as const, content: "msg1", timestamp: "2026-04-07T00:00:00Z" },
      { id: "m2", role: "assistant" as const, content: "msg2", timestamp: "2026-04-07T00:00:01Z" },
    ];
    render(<ChatThread messages={msgs} participantName="Bob" />);
    expect(screen.getByText("msg1")).toBeTruthy();
  });
  it("renders empty", () => {
    const { container } = render(<ChatThread messages={[]} participantName="Bob" />);
    expect(container).toBeTruthy();
  });
});

import { LogRow } from "../log-row";
describe("LogRow", () => {
  const base = { timestamp: "2026-04-07T00:00:00Z", level: "INFO" as const, logger: "sovyx.engine", event: "started" };
  it("renders", () => {
    render(<LogRow entry={base} />);
    expect(screen.getByText("started")).toBeTruthy();
  });
  it("expands extra fields", () => {
    render(<LogRow entry={{ ...base, extra_key: "extra_value" }} />);
    fireEvent.click(screen.getByText("started"));
    expect(screen.getByText(/"extra_key"/)).toBeTruthy();
  });
  it("all levels render", () => {
    for (const level of ["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] as const) {
      const { unmount } = render(<LogRow entry={{ ...base, level }} />);
      unmount();
    }
  });
});

import { CategoryLegend } from "../category-legend";
describe("CategoryLegend", () => {
  it("renders", () => {
    const { container } = render(<CategoryLegend />);
    expect(container).toBeTruthy();
  });
});

import { HealthGrid } from "../health-grid";
describe("HealthGrid", () => {
  it("renders checks", () => {
    const checks = [
      { name: "disk", status: "green" as const, message: "OK", latency_ms: 1 },
      { name: "ram", status: "yellow" as const, message: "Low", latency_ms: 2 },
    ];
    render(<HealthGrid checks={checks} />);
    expect(screen.getByText("disk")).toBeTruthy();
  });
  it("renders empty", () => {
    const { container } = render(<HealthGrid checks={[]} />);
    expect(container).toBeTruthy();
  });
});

import { OverviewSkeleton, BrainSkeleton, LogsSkeleton, ConversationsSkeleton } from "../../skeletons";
describe("Skeletons", () => {
  it("OverviewSkeleton", () => { expect(render(<OverviewSkeleton />).container.children.length).toBeGreaterThan(0); });
  it("BrainSkeleton", () => { expect(render(<BrainSkeleton />).container.children.length).toBeGreaterThan(0); });
  it("LogsSkeleton", () => { expect(render(<LogsSkeleton />).container.children.length).toBeGreaterThan(0); });
  it("ConversationsSkeleton", () => { expect(render(<ConversationsSkeleton />).container.children.length).toBeGreaterThan(0); });
});
