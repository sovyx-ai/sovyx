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
