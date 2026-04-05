import type { ConceptCategory } from "@/types/api";
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
          <span className="text-[11px] text-muted-foreground">
            {CATEGORY_LABELS[cat]}
            {counts?.[cat] != null && (
              <span className="ml-1 text-foreground/70">{counts[cat]}</span>
            )}
          </span>
        </div>
      ))}
    </div>
  );
}
