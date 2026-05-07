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
import i18n from "@/lib/i18n";
import type {
  CalibrationBackupListResponse,
  CalibrationFeatureFlagResponse,
  CalibrationFeatureFlagUpdateRequest,
  CalibrationRollbackResponse,
  CancelJobResponse,
  PreviewFingerprintResponse,
  StartCalibrationRequest,
  StartCalibrationResponse,
  WizardJobSnapshot,
} from "@/types/api";
import { isWizardCalibrationTerminal } from "@/types/api";
import {
  CalibrationBackupListResponseSchema,
  CalibrationFeatureFlagResponseSchema,
  CalibrationRollbackResponseSchema,
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
  /**
   * The calibration wizard mount flag, sourced from the backend's
   * ``GET /api/voice/calibration/feature-flag`` (which reflects
   * ``EngineConfig.voice.calibration_wizard_enabled`` on the running
   * daemon). Null while the slice is unloaded; populate via
   * ``loadCalibrationFeatureFlag`` on app boot. The frontend treats
   * null as "do not mount" (conservative gate).
   */
  calibrationFeatureFlag: CalibrationFeatureFlagResponse | null;
  /**
   * rc.12 — count of available rollback generations from
   * ``GET /api/voice/calibration/backups``. Null while unloaded;
   * populate via ``loadCalibrationBackups``. The RollbackButton uses
   * this to render enabled (>0) or disabled (0).
   */
  calibrationBackupCount: number | null;

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

  /**
   * Fetch the current calibration wizard mount flag from the backend.
   * Idempotent; safe to call on every app boot. Populates
   * ``calibrationFeatureFlag``; on failure, leaves the field null
   * (conservative gate; frontend treats null as "do not mount").
   */
  loadCalibrationFeatureFlag: () => Promise<CalibrationFeatureFlagResponse | null>;

  /**
   * Flip the calibration wizard mount flag in-memory on the running
   * daemon. Persists only for the daemon's lifetime; permanent
   * changes require editing env / system.yaml + restart. Returns
   * the new state on success or null on failure.
   */
  setCalibrationFeatureFlag: (
    enabled: boolean,
  ) => Promise<CalibrationFeatureFlagResponse | null>;

  /**
   * rc.12 — fetch the number of available rollback generations
   * from ``GET /api/voice/calibration/backups``. Idempotent + cheap.
   * Populates ``calibrationBackupCount``; on failure leaves the
   * field null (RollbackButton treats null as "do not render"
   * — conservative gate).
   */
  loadCalibrationBackups: () => Promise<number | null>;

  /**
   * rc.12 — POST ``/api/voice/calibration/rollback``. Restores
   * generation 1 of the chain as the current profile. Returns the
   * full response (with remaining-generations counter) on success
   * or null on failure (409 chain exhausted / 500 corrupt backup
   * / 401 / etc.) — error string populated for UI.
   */
  rollbackCalibration: () => Promise<CalibrationRollbackResponse | null>;
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
  calibrationFeatureFlag: null,
  calibrationBackupCount: null,

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
      // rc.6 (Agent 2 B.6): i18n the API fallback messages so pt-BR/es
      // operators don't see English fallbacks when the backend returns
      // an error without a `detail` field.
      const message = _extractApiError(
        err,
        i18n.t("voice:calibration.error.api_fingerprint_failed"),
      );
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
      const message = _extractApiError(
        err,
        i18n.t("voice:calibration.error.api_start_failed"),
      );
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
      const message = _extractApiError(
        err,
        i18n.t("voice:calibration.error.api_load_failed"),
      );
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
      const message = _extractApiError(
        err,
        i18n.t("voice:calibration.error.api_cancel_failed"),
      );
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
      // rc.6 (Agent 2 B.6): i18n the WS connection error.
      set({
        calibrationError: i18n.t("voice:calibration.error.ws_connection_error"),
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

  // ── loadCalibrationFeatureFlag ──
  loadCalibrationFeatureFlag: async () => {
    try {
      const data = await api.get<CalibrationFeatureFlagResponse>(
        "/api/voice/calibration/feature-flag",
        { schema: CalibrationFeatureFlagResponseSchema },
      );
      set({ calibrationFeatureFlag: data });
      return data;
    } catch (err) {
      // Conservative: on failure leave the flag null (= do not mount).
      // Don't surface as a calibrationError -- this fetch runs on
      // every app boot and a transient backend hiccup shouldn't
      // surface a banner; the operator just sees the wizard not
      // mount, falls through to the existing setup flow.
      // We DO log to the console for triage observability.
      // eslint-disable-next-line no-console
      console.warn("[calibration] feature-flag load failed:", err);
      set({ calibrationFeatureFlag: null });
      return null;
    }
  },

  // ── setCalibrationFeatureFlag ──
  setCalibrationFeatureFlag: async (enabled: boolean) => {
    set({ calibrationLoading: true, calibrationError: null });
    try {
      const body: CalibrationFeatureFlagUpdateRequest = { enabled };
      const data = await api.post<CalibrationFeatureFlagResponse>(
        "/api/voice/calibration/feature-flag",
        body,
        { schema: CalibrationFeatureFlagResponseSchema },
      );
      set({ calibrationFeatureFlag: data, calibrationLoading: false });
      return data;
    } catch (err) {
      const message = _extractApiError(err, "Failed to update feature flag");
      set({ calibrationLoading: false, calibrationError: message });
      return null;
    }
  },

  // ── loadCalibrationBackups (rc.12) ──
  loadCalibrationBackups: async () => {
    try {
      const data = await api.get<CalibrationBackupListResponse>(
        "/api/voice/calibration/backups",
        { schema: CalibrationBackupListResponseSchema },
      );
      set({ calibrationBackupCount: data.generations.length });
      return data.generations.length;
    } catch (err) {
      // Same conservative-gate pattern as loadCalibrationFeatureFlag
      // — transient backend hiccup shouldn't poison the UI; leave
      // the count null so RollbackButton stays disabled.
      // eslint-disable-next-line no-console
      console.warn("[calibration] backup-list load failed:", err);
      set({ calibrationBackupCount: null });
      return null;
    }
  },

  // ── rollbackCalibration (rc.12) ──
  rollbackCalibration: async () => {
    set({ calibrationLoading: true, calibrationError: null });
    try {
      const data = await api.post<CalibrationRollbackResponse>(
        "/api/voice/calibration/rollback",
        {},
        { schema: CalibrationRollbackResponseSchema },
      );
      set({
        calibrationLoading: false,
        calibrationBackupCount: data.backup_generations_remaining,
      });
      return data;
    } catch (err) {
      const message = _extractApiError(
        err,
        i18n.t("voice:calibration.error.api_rollback_failed"),
      );
      set({ calibrationLoading: false, calibrationError: message });
      // rc.15 LOW.2 — refresh backup count on failure. Pre-rc.15 a
      // 409 chain-exhausted left the cached count stale; the
      // RollbackButton would still render enabled (count > 0) and the
      // operator could double-click into the same 409. Now: re-fetch
      // the backup count so the UI reflects ground truth (likely 0,
      // disabling the button) immediately. Best-effort: if the
      // refresh ALSO fails, the conservative-gate path leaves the
      // count null (button disabled) which is the safe fallback.
      void get().loadCalibrationBackups();
      return null;
    }
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
