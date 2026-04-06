import type { ConceptCategory } from "@/types/api";

/** Human-readable labels for brain concept categories. */
export const CATEGORY_LABELS: Record<ConceptCategory, string> = {
  fact: "Fact",
  preference: "Preference",
  entity: "Entity",
  skill: "Skill",
  belief: "Belief",
  event: "Event",
  relationship: "Relationship",
};

/**
 * Color mapping for brain concept categories.
 *
 * ⚠️ INTENTIONAL HEX VALUES: Canvas 2D API cannot read CSS custom properties.
 * react-force-graph-2d renders nodes via <canvas>, requiring raw hex/rgba.
 * Each color is documented with its closest --svx-* token equivalent.
 */
export const CATEGORY_COLORS: Record<ConceptCategory, string> = {
  fact: "#22d3ee",       // cyan — matches --svx-color-accent-cyan
  preference: "#ec4899", // pink
  entity: "#38bdf8",     // sky
  skill: "#a855f7",      // purple — close to --svx-color-brand-primary
  belief: "#facc15",     // yellow — close to --svx-color-warning
  event: "#34d399",      // emerald — close to --svx-color-success
  relationship: "#fb923c", // orange
};

/**
 * Canvas colors for brain graph (POLISH-08).
 * Canvas 2D context doesn't support CSS custom properties,
 * so we duplicate as rgba constants with clear token references.
 */
export const GRAPH_COLORS = {
  /** Hover glow ring — rgba of --svx-color-brand-primary (#8B5CF6) at 25% */
  brandGlow: "rgba(139, 92, 246, 0.25)",
  /** Contradicts relation highlight — rgba of --svx-color-error (#EF4444) at 40% */
  contradicts: "rgba(239, 68, 68, 0.4)",
  /** Default link color — rgba of --svx-color-text-secondary (#94A3B8) at 15% */
  defaultLink: "rgba(148, 163, 184, 0.15)",
  /** Text fallback — --svx-color-text-secondary */
  textSecondary: "#94a3b8",
} as const;
