/**
 * BrainGraph — Interactive knowledge graph visualization.
 *
 * Uses react-force-graph-2d for rendering. Nodes are concepts,
 * links are relations. Visual encoding:
 *
 * Nodes:
 * - Color: by category (7 categories, from CATEGORY_COLORS)
 * - Radius: 3-12px from importance (0.0-1.0)
 * - Opacity: 0.3-1.0 from confidence (0.0-1.0)
 * - Hover: brand glow effect + label
 * - Click: triggers detail panel
 * - Search highlight: non-matching nodes dimmed when search is active
 *
 * Links:
 * - Line dash: per relation_type (solid, dashed, dotted, etc.)
 * - Width: 0.5-3px from weight
 * - Color: subtle gray (contradicts = red)
 *
 * Ref: Architecture §3.3, META-04 §5, V05-P03 search highlight
 */

import { useCallback, useRef, useEffect, useState } from "react";
import ForceGraph2D, { type ForceGraphMethods } from "react-force-graph-2d";
import type { BrainNode, BrainLink, RelationType } from "@/types/api";
import { CATEGORY_COLORS } from "@/lib/constants";

interface GraphData {
  nodes: BrainNode[];
  links: BrainLink[];
}

interface BrainGraphProps {
  data: GraphData;
  width: number;
  height: number;
  onNodeClick?: (node: BrainNode) => void;
  /** Set of node IDs to highlight (e.g. from search results). */
  highlightedNodeIds?: Set<string>;
}

/** Line dash patterns per relation type — from immersion node. */
const RELATION_LINE_DASH: Record<RelationType, number[] | null> = {
  related_to: null,           // solid
  part_of: [5, 3],            // dashed
  causes: [8, 4],             // long dash
  contradicts: [2, 2],        // dotted (red)
  example_of: [8, 2, 2, 2],  // dash-dot
  temporal: [4, 4],           // even dash
  emotional: [1, 3],          // fine dot
};

import { GRAPH_COLORS } from "@/lib/constants";

export function BrainGraph({ data, width, height, onNodeClick, highlightedNodeIds }: BrainGraphProps) {
  const fgRef = useRef<ForceGraphMethods<BrainNode>>(undefined);
  const [hoveredNode, setHoveredNode] = useState<string | null>(null);

  // Zoom to fit on data change
  useEffect(() => {
    const timer = setTimeout(() => {
      fgRef.current?.zoomToFit(400, 40);
    }, 500);
    return () => clearTimeout(timer);
  }, [data]);

  const hasHighlights = highlightedNodeIds != null && highlightedNodeIds.size > 0;

  // Custom node renderer with glow on hover and search highlight
  const nodeCanvasObject = useCallback(
    (node: BrainNode, ctx: CanvasRenderingContext2D, globalScale: number) => {
      const x = (node as unknown as { x: number }).x;
      const y = (node as unknown as { y: number }).y;
      if (x === undefined || y === undefined) return;

      const radius = 3 + node.importance * 9; // 3-12px
      // Canvas API requires hex — fallback maps to --svx-color-text-secondary
      const color = CATEGORY_COLORS[node.category] ?? GRAPH_COLORS.textSecondary;
      const isHovered = hoveredNode === node.id;
      const isHighlighted = highlightedNodeIds?.has(node.id) ?? false;
      // Dim non-matching nodes when search is active
      const isDimmed = hasHighlights && !isHighlighted && !isHovered;

      // Highlight ring for search-matched nodes
      if (isHighlighted && !isHovered) {
        ctx.beginPath();
        ctx.arc(x, y, radius + 3 / globalScale, 0, 2 * Math.PI);
        ctx.fillStyle = GRAPH_COLORS.searchHighlight ?? GRAPH_COLORS.brandGlow;
        ctx.fill();
      }

      // Glow ring for hovered node (rendered BEFORE the node)
      if (isHovered) {
        ctx.beginPath();
        ctx.arc(x, y, radius + 4 / globalScale, 0, 2 * Math.PI);
        ctx.fillStyle = GRAPH_COLORS.brandGlow;
        ctx.fill();
      }

      // Node circle
      ctx.beginPath();
      ctx.arc(x, y, radius, 0, 2 * Math.PI);
      ctx.fillStyle = color;
      ctx.globalAlpha = isDimmed ? 0.12 : (0.3 + node.confidence * 0.7);
      ctx.fill();

      // Border
      ctx.strokeStyle = color;
      ctx.lineWidth = isHighlighted ? 2 : (isHovered ? 2 : 1);
      ctx.globalAlpha = isDimmed ? 0.2 : 1;
      ctx.stroke();

      // Label (visible when zoomed in, hovered, or highlighted)
      if (globalScale > 1.5 || isHovered || isHighlighted) {
        const label = node.name;
        const fontSize = Math.max(10 / globalScale, 2);
        ctx.font = `${fontSize}px "Geist Sans", ui-sans-serif, sans-serif`;
        ctx.textAlign = "center";
        ctx.textBaseline = "top";
        ctx.fillStyle = isDimmed
          ? "rgba(248, 250, 252, 0.2)"
          : isHovered
            ? "rgba(248, 250, 252, 0.95)"
            : "rgba(248, 250, 252, 0.7)";
        ctx.globalAlpha = 1;
        ctx.fillText(label, x, y + radius + 2);
      }

      ctx.globalAlpha = 1; // Reset
    },
    [hoveredNode, highlightedNodeIds, hasHighlights],
  );

  // Relation type → line dash
  const linkLineDash = useCallback(
    (link: BrainLink) => RELATION_LINE_DASH[link.relation_type] ?? null,
    [],
  );

  // Relation type → color (contradicts = red, rest = subtle gray)
  const linkColor = useCallback(
    (link: BrainLink) =>
      link.relation_type === "contradicts" ? GRAPH_COLORS.contradicts : GRAPH_COLORS.defaultLink,
    [],
  );

  // Width from weight
  const linkWidth = useCallback(
    (link: BrainLink) => Math.max(0.5, link.weight * 3),
    [],
  );

  return (
    <ForceGraph2D
      ref={fgRef}
      graphData={data}
      width={width}
      height={height}
      backgroundColor="transparent"
      nodeCanvasObject={nodeCanvasObject}
      nodePointerAreaPaint={(node: BrainNode, color: string, ctx: CanvasRenderingContext2D) => {
        const x = (node as unknown as { x: number }).x;
        const y = (node as unknown as { y: number }).y;
        if (x === undefined || y === undefined) return;
        const radius = 3 + node.importance * 9 + 4;
        ctx.beginPath();
        ctx.arc(x, y, radius, 0, 2 * Math.PI);
        ctx.fillStyle = color;
        ctx.fill();
      }}
      linkLineDash={linkLineDash}
      linkColor={linkColor}
      linkWidth={linkWidth}
      linkDirectionalParticles={0}
      onNodeHover={(node: BrainNode | null) => setHoveredNode(node?.id ?? null)}
      onNodeClick={(node: BrainNode) => onNodeClick?.(node)}
      cooldownTicks={100}
      d3AlphaDecay={0.02}
      d3VelocityDecay={0.3}
    />
  );
}
