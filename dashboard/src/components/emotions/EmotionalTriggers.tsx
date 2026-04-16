import { cn } from "@/lib/utils";

interface Trigger {
  concept: string;
  category: string;
  valence: number;
  arousal: number;
  label: string;
  access_count: number;
}

interface EmotionalTriggersProps {
  triggers: Trigger[];
}

const QUADRANT_DOT: Record<string, string> = {
  Excited: "bg-[#f59e0b]",
  Calm: "bg-[#14b8a6]",
  Stressed: "bg-[#f87171]",
  Melancholy: "bg-[#818cf8]",
  Neutral: "bg-[var(--svx-color-text-disabled)]",
};

function dotClass(label: string): string {
  for (const [key, cls] of Object.entries(QUADRANT_DOT)) {
    if (label.startsWith(key)) return cls;
  }
  return QUADRANT_DOT.Neutral ?? "bg-[var(--svx-color-text-disabled)]";
}

export function EmotionalTriggers({ triggers }: EmotionalTriggersProps) {
  return (
    <div className="rounded-[var(--svx-radius-lg)] border border-[var(--svx-color-border-default)] bg-[var(--svx-color-bg-surface)] p-5">
      <h3 className="mb-3 text-sm font-medium text-[var(--svx-color-text-primary)]">
        Emotional Triggers
      </h3>
      {triggers.length === 0 ? (
        <p className="py-4 text-center text-xs text-[var(--svx-color-text-tertiary)]">
          No strong emotional associations yet
        </p>
      ) : (
        <div className="space-y-2">
          {triggers.map((t) => (
            <div
              key={t.concept}
              className="flex items-center gap-2.5 rounded-[var(--svx-radius-md)] px-3 py-2 text-xs transition-colors hover:bg-[var(--svx-color-bg-hover)]"
            >
              <span className={cn("size-2 shrink-0 rounded-full", dotClass(t.label))} />
              <span className="flex-1 truncate font-medium text-[var(--svx-color-text-primary)]">
                {t.concept}
              </span>
              <span className="shrink-0 text-[var(--svx-color-text-tertiary)]">
                {t.label}
              </span>
              <span className="shrink-0 font-mono text-[10px] text-[var(--svx-color-text-disabled)]">
                v:{t.valence >= 0 ? "+" : ""}{t.valence.toFixed(2)}
              </span>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
