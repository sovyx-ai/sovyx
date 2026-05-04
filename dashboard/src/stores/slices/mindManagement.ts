/**
 * Mind-management slice — Zustand store for per-mind destructive +
 * retention operations.
 *
 * Mission ``MISSION-claude-autonomous-batch-2026-05-03.md`` §Phase 2 (D3).
 *
 * Backed by two endpoints (Phase 8 / T8.21):
 *   POST /api/mind/{id}/forget          (right-to-erasure; requires
 *                                        ``confirm: <mind_id>`` typed
 *                                        verbatim per backend defense-
 *                                        in-depth)
 *   POST /api/mind/{id}/retention/prune (time-based scheduled-policy
 *                                        prune; only removes AGED
 *                                        records, no confirm needed)
 *
 * State is per-mind keyed (Record<string, T>) — multiple minds may be
 * mid-flight simultaneously, and the cards mount one per mind so each
 * needs to track its own pending / report / error independently.
 *
 * Pessimistic update contract:
 *   Forget is destructive — no optimistic UI; the operator only sees
 *   the report after the server confirms. Retention prune is also
 *   pessimistic because the report's ``effective_horizons`` map is
 *   server-computed (operator can't predict it locally).
 *
 * Per ``feedback_validation_batching``, the cards that consume this
 * slice are flagged for D22 batch browser validation rather than
 * blocking the slice ship on synchronous UX review.
 */
import type { StateCreator } from "zustand";

import { api, ApiError } from "@/lib/api";
import type {
  ForgetMindResponse,
  PruneRetentionResponse,
} from "@/types/api";
import {
  ForgetMindResponseSchema,
  PruneRetentionResponseSchema,
} from "@/types/schemas";

import type { DashboardState } from "../dashboard";

export interface MindManagementSlice {
  // ── State (per-mind, keyed by mind_id) ──
  forgetReports: Record<string, ForgetMindResponse | null>;
  forgetPending: Record<string, boolean>;
  forgetErrors: Record<string, string | null>;

  retentionReports: Record<string, PruneRetentionResponse | null>;
  retentionPending: Record<string, boolean>;
  retentionErrors: Record<string, string | null>;

  // ── Actions ──
  forgetMind: (
    mindId: string,
    opts: { confirm: string; dryRun?: boolean },
  ) => Promise<void>;
  pruneRetention: (
    mindId: string,
    opts: { dryRun?: boolean },
  ) => Promise<void>;

  clearForgetReport: (mindId: string) => void;
  clearForgetError: (mindId: string) => void;
  clearRetentionReport: (mindId: string) => void;
  clearRetentionError: (mindId: string) => void;
}

export const createMindManagementSlice: StateCreator<
  DashboardState,
  [],
  [],
  MindManagementSlice
> = (set) => ({
  // ── Initial state ──
  forgetReports: {},
  forgetPending: {},
  forgetErrors: {},
  retentionReports: {},
  retentionPending: {},
  retentionErrors: {},

  // ── Forget (destructive) ──
  forgetMind: async (mindId, { confirm, dryRun = false }) => {
    set((state) => ({
      forgetPending: { ...state.forgetPending, [mindId]: true },
      forgetErrors: { ...state.forgetErrors, [mindId]: null },
    }));
    try {
      const data = await api.post<ForgetMindResponse>(
        `/api/mind/${encodeURIComponent(mindId)}/forget`,
        { confirm, dry_run: dryRun },
        { schema: ForgetMindResponseSchema },
      );
      set((state) => ({
        forgetReports: { ...state.forgetReports, [mindId]: data },
        forgetPending: { ...state.forgetPending, [mindId]: false },
      }));
    } catch (err) {
      const message = _extractErrorMessage(err, "Failed to forget mind");
      set((state) => ({
        forgetPending: { ...state.forgetPending, [mindId]: false },
        forgetErrors: { ...state.forgetErrors, [mindId]: message },
      }));
    }
  },

  // ── Retention prune (scheduled-policy, less destructive) ──
  pruneRetention: async (mindId, { dryRun = false }) => {
    set((state) => ({
      retentionPending: { ...state.retentionPending, [mindId]: true },
      retentionErrors: { ...state.retentionErrors, [mindId]: null },
    }));
    try {
      const data = await api.post<PruneRetentionResponse>(
        `/api/mind/${encodeURIComponent(mindId)}/retention/prune`,
        { dry_run: dryRun },
        { schema: PruneRetentionResponseSchema },
      );
      set((state) => ({
        retentionReports: { ...state.retentionReports, [mindId]: data },
        retentionPending: { ...state.retentionPending, [mindId]: false },
      }));
    } catch (err) {
      const message = _extractErrorMessage(err, "Failed to prune mind");
      set((state) => ({
        retentionPending: { ...state.retentionPending, [mindId]: false },
        retentionErrors: { ...state.retentionErrors, [mindId]: message },
      }));
    }
  },

  clearForgetReport: (mindId) => {
    set((state) => ({
      forgetReports: { ...state.forgetReports, [mindId]: null },
    }));
  },
  clearForgetError: (mindId) => {
    set((state) => ({
      forgetErrors: { ...state.forgetErrors, [mindId]: null },
    }));
  },
  clearRetentionReport: (mindId) => {
    set((state) => ({
      retentionReports: { ...state.retentionReports, [mindId]: null },
    }));
  },
  clearRetentionError: (mindId) => {
    set((state) => ({
      retentionErrors: { ...state.retentionErrors, [mindId]: null },
    }));
  },
});

/**
 * Extract operator-facing message from a mutation failure. Mirrors the
 * wake-word slice helper: prefer the parsed ``detail`` field from
 * :class:`ApiError`'s body (which the backend uses for actionable
 * remediation text), then fall through to the exception message, then
 * to a localized fallback. Keeps the resolver's full text intact —
 * never paraphrase backend remediation.
 */
function _extractErrorMessage(err: unknown, fallback: string): string {
  if (err instanceof ApiError) {
    const detail = err.body?.detail;
    if (typeof detail === "string" && detail.length > 0) {
      return detail;
    }
    return err.message;
  }
  if (err instanceof Error) {
    return err.message;
  }
  return fallback;
}
