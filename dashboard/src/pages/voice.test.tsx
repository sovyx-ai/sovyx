/**
 * Voice page tests — TASK-204
 *
 * Tests: render, loading state, error state, populated data, model matrix.
 */

import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor } from "@/test/test-utils";
import { fireEvent } from "@testing-library/react";
import VoicePage, { computeVoiceFreshness } from "./voice";

/* ── Mock API ── */

const mockGet = vi.fn();

vi.mock("@/lib/api", () => ({
  api: { get: (...args: unknown[]) => mockGet(...args) },
  isAbortError: (err: unknown) =>
    err instanceof DOMException && (err as DOMException).name === "AbortError",
  // Mission C4 §T1.11 — the new DegradedBannerPerPageMount on VoicePage
  // triggers useApiPoller which references ApiError. Re-export here so
  // the mock is structurally complete.
  ApiError: class ApiError extends Error {
    status: number;
    constructor(status: number, message: string) {
      super(message);
      this.status = status;
    }
  },
}));

// Mission C4 §T1.11 — short-circuit the engine-degraded poller so the
// page tests don't poll the network. Returns a clean "no degraded
// state" payload so the banner mounts render nothing.
vi.mock("@/hooks/use-engine-degraded-poller", () => ({
  useEngineDegradedPoller: () => ({
    data: { axes: [], composite_severity: null, composite_axis_count: 0, ack: { acked: false } },
    error: "ok",
    consecutive5xx: 0,
  }),
  ENGINE_DEGRADED_POLL_INTERVAL_MS: 5000,
}));

/* ── Fixtures ── */

const VOICE_STATUS = {
  pipeline: { running: true, state: "idle", latency_ms: 42 },
  // LIVE-2 P0-1: health is the real readiness signal; the VAD/Wake status
  // dots are driven by health === "healthy", not by registration.
  stt: { engine: "MoonshineSTT", model: "moonshine-tiny", state: "ready", health: "healthy" },
  tts: {
    engine: "PiperTTS",
    model: "en_US-lessac-medium",
    initialized: true,
    health: "healthy",
  },
  wake_word: { enabled: true, phrase: "hey sovyx", health: "healthy" },
  vad: { enabled: true, health: "healthy" },
  // LIVE-2 P1-10: configured (server registered) so the card renders; the
  // unconfigured-hidden case is covered by a dedicated test below.
  wyoming: { configured: true, connected: false, endpoint: null },
  hardware: { tier: "PI5", ram_mb: 4096 },
};

const VOICE_MODELS = {
  detected_tier: "PI5",
  active: {
    stt_primary: "moonshine-tiny",
    stt_streaming: "moonshine-tiny",
    tts_primary: "piper-lessac",
    tts_quality: "piper-lessac",
    wake: "openwakeword",
    vad: "silero-v5",
  },
  available_tiers: {
    PI5: {
      stt_primary: "moonshine-tiny",
      stt_streaming: "moonshine-tiny",
      tts_primary: "piper-lessac",
      tts_quality: "piper-lessac",
      wake: "openwakeword",
      vad: "silero-v5",
    },
    N100: {
      stt_primary: "moonshine-base",
      stt_streaming: "moonshine-base",
      tts_primary: "kokoro-82m",
      tts_quality: "kokoro-82m",
      wake: "openwakeword",
      vad: "silero-v5",
    },
  },
};

/* ── Per-mind wake-word fixtures (Mission MISSION-wake-word-ui §T5) ── */

const PER_MIND_WAKE_WORD_HEALTHY = {
  mind_id: "aria",
  wake_word: "Aria",
  voice_language: "en",
  wake_word_enabled: true,
  runtime_registered: true,
  model_path: "/data/wake_word_models/pretrained/aria.onnx",
  resolution_strategy: "exact" as const,
  matched_name: "aria",
  phoneme_distance: 0,
  last_error: null,
};

const PER_MIND_WAKE_WORD_PHONETIC = {
  mind_id: "lucia",
  wake_word: "Lúcia",
  voice_language: "pt-BR",
  wake_word_enabled: true,
  runtime_registered: true,
  model_path: "/data/wake_word_models/pretrained/lucia.onnx",
  resolution_strategy: "phonetic" as const,
  matched_name: "lucia",
  phoneme_distance: 0,
  last_error: null,
};

const PER_MIND_WAKE_WORD_BROKEN = {
  mind_id: "lucia",
  wake_word: "Lucia",
  voice_language: "pt-BR",
  wake_word_enabled: true,
  runtime_registered: false,
  model_path: null,
  resolution_strategy: "none" as const,
  matched_name: null,
  phoneme_distance: null,
  last_error:
    "No ONNX model resolved for wake word 'Lucia' ... train via `sovyx voice train-wake-word`",
};

const PER_MIND_WAKE_WORD_DISABLED = {
  mind_id: "joao",
  wake_word: "Joao",
  voice_language: "en",
  wake_word_enabled: false,
  runtime_registered: false,
  model_path: null,
  resolution_strategy: null,
  matched_name: null,
  phoneme_distance: null,
  last_error: null,
};

function setupMockSuccess(perMindOverride?: unknown) {
  mockGet.mockImplementation((path: string) => {
    if (path === "/api/voice/status") return Promise.resolve(VOICE_STATUS);
    if (path === "/api/voice/models") return Promise.resolve(VOICE_MODELS);
    // v1.3 §4.3 L5a — the page now renders LinuxMicGainCard which
    // fetches this endpoint on mount. Provide a healthy default so
    // existing tests don't fail on an unhandled path.
    if (path === "/api/voice/linux-mixer-diagnostics") {
      return Promise.resolve({
        platform_supported: false,
        amixer_available: false,
        snapshots: [],
        aggregated_boost_db_ceiling: 18,
        saturation_ratio_ceiling: 0.5,
        reset_enabled_by_default: true,
      });
    }
    if (path === "/api/voice/wake-word/status") {
      return Promise.resolve(
        perMindOverride ?? {
          minds: [
            PER_MIND_WAKE_WORD_HEALTHY,
            PER_MIND_WAKE_WORD_BROKEN,
            PER_MIND_WAKE_WORD_DISABLED,
          ],
        },
      );
    }
    return Promise.reject(new Error("unknown path"));
  });
}

/* ── Tests ── */

describe("VoicePage", () => {
  beforeEach(() => {
    mockGet.mockReset();
  });

  it("shows loading state initially", () => {
    // Never resolve
    mockGet.mockReturnValue(new Promise(() => {}));
    render(<VoicePage />);
    expect(screen.getByText("Loading voice status…")).toBeInTheDocument();
  });

  it("shows error state on fetch failure", async () => {
    mockGet.mockRejectedValue(new Error("Network error"));
    render(<VoicePage />);
    await waitFor(() => {
      expect(screen.getByText("Failed to load voice status")).toBeInTheDocument();
    });
    expect(screen.getByText("Retry")).toBeInTheDocument();
  });

  it("renders page title and subtitle after loading", async () => {
    setupMockSuccess();
    render(<VoicePage />);
    await waitFor(() => {
      expect(screen.getByText("Voice Pipeline")).toBeInTheDocument();
    });
    expect(screen.getByText(/Real-time voice interaction/)).toBeInTheDocument();
  });

  it("renders pipeline status with running dot", async () => {
    setupMockSuccess();
    render(<VoicePage />);
    await waitFor(() => {
      expect(screen.getByText("Running")).toBeInTheDocument();
    });
    expect(screen.getByText("42ms")).toBeInTheDocument();
  });

  it("renders STT engine and model", async () => {
    setupMockSuccess();
    render(<VoicePage />);
    await waitFor(() => {
      expect(screen.getByText("MoonshineSTT")).toBeInTheDocument();
    });
    // moonshine-tiny appears in both STT section and model matrix
    expect(screen.getAllByText("moonshine-tiny").length).toBeGreaterThanOrEqual(1);
  });

  it("renders TTS engine and model", async () => {
    setupMockSuccess();
    render(<VoicePage />);
    await waitFor(() => {
      expect(screen.getByText("PiperTTS")).toBeInTheDocument();
    });
    expect(screen.getByText("en_US-lessac-medium")).toBeInTheDocument();
  });

  it("shows wake word phrase when enabled", async () => {
    setupMockSuccess();
    render(<VoicePage />);
    await waitFor(() => {
      expect(screen.getByText("hey sovyx")).toBeInTheDocument();
    });
  });

  it("renders VAD enabled status", async () => {
    setupMockSuccess();
    render(<VoicePage />);
    await waitFor(() => {
      expect(screen.getAllByText("Enabled").length).toBeGreaterThanOrEqual(1);
    });
  });

  it("renders Wyoming disconnected status when configured but not connected", async () => {
    // Fixture is configured:true, connected:false → card shows "Disconnected".
    setupMockSuccess();
    render(<VoicePage />);
    await waitFor(() => {
      expect(screen.getByText("Disconnected")).toBeInTheDocument();
    });
  });

  // ── LIVE-2 P1-10 — Wyoming truth ──

  it("hides the Wyoming card entirely when not configured", async () => {
    // The default daemon never wires a Wyoming server → configured false →
    // the card is hidden instead of showing a misleading "Disconnected".
    setupMockWithStatus({
      wyoming: { configured: false, connected: false, endpoint: null },
    });
    render(<VoicePage />);
    // Wait for the page to settle (STT engine present), then assert the
    // Wyoming section is absent.
    await waitFor(() => {
      expect(screen.getByText("MoonshineSTT")).toBeInTheDocument();
    });
    expect(screen.queryByText("Wyoming Protocol")).not.toBeInTheDocument();
    expect(screen.queryByText("Disconnected")).not.toBeInTheDocument();
  });

  it("shows Wyoming connected + endpoint when configured and running", async () => {
    setupMockWithStatus({
      wyoming: {
        configured: true,
        connected: true,
        endpoint: "127.0.0.1:10700",
      },
    });
    render(<VoicePage />);
    await waitFor(() => {
      expect(screen.getByText("Connected")).toBeInTheDocument();
    });
    expect(screen.getByText("127.0.0.1:10700")).toBeInTheDocument();
  });

  it("renders hardware tier", async () => {
    setupMockSuccess();
    render(<VoicePage />);
    await waitFor(() => {
      // PI5 appears in hardware section and model matrix
      expect(screen.getAllByText("PI5").length).toBeGreaterThanOrEqual(1);
    });
    expect(screen.getByText("4096 MB")).toBeInTheDocument();
  });

  it("renders model matrix table with tier columns", async () => {
    setupMockSuccess();
    render(<VoicePage />);
    await waitFor(() => {
      expect(screen.getByText("Model Matrix")).toBeInTheDocument();
    });
    // Both tiers present
    expect(screen.getByText("N100")).toBeInTheDocument();
    // Model values
    // kokoro-82m appears in both tts_primary and tts_quality for N100
    expect(screen.getAllByText("kokoro-82m").length).toBeGreaterThanOrEqual(1);
    // silero-v5 appears in both PI5 and N100 tiers
    expect(screen.getAllByText("silero-v5").length).toBeGreaterThanOrEqual(1);
  });

  it("shows not-configured banner when pipeline state is not_configured", async () => {
    mockGet.mockImplementation((path: string) => {
      if (path === "/api/voice/status") {
        return Promise.resolve({
          ...VOICE_STATUS,
          pipeline: { running: false, state: "not_configured", latency_ms: null },
        });
      }
      if (path === "/api/voice/models") return Promise.resolve(VOICE_MODELS);
      return Promise.reject(new Error("unknown"));
    });
    render(<VoicePage />);
    await waitFor(() => {
      expect(screen.getByText(/Voice pipeline is not configured/)).toBeInTheDocument();
    });
  });

  it("shows no STT message when engine is null", async () => {
    mockGet.mockImplementation((path: string) => {
      if (path === "/api/voice/status") {
        return Promise.resolve({
          ...VOICE_STATUS,
          stt: { engine: null, model: null, state: null },
        });
      }
      if (path === "/api/voice/models") return Promise.resolve(VOICE_MODELS);
      return Promise.reject(new Error("unknown"));
    });
    render(<VoicePage />);
    await waitFor(() => {
      expect(screen.getByText("No STT engine configured")).toBeInTheDocument();
    });
  });

  it("shows no TTS message when engine is null", async () => {
    mockGet.mockImplementation((path: string) => {
      if (path === "/api/voice/status") {
        return Promise.resolve({
          ...VOICE_STATUS,
          tts: { engine: null, model: null, initialized: false },
        });
      }
      if (path === "/api/voice/models") return Promise.resolve(VOICE_MODELS);
      return Promise.reject(new Error("unknown"));
    });
    render(<VoicePage />);
    await waitFor(() => {
      expect(screen.getByText("No TTS engine configured")).toBeInTheDocument();
    });
  });

  it("renders status dots with correct test ids", async () => {
    setupMockSuccess();
    render(<VoicePage />);
    await waitFor(() => {
      const activeDots = screen.getAllByTestId("status-active");
      expect(activeDots.length).toBeGreaterThanOrEqual(3); // pipeline, vad, wake word
    });
    const inactiveDots = screen.getAllByTestId("status-inactive");
    expect(inactiveDots.length).toBeGreaterThanOrEqual(1); // wyoming
  });

  // ── LIVE-2 Phase 3 (P0-1) — health, not mere registration ──

  function setupMockWithStatus(statusOverride: Record<string, unknown>) {
    mockGet.mockImplementation((path: string) => {
      if (path === "/api/voice/status")
        return Promise.resolve({ ...VOICE_STATUS, ...statusOverride });
      if (path === "/api/voice/models") return Promise.resolve(VOICE_MODELS);
      if (path === "/api/voice/linux-mixer-diagnostics")
        return Promise.resolve({
          platform_supported: false,
          amixer_available: false,
          snapshots: [],
          aggregated_boost_db_ceiling: 18,
          saturation_ratio_ceiling: 0.5,
          reset_enabled_by_default: true,
        });
      if (path === "/api/voice/wake-word/status")
        return Promise.resolve({ minds: [] });
      return Promise.reject(new Error("unknown path"));
    });
  }

  it("does NOT render a healthy green dot for a registered-but-failed VAD", async () => {
    // The registration flag (enabled) is still true, but health=failed.
    // The presence-only lie would have shown a green dot; truthful health
    // must show a "Failed" badge and leave VAD's dot inactive.
    setupMockWithStatus({
      vad: { enabled: true, health: "failed" },
    });
    render(<VoicePage />);

    await waitFor(() => {
      expect(screen.getByTestId("health-failed")).toBeInTheDocument();
    });
    // Only pipeline (running) + wake word (healthy) are green — NOT vad.
    expect(screen.getAllByTestId("status-active")).toHaveLength(2);
  });

  it("surfaces a degraded badge for a registered-but-uninitialized STT", async () => {
    setupMockWithStatus({
      stt: { engine: "MoonshineSTT", model: "tiny", state: "uninitialized", health: "degraded" },
    });
    render(<VoicePage />);

    await waitFor(() => {
      expect(screen.getByTestId("health-degraded")).toBeInTheDocument();
    });
    // The engine block still renders (it IS registered) — but not as healthy.
    expect(screen.queryByTestId("health-healthy")).not.toBeNull();
  });

  // ── LIVE-2 Phase 2 (P1-3) — pipeline latency truth ──

  it("renders measured pipeline latency when present", async () => {
    setupMockWithStatus({
      pipeline: { running: true, state: "idle", latency_ms: 137 },
    });
    render(<VoicePage />);
    await waitFor(() => {
      expect(screen.getByText("137ms")).toBeInTheDocument();
    });
  });

  it("explains why latency is unavailable instead of a bare dash", async () => {
    setupMockWithStatus({
      pipeline: { running: true, state: "idle", latency_ms: null },
    });
    render(<VoicePage />);
    await waitFor(() => {
      expect(
        screen.getByText(/No utterance processed yet/i),
      ).toBeInTheDocument();
    });
  });

  // ── LIVE-2 P1-7 / P1-8 — data-freshness honesty ──

  it("shows auto-refresh-paused when capture is not running (P1-7)", async () => {
    // VOICE_STATUS has no capture block → captureRunning false → the
    // circuit-breaker poller is disabled, so the snapshot is static.
    setupMockSuccess();
    render(<VoicePage />);
    await waitFor(() => {
      expect(
        screen.getByTestId("voice-freshness-paused"),
      ).toBeInTheDocument();
    });
  });

  it("shows live freshness when capture is running", async () => {
    setupMockWithStatus({
      capture: {
        running: true,
        input_device: 1,
        host_api: "WASAPI",
        sample_rate: 16000,
        frames_delivered: 5,
        last_rms_db: -40,
      },
    });
    render(<VoicePage />);
    await waitFor(() => {
      expect(screen.getByTestId("voice-freshness-live")).toBeInTheDocument();
    });
  });

  it("surfaces fetch-failed (showing stale data) when a refresh fails (P1-8)", async () => {
    // First /status succeeds (snapshot lands, capture stopped so the
    // poller stays disabled); the manual refresh's /status rejects. The
    // page must keep the stale snapshot AND flip to fetch_failed rather
    // than silently looking fresh (the audit's C-12 swallowed-error case).
    let statusCalls = 0;
    mockGet.mockImplementation((path: string) => {
      if (path === "/api/voice/status") {
        statusCalls += 1;
        if (statusCalls >= 2) return Promise.reject(new Error("boom"));
        return Promise.resolve(VOICE_STATUS);
      }
      if (path === "/api/voice/models") return Promise.resolve(VOICE_MODELS);
      if (path === "/api/voice/linux-mixer-diagnostics")
        return Promise.resolve({
          platform_supported: false,
          amixer_available: false,
          snapshots: [],
          aggregated_boost_db_ceiling: 18,
          saturation_ratio_ceiling: 0.5,
          reset_enabled_by_default: true,
        });
      if (path === "/api/voice/wake-word/status")
        return Promise.resolve({ minds: [] });
      return Promise.reject(new Error("unknown path"));
    });

    render(<VoicePage />);
    // Initial snapshot lands → paused (capture stopped).
    await waitFor(() => {
      expect(
        screen.getByTestId("voice-freshness-paused"),
      ).toBeInTheDocument();
    });

    // Trigger a manual refresh whose /status call rejects.
    fireEvent.click(screen.getByRole("button", { name: "Retry" }));
    await waitFor(() => {
      expect(
        screen.getByTestId("voice-freshness-fetch_failed"),
      ).toBeInTheDocument();
    });
    // The stale snapshot is still rendered — we didn't blank the page.
    expect(screen.getByText("MoonshineSTT")).toBeInTheDocument();
  });

  // ── v1.3 §4.3 L5a — LinuxMicGainCard surface on the Voice page ──

  it("renders LinuxMicGainCard with saturation alert when mixer is saturated", async () => {
    mockGet.mockImplementation((path: string) => {
      if (path === "/api/voice/status") return Promise.resolve(VOICE_STATUS);
      if (path === "/api/voice/models") return Promise.resolve(VOICE_MODELS);
      if (path === "/api/voice/linux-mixer-diagnostics") {
        return Promise.resolve({
          platform_supported: true,
          amixer_available: true,
          snapshots: [
            {
              card_index: 1,
              card_id: "Generic_1",
              card_longname: "HD-Audio Generic",
              aggregated_boost_db: 42.0,
              saturation_warning: true,
              controls: [
                {
                  name: "Internal Mic Boost",
                  min_raw: 0,
                  max_raw: 3,
                  current_raw: 3,
                  current_db: 36,
                  max_db: 36,
                  is_boost_control: true,
                  saturation_risk: true,
                  asymmetric: false,
                },
              ],
            },
          ],
          aggregated_boost_db_ceiling: 18.0,
          saturation_ratio_ceiling: 0.5,
          reset_enabled_by_default: true,
        });
      }
      return Promise.reject(new Error("unknown path"));
    });
    render(<VoicePage />);
    await waitFor(() => {
      expect(screen.getByTestId("linux-mic-gain-alert")).toBeInTheDocument();
      expect(
        screen.getByTestId("reset-linux-mic-gain-button"),
      ).toBeInTheDocument();
    });
  });

  it("hides LinuxMicGainCard entirely on non-Linux hosts", async () => {
    setupMockSuccess(); // default: platform_supported=false
    const { container } = render(<VoicePage />);
    // The card self-hides on non-Linux — wait for that state to settle.
    // A plain post-waitFor assertion would race the card's mount-time
    // fetch which briefly puts it into ``loading`` mode (rendering a
    // placeholder until the first response resolves).
    await waitFor(() => {
      expect(
        container.querySelector('[data-testid="linux-mic-gain-card"]'),
      ).toBeNull();
    });
  });
});

/* ── Mission MISSION-wake-word-ui §T5 — per-mind section ── */

import { useDashboardStore } from "@/stores/dashboard";

describe("VoicePage — per-mind wake-word section", () => {
  beforeEach(() => {
    mockGet.mockReset();
    // Reset the Zustand slice between tests so per-mind status from
    // a prior test doesn't leak into the next render.
    useDashboardStore.setState({
      perMindStatus: [],
      wakeWordLoading: false,
      wakeWordError: null,
    });
  });

  it("renders the empty state when no minds are on disk", async () => {
    setupMockSuccess({ minds: [] });
    render(<VoicePage />);
    await waitFor(() => {
      expect(screen.getByText("Per-Mind Wake Word")).toBeInTheDocument();
    });
    expect(screen.getByText(/No minds yet/)).toBeInTheDocument();
  });

  it("renders one card per mind in the response", async () => {
    setupMockSuccess();
    render(<VoicePage />);
    await waitFor(() => {
      expect(screen.getByText("aria")).toBeInTheDocument();
    });
    expect(screen.getByText("lucia")).toBeInTheDocument();
    expect(screen.getByText("joao")).toBeInTheDocument();
  });

  it("renders the registered status pill for healthy minds", async () => {
    setupMockSuccess();
    render(<VoicePage />);
    await waitFor(() => {
      // aria is the only mind with runtime_registered=true.
      expect(screen.getByText("Registered")).toBeInTheDocument();
    });
  });

  it("renders the error pill for NONE-strategy minds", async () => {
    setupMockSuccess();
    render(<VoicePage />);
    await waitFor(() => {
      // lucia has resolution_strategy=none + wake_word_enabled=true.
      expect(screen.getByText("Configuration error")).toBeInTheDocument();
    });
  });

  it("expands the error-details disclosure with the resolver remediation", async () => {
    setupMockSuccess();
    render(<VoicePage />);
    await waitFor(() => {
      expect(screen.getByText("View error details")).toBeInTheDocument();
    });
    // The remediation text is inside a <details> element — present in
    // the DOM even before the disclosure is expanded.
    expect(
      screen.getByText(/train via .sovyx voice train-wake-word./),
    ).toBeInTheDocument();
  });

  it("renders the Train this wake word button for NONE-strategy minds (Mission v0.30.0 §T1.4)", async () => {
    // The broken-state mind (lucia in the default fixture) has
    // wake_word_enabled=true + resolution_strategy=none → button shows.
    setupMockSuccess();
    render(<VoicePage />);
    await waitFor(() => {
      expect(screen.getByText("Train this wake word")).toBeInTheDocument();
    });
  });

  it("does NOT render the Train button for healthy or disabled minds", async () => {
    // Filter to only the healthy + disabled cases (no NONE-strategy mind).
    setupMockSuccess({
      minds: [PER_MIND_WAKE_WORD_HEALTHY, PER_MIND_WAKE_WORD_DISABLED],
    });
    render(<VoicePage />);
    await waitFor(() => {
      expect(screen.getByText("aria")).toBeInTheDocument();
    });
    // Train button should NOT appear because no mind matches the
    // broken-state predicate.
    expect(screen.queryByText("Train this wake word")).toBeNull();
  });

  it("renders the phonetic-match disclosure for PHONETIC strategy entries", async () => {
    // Mission MISSION-v0.29.1-tightening §T1: PHONETIC matches
    // surface "Matched as <file>.onnx (distance: N)" so operators
    // see WHICH file matched their diacritic / phonetic wake word.
    setupMockSuccess({
      minds: [PER_MIND_WAKE_WORD_PHONETIC, PER_MIND_WAKE_WORD_DISABLED],
    });
    render(<VoicePage />);
    await waitFor(() => {
      expect(screen.getByText("lucia")).toBeInTheDocument();
    });
    // i18n template populated with file = "lucia.onnx" + distance = 0.
    expect(screen.getByText(/Matched as lucia\.onnx/)).toBeInTheDocument();
    expect(screen.getByText(/distance: 0/)).toBeInTheDocument();
  });

  it("does NOT render the phonetic-match disclosure for EXACT strategy entries", async () => {
    setupMockSuccess({ minds: [PER_MIND_WAKE_WORD_HEALTHY] });
    render(<VoicePage />);
    await waitFor(() => {
      expect(screen.getByText("aria")).toBeInTheDocument();
    });
    // EXACT case is redundant with file name; disclosure should NOT render.
    expect(screen.queryByText(/Matched as/)).toBeNull();
  });
});

/* ── LIVE-2 P1-7 / P1-8 — freshness classification (pure, no timers) ── */

describe("computeVoiceFreshness", () => {
  it("returns live when capture is running and polls succeed", () => {
    expect(
      computeVoiceFreshness({
        fetchError: false,
        captureRunning: true,
        consecutive5xx: 0,
      }),
    ).toBe("live");
  });

  it("returns paused when capture is not running (poller disabled)", () => {
    expect(
      computeVoiceFreshness({
        fetchError: false,
        captureRunning: false,
        consecutive5xx: 0,
      }),
    ).toBe("paused");
  });

  it("returns poll_stale when capture runs but polls are failing", () => {
    expect(
      computeVoiceFreshness({
        fetchError: false,
        captureRunning: true,
        consecutive5xx: 3,
      }),
    ).toBe("poll_stale");
  });

  it("returns fetch_failed when the last full fetch errored", () => {
    expect(
      computeVoiceFreshness({
        fetchError: true,
        captureRunning: true,
        consecutive5xx: 0,
      }),
    ).toBe("fetch_failed");
  });

  it("prioritises fetch_failed over paused and poll_stale", () => {
    // A failed fetch is the most actionable signal — it wins even when
    // capture is stopped and polls are also failing.
    expect(
      computeVoiceFreshness({
        fetchError: true,
        captureRunning: false,
        consecutive5xx: 9,
      }),
    ).toBe("fetch_failed");
  });

  it("prioritises paused over poll_stale", () => {
    expect(
      computeVoiceFreshness({
        fetchError: false,
        captureRunning: false,
        consecutive5xx: 9,
      }),
    ).toBe("paused");
  });
});
