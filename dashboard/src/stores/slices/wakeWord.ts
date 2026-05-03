/**
 * Wake-word slice — Zustand store for per-mind wake-word state.
 *
 * Mission ``MISSION-wake-word-ui-2026-05-03.md`` §T3 (D5).
 *
 * State: per-mind status list, loading/error.
 * Actions: fetchPerMindStatus (GET /api/voice/wake-word/status),
 *          toggleMind (POST /api/mind/{id}/wake-word/toggle with
 *          optimistic update + 422/500 rollback).
 *
 * Optimistic-update contract (D4 of the mission):
 *   1. On toggle click, immediately update the matching entry's
 *      ``wake_word_enabled`` to the target value (UI reflects the
 *      click instantly).
 *   2. Fire POST. On 200, refetch the full list to reconcile
 *      ``runtime_registered`` + ``model_path`` + ``last_error``
 *      (which the backend may have updated as part of hot-apply).
 *   3. On 422 (NONE strategy) or 500: rollback the optimistic
 *      update + populate ``error`` with the response detail.
 *
 * The slice has its own fetch lifecycle (NOT triggered on global
 * dashboard tick) — wake-word UI consumers call ``fetchPerMindStatus``
 * on mount + after each toggle reconciliation.
 */
import type { StateCreator } from "zustand";

import { api, ApiError } from "@/lib/api";
import type {
  WakeWordPerMindStatus,
  WakeWordPerMindStatusResponse,
  WakeWordToggleResponse,
} from "@/types/api";
import {
  WakeWordPerMindStatusResponseSchema,
  WakeWordToggleResponseSchema,
} from "@/types/schemas";

import type { DashboardState } from "../dashboard";

export interface WakeWordSlice {
  // ── State ──
  perMindStatus: WakeWordPerMindStatus[];
  wakeWordLoading: boolean;
  wakeWordError: string | null;

  // ── Actions ──
  fetchPerMindStatus: () => Promise<void>;
  toggleMind: (mindId: string, enabled: boolean) => Promise<void>;
  clearWakeWordError: () => void;
}

export const createWakeWordSlice: StateCreator<
  DashboardState,
  [],
  [],
  WakeWordSlice
> = (set, get) => ({
  // ── Initial state ──
  perMindStatus: [],
  wakeWordLoading: false,
  wakeWordError: null,

  // ── Fetch per-mind status ──
  fetchPerMindStatus: async () => {
    set({ wakeWordLoading: true, wakeWordError: null });
    try {
      const data = await api.get<WakeWordPerMindStatusResponse>(
        "/api/voice/wake-word/status",
        { schema: WakeWordPerMindStatusResponseSchema },
      );
      set({ perMindStatus: data.minds, wakeWordLoading: false });
    } catch (err) {
      const message =
        err instanceof Error
          ? err.message
          : "Failed to load per-mind wake-word status";
      set({ wakeWordLoading: false, wakeWordError: message });
    }
  },

  // ── Toggle mind (optimistic + rollback) ──
  toggleMind: async (mindId: string, enabled: boolean) => {
    const prev = get().perMindStatus;
    const targetEntry = prev.find((entry) => entry.mind_id === mindId);
    if (!targetEntry) {
      set({ wakeWordError: `Unknown mind: ${mindId}` });
      return;
    }

    // ── 1. Optimistic update ──
    set({
      perMindStatus: prev.map((entry) =>
        entry.mind_id === mindId
          ? { ...entry, wake_word_enabled: enabled }
          : entry,
      ),
      wakeWordError: null,
    });

    // ── 2. Fire toggle ──
    try {
      await api.post<WakeWordToggleResponse>(
        `/api/mind/${encodeURIComponent(mindId)}/wake-word/toggle`,
        { enabled },
        { schema: WakeWordToggleResponseSchema },
      );
      // 3a. Success — refetch to reconcile runtime_registered + model_path.
      await get().fetchPerMindStatus();
    } catch (err) {
      // 3b. Rollback optimistic update; surface remediation message.
      const message = _extractToggleError(err);
      set({ perMindStatus: prev, wakeWordError: message });
    }
  },

  clearWakeWordError: () => {
    set({ wakeWordError: null });
  },
});

/**
 * Extract operator-facing remediation text from a toggle failure.
 *
 * The backend's HTTP 422 path carries the resolver's full remediation
 * message in the ``detail`` field — we surface that directly (no
 * translation drift). HTTP 500 falls back to the exception message
 * (server-side error). Other errors get a generic "couldn't toggle"
 * fallback so the dashboard always has SOMETHING to display.
 */
function _extractToggleError(err: unknown): string {
  if (err instanceof ApiError) {
    // ApiError exposes the parsed response body via ``body`` when the
    // server returned JSON. The ``detail`` field is the pydantic-style
    // error message; preserve it verbatim so operators see the
    // resolver's full remediation text (T1 of v0.28.3 returned the
    // resolver's full remediation directly in the 422 detail).
    const detail = err.body?.detail;
    if (typeof detail === "string" && detail.length > 0) {
      return detail;
    }
    return err.message;
  }
  if (err instanceof Error) {
    return err.message;
  }
  return "Failed to toggle wake word — please retry";
}
