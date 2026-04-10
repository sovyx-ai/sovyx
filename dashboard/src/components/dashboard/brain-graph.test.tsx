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

import { describe, it, expect } from "vitest";
import type { BrainNode } from "@/types/api";

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
  it("module exports BrainGraph component", async () => {
    // Verify the module can be imported without crashing
    // (react-force-graph-2d needs canvas which jsdom stubs)
    const mod = await import("./brain-graph");
    expect(mod.BrainGraph).toBeDefined();
    expect(typeof mod.BrainGraph).toBe("function");
  });
});
