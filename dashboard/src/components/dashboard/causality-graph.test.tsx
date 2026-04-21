/**
 * CausalityGraph component tests — Phase 12.11.
 *
 * Pins layout, ancestry highlighting, click delegation, and the
 * empty-state sentinel for the saga DAG view.
 */

import { describe, it, expect, vi } from "vitest";
import { render, screen, fireEvent } from "@/test/test-utils";
import type { CausalityEdge } from "@/types/api";

import { CausalityGraph } from "./causality-graph";

function mkEdge(overrides: Partial<CausalityEdge> = {}): CausalityEdge {
  return {
    id: "n1",
    cause_id: null,
    event: "engine.boot",
    logger: "sovyx.engine",
    timestamp: "2026-04-20T12:00:00Z",
    level: "INFO",
    ...overrides,
  };
}

describe("CausalityGraph", () => {
  it("renders the empty-state copy when no edges are supplied", () => {
    render(<CausalityGraph edges={[]} />);
    expect(
      screen.getByText("No causality edges recorded for this saga."),
    ).toBeInTheDocument();
  });

  it("does NOT render an SVG when there are no edges", () => {
    const { container } = render(<CausalityGraph edges={[]} />);
    expect(container.querySelector("svg")).toBeNull();
  });

  it("renders an SVG with the i18n aria-label when edges exist", () => {
    render(<CausalityGraph edges={[mkEdge()]} />);
    const svg = screen.getByRole("img", { name: "Causality" });
    expect(svg.tagName.toLowerCase()).toBe("svg");
  });

  it("draws one node per edge plus one path per non-root edge", () => {
    const edges: CausalityEdge[] = [
      mkEdge({ id: "root", cause_id: null }),
      mkEdge({ id: "child-a", cause_id: "root", event: "step.a" }),
      mkEdge({ id: "child-b", cause_id: "root", event: "step.b" }),
    ];
    const { container } = render(<CausalityGraph edges={edges} />);
    expect(container.querySelectorAll("circle")).toHaveLength(3);
    // Two children → two cubic-Bezier edges; the root has no parent path.
    expect(container.querySelectorAll("path")).toHaveLength(2);
  });

  it("invokes onNodeSelect with the clicked edge", () => {
    const handler = vi.fn();
    const edges: CausalityEdge[] = [
      mkEdge({ id: "root", cause_id: null, event: "boot" }),
      mkEdge({ id: "child", cause_id: "root", event: "next" }),
    ];
    const { container } = render(
      <CausalityGraph edges={edges} onNodeSelect={handler} />,
    );
    const groups = container.querySelectorAll("svg > g");
    // Click the second node group (the child) — the first one is the root.
    fireEvent.click(groups[1]!);
    expect(handler).toHaveBeenCalledTimes(1);
    expect(handler.mock.calls[0]?.[0]).toMatchObject({ id: "child" });
  });

  it("renders the event label truncated to 18 chars", () => {
    const edges = [mkEdge({ event: "this.is.a.very.long.event.name.too" })];
    render(<CausalityGraph edges={edges} />);
    // First 18 chars of the event are surfaced as the node caption.
    expect(screen.getByText("this.is.a.very.lon")).toBeInTheDocument();
  });

  it("dims non-ancestry edges when highlightedId is provided", () => {
    const edges: CausalityEdge[] = [
      mkEdge({ id: "root", cause_id: null }),
      mkEdge({ id: "mid", cause_id: "root" }),
      mkEdge({ id: "leaf", cause_id: "mid" }),
      mkEdge({ id: "off-chain", cause_id: "root" }),
    ];
    const { container } = render(
      <CausalityGraph edges={edges} highlightedId="leaf" />,
    );
    const paths = container.querySelectorAll("path");
    // Three child-edges → three paths. Two are on the leaf's ancestry chain
    // (root→mid, mid→leaf), one is off-chain (root→off-chain).
    const opacities = Array.from(paths).map((p) => Number(p.getAttribute("opacity")));
    expect(opacities.filter((o) => o === 0.9)).toHaveLength(2);
    expect(opacities.filter((o) => o === 0.15)).toHaveLength(1);
  });

  it("falls back to the INFO colour when an unknown level is supplied", () => {
    const edges = [mkEdge({ level: "MYSTERY" as never })];
    const { container } = render(<CausalityGraph edges={edges} />);
    const circle = container.querySelector("circle");
    expect(circle?.getAttribute("fill")).toBe("var(--svx-color-success)");
  });

  it("respects width / height overrides", () => {
    const { container } = render(
      <CausalityGraph edges={[mkEdge()]} width={500} height={250} />,
    );
    const svg = container.querySelector("svg")!;
    expect(svg.getAttribute("width")).toBe("500");
    expect(svg.getAttribute("height")).toBe("250");
  });
});
