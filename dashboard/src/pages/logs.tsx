/**
 * Logs page — Phase 10 rewrite.
 *
 * 3-pane layout:
 *   ┌──────────────┬───────────────────────────┬──────────────────┐
 *   │ FilterBar    │ Virtualized log table     │ Detail tabs      │
 *   │ (left, 280)  │ (center, flex-1)          │ (right, 480)     │
 *   └──────────────┴───────────────────────────┴──────────────────┘
 *
 * Data:
 *   * GET /api/logs/search    — primary FTS5 query, schema-validated.
 *   * GET /api/logs           — fallback when the indexer returns 503
 *                               (pre-Phase-10 deployments).
 *   * GET /api/logs/sagas/:id, /causality, /story — populate the detail
 *                               tabs once a row is selected.
 *   * WS  /api/logs/stream    — live tail with the same filters.
 *
 * URL state: search, level, logger, saga_id, since, until are reflected
 * in the URL via `useSearchParams` so the view is shareable and
 * browser-history aware.
 *
 * The right-pane tab bodies (CausalityGraph, SagaTimeline, NarrativePanel)
 * and the LogFilterBar are extracted into their own components by the
 * subsequent tasks (P10.5 – P10.8). For now they live inline so this
 * commit ships a working page.
 */

import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
  type ChangeEvent,
} from "react";
import { useTranslation } from "react-i18next";
import { useSearchParams } from "react-router";
import { useVirtualizer } from "@tanstack/react-virtual";
import {
  AlertTriangleIcon,
  ArrowDownIcon,
  FileTextIcon,
  RefreshCwIcon,
  SearchIcon,
  TrashIcon,
  XIcon,
} from "lucide-react";

import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { CausalityGraph } from "@/components/dashboard/causality-graph";
import { LogRow } from "@/components/dashboard/log-row";
import { EmptyState } from "@/components/empty-state";
import { LogsEmptyAnimation } from "@/components/empty-state-animations";
import { useDashboardStore } from "@/stores/dashboard";
import { api, ApiError, getToken, isAbortError, BASE_URL } from "@/lib/api";
import { safeStringify } from "@/lib/safe-json";
import { cn } from "@/lib/utils";

import type {
  AnomaliesResponse,
  CausalityGraphResponse,
  LogEntry,
  LogSearchResponse,
  LogStreamFrame,
  NarrativeResponse,
  SagaResponse,
} from "@/types/api";
import {
  AnomaliesResponseSchema,
  CausalityGraphResponseSchema,
  LogSearchResponseSchema,
  LogsResponseSchema,
  NarrativeResponseSchema,
  SagaResponseSchema,
} from "@/types/schemas";

// ── Constants ──────────────────────────────────────────────────────

const LOG_LEVELS = ["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] as const;
type LogLevel = (typeof LOG_LEVELS)[number];

const PAGE_SIZE = 500;
const TAB_KEYS = ["detail", "causality", "saga", "narrative"] as const;
type TabKey = (typeof TAB_KEYS)[number];

const LEVEL_COLORS: Record<LogLevel, string> = {
  DEBUG: "text-[var(--svx-color-text-tertiary)]",
  INFO: "text-[var(--svx-color-success)]",
  WARNING: "text-[var(--svx-color-warning)]",
  ERROR: "text-[var(--svx-color-error)]",
  CRITICAL: "text-[var(--svx-color-error)]",
};

interface FilterState {
  q: string;
  level: LogLevel | null;
  logger: string;
  saga_id: string;
  since: string;
  until: string;
}

function readFilters(params: URLSearchParams): FilterState {
  const level = params.get("level");
  return {
    q: params.get("q") ?? "",
    level:
      level && (LOG_LEVELS as readonly string[]).includes(level)
        ? (level as LogLevel)
        : null,
    logger: params.get("logger") ?? "",
    saga_id: params.get("saga_id") ?? "",
    since: params.get("since") ?? "",
    until: params.get("until") ?? "",
  };
}

function writeFilters(prev: URLSearchParams, next: FilterState): URLSearchParams {
  const out = new URLSearchParams(prev);
  for (const [key, value] of Object.entries(next)) {
    if (value === null || value === "") out.delete(key);
    else out.set(key, String(value));
  }
  return out;
}

function buildSearchPath(filters: FilterState): string {
  const search = new URLSearchParams();
  if (filters.q) search.set("q", filters.q);
  if (filters.level) search.set("level", filters.level);
  if (filters.logger) search.set("logger", filters.logger);
  if (filters.saga_id) search.set("saga_id", filters.saga_id);
  if (filters.since) search.set("since", filters.since);
  if (filters.until) search.set("until", filters.until);
  search.set("limit", String(PAGE_SIZE));
  return `/api/logs/search?${search.toString()}`;
}

function buildLegacyPath(filters: FilterState): string {
  const search = new URLSearchParams({ limit: String(PAGE_SIZE) });
  if (filters.q) search.set("search", filters.q);
  if (filters.level) search.set("level", filters.level);
  if (filters.logger) search.set("module", filters.logger);
  return `/api/logs?${search.toString()}`;
}

function buildStreamUrl(filters: FilterState): string {
  const proto = window.location.protocol === "https:" ? "wss:" : "ws:";
  const host = BASE_URL ? new URL(BASE_URL, window.location.href).host : window.location.host;
  const params = new URLSearchParams();
  const token = getToken();
  if (token) params.set("token", token);
  if (filters.q) params.set("q", filters.q);
  if (filters.level) params.set("level", filters.level);
  if (filters.logger) params.set("logger", filters.logger);
  if (filters.saga_id) params.set("saga_id", filters.saga_id);
  return `${proto}//${host}/api/logs/stream?${params.toString()}`;
}

function entryKey(entry: LogEntry, fallback: number): string {
  const seq = entry.sequence_no;
  if (typeof seq === "number") return `seq-${seq}`;
  return `${entry.timestamp}-${entry.event}-${fallback}`;
}

function dedupedAppend(existing: LogEntry[], incoming: LogEntry[]): LogEntry[] {
  if (incoming.length === 0) return existing;
  const seen = new Set<string>();
  for (const entry of existing) seen.add(entryKey(entry, 0));
  const merged = [...existing];
  for (const entry of incoming) {
    const key = entryKey(entry, merged.length);
    if (seen.has(key)) continue;
    seen.add(key);
    merged.push(entry);
  }
  return merged;
}

// ── Page ───────────────────────────────────────────────────────────

export default function LogsPage() {
  const { t } = useTranslation(["logs", "common"]);
  const setLogs = useDashboardStore((s) => s.setLogs);
  const clearLogs = useDashboardStore((s) => s.clearLogs);

  const [searchParams, setSearchParams] = useSearchParams();
  const [filters, setFilters] = useState<FilterState>(() => readFilters(searchParams));

  const [entries, setEntries] = useState<LogEntry[]>([]);
  const [selectedKey, setSelectedKey] = useState<string | null>(null);
  const [activeTab, setActiveTab] = useState<TabKey>("detail");
  const [autoFollow, setAutoFollow] = useState(true);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [streamConnected, setStreamConnected] = useState(false);
  const [usingFallback, setUsingFallback] = useState(false);

  const parentRef = useRef<HTMLDivElement>(null);
  const prevCountRef = useRef(0);
  const wsRef = useRef<WebSocket | null>(null);

  // Sync filter state ↔ URL search params
  useEffect(() => {
    setFilters(readFilters(searchParams));
  }, [searchParams]);

  const updateFilters = useCallback(
    (patch: Partial<FilterState>) => {
      setSearchParams(
        (prev) => writeFilters(prev, { ...readFilters(prev), ...patch }),
        { replace: true },
      );
    },
    [setSearchParams],
  );

  const resetFilters = useCallback(() => {
    setSearchParams(new URLSearchParams(), { replace: true });
  }, [setSearchParams]);

  // ── Initial / filter-driven fetch ─────────────────────────────────
  const fetchEntries = useCallback(
    async (signal?: AbortSignal) => {
      setLoading(true);
      setError(null);
      try {
        const data = await api.get<LogSearchResponse>(buildSearchPath(filters), {
          signal,
          schema: LogSearchResponseSchema,
        });
        setUsingFallback(false);
        setEntries(data.entries);
        setLogs(data.entries);
      } catch (err) {
        if (isAbortError(err)) return;
        // 503 from /api/logs/search means the FTS sidecar isn't wired
        // up yet — fall back to the legacy file-scan endpoint so the
        // page keeps working through the Phase-10 rollout window.
        if (err instanceof ApiError && err.status === 503) {
          try {
            const legacy = await api.get<{ entries: LogEntry[] }>(
              buildLegacyPath(filters),
              { signal, schema: LogsResponseSchema },
            );
            setUsingFallback(true);
            setEntries(legacy.entries);
            setLogs(legacy.entries);
            return;
          } catch (fallbackErr) {
            if (isAbortError(fallbackErr)) return;
          }
        }
        setError(t("error.loadFailed"));
      } finally {
        setLoading(false);
      }
    },
    [filters, setLogs, t],
  );

  useEffect(() => {
    const controller = new AbortController();
    void fetchEntries(controller.signal);
    return () => controller.abort();
  }, [fetchEntries]);

  // ── Real-time tail via WS /api/logs/stream ───────────────────────
  useEffect(() => {
    if (usingFallback) return;
    let cancelled = false;
    let backoff = 1_000;
    const MAX_BACKOFF = 30_000;
    let reconnectTimer: ReturnType<typeof setTimeout> | null = null;

    const connect = () => {
      if (cancelled) return;
      const ws = new WebSocket(buildStreamUrl(filters));
      wsRef.current = ws;

      ws.onopen = () => {
        backoff = 1_000;
        setStreamConnected(true);
      };

      ws.onmessage = (event) => {
        try {
          const frame = JSON.parse(event.data as string) as LogStreamFrame;
          if (frame.type === "batch" && frame.entries.length > 0) {
            setEntries((prev) => dedupedAppend(prev, frame.entries));
          }
        } catch {
          // Ignore malformed frames
        }
      };

      ws.onclose = () => {
        setStreamConnected(false);
        if (cancelled) return;
        const delay = backoff;
        backoff = Math.min(backoff * 2, MAX_BACKOFF);
        reconnectTimer = setTimeout(connect, delay);
      };

      ws.onerror = () => ws.close();
    };

    connect();

    return () => {
      cancelled = true;
      if (reconnectTimer) clearTimeout(reconnectTimer);
      wsRef.current?.close();
      setStreamConnected(false);
    };
  }, [filters, usingFallback]);

  // ── Virtualization ───────────────────────────────────────────────
  const virtualizer = useVirtualizer({
    count: entries.length,
    getScrollElement: () => parentRef.current,
    estimateSize: () => 40,
    overscan: 8,
    getItemKey: useCallback(
      (index: number) => entryKey(entries[index] ?? ({} as LogEntry), index),
      [entries],
    ),
  });

  // Auto-follow: scroll to bottom when new entries arrive
  useEffect(() => {
    if (autoFollow && entries.length > prevCountRef.current && entries.length > 0) {
      virtualizer.scrollToIndex(entries.length - 1, { align: "end" });
    }
    prevCountRef.current = entries.length;
  }, [entries.length, autoFollow, virtualizer]);

  const handleScroll = useCallback(() => {
    const el = parentRef.current;
    if (!el) return;
    const atBottom = el.scrollHeight - el.scrollTop - el.clientHeight < 50;
    setAutoFollow(atBottom);
  }, []);

  // ── Selection ────────────────────────────────────────────────────
  const selectedEntry = useMemo(() => {
    if (!selectedKey) return null;
    for (let i = 0; i < entries.length; i += 1) {
      const entry = entries[i];
      if (entry && entryKey(entry, i) === selectedKey) return entry;
    }
    return null;
  }, [entries, selectedKey]);

  const selectedSagaId = selectedEntry?.saga_id ?? null;

  // ── Render ───────────────────────────────────────────────────────
  return (
    <div className="flex h-[calc(100vh-6rem)] flex-col gap-3">
      <header className="flex items-center justify-between gap-3">
        <div>
          <h1 className="text-2xl font-bold">{t("title")}</h1>
          <p className="text-xs text-[var(--svx-color-text-secondary)]">
            {t("entryCount", { count: entries.length })}
            {streamConnected && (
              <span className="ml-2 inline-flex items-center gap-1 text-[var(--svx-color-success)]">
                <span className="size-1.5 rounded-full bg-[var(--svx-color-success)]" />
                {t("status.live")}
              </span>
            )}
            {usingFallback && (
              <span className="ml-2 text-[var(--svx-color-warning)]">
                {t("status.legacyFallback")}
              </span>
            )}
          </p>
        </div>
        <div className="flex items-center gap-1.5">
          <Button
            variant="ghost"
            size="icon"
            className="size-7"
            onClick={() => void fetchEntries()}
            title={t("common:actions.retry")}
          >
            <RefreshCwIcon className="size-3.5" />
          </Button>
          <Button
            variant="ghost"
            size="icon"
            className="size-7"
            onClick={() => {
              setEntries([]);
              clearLogs();
            }}
            title={t("actions.clear")}
          >
            <TrashIcon className="size-3.5" />
          </Button>
        </div>
      </header>

      <div className="flex flex-1 gap-3 overflow-hidden">
        {/* ── Left pane: filters (will become LogFilterBar in P10.8) ── */}
        <aside className="flex w-72 shrink-0 flex-col gap-3 overflow-y-auto rounded-[var(--svx-radius-lg)] border border-[var(--svx-color-border-default)] bg-[var(--svx-color-bg-surface)] p-3">
          <div>
            <label className="text-[10px] font-medium uppercase tracking-wider text-[var(--svx-color-text-tertiary)]">
              {t("filters.search")}
            </label>
            <div className="relative mt-1">
              <SearchIcon className="absolute left-2 top-1/2 size-3.5 -translate-y-1/2 text-[var(--svx-color-text-secondary)]" />
              <Input
                value={filters.q}
                onChange={(e: ChangeEvent<HTMLInputElement>) =>
                  updateFilters({ q: e.target.value })
                }
                placeholder={t("filters.searchPlaceholder")}
                className="h-8 pl-7 text-xs"
              />
            </div>
          </div>

          <div>
            <label className="text-[10px] font-medium uppercase tracking-wider text-[var(--svx-color-text-tertiary)]">
              {t("filters.level")}
            </label>
            <div className="mt-1 grid grid-cols-3 gap-1">
              <button
                type="button"
                onClick={() => updateFilters({ level: null })}
                className={cn(
                  "rounded-[var(--svx-radius-sm)] border px-2 py-1 text-[10px] font-medium transition-colors",
                  filters.level === null
                    ? "border-[var(--svx-color-brand-primary)] bg-[var(--svx-color-bg-elevated)] text-[var(--svx-color-text-primary)]"
                    : "border-[var(--svx-color-border-strong)] hover:bg-[var(--svx-color-bg-elevated)]",
                )}
              >
                {t("filters.allLevels")}
              </button>
              {LOG_LEVELS.map((level) => (
                <button
                  key={level}
                  type="button"
                  onClick={() =>
                    updateFilters({ level: filters.level === level ? null : level })
                  }
                  className={cn(
                    "rounded-[var(--svx-radius-sm)] border px-2 py-1 text-[10px] font-medium transition-colors",
                    filters.level === level
                      ? "border-[var(--svx-color-brand-primary)] bg-[var(--svx-color-bg-elevated)]"
                      : "border-[var(--svx-color-border-strong)] hover:bg-[var(--svx-color-bg-elevated)]",
                    LEVEL_COLORS[level],
                  )}
                >
                  {level}
                </button>
              ))}
            </div>
          </div>

          <div>
            <label className="text-[10px] font-medium uppercase tracking-wider text-[var(--svx-color-text-tertiary)]">
              {t("filters.logger")}
            </label>
            <Input
              value={filters.logger}
              onChange={(e: ChangeEvent<HTMLInputElement>) =>
                updateFilters({ logger: e.target.value })
              }
              placeholder="sovyx.brain"
              className="mt-1 h-8 text-xs"
            />
          </div>

          <div>
            <label className="text-[10px] font-medium uppercase tracking-wider text-[var(--svx-color-text-tertiary)]">
              {t("filters.sagaId")}
            </label>
            <Input
              value={filters.saga_id}
              onChange={(e: ChangeEvent<HTMLInputElement>) =>
                updateFilters({ saga_id: e.target.value })
              }
              placeholder="saga-uuid"
              className="mt-1 h-8 text-xs"
            />
          </div>

          <div>
            <label className="text-[10px] font-medium uppercase tracking-wider text-[var(--svx-color-text-tertiary)]">
              {t("filters.since")}
            </label>
            <Input
              type="datetime-local"
              value={filters.since}
              onChange={(e: ChangeEvent<HTMLInputElement>) =>
                updateFilters({ since: e.target.value })
              }
              className="mt-1 h-8 text-xs"
            />
          </div>

          <div>
            <label className="text-[10px] font-medium uppercase tracking-wider text-[var(--svx-color-text-tertiary)]">
              {t("filters.until")}
            </label>
            <Input
              type="datetime-local"
              value={filters.until}
              onChange={(e: ChangeEvent<HTMLInputElement>) =>
                updateFilters({ until: e.target.value })
              }
              className="mt-1 h-8 text-xs"
            />
          </div>

          <Button
            variant="ghost"
            size="sm"
            className="mt-auto h-7 gap-1.5 text-xs"
            onClick={resetFilters}
          >
            <XIcon className="size-3.5" />
            {t("filters.reset")}
          </Button>
        </aside>

        {/* ── Center pane: virtualized log table ─── */}
        <section className="flex flex-1 flex-col overflow-hidden rounded-[var(--svx-radius-lg)] border border-[var(--svx-color-border-default)] bg-[var(--svx-color-bg-surface)]">
          {error ? (
            <EmptyState
              icon={<AlertTriangleIcon className="size-10" />}
              title={error}
              action={{
                label: t("common:actions.retry"),
                onClick: () => void fetchEntries(),
              }}
              className="h-full"
            />
          ) : loading && entries.length === 0 ? (
            <div className="flex h-full items-center justify-center">
              <div className="size-6 animate-spin rounded-full border-2 border-[var(--svx-color-brand-primary)] border-t-transparent" />
            </div>
          ) : entries.length === 0 ? (
            <EmptyState
              icon={<FileTextIcon className="size-10" />}
              animation={<LogsEmptyAnimation />}
              title={t("empty")}
              description={t("emptyDescription")}
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
                  const entry = entries[virtualRow.index];
                  if (!entry) return null;
                  const key = entryKey(entry, virtualRow.index);
                  const isSelected = key === selectedKey;
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
                      className={cn(
                        isSelected && "bg-[var(--svx-color-brand-primary)]/10",
                      )}
                      onClick={() => {
                        setSelectedKey(key);
                        setActiveTab("detail");
                      }}
                    >
                      <LogRow entry={entry} />
                    </div>
                  );
                })}
              </div>
            </div>
          )}
        </section>

        {/* ── Right pane: detail tabs (only when something is selected) ── */}
        {selectedEntry && (
          <DetailPanel
            entry={selectedEntry}
            sagaId={selectedSagaId}
            activeTab={activeTab}
            onTabChange={setActiveTab}
            onClose={() => setSelectedKey(null)}
          />
        )}
      </div>

      {!autoFollow && entries.length > 0 && (
        <Button
          size="sm"
          variant="secondary"
          className="absolute bottom-6 right-[33%] gap-1.5 shadow-lg"
          onClick={() => {
            setAutoFollow(true);
            virtualizer.scrollToIndex(entries.length - 1, { align: "end" });
          }}
        >
          <ArrowDownIcon className="size-3.5" />
          {t("follow")}
        </Button>
      )}
    </div>
  );
}

// ──────────────────────────────────────────────────────────────────
// Right-pane detail tabs — inlined here so the page works on its own.
// CausalityGraph (P10.5), SagaTimeline (P10.6) and NarrativePanel
// (P10.7) replace the placeholders below in their respective commits.
// ──────────────────────────────────────────────────────────────────

interface DetailPanelProps {
  entry: LogEntry;
  sagaId: string | null;
  activeTab: TabKey;
  onTabChange: (tab: TabKey) => void;
  onClose: () => void;
}

function DetailPanel({
  entry,
  sagaId,
  activeTab,
  onTabChange,
  onClose,
}: DetailPanelProps) {
  const { t } = useTranslation(["logs"]);

  return (
    <aside className="flex w-[28rem] shrink-0 flex-col overflow-hidden rounded-[var(--svx-radius-lg)] border border-[var(--svx-color-border-default)] bg-[var(--svx-color-bg-surface)]">
      <header className="flex items-center justify-between border-b border-[var(--svx-color-border-default)] px-3 py-2">
        <span className="truncate text-xs font-medium">{entry.event}</span>
        <Button variant="ghost" size="icon" className="size-6" onClick={onClose}>
          <XIcon className="size-3.5" />
        </Button>
      </header>

      <nav className="flex border-b border-[var(--svx-color-border-default)]">
        {TAB_KEYS.map((tab) => (
          <button
            key={tab}
            type="button"
            onClick={() => onTabChange(tab)}
            className={cn(
              "flex-1 border-b-2 px-2 py-1.5 text-[11px] font-medium transition-colors",
              activeTab === tab
                ? "border-[var(--svx-color-brand-primary)] text-[var(--svx-color-text-primary)]"
                : "border-transparent text-[var(--svx-color-text-secondary)] hover:text-[var(--svx-color-text-primary)]",
            )}
          >
            {t(`tabs.${tab}`)}
          </button>
        ))}
      </nav>

      <div className="flex-1 overflow-auto p-3">
        {activeTab === "detail" && <DetailTab entry={entry} />}
        {activeTab === "causality" && <CausalityTab sagaId={sagaId} />}
        {activeTab === "saga" && <SagaTab sagaId={sagaId} />}
        {activeTab === "narrative" && <NarrativeTab sagaId={sagaId} />}
      </div>
    </aside>
  );
}

function DetailTab({ entry }: { entry: LogEntry }) {
  return (
    <pre className="font-code overflow-x-auto whitespace-pre-wrap rounded-[var(--svx-radius-sm)] bg-[var(--svx-color-bg-elevated)] p-2 text-[10px] text-[var(--svx-color-text-secondary)]">
      {safeStringify(entry)}
    </pre>
  );
}

function CausalityTab({ sagaId }: { sagaId: string | null }) {
  const { t } = useTranslation(["logs"]);
  const [graph, setGraph] = useState<CausalityGraphResponse | null>(null);
  const [loading, setLoading] = useState(false);

  useEffect(() => {
    if (!sagaId) return;
    const controller = new AbortController();
    setLoading(true);
    void api
      .get<CausalityGraphResponse>(
        `/api/logs/sagas/${encodeURIComponent(sagaId)}/causality`,
        { signal: controller.signal, schema: CausalityGraphResponseSchema },
      )
      .then(setGraph)
      .catch((err) => {
        if (!isAbortError(err)) setGraph(null);
      })
      .finally(() => setLoading(false));
    return () => controller.abort();
  }, [sagaId]);

  if (!sagaId) {
    return <p className="text-xs text-[var(--svx-color-text-secondary)]">{t("tabs.noSaga")}</p>;
  }
  if (loading) return <p className="text-xs text-[var(--svx-color-text-secondary)]">…</p>;
  if (!graph || graph.edges.length === 0) {
    return <p className="text-xs text-[var(--svx-color-text-secondary)]">{t("tabs.causalityEmpty")}</p>;
  }
  return <CausalityGraph edges={graph.edges} />;
}

function SagaTab({ sagaId }: { sagaId: string | null }) {
  const { t } = useTranslation(["logs"]);
  const [saga, setSaga] = useState<SagaResponse | null>(null);
  const [loading, setLoading] = useState(false);

  useEffect(() => {
    if (!sagaId) return;
    const controller = new AbortController();
    setLoading(true);
    void api
      .get<SagaResponse>(`/api/logs/sagas/${encodeURIComponent(sagaId)}`, {
        signal: controller.signal,
        schema: SagaResponseSchema,
      })
      .then(setSaga)
      .catch((err) => {
        if (!isAbortError(err)) setSaga(null);
      })
      .finally(() => setLoading(false));
    return () => controller.abort();
  }, [sagaId]);

  if (!sagaId) {
    return <p className="text-xs text-[var(--svx-color-text-secondary)]">{t("tabs.noSaga")}</p>;
  }
  if (loading) return <p className="text-xs text-[var(--svx-color-text-secondary)]">…</p>;
  if (!saga || saga.entries.length === 0) {
    return <p className="text-xs text-[var(--svx-color-text-secondary)]">{t("tabs.sagaEmpty")}</p>;
  }
  // Placeholder: timeline rendering lands in P10.6 (SagaTimeline.tsx).
  return (
    <ul className="space-y-1">
      {saga.entries.map((entry, idx) => (
        <li
          key={entryKey(entry, idx)}
          className="font-code rounded-[var(--svx-radius-sm)] bg-[var(--svx-color-bg-elevated)] p-2 text-[10px]"
        >
          <div className="text-[var(--svx-color-text-tertiary)]">{entry.timestamp}</div>
          <div className="text-[var(--svx-color-text-primary)]">{entry.event}</div>
        </li>
      ))}
    </ul>
  );
}

function NarrativeTab({ sagaId }: { sagaId: string | null }) {
  const { t, i18n } = useTranslation(["logs"]);
  const locale = i18n.language?.startsWith("pt") ? "pt-BR" : "en-US";
  const [narrative, setNarrative] = useState<NarrativeResponse | null>(null);
  const [loading, setLoading] = useState(false);

  useEffect(() => {
    if (!sagaId) return;
    const controller = new AbortController();
    setLoading(true);
    void api
      .get<NarrativeResponse>(
        `/api/logs/sagas/${encodeURIComponent(sagaId)}/story?locale=${locale}`,
        { signal: controller.signal, schema: NarrativeResponseSchema },
      )
      .then(setNarrative)
      .catch((err) => {
        if (!isAbortError(err)) setNarrative(null);
      })
      .finally(() => setLoading(false));
    return () => controller.abort();
  }, [sagaId, locale]);

  if (!sagaId) {
    return <p className="text-xs text-[var(--svx-color-text-secondary)]">{t("tabs.noSaga")}</p>;
  }
  if (loading) return <p className="text-xs text-[var(--svx-color-text-secondary)]">…</p>;
  if (!narrative) {
    return <p className="text-xs text-[var(--svx-color-text-secondary)]">{t("tabs.narrativeEmpty")}</p>;
  }
  // Placeholder: rich rendering lands in P10.7 (NarrativePanel.tsx).
  return (
    <article className="prose prose-invert max-w-none text-xs">
      <pre className="whitespace-pre-wrap text-xs text-[var(--svx-color-text-primary)]">
        {narrative.story}
      </pre>
    </article>
  );
}

// ── Re-export so test files can reach the inline helpers if needed ──
export type { FilterState };
export { readFilters, writeFilters, buildSearchPath, dedupedAppend };

// Anomaly types are declared so future P10.8 components can import the
// contract without re-defining it; silence the unused-import linter.
export type { AnomaliesResponse };
export { AnomaliesResponseSchema };
