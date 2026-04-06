import { useEffect, useState, useCallback, useRef, useMemo } from "react";
import { useTranslation } from "react-i18next";
import { useVirtualizer } from "@tanstack/react-virtual";
import { SearchIcon, ArrowDownIcon, TrashIcon, FileTextIcon, AlertTriangleIcon } from "lucide-react";
import { Card, CardContent } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Button } from "@/components/ui/button";
import { useDashboardStore } from "@/stores/dashboard";
import { api, isAbortError } from "@/lib/api";
import { LogRow } from "@/components/dashboard/log-row";
import type { LogEntry } from "@/types/api";
import { EmptyState } from "@/components/empty-state";
import { LogsEmptyAnimation } from "@/components/empty-state-animations";
import { cn } from "@/lib/utils";

type LogLevel = LogEntry["level"] | "ALL";
const LOG_LEVELS: LogLevel[] = ["ALL", "DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"];

const LEVEL_COLORS: Record<LogLevel, string> = {
  ALL: "text-[var(--svx-color-text-primary)]",
  DEBUG: "text-[var(--svx-color-text-tertiary)]",
  INFO: "text-[var(--svx-color-success)]",
  WARNING: "text-[var(--svx-color-warning)]",
  ERROR: "text-[var(--svx-color-error)]",
  CRITICAL: "text-[var(--svx-color-error)]",
};

export default function LogsPage() {
  const { t } = useTranslation(["logs", "common"]);
  const logs = useDashboardStore((s) => s.logs);
  const setLogs = useDashboardStore((s) => s.setLogs);
  const clearLogs = useDashboardStore((s) => s.clearLogs);

  const [search, setSearch] = useState("");
  const [levelFilter, setLevelFilter] = useState<LogLevel>("ALL");
  const [autoFollow, setAutoFollow] = useState(true);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const parentRef = useRef<HTMLDivElement>(null);
  const prevLogCountRef = useRef(0);

  // Fetch initial logs with AbortController (POLISH-01)
  const fetchLogs = useCallback(async (signal?: AbortSignal) => {
    try {
      setLoading(true);
      setError(null);
      const params = new URLSearchParams({ limit: "500" });
      if (levelFilter !== "ALL") params.set("level", levelFilter);
      if (search) params.set("search", search);
      const data = await api.get<{ entries: LogEntry[] }>(`/api/logs?${params}`, { signal });
      setLogs(data.entries);
    } catch (err) {
      if (isAbortError(err)) return;
      setError("Failed to load logs");
    } finally {
      setLoading(false);
    }
  }, [levelFilter, search, setLogs]);

  useEffect(() => {
    const controller = new AbortController();
    void fetchLogs(controller.signal);
    return () => controller.abort();
  }, [fetchLogs]);

  // Filtered logs
  const filtered = useMemo(() => {
    let result = logs;
    if (levelFilter !== "ALL") {
      result = result.filter((l) => l.level === levelFilter);
    }
    if (search) {
      const q = search.toLowerCase();
      result = result.filter(
        (l) =>
          l.event.toLowerCase().includes(q) ||
          l.logger.toLowerCase().includes(q),
      );
    }
    return result;
  }, [logs, levelFilter, search]);

  // Virtual scroll
  const virtualizer = useVirtualizer({
    count: filtered.length,
    getScrollElement: () => parentRef.current,
    estimateSize: () => 40,
    overscan: 5,
    getItemKey: useCallback((index: number) => filtered[index]?.timestamp ?? index, [filtered]),
  });

  // Auto-follow: scroll to bottom when new logs arrive
  useEffect(() => {
    if (autoFollow && filtered.length > prevLogCountRef.current && filtered.length > 0) {
      virtualizer.scrollToIndex(filtered.length - 1, { align: "end" });
    }
    prevLogCountRef.current = filtered.length;
  }, [filtered.length, autoFollow, virtualizer]);

  // Break auto-follow on manual scroll up
  const handleScroll = useCallback(() => {
    const el = parentRef.current;
    if (!el) return;
    const atBottom = el.scrollHeight - el.scrollTop - el.clientHeight < 50;
    setAutoFollow(atBottom);
  }, []);

  return (
    <div className="flex h-[calc(100vh-6rem)] flex-col gap-4">
      {/* Header + Filters */}
      <div className="flex flex-col gap-3 sm:flex-row sm:flex-wrap sm:items-center sm:justify-between">
        <div>
          <h1 className="text-2xl font-bold">{t("title")}</h1>
          <p className="text-sm text-[var(--svx-color-text-secondary)]">
            {filtered.length} entries
          </p>
        </div>

        <div className="flex items-center gap-2">
          {/* Level filter */}
          <div className="flex overflow-x-auto rounded-md border border-[var(--svx-color-border-strong)]">
            {LOG_LEVELS.map((level) => (
              <button
                key={level}
                type="button"
                onClick={() => setLevelFilter(level)}
                className={cn(
                  "px-2 py-1 text-[10px] font-medium transition-colors",
                  level === levelFilter
                    ? "bg-[var(--svx-color-bg-elevated)] text-[var(--svx-color-text-primary)]"
                    : cn("hover:bg-[var(--svx-color-bg-elevated)]", LEVEL_COLORS[level]),
                )}
              >
                {level}
              </button>
            ))}
          </div>

          {/* Clear */}
          <Button
            variant="ghost"
            size="icon"
            className="size-7"
            onClick={clearLogs}
            title={t("actions.clear")}
          >
            <TrashIcon className="size-3.5" />
          </Button>
        </div>
      </div>

      {/* Search */}
      <div className="relative">
        <SearchIcon className="absolute left-3 top-1/2 size-3.5 -translate-y-1/2 text-[var(--svx-color-text-secondary)]" />
        <Input
          placeholder={t("search")}
          value={search}
          onChange={(e) => setSearch(e.target.value)}
          className="h-8 pl-8 text-xs"
        />
      </div>

      {/* Log viewer */}
      <Card className="flex-1 overflow-hidden">
        <CardContent className="h-full p-0">
          {error ? (
            <EmptyState
              icon={<AlertTriangleIcon className="size-10" />}
              title={error}
              action={{ label: "Retry", onClick: () => void fetchLogs() }}
              className="h-full"
            />
          ) : loading && logs.length === 0 ? (
            <div className="flex h-full items-center justify-center">
              <div className="size-6 animate-spin rounded-full border-2 border-[var(--svx-color-brand-primary)] border-t-transparent" />
            </div>
          ) : filtered.length === 0 ? (
            <EmptyState
              icon={<FileTextIcon className="size-10" />}
              animation={<LogsEmptyAnimation />}
              title={t("empty")}
              description="Log entries will stream here in real-time as the engine runs."
              className="h-full"
            />
          ) : (
            <div
              ref={parentRef}
              onScroll={handleScroll}
              className="h-full overflow-auto contain-strict"
              style={{ overflowAnchor: "none" }}
            >
              <div
                style={{
                  height: virtualizer.getTotalSize(),
                  width: "100%",
                  position: "relative",
                }}
              >
                {virtualizer.getVirtualItems().map((virtualRow) => {
                  const entry = filtered[virtualRow.index];
                  if (!entry) return null;
                  return (
                    <div
                      key={virtualRow.key}
                      data-index={virtualRow.index}
                      ref={virtualizer.measureElement}
                      style={{
                        position: "absolute",
                        top: 0,
                        left: 0,
                        width: "100%",
                        transform: `translateY(${virtualRow.start}px)`,
                      }}
                    >
                      <LogRow entry={entry} />
                    </div>
                  );
                })}
              </div>
            </div>
          )}
        </CardContent>
      </Card>

      {/* Auto-follow indicator */}
      {!autoFollow && filtered.length > 0 && (
        <Button
          size="sm"
          variant="secondary"
          className="fixed bottom-6 right-6 gap-1.5 shadow-lg"
          onClick={() => {
            setAutoFollow(true);
            virtualizer.scrollToIndex(filtered.length - 1, { align: "end" });
          }}
        >
          <ArrowDownIcon className="size-3.5" />
          Follow
        </Button>
      )}
    </div>
  );
}
