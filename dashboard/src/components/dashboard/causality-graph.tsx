/**
 * CausalityGraph — directed-acyclic-graph view of saga causation.
 *
 * Renders the cause→event edges returned by
 * ``GET /api/logs/sagas/{saga_id}/causality`` as a layered SVG graph.
 *
 * Layout strategy (kept dependency-free on purpose — no dagre / no
 * react-flow): a BFS from the roots (``cause_id === null``) assigns
 * each node a depth column; siblings are stacked vertically within
 * their column, and edges are drawn as cubic-Bezier paths from the
 * parent's right edge to the child's left edge. For sagas with up to
 * a few hundred events the cost is negligible and the result reads
 * like a process diagram instead of an arbitrary force layout.
 *
 * Visual encoding:
 *   * Node colour: log level (DEBUG/INFO/WARNING/ERROR/CRITICAL).
 *   * Node radius: 14 px fixed — readability over information density.
 *   * Edges: thin grey curves; they fade out when a node is selected
 *     unless they are on the selected node's ancestry chain.
 *
 * Interaction:
 *   * Click a node → ``onNodeSelect(edge)`` so the host page can swap
 *     the rest of the detail panel (e.g. show that event's metadata).
 *   * Hover a node → native ``<title>`` tooltip with timestamp + logger.
 *
 * Aligned with IMPL-OBSERVABILITY-001 §16 Task 10.5.
 */

import { memo, useMemo, useState } from "react";
import { useTranslation } from "react-i18next";

import type { CausalityEdge } from "@/types/api";
import { cn } from "@/lib/utils";

interface CausalityGraphProps {
  edges: CausalityEdge[];
  /** Optional callback invoked when a node is clicked. */
  onNodeSelect?: (edge: CausalityEdge) => void;
  /** Highlighted node id (e.g. the currently selected log entry). */
  highlightedId?: string | null;
  /** Override SVG width/height. Defaults adapt to the graph content. */
  width?: number;
  height?: number;
  className?: string;
}

const LEVEL_FILL: Record<string, string> = {
  DEBUG: "var(--svx-color-text-tertiary)",
  INFO: "var(--svx-color-success)",
  WARNING: "var(--svx-color-warning)",
  ERROR: "var(--svx-color-error)",
  CRITICAL: "#7c3aed", // purple — distinct from ERROR red.
};

const NODE_R = 14;
const COL_W = 140;
const ROW_H = 48;
const PADDING = 24;

interface LayoutNode {
  edge: CausalityEdge;
  x: number;
  y: number;
}

/**
 * Compute layered positions for every edge node.
 *
 * Roots (cause_id null) sit in column 0. Every child is placed in
 * ``parent.column + 1``. Cycles (which should not happen for a
 * causal chain, but we defend against bad data) bail out at the
 * 1 000th step so the UI never spins forever.
 */
function layout(edges: CausalityEdge[]): {
  nodes: LayoutNode[];
  byId: Map<string, LayoutNode>;
  width: number;
  height: number;
} {
  const byId = new Map<string, LayoutNode>();
  const childrenOf = new Map<string | null, CausalityEdge[]>();

  for (const edge of edges) {
    const parent = edge.cause_id ?? null;
    const bucket = childrenOf.get(parent) ?? [];
    bucket.push(edge);
    childrenOf.set(parent, bucket);
  }

  const depth = new Map<string, number>();
  const queue: Array<{ edge: CausalityEdge; column: number }> = [];

  for (const root of childrenOf.get(null) ?? []) {
    if (root.id) depth.set(root.id, 0);
    queue.push({ edge: root, column: 0 });
  }

  let guard = 0;
  while (queue.length && guard < 1_000) {
    guard += 1;
    const { edge, column } = queue.shift()!;
    if (!edge.id) continue;
    const children = childrenOf.get(edge.id) ?? [];
    for (const child of children) {
      if (!child.id || depth.has(child.id)) continue;
      depth.set(child.id, column + 1);
      queue.push({ edge: child, column: column + 1 });
    }
  }

  // Bucket nodes by column for vertical stacking.
  const columns = new Map<number, CausalityEdge[]>();
  for (const edge of edges) {
    const col = edge.id ? depth.get(edge.id) ?? 0 : 0;
    const bucket = columns.get(col) ?? [];
    bucket.push(edge);
    columns.set(col, bucket);
  }

  let maxRow = 0;
  let maxCol = 0;
  const nodes: LayoutNode[] = [];

  for (const [col, bucket] of [...columns.entries()].sort((a, b) => a[0] - b[0])) {
    bucket.forEach((edge, row) => {
      const node: LayoutNode = {
        edge,
        x: PADDING + col * COL_W + NODE_R,
        y: PADDING + row * ROW_H + NODE_R,
      };
      nodes.push(node);
      if (edge.id) byId.set(edge.id, node);
      if (row > maxRow) maxRow = row;
      if (col > maxCol) maxCol = col;
    });
  }

  return {
    nodes,
    byId,
    width: PADDING * 2 + (maxCol + 1) * COL_W,
    height: PADDING * 2 + (maxRow + 1) * ROW_H,
  };
}

function ancestorsOf(id: string, byCauseId: Map<string, string | null>): Set<string> {
  const set = new Set<string>();
  let cur: string | null = id;
  let guard = 0;
  while (cur && guard < 1_000) {
    guard += 1;
    const next = byCauseId.get(cur);
    if (!next || set.has(next)) break;
    set.add(next);
    cur = next;
  }
  return set;
}

function CausalityGraphImpl({
  edges,
  onNodeSelect,
  highlightedId,
  width,
  height,
  className,
}: CausalityGraphProps) {
  const { t } = useTranslation(["logs"]);
  const [hoveredId, setHoveredId] = useState<string | null>(null);

  const { nodes, byId, width: w, height: h } = useMemo(() => layout(edges), [edges]);

  const causeMap = useMemo(() => {
    const m = new Map<string, string | null>();
    for (const edge of edges) if (edge.id) m.set(edge.id, edge.cause_id);
    return m;
  }, [edges]);

  const ancestry = useMemo(
    () => (highlightedId ? ancestorsOf(highlightedId, causeMap) : new Set<string>()),
    [highlightedId, causeMap],
  );

  if (edges.length === 0) {
    return (
      <p className="text-xs text-[var(--svx-color-text-secondary)]">
        {t("tabs.causalityEmpty")}
      </p>
    );
  }

  const svgW = width ?? Math.max(w, 200);
  const svgH = height ?? Math.max(h, 120);

  return (
    <svg
      role="img"
      aria-label={t("tabs.causality")}
      viewBox={`0 0 ${w} ${h}`}
      width={svgW}
      height={svgH}
      className={cn("max-w-full", className)}
    >
      {/* Edges first so nodes sit on top. */}
      {nodes.map((node) => {
        const parent = node.edge.cause_id ? byId.get(node.edge.cause_id) : null;
        if (!parent) return null;
        const x1 = parent.x + NODE_R;
        const y1 = parent.y;
        const x2 = node.x - NODE_R;
        const y2 = node.y;
        const cx = (x1 + x2) / 2;
        const path = `M ${x1} ${y1} C ${cx} ${y1}, ${cx} ${y2}, ${x2} ${y2}`;
        const onAncestry =
          highlightedId &&
          node.edge.id &&
          (node.edge.id === highlightedId || ancestry.has(node.edge.id));
        return (
          <path
            key={`edge-${node.edge.id ?? node.edge.timestamp}`}
            d={path}
            fill="none"
            stroke="currentColor"
            strokeWidth={onAncestry ? 1.6 : 1}
            opacity={highlightedId ? (onAncestry ? 0.9 : 0.15) : 0.4}
            className="text-[var(--svx-color-text-tertiary)]"
          />
        );
      })}

      {nodes.map((node) => {
        const fill = LEVEL_FILL[node.edge.level] ?? LEVEL_FILL.INFO;
        const isSelected = highlightedId && node.edge.id === highlightedId;
        const isHover = hoveredId === node.edge.id;
        return (
          <g
            key={`node-${node.edge.id ?? node.edge.timestamp}`}
            transform={`translate(${node.x}, ${node.y})`}
            className="cursor-pointer"
            onMouseEnter={() => setHoveredId(node.edge.id)}
            onMouseLeave={() => setHoveredId(null)}
            onClick={() => onNodeSelect?.(node.edge)}
          >
            <title>
              {node.edge.event}
              {"\n"}
              {node.edge.timestamp}
              {"\n"}
              {node.edge.logger ?? ""}
            </title>
            <circle
              r={NODE_R}
              fill={fill}
              opacity={isSelected ? 1 : isHover ? 0.95 : 0.85}
              stroke={
                isSelected
                  ? "var(--svx-color-brand-primary)"
                  : "var(--svx-color-bg-surface)"
              }
              strokeWidth={isSelected ? 3 : 2}
            />
            <text
              y={NODE_R + 14}
              textAnchor="middle"
              className="fill-[var(--svx-color-text-primary)] text-[10px]"
            >
              {(node.edge.event ?? "?").slice(0, 18)}
            </text>
          </g>
        );
      })}
    </svg>
  );
}

export const CausalityGraph = memo(CausalityGraphImpl);
