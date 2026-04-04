/**
 * Global dashboard store — zustand
 * Single source of truth for real-time state.
 */
import { create } from "zustand";
import type {
  SystemStatus,
  HealthCheck,
  Conversation,
  LogEntry,
  WsEvent,
} from "@/types/api";

interface DashboardState {
  // ── Connection ──
  connected: boolean;
  setConnected: (v: boolean) => void;

  // ── Status ──
  status: SystemStatus | null;
  setStatus: (s: SystemStatus) => void;

  // ── Health ──
  healthChecks: HealthCheck[];
  setHealthChecks: (checks: HealthCheck[]) => void;

  // ── Conversations ──
  conversations: Conversation[];
  setConversations: (convs: Conversation[]) => void;
  activeConversationId: string | null;
  setActiveConversationId: (id: string | null) => void;

  // ── Logs ──
  logs: LogEntry[];
  addLog: (entry: LogEntry) => void;
  clearLogs: () => void;

  // ── Activity feed ──
  recentEvents: WsEvent[];
  addEvent: (event: WsEvent) => void;
}

const MAX_LOGS = 5000;
const MAX_EVENTS = 50;

export const useDashboardStore = create<DashboardState>((set) => ({
  // Connection
  connected: false,
  setConnected: (v) => set({ connected: v }),

  // Status
  status: null,
  setStatus: (s) => set({ status: s }),

  // Health
  healthChecks: [],
  setHealthChecks: (checks) => set({ healthChecks: checks }),

  // Conversations
  conversations: [],
  setConversations: (convs) => set({ conversations: convs }),
  activeConversationId: null,
  setActiveConversationId: (id) => set({ activeConversationId: id }),

  // Logs — ring buffer (keep last MAX_LOGS)
  logs: [],
  addLog: (entry) =>
    set((state) => ({
      logs:
        state.logs.length >= MAX_LOGS
          ? [...state.logs.slice(-MAX_LOGS + 1), entry]
          : [...state.logs, entry],
    })),
  clearLogs: () => set({ logs: [] }),

  // Activity — keep last MAX_EVENTS
  recentEvents: [],
  addEvent: (event) =>
    set((state) => ({
      recentEvents:
        state.recentEvents.length >= MAX_EVENTS
          ? [...state.recentEvents.slice(-MAX_EVENTS + 1), event]
          : [...state.recentEvents, event],
    })),
}));
