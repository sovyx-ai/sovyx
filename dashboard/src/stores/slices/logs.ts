import type { StateCreator } from "zustand";
import type { LogEntry, WsEvent } from "@/types/api";
import type { DashboardState } from "../dashboard";

const MAX_LOGS = 5_000;
const MAX_EVENTS = 50;

export interface LogsSlice {
  // ── Logs ──
  logs: LogEntry[];
  addLog: (entry: LogEntry) => void;
  setLogs: (entries: LogEntry[]) => void;
  clearLogs: () => void;

  // ── Activity feed ──
  recentEvents: WsEvent[];
  addEvent: (event: WsEvent) => void;
}

export const createLogsSlice: StateCreator<
  DashboardState,
  [],
  [],
  LogsSlice
> = (set) => ({
  // Logs — ring buffer
  logs: [],
  addLog: (entry) =>
    set((state) => {
      if (state.logs.length >= MAX_LOGS) {
        // Slice from 10% in to avoid O(n) on every append
        const trimmed = state.logs.slice(Math.floor(MAX_LOGS * 0.1));
        trimmed.push(entry);
        return { logs: trimmed };
      }
      return { logs: [...state.logs, entry] };
    }),
  setLogs: (entries) => set({ logs: entries }),
  clearLogs: () => set({ logs: [] }),

  // Activity — keep last MAX_EVENTS
  recentEvents: [],
  addEvent: (event) =>
    set((state) => {
      if (state.recentEvents.length >= MAX_EVENTS) {
        const trimmed = state.recentEvents.slice(1);
        trimmed.push(event);
        return { recentEvents: trimmed };
      }
      return { recentEvents: [...state.recentEvents, event] };
    }),
});
