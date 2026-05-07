/**
 * Regression guard for :class:`VoiceStep` → ``POST /api/voice/enable``.
 *
 * The voice-language-coherence bug (v0.14 timeline) manifested in two
 * layers: the backend factory ignored ``MindConfig.voice_id`` (fixed in
 * Phase 3), and the wizard UI never told the backend about the live
 * pick to begin with. These tests lock in the second half — the
 * ``enable`` POST must carry the ``voice_id`` + ``language`` surfaced
 * by :class:`HardwareDetection` via ``onVoiceChange``.
 */
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor, fireEvent } from "@testing-library/react";
// Initialize i18n synchronously so VoiceStep's useTranslation finds
// resources. v0.30.4 migrated the wizard opt-in strings to t() calls.
import "@/lib/i18n";
import { VoiceStep } from "./VoiceStep";

const mockGet = vi.fn();
const mockPost = vi.fn();

vi.mock("@/lib/api", () => ({
  api: {
    get: (...args: unknown[]) => mockGet(...args),
    post: (...args: unknown[]) => mockPost(...args),
  },
  ApiError: class ApiError extends Error {
    constructor(
      public status: number,
      message: string,
    ) {
      super(message);
    }
  },
}));

const hardwareInfo = {
  hardware: {
    cpu_cores: 8,
    ram_mb: 16384,
    has_gpu: true,
    gpu_vram_mb: 8192,
    tier: "DESKTOP_GPU",
  },
  audio: {
    available: true,
    input_devices: [{ index: 0, name: "Mic", is_default: true }],
    output_devices: [{ index: 1, name: "Speakers", is_default: true }],
  },
  recommended_models: [],
  total_download_mb: 0,
};

const voiceCatalog = {
  supported_languages: ["en-us", "pt-br"],
  by_language: {
    "en-us": [
      { id: "af_heart", display_name: "Heart", language: "en-us", gender: "female" },
    ],
    "pt-br": [
      { id: "pf_dora", display_name: "Dora", language: "pt-br", gender: "female" },
    ],
  },
  recommended_per_language: {
    "en-us": "af_heart",
    "pt-br": "pf_dora",
  },
};

const modelsStatus = {
  model_dir: "/tmp",
  all_installed: true,
  missing_count: 0,
  missing_download_mb: 0,
  models: [],
};

function stubGets() {
  mockGet.mockImplementation((url: string) => {
    if (url === "/api/voice/hardware-detect") return Promise.resolve(hardwareInfo);
    if (url === "/api/voice/voices") return Promise.resolve(voiceCatalog);
    if (url === "/api/voice/models/status") return Promise.resolve(modelsStatus);
    return Promise.reject(new Error(`unexpected GET ${url}`));
  });
}

beforeEach(() => {
  mockGet.mockReset();
  mockPost.mockReset();
  // Default for telemetry POSTs from VoiceSetupWizard (Mission v0.30.1
  // §T1.2). When VoiceStep mounts the wizard inline, the wizard's
  // step_dwell + completion telemetry calls api.post; without a
  // default, undefined.catch() crashes the cleanup hook on unmount.
  mockPost.mockResolvedValue({ ok: true });
});

describe("VoiceStep", () => {
  it("forwards voice_id + language in POST /api/voice/enable", async () => {
    stubGets();
    mockPost.mockResolvedValue({ ok: true, status: "active", tts_engine: "kokoro" });

    render(
      <VoiceStep
        onConfigured={() => {}}
        onSkip={() => {}}
        language="pt"
      />,
    );

    // Wait for hardware-detect to settle + catalog seed to fire.
    await screen.findByText(/8 cores/i);
    await waitFor(() => {
      const langSelect = screen.getByLabelText(/voice-test language/i) as HTMLSelectElement;
      expect(langSelect.value).toBe("pt-br");
    });

    // Click "Enable Voice" — the button only renders after detection.
    const enableBtn = await screen.findByRole("button", { name: /enable voice/i });
    fireEvent.click(enableBtn);

    await waitFor(() => {
      expect(mockPost).toHaveBeenCalledTimes(1);
    });

    const [path, body] = mockPost.mock.calls[0];
    expect(path).toBe("/api/voice/enable");
    expect(body).toMatchObject({
      input_device: 0,
      output_device: 1,
      voice_id: "pf_dora",
      language: "pt-br",
    });
  });

  it("omits voice_id when the catalog never resolved", async () => {
    // Catalog fails → onVoiceChange never fires → body has no voice_id.
    // Backend still boots from MindConfig — that's the intended fallback.
    mockGet.mockImplementation((url: string) => {
      if (url === "/api/voice/hardware-detect") return Promise.resolve(hardwareInfo);
      if (url === "/api/voice/voices") return Promise.reject(new Error("offline"));
      if (url === "/api/voice/models/status") return Promise.resolve(modelsStatus);
      return Promise.reject(new Error(`unexpected GET ${url}`));
    });
    mockPost.mockResolvedValue({ ok: true, status: "active", tts_engine: "kokoro" });

    render(<VoiceStep onConfigured={() => {}} onSkip={() => {}} />);
    await screen.findByText(/8 cores/i);

    const enableBtn = await screen.findByRole("button", { name: /enable voice/i });
    fireEvent.click(enableBtn);

    await waitFor(() => {
      expect(mockPost).toHaveBeenCalledTimes(1);
    });
    const [, body] = mockPost.mock.calls[0];
    expect(body).toEqual({ input_device: 0, output_device: 1 });
  });

  it("does not mount the wizard by default (opt-in affordance)", async () => {
    stubGets();
    render(<VoiceStep onConfigured={() => {}} onSkip={() => {}} />);
    await screen.findByText(/8 cores/i);
    // Button is rendered, but wizard surface (its testid) is not.
    expect(
      screen.getByRole("button", { name: /open setup wizard/i }),
    ).toBeInTheDocument();
    expect(screen.queryByTestId("voice-setup-wizard")).not.toBeInTheDocument();
  });

  it("mounts wizard inline when 'Test microphone' is clicked", async () => {
    // Wizard's own mount fetches /api/voice/wizard/devices on mount —
    // stub it so the component's loading effect resolves.
    mockGet.mockImplementation((url: string) => {
      if (url === "/api/voice/hardware-detect") return Promise.resolve(hardwareInfo);
      if (url === "/api/voice/voices") return Promise.resolve(voiceCatalog);
      if (url === "/api/voice/models/status") return Promise.resolve(modelsStatus);
      if (url === "/api/voice/wizard/devices") return Promise.resolve({ devices: [] });
      return Promise.reject(new Error(`unexpected GET ${url}`));
    });
    render(<VoiceStep onConfigured={() => {}} onSkip={() => {}} />);
    await screen.findByText(/8 cores/i);

    const testBtn = screen.getByRole("button", { name: /open setup wizard/i });
    fireEvent.click(testBtn);

    await waitFor(() => {
      expect(screen.getByTestId("voice-setup-wizard")).toBeInTheDocument();
    });
    // The opt-in button is gone while the wizard is open.
    expect(
      screen.queryByRole("button", { name: /open setup wizard/i }),
    ).not.toBeInTheDocument();
  });
});

// ════════════════════════════════════════════════════════════════════
// rc.4 (Agent 3 #8) — VoiceStep single-flow conditional coverage.
// Pre-rc.4 the conditional at VoiceStep.tsx:212 had no vitest coverage:
// the existing tests above only exercise the calibrationWizardEnabled=
// false branch (default store state). A regression that re-introduced
// dual-mount of <HardwareDetection /> + <VoiceCalibrationStep /> would
// land green. These tests assert the contract: EXACTLY ONE branch
// mounts, never both.
// ════════════════════════════════════════════════════════════════════

import { useDashboardStore } from "@/stores/dashboard";

// Replace VoiceCalibrationStep with a sentinel so we can assert mount
// presence without pulling in its complex side effects (fingerprint
// fetch, websocket, etc). Same pattern as recalibrate-button.test.tsx.
vi.mock("@/components/onboarding/VoiceCalibrationStep", () => ({
  VoiceCalibrationStep: () => (
    <div data-testid="voice-calibration-step-sentinel">calibration-step</div>
  ),
}));

/** Compose stubGets() with the calibration feature-flag endpoint
 * stubbed to a specific response so VoiceStep's mount-time
 * loadCalibrationFeatureFlag() resolves to the desired value (instead
 * of a default rejection that nukes our preloaded state).
 */
function stubGetsWithCalibrationFlag(flag: {
  enabled: boolean;
  runtime_override_active?: boolean;
  platform_supported?: boolean;
}) {
  mockGet.mockImplementation((url: string) => {
    if (url === "/api/voice/hardware-detect") return Promise.resolve(hardwareInfo);
    if (url === "/api/voice/voices") return Promise.resolve(voiceCatalog);
    if (url === "/api/voice/models/status") return Promise.resolve(modelsStatus);
    if (url === "/api/voice/calibration/feature-flag") return Promise.resolve(flag);
    if (url === "/api/voice/wizard/devices") return Promise.resolve({ devices: [] });
    return Promise.reject(new Error(`unexpected GET ${url}`));
  });
}

describe("VoiceStep — single-flow conditional (rc.4 Agent 3 #8)", () => {
  beforeEach(() => {
    // Reset the calibration slice's feature-flag state between tests.
    useDashboardStore.setState({ calibrationFeatureFlag: null });
  });

  it("renders <HardwareDetection /> + does NOT render <VoiceCalibrationStep /> when flag is OFF", async () => {
    // Mount-time loadCalibrationFeatureFlag resolves to enabled=false.
    stubGetsWithCalibrationFlag({ enabled: false, runtime_override_active: false });
    render(<VoiceStep onConfigured={() => {}} onSkip={() => {}} />);

    // Hardware detection runs and renders the cores summary.
    await screen.findByText(/8 cores/i);

    // The legacy "Open setup wizard" affordance is present (legacy path).
    expect(
      screen.getByRole("button", { name: /open setup wizard/i }),
    ).toBeInTheDocument();

    // The calibration step sentinel is NOT mounted.
    expect(
      screen.queryByTestId("voice-calibration-step-sentinel"),
    ).not.toBeInTheDocument();
  });

  it("renders <VoiceCalibrationStep /> + does NOT render <HardwareDetection /> when flag is ON", async () => {
    // Mount-time loadCalibrationFeatureFlag resolves to enabled=true.
    // We also pre-set the store so the FIRST render already sees the
    // flag as enabled (otherwise the first paint would briefly show the
    // legacy branch, and our absence assertion below would race).
    useDashboardStore.setState({
      calibrationFeatureFlag: { enabled: true, runtime_override_active: false },
    });
    stubGetsWithCalibrationFlag({ enabled: true, runtime_override_active: false });

    render(<VoiceStep onConfigured={() => {}} onSkip={() => {}} />);

    // Calibration step sentinel mounts.
    await waitFor(() => {
      expect(
        screen.getByTestId("voice-calibration-step-sentinel"),
      ).toBeInTheDocument();
    });

    // HardwareDetection's "8 cores" summary is NOT shown — the
    // conditional took the calibration branch and the legacy component
    // never mounts.
    expect(screen.queryByText(/8 cores/i)).not.toBeInTheDocument();

    // The legacy "Open setup wizard" affordance is also NOT mounted.
    expect(
      screen.queryByRole("button", { name: /open setup wizard/i }),
    ).not.toBeInTheDocument();
  });

  it("never dual-mounts both branches under any feature-flag state", async () => {
    // Flag ON path.
    useDashboardStore.setState({
      calibrationFeatureFlag: { enabled: true, runtime_override_active: false },
    });
    stubGetsWithCalibrationFlag({ enabled: true, runtime_override_active: false });

    const { unmount } = render(<VoiceStep onConfigured={() => {}} onSkip={() => {}} />);
    await waitFor(() => {
      expect(
        screen.getByTestId("voice-calibration-step-sentinel"),
      ).toBeInTheDocument();
    });
    // Hardware detection sub-strings are absent.
    expect(screen.queryByText(/8 cores/i)).not.toBeInTheDocument();
    unmount();

    // Flag OFF path (separate render with reset state).
    useDashboardStore.setState({ calibrationFeatureFlag: null });
    stubGetsWithCalibrationFlag({ enabled: false, runtime_override_active: false });
    render(<VoiceStep onConfigured={() => {}} onSkip={() => {}} />);
    await screen.findByText(/8 cores/i);
    expect(
      screen.queryByTestId("voice-calibration-step-sentinel"),
    ).not.toBeInTheDocument();
  });
});

// ════════════════════════════════════════════════════════════════════
// rc.11 (EIXO 2) — VoiceStep cross-platform gate.
// Pre-rc.11, ``platform_supported`` did not exist; the calibration
// wizard mounted on every platform when ``enabled`` was true. On
// Win/macOS that meant operators clicked Start and silently fell
// through to FALLBACK from DiagPrerequisiteError. rc.11 adds the
// platform_supported field to the feature-flag response and gates
// mount on (enabled AND platform_supported), surfacing the limitation
// upfront via a banner that points to the cross-platform fallback.
// ════════════════════════════════════════════════════════════════════

describe("VoiceStep — cross-platform gate (rc.11 EIXO 2)", () => {
  beforeEach(() => {
    useDashboardStore.setState({ calibrationFeatureFlag: null });
  });

  it("does NOT mount the calibration wizard when platform_supported=false", async () => {
    // Operator intent (enabled=true) but daemon is on Win/macOS.
    useDashboardStore.setState({
      calibrationFeatureFlag: {
        enabled: true,
        runtime_override_active: false,
        platform_supported: false,
      },
    });
    stubGetsWithCalibrationFlag({
      enabled: true,
      runtime_override_active: false,
      platform_supported: false,
    });

    render(<VoiceStep onConfigured={() => {}} onSkip={() => {}} />);

    // Legacy HardwareDetection mounts (cross-platform fallback path).
    await screen.findByText(/8 cores/i);

    // The calibration step sentinel does NOT mount.
    expect(
      screen.queryByTestId("voice-calibration-step-sentinel"),
    ).not.toBeInTheDocument();
  });

  it("renders the platform-unsupported banner when enabled but unsupported", async () => {
    useDashboardStore.setState({
      calibrationFeatureFlag: {
        enabled: true,
        runtime_override_active: false,
        platform_supported: false,
      },
    });
    stubGetsWithCalibrationFlag({
      enabled: true,
      runtime_override_active: false,
      platform_supported: false,
    });

    render(<VoiceStep onConfigured={() => {}} onSkip={() => {}} />);
    await screen.findByText(/8 cores/i);

    // The banner pointing the operator at the simple setup is present.
    expect(
      screen.getByTestId("voice-calibration-platform-unsupported"),
    ).toBeInTheDocument();
  });

  it("does NOT render the banner when platform_supported=true", async () => {
    useDashboardStore.setState({
      calibrationFeatureFlag: {
        enabled: true,
        runtime_override_active: false,
        platform_supported: true,
      },
    });
    stubGetsWithCalibrationFlag({
      enabled: true,
      runtime_override_active: false,
      platform_supported: true,
    });

    render(<VoiceStep onConfigured={() => {}} onSkip={() => {}} />);
    await waitFor(() => {
      expect(
        screen.getByTestId("voice-calibration-step-sentinel"),
      ).toBeInTheDocument();
    });
    // Banner is gated on (enabled && !platform_supported); should be absent.
    expect(
      screen.queryByTestId("voice-calibration-platform-unsupported"),
    ).not.toBeInTheDocument();
  });

  it("does NOT render the banner when enabled=false (no operator intent)", async () => {
    // platform_supported=false but operator never asked for the wizard
    // — no banner needed; the legacy flow is what they expected anyway.
    useDashboardStore.setState({
      calibrationFeatureFlag: {
        enabled: false,
        runtime_override_active: false,
        platform_supported: false,
      },
    });
    stubGetsWithCalibrationFlag({
      enabled: false,
      runtime_override_active: false,
      platform_supported: false,
    });

    render(<VoiceStep onConfigured={() => {}} onSkip={() => {}} />);
    await screen.findByText(/8 cores/i);

    expect(
      screen.queryByTestId("voice-calibration-platform-unsupported"),
    ).not.toBeInTheDocument();
  });

  it("treats missing platform_supported as true (pre-rc.11 daemon back-compat)", async () => {
    // Older daemon doesn't ship the field. zod schema defaults to true,
    // preserving legacy single-platform behaviour: enabled=true mounts
    // the wizard exactly as it did before.
    useDashboardStore.setState({
      calibrationFeatureFlag: {
        enabled: true,
        runtime_override_active: false,
      },
    });
    stubGetsWithCalibrationFlag({
      enabled: true,
      runtime_override_active: false,
    });

    render(<VoiceStep onConfigured={() => {}} onSkip={() => {}} />);
    await waitFor(() => {
      expect(
        screen.getByTestId("voice-calibration-step-sentinel"),
      ).toBeInTheDocument();
    });
    expect(
      screen.queryByTestId("voice-calibration-platform-unsupported"),
    ).not.toBeInTheDocument();
  });
});
