import type { StateCreator } from "zustand";
import type {
  StatsHistoryResponse,
  DailyStats,
  StatsTotals,
  StatsMonth,
} from "@/types/api";
import type { DashboardState } from "../dashboard";
import { api, isAbortError } from "@/lib/api";

export interface StatsSlice {
  statsHistory: DailyStats[];
  statsTotals: StatsTotals | null;
  statsMonth: StatsMonth | null;
  statsLoading: boolean;
  statsError: string | null;
  fetchStatsHistory: (days?: number) => Promise<void>;
}

export const createStatsSlice: StateCreator<
  DashboardState,
  [],
  [],
  StatsSlice
> = (set) => ({
  statsHistory: [],
  statsTotals: null,
  statsMonth: null,
  statsLoading: false,
  statsError: null,

  fetchStatsHistory: async (days = 30) => {
    set({ statsLoading: true, statsError: null });
    try {
      const data = await api.get<StatsHistoryResponse>(
        `/api/stats/history?days=${days}`,
      );
      set({
        statsHistory: data.days,
        statsTotals: data.totals,
        statsMonth: data.current_month,
        statsLoading: false,
      });
    } catch (err) {
      if (!isAbortError(err)) {
        set({
          statsError: err instanceof Error ? err.message : "Failed to fetch stats",
          statsLoading: false,
        });
      }
    }
  },
});
