/**
 * Calibration slice tests -- v0.30.17 patch 5 of
 * MISSION-voice-self-calibrating-system-2026-05-05.md Layer 3.
 *
 * Validates: initial state, clearCalibrationError,
 * fetchCalibrationPreview (success + error), startCalibration
 * (202 + 409 conflict + 503), fetchCalibrationJob (success + 404),
 * cancelCalibrationJob (success + error).
 *
 * subscribeToCalibrationJob is NOT tested at this level -- jsdom's
 * WebSocket mock has limitations that the wake-word training slice
 * also hits; that flow is covered by the v0.30.18 E2E integration
 * test (mission §6.6.T3.7).
 */
import { beforeEach, describe, expect, it, vi } from "vitest";

import { ApiError } from "@/lib/api";
import type { WizardJobSnapshot } from "@/types/api";

import { useDashboardStore } from "../dashboard";

const HEALTHY_SNAPSHOT: WizardJobSnapshot = {
  job_id: "default",
  mind_id: "default",
  status: "probing",
  progress: 0.05,
  current_stage_message: "Capturing hardware fingerprint",
  created_at_utc: "2026-05-06T18:00:00Z",
  updated_at_utc: "2026-05-06T18:00:01Z",
  profile_path: null,
  triage_winner_hid: null,
  error_summary: null,
  fallback_reason: null,
};

function _resetCalibrationState() {
  useDashboardStore.setState({
    currentCalibrationJob: null,
    calibrationPreview: null,
    calibrationLoading: false,
    calibrationError: null,
    calibrationWs: null,
  });
}

beforeEach(() => {
  _resetCalibrationState();
  vi.restoreAllMocks();
});

// ── Initial state ────────────────────────────────────────────────

describe("calibration slice — initial state", () => {
  it("starts with empty preview and no error", () => {
    const state = useDashboardStore.getState();
    expect(state.currentCalibrationJob).toBeNull();
    expect(state.calibrationPreview).toBeNull();
    expect(state.calibrationLoading).toBe(false);
    expect(state.calibrationError).toBeNull();
    expect(state.calibrationWs).toBeNull();
  });
});

// ── clearCalibrationError ───────────────────────────────────────

describe("calibration slice — clearCalibrationError", () => {
  it("clears the error field", () => {
    useDashboardStore.setState({ calibrationError: "boom" });
    useDashboardStore.getState().clearCalibrationError();
    expect(useDashboardStore.getState().calibrationError).toBeNull();
  });
});

// ── fetchCalibrationPreview ─────────────────────────────────────

describe("calibration slice — fetchCalibrationPreview", () => {
  it("populates preview on success", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValueOnce({
      ok: true,
      json: () =>
        Promise.resolve({
          fingerprint_hash: "a".repeat(64),
          audio_stack: "pipewire",
          system_vendor: "Sony",
          system_product: "VAIO",
          recommendation: "slow_path",
        }),
    } as Response);

    const result = await useDashboardStore.getState().fetchCalibrationPreview();

    expect(result).not.toBeNull();
    expect(result?.audio_stack).toBe("pipewire");
    const state = useDashboardStore.getState();
    expect(state.calibrationPreview?.system_vendor).toBe("Sony");
    expect(state.calibrationLoading).toBe(false);
    expect(state.calibrationError).toBeNull();
  });

  it("sets error on network failure + returns null", async () => {
    vi.spyOn(globalThis, "fetch").mockRejectedValue(new Error("Network down"));

    const result = await useDashboardStore.getState().fetchCalibrationPreview();

    expect(result).toBeNull();
    const state = useDashboardStore.getState();
    expect(state.calibrationLoading).toBe(false);
    expect(state.calibrationError).toContain("Network");
  });
});

// ── startCalibration ────────────────────────────────────────────

describe("calibration slice — startCalibration", () => {
  it("returns the new job_id on HTTP 202", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValueOnce({
      ok: true,
      json: () =>
        Promise.resolve({
          job_id: "default",
          stream_url: "/api/voice/calibration/jobs/default/stream",
        }),
    } as Response);

    const jobId = await useDashboardStore
      .getState()
      .startCalibration({ mind_id: "default" });

    expect(jobId).toBe("default");
    const state = useDashboardStore.getState();
    expect(state.calibrationLoading).toBe(false);
    expect(state.calibrationError).toBeNull();
  });

  it("returns null on HTTP 409 conflict + populates error from detail", async () => {
    // Simulate ApiError with a structured body.detail (the backend
    // emits this for the in-flight-job conflict path).
    const apiError = new ApiError(409, "Conflict");
    apiError.body = {
      detail: "A calibration job for mind 'default' is already in flight.",
    };
    vi.spyOn(globalThis, "fetch").mockRejectedValue(apiError);

    const jobId = await useDashboardStore
      .getState()
      .startCalibration({ mind_id: "default" });

    expect(jobId).toBeNull();
    const state = useDashboardStore.getState();
    expect(state.calibrationError).toContain("already in flight");
  });

  it("returns null on generic error + uses fallback message", async () => {
    vi.spyOn(globalThis, "fetch").mockRejectedValue(new Error("Boom"));

    const jobId = await useDashboardStore
      .getState()
      .startCalibration({ mind_id: "default" });

    expect(jobId).toBeNull();
    expect(useDashboardStore.getState().calibrationError).toContain("Boom");
  });
});

// ── fetchCalibrationJob ─────────────────────────────────────────

describe("calibration slice — fetchCalibrationJob", () => {
  it("populates currentCalibrationJob on success", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValueOnce({
      ok: true,
      json: () => Promise.resolve(HEALTHY_SNAPSHOT),
    } as Response);

    await useDashboardStore.getState().fetchCalibrationJob("default");

    const state = useDashboardStore.getState();
    expect(state.currentCalibrationJob?.job_id).toBe("default");
    expect(state.currentCalibrationJob?.status).toBe("probing");
    expect(state.calibrationLoading).toBe(false);
  });

  it("clears job + sets error on 404", async () => {
    const apiError = new ApiError(404, "Not Found");
    apiError.body = { detail: "Calibration job 'ghost' not found." };
    vi.spyOn(globalThis, "fetch").mockRejectedValue(apiError);

    await useDashboardStore.getState().fetchCalibrationJob("ghost");

    const state = useDashboardStore.getState();
    expect(state.currentCalibrationJob).toBeNull();
    expect(state.calibrationError).toContain("not found");
  });
});

// ── cancelCalibrationJob ────────────────────────────────────────

describe("calibration slice — cancelCalibrationJob", () => {
  it("returns true on success", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValueOnce({
      ok: true,
      json: () =>
        Promise.resolve({
          job_id: "default",
          cancel_signal_written: true,
          already_terminal: false,
        }),
    } as Response);

    const result = await useDashboardStore
      .getState()
      .cancelCalibrationJob("default");

    expect(result).toBe(true);
    expect(useDashboardStore.getState().calibrationError).toBeNull();
  });

  it("returns false + sets error on failure", async () => {
    vi.spyOn(globalThis, "fetch").mockRejectedValue(new Error("Boom"));

    const result = await useDashboardStore
      .getState()
      .cancelCalibrationJob("default");

    expect(result).toBe(false);
    expect(useDashboardStore.getState().calibrationError).toContain("Boom");
  });
});

// ── unsubscribeFromCalibrationJob (idempotent without WS) ──────

describe("calibration slice — unsubscribeFromCalibrationJob", () => {
  it("is a no-op when no WS is active", () => {
    expect(useDashboardStore.getState().calibrationWs).toBeNull();
    useDashboardStore.getState().unsubscribeFromCalibrationJob();
    expect(useDashboardStore.getState().calibrationWs).toBeNull();
  });
});

// ── rc.15 LOW.2 — backup count refresh on rollback failure ──────

describe("calibration slice — rollbackCalibration failure refresh (rc.15 LOW.2)", () => {
  it("refreshes backup count when rollback returns 409 chain-exhausted", async () => {
    // Pre-state: count was 1 (cached from earlier load).
    useDashboardStore.setState({ calibrationBackupCount: 1 });

    const fetchSpy = vi.spyOn(globalThis, "fetch");
    // Call 1: POST /rollback → 409 (chain exhausted server-side).
    const rollbackError = new ApiError(409, "Conflict");
    rollbackError.body = { detail: "no calibration backup at .bak.1 — chain exhausted" };
    fetchSpy.mockRejectedValueOnce(rollbackError);
    // Call 2: GET /backups (the LOW.2 auto-refresh) → 0 backups.
    fetchSpy.mockResolvedValueOnce({
      ok: true,
      json: () => Promise.resolve({ mind_id: "default", generations: [] }),
    } as Response);

    const result = await useDashboardStore.getState().rollbackCalibration();

    // Rollback returned null (failure surface for the component).
    expect(result).toBeNull();
    // Backup count was refreshed: now 0 instead of stale 1.
    // Wait one microtask for the void-fired loadCalibrationBackups
    // to complete.
    await new Promise((r) => setTimeout(r, 0));
    expect(useDashboardStore.getState().calibrationBackupCount).toBe(0);
    // Error string was set so the UI shows a toast.
    expect(useDashboardStore.getState().calibrationError).toContain(
      "exhausted",
    );
  });
});
