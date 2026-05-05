/**
 * VoiceSetupWizard tests — Mission v0.30.0 §T2.4.
 *
 * Covers the 5-step state machine: devices → record → results
 * (with retry path) → save → done. Each step's transition rule is
 * pinned by an explicit assertion to guard against the reducer
 * drifting in future refactors.
 */
import { describe, it, expect, beforeEach, vi } from "vitest";
import { render, screen, fireEvent, waitFor } from "@/test/test-utils";

import { VoiceSetupWizard } from "./VoiceSetupWizard";

const mockGet = vi.fn();
const mockPost = vi.fn();

vi.mock("@/lib/api", () => ({
  api: {
    get: (...args: unknown[]) => mockGet(...args),
    post: (...args: unknown[]) => mockPost(...args),
  },
}));

const DEVICES_RESPONSE = {
  devices: [
    {
      device_id: "dev-1",
      name: "Microphone Array",
      friendly_name: "Built-in Microphone",
      max_input_channels: 2,
      default_sample_rate: 48000,
      is_default: true,
      diagnosis_hint: "ready",
    },
    {
      device_id: "dev-2",
      name: "USB Mic",
      friendly_name: "Razer Seiren",
      max_input_channels: 1,
      default_sample_rate: 16000,
      is_default: false,
      diagnosis_hint: "warning_low_channels",
    },
  ],
  total_count: 2,
  default_device_id: "dev-1",
};

const TEST_RESULT_OK = {
  session_id: "s-1",
  success: true,
  duration_actual_s: 3.0,
  sample_rate_hz: 16000,
  level_rms_dbfs: -20.5,
  level_peak_dbfs: -8.0,
  snr_db: 22.0,
  clipping_detected: false,
  silent_capture: false,
  diagnosis: "ok",
};

const DIAGNOSTIC_RESPONSE = {
  platform: "win32",
  voice_clarity_active: false,
  active_capture_device: "Built-in Microphone",
  ready: true,
  recommendations: [] as string[],
};

beforeEach(() => {
  mockGet.mockReset();
  mockPost.mockReset();
  // Default for telemetry POSTs (Mission v0.30.1 §T1.2) — wizard now
  // emits step_dwell + completion via api.post on every transition;
  // tests that exercise specific endpoints override this with their
  // own mockResolvedValueOnce. Without a default, every transition
  // would crash on .catch() since the unstubbed mock returns undefined.
  mockPost.mockResolvedValue({});
});

describe("VoiceSetupWizard — devices step", () => {
  it("renders the loading state on mount", () => {
    mockGet.mockReturnValue(new Promise(() => {})); // never resolves
    render(<VoiceSetupWizard />);
    expect(screen.getByText("Detecting microphones…")).toBeInTheDocument();
  });

  it("renders device list after fetch resolves", async () => {
    mockGet.mockImplementation((path: string) => {
      if (path === "/api/voice/wizard/devices") return Promise.resolve(DEVICES_RESPONSE);
      return Promise.reject(new Error("unknown path"));
    });
    render(<VoiceSetupWizard />);
    await waitFor(() => {
      expect(screen.getByText("Built-in Microphone")).toBeInTheDocument();
    });
    expect(screen.getByText("Razer Seiren")).toBeInTheDocument();
  });

  it("transitions to record step on device click", async () => {
    mockGet.mockResolvedValueOnce(DEVICES_RESPONSE);
    render(<VoiceSetupWizard />);
    await waitFor(() => {
      expect(screen.getByText("Built-in Microphone")).toBeInTheDocument();
    });
    fireEvent.click(screen.getByText("Built-in Microphone"));
    expect(screen.getByText(/Step 2 of 4/)).toBeInTheDocument();
  });
});

describe("VoiceSetupWizard — record + results flow", () => {
  it("records → results → diagnosis OK → advance enabled", async () => {
    mockGet.mockImplementation((path: string) => {
      if (path === "/api/voice/wizard/devices") return Promise.resolve(DEVICES_RESPONSE);
      if (path === "/api/voice/wizard/diagnostic") return Promise.resolve(DIAGNOSTIC_RESPONSE);
      return Promise.reject(new Error("unknown path"));
    });
    // Discriminate by URL — telemetry POSTs (Mission v0.30.1 §T1.2)
    // share the mock with /test-record, so a once-pin would race.
    mockPost.mockImplementation((path: string) => {
      if (path === "/api/voice/wizard/test-record") {
        return Promise.resolve(TEST_RESULT_OK);
      }
      return Promise.resolve({});
    });

    render(<VoiceSetupWizard />);
    await waitFor(() => {
      expect(screen.getByText("Built-in Microphone")).toBeInTheDocument();
    });
    fireEvent.click(screen.getByText("Built-in Microphone"));
    fireEvent.click(screen.getByText("Start 3-second recording"));

    await waitFor(() => {
      expect(screen.getByText("Recording looks good.")).toBeInTheDocument();
    });
    expect(screen.getByText(/-20.5 dBFS/)).toBeInTheDocument();
    expect(screen.getByText("Save selection")).toBeInTheDocument();
  });

  it("renders backend diagnosis_hint as actionable paragraph for non-ok diagnoses (T2.7)", async () => {
    // Mission MISSION-voice-linux-silent-mic-remediation-2026-05-04
    // §Phase 2 T2.7 — when the backend returns a non-empty
    // diagnosis_hint on a failure verdict, the results step MUST
    // render it as a paragraph below the diagnosis label. Pre-T2.7
    // the hint was emitted but never rendered, so operators saw
    // only the short i18n label and had no actionable next step.
    const TEST_RESULT_NO_AUDIO_LINUX = {
      session_id: "s-2",
      success: true,
      duration_actual_s: 3.0,
      sample_rate_hz: 16000,
      level_rms_dbfs: -83.0,
      level_peak_dbfs: -78.0,
      snr_db: null,
      clipping_detected: false,
      silent_capture: true,
      diagnosis: "no_audio",
      diagnosis_hint:
        "No usable signal captured. On Linux+PipeWire this is " +
        "almost always either an ALSA mixer state issue OR a " +
        "WirePlumber default-source routing issue. Run amixer + wpctl.",
    };
    mockGet.mockImplementation((path: string) => {
      if (path === "/api/voice/wizard/devices") return Promise.resolve(DEVICES_RESPONSE);
      if (path === "/api/voice/wizard/diagnostic") return Promise.resolve(DIAGNOSTIC_RESPONSE);
      return Promise.reject(new Error("unknown path"));
    });
    mockPost.mockImplementation((path: string) => {
      if (path === "/api/voice/wizard/test-record") {
        return Promise.resolve(TEST_RESULT_NO_AUDIO_LINUX);
      }
      return Promise.resolve({});
    });

    render(<VoiceSetupWizard />);
    await waitFor(() => {
      expect(screen.getByText("Built-in Microphone")).toBeInTheDocument();
    });
    fireEvent.click(screen.getByText("Built-in Microphone"));
    fireEvent.click(screen.getByText("Start 3-second recording"));

    // The actionable hint paragraph should appear below the diagnosis
    // label. We assert on the unique substring the backend embeds for
    // Linux platforms.
    await waitFor(() => {
      expect(screen.getByText(/amixer.*wpctl/i)).toBeInTheDocument();
    });
  });

  it("does NOT render diagnosis_hint paragraph for ok diagnosis (T2.7)", async () => {
    // OK verdict carries a hint too ("Microphone looks good.") but
    // the success badge already conveys the message — rendering both
    // would be visually noisy. Guard: hint paragraph hidden when isOk.
    mockGet.mockImplementation((path: string) => {
      if (path === "/api/voice/wizard/devices") return Promise.resolve(DEVICES_RESPONSE);
      if (path === "/api/voice/wizard/diagnostic") return Promise.resolve(DIAGNOSTIC_RESPONSE);
      return Promise.reject(new Error("unknown path"));
    });
    const TEST_RESULT_OK_WITH_HINT = {
      ...TEST_RESULT_OK,
      diagnosis_hint: "Microphone looks good.",
    };
    mockPost.mockImplementation((path: string) => {
      if (path === "/api/voice/wizard/test-record") {
        return Promise.resolve(TEST_RESULT_OK_WITH_HINT);
      }
      return Promise.resolve({});
    });

    render(<VoiceSetupWizard />);
    await waitFor(() => {
      expect(screen.getByText("Built-in Microphone")).toBeInTheDocument();
    });
    fireEvent.click(screen.getByText("Built-in Microphone"));
    fireEvent.click(screen.getByText("Start 3-second recording"));

    await waitFor(() => {
      expect(screen.getByText("Recording looks good.")).toBeInTheDocument();
    });
    // Hint paragraph should be absent for ok verdict.
    expect(screen.queryByText("Microphone looks good.")).not.toBeInTheDocument();
  });

  it("retry returns from results → record", async () => {
    mockGet.mockImplementation((path: string) => {
      if (path === "/api/voice/wizard/devices") return Promise.resolve(DEVICES_RESPONSE);
      if (path === "/api/voice/wizard/diagnostic") return Promise.resolve(DIAGNOSTIC_RESPONSE);
      return Promise.reject(new Error("unknown path"));
    });
    // Discriminate by URL — telemetry POSTs (Mission v0.30.1 §T1.2)
    // share the mock with /test-record, so a once-pin would race.
    mockPost.mockImplementation((path: string) => {
      if (path === "/api/voice/wizard/test-record") {
        return Promise.resolve(TEST_RESULT_OK);
      }
      return Promise.resolve({});
    });

    render(<VoiceSetupWizard />);
    await waitFor(() => {
      expect(screen.getByText("Built-in Microphone")).toBeInTheDocument();
    });
    fireEvent.click(screen.getByText("Built-in Microphone"));
    fireEvent.click(screen.getByText("Start 3-second recording"));
    await waitFor(() => {
      expect(screen.getByText("Try again")).toBeInTheDocument();
    });
    fireEvent.click(screen.getByText("Try again"));
    expect(screen.getByText(/Step 2 of 4/)).toBeInTheDocument();
  });
});

describe("VoiceSetupWizard — A/B telemetry (Mission v0.30.1 §T1.2)", () => {
  it("emits step_dwell on step transition", async () => {
    mockGet.mockResolvedValueOnce(DEVICES_RESPONSE);
    render(<VoiceSetupWizard />);
    await waitFor(() => {
      expect(screen.getByText("Built-in Microphone")).toBeInTheDocument();
    });
    // devices → record transition fires step_dwell for "devices".
    fireEvent.click(screen.getByText("Built-in Microphone"));

    await waitFor(() => {
      const telemetryCalls = mockPost.mock.calls.filter(
        ([path]) => path === "/api/voice/wizard/telemetry",
      );
      expect(telemetryCalls.length).toBeGreaterThanOrEqual(1);
      const [, body] = telemetryCalls[0];
      expect(body.event).toBe("step_dwell");
      expect(body.step).toBe("devices");
      expect(typeof body.duration_ms).toBe("number");
      expect(body.duration_ms).toBeGreaterThanOrEqual(0);
    });
  });

  it("emits completion=abandoned on unmount before reaching done", async () => {
    mockGet.mockResolvedValueOnce(DEVICES_RESPONSE);
    const { unmount } = render(<VoiceSetupWizard />);
    await waitFor(() => {
      expect(screen.getByText("Built-in Microphone")).toBeInTheDocument();
    });
    unmount();

    await waitFor(() => {
      const completionCalls = mockPost.mock.calls.filter(
        ([path, body]) =>
          path === "/api/voice/wizard/telemetry" &&
          body.event === "completion",
      );
      expect(completionCalls.length).toBe(1);
      const [, body] = completionCalls[0];
      expect(body.outcome).toBe("abandoned");
      expect(body.exit_step).toBe("devices");
    });
  });

  it("emits completion=completed when wizard reaches done", async () => {
    mockGet.mockImplementation((path: string) => {
      if (path === "/api/voice/wizard/devices") return Promise.resolve(DEVICES_RESPONSE);
      if (path === "/api/voice/wizard/diagnostic") return Promise.resolve(DIAGNOSTIC_RESPONSE);
      return Promise.reject(new Error("unknown path"));
    });
    mockPost.mockImplementation((path: string) => {
      if (path === "/api/voice/wizard/test-record") {
        return Promise.resolve(TEST_RESULT_OK);
      }
      return Promise.resolve({});
    });

    render(<VoiceSetupWizard />);
    await waitFor(() => {
      expect(screen.getByText("Built-in Microphone")).toBeInTheDocument();
    });
    fireEvent.click(screen.getByText("Built-in Microphone"));
    fireEvent.click(screen.getByText("Start 3-second recording"));
    await waitFor(() => {
      expect(screen.getByText("Save selection")).toBeInTheDocument();
    });
    fireEvent.click(screen.getByText("Save selection"));
    fireEvent.click(screen.getByText("Save"));

    await waitFor(() => {
      const completionCalls = mockPost.mock.calls.filter(
        ([path, body]) =>
          path === "/api/voice/wizard/telemetry" &&
          body.event === "completion" &&
          body.outcome === "completed",
      );
      expect(completionCalls.length).toBe(1);
      expect(completionCalls[0][1].exit_step).toBe("done");
    });
  });
});

describe("VoiceSetupWizard — save + done", () => {
  it("save → done + onComplete fires with deviceId", async () => {
    mockGet.mockImplementation((path: string) => {
      if (path === "/api/voice/wizard/devices") return Promise.resolve(DEVICES_RESPONSE);
      if (path === "/api/voice/wizard/diagnostic") return Promise.resolve(DIAGNOSTIC_RESPONSE);
      return Promise.reject(new Error("unknown path"));
    });
    // Discriminate by URL — telemetry POSTs (Mission v0.30.1 §T1.2)
    // share the mock with /test-record, so a once-pin would race.
    mockPost.mockImplementation((path: string) => {
      if (path === "/api/voice/wizard/test-record") {
        return Promise.resolve(TEST_RESULT_OK);
      }
      return Promise.resolve({});
    });

    const onComplete = vi.fn();
    render(<VoiceSetupWizard onComplete={onComplete} />);
    await waitFor(() => {
      expect(screen.getByText("Built-in Microphone")).toBeInTheDocument();
    });
    fireEvent.click(screen.getByText("Built-in Microphone"));
    fireEvent.click(screen.getByText("Start 3-second recording"));
    await waitFor(() => {
      expect(screen.getByText("Save selection")).toBeInTheDocument();
    });
    fireEvent.click(screen.getByText("Save selection"));
    expect(screen.getByText("Save")).toBeInTheDocument(); // step 4
    fireEvent.click(screen.getByText("Save"));
    expect(screen.getByText(/All set/)).toBeInTheDocument();
    expect(onComplete).toHaveBeenCalledWith("dev-1");
  });
});
