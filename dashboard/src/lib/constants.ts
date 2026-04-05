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

/** Color mapping for brain concept categories (hex for canvas rendering). */
export const CATEGORY_COLORS: Record<ConceptCategory, string> = {
  fact: "#22d3ee",       // cyan
  preference: "#ec4899", // pink
  entity: "#38bdf8",     // sky
  skill: "#a855f7",      // purple
  belief: "#facc15",     // yellow
  event: "#34d399",      // emerald
  relationship: "#fb923c", // orange
};
