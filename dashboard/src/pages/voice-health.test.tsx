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
const mockFetchMixerKbList = vi.fn();
const mockFetchMixerKbDetail = vi.fn();
const mockValidateMixerKb = vi.fn();

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
    // Mixer-KB card defaults: empty list, no detail cache, stubbed
    // actions. Individual tests override via ``extras`` when they
    // exercise the card's behaviour.
    mixerKbList: null,
    mixerKbLoading: false,
    mixerKbError: null,
    mixerKbDetails: {},
    fetchMixerKbList: mockFetchMixerKbList,
    fetchMixerKbDetail: mockFetchMixerKbDetail,
    validateMixerKbProfile: mockValidateMixerKb,
    ...extras,
  };
}

beforeEach(() => {
  mockFetch.mockReset();
  mockReprobe.mockReset().mockResolvedValue(null);
  mockForget.mockReset().mockResolvedValue(true);
  mockPin.mockReset().mockResolvedValue(true);
  mockClearError.mockReset();
  mockFetchMixerKbList.mockReset().mockResolvedValue(undefined);
  mockFetchMixerKbDetail.mockReset().mockResolvedValue(null);
  mockValidateMixerKb.mockReset().mockResolvedValue({
    ok: true,
    profile_id: null,
    profile_version: null,
    issues: [],
  });
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

  it("dispatches a cold re-probe without a device_index so the backend resolves it", async () => {
    setStore({
      combo_store: [makeCombo()],
      overrides: [],
      data_dir: "/tmp/sovyx",
      voice_enabled: false,
    });
    render(<VoiceHealthPage />);
    fireEvent.click(screen.getByTestId("btn-reprobe-cold-EP-A"));
    await waitFor(() => expect(mockReprobe).toHaveBeenCalledTimes(1));
    const call = mockReprobe.mock.calls[0][0];
    expect(call).toEqual(
      expect.objectContaining({
        endpoint_guid: "EP-A",
        mode: "cold",
        combo: expect.objectContaining({ host_api: "WASAPI", exclusive: true }),
      }),
    );
    expect(call).not.toHaveProperty("device_index");
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

describe("VoiceHealthPage — Mixer KB card", () => {
  const _healthySnapshot = {
    combo_store: [],
    overrides: [],
    data_dir: "/tmp/sovyx",
    voice_enabled: true,
  } as const;

  it("fetches the KB list on mount", async () => {
    setStore(_healthySnapshot);
    render(<VoiceHealthPage />);
    await waitFor(() => expect(mockFetchMixerKbList).toHaveBeenCalled());
  });

  it("renders the empty-state when the backend returns zero profiles", async () => {
    setStore(_healthySnapshot, {
      mixerKbList: { profiles: [], shipped_count: 0, user_count: 0 },
    });
    render(<VoiceHealthPage />);
    expect(await screen.findByTestId("mixer-kb-empty")).toBeInTheDocument();
    expect(screen.getByTestId("mixer-kb-count")).toHaveTextContent("0 / 0");
  });

  it("renders the loading state before the first list lands", () => {
    setStore(_healthySnapshot, {
      mixerKbList: null,
      mixerKbLoading: true,
    });
    render(<VoiceHealthPage />);
    expect(screen.getByTestId("mixer-kb-loading")).toBeInTheDocument();
  });

  it("surfaces a mixer-KB fetch error in its own banner", () => {
    setStore(_healthySnapshot, {
      mixerKbList: null,
      mixerKbLoading: false,
      mixerKbError: "HTTP 500: kb unavailable",
    });
    render(<VoiceHealthPage />);
    expect(screen.getByTestId("mixer-kb-error")).toHaveTextContent(
      "kb unavailable",
    );
  });

  it("renders one row per profile + the shipped/user counts", () => {
    setStore(_healthySnapshot, {
      mixerKbList: {
        profiles: [
          {
            pool: "shipped",
            profile_id: "vaio_vjfe69_sn6180",
            profile_version: 1,
            schema_version: 1,
            driver_family: "hda",
            codec_id_glob: "14F1:5045",
            match_threshold: 0.6,
            factory_regime: "attenuation",
            contributed_by: "sovyx-core",
          },
          {
            pool: "user",
            profile_id: "community_xps",
            profile_version: 2,
            schema_version: 1,
            driver_family: "usb-audio",
            codec_id_glob: "1532:0543",
            match_threshold: 0.5,
            factory_regime: "saturation",
            contributed_by: "alice",
          },
        ],
        shipped_count: 1,
        user_count: 1,
      },
    });
    render(<VoiceHealthPage />);
    expect(
      screen.getByTestId("mixer-kb-profile-vaio_vjfe69_sn6180"),
    ).toBeInTheDocument();
    expect(
      screen.getByTestId("mixer-kb-profile-community_xps"),
    ).toBeInTheDocument();
    expect(screen.getByTestId("mixer-kb-count")).toHaveTextContent("1 / 1");
  });

  it("expanding a row triggers a detail fetch and renders the panel", async () => {
    setStore(_healthySnapshot, {
      mixerKbList: {
        profiles: [
          {
            pool: "shipped",
            profile_id: "vaio_one",
            profile_version: 1,
            schema_version: 1,
            driver_family: "hda",
            codec_id_glob: "14F1:5045",
            match_threshold: 0.6,
            factory_regime: "attenuation",
            contributed_by: "sovyx-core",
          },
        ],
        shipped_count: 1,
        user_count: 0,
      },
    });
    render(<VoiceHealthPage />);
    const row = screen.getByTestId("mixer-kb-profile-vaio_one");
    const toggle = row.querySelector("button");
    expect(toggle).not.toBeNull();
    fireEvent.click(toggle!);
    await waitFor(() =>
      expect(mockFetchMixerKbDetail).toHaveBeenCalledWith("vaio_one"),
    );
    expect(screen.getByTestId("mixer-kb-detail-vaio_one")).toBeInTheDocument();
  });

  it("renders cached detail immediately without a second fetch", () => {
    setStore(_healthySnapshot, {
      mixerKbList: {
        profiles: [
          {
            pool: "shipped",
            profile_id: "vaio_detail",
            profile_version: 1,
            schema_version: 1,
            driver_family: "hda",
            codec_id_glob: "14F1:5045",
            match_threshold: 0.6,
            factory_regime: "attenuation",
            contributed_by: "sovyx-core",
          },
        ],
        shipped_count: 1,
        user_count: 0,
      },
      mixerKbDetails: {
        vaio_detail: {
          pool: "shipped",
          profile_id: "vaio_detail",
          profile_version: 1,
          schema_version: 1,
          driver_family: "hda",
          codec_id_glob: "14F1:5045",
          match_threshold: 0.6,
          factory_regime: "attenuation",
          contributed_by: "sovyx-core",
          system_vendor_glob: "Sony*",
          system_product_glob: "VJFE69*",
          distro_family: null,
          audio_stack: "pipewire",
          kernel_major_minor_glob: "6.*",
          factory_signature_roles: ["capture_master", "internal_mic_boost"],
          verified_on_count: 1,
        },
      },
    });
    render(<VoiceHealthPage />);
    const row = screen.getByTestId("mixer-kb-profile-vaio_detail");
    const toggle = row.querySelector("button");
    fireEvent.click(toggle!);
    expect(screen.getByText(/capture_master, internal_mic_boost/)).toBeInTheDocument();
    // Cache hit — no additional fetch.
    expect(mockFetchMixerKbDetail).not.toHaveBeenCalled();
  });

  it("validate panel: submits the textarea body and renders an OK verdict", async () => {
    setStore(_healthySnapshot, {
      mixerKbList: { profiles: [], shipped_count: 0, user_count: 0 },
    });
    mockValidateMixerKb.mockResolvedValueOnce({
      ok: true,
      profile_id: "vaio_candidate",
      profile_version: 1,
      issues: [],
    });
    render(<VoiceHealthPage />);
    fireEvent.click(screen.getByTestId("mixer-kb-validate-toggle"));
    const textarea = screen.getByTestId(
      "mixer-kb-validate-textarea",
    ) as HTMLTextAreaElement;
    fireEvent.change(textarea, {
      target: { value: "profile_id: vaio_candidate\n" },
    });
    fireEvent.click(screen.getByTestId("mixer-kb-validate-submit"));
    await waitFor(() =>
      expect(mockValidateMixerKb).toHaveBeenCalledWith({
        yaml_body: "profile_id: vaio_candidate\n",
      }),
    );
    expect(
      await screen.findByTestId("mixer-kb-validate-verdict"),
    ).toHaveTextContent(/Schema OK/);
  });

  it("validate panel: renders the full issue list on failure", async () => {
    setStore(_healthySnapshot, {
      mixerKbList: { profiles: [], shipped_count: 0, user_count: 0 },
    });
    mockValidateMixerKb.mockResolvedValueOnce({
      ok: false,
      profile_id: null,
      profile_version: null,
      issues: [
        { loc: "codec_id_glob", msg: "Field required" },
        { loc: "verified_on", msg: "must be non-empty" },
      ],
    });
    render(<VoiceHealthPage />);
    fireEvent.click(screen.getByTestId("mixer-kb-validate-toggle"));
    const textarea = screen.getByTestId(
      "mixer-kb-validate-textarea",
    ) as HTMLTextAreaElement;
    fireEvent.change(textarea, { target: { value: "not enough fields" } });
    fireEvent.click(screen.getByTestId("mixer-kb-validate-submit"));
    const issues = await screen.findByTestId("mixer-kb-validate-issues");
    expect(issues).toHaveTextContent("codec_id_glob");
    expect(issues).toHaveTextContent("Field required");
    expect(issues).toHaveTextContent("verified_on");
  });
});
