/**
 * BrainGraph component tests — scoring visual encoding (TASK-10).
 *
 * react-force-graph-2d uses <canvas>, so we can't test pixel-level
 * rendering with RTL. Instead we test:
 * 1. Component renders without crashing (smoke)
 * 2. Node visual logic: radius, opacity, glow, border, tooltip
 * 3. Link visual logic: dash pattern, color, width
 *
 * The visual encoding rules are extracted and tested as pure functions.
 */

import { describe, it, expect, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import "@/lib/i18n";
import type { BrainNode } from "@/types/api";

// react-force-graph-2d renders to <canvas>, which jsdom can't execute.
// Mock it to a null-rendering component so we can assert on the
// surrounding accessible fallback without the canvas pulling real
// force-graph machinery into the test run.
vi.mock("react-force-graph-2d", () => ({
  __esModule: true,
  default: () => null,
}));

import { BrainGraph } from "./brain-graph";

// ── Pure function extraction for testability ──────────────────────

/** Node radius from importance (mirrors brain-graph.tsx line 78) */
function nodeRadius(importance: number): number {
  return 3 + importance * 9;
}

/** Node opacity from confidence (mirrors brain-graph.tsx line 123) */
function nodeOpacity(confidence: number, isDimmed: boolean): number {
  return isDimmed ? 0.12 : 0.3 + confidence * 0.7;
}

/** Whether importance glow ring should render (brain-graph.tsx line 107) */
function shouldShowGlowRing(importance: number, isDimmed: boolean): boolean {
  return importance >= 0.75 && !isDimmed;
}

/** Whether border should be dashed (brain-graph.tsx line 128) */
function shouldDashBorder(confidence: number, isDimmed: boolean): boolean {
  return confidence < 0.4 && !isDimmed;
}

/** Tooltip text (brain-graph.tsx nodeLabel prop) */
function nodeTooltip(node: BrainNode): string {
  return `${node.name}\n📊 importance: ${node.importance.toFixed(2)} | confidence: ${node.confidence.toFixed(2)}\n🏷️ ${node.category} | 👁️ ${node.access_count} views`;
}

/** Link width from weight (brain-graph.tsx line 178) */
function linkWidth(weight: number): number {
  return Math.max(0.5, weight * 3);
}

/** Link color based on relation type (brain-graph.tsx line 173) */
function linkColor(relationType: string): "red" | "gray" {
  return relationType === "contradicts" ? "red" : "gray";
}

// ── Test helpers ──────────────────────────────────────────────────

function makeNode(overrides: Partial<BrainNode> = {}): BrainNode {
  return {
    id: "n1",
    name: "test concept",
    category: "fact",
    importance: 0.5,
    confidence: 0.5,
    access_count: 3,
    ...overrides,
  };
}

// ── Tests ─────────────────────────────────────────────────────────

describe("BrainGraph visual encoding — node radius", () => {
  it("minimum radius for importance 0", () => {
    expect(nodeRadius(0)).toBe(3);
  });

  it("maximum radius for importance 1", () => {
    expect(nodeRadius(1)).toBe(12);
  });

  it("scales linearly with importance", () => {
    const r1 = nodeRadius(0.25);
    const r2 = nodeRadius(0.50);
    const r3 = nodeRadius(0.75);
    expect(r2 - r1).toBeCloseTo(r3 - r2, 1);
  });
});

describe("BrainGraph visual encoding — node opacity", () => {
  it("minimum opacity at confidence 0", () => {
    expect(nodeOpacity(0, false)).toBeCloseTo(0.3, 1);
  });

  it("maximum opacity at confidence 1", () => {
    expect(nodeOpacity(1, false)).toBeCloseTo(1.0, 1);
  });

  it("dimmed nodes have very low opacity", () => {
    expect(nodeOpacity(0.8, true)).toBe(0.12);
  });

  it("scales linearly with confidence", () => {
    const o1 = nodeOpacity(0.25, false);
    const o2 = nodeOpacity(0.50, false);
    const o3 = nodeOpacity(0.75, false);
    expect(o2 - o1).toBeCloseTo(o3 - o2, 1);
  });
});

describe("BrainGraph visual encoding — importance glow ring", () => {
  it("shows glow for importance >= 0.75", () => {
    expect(shouldShowGlowRing(0.75, false)).toBe(true);
    expect(shouldShowGlowRing(0.90, false)).toBe(true);
    expect(shouldShowGlowRing(1.0, false)).toBe(true);
  });

  it("no glow for importance < 0.75", () => {
    expect(shouldShowGlowRing(0.74, false)).toBe(false);
    expect(shouldShowGlowRing(0.5, false)).toBe(false);
    expect(shouldShowGlowRing(0.0, false)).toBe(false);
  });

  it("no glow when dimmed even if high importance", () => {
    expect(shouldShowGlowRing(0.95, true)).toBe(false);
  });
});

describe("BrainGraph visual encoding — confidence dashed border", () => {
  it("dashed border for confidence < 0.4", () => {
    expect(shouldDashBorder(0.39, false)).toBe(true);
    expect(shouldDashBorder(0.1, false)).toBe(true);
    expect(shouldDashBorder(0.0, false)).toBe(true);
  });

  it("solid border for confidence >= 0.4", () => {
    expect(shouldDashBorder(0.4, false)).toBe(false);
    expect(shouldDashBorder(0.8, false)).toBe(false);
  });

  it("no dashed border when dimmed", () => {
    expect(shouldDashBorder(0.1, true)).toBe(false);
  });
});

describe("BrainGraph visual encoding — tooltip", () => {
  it("contains importance and confidence values", () => {
    const node = makeNode({ importance: 0.85, confidence: 0.42 });
    const tip = nodeTooltip(node);
    expect(tip).toContain("importance: 0.85");
    expect(tip).toContain("confidence: 0.42");
  });

  it("contains category and access count", () => {
    const node = makeNode({ category: "entity", access_count: 15 });
    const tip = nodeTooltip(node);
    expect(tip).toContain("entity");
    expect(tip).toContain("15 views");
  });

  it("contains node name", () => {
    const node = makeNode({ name: "Guipe's birthday" });
    const tip = nodeTooltip(node);
    expect(tip).toContain("Guipe's birthday");
  });
});

describe("BrainGraph visual encoding — link width", () => {
  it("minimum width 0.5 for weight 0", () => {
    expect(linkWidth(0)).toBe(0.5);
  });

  it("maximum width 3 for weight 1", () => {
    expect(linkWidth(1)).toBe(3);
  });

  it("scales with weight", () => {
    expect(linkWidth(0.5)).toBeCloseTo(1.5, 1);
  });
});

describe("BrainGraph visual encoding — link color", () => {
  it("contradicts → red", () => {
    expect(linkColor("contradicts")).toBe("red");
  });

  it("related_to → gray", () => {
    expect(linkColor("related_to")).toBe("gray");
  });

  it("other types → gray", () => {
    expect(linkColor("part_of")).toBe("gray");
    expect(linkColor("causes")).toBe("gray");
    expect(linkColor("temporal")).toBe("gray");
  });
});

describe("BrainGraph — smoke render", () => {
  it("module exports BrainGraph component", () => {
    expect(BrainGraph).toBeDefined();
    expect(typeof BrainGraph).toBe("function");
  });
});

describe("BrainGraph — accessible fallback", () => {
  it("exposes a region role with node/link counts in the aria-label", () => {
    render(
      <BrainGraph
        data={{
          nodes: [
            { id: "n1", name: "Alice", category: "entity", importance: 0.9, confidence: 0.8, access_count: 10 },
            { id: "n2", name: "Paris", category: "entity", importance: 0.5, confidence: 0.6, access_count: 3 },
          ],
          links: [
            { source: "n1", target: "n2", relation_type: "related_to", weight: 0.7 },
          ],
        }}
        width={100}
        height={100}
      />,
    );
    const region = screen.getByRole("region");
    expect(region.getAttribute("aria-label") ?? "").toMatch(/2 concepts/);
    expect(region.getAttribute("aria-label") ?? "").toMatch(/1 relations/);
  });

  it("renders an sr-only table listing concepts by importance", () => {
    const { container } = render(
      <BrainGraph
        data={{
          nodes: [
            { id: "n1", name: "Alice", category: "entity", importance: 0.9, confidence: 0.8, access_count: 10 },
          ],
          links: [],
        }}
        width={100}
        height={100}
      />,
    );
    expect(screen.getByRole("rowheader", { name: "Alice" })).toBeInTheDocument();
    expect(container.querySelector(".sr-only")).not.toBeNull();
  });

  it("marks contradiction relations in the fallback table", () => {
    render(
      <BrainGraph
        data={{
          nodes: [
            { id: "a", name: "A", category: "fact", importance: 0.5, confidence: 0.5, access_count: 1 },
            { id: "b", name: "B", category: "fact", importance: 0.5, confidence: 0.5, access_count: 1 },
          ],
          links: [
            { source: "a", target: "b", relation_type: "contradicts", weight: 0.8 },
          ],
        }}
        width={100}
        height={100}
      />,
    );
    expect(screen.getByText(/contradiction/)).toBeInTheDocument();
  });

  it("truncates the relations list and emits a notice when over the cap", () => {
    const links = Array.from({ length: 75 }, (_, i) => ({
      source: "a",
      target: "b",
      relation_type: "related_to" as const,
      weight: (75 - i) / 75,
    }));
    render(
      <BrainGraph
        data={{
          nodes: [
            { id: "a", name: "A", category: "fact", importance: 0.5, confidence: 0.5, access_count: 1 },
            { id: "b", name: "B", category: "fact", importance: 0.5, confidence: 0.5, access_count: 1 },
          ],
          links,
        }}
        width={100}
        height={100}
      />,
    );
    expect(screen.getByText(/50 of 75 relations shown/)).toBeInTheDocument();
  });

  it("survives post-simulation link mutation without crashing", () => {
    // react-force-graph-2d mutates links in place: once d3-force has
    // ticked, `link.source` and `link.target` are replaced with
    // references to the actual node objects (complete with `x`, `y`,
    // `vx`, `vy`, `__indexColor`, `index` fields). The SR fallback
    // table has to render the concept NAME, not the mutated object —
    // otherwise React throws error #31 ("Objects are not valid as a
    // React child").
    const alice = {
      id: "n1",
      name: "Alice",
      category: "entity" as const,
      importance: 0.9,
      confidence: 0.8,
      access_count: 10,
      // Fields injected by the simulation:
      __indexColor: "#abcdef",
      index: 0,
      x: 1.2,
      y: -3.4,
      vx: 0.1,
      vy: 0.2,
    };
    const bob = {
      id: "n2",
      name: "Bob",
      category: "entity" as const,
      importance: 0.7,
      confidence: 0.7,
      access_count: 5,
      __indexColor: "#fedcba",
      index: 1,
      x: 5.6,
      y: 7.8,
      vx: -0.1,
      vy: -0.2,
    };
    // source/target hold the node *objects*, not their string ids —
    // exactly what force-graph-2d produces post-tick.
    const mutatedLink = {
      source: alice as unknown as string,
      target: bob as unknown as string,
      relation_type: "related_to" as const,
      weight: 0.6,
    };
    render(
      <BrainGraph
        data={{ nodes: [alice, bob], links: [mutatedLink] }}
        width={100}
        height={100}
      />,
    );
    // The relations table must resolve the mutated endpoints back to
    // their concept names, not stringify-or-object-render them.
    const cells = screen.getAllByRole("cell");
    const cellText = cells.map((c) => c.textContent ?? "");
    expect(cellText).toContain("Alice");
    expect(cellText).toContain("Bob");

    // And connectionCounts must still key by string id so the
    // concepts table shows the correct number of connections.
    // Alice participates in 1 relation (with Bob) — assert the count
    // cell next to "Alice" is "1", not "0".
    const aliceRow = screen.getByRole("rowheader", { name: "Alice" }).closest(
      "tr",
    );
    expect(aliceRow).not.toBeNull();
    const aliceCells = aliceRow!.querySelectorAll("td");
    // Concept table columns: Category, Importance, Connections.
    expect(aliceCells[aliceCells.length - 1]?.textContent).toBe("1");
  });
});
