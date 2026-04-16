import { cn } from "@/lib/utils";

interface CurrentMoodProps {
  valence: number;
  arousal: number;
  dominance: number;
  label: string;
  description: string;
  quadrant: string;
  episodeCount: number;
}

const QUADRANT_COLORS: Record<string, string> = {
  positive_active: "#f59e0b",
  positive_passive: "#14b8a6",
  negative_active: "#f87171",
  negative_passive: "#818cf8",
  neutral: "var(--svx-color-text-tertiary)",
};

export function CurrentMood({
  valence,
  arousal,
  dominance,
  label,
  description,
  quadrant,
  episodeCount,
}: CurrentMoodProps) {
  const color = QUADRANT_COLORS[quadrant] ?? QUADRANT_COLORS.neutral;

  return (
    <div className="rounded-[var(--svx-radius-lg)] border border-[var(--svx-color-border-default)] bg-[var(--svx-color-bg-surface)] p-5">
      <div className="flex items-start gap-4">
        <div
          className="mt-1 size-10 shrink-0 rounded-full"
          style={{
            background: `radial-gradient(circle, ${color}40, ${color}15)`,
            border: `2px solid ${color}`,
          }}
        />
        <div className="min-w-0 flex-1">
          <h3
            className="text-lg font-semibold"
            style={{ color }}
          >
            {label}
          </h3>
          <p className="mt-0.5 text-xs text-[var(--svx-color-text-secondary)]">
            {description}
          </p>
          <div className="mt-3 flex gap-4 text-[11px] text-[var(--svx-color-text-tertiary)]">
            <span>
              Valence{" "}
              <span className={cn("font-mono font-medium", valence >= 0 ? "text-[#14b8a6]" : "text-[#f87171]")}>
                {valence >= 0 ? "+" : ""}{valence.toFixed(2)}
              </span>
            </span>
            <span>
              Arousal{" "}
              <span className="font-mono font-medium text-[var(--svx-color-text-secondary)]">
                {arousal.toFixed(2)}
              </span>
            </span>
            <span>
              Dominance{" "}
              <span className="font-mono font-medium text-[var(--svx-color-text-secondary)]">
                {dominance.toFixed(2)}
              </span>
            </span>
          </div>
          <p className="mt-1 text-[10px] text-[var(--svx-color-text-disabled)]">
            Based on {episodeCount} recent episodes
          </p>
        </div>
      </div>
    </div>
  );
}
