/**
 * Regression guards for :class:`HardwareDetection`.
 *
 * These tests lock in two bugs that together blanked the Voice Setup
 * wizard's Step 4 after an `api/voice/enable` 429:
 *
 *   1. The `/api/voice/hardware-detect` fetch effect used to depend on
 *      `onDetected`/`onDeviceChange`. Callers pass inline closures, so
 *      every parent re-render changed the prop identity and the effect
 *      re-fired — pounding the endpoint into the 120 req/min limiter.
 *   2. Once the fetch failed, the error branch replaced the entire
 *      card (dropdowns, models, etc.) with a single red panel. A
 *      transient 429 wiped the whole Step 4.
 */
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor, fireEvent, within } from "@testing-library/react";
import { useState } from "react";
import { HardwareDetection } from "./HardwareDetection";

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
    input_devices: [
      { index: 0, name: "Razer BlackShark V2", is_default: true },
    ],
    output_devices: [
      { index: 1, name: "Speakers", is_default: true },
    ],
  },
  recommended_models: [
    {
      name: "kokoro-v1.0-int8",
      category: "tts",
      size_mb: 88,
      download_available: true,
      description: "TTS",
    },
  ],
  total_download_mb: 115,
};

const voiceCatalog = {
  supported_languages: ["en-us", "pt-br", "ja"],
  by_language: {
    "en-us": [
      { id: "af_heart", display_name: "Heart", language: "en-us", gender: "female" },
      { id: "am_adam", display_name: "Adam", language: "en-us", gender: "male" },
    ],
    "pt-br": [
      { id: "pf_dora", display_name: "Dora", language: "pt-br", gender: "female" },
      { id: "pm_alex", display_name: "Alex", language: "pt-br", gender: "male" },
    ],
    ja: [
      { id: "jf_alpha", display_name: "Alpha", language: "ja", gender: "female" },
    ],
  },
  recommended_per_language: {
    "en-us": "af_heart",
    "pt-br": "pf_dora",
    ja: "jf_alpha",
  },
};

const modelsStatus = {
  model_dir: "/tmp",
  all_installed: true,
  missing_count: 0,
  missing_download_mb: 0,
  models: [
    {
      name: "kokoro-v1.0-int8",
      category: "tts",
      description: "TTS",
      installed: true,
      path: "/tmp/k.onnx",
      size_mb: 88,
      expected_size_mb: 88,
      download_available: true,
    },
  ],
};

function RerenderHarness({ children }: { children: (n: number) => React.ReactNode }) {
  const [n, setN] = useState(0);
  return (
    <div>
      <button data-testid="rerender" onClick={() => setN((x) => x + 1)}>
        rerender
      </button>
      {children(n)}
    </div>
  );
}

beforeEach(() => {
  mockGet.mockReset();
  mockPost.mockReset();
});

describe("HardwareDetection", () => {
  it("fetches /hardware-detect exactly once even when the parent re-renders with fresh inline callbacks", async () => {
    // First call is hardware-detect. Subsequent calls (models status)
    // are driven by useVoiceModels on mount — we stub them out too.
    mockGet.mockImplementation((url: string) => {
      if (url === "/api/voice/hardware-detect") return Promise.resolve(hardwareInfo);
      if (url === "/api/voice/models/status") return Promise.resolve(modelsStatus);
      if (url === "/api/voice/voices") return Promise.resolve(voiceCatalog);
      return Promise.reject(new Error(`unexpected GET ${url}`));
    });

    const { getByTestId } = render(
      <RerenderHarness>
        {(n) => (
          <HardwareDetection
            onDetected={() => {
              // Inline — fresh identity every render, exactly the shape
              // VoiceStep ships. The effect MUST NOT re-fire.
              void n;
            }}
            onDeviceChange={() => {
              void n;
            }}
          />
        )}
      </RerenderHarness>,
    );

    await waitFor(() => {
      expect(screen.getByText(/8 cores/i)).toBeInTheDocument();
    });

    const initialHardwareCalls = mockGet.mock.calls.filter(
      (c) => c[0] === "/api/voice/hardware-detect",
    ).length;
    expect(initialHardwareCalls).toBe(1);

    // Pound the parent — this used to re-fire the fetch and trip the
    // 120 req/min limiter in minutes.
    for (let i = 0; i < 20; i++) {
      getByTestId("rerender").click();
    }

    // Give pending microtasks a chance to settle.
    await new Promise((r) => setTimeout(r, 10));

    const finalHardwareCalls = mockGet.mock.calls.filter(
      (c) => c[0] === "/api/voice/hardware-detect",
    ).length;
    expect(finalHardwareCalls).toBe(1);
  });

  it("surfaces an error panel when the initial detect fetch fails", async () => {
    mockGet.mockImplementation((url: string) => {
      if (url === "/api/voice/hardware-detect") return Promise.reject(new Error("boom"));
      if (url === "/api/voice/voices") return Promise.resolve(voiceCatalog);
      return Promise.resolve({});
    });

    render(<HardwareDetection />);

    await waitFor(() => {
      expect(screen.getByText(/boom/)).toBeInTheDocument();
    });
    // No cpu/ram chips rendered.
    expect(screen.queryByText(/cores/i)).not.toBeInTheDocument();
  });

  describe("voice test picker", () => {
    function stubAll() {
      mockGet.mockImplementation((url: string) => {
        if (url === "/api/voice/hardware-detect") return Promise.resolve(hardwareInfo);
        if (url === "/api/voice/models/status") return Promise.resolve(modelsStatus);
        if (url === "/api/voice/voices") return Promise.resolve(voiceCatalog);
        return Promise.reject(new Error(`unexpected GET ${url}`));
      });
    }

    it("seeds recommended voice from initialLanguage (pt → pt-br/pf_dora)", async () => {
      stubAll();

      render(<HardwareDetection initialLanguage="pt" />);

      const languageSelect = await screen.findByLabelText(/voice-test language/i);
      const voiceSelect = await screen.findByLabelText(/^voice$/i);

      await waitFor(() => {
        expect((languageSelect as HTMLSelectElement).value).toBe("pt-br");
        expect((voiceSelect as HTMLSelectElement).value).toBe("pf_dora");
      });
    });

    it("falls back to en-us when initialLanguage is omitted", async () => {
      stubAll();
      render(<HardwareDetection />);

      const languageSelect = await screen.findByLabelText(/voice-test language/i);
      const voiceSelect = await screen.findByLabelText(/^voice$/i);

      await waitFor(() => {
        expect((languageSelect as HTMLSelectElement).value).toBe("en-us");
        expect((voiceSelect as HTMLSelectElement).value).toBe("af_heart");
      });
    });

    it("switching language resets the voice to that language's recommended", async () => {
      stubAll();
      render(<HardwareDetection initialLanguage="en" />);

      const languageSelect = await screen.findByLabelText(/voice-test language/i);
      const voiceSelect = await screen.findByLabelText(/^voice$/i);

      await waitFor(() => {
        expect((voiceSelect as HTMLSelectElement).value).toBe("af_heart");
      });

      fireEvent.change(languageSelect, { target: { value: "ja" } });

      await waitFor(() => {
        expect((voiceSelect as HTMLSelectElement).value).toBe("jf_alpha");
      });
      // The voice dropdown should only list Japanese voices now.
      const options = within(voiceSelect as HTMLSelectElement).getAllByRole(
        "option",
      ) as HTMLOptionElement[];
      expect(options.map((o) => o.value)).toEqual(["jf_alpha"]);
    });

    it("voice dropdown only lists voices for the selected language", async () => {
      stubAll();
      render(<HardwareDetection initialLanguage="pt-br" />);

      const voiceSelect = await screen.findByLabelText(/^voice$/i);
      await waitFor(() => {
        expect((voiceSelect as HTMLSelectElement).value).toBe("pf_dora");
      });

      const options = within(voiceSelect as HTMLSelectElement).getAllByRole(
        "option",
      ) as HTMLOptionElement[];
      expect(options.map((o) => o.value).sort()).toEqual(["pf_dora", "pm_alex"]);
    });
  });
});
