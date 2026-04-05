/**
 * CategoryLegend + RelationLegend — Brain graph legends.
 *
 * Ref: Architecture §3.3, META-04 §5
 */

import type { ConceptCategory, RelationType } from "@/types/api";
import { CATEGORY_LABELS, CATEGORY_COLORS } from "@/lib/constants";

interface CategoryLegendProps {
  counts?: Record<string, number>;
}

export function CategoryLegend({ counts }: CategoryLegendProps) {
  const categories = Object.keys(CATEGORY_LABELS) as ConceptCategory[];

  return (
    <div className="flex flex-wrap gap-3">
      {categories.map((cat) => (
        <div key={cat} className="flex items-center gap-1.5">
          <span
            className="inline-block size-2.5 rounded-full"
            style={{ backgroundColor: CATEGORY_COLORS[cat] }}
            aria-hidden="true"
          />
          <span className="text-[11px] text-[var(--svx-color-text-secondary)]">
            {CATEGORY_LABELS[cat]}
            {counts?.[cat] != null && (
              <span className="ml-1 text-[var(--svx-color-text-primary)] opacity-70">
                {counts[cat]}
              </span>
            )}
          </span>
        </div>
      ))}
    </div>
  );
}

/** Relation type legend — shows line styles. */
const RELATION_LABELS: Record<RelationType, string> = {
  related_to: "Related",
  part_of: "Part of",
  causes: "Causes",
  contradicts: "Contradicts",
  example_of: "Example",
  temporal: "Temporal",
  emotional: "Emotional",
};

const RELATION_STYLES: Record<RelationType, string> = {
  related_to: "border-solid",
  part_of: "border-dashed",
  causes: "border-dashed",
  contradicts: "border-dotted",
  example_of: "border-dashed",
  temporal: "border-dashed",
  emotional: "border-dotted",
};

export function RelationLegend() {
  const types = Object.keys(RELATION_LABELS) as RelationType[];

  return (
    <div className="flex flex-wrap gap-3">
      {types.map((type) => (
        <div key={type} className="flex items-center gap-1.5">
          <span
            className={`inline-block w-4 border-t-2 ${RELATION_STYLES[type]} ${
              type === "contradicts"
                ? "border-[var(--svx-color-error)]"
                : "border-[var(--svx-color-text-tertiary)]"
            }`}
            aria-hidden="true"
          />
          <span className="text-[11px] text-[var(--svx-color-text-secondary)]">
            {RELATION_LABELS[type]}
          </span>
        </div>
      ))}
    </div>
  );
}
