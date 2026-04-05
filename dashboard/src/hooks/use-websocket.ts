/**
 * WebSocket hook with auto-reconnect (exponential backoff).
 * Dispatches events to the zustand store.
 */
import { useEffect, useRef, useCallback } from "react";
import { useDashboardStore } from "@/stores/dashboard";
import type { WsEvent, SystemStatus } from "@/types/api";

const WS_BASE = import.meta.env.VITE_WS_URL ?? `ws://${window.location.host}`;
const MAX_BACKOFF_MS = 30_000;
const INITIAL_BACKOFF_MS = 1_000;

export function useWebSocket(): void {
  const wsRef = useRef<WebSocket | null>(null);
  const backoffRef = useRef(INITIAL_BACKOFF_MS);
  const mountedRef = useRef(true);

  const {
    setConnected,
    setStatus,
    setHealthChecks,
    addLog,
    addEvent,
  } = useDashboardStore();

  const handleMessage = useCallback(
    (raw: MessageEvent) => {
      try {
        const event = JSON.parse(raw.data as string) as WsEvent;
        addEvent(event);

        switch (event.type) {
          case "ServiceHealthChanged":
            // Trigger a health refresh on next poll cycle
            break;
          case "ThinkCompleted":
            // LLM cost update — status refresh will pick it up
            break;
          default:
            break;
        }
      } catch {
        // Ignore malformed messages
      }
    },
    [addEvent, addLog, setHealthChecks],
  );

  const connect = useCallback(() => {
    if (!mountedRef.current) return;

    const token = localStorage.getItem("sovyx_token") ?? "";
    const ws = new WebSocket(`${WS_BASE}/ws?token=${encodeURIComponent(token)}`);
    wsRef.current = ws;

    ws.onopen = () => {
      setConnected(true);
      backoffRef.current = INITIAL_BACKOFF_MS;
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

  // Fetch initial status on connect
  const connected = useDashboardStore((s) => s.connected);
  const prevConnected = useRef(false);

  useEffect(() => {
    if (connected && !prevConnected.current) {
      fetch(`${import.meta.env.VITE_API_URL ?? ""}/api/status`, {
        headers: {
          Authorization: `Bearer ${localStorage.getItem("sovyx_token") ?? ""}`,
        },
      })
        .then((r) => r.json())
        .then((data: SystemStatus) => setStatus(data))
        .catch(() => {
          /* will retry on next reconnect */
        });
    }
    prevConnected.current = connected;
  }, [connected, setStatus]);

  useEffect(() => {
    mountedRef.current = true;
    connect();

    return () => {
      mountedRef.current = false;
      wsRef.current?.close();
    };
  }, [connect]);
}
