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

import { useCallback, useMemo, useRef, useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
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

/** Max rows surfaced in the accessible fallback tables.
 *
 * Canvas renders every node/link up to the server-side limit (200 by the
 * current dashboard query). Exposing all 200 × 200 = 40 000 cells via the
 * SR tree would be hostile even for SR users; we truncate to the most
 * important concepts and the strongest relations and emit a translated
 * "N of M" notice when the list is clipped.
 */
const _A11Y_MAX_ROWS = 50;

/**
 * Normalise a link endpoint to its string node ID.
 *
 * ``react-force-graph-2d`` (via d3-force) **mutates links in place**
 * once the simulation starts: ``link.source`` and ``link.target`` are
 * replaced with direct references to the node objects so the physics
 * loop can read ``x/y/vx/vy`` without a lookup. Our wire-format type
 * declares them as strings (matching the backend), but at runtime
 * they can be either a string ID (pre-simulation) or a node object
 * with an ``id`` field (post-simulation, with extra fields like
 * ``__indexColor``, ``index``, ``x``, ``vx``). Rendering the mutated
 * object as a JSX child triggers React error #31, and feeding it to a
 * ``Map<string, ...>`` silently misses every lookup.
 *
 * This helper accepts both shapes and returns the string ID. All
 * downstream code (connection counts, render keys, table cells) must
 * go through it.
 */
function linkEndpointId(endpoint: unknown): string {
  if (typeof endpoint === "string") return endpoint;
  if (endpoint != null && typeof endpoint === "object" && "id" in endpoint) {
    const id = (endpoint as { id: unknown }).id;
    return typeof id === "string" ? id : String(id);
  }
  return String(endpoint);
}

export function BrainGraph({ data, width, height, onNodeClick, highlightedNodeIds }: BrainGraphProps) {
  const { t } = useTranslation("brain");
  const fgRef = useRef<ForceGraphMethods<BrainNode>>(undefined);
  const [hoveredNode, setHoveredNode] = useState<string | null>(null);

  // ── Accessible fallback data ───────────────────────────────────────
  //
  // Screen readers can't read the <canvas> visualisation. Derive the
  // same information — top concepts by importance + strongest relations
  // — into plain HTML tables hidden from sighted users via `sr-only`.
  const { nodesByImportance, linksByWeight, connectionCounts } = useMemo(() => {
    const counts = new Map<string, number>();
    for (const link of data.links) {
      const sourceId = linkEndpointId(link.source);
      const targetId = linkEndpointId(link.target);
      counts.set(sourceId, (counts.get(sourceId) ?? 0) + 1);
      counts.set(targetId, (counts.get(targetId) ?? 0) + 1);
    }
    const sortedNodes = [...data.nodes]
      .sort((a, b) => b.importance - a.importance)
      .slice(0, _A11Y_MAX_ROWS);
    const sortedLinks = [...data.links]
      .sort((a, b) => b.weight - a.weight)
      .slice(0, _A11Y_MAX_ROWS);
    return {
      nodesByImportance: sortedNodes,
      linksByWeight: sortedLinks,
      connectionCounts: counts,
    };
  }, [data]);

  const nodeNameById = useMemo(() => {
    const map = new Map<string, string>();
    for (const n of data.nodes) map.set(n.id, n.name);
    return map;
  }, [data.nodes]);

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

      // Importance glow ring (visible for importance >= 0.75)
      if (node.importance >= 0.75 && !isDimmed) {
        const glowRadius = radius + 2 / globalScale;
        ctx.beginPath();
        ctx.arc(x, y, glowRadius, 0, 2 * Math.PI);
        ctx.globalAlpha = Math.min(0.35, (node.importance - 0.5) * 0.7);
        ctx.fillStyle = GRAPH_COLORS.brandGlow;
        ctx.fill();
        ctx.globalAlpha = 1;
      }

      // Node circle
      ctx.beginPath();
      ctx.arc(x, y, radius, 0, 2 * Math.PI);
      ctx.fillStyle = color;
      ctx.globalAlpha = isDimmed ? 0.12 : (0.3 + node.confidence * 0.7);
      ctx.fill();

      // Border — dashed for low confidence (< 0.4)
      ctx.strokeStyle = color;
      ctx.lineWidth = isHighlighted ? 2 : (isHovered ? 2 : 1);
      if (node.confidence < 0.4 && !isDimmed) {
        ctx.setLineDash([2 / globalScale, 2 / globalScale]);
      } else {
        ctx.setLineDash([]);
      }
      ctx.globalAlpha = isDimmed ? 0.2 : 1;
      ctx.stroke();
      ctx.setLineDash([]); // Reset after border

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

  const nodesTotal = data.nodes.length;
  const linksTotal = data.links.length;
  const linksShown = linksByWeight.length;

  return (
    <div
      role="region"
      aria-label={t("graph.regionLabel", {
        nodes: nodesTotal,
        links: linksTotal,
      })}
    >
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
        linkLabel={(link: BrainLink) => `${link.relation_type.replace("_", " ")} (${link.weight.toFixed(2)})`}
        linkDirectionalParticles={0}
        nodeLabel={(node: BrainNode) =>
          `${node.name}\n📊 importance: ${node.importance.toFixed(2)} | confidence: ${node.confidence.toFixed(2)}\n🏷️ ${node.category} | 👁️ ${node.access_count} views`
        }
        onNodeHover={(node: BrainNode | null) => setHoveredNode(node?.id ?? null)}
        onNodeClick={(node: BrainNode) => onNodeClick?.(node)}
        cooldownTicks={100}
        d3AlphaDecay={0.02}
        d3VelocityDecay={0.3}
      />

      {/* ── Screen-reader fallback ─────────────────────────────────
       *
       * force-graph-2d is a <canvas> — opaque to assistive tech and
       * keyboard users. The block below surfaces the same data as two
       * plain tables, visually hidden with Tailwind's `sr-only` utility.
       * Tables list the top-50 concepts by importance and the
       * strongest relations by weight. Category + relation type route
       * through the existing brain i18n namespace so future locales
       * work without extra plumbing.
       */}
      <div className="sr-only">
        <p>
          {t("graph.a11ySummary", { nodes: nodesTotal, links: linksTotal })}
        </p>

        <h3>{t("graph.a11yConceptsHeading")}</h3>
        <table>
          <thead>
            <tr>
              <th scope="col">{t("graph.a11yTableColName")}</th>
              <th scope="col">{t("graph.a11yTableColCategory")}</th>
              <th scope="col">{t("graph.a11yTableColImportance")}</th>
              <th scope="col">{t("graph.a11yTableColConnections")}</th>
            </tr>
          </thead>
          <tbody>
            {nodesByImportance.map((node) => (
              <tr key={node.id}>
                <th scope="row">{node.name}</th>
                <td>{t(`categories.${node.category}`)}</td>
                <td>{node.importance.toFixed(2)}</td>
                <td>{connectionCounts.get(node.id) ?? 0}</td>
              </tr>
            ))}
          </tbody>
        </table>

        {linksTotal > 0 && (
          <>
            <h3>{t("graph.a11yRelationsHeading")}</h3>
            <table>
              <thead>
                <tr>
                  <th scope="col">{t("graph.a11yRelationsTableColSource")}</th>
                  <th scope="col">{t("graph.a11yRelationsTableColTarget")}</th>
                  <th scope="col">{t("graph.a11yRelationsTableColType")}</th>
                  <th scope="col">{t("graph.a11yRelationsTableColWeight")}</th>
                </tr>
              </thead>
              <tbody>
                {linksByWeight.map((link, idx) => {
                  // Coerce each endpoint through linkEndpointId so the
                  // post-simulation object form (see helper docstring)
                  // renders as the concept name string, not as a raw
                  // node object — which would crash with React #31.
                  const sourceId = linkEndpointId(link.source);
                  const targetId = linkEndpointId(link.target);
                  return (
                    <tr key={`${sourceId}-${targetId}-${idx}`}>
                      <td>{nodeNameById.get(sourceId) ?? sourceId}</td>
                      <td>{nodeNameById.get(targetId) ?? targetId}</td>
                      <td>
                        {t(`relations.${link.relation_type}`)}
                        {link.relation_type === "contradicts" && (
                          <> — {t("graph.a11yContradictsBadge")}</>
                        )}
                      </td>
                      <td>{link.weight.toFixed(2)}</td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
            {linksShown < linksTotal && (
              <p>
                {t("graph.a11yRelationsTruncated", {
                  shown: linksShown,
                  total: linksTotal,
                })}
              </p>
            )}
          </>
        )}
      </div>
    </div>
  );
}
