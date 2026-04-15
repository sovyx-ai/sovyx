/**
 * LogRow — Single expandable log entry for the virtual scroll list.
 *
 * Click to expand structured extra fields (JSON). Uses measureElement
 * from @tanstack/react-virtual for dynamic height remeasurement.
 *
 * Ref: Architecture §3.4, immersion-final §3
 */

import {
  memo,
  useCallback,
  useMemo,
  useState,
  type KeyboardEvent,
  type MouseEvent,
} from "react";
import type { LogEntry } from "@/types/api";
import { formatTimePrecise } from "@/lib/format";
import { safeStringify } from "@/lib/safe-json";
import { cn } from "@/lib/utils";

interface LogRowProps {
  entry: LogEntry;
}

const LEVEL_STYLES: Record<LogEntry["level"], string> = {
  DEBUG: "text-[var(--svx-color-text-tertiary)]",
  INFO: "text-[var(--svx-color-success)]",
  WARNING: "text-[var(--svx-color-warning)]",
  ERROR: "text-[var(--svx-color-error)]",
  CRITICAL: "text-[var(--svx-color-error)] font-bold",
};

const LEVEL_BG: Record<LogEntry["level"], string> = {
  DEBUG: "",
  INFO: "",
  WARNING: "bg-[var(--svx-color-warning-subtle)]",
  ERROR: "bg-[var(--svx-color-error-subtle)]",
  CRITICAL: "bg-[var(--svx-color-error-subtle)]",
};



function LogRowImpl({ entry }: LogRowProps) {
  const [expanded, setExpanded] = useState(false);

  const toggle = useCallback(() => {
    setExpanded((v) => !v);
  }, []);

  const handleClick = useCallback(
    (e: MouseEvent) => {
      e.stopPropagation();
      toggle();
    },
    [toggle],
  );

  const handleKeyDown = useCallback(
    (e: KeyboardEvent<HTMLDivElement>) => {
      // Only Enter and Space activate — matches native <button> semantics.
      if (e.key === "Enter" || e.key === " ") {
        e.preventDefault();
        e.stopPropagation();
        toggle();
      }
    },
    [toggle],
  );

  // Extract known fields; rest is extra structured data
  const { extraFields, hasExtra } = useMemo(() => {
    const { timestamp: _ts, level: _lv, logger: _lg, event: _ev, ...rest } = entry;
    return { extraFields: rest, hasExtra: Object.keys(rest).length > 0 };
  }, [entry]);

  // Only expose the button role + tabstop when there's something to expand.
  // An inert row (no extra fields) stays as a plain <div>.
  const interactiveProps = hasExtra
    ? {
        role: "button" as const,
        tabIndex: 0,
        "aria-expanded": expanded,
        onClick: handleClick,
        onKeyDown: handleKeyDown,
      }
    : {};

  return (
    <div
      className={cn(
        "font-code border-b border-[var(--svx-color-border-subtle)] px-3 py-1.5 text-xs transition-colors",
        LEVEL_BG[entry.level],
        hasExtra && "cursor-pointer hover:bg-[var(--svx-color-bg-hover)] focus:outline-none focus:ring-2 focus:ring-[var(--svx-color-brand-primary)]/60",
      )}
      {...interactiveProps}
    >
      <div className="flex items-baseline gap-3">
        <span className="shrink-0 text-[var(--svx-color-text-tertiary)]">
          {formatTimePrecise(entry.timestamp)}
        </span>
        <span className={cn("w-12 shrink-0 font-medium", LEVEL_STYLES[entry.level])}>
          {entry.level.padEnd(5)}
        </span>
        <span className="shrink-0 text-[var(--svx-color-brand-muted)]">
          {entry.logger}
        </span>
        <span className="min-w-0 truncate text-[var(--svx-color-text-primary)]">
          {entry.event}
        </span>
      </div>
      {expanded && hasExtra && (
        <pre className="mt-1 overflow-x-auto rounded-[var(--svx-radius-sm)] bg-[var(--svx-color-bg-elevated)] p-2 text-[10px] text-[var(--svx-color-text-secondary)]">
          {safeStringify(extraFields)}
        </pre>
      )}
    </div>
  );
}

export const LogRow = memo(LogRowImpl);
