/**
 * Activity slice — persistent cognitive timeline from /api/activity/timeline.
 *
 * Unlike the real-time WS event feed (logs slice), this fetches historical
 * data from the database so the dashboard shows activity even after refresh.
 */
import type { StateCreator } from "zustand";
import type { TimelineEntry } from "@/types/api";
import type { DashboardState } from "../dashboard";
import { api } from "@/lib/api";
import { TimelineResponseSchema } from "@/types/schemas";

interface TimelineResponse {
  entries: TimelineEntry[];
  meta: {
    hours: number;
    limit: number;
    total_before_limit: number;
    sources: Record<string, number>;
  };
}

export interface ActivitySlice {
  timelineEntries: TimelineEntry[];
  isLoadingTimeline: boolean;
  timelineError: string | null;
  fetchTimeline: (hours?: number, limit?: number) => Promise<void>;
}

export const createActivitySlice: StateCreator<
  DashboardState,
  [],
  [],
  ActivitySlice
> = (set) => ({
  timelineEntries: [],
  isLoadingTimeline: false,
  timelineError: null,

  fetchTimeline: async (hours = 24, limit = 100) => {
    set({ isLoadingTimeline: true, timelineError: null });
    try {
      const data = await api.get<TimelineResponse>(
        `/api/activity/timeline?hours=${hours}&limit=${limit}`,
        { schema: TimelineResponseSchema },
      );
      set({ timelineEntries: data.entries, isLoadingTimeline: false });
    } catch {
      set({ isLoadingTimeline: false, timelineError: "Failed to load timeline" });
    }
  },
});
