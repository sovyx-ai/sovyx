/**
 * SagaTimeline component tests — Phase 12.11.
 *
 * Pins the swim-lane derivation, span ordering, click delegation,
 * level-driven styling, and the empty-state sentinel.
 */

import { describe, it, expect, vi } from "vitest";
import { render, screen, fireEvent } from "@/test/test-utils";
import type { LogEntry } from "@/types/api";

import { SagaTimeline } from "./saga-timeline";

function mk(overrides: Partial<LogEntry> = {}): LogEntry {
  return {
    timestamp: "2026-04-20T12:00:00.000Z",
    level: "INFO",
    logger: "sovyx.voice.pipeline",
    event: "voice.frame",
    ...overrides,
  };
}

describe("SagaTimeline", () => {
  it("renders the empty-state copy when no entries are supplied", () => {
    render(<SagaTimeline entries={[]} />);
    expect(
      screen.getByText("No entries indexed for this saga yet."),
    ).toBeInTheDocument();
  });

  it("does NOT render an SVG when there are no entries", () => {
    const { container } = render(<SagaTimeline entries={[]} />);
    expect(container.querySelector("svg")).toBeNull();
  });

  it("renders an SVG with the i18n aria-label when entries exist", () => {
    render(<SagaTimeline entries={[mk()]} />);
    const svg = screen.getByRole("img", { name: "Saga" });
    expect(svg.tagName.toLowerCase()).toBe("svg");
  });

  it("derives the lane from the second segment of a sovyx logger", () => {
    const entries = [
      mk({ logger: "sovyx.voice.pipeline._orchestrator" }),
      mk({ logger: "sovyx.brain.service", timestamp: "2026-04-20T12:00:00.100Z" }),
    ];
    render(<SagaTimeline entries={entries} />);
    expect(screen.getByText("voice")).toBeInTheDocument();
    expect(screen.getByText("brain")).toBeInTheDocument();
  });

  it("falls back to the first segment when logger is not sovyx-prefixed", () => {
    const entries = [mk({ logger: "thirdparty.module" })];
    render(<SagaTimeline entries={entries} />);
    expect(screen.getByText("thirdparty")).toBeInTheDocument();
  });

  it("falls back to 'default' for empty / malformed loggers", () => {
    const entries = [mk({ logger: "" })];
    render(<SagaTimeline entries={entries} />);
    expect(screen.getByText("default")).toBeInTheDocument();
  });

  it("draws one rect per entry", () => {
    const entries = [
      mk({ timestamp: "2026-04-20T12:00:00.000Z" }),
      mk({ timestamp: "2026-04-20T12:00:00.500Z" }),
      mk({ timestamp: "2026-04-20T12:00:01.000Z" }),
    ];
    const { container } = render(<SagaTimeline entries={entries} />);
    expect(container.querySelectorAll("rect")).toHaveLength(3);
  });

  it("invokes onSpanSelect when a span is clicked", () => {
    const handler = vi.fn();
    const entries = [
      mk({ event: "first", timestamp: "2026-04-20T12:00:00.000Z" }),
      mk({ event: "second", timestamp: "2026-04-20T12:00:00.250Z" }),
    ];
    const { container } = render(
      <SagaTimeline entries={entries} onSpanSelect={handler} />,
    );
    // Group elements with cursor-pointer wrap each rect — click the first.
    const groups = container.querySelectorAll("g.cursor-pointer");
    expect(groups.length).toBeGreaterThan(0);
    fireEvent.click(groups[0]!);
    expect(handler).toHaveBeenCalledTimes(1);
    expect(handler.mock.calls[0]?.[0]).toMatchObject({ event: "first" });
  });

  it("omits the cursor-pointer affordance when no onSpanSelect is given", () => {
    const { container } = render(<SagaTimeline entries={[mk()]} />);
    expect(container.querySelectorAll("g.cursor-pointer")).toHaveLength(0);
  });

  it("paints ERROR spans with the error border colour", () => {
    const entries = [mk({ level: "ERROR" })];
    const { container } = render(<SagaTimeline entries={entries} />);
    const rect = container.querySelector("rect")!;
    expect(rect.getAttribute("stroke")).toBe("var(--svx-color-error)");
  });

  it("paints WARNING spans with the warning border colour", () => {
    const entries = [mk({ level: "WARNING" })];
    const { container } = render(<SagaTimeline entries={entries} />);
    const rect = container.querySelector("rect")!;
    expect(rect.getAttribute("stroke")).toBe("var(--svx-color-warning)");
  });

  it("highlights a span when highlightedKey matches its sequence_no", () => {
    const entries = [
      mk({ sequence_no: 7, timestamp: "2026-04-20T12:00:00.000Z" }),
      mk({ sequence_no: 8, timestamp: "2026-04-20T12:00:00.500Z" }),
    ];
    const { container } = render(
      <SagaTimeline entries={entries} highlightedKey="seq-7" />,
    );
    const rects = container.querySelectorAll("rect");
    const highlighted = Array.from(rects).filter(
      (r) => r.getAttribute("stroke") === "var(--svx-color-brand-primary)",
    );
    expect(highlighted).toHaveLength(1);
  });
});
