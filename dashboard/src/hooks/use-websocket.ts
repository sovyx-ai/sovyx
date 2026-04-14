/**
 * WebSocket hook with auto-reconnect + debounced API refreshes.
 *
 * DASH-32: Added debouncing to prevent API call bursts when rapid WS events
 * arrive (e.g., 5 ConceptCreated in 200ms during consolidation would previously
 * trigger 5 simultaneous refreshBrain() + refreshStatus() calls).
 *
 * Each refresh target (status, health, brain, conversation) has its own
 * debounce timer. Trailing-edge: only the last call in the window fires.
 * Window: 300ms (fast enough for perceived real-time, slow enough to batch).
 *
 * ZERO-02: Refresh functions use centralized api.get() instead of raw fetch.
 * Only the WS URL construction reads the token directly (WebSocket API
 * doesn't support custom headers — token must go in query string).
 *
 * Events handled (11 real from DashboardEventBridge._serialize_event()):
 *   EngineStarted, EngineStopping, ServiceHealthChanged,
 *   PerceptionReceived, ThinkCompleted, ResponseSent,
 *   ConceptCreated, EpisodeEncoded, ConsolidationCompleted,
 *   ChannelConnected, ChannelDisconnected
 *
 * Ref: DASH-32, Architecture §6, META-05
 */
import { useEffect, useRef, useCallback } from "react";
import { useDashboardStore } from "@/stores/dashboard";
import { api } from "@/lib/api";
import type {
  WsEvent,
  SystemStatus,
  HealthResponse,

  BrainGraph,
  ConversationsResponse,
  ConversationDetailResponse,
} from "@/types/api";

/**
 * Build the default WebSocket base URL.
 *
 * Derives the scheme from the current page (`https:` → `wss:`, otherwise
 * `ws:`) so the dashboard does not silently break when served from HTTPS.
 * Can be overridden with `VITE_WS_URL`.
 */
const WS_BASE =
  import.meta.env.VITE_WS_URL ??
  `${window.location.protocol === "https:" ? "wss:" : "ws:"}//${window.location.host}`;
const MAX_BACKOFF_MS = 30_000;
const INITIAL_BACKOFF_MS = 1_000;
const STATUS_POLL_MS = 5_000;
const HEALTH_POLL_MS = 10_000;
const DEBOUNCE_MS = 300;

/** Read token for WS URL query param (WebSocket API has no custom headers). */
function getWsToken(): string {
  // Read via sessionStorage + in-memory fallback to match `@/lib/api`.
  // Never fall back to localStorage — legacy builds are migrated once in
  // `api.ts`, and keeping an explicit `localStorage` read here would
  // re-open the XSS-token-exfiltration surface we just closed.
  try {
    return window.sessionStorage?.getItem("sovyx_token") ?? "";
  } catch {
    return "";
  }
}

// ── Debounce utility (trailing edge, per-key) ──

const debounceTimers = new Map<string, ReturnType<typeof setTimeout>>();

function debouncedCall(key: string, fn: () => void, ms = DEBOUNCE_MS): void {
  const existing = debounceTimers.get(key);
  if (existing) clearTimeout(existing);
  debounceTimers.set(
    key,
    setTimeout(() => {
      debounceTimers.delete(key);
      fn();
    }, ms),
  );
}

// ── API refresh functions (via centralized api.get) ──

/** Force an immediate status poll. Useful for onboarding responsiveness. */
export async function refreshStatus(): Promise<void> {
  try {
    const data = await api.get<SystemStatus>("/api/status");
    const store = useDashboardStore.getState();
    store.setStatus(data);

    // Hydrate cost chart from persisted history (only if chart is empty)
    if (data.cost_history?.length && store.costData.length === 0) {
      const points = data.cost_history.map((h) => ({
        time: h.time,
        value: Math.round(h.cumulative * 10000) / 10000,
      }));
      useDashboardStore.setState({ costData: points });
    }
  } catch {
    // Will retry on next poll
  }
}

/** Force an immediate health poll. Useful for onboarding responsiveness. */
export async function refreshHealth(): Promise<void> {
  try {
    const data = await api.get<HealthResponse>("/api/health");
    useDashboardStore.getState().setHealthChecks(data.checks);
  } catch {
    // Will retry on next poll
  }
}

async function refreshBrain(): Promise<void> {
  try {
    const data = await api.get<BrainGraph>("/api/brain/graph?limit=200");
    useDashboardStore.getState().setBrainGraph(data);
  } catch {
    // Will retry on next event
  }
}

async function refreshActiveConversation(): Promise<void> {
  const { activeConversationId, setActiveMessages } = useDashboardStore.getState();
  if (!activeConversationId) return;
  try {
    const data = await api.get<ConversationDetailResponse>(
      `/api/conversations/${activeConversationId}`,
    );
    setActiveMessages(data.messages);
  } catch {
    // Will retry
  }
}

async function refreshTimeline(): Promise<void> {
  try {
    await useDashboardStore.getState().fetchTimeline();
  } catch {
    // Will retry on next event
  }
}

async function refreshConversationList(): Promise<void> {
  try {
    const data = await api.get<ConversationsResponse>(
      "/api/conversations?limit=50&offset=0",
    );
    useDashboardStore.getState().setConversations(data.conversations);
  } catch {
    // Will retry on next event
  }
}

// NOTE: pushEventAsLog was removed in v0.5.24 (logs-hardening TASK-08).
// WS events belong in the activity feed (recentEvents), not the logs store.
// The logs store is now fed exclusively by the /api/logs polling endpoint,
// which reads the actual daemon log file (structured JSON).

// ── Debounced wrappers (each target gets its own timer) ──

function debouncedRefreshStatus(): void {
  debouncedCall("status", () => void refreshStatus());
}

function debouncedRefreshHealth(): void {
  debouncedCall("health", () => void refreshHealth());
}

function debouncedRefreshBrain(): void {
  debouncedCall("brain", () => void refreshBrain());
}

function debouncedRefreshConversation(): void {
  debouncedCall("conversation", () => void refreshActiveConversation());
}

function debouncedRefreshConversationList(): void {
  debouncedCall("conversation-list", () => void refreshConversationList());
}

function debouncedRefreshTimeline(): void {
  debouncedCall("timeline", () => void refreshTimeline(), 5_000);
}

// ── Hook ──

export function useWebSocket(): void {
  const wsRef = useRef<WebSocket | null>(null);
  const backoffRef = useRef(INITIAL_BACKOFF_MS);
  const mountedRef = useRef(true);
  const statusTimerRef = useRef<ReturnType<typeof setInterval>>(undefined);
  const healthTimerRef = useRef<ReturnType<typeof setInterval>>(undefined);

  const setConnected = useDashboardStore((s) => s.setConnected);
  const setConnectionState = useDashboardStore((s) => s.setConnectionState);
  const addEvent = useDashboardStore((s) => s.addEvent);

  const handleMessage = useCallback(
    (raw: MessageEvent) => {
      if (raw.data === "pong") return;

      try {
        const event = JSON.parse(raw.data as string) as WsEvent;

        // All events go to activity feed (immediate, no debounce).
        // Logs are fetched separately via /api/logs polling.
        addEvent(event);

        // Targeted refreshes — DEBOUNCED to prevent API bursts
        switch (event.type) {
          case "ServiceHealthChanged":
            debouncedRefreshHealth();
            break;

          case "ThinkCompleted":
          case "ResponseSent":
            debouncedRefreshStatus();
            debouncedRefreshConversation();
            debouncedRefreshConversationList();
            debouncedRefreshTimeline();
            break;

          case "PerceptionReceived":
            debouncedRefreshConversation();
            debouncedRefreshConversationList();
            break;

          case "ConceptCreated":
          case "EpisodeEncoded":
            debouncedRefreshStatus();
            debouncedRefreshBrain();
            break;

          case "ConsolidationCompleted":
            debouncedRefreshStatus();
            debouncedRefreshBrain();
            debouncedRefreshTimeline();
            break;

          case "EngineStarted":
            // Full refresh on engine start — immediate, not debounced
            void refreshStatus();
            void refreshHealth();
            void refreshBrain();
            break;

          case "EngineStopping":
          case "ChannelConnected":
          case "ChannelDisconnected":
            debouncedRefreshStatus();
            break;

          case "PluginStateChanged":
          case "PluginAutoDisabled":
          case "PluginToolExecuted":
            useDashboardStore
              .getState()
              .handlePluginEvent(event.type, event.data as import("@/types/api").PluginStateChangedEvent);
            break;
        }
      } catch {
        // Ignore malformed messages
      }
    },
    [addEvent],
  );

  const connect = useCallback(() => {
    if (!mountedRef.current) return;

    const ws = new WebSocket(
      `${WS_BASE}/ws?token=${encodeURIComponent(getWsToken())}`,
    );
    wsRef.current = ws;

    ws.onopen = () => {
      setConnected(true);
      backoffRef.current = INITIAL_BACKOFF_MS;

      // Initial data load on (re)connect — immediate
      void refreshStatus();
      void refreshHealth();
      void refreshTimeline();
    };

    ws.onmessage = handleMessage;

    ws.onclose = () => {
      if (!mountedRef.current) {
        setConnected(false);
        return;
      }
      setConnectionState("reconnecting");

      const delay = backoffRef.current;
      backoffRef.current = Math.min(delay * 2, MAX_BACKOFF_MS);
      setTimeout(connect, delay);
    };

    ws.onerror = () => {
      ws.close();
    };
  }, [setConnected, handleMessage]);

  // Periodic polling (supplements WS events)
  useEffect(() => {
    statusTimerRef.current = setInterval(() => void refreshStatus(), STATUS_POLL_MS);
    healthTimerRef.current = setInterval(() => void refreshHealth(), HEALTH_POLL_MS);

    return () => {
      clearInterval(statusTimerRef.current);
      clearInterval(healthTimerRef.current);
    };
  }, []);

  // WebSocket lifecycle
  useEffect(() => {
    mountedRef.current = true;
    connect();

    return () => {
      mountedRef.current = false;
      wsRef.current?.close();
      for (const timer of debounceTimers.values()) clearTimeout(timer);
      debounceTimers.clear();
    };
  }, [connect]);
}
