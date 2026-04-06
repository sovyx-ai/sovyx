/**
 * CategoryLegend + RelationLegend — Brain graph legends.
 *
 * Ref: Architecture §3.3, META-04 §5
 */

import { useTranslation } from "react-i18next";
import type { ConceptCategory, RelationType } from "@/types/api";
import { CATEGORY_COLORS } from "@/lib/constants";

interface CategoryLegendProps {
  counts?: Record<string, number>;
}

const CATEGORIES: ConceptCategory[] = ["fact", "preference", "entity", "skill", "belief", "event", "relationship"];

export function CategoryLegend({ counts }: CategoryLegendProps) {
  const { t } = useTranslation("brain");

  return (
    <div className="flex flex-wrap gap-3">
      {CATEGORIES.map((cat) => (
        <div key={cat} className="flex items-center gap-1.5">
          <span
            className="inline-block size-2.5 rounded-full"
            style={{ backgroundColor: CATEGORY_COLORS[cat] }}
            aria-hidden="true"
          />
          <span className="text-[11px] text-[var(--svx-color-text-secondary)]">
            {t(`categories.${cat}`)}
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

const RELATION_STYLES: Record<RelationType, string> = {
  related_to: "border-solid",
  part_of: "border-dashed",
  causes: "border-dashed",
  contradicts: "border-dotted",
  example_of: "border-dashed",
  temporal: "border-dashed",
  emotional: "border-dotted",
};

const RELATION_TYPES: RelationType[] = ["related_to", "part_of", "causes", "contradicts", "example_of", "temporal", "emotional"];

export function RelationLegend() {
  const { t } = useTranslation("brain");
  const types = RELATION_TYPES;

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
            {t(`relations.${type}`)}
          </span>
        </div>
      ))}
    </div>
  );
}
