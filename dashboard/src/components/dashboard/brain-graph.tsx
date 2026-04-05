import { useCallback, useRef, useEffect, useState } from "react";
import ForceGraph2D, { type ForceGraphMethods } from "react-force-graph-2d";
import type { BrainNode, BrainLink } from "@/types/api";
import { CATEGORY_COLORS, CATEGORY_LABELS } from "@/lib/constants";

// Re-export for backwards compatibility
export { CATEGORY_LABELS };

interface GraphData {
  nodes: BrainNode[];
  links: BrainLink[];
}

interface BrainGraphProps {
  data: GraphData;
  width: number;
  height: number;
  onNodeClick?: (node: BrainNode) => void;
}

export function BrainGraph({ data, width, height, onNodeClick }: BrainGraphProps) {
  const fgRef = useRef<ForceGraphMethods<BrainNode>>(undefined);
  const [hoveredNode, setHoveredNode] = useState<string | null>(null);

  // Zoom to fit on data change
  useEffect(() => {
    const timer = setTimeout(() => {
      fgRef.current?.zoomToFit(400, 40);
    }, 500);
    return () => clearTimeout(timer);
  }, [data]);

  // Custom node renderer
  const nodeCanvasObject = useCallback(
    (node: BrainNode, ctx: CanvasRenderingContext2D, globalScale: number) => {
      const x = (node as unknown as { x: number }).x;
      const y = (node as unknown as { y: number }).y;
      if (x === undefined || y === undefined) return;

      const radius = 3 + node.importance * 9; // 3-12px
      const color = CATEGORY_COLORS[node.category] ?? "#94a3b8";
      const isHovered = hoveredNode === node.id;

      // Glow for hovered
      if (isHovered) {
        ctx.shadowBlur = 15;
        ctx.shadowColor = color;
      }

      // Node circle
      ctx.beginPath();
      ctx.arc(x, y, radius, 0, 2 * Math.PI);
      ctx.fillStyle = color;
      ctx.globalAlpha = 0.3 + node.confidence * 0.7;
      ctx.fill();

      // Border
      ctx.strokeStyle = color;
      ctx.lineWidth = isHovered ? 2 : 1;
      ctx.globalAlpha = 1;
      ctx.stroke();

      ctx.shadowBlur = 0;

      // Label (only when zoomed in enough or hovered)
      if (globalScale > 1.5 || isHovered) {
        const label = node.name;
        const fontSize = Math.max(10 / globalScale, 2);
        ctx.font = `${fontSize}px Inter, sans-serif`;
        ctx.textAlign = "center";
        ctx.textBaseline = "top";
        ctx.fillStyle = "rgba(255,255,255,0.85)";
        ctx.fillText(label, x, y + radius + 2);
      }
    },
    [hoveredNode],
  );

  // Link styling
  const linkColor = useCallback(() => "rgba(148,163,184,0.15)", []);
  const linkWidth = useCallback((link: BrainLink) => 0.5 + link.weight * 2, []);

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
        const radius = 3 + node.importance * 9 + 2;
        ctx.beginPath();
        ctx.arc(x, y, radius, 0, 2 * Math.PI);
        ctx.fillStyle = color;
        ctx.fill();
      }}
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
