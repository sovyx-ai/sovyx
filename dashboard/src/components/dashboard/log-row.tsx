import { useState, type MouseEvent } from "react";
import type { LogEntry } from "@/types/api";
import { cn } from "@/lib/utils";

interface LogRowProps {
  entry: LogEntry;
}

const LEVEL_STYLES: Record<LogEntry["level"], string> = {
  DEBUG: "text-muted-foreground",
  INFO: "text-[var(--color-success)]",
  WARNING: "text-[var(--color-warning)]",
  ERROR: "text-destructive",
  CRITICAL: "text-destructive font-bold",
};

const LEVEL_BG: Record<LogEntry["level"], string> = {
  DEBUG: "",
  INFO: "",
  WARNING: "bg-[var(--color-warning)]/5",
  ERROR: "bg-destructive/5",
  CRITICAL: "bg-destructive/10",
};

function formatLogTime(iso: string): string {
  try {
    const d = new Date(iso);
    return d.toLocaleTimeString("en-US", {
      hour: "2-digit",
      minute: "2-digit",
      second: "2-digit",
      // fractionalSecondDigits not in TS lib yet
      hour12: false,
    });
  } catch {
    return "—";
  }
}

export function LogRow({ entry }: LogRowProps) {
  const [expanded, setExpanded] = useState(false);

  const handleClick = (e: MouseEvent) => {
    if (entry.metadata && Object.keys(entry.metadata).length > 0) {
      e.stopPropagation();
      setExpanded((v) => !v);
    }
  };

  const hasMetadata =
    entry.metadata != null && Object.keys(entry.metadata).length > 0;

  return (
    <div
      className={cn(
        "font-code border-b border-border/50 px-3 py-1.5 text-xs transition-colors",
        LEVEL_BG[entry.level],
        hasMetadata && "cursor-pointer hover:bg-secondary/50",
      )}
      onClick={handleClick}
    >
      <div className="flex items-baseline gap-3">
        <span className="shrink-0 text-muted-foreground">
          {formatLogTime(entry.timestamp)}
        </span>
        <span className={cn("w-12 shrink-0 font-medium", LEVEL_STYLES[entry.level])}>
          {entry.level.padEnd(5)}
        </span>
        <span className="shrink-0 text-primary/70">{entry.module}</span>
        <span className="min-w-0 truncate text-foreground">
          {entry.message}
        </span>
      </div>
      {expanded && hasMetadata && (
        <pre className="mt-1 overflow-x-auto rounded bg-secondary/50 p-2 text-[10px] text-muted-foreground">
          {JSON.stringify(entry.metadata, null, 2)}
        </pre>
      )}
    </div>
  );
}
