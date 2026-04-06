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
import type { WsEvent, SystemStatus, HealthResponse, LogEntry, BrainGraph, Message } from "@/types/api";

const API_BASE = import.meta.env.VITE_API_URL ?? "";
const WS_BASE = import.meta.env.VITE_WS_URL ?? `ws://${window.location.host}`;
const MAX_BACKOFF_MS = 30_000;
const INITIAL_BACKOFF_MS = 1_000;
const STATUS_POLL_MS = 5_000;
const HEALTH_POLL_MS = 10_000;
const DEBOUNCE_MS = 300;

function getToken(): string {
  return localStorage.getItem("sovyx_token") ?? "";
}

function authHeaders(): HeadersInit {
  return { Authorization: `Bearer ${getToken()}` };
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

// ── API refresh functions ──

async function refreshStatus(): Promise<void> {
  try {
    const res = await fetch(`${API_BASE}/api/status`, { headers: authHeaders() });
    if (res.ok) {
      const data = (await res.json()) as SystemStatus;
      useDashboardStore.getState().setStatus(data);
    }
  } catch {
    // Will retry on next poll
  }
}

async function refreshHealth(): Promise<void> {
  try {
    const res = await fetch(`${API_BASE}/api/health`, { headers: authHeaders() });
    if (res.ok) {
      const data = (await res.json()) as HealthResponse;
      useDashboardStore.getState().setHealthChecks(data.checks);
    }
  } catch {
    // Will retry on next poll
  }
}

async function refreshBrain(): Promise<void> {
  try {
    const res = await fetch(`${API_BASE}/api/brain/graph?limit=200`, { headers: authHeaders() });
    if (res.ok) {
      const data = (await res.json()) as BrainGraph;
      useDashboardStore.getState().setBrainGraph(data);
    }
  } catch {
    // Will retry on next event
  }
}

async function refreshActiveConversation(): Promise<void> {
  const { activeConversationId, setActiveMessages } = useDashboardStore.getState();
  if (!activeConversationId) return;
  try {
    const res = await fetch(
      `${API_BASE}/api/conversations/${activeConversationId}`,
      { headers: authHeaders() },
    );
    if (res.ok) {
      const data = (await res.json()) as { conversation_id: string; messages: Message[] };
      setActiveMessages(data.messages);
    }
  } catch {
    // Will retry
  }
}

/** Push a WS event as a log entry to the store. */
function pushEventAsLog(event: WsEvent): void {
  const entry: LogEntry = {
    timestamp: event.timestamp,
    level: "INFO",
    logger: "sovyx.dashboard.events",
    event: `[${event.type}] ${event.data ? JSON.stringify(event.data) : ""}`.slice(0, 500),
  };
  useDashboardStore.getState().addLog(entry);
}

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

        // All events go to activity feed + logs (immediate, no debounce)
        addEvent(event);
        pushEventAsLog(event);

        // Targeted refreshes — DEBOUNCED to prevent API bursts
        switch (event.type) {
          case "ServiceHealthChanged":
            debouncedRefreshHealth();
            break;

          case "ThinkCompleted":
          case "ResponseSent":
            debouncedRefreshStatus();
            debouncedRefreshConversation();
            break;

          case "PerceptionReceived":
            debouncedRefreshConversation();
            break;

          case "ConceptCreated":
          case "EpisodeEncoded":
            debouncedRefreshStatus();
            debouncedRefreshBrain();
            break;

          case "ConsolidationCompleted":
            debouncedRefreshStatus();
            debouncedRefreshBrain();
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
      `${WS_BASE}/ws?token=${encodeURIComponent(getToken())}`,
    );
    wsRef.current = ws;

    ws.onopen = () => {
      setConnected(true);
      backoffRef.current = INITIAL_BACKOFF_MS;

      // Initial data load on (re)connect — immediate
      void refreshStatus();
      void refreshHealth();
    };

    ws.onmessage = handleMessage;

    ws.onclose = () => {
      if (!mountedRef.current) {
        setConnected(false);
        return;
      }
      // Show "reconnecting" state instead of just "disconnected"
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
      // Clean up debounce timers
      for (const timer of debounceTimers.values()) clearTimeout(timer);
      debounceTimers.clear();
    };
  }, [connect]);
}
