/**
 * NarrativePanel — render a saga as a human-readable storyline.
 *
 * Backed by ``GET /api/logs/sagas/{id}/story`` (locale-aware), which
 * returns a pre-rendered multi-line story produced by
 * :func:`sovyx.observability.narrative.build_user_journey`. The
 * backend already does the localization (pt-BR / en-US), so this
 * component focuses on presentation:
 *
 * * **Compact** mode: one line per event, monospace, easy to scan.
 * * **Expanded** mode: same lines with a numeric prefix and bigger
 *   spacing — friendlier for sharing in incident reports.
 *
 * If the backend ever ships a structured ``steps`` array on
 * :type:`NarrativeResponse` (per-event metadata), the component
 * promotes it into a richer ordered list with timestamps + level
 * icons. Until then it parses the ``story`` string into one entry
 * per line.
 *
 * Aligned with IMPL-OBSERVABILITY-001 §16 Task 10.7.
 */

import { memo, useMemo, useState } from "react";
import { useTranslation } from "react-i18next";
import {
  AlertTriangleIcon,
  CircleAlertIcon,
  CircleIcon,
  InfoIcon,
  RadioTowerIcon,
} from "lucide-react";

import type { NarrativeResponse, NarrativeStep } from "@/types/api";
import { Button } from "@/components/ui/button";
import { cn } from "@/lib/utils";

interface NarrativePanelProps {
  narrative: NarrativeResponse;
  /** Initial mode — defaults to compact. */
  defaultMode?: NarrativeMode;
  className?: string;
}

type NarrativeMode = "compact" | "expanded";

const MODE_KEYS: NarrativeMode[] = ["compact", "expanded"];

const LEVEL_ICON: Record<string, typeof InfoIcon> = {
  DEBUG: CircleIcon,
  INFO: InfoIcon,
  WARNING: AlertTriangleIcon,
  ERROR: CircleAlertIcon,
  CRITICAL: CircleAlertIcon,
};

const LEVEL_TINT: Record<string, string> = {
  DEBUG: "text-[var(--svx-color-text-tertiary)]",
  INFO: "text-[var(--svx-color-success)]",
  WARNING: "text-[var(--svx-color-warning)]",
  ERROR: "text-[var(--svx-color-error)]",
  CRITICAL: "text-[var(--svx-color-error)]",
};

interface RenderedLine {
  text: string;
  step?: NarrativeStep;
  /** Iso timestamp when the backend provided a structured step. */
  ts?: string;
  /** Detected level (from the structured step or a per-line heuristic). */
  level?: string;
}

/**
 * Pull the lines out of either ``steps`` (structured, preferred) or
 * the plain ``story`` string (fallback for the current backend).
 */
function buildLines(narrative: NarrativeResponse): RenderedLine[] {
  if (narrative.steps?.length) {
    return narrative.steps.map((step) => ({
      text: step.text,
      step,
      ts: step.timestamp,
    }));
  }
  return narrative.story
    .split(/\r?\n/)
    .filter((line) => line.trim().length > 0)
    .map((text) => ({ text }));
}

/** Heuristic: detect WARNING / ERROR markers in fallback prose. */
function inferLevel(line: string): string | undefined {
  const lower = line.toLowerCase();
  if (lower.includes("[error]") || lower.includes("erro")) return "ERROR";
  if (lower.includes("[warn") || lower.includes("warning")) return "WARNING";
  return undefined;
}

function relativeTs(prev: string | undefined, current: string | undefined): string | null {
  if (!current) return null;
  if (!prev) return "0 ms";
  const a = Date.parse(prev);
  const b = Date.parse(current);
  if (!Number.isFinite(a) || !Number.isFinite(b)) return null;
  const delta = b - a;
  if (delta < 1_000) return `+${delta} ms`;
  if (delta < 60_000) return `+${(delta / 1_000).toFixed(2)} s`;
  return `+${(delta / 60_000).toFixed(1)} min`;
}

function NarrativePanelImpl({
  narrative,
  defaultMode = "compact",
  className,
}: NarrativePanelProps) {
  const { t } = useTranslation(["logs"]);
  const [mode, setMode] = useState<NarrativeMode>(defaultMode);

  const lines = useMemo(() => buildLines(narrative), [narrative]);

  if (lines.length === 0) {
    return (
      <p className="text-xs text-[var(--svx-color-text-secondary)]">
        {t("tabs.narrativeEmpty")}
      </p>
    );
  }

  return (
    <section className={cn("flex flex-col gap-3", className)}>
      <header className="flex items-center justify-between">
        <span className="inline-flex items-center gap-1.5 text-[10px] uppercase tracking-wider text-[var(--svx-color-text-tertiary)]">
          <RadioTowerIcon className="size-3" />
          {t("narrative.locale")}: {narrative.locale}
        </span>
        <div className="inline-flex rounded-[var(--svx-radius-sm)] border border-[var(--svx-color-border-strong)]">
          {MODE_KEYS.map((m) => (
            <Button
              key={m}
              type="button"
              variant="ghost"
              size="sm"
              className={cn(
                "h-6 rounded-none px-2 text-[10px] font-medium",
                mode === m
                  ? "bg-[var(--svx-color-bg-elevated)] text-[var(--svx-color-text-primary)]"
                  : "text-[var(--svx-color-text-secondary)]",
              )}
              onClick={() => setMode(m)}
            >
              {t(`narrative.mode.${m}`)}
            </Button>
          ))}
        </div>
      </header>

      {mode === "compact" ? (
        <pre className="font-code overflow-x-auto whitespace-pre-wrap rounded-[var(--svx-radius-sm)] bg-[var(--svx-color-bg-elevated)] p-3 text-[11px] leading-snug text-[var(--svx-color-text-primary)]">
          {lines.map((line) => line.text).join("\n")}
        </pre>
      ) : (
        <ol className="space-y-2">
          {lines.map((line, idx) => {
            const level = line.level ?? line.step?.event ?? inferLevel(line.text);
            const Icon = level ? LEVEL_ICON[level] ?? CircleIcon : CircleIcon;
            const tint = level ? LEVEL_TINT[level] ?? LEVEL_TINT.INFO : LEVEL_TINT.INFO;
            const prev = lines[idx - 1]?.ts;
            const rel = relativeTs(prev, line.ts);
            return (
              <li
                key={`${idx}-${line.ts ?? line.text.slice(0, 16)}`}
                className="flex gap-2 rounded-[var(--svx-radius-sm)] border border-[var(--svx-color-border-subtle)] bg-[var(--svx-color-bg-elevated)] p-2"
              >
                <span className={cn("mt-0.5 shrink-0", tint)}>
                  <Icon className="size-3.5" />
                </span>
                <div className="flex min-w-0 flex-col gap-0.5">
                  <span className="text-[11px] text-[var(--svx-color-text-primary)]">
                    {line.text}
                  </span>
                  {(line.ts || rel) && (
                    <span className="text-[10px] text-[var(--svx-color-text-tertiary)]">
                      {line.ts}
                      {rel && <span className="ml-2">{rel}</span>}
                    </span>
                  )}
                </div>
                <span className="ml-auto shrink-0 text-[10px] text-[var(--svx-color-text-tertiary)]">
                  #{idx + 1}
                </span>
              </li>
            );
          })}
        </ol>
      )}
    </section>
  );
}

export const NarrativePanel = memo(NarrativePanelImpl);
