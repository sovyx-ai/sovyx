/**
 * SagaTimeline — horizontal swim-lane timeline of a saga's spans.
 *
 * Each lane corresponds to a logical area (voice, llm, brain, bridge,
 * dashboard, …) derived from the dotted-prefix of the entry's logger
 * name. Spans are positioned along the X axis by their offset (in ms)
 * from the saga's first event; the span width is computed by pairing
 * each entry with the next entry on the same span_id (or the next
 * entry overall if no span is set).
 *
 * The component is read-only — interactions like "open this span"
 * stay in the host page, which already owns selection state.
 *
 * Visual encoding:
 *   * Lane colour: per area, from a stable palette.
 *   * Span height: 18 px.
 *   * Span border: warning when level is WARNING; error glow when
 *     level is ERROR / CRITICAL.
 *   * Tooltip: native <title> shows event name + duration_ms + level.
 *
 * Aligned with IMPL-OBSERVABILITY-001 §16 Task 10.6.
 */

import { memo, useMemo } from "react";
import { useTranslation } from "react-i18next";

import type { LogEntry } from "@/types/api";
import { cn } from "@/lib/utils";

interface SagaTimelineProps {
  entries: LogEntry[];
  /** Optional click callback so the host page can open the row. */
  onSpanSelect?: (entry: LogEntry) => void;
  /** Highlighted entry (e.g. the currently focused log row). */
  highlightedKey?: string | null;
  className?: string;
}

const LANE_COLOR: Record<string, string> = {
  voice: "var(--svx-color-brand-primary)",
  llm: "#a855f7", // violet
  brain: "#0ea5e9", // sky
  bridge: "#22c55e", // green
  dashboard: "#f97316", // orange
  cognitive: "#ec4899", // pink
  plugins: "#facc15", // yellow
  observability: "var(--svx-color-text-tertiary)",
  default: "var(--svx-color-text-secondary)",
};

const LANE_H = 28;
const SPAN_H = 18;
const PADDING_X = 60;
const PADDING_Y = 16;

function laneOf(logger: string): string {
  // Logger names look like ``sovyx.voice.pipeline._orchestrator``; pull
  // the second segment (the area). Fall back to the first segment, or
  // ``default`` for empty / malformed loggers.
  const parts = logger.split(".");
  if (parts.length >= 2 && parts[0] === "sovyx") return parts[1] ?? "default";
  return parts[0] || "default";
}

function entryKey(entry: LogEntry, idx: number): string {
  if (typeof entry.sequence_no === "number") return `seq-${entry.sequence_no}`;
  return `${entry.timestamp}-${idx}`;
}

function parseTs(ts: string): number {
  const t = Date.parse(ts);
  return Number.isFinite(t) ? t : 0;
}

interface Span {
  entry: LogEntry;
  lane: string;
  startMs: number;
  durationMs: number;
}

function buildSpans(entries: LogEntry[]): {
  spans: Span[];
  lanes: string[];
  totalMs: number;
} {
  if (entries.length === 0) return { spans: [], lanes: [], totalMs: 1 };

  const sorted = [...entries].sort((a, b) => parseTs(a.timestamp) - parseTs(b.timestamp));
  const t0 = parseTs(sorted[0]!.timestamp);
  const tEnd = parseTs(sorted[sorted.length - 1]!.timestamp);
  const totalMs = Math.max(tEnd - t0, 1);

  // Pair each entry with the next sibling sharing its span_id (if any)
  // to derive a duration. Otherwise fall back to the next entry's
  // timestamp — gives a contiguous timeline without gaps.
  const spans: Span[] = [];
  const lastSeenBySpan = new Map<string, number>();

  sorted.forEach((entry, idx) => {
    const start = parseTs(entry.timestamp) - t0;
    const next = sorted[idx + 1];
    const nextStart = next ? parseTs(next.timestamp) - t0 : start + 1;
    const dur = Math.max(nextStart - start, 1);
    const lane = laneOf(entry.logger);
    spans.push({ entry, lane, startMs: start, durationMs: dur });
    if (entry.span_id) lastSeenBySpan.set(entry.span_id, idx);
  });

  // Order lanes by first appearance — keeps related areas together.
  const seen = new Set<string>();
  const lanes: string[] = [];
  for (const span of spans) {
    if (!seen.has(span.lane)) {
      seen.add(span.lane);
      lanes.push(span.lane);
    }
  }

  return { spans, lanes, totalMs };
}

function SagaTimelineImpl({
  entries,
  onSpanSelect,
  highlightedKey,
  className,
}: SagaTimelineProps) {
  const { t } = useTranslation(["logs"]);

  const { spans, lanes, totalMs } = useMemo(() => buildSpans(entries), [entries]);

  if (entries.length === 0) {
    return (
      <p className="text-xs text-[var(--svx-color-text-secondary)]">
        {t("tabs.sagaEmpty")}
      </p>
    );
  }

  // Width: 600 px or 4 px per ms (capped at 4000 px so absurdly long
  // sagas stay scrollable without overflowing memory).
  const innerWidth = Math.min(Math.max(totalMs * 4, 600), 4000);
  const width = innerWidth + PADDING_X;
  const height = lanes.length * LANE_H + PADDING_Y * 2;

  return (
    <div className={cn("overflow-x-auto", className)}>
      <svg
        role="img"
        aria-label={t("tabs.saga")}
        width={width}
        height={height}
        className="block"
      >
        {/* Lane labels + horizontal grid lines */}
        {lanes.map((lane, i) => {
          const y = PADDING_Y + i * LANE_H;
          return (
            <g key={`lane-${lane}`}>
              <text
                x={4}
                y={y + LANE_H / 2 + 4}
                className="fill-[var(--svx-color-text-secondary)] text-[10px]"
              >
                {lane}
              </text>
              <line
                x1={PADDING_X}
                x2={width}
                y1={y + LANE_H}
                y2={y + LANE_H}
                stroke="var(--svx-color-border-subtle)"
                strokeWidth={0.5}
              />
            </g>
          );
        })}

        {spans.map((span, idx) => {
          const laneIdx = lanes.indexOf(span.lane);
          const x = PADDING_X + (span.startMs / totalMs) * innerWidth;
          const w = Math.max((span.durationMs / totalMs) * innerWidth, 2);
          const y = PADDING_Y + laneIdx * LANE_H + (LANE_H - SPAN_H) / 2;
          const fill = LANE_COLOR[span.lane] ?? LANE_COLOR.default;
          const isError = span.entry.level === "ERROR" || span.entry.level === "CRITICAL";
          const isWarn = span.entry.level === "WARNING";
          const key = entryKey(span.entry, idx);
          const isSelected = highlightedKey === key;

          return (
            <g
              key={key}
              className={onSpanSelect ? "cursor-pointer" : undefined}
              onClick={() => onSpanSelect?.(span.entry)}
            >
              <title>
                {span.entry.event}
                {"\n"}
                {span.entry.logger}
                {"\n"}
                {Math.round(span.durationMs)} ms · {span.entry.level}
              </title>
              <rect
                x={x}
                y={y}
                width={w}
                height={SPAN_H}
                rx={3}
                ry={3}
                fill={fill}
                fillOpacity={isError ? 0.95 : 0.75}
                stroke={
                  isSelected
                    ? "var(--svx-color-brand-primary)"
                    : isError
                      ? "var(--svx-color-error)"
                      : isWarn
                        ? "var(--svx-color-warning)"
                        : "transparent"
                }
                strokeWidth={isSelected ? 2 : isError || isWarn ? 1 : 0}
              />
            </g>
          );
        })}
      </svg>
    </div>
  );
}

export const SagaTimeline = memo(SagaTimelineImpl);
