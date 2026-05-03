/**
 * Training slice — Zustand store for wake-word training jobs.
 *
 * Mission ``MISSION-v0.30.0-single-mind-ga-2026-05-03.md`` §T1.3 (D5).
 *
 * State: job list (poll-based), current-job detail (WebSocket-based),
 * loading + error.
 *
 * Actions:
 * * ``fetchJobs()`` — GET /api/voice/training/jobs (list).
 * * ``fetchJobDetail(jobId)`` — GET /api/voice/training/jobs/{id}.
 * * ``startTraining(req)`` — POST /api/voice/training/jobs/start
 *   (returns the new job_id).
 * * ``cancelJob(jobId)`` — POST /api/voice/training/jobs/{id}/cancel.
 * * ``subscribeToJob(jobId)`` — open WebSocket to live progress
 *   stream; updates ``currentJob`` on each snapshot; closes on
 *   terminal state OR error.
 * * ``unsubscribeFromJob()`` — close any active WS.
 *
 * The WebSocket reconnect / heartbeat handling is intentionally
 * minimal (one connection per job; on close, the panel UI reflects
 * the terminal/error state and the operator can navigate away). The
 * slice does NOT auto-reconnect — terminal closures are FINAL.
 */
import type { StateCreator } from "zustand";

import { api, ApiError, BASE_URL } from "@/lib/api";
import type {
  CancelJobResponse,
  StartTrainingRequest,
  StartTrainingResponse,
  TrainingJobDetailResponse,
  TrainingJobStreamMessage,
  TrainingJobSummary,
  TrainingJobsResponse,
} from "@/types/api";
import {
  CancelJobResponseSchema,
  StartTrainingResponseSchema,
  TrainingJobDetailResponseSchema,
  TrainingJobStreamMessageSchema,
  TrainingJobsResponseSchema,
} from "@/types/schemas";

import type { DashboardState } from "../dashboard";

export interface TrainingSlice {
  // ── State ──
  trainingJobs: TrainingJobSummary[];
  currentTrainingJob: TrainingJobDetailResponse | null;
  trainingLoading: boolean;
  trainingError: string | null;
  /** Active WebSocket connection. Null when no subscription is open. */
  trainingWs: WebSocket | null;

  // ── Actions ──
  fetchTrainingJobs: () => Promise<void>;
  fetchTrainingJobDetail: (jobId: string) => Promise<void>;
  startTraining: (req: StartTrainingRequest) => Promise<string | null>;
  cancelTrainingJob: (jobId: string) => Promise<boolean>;
  subscribeToTrainingJob: (jobId: string) => void;
  unsubscribeFromTrainingJob: () => void;
  clearTrainingError: () => void;
}

export const createTrainingSlice: StateCreator<
  DashboardState,
  [],
  [],
  TrainingSlice
> = (set, get) => ({
  // ── Initial state ──
  trainingJobs: [],
  currentTrainingJob: null,
  trainingLoading: false,
  trainingError: null,
  trainingWs: null,

  // ── fetchTrainingJobs ──
  fetchTrainingJobs: async () => {
    set({ trainingLoading: true, trainingError: null });
    try {
      const data = await api.get<TrainingJobsResponse>(
        "/api/voice/training/jobs",
        { schema: TrainingJobsResponseSchema },
      );
      set({ trainingJobs: data.jobs, trainingLoading: false });
    } catch (err) {
      const message = err instanceof Error ? err.message : "Failed to load training jobs";
      set({ trainingLoading: false, trainingError: message });
    }
  },

  // ── fetchTrainingJobDetail ──
  fetchTrainingJobDetail: async (jobId: string) => {
    set({ trainingLoading: true, trainingError: null });
    try {
      const data = await api.get<TrainingJobDetailResponse>(
        `/api/voice/training/jobs/${encodeURIComponent(jobId)}`,
        { schema: TrainingJobDetailResponseSchema },
      );
      set({ currentTrainingJob: data, trainingLoading: false });
    } catch (err) {
      const message = err instanceof Error ? err.message : "Failed to load job detail";
      set({ trainingLoading: false, trainingError: message });
    }
  },

  // ── startTraining ──
  // Per Mission §T1.1 D1: returns the new job_id on 202 Accepted.
  // Returns null on failure (error populated for UI surface).
  startTraining: async (req: StartTrainingRequest) => {
    set({ trainingError: null });
    try {
      const response = await api.post<StartTrainingResponse>(
        "/api/voice/training/jobs/start",
        req,
        { schema: StartTrainingResponseSchema },
      );
      // Refetch the full list so the new job appears in the panel.
      await get().fetchTrainingJobs();
      return response.job_id;
    } catch (err) {
      const message = _extractApiError(err, "Failed to start training");
      set({ trainingError: message });
      return null;
    }
  },

  // ── cancelTrainingJob ──
  cancelTrainingJob: async (jobId: string) => {
    set({ trainingError: null });
    try {
      await api.post<CancelJobResponse>(
        `/api/voice/training/jobs/${encodeURIComponent(jobId)}/cancel`,
        undefined,
        { schema: CancelJobResponseSchema },
      );
      // Refetch so the job's cancelled_signalled flips True in the UI.
      await get().fetchTrainingJobs();
      return true;
    } catch (err) {
      const message = _extractApiError(err, "Failed to cancel training");
      set({ trainingError: message });
      return false;
    }
  },

  // ── subscribeToTrainingJob ──
  // Opens a WebSocket to the live progress stream. On each snapshot,
  // updates ``currentTrainingJob`` (re-fetches the detail synchronously
  // from the snapshot's state dict). On terminal status, the server
  // closes the socket cleanly and we leave the terminal state in place
  // so the panel renders the final snapshot.
  subscribeToTrainingJob: (jobId: string) => {
    // Close any prior subscription first.
    get().unsubscribeFromTrainingJob();

    // Resolve auth token from sessionStorage (in-memory fallback per
    // CLAUDE.md auth rule). If absent, the WS handshake will fail
    // with 4401 and the slice surfaces a sensible error.
    const token =
      typeof window !== "undefined"
        ? window.sessionStorage.getItem("sovyx_auth_token") || ""
        : "";

    // Construct ws:// or wss:// URL. BASE_URL may be empty (relative
    // path); in that case use window.location.host.
    const proto =
      typeof window !== "undefined" && window.location.protocol === "https:"
        ? "wss:"
        : "ws:";
    const host =
      BASE_URL && BASE_URL.length > 0
        ? BASE_URL.replace(/^https?:/, "").replace(/^\/+/, "")
        : typeof window !== "undefined"
          ? window.location.host
          : "";
    const path = `/api/voice/training/jobs/${encodeURIComponent(jobId)}/stream`;
    const url = `${proto}//${host}${path}?token=${encodeURIComponent(token)}`;

    const ws = new WebSocket(url);
    set({ trainingWs: ws, trainingError: null });

    ws.onmessage = (event) => {
      let raw: unknown;
      try {
        raw = JSON.parse(event.data);
      } catch {
        return; // ignore malformed messages
      }
      const parsed = TrainingJobStreamMessageSchema.safeParse(raw);
      if (!parsed.success) {
        return;
      }
      const msg: TrainingJobStreamMessage = parsed.data;
      if (msg.type === "error") {
        set({ trainingError: msg.message });
        return;
      }
      // Snapshot or terminal: update currentTrainingJob's summary
      // from the state dict. We synthesize a TrainingJobDetailResponse
      // shape so consumers get the same view as fetchTrainingJobDetail.
      // The state dict's keys map 1:1 to TrainingJobSummary fields
      // (the backend's TrainingJobState.to_dict serialises this way).
      const stateDict = msg.state as Record<string, string | number>;
      const summary: TrainingJobSummary = {
        job_id: jobId,
        wake_word: String(stateDict["wake_word"] ?? ""),
        mind_id: String(stateDict["mind_id"] ?? ""),
        language: String(stateDict["language"] ?? ""),
        status: String(stateDict["status"] ?? "") as TrainingJobSummary["status"],
        progress: Number(stateDict["progress"] ?? 0),
        samples_generated: Number(stateDict["samples_generated"] ?? 0),
        target_samples: Number(stateDict["target_samples"] ?? 0),
        started_at: String(stateDict["started_at"] ?? ""),
        updated_at: String(stateDict["updated_at"] ?? ""),
        completed_at: String(stateDict["completed_at"] ?? ""),
        output_path: String(stateDict["output_path"] ?? ""),
        error_summary: String(stateDict["error_summary"] ?? ""),
        cancelled_signalled: Boolean(stateDict["cancelled_signalled"] ?? false),
      };
      set({
        currentTrainingJob: {
          summary,
          history: get().currentTrainingJob?.history ?? [],
          history_truncated: false,
        },
      });
    };

    ws.onclose = () => {
      // Leave currentTrainingJob populated so the panel renders the
      // terminal snapshot. Just clear the connection reference.
      set({ trainingWs: null });
    };

    ws.onerror = () => {
      set({
        trainingError: "WebSocket connection error",
        trainingWs: null,
      });
    };
  },

  // ── unsubscribeFromTrainingJob ──
  unsubscribeFromTrainingJob: () => {
    const ws = get().trainingWs;
    if (ws !== null) {
      try {
        ws.close();
      } catch {
        // ignore — WS may already be closed
      }
      set({ trainingWs: null });
    }
  },

  clearTrainingError: () => {
    set({ trainingError: null });
  },
});

/**
 * Extract operator-facing remediation text from a training endpoint
 * failure. The backend's HTTP 503 / 409 paths carry actionable detail
 * in ``ApiError.body.detail``; surface that verbatim.
 */
function _extractApiError(err: unknown, fallback: string): string {
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
