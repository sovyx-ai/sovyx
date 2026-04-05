/**
 * WebSocket hook with auto-reconnect (exponential backoff).
 *
 * FE-00b: Rewritten to handle all 11 real event types from
 * DashboardEventBridge._serialize_event(). Events are:
 *
 *   EngineStarted, EngineStopping, ServiceHealthChanged,
 *   PerceptionReceived, ThinkCompleted, ResponseSent,
 *   ConceptCreated, EpisodeEncoded, ConsolidationCompleted,
 *   ChannelConnected, ChannelDisconnected
 *
 * All events go to the activity feed. Specific events trigger
 * targeted refreshes (health, status).
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

function getToken(): string {
  return localStorage.getItem("sovyx_token") ?? "";
}

function authHeaders(): HeadersInit {
  return { Authorization: `Bearer ${getToken()}` };
}

/** Fetch status from REST API and update store. */
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

/** Fetch health checks from REST API and update store. */
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

/** Refresh brain graph from API. */
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

/** Refresh active conversation messages if one is selected. */
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

export function useWebSocket(): void {
  const wsRef = useRef<WebSocket | null>(null);
  const backoffRef = useRef(INITIAL_BACKOFF_MS);
  const mountedRef = useRef(true);
  const statusTimerRef = useRef<ReturnType<typeof setInterval>>(undefined);
  const healthTimerRef = useRef<ReturnType<typeof setInterval>>(undefined);

  const setConnected = useDashboardStore((s) => s.setConnected);
  const addEvent = useDashboardStore((s) => s.addEvent);

  const handleMessage = useCallback(
    (raw: MessageEvent) => {
      // Ignore pong responses
      if (raw.data === "pong") return;

      try {
        const event = JSON.parse(raw.data as string) as WsEvent;

        // All events go to activity feed + logs
        addEvent(event);
        pushEventAsLog(event);

        // Targeted refreshes for specific events
        switch (event.type) {
          case "ServiceHealthChanged":
            void refreshHealth();
            break;

          case "ThinkCompleted":
          case "ResponseSent":
            // LLM cost/tokens changed + new message in conversation
            void refreshStatus();
            void refreshActiveConversation();
            break;

          case "PerceptionReceived":
            // New user message — refresh active conversation
            void refreshActiveConversation();
            break;

          case "ConceptCreated":
          case "EpisodeEncoded":
            // Brain changed — refresh status + brain graph
            void refreshStatus();
            void refreshBrain();
            break;

          case "ConsolidationCompleted":
            // Major brain change — refresh all brain data
            void refreshStatus();
            void refreshBrain();
            break;

          case "EngineStarted":
            // Full refresh on engine start
            void refreshStatus();
            void refreshHealth();
            void refreshBrain();
            break;

          case "EngineStopping":
          case "ChannelConnected":
          case "ChannelDisconnected":
            // Activity-only events — status refresh for channel count
            void refreshStatus();
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

      // Initial data load on (re)connect
      void refreshStatus();
      void refreshHealth();
    };

    ws.onmessage = handleMessage;

    ws.onclose = () => {
      setConnected(false);
      if (!mountedRef.current) return;

      // Exponential backoff reconnect
      const delay = backoffRef.current;
      backoffRef.current = Math.min(delay * 2, MAX_BACKOFF_MS);
      setTimeout(connect, delay);
    };

    ws.onerror = () => {
      ws.close();
    };
  }, [setConnected, handleMessage]);

  // Periodic polling for status and health (supplements WebSocket events)
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
    };
  }, [connect]);
}
