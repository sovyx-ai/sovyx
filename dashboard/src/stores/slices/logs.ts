/**
 * Logs slice — Phase-10 state for the FTS-backed Logs page.
 *
 * State surface
 * -------------
 * Existing (kept for backwards compat with the activity feed and cost
 * chart that other slices reach into):
 *   * ``logs``         — generic ring buffer fed by the global WS bus.
 *   * ``recentEvents`` — last 50 ``WsEvent``s for the activity feed.
 *   * ``costData``     — cumulative LLM cost points.
 *
 * Phase-10 additions (FTS5 + sagas + causality + narrative):
 *   * ``logEntries``      — current search result set.
 *   * ``logCursor``       — oldest timestamp seen, drives ``loadMore``.
 *   * ``logFilters``      — q / level / logger / saga_id / since / until.
 *   * ``selectedSagaId``  — currently inspected saga.
 *   * ``causalityGraph``  — graph for the selected saga.
 *   * ``narrative``       — rendered story for the selected saga.
 *   * ``anomalies``       — recent ``anomaly.*`` envelopes.
 *
 * The page that owns selection state can `useDashboardStore` and call
 * the actions exposed below; all network I/O goes through ``api.*``
 * (auth + retry + zod validation) and the WebSocket lifecycle is
 * managed here so unmounting the page in *any* tab tears the stream
 * down cleanly.
 *
 * Aligned with IMPL-OBSERVABILITY-001 §16 Task 10.9.
 */

import type { StateCreator } from "zustand";

import type {
  AnomaliesResponse,
  CausalityGraphResponse,
  LogEntry,
  LogSearchResponse,
  LogStreamFrame,
  NarrativeResponse,
  SagaResponse,
  WsEvent,
} from "@/types/api";
import {
  AnomaliesResponseSchema,
  CausalityGraphResponseSchema,
  LogSearchResponseSchema,
  LogsResponseSchema,
  NarrativeResponseSchema,
  SagaResponseSchema,
} from "@/types/schemas";
import {
  api,
  ApiError,
  BASE_URL,
  getToken,
  isAbortError,
} from "@/lib/api";

import type { DashboardState } from "../dashboard";

const MAX_LOGS = 5_000;
const MAX_EVENTS = 50;
const MAX_COST_POINTS = 288; // 24h at 5min intervals

const PAGE_SIZE = 500;
const STREAM_MAX_BACKOFF_MS = 30_000;
const STREAM_INITIAL_BACKOFF_MS = 1_000;

interface CostDataPoint {
  time: number;
  value: number;
}

export type LogLevelFilter =
  | "DEBUG"
  | "INFO"
  | "WARNING"
  | "ERROR"
  | "CRITICAL";

export interface LogFiltersState {
  q: string;
  level: LogLevelFilter | null;
  logger: string;
  saga_id: string;
  since: string;
  until: string;
}

export const EMPTY_LOG_FILTERS: LogFiltersState = {
  q: "",
  level: null,
  logger: "",
  saga_id: "",
  since: "",
  until: "",
};

// ── WebSocket lifecycle held outside the store ─────────────────────
//
// WebSocket instances are not serializable and tracking them inside
// devtools-managed state causes noisy snapshots. The store owns
// *intent* (`streamConnected`, `streamFiltersKey`); the actual socket
// + reconnect timer live in module scope.

let activeStream: WebSocket | null = null;
let reconnectTimer: ReturnType<typeof setTimeout> | null = null;
let streamCancelled = false;
let currentBackoff = STREAM_INITIAL_BACKOFF_MS;
let activeFiltersKey: string | null = null;

function filtersKey(f: LogFiltersState): string {
  return JSON.stringify([f.q, f.level, f.logger, f.saga_id, f.since, f.until]);
}

function buildSearchPath(filters: LogFiltersState, beforeUntil?: string): string {
  const search = new URLSearchParams();
  if (filters.q) search.set("q", filters.q);
  if (filters.level) search.set("level", filters.level);
  if (filters.logger) search.set("logger", filters.logger);
  if (filters.saga_id) search.set("saga_id", filters.saga_id);
  if (filters.since) search.set("since", filters.since);
  // ``loadMore`` overrides the user's ``until`` with the cursor so we
  // page strictly older entries; the request stays inclusive on the
  // server side, dedup happens on the client.
  const until = beforeUntil ?? filters.until;
  if (until) search.set("until", until);
  search.set("limit", String(PAGE_SIZE));
  return `/api/logs/search?${search.toString()}`;
}

function buildLegacyPath(filters: LogFiltersState): string {
  const search = new URLSearchParams({ limit: String(PAGE_SIZE) });
  if (filters.q) search.set("search", filters.q);
  if (filters.level) search.set("level", filters.level);
  if (filters.logger) search.set("module", filters.logger);
  return `/api/logs?${search.toString()}`;
}

function buildStreamUrl(filters: LogFiltersState): string {
  const proto = window.location.protocol === "https:" ? "wss:" : "ws:";
  const host = BASE_URL
    ? new URL(BASE_URL, window.location.href).host
    : window.location.host;
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
  if (typeof entry.sequence_no === "number") return `seq-${entry.sequence_no}`;
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

function dedupedPrepend(existing: LogEntry[], older: LogEntry[]): LogEntry[] {
  if (older.length === 0) return existing;
  const seen = new Set<string>();
  for (const entry of existing) seen.add(entryKey(entry, 0));
  const fresh: LogEntry[] = [];
  for (const entry of older) {
    const key = entryKey(entry, fresh.length);
    if (seen.has(key)) continue;
    seen.add(key);
    fresh.push(entry);
  }
  // Older first → existing kept later → ascending order preserved.
  return [...fresh, ...existing];
}

function oldestTimestamp(entries: LogEntry[]): string | null {
  if (entries.length === 0) return null;
  let oldest = entries[0]!.timestamp;
  for (let i = 1; i < entries.length; i += 1) {
    const ts = entries[i]!.timestamp;
    if (ts < oldest) oldest = ts;
  }
  return oldest;
}

export interface LogsSlice {
  // ── Existing (preserved) ──────────────────────────────────────────
  logs: LogEntry[];
  addLog: (entry: LogEntry) => void;
  setLogs: (entries: LogEntry[]) => void;
  clearLogs: () => void;

  recentEvents: WsEvent[];
  addEvent: (event: WsEvent) => void;

  costData: CostDataPoint[];

  // ── Phase 10 — search + saga + causality + narrative ─────────────
  logEntries: LogEntry[];
  logCursor: string | null;
  logFilters: LogFiltersState;
  logSearchLoading: boolean;
  logSearchError: string | null;
  logUsingFallback: boolean;
  logHasMore: boolean;

  selectedSagaId: string | null;
  saga: SagaResponse | null;
  sagaLoading: boolean;

  causalityGraph: CausalityGraphResponse | null;
  causalityLoading: boolean;

  narrative: NarrativeResponse | null;
  narrativeLoading: boolean;

  anomalies: AnomaliesResponse | null;
  anomaliesLoading: boolean;

  streamConnected: boolean;

  setLogFilters: (patch: Partial<LogFiltersState>) => void;
  resetLogFilters: () => void;

  searchLogs: (filters?: Partial<LogFiltersState>, signal?: AbortSignal) => Promise<void>;
  loadMore: (signal?: AbortSignal) => Promise<void>;

  streamStart: () => void;
  streamStop: () => void;

  selectSaga: (sagaId: string | null) => void;
  loadSaga: (sagaId: string, signal?: AbortSignal) => Promise<void>;
  loadCausality: (sagaId: string, signal?: AbortSignal) => Promise<void>;
  loadNarrative: (sagaId: string, locale?: "pt-BR" | "en-US", signal?: AbortSignal) => Promise<void>;

  loadAnomalies: (signal?: AbortSignal) => Promise<void>;
}

export const createLogsSlice: StateCreator<DashboardState, [], [], LogsSlice> = (
  set,
  get,
) => ({
  // ── Legacy ring buffer (still consumed by the activity feed) ──────
  logs: [],
  addLog: (entry) =>
    set((state) => {
      if (state.logs.length >= MAX_LOGS) {
        const trimmed = state.logs.slice(Math.floor(MAX_LOGS * 0.1));
        trimmed.push(entry);
        return { logs: trimmed };
      }
      return { logs: [...state.logs, entry] };
    }),
  setLogs: (entries) => set({ logs: entries }),
  clearLogs: () => set({ logs: [] }),

  recentEvents: [],
  addEvent: (event) =>
    set((state) => {
      const newEvents =
        state.recentEvents.length >= MAX_EVENTS
          ? [...state.recentEvents.slice(1), event]
          : [...state.recentEvents, event];

      let newCostData = state.costData;
      if (event.type === "ThinkCompleted" && event.data) {
        const costUsd = Number(
          (event.data as Record<string, unknown>)["cost_usd"] ?? 0,
        );
        if (costUsd > 0) {
          const ts = new Date(event.timestamp).getTime();
          const lastEntry = state.costData[state.costData.length - 1];
          const lastValue = lastEntry?.value ?? 0;
          const point: CostDataPoint = {
            time: ts,
            value: Math.round((lastValue + costUsd) * 10000) / 10000,
          };
          newCostData =
            state.costData.length >= MAX_COST_POINTS
              ? [...state.costData.slice(1), point]
              : [...state.costData, point];
        }
      }

      return { recentEvents: newEvents, costData: newCostData };
    }),

  costData: [],

  // ── Phase 10 ─────────────────────────────────────────────────────
  logEntries: [],
  logCursor: null,
  logFilters: { ...EMPTY_LOG_FILTERS },
  logSearchLoading: false,
  logSearchError: null,
  logUsingFallback: false,
  logHasMore: false,

  selectedSagaId: null,
  saga: null,
  sagaLoading: false,

  causalityGraph: null,
  causalityLoading: false,

  narrative: null,
  narrativeLoading: false,

  anomalies: null,
  anomaliesLoading: false,

  streamConnected: false,

  setLogFilters: (patch) =>
    set((state) => ({ logFilters: { ...state.logFilters, ...patch } })),

  resetLogFilters: () => set({ logFilters: { ...EMPTY_LOG_FILTERS } }),

  searchLogs: async (patch, signal) => {
    const merged: LogFiltersState = patch
      ? { ...get().logFilters, ...patch }
      : { ...get().logFilters };
    set({
      logFilters: merged,
      logSearchLoading: true,
      logSearchError: null,
    });
    try {
      const data = await api.get<LogSearchResponse>(buildSearchPath(merged), {
        signal,
        schema: LogSearchResponseSchema,
      });
      set({
        logEntries: data.entries,
        logCursor: oldestTimestamp(data.entries),
        logUsingFallback: false,
        logHasMore: data.entries.length >= PAGE_SIZE,
        logSearchLoading: false,
      });
    } catch (err) {
      if (isAbortError(err)) {
        set({ logSearchLoading: false });
        return;
      }
      if (err instanceof ApiError && err.status === 503) {
        try {
          const legacy = await api.get<{ entries: LogEntry[] }>(
            buildLegacyPath(merged),
            { signal, schema: LogsResponseSchema },
          );
          set({
            logEntries: legacy.entries,
            logCursor: oldestTimestamp(legacy.entries),
            logUsingFallback: true,
            logHasMore: false,
            logSearchLoading: false,
          });
          return;
        } catch (fallbackErr) {
          if (isAbortError(fallbackErr)) {
            set({ logSearchLoading: false });
            return;
          }
        }
      }
      set({
        logSearchLoading: false,
        logSearchError: "Failed to load logs",
      });
    }
  },

  loadMore: async (signal) => {
    const { logFilters, logCursor, logUsingFallback, logSearchLoading } = get();
    // The legacy fallback returns a single full page (5 000 lines max
    // from the file scan) — there is no cursor we can trust, so the
    // button stays hidden when ``logUsingFallback`` is true.
    if (!logCursor || logUsingFallback || logSearchLoading) return;
    set({ logSearchLoading: true });
    try {
      const data = await api.get<LogSearchResponse>(
        buildSearchPath(logFilters, logCursor),
        { signal, schema: LogSearchResponseSchema },
      );
      set((state) => {
        const merged = dedupedPrepend(state.logEntries, data.entries);
        return {
          logEntries: merged,
          logCursor: oldestTimestamp(merged),
          logHasMore: data.entries.length >= PAGE_SIZE,
          logSearchLoading: false,
        };
      });
    } catch (err) {
      if (isAbortError(err)) {
        set({ logSearchLoading: false });
        return;
      }
      set({ logSearchLoading: false, logSearchError: "Failed to load logs" });
    }
  },

  streamStart: () => {
    const { logFilters, logUsingFallback } = get();
    if (logUsingFallback) return; // No live tail when FTS is offline.

    const desiredKey = filtersKey(logFilters);
    if (activeStream && activeFiltersKey === desiredKey) return;

    // Tear down any prior socket before opening a new one with the
    // freshly-applied filters.
    if (activeStream) {
      streamCancelled = true;
      try {
        activeStream.close();
      } catch {
        /* ignore */
      }
      activeStream = null;
    }
    if (reconnectTimer) {
      clearTimeout(reconnectTimer);
      reconnectTimer = null;
    }
    streamCancelled = false;
    currentBackoff = STREAM_INITIAL_BACKOFF_MS;
    activeFiltersKey = desiredKey;

    const connect = () => {
      if (streamCancelled) return;
      const ws = new WebSocket(buildStreamUrl(get().logFilters));
      activeStream = ws;

      ws.onopen = () => {
        currentBackoff = STREAM_INITIAL_BACKOFF_MS;
        set({ streamConnected: true });
      };

      ws.onmessage = (event) => {
        try {
          const frame = JSON.parse(event.data as string) as LogStreamFrame;
          if (frame.type === "batch" && frame.entries.length > 0) {
            set((state) => ({
              logEntries: dedupedAppend(state.logEntries, frame.entries),
            }));
          }
        } catch {
          /* malformed frame — ignore */
        }
      };

      ws.onclose = () => {
        set({ streamConnected: false });
        if (streamCancelled) return;
        const delay = currentBackoff;
        currentBackoff = Math.min(currentBackoff * 2, STREAM_MAX_BACKOFF_MS);
        reconnectTimer = setTimeout(connect, delay);
      };

      ws.onerror = () => {
        try {
          ws.close();
        } catch {
          /* ignore */
        }
      };
    };

    connect();
  },

  streamStop: () => {
    streamCancelled = true;
    if (reconnectTimer) {
      clearTimeout(reconnectTimer);
      reconnectTimer = null;
    }
    if (activeStream) {
      try {
        activeStream.close();
      } catch {
        /* ignore */
      }
      activeStream = null;
    }
    activeFiltersKey = null;
    set({ streamConnected: false });
  },

  selectSaga: (sagaId) =>
    set({
      selectedSagaId: sagaId,
      // Drop stale per-saga state so the UI doesn't flash old data
      // while the next fetch is in flight.
      saga: null,
      causalityGraph: null,
      narrative: null,
    }),

  loadSaga: async (sagaId, signal) => {
    set({ sagaLoading: true });
    try {
      const data = await api.get<SagaResponse>(
        `/api/logs/sagas/${encodeURIComponent(sagaId)}`,
        { signal, schema: SagaResponseSchema },
      );
      set({ saga: data, sagaLoading: false });
    } catch (err) {
      if (isAbortError(err)) {
        set({ sagaLoading: false });
        return;
      }
      set({ saga: null, sagaLoading: false });
    }
  },

  loadCausality: async (sagaId, signal) => {
    set({ causalityLoading: true });
    try {
      const data = await api.get<CausalityGraphResponse>(
        `/api/logs/sagas/${encodeURIComponent(sagaId)}/causality`,
        { signal, schema: CausalityGraphResponseSchema },
      );
      set({ causalityGraph: data, causalityLoading: false });
    } catch (err) {
      if (isAbortError(err)) {
        set({ causalityLoading: false });
        return;
      }
      set({ causalityGraph: null, causalityLoading: false });
    }
  },

  loadNarrative: async (sagaId, locale = "en-US", signal) => {
    set({ narrativeLoading: true });
    try {
      const data = await api.get<NarrativeResponse>(
        `/api/logs/sagas/${encodeURIComponent(sagaId)}/story?locale=${locale}`,
        { signal, schema: NarrativeResponseSchema },
      );
      set({ narrative: data, narrativeLoading: false });
    } catch (err) {
      if (isAbortError(err)) {
        set({ narrativeLoading: false });
        return;
      }
      set({ narrative: null, narrativeLoading: false });
    }
  },

  loadAnomalies: async (signal) => {
    set({ anomaliesLoading: true });
    try {
      const data = await api.get<AnomaliesResponse>("/api/logs/anomalies", {
        signal,
        schema: AnomaliesResponseSchema,
      });
      set({ anomalies: data, anomaliesLoading: false });
    } catch (err) {
      if (isAbortError(err)) {
        set({ anomaliesLoading: false });
        return;
      }
      set({ anomalies: null, anomaliesLoading: false });
    }
  },
});
