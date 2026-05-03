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
    mockPost.mockResolvedValueOnce(TEST_RESULT_OK);

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

  it("retry returns from results → record", async () => {
    mockGet.mockImplementation((path: string) => {
      if (path === "/api/voice/wizard/devices") return Promise.resolve(DEVICES_RESPONSE);
      if (path === "/api/voice/wizard/diagnostic") return Promise.resolve(DIAGNOSTIC_RESPONSE);
      return Promise.reject(new Error("unknown path"));
    });
    mockPost.mockResolvedValueOnce(TEST_RESULT_OK);

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

describe("VoiceSetupWizard — save + done", () => {
  it("save → done + onComplete fires with deviceId", async () => {
    mockGet.mockImplementation((path: string) => {
      if (path === "/api/voice/wizard/devices") return Promise.resolve(DEVICES_RESPONSE);
      if (path === "/api/voice/wizard/diagnostic") return Promise.resolve(DIAGNOSTIC_RESPONSE);
      return Promise.reject(new Error("unknown path"));
    });
    mockPost.mockResolvedValueOnce(TEST_RESULT_OK);

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
