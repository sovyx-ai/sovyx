import type { StateCreator } from "zustand";
import type { LogEntry, WsEvent } from "@/types/api";
import type { DashboardState } from "../dashboard";

const MAX_LOGS = 5_000;
const MAX_EVENTS = 50;
const MAX_COST_POINTS = 288; // 24h at 5min intervals

interface CostDataPoint {
  time: number; // Unix ms
  value: number; // cumulative cost USD
}

export interface LogsSlice {
  // ── Logs ──
  logs: LogEntry[];
  addLog: (entry: LogEntry) => void;
  setLogs: (entries: LogEntry[]) => void;
  clearLogs: () => void;

  // ── Activity feed ──
  recentEvents: WsEvent[];
  addEvent: (event: WsEvent) => void;

  // ── Cost chart data (from ThinkCompleted events) ──
  costData: CostDataPoint[];
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
      const newEvents =
        state.recentEvents.length >= MAX_EVENTS
          ? [...state.recentEvents.slice(1), event]
          : [...state.recentEvents, event];

      // Accumulate cost data from ThinkCompleted events
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

  // Cost chart
  costData: [],
});
