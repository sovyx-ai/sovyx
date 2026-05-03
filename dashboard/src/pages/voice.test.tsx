/**
 * Voice page tests — TASK-204
 *
 * Tests: render, loading state, error state, populated data, model matrix.
 */

import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor } from "@/test/test-utils";
import VoicePage from "./voice";

/* ── Mock API ── */

const mockGet = vi.fn();

vi.mock("@/lib/api", () => ({
  api: { get: (...args: unknown[]) => mockGet(...args) },
  isAbortError: (err: unknown) =>
    err instanceof DOMException && (err as DOMException).name === "AbortError",
}));

/* ── Fixtures ── */

const VOICE_STATUS = {
  pipeline: { running: true, state: "idle", latency_ms: 42 },
  stt: { engine: "MoonshineSTT", model: "moonshine-tiny", state: "ready" },
  tts: { engine: "PiperTTS", model: "en_US-lessac-medium", initialized: true },
  wake_word: { enabled: true, phrase: "hey sovyx" },
  vad: { enabled: true },
  wyoming: { connected: false, endpoint: null },
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

  it("renders Wyoming disconnected status", async () => {
    setupMockSuccess();
    render(<VoicePage />);
    await waitFor(() => {
      expect(screen.getByText("Disconnected")).toBeInTheDocument();
    });
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
});
