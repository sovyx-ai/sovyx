/**
 * Calibration slice -- Zustand store for the voice calibration wizard.
 *
 * Mission: MISSION-voice-self-calibrating-system-2026-05-05.md Layer 3
 * (v0.30.17 patch 2). Mirrors the wake-word training slice pattern
 * (stores/slices/training.ts) but for the L3 calibration wizard
 * endpoints shipped in v0.30.16:
 *
 * Endpoints consumed:
 * * POST /api/voice/calibration/start                -> {job_id, stream_url}
 * * GET  /api/voice/calibration/jobs/{id}            -> WizardJobSnapshot
 * * POST /api/voice/calibration/jobs/{id}/cancel     -> CancelJobResponse
 * * GET  /api/voice/calibration/preview-fingerprint  -> PreviewFingerprintResponse
 * * WS   /api/voice/calibration/jobs/{id}/stream     -> WizardJobSnapshot per message
 *
 * State: current job (WS-driven), preview fingerprint (one-shot),
 * loading + error.
 *
 * The WS reconnect / heartbeat handling is intentionally minimal --
 * one connection per job; on close, the panel UI reflects the
 * terminal state and the operator can retry. The slice does NOT
 * auto-reconnect -- terminal closures are FINAL.
 *
 * The token is read from ``sessionStorage.sovyx_token`` (the
 * canonical key per ``lib/api.ts:32``); never falls back to
 * localStorage (XSS hardening per CLAUDE.md auth rule).
 */
import type { StateCreator } from "zustand";

import { ApiError, BASE_URL, api } from "@/lib/api";
import type {
  CancelJobResponse,
  PreviewFingerprintResponse,
  StartCalibrationRequest,
  StartCalibrationResponse,
  WizardJobSnapshot,
} from "@/types/api";
import { isWizardCalibrationTerminal } from "@/types/api";
import {
  CancelJobResponseSchema,
  PreviewFingerprintResponseSchema,
  StartCalibrationResponseSchema,
  WizardJobSnapshotSchema,
} from "@/types/schemas";

import type { DashboardState } from "../dashboard";

export interface CalibrationSlice {
  // ── State ──
  /** Current job snapshot (driven by WS or fetchCalibrationJob). */
  currentCalibrationJob: WizardJobSnapshot | null;
  /** One-shot preview-fingerprint result. Refreshed on demand. */
  calibrationPreview: PreviewFingerprintResponse | null;
  /** True while a network call is in flight (start / preview / cancel / fetch). */
  calibrationLoading: boolean;
  /**
   * Operator-facing error string. Cleared by clearCalibrationError or
   * on the next successful action.
   */
  calibrationError: string | null;
  /** Active WebSocket. Null when no subscription is open. */
  calibrationWs: WebSocket | null;

  // ── Actions ──
  /**
   * Capture the host fingerprint via the backend's
   * ``GET /api/voice/calibration/preview-fingerprint`` and stash the
   * result in ``calibrationPreview``. Returns the response or null on
   * failure (error populated for UI surface).
   */
  fetchCalibrationPreview: () => Promise<PreviewFingerprintResponse | null>;

  /**
   * Spawn a new calibration job. Returns the job_id on HTTP 202, or
   * null on failure (HTTP 409 conflict / 401 / etc.). The error
   * message is populated for the UI to render.
   */
  startCalibration: (req: StartCalibrationRequest) => Promise<string | null>;

  /**
   * Fetch the most-recent snapshot for one job (HTTP polling
   * fallback when the WS isn't open). 404 -> currentCalibrationJob
   * is set to null + an error message is rendered.
   */
  fetchCalibrationJob: (jobId: string) => Promise<void>;

  /** Cancel a running job (touch the .cancel file). Idempotent. */
  cancelCalibrationJob: (jobId: string) => Promise<boolean>;

  /**
   * Open a WebSocket to the live progress stream. Updates
   * ``currentCalibrationJob`` on every snapshot. Closes when the
   * server emits a terminal status (done / failed / cancelled /
   * fallback). Subsequent transitions on the same job require a new
   * subscribeToCalibrationJob call.
   */
  subscribeToCalibrationJob: (jobId: string) => void;

  /** Close any active WS subscription. Idempotent. */
  unsubscribeFromCalibrationJob: () => void;

  /** Null the error field. */
  clearCalibrationError: () => void;
}

export const createCalibrationSlice: StateCreator<
  DashboardState,
  [],
  [],
  CalibrationSlice
> = (set, get) => ({
  // ── Initial state ──
  currentCalibrationJob: null,
  calibrationPreview: null,
  calibrationLoading: false,
  calibrationError: null,
  calibrationWs: null,

  // ── fetchCalibrationPreview ──
  fetchCalibrationPreview: async () => {
    set({ calibrationLoading: true, calibrationError: null });
    try {
      const data = await api.get<PreviewFingerprintResponse>(
        "/api/voice/calibration/preview-fingerprint",
        { schema: PreviewFingerprintResponseSchema },
      );
      set({ calibrationPreview: data, calibrationLoading: false });
      return data;
    } catch (err) {
      const message = _extractApiError(err, "Failed to capture fingerprint");
      set({ calibrationLoading: false, calibrationError: message });
      return null;
    }
  },

  // ── startCalibration ──
  startCalibration: async (req: StartCalibrationRequest) => {
    set({ calibrationLoading: true, calibrationError: null });
    try {
      const response = await api.post<StartCalibrationResponse>(
        "/api/voice/calibration/start",
        req,
        { schema: StartCalibrationResponseSchema },
      );
      set({ calibrationLoading: false });
      return response.job_id;
    } catch (err) {
      const message = _extractApiError(err, "Failed to start calibration");
      set({ calibrationLoading: false, calibrationError: message });
      return null;
    }
  },

  // ── fetchCalibrationJob ──
  fetchCalibrationJob: async (jobId: string) => {
    set({ calibrationLoading: true, calibrationError: null });
    try {
      const data = await api.get<WizardJobSnapshot>(
        `/api/voice/calibration/jobs/${encodeURIComponent(jobId)}`,
        { schema: WizardJobSnapshotSchema },
      );
      set({ currentCalibrationJob: data, calibrationLoading: false });
    } catch (err) {
      const message = _extractApiError(err, "Failed to load calibration job");
      set({
        calibrationLoading: false,
        calibrationError: message,
        currentCalibrationJob: null,
      });
    }
  },

  // ── cancelCalibrationJob ──
  cancelCalibrationJob: async (jobId: string) => {
    set({ calibrationError: null });
    try {
      await api.post<CancelJobResponse>(
        `/api/voice/calibration/jobs/${encodeURIComponent(jobId)}/cancel`,
        undefined,
        { schema: CancelJobResponseSchema },
      );
      return true;
    } catch (err) {
      const message = _extractApiError(err, "Failed to cancel calibration");
      set({ calibrationError: message });
      return false;
    }
  },

  // ── subscribeToCalibrationJob ──
  subscribeToCalibrationJob: (jobId: string) => {
    // Close any prior subscription first.
    get().unsubscribeFromCalibrationJob();

    // Auth token (canonical sessionStorage key per lib/api.ts:32).
    const token =
      typeof window !== "undefined"
        ? window.sessionStorage.getItem("sovyx_token") || ""
        : "";

    // Compose ws://host/path?token=... -- BASE_URL may be empty
    // (relative path), in which case fall back to window.location.
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
    const path = `/api/voice/calibration/jobs/${encodeURIComponent(jobId)}/stream`;
    const url = `${proto}//${host}${path}?token=${encodeURIComponent(token)}`;

    const ws = new WebSocket(url);
    set({ calibrationWs: ws, calibrationError: null });

    ws.onmessage = (event) => {
      let raw: unknown;
      try {
        raw = JSON.parse(event.data);
      } catch {
        return; // ignore malformed
      }
      const parsed = WizardJobSnapshotSchema.safeParse(raw);
      if (!parsed.success) {
        return;
      }
      set({ currentCalibrationJob: parsed.data });
      if (isWizardCalibrationTerminal(parsed.data.status)) {
        // Server will close the socket; we just log the terminal
        // state in currentCalibrationJob so the panel renders it.
        // The onclose handler clears calibrationWs.
      }
    };

    ws.onclose = () => {
      // Leave currentCalibrationJob populated so the panel renders
      // the terminal snapshot. Just clear the connection reference.
      set({ calibrationWs: null });
    };

    ws.onerror = () => {
      set({
        calibrationError: "Calibration WebSocket connection error",
        calibrationWs: null,
      });
    };
  },

  // ── unsubscribeFromCalibrationJob ──
  unsubscribeFromCalibrationJob: () => {
    const ws = get().calibrationWs;
    if (ws !== null) {
      try {
        ws.close();
      } catch {
        // ignore -- WS may already be closed
      }
      set({ calibrationWs: null });
    }
  },

  clearCalibrationError: () => {
    set({ calibrationError: null });
  },
});

/**
 * Extract operator-facing remediation text from a calibration
 * endpoint failure. The backend's HTTP 409 / 503 paths carry
 * actionable detail in ApiError.body.detail; surface that verbatim
 * so the dashboard can render the message without re-parsing.
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
