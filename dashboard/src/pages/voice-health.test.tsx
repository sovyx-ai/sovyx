/**
 * Voice Health page tests — L7 Frontend (ADR §4.7).
 *
 * Mocks the zustand store and asserts that the page fetches on mount,
 * renders combos + overrides, disables the warm re-probe when the
 * pipeline is offline, and wires the three mutations (reprobe, forget,
 * pin) to the right store actions with the right arguments.
 */

import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen, fireEvent, waitFor } from "@/test/test-utils";
import type {
  VoiceHealthComboEntry,
  VoiceHealthOverrideEntry,
  VoiceHealthProbeResult,
  VoiceHealthSnapshotResponse,
} from "@/types/api";

const mockFetch = vi.fn();
const mockReprobe = vi.fn();
const mockForget = vi.fn();
const mockPin = vi.fn();
const mockClearError = vi.fn();

let mockState: Record<string, unknown> = {};

vi.mock("@/stores/dashboard", () => ({
  useDashboardStore: (selector: (s: Record<string, unknown>) => unknown) =>
    typeof selector === "function" ? selector(mockState) : mockState,
}));

import VoiceHealthPage from "./voice-health";

/* ── Fixtures ── */

function makeCombo(
  overrides: Partial<VoiceHealthComboEntry> = {},
): VoiceHealthComboEntry {
  return {
    endpoint_guid: "EP-A",
    device_friendly_name: "Headset Mic",
    device_interface_name: "\\\\?\\SWD#MMDEVAPI#A",
    device_class: "Audio",
    endpoint_fxproperties_sha: "abc123",
    winning_combo: {
      host_api: "WASAPI",
      sample_rate: 48000,
      channels: 1,
      sample_format: "float32",
      exclusive: true,
      auto_convert: false,
      frames_per_buffer: 480,
    },
    validated_at: "2026-04-19T10:00:00Z",
    validation_mode: "cold",
    vad_max_prob_at_validation: 0.92,
    vad_mean_prob_at_validation: 0.34,
    rms_db_at_validation: -22.5,
    probe_duration_ms: 1500,
    detected_apos_at_validation: [],
    cascade_attempts_before_success: 1,
    boots_validated: 3,
    last_boot_validated: "2026-04-19T10:00:00Z",
    last_boot_diagnosis: "healthy",
    probe_history: [],
    pinned: false,
    needs_revalidation: false,
    ...overrides,
  };
}

function makeOverride(
  overrides: Partial<VoiceHealthOverrideEntry> = {},
): VoiceHealthOverrideEntry {
  return {
    endpoint_guid: "EP-PIN",
    device_friendly_name: "Studio Mic",
    pinned_combo: {
      host_api: "WASAPI",
      sample_rate: 44100,
      channels: 1,
      sample_format: "int16",
      exclusive: true,
      auto_convert: false,
      frames_per_buffer: 441,
    },
    pinned_at: "2026-04-18T09:30:00Z",
    pinned_by: "user",
    reason: "mission-critical",
    ...overrides,
  };
}

function makeProbeResult(
  overrides: Partial<VoiceHealthProbeResult> = {},
): VoiceHealthProbeResult {
  return {
    diagnosis: "healthy",
    mode: "cold",
    combo: {
      host_api: "WASAPI",
      sample_rate: 48000,
      channels: 1,
      sample_format: "float32",
      exclusive: true,
      auto_convert: false,
      frames_per_buffer: 480,
    },
    vad_max_prob: 0.87,
    vad_mean_prob: 0.21,
    rms_db: -25.4,
    callbacks_fired: 75,
    duration_ms: 1520,
    error: null,
    remediation: null,
    ...overrides,
  };
}

function setStore(
  snapshot: VoiceHealthSnapshotResponse | null,
  extras: Record<string, unknown> = {},
) {
  mockState = {
    voiceHealthSnapshot: snapshot,
    voiceHealthLoading: false,
    voiceHealthError: null,
    voiceHealthLastProbe: {},
    voiceHealthBusy: {},
    fetchVoiceHealth: mockFetch,
    reprobeVoiceEndpoint: mockReprobe,
    forgetVoiceEndpoint: mockForget,
    pinVoiceEndpoint: mockPin,
    clearVoiceHealthError: mockClearError,
    ...extras,
  };
}

beforeEach(() => {
  mockFetch.mockReset();
  mockReprobe.mockReset().mockResolvedValue(null);
  mockForget.mockReset().mockResolvedValue(true);
  mockPin.mockReset().mockResolvedValue(true);
  mockClearError.mockReset();
  setStore(null, { voiceHealthLoading: true });
});

afterEach(() => {
  vi.restoreAllMocks();
});

/* ── Tests ── */

describe("VoiceHealthPage", () => {
  it("shows loading state before the first snapshot lands", () => {
    setStore(null, { voiceHealthLoading: true });
    render(<VoiceHealthPage />);
    expect(screen.getByTestId("voice-health-loading")).toBeInTheDocument();
  });

  it("shows error state when the initial fetch fails", () => {
    setStore(null, {
      voiceHealthLoading: false,
      voiceHealthError: "HTTP 503: probe unavailable",
    });
    render(<VoiceHealthPage />);
    expect(screen.getByText("HTTP 503: probe unavailable")).toBeInTheDocument();
  });

  it("fetches the snapshot on mount", async () => {
    setStore({
      combo_store: [],
      overrides: [],
      data_dir: "/tmp/sovyx",
      voice_enabled: false,
    });
    render(<VoiceHealthPage />);
    await waitFor(() => expect(mockFetch).toHaveBeenCalled());
  });

  it("renders empty states when both panels are empty", () => {
    setStore({
      combo_store: [],
      overrides: [],
      data_dir: "/tmp/sovyx",
      voice_enabled: true,
    });
    render(<VoiceHealthPage />);
    expect(screen.getByTestId("combos-empty")).toBeInTheDocument();
    expect(screen.getByTestId("overrides-empty")).toBeInTheDocument();
  });

  it("renders combos + overrides + data_dir", () => {
    setStore({
      combo_store: [makeCombo()],
      overrides: [makeOverride()],
      data_dir: "/var/lib/sovyx",
      voice_enabled: true,
    });
    render(<VoiceHealthPage />);
    expect(screen.getByTestId("combo-row-EP-A")).toBeInTheDocument();
    expect(screen.getByTestId("override-row-EP-PIN")).toBeInTheDocument();
    expect(screen.getByText(/\/var\/lib\/sovyx/)).toBeInTheDocument();
    expect(screen.getByText("Headset Mic")).toBeInTheDocument();
    expect(screen.getByText("Studio Mic")).toBeInTheDocument();
  });

  it("surfaces the voice-enabled indicator and disables warm re-probe when offline", () => {
    setStore({
      combo_store: [makeCombo()],
      overrides: [],
      data_dir: "/tmp/sovyx",
      voice_enabled: false,
    });
    render(<VoiceHealthPage />);
    expect(screen.getByText("Voice pipeline offline")).toBeInTheDocument();
    const warmBtn = screen.getByTestId("btn-reprobe-warm-EP-A");
    expect(warmBtn).toBeDisabled();
  });

  it("enables warm re-probe when the pipeline is running", () => {
    setStore({
      combo_store: [makeCombo()],
      overrides: [],
      data_dir: "/tmp/sovyx",
      voice_enabled: true,
    });
    render(<VoiceHealthPage />);
    const warmBtn = screen.getByTestId("btn-reprobe-warm-EP-A");
    expect(warmBtn).not.toBeDisabled();
  });

  it("dispatches a cold re-probe with device_index=-1 + the stored combo", async () => {
    setStore({
      combo_store: [makeCombo()],
      overrides: [],
      data_dir: "/tmp/sovyx",
      voice_enabled: false,
    });
    render(<VoiceHealthPage />);
    fireEvent.click(screen.getByTestId("btn-reprobe-cold-EP-A"));
    await waitFor(() => expect(mockReprobe).toHaveBeenCalledTimes(1));
    expect(mockReprobe).toHaveBeenCalledWith(
      expect.objectContaining({
        endpoint_guid: "EP-A",
        device_index: -1,
        mode: "cold",
        combo: expect.objectContaining({ host_api: "WASAPI", exclusive: true }),
      }),
    );
  });

  it("dispatches a warm re-probe when the pipeline is running", async () => {
    setStore({
      combo_store: [makeCombo()],
      overrides: [],
      data_dir: "/tmp/sovyx",
      voice_enabled: true,
    });
    render(<VoiceHealthPage />);
    fireEvent.click(screen.getByTestId("btn-reprobe-warm-EP-A"));
    await waitFor(() => expect(mockReprobe).toHaveBeenCalledTimes(1));
    expect(mockReprobe).toHaveBeenCalledWith(
      expect.objectContaining({ mode: "warm" }),
    );
  });

  it("asks for confirmation then forgets an endpoint on confirm", async () => {
    const confirmSpy = vi.spyOn(window, "confirm").mockReturnValue(true);
    setStore({
      combo_store: [makeCombo()],
      overrides: [],
      data_dir: "/tmp/sovyx",
      voice_enabled: false,
    });
    render(<VoiceHealthPage />);
    fireEvent.click(screen.getByTestId("btn-forget-EP-A"));
    await waitFor(() => expect(mockForget).toHaveBeenCalledWith("EP-A"));
    expect(confirmSpy).toHaveBeenCalled();
  });

  it("skips the forget action when the user cancels the confirm", async () => {
    const confirmSpy = vi.spyOn(window, "confirm").mockReturnValue(false);
    setStore({
      combo_store: [makeCombo()],
      overrides: [],
      data_dir: "/tmp/sovyx",
      voice_enabled: false,
    });
    render(<VoiceHealthPage />);
    fireEvent.click(screen.getByTestId("btn-forget-EP-A"));
    expect(confirmSpy).toHaveBeenCalled();
    expect(mockForget).not.toHaveBeenCalled();
  });

  it("pins an unpinned endpoint via the store with source=user", async () => {
    setStore({
      combo_store: [makeCombo({ pinned: false })],
      overrides: [],
      data_dir: "/tmp/sovyx",
      voice_enabled: false,
    });
    render(<VoiceHealthPage />);
    fireEvent.click(screen.getByTestId("btn-pin-EP-A"));
    await waitFor(() => expect(mockPin).toHaveBeenCalledTimes(1));
    expect(mockPin).toHaveBeenCalledWith(
      expect.objectContaining({
        endpoint_guid: "EP-A",
        device_friendly_name: "Headset Mic",
        source: "user",
        combo: expect.objectContaining({ host_api: "WASAPI" }),
      }),
    );
  });

  it("hides the pin button on already-pinned entries", () => {
    setStore({
      combo_store: [makeCombo({ pinned: true })],
      overrides: [],
      data_dir: "/tmp/sovyx",
      voice_enabled: false,
    });
    render(<VoiceHealthPage />);
    expect(screen.queryByTestId("btn-pin-EP-A")).not.toBeInTheDocument();
  });

  it("renders the latest-probe card when a probe has landed", () => {
    setStore(
      {
        combo_store: [makeCombo()],
        overrides: [],
        data_dir: "/tmp/sovyx",
        voice_enabled: true,
      },
      {
        voiceHealthLastProbe: {
          "EP-A": makeProbeResult({ diagnosis: "low_signal", rms_db: -55.0 }),
        },
      },
    );
    render(<VoiceHealthPage />);
    expect(screen.getAllByTestId("diagnosis-low_signal").length).toBeGreaterThan(0);
    expect(screen.getByText(/-55\.0 dBFS/)).toBeInTheDocument();
  });

  it("disables per-row action buttons while a mutation is in flight", () => {
    setStore(
      {
        combo_store: [makeCombo()],
        overrides: [],
        data_dir: "/tmp/sovyx",
        voice_enabled: true,
      },
      { voiceHealthBusy: { "EP-A": true } },
    );
    render(<VoiceHealthPage />);
    expect(screen.getByTestId("btn-reprobe-cold-EP-A")).toBeDisabled();
    expect(screen.getByTestId("btn-reprobe-warm-EP-A")).toBeDisabled();
    expect(screen.getByTestId("btn-forget-EP-A")).toBeDisabled();
  });

  it("clears error + refetches when the error retry button is clicked", async () => {
    setStore(null, {
      voiceHealthLoading: false,
      voiceHealthError: "boom",
    });
    render(<VoiceHealthPage />);
    fireEvent.click(screen.getByText("Refresh"));
    await waitFor(() => expect(mockClearError).toHaveBeenCalled());
    expect(mockFetch).toHaveBeenCalled();
  });

  it("refetches when the header refresh button is clicked", async () => {
    setStore({
      combo_store: [],
      overrides: [],
      data_dir: "/tmp/sovyx",
      voice_enabled: true,
    });
    render(<VoiceHealthPage />);
    mockFetch.mockClear();
    fireEvent.click(screen.getByTestId("btn-refresh-voice-health"));
    await waitFor(() => expect(mockFetch).toHaveBeenCalled());
  });
});
