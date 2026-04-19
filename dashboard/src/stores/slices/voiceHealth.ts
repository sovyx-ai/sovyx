/**
 * Voice Capture Health slice — fetches the L7 snapshot and exposes the
 * three operator actions (reprobe / forget / pin) that back the panel
 * on ``/voice/health``.
 *
 * ADR-voice-capture-health-lifecycle §4.7. The backend is stateless on
 * these endpoints (reads + writes go straight to ComboStore /
 * CaptureOverrides JSON), so we refetch the snapshot after every
 * mutation rather than splicing optimistic updates — the files are tiny
 * and keeping the single source of truth on disk is worth the extra
 * round-trip.
 */
import type { StateCreator } from "zustand";
import type {
  VoiceHealthForgetResponse,
  VoiceHealthPinRequest,
  VoiceHealthPinResponse,
  VoiceHealthProbeMode,
  VoiceHealthProbeResult,
  VoiceHealthReprobeRequest,
  VoiceHealthReprobeResponse,
  VoiceHealthSnapshotResponse,
} from "@/types/api";
import {
  VoiceHealthForgetResponseSchema,
  VoiceHealthPinResponseSchema,
  VoiceHealthReprobeResponseSchema,
  VoiceHealthSnapshotResponseSchema,
} from "@/types/schemas";
import { ApiError, api, isAbortError } from "@/lib/api";
import type { DashboardState } from "../dashboard";

export interface VoiceHealthSlice {
  // ── State ──
  voiceHealthSnapshot: VoiceHealthSnapshotResponse | null;
  voiceHealthLoading: boolean;
  voiceHealthError: string | null;
  /** Last reprobe result keyed by endpoint_guid — drives the inline "latest probe" badge. */
  voiceHealthLastProbe: Record<string, VoiceHealthProbeResult>;
  /** Per-endpoint in-flight action flag — disables buttons while a mutation is pending. */
  voiceHealthBusy: Record<string, boolean>;

  // ── Actions ──
  fetchVoiceHealth: (signal?: AbortSignal) => Promise<void>;
  reprobeVoiceEndpoint: (
    body: VoiceHealthReprobeRequest,
  ) => Promise<VoiceHealthProbeResult | null>;
  forgetVoiceEndpoint: (
    endpoint_guid: string,
    reason?: string,
  ) => Promise<boolean>;
  pinVoiceEndpoint: (body: VoiceHealthPinRequest) => Promise<boolean>;
  clearVoiceHealthError: () => void;
}

export const createVoiceHealthSlice: StateCreator<
  DashboardState,
  [],
  [],
  VoiceHealthSlice
> = (set, get) => ({
  // ── Initial State ──
  voiceHealthSnapshot: null,
  voiceHealthLoading: false,
  voiceHealthError: null,
  voiceHealthLastProbe: {},
  voiceHealthBusy: {},

  fetchVoiceHealth: async (signal?: AbortSignal) => {
    set({ voiceHealthLoading: true, voiceHealthError: null });
    try {
      const data = await api.get<VoiceHealthSnapshotResponse>(
        "/api/voice/health",
        { signal, schema: VoiceHealthSnapshotResponseSchema },
      );
      set({ voiceHealthSnapshot: data, voiceHealthLoading: false });
    } catch (err) {
      if (isAbortError(err)) {
        set({ voiceHealthLoading: false });
        return;
      }
      const msg =
        err instanceof ApiError
          ? `HTTP ${err.status}: ${err.message}`
          : err instanceof Error
            ? err.message
            : String(err);
      set({ voiceHealthLoading: false, voiceHealthError: msg });
    }
  },

  reprobeVoiceEndpoint: async (body: VoiceHealthReprobeRequest) => {
    const guid = body.endpoint_guid;
    set((s) => ({ voiceHealthBusy: { ...s.voiceHealthBusy, [guid]: true } }));
    try {
      const resp = await api.post<VoiceHealthReprobeResponse>(
        "/api/voice/health/reprobe",
        body,
        { schema: VoiceHealthReprobeResponseSchema },
      );
      set((s) => ({
        voiceHealthLastProbe: {
          ...s.voiceHealthLastProbe,
          [guid]: resp.result,
        },
      }));
      // Refresh the snapshot so probe_history reflects the new entry.
      void get().fetchVoiceHealth();
      return resp.result;
    } catch (err) {
      const msg =
        err instanceof ApiError
          ? `HTTP ${err.status}: ${err.message}`
          : err instanceof Error
            ? err.message
            : String(err);
      set({ voiceHealthError: msg });
      return null;
    } finally {
      set((s) => {
        const next = { ...s.voiceHealthBusy };
        delete next[guid];
        return { voiceHealthBusy: next };
      });
    }
  },

  forgetVoiceEndpoint: async (endpoint_guid: string, reason = "dashboard-forget") => {
    set((s) => ({
      voiceHealthBusy: { ...s.voiceHealthBusy, [endpoint_guid]: true },
    }));
    try {
      const resp = await api.post<VoiceHealthForgetResponse>(
        "/api/voice/health/forget",
        { endpoint_guid, reason },
        { schema: VoiceHealthForgetResponseSchema },
      );
      void get().fetchVoiceHealth();
      return resp.invalidated;
    } catch (err) {
      const msg =
        err instanceof ApiError
          ? `HTTP ${err.status}: ${err.message}`
          : err instanceof Error
            ? err.message
            : String(err);
      set({ voiceHealthError: msg });
      return false;
    } finally {
      set((s) => {
        const next = { ...s.voiceHealthBusy };
        delete next[endpoint_guid];
        return { voiceHealthBusy: next };
      });
    }
  },

  pinVoiceEndpoint: async (body: VoiceHealthPinRequest) => {
    const guid = body.endpoint_guid;
    set((s) => ({ voiceHealthBusy: { ...s.voiceHealthBusy, [guid]: true } }));
    try {
      const resp = await api.post<VoiceHealthPinResponse>(
        "/api/voice/health/pin",
        body,
        { schema: VoiceHealthPinResponseSchema },
      );
      void get().fetchVoiceHealth();
      return resp.pinned;
    } catch (err) {
      const msg =
        err instanceof ApiError
          ? `HTTP ${err.status}: ${err.message}`
          : err instanceof Error
            ? err.message
            : String(err);
      set({ voiceHealthError: msg });
      return false;
    } finally {
      set((s) => {
        const next = { ...s.voiceHealthBusy };
        delete next[guid];
        return { voiceHealthBusy: next };
      });
    }
  },

  clearVoiceHealthError: () => set({ voiceHealthError: null }),
});

/** Mode options exposed by the panel UI (kept here so the page can import
 * directly rather than hard-coding strings). */
export const VOICE_HEALTH_PROBE_MODES: VoiceHealthProbeMode[] = ["cold", "warm"];
