import { memo } from "react";
import { cn } from "@/lib/utils";

interface CognitiveProgressProps {
  phase: string;
  detail: string;
}

const PHASE_ORDER = ["perceiving", "attending", "thinking", "acting", "reflecting"];

const PHASE_LABELS: Record<string, string> = {
  perceiving: "Perceiving your message",
  attending: "Checking safety",
  thinking: "Thinking",
  acting: "Acting",
  reflecting: "Reflecting",
};

function phaseProgress(phase: string): number {
  const idx = PHASE_ORDER.indexOf(phase);
  if (idx < 0) return 0;
  return ((idx + 1) / PHASE_ORDER.length) * 100;
}

function CognitiveProgressImpl({ phase, detail }: CognitiveProgressProps) {
  const label = PHASE_LABELS[phase] ?? phase;
  const pct = phaseProgress(phase);

  return (
    <div className="space-y-1.5 rounded-2xl rounded-tl-sm bg-[var(--svx-color-bg-elevated)] px-4 py-3">
      <div className="flex items-center gap-2">
        <div className="size-1.5 animate-pulse rounded-full bg-[var(--svx-color-brand-primary)]" />
        <span className="text-xs font-medium text-[var(--svx-color-text-secondary)]">
          {label}
          {detail && (
            <span className="ml-1 font-normal text-[var(--svx-color-text-tertiary)]">
              — {detail}
            </span>
          )}
        </span>
      </div>
      <div className="h-1 overflow-hidden rounded-full bg-[var(--svx-color-bg-base)]">
        <div
          className={cn(
            "h-full rounded-full bg-[var(--svx-color-brand-primary)] transition-all duration-500 ease-out",
          )}
          style={{ width: `${pct}%` }}
        />
      </div>
    </div>
  );
}

export const CognitiveProgress = memo(CognitiveProgressImpl);
