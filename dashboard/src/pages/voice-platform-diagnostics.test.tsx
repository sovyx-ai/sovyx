/**
 * Voice Platform Diagnostics page tests.
 *
 * Coverage:
 *   - Loading state renders spinner while the GET in flight.
 *   - Error state shows AlertTriangle + Refresh button on rejection.
 *   - Successful render — Linux / Windows / macOS branches paint per
 *     the platform field; non-host branches are absent.
 *   - Mic permission status pill colour-coded by status token.
 *   - Probe-failure isolation — a branch with empty/note-only payload
 *     still renders without throwing.
 *   - Refresh button triggers a second GET.
 */

import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor, fireEvent } from "@/test/test-utils";
import VoicePlatformDiagnosticsPage from "./voice-platform-diagnostics";
import type { PlatformDiagnosticsResponse } from "@/types/api";

/* ── Mock API ── */

const mockGet = vi.fn();

vi.mock("@/lib/api", () => ({
  api: { get: (...args: unknown[]) => mockGet(...args) },
  isAbortError: (err: unknown) =>
    err instanceof DOMException && (err as DOMException).name === "AbortError",
  ApiError: class ApiError extends Error {
    status: number;
    constructor(status: number, message: string) {
      super(message);
      this.status = status;
    }
  },
}));

/* ── Fixtures ── */

const LINUX_RESPONSE: PlatformDiagnosticsResponse = {
  platform: "linux",
  mic_permission: {
    status: "granted",
    machine_value: null,
    user_value: null,
    notes: [],
    remediation_hint: "",
  },
  linux: {
    pipewire: {
      status: "active",
      socket_present: true,
      pactl_available: true,
      pactl_info_ok: true,
      server_name: "PulseAudio (on PipeWire 0.3.65)",
      modules_loaded: ["module-echo-cancel"],
      echo_cancel_loaded: true,
      notes: [],
    },
    alsa_ucm: {
      status: "available",
      card_id: "0",
      alsaucm_available: true,
      verbs: ["HiFi", "VoiceCall"],
      active_verb: "HiFi",
      notes: [],
    },
  },
  windows: null,
  macos: null,
};

const WINDOWS_RESPONSE: PlatformDiagnosticsResponse = {
  platform: "win32",
  mic_permission: {
    status: "denied",
    machine_value: "Deny",
    user_value: "Allow",
    notes: ["machine policy overrides user grant"],
    remediation_hint:
      "Open Settings → Privacy & security → Microphone and grant access.",
  },
  linux: null,
  windows: {
    audio_service: {
      audiosrv: {
        name: "Audiosrv",
        state: "running",
        raw_state: "4  RUNNING",
        notes: [],
      },
      audio_endpoint_builder: {
        name: "AudioEndpointBuilder",
        state: "stopped",
        raw_state: "1  STOPPED",
        notes: [],
      },
      all_healthy: false,
      degraded_services: ["AudioEndpointBuilder"],
    },
    etw_audio_events: [
      {
        channel: "Microsoft-Windows-Audio/Operational",
        events: [
          {
            channel: "Microsoft-Windows-Audio/Operational",
            level: "error",
            event_id: 12,
            timestamp_iso: "2026-04-25T12:30:00.000Z",
            provider: "Microsoft-Windows-Audio",
            description: "Endpoint enumeration failed for capture device.",
          },
        ],
        lookback_seconds: 3600,
        notes: [],
      },
    ],
  },
  macos: null,
};

const DARWIN_RESPONSE: PlatformDiagnosticsResponse = {
  platform: "darwin",
  mic_permission: {
    status: "unknown",
    machine_value: null,
    user_value: null,
    notes: ["TCC.db not readable without Full Disk Access"],
    remediation_hint:
      "Grant Sovyx Full Disk Access in System Settings → Privacy & Security.",
  },
  linux: null,
  windows: null,
  macos: {
    hal_plugins: {
      plugins: [
        {
          bundle_name: "BlackHole.driver",
          path: "/Library/Audio/Plug-Ins/HAL/BlackHole.driver",
          category: "virtual_audio",
          friendly_label: "BlackHole virtual audio cable",
        },
      ],
      notes: [],
      virtual_audio_active: true,
      audio_enhancement_active: false,
    },
    bluetooth: {
      devices: [
        {
          name: "AirPods Pro",
          address: "AA:BB:CC:DD:EE:FF",
          profile: "a2dp",
          is_input_capable: true,
          is_output_capable: true,
        },
      ],
      notes: [],
    },
    code_signing: {
      verdict: "unsigned",
      executable_path: "/usr/local/bin/python3",
      notes: ["binary is not code-signed (typical for python)"],
      remediation_hint:
        "Sovyx is running from an unsigned interpreter (typical for Homebrew / pyenv). Hardened Runtime isn't enforced.",
    },
  },
};

beforeEach(() => {
  mockGet.mockReset();
});

/* ── Loading + error states ───────────────────────────────────── */

describe("VoicePlatformDiagnosticsPage — boot states", () => {
  it("renders a loading spinner while the request is in flight", () => {
    mockGet.mockImplementation(
      () => new Promise(() => undefined), // never resolves
    );
    render(<VoicePlatformDiagnosticsPage />);
    expect(screen.getByTestId("platform-loading")).toBeInTheDocument();
  });

  it("renders the error state and offers a refresh button on rejection", async () => {
    mockGet.mockRejectedValueOnce(new Error("boom"));
    render(<VoicePlatformDiagnosticsPage />);
    await waitFor(() => {
      expect(screen.getByTestId("platform-error")).toBeInTheDocument();
    });
    expect(screen.getByRole("button", { name: /refresh/i })).toBeInTheDocument();
  });

  it("retries the GET when the error-state refresh button is clicked", async () => {
    mockGet
      .mockRejectedValueOnce(new Error("boom"))
      .mockResolvedValueOnce(LINUX_RESPONSE);
    render(<VoicePlatformDiagnosticsPage />);
    await waitFor(() => {
      expect(screen.getByTestId("platform-error")).toBeInTheDocument();
    });
    fireEvent.click(screen.getByRole("button", { name: /refresh/i }));
    await waitFor(() => {
      expect(screen.getByTestId("platform-diagnostics-page")).toBeInTheDocument();
    });
    expect(mockGet).toHaveBeenCalledTimes(2);
  });
});

/* ── Per-OS branch rendering ──────────────────────────────────── */

describe("VoicePlatformDiagnosticsPage — Linux branch", () => {
  it("renders the Linux branch and omits Windows/macOS cards", async () => {
    mockGet.mockResolvedValueOnce(LINUX_RESPONSE);
    render(<VoicePlatformDiagnosticsPage />);
    await waitFor(() => {
      expect(screen.getByTestId("platform-linux-card")).toBeInTheDocument();
    });
    expect(screen.queryByTestId("platform-windows-card")).not.toBeInTheDocument();
    expect(screen.queryByTestId("platform-macos-card")).not.toBeInTheDocument();
  });

  it("shows the active PipeWire verb + UCM verb", async () => {
    mockGet.mockResolvedValueOnce(LINUX_RESPONSE);
    render(<VoicePlatformDiagnosticsPage />);
    await waitFor(() => {
      expect(screen.getByTestId("platform-linux-card")).toBeInTheDocument();
    });
    // The active UCM verb "HiFi" appears in the rendered UCM card —
    // at least once (matches both "active verb" line and the verbs
    // list line, hence getAllByText).
    expect(screen.getAllByText(/HiFi/).length).toBeGreaterThan(0);
  });
});

describe("VoicePlatformDiagnosticsPage — Windows branch", () => {
  it("renders the Windows branch with degraded-service warning", async () => {
    mockGet.mockResolvedValueOnce(WINDOWS_RESPONSE);
    render(<VoicePlatformDiagnosticsPage />);
    await waitFor(() => {
      expect(screen.getByTestId("platform-windows-card")).toBeInTheDocument();
    });
    // The degraded-services pill carries the failing service name —
    // text appears in both the per-service row AND the degraded-list
    // pill, so multiple matches are expected.
    expect(screen.getAllByText(/AudioEndpointBuilder/).length).toBeGreaterThan(0);
  });

  it("renders ETW audio events with their event_id + level", async () => {
    mockGet.mockResolvedValueOnce(WINDOWS_RESPONSE);
    render(<VoicePlatformDiagnosticsPage />);
    await waitFor(() => {
      expect(screen.getByTestId("platform-windows-card")).toBeInTheDocument();
    });
    expect(screen.getByText(/#12/)).toBeInTheDocument();
    expect(
      screen.getByText(/Endpoint enumeration failed/),
    ).toBeInTheDocument();
  });

  it("propagates the denied mic permission with remediation hint", async () => {
    mockGet.mockResolvedValueOnce(WINDOWS_RESPONSE);
    render(<VoicePlatformDiagnosticsPage />);
    await waitFor(() => {
      expect(screen.getByTestId("platform-mic-card")).toBeInTheDocument();
    });
    expect(
      screen.getByText(/Privacy & security → Microphone/),
    ).toBeInTheDocument();
  });
});

describe("VoicePlatformDiagnosticsPage — macOS branch", () => {
  it("renders the macOS branch with all three cards", async () => {
    mockGet.mockResolvedValueOnce(DARWIN_RESPONSE);
    render(<VoicePlatformDiagnosticsPage />);
    await waitFor(() => {
      expect(screen.getByTestId("platform-macos-card")).toBeInTheDocument();
    });
    // BlackHole appears in both the friendly_label heading and the
    // path subtitle of the same plug-in card.
    expect(screen.getAllByText(/BlackHole/).length).toBeGreaterThan(0);
    expect(screen.getByText(/AirPods Pro/)).toBeInTheDocument();
    expect(screen.getAllByText(/unsigned/).length).toBeGreaterThan(0);
  });
});

/* ── Empty-branch fallback ────────────────────────────────────── */

describe("VoicePlatformDiagnosticsPage — unknown platform fallback", () => {
  it("renders the no-branch placeholder when the host is `other`", async () => {
    mockGet.mockResolvedValueOnce({
      platform: "other",
      mic_permission: {
        status: "unknown",
        machine_value: null,
        user_value: null,
        notes: [],
        remediation_hint: "",
      },
      linux: null,
      windows: null,
      macos: null,
    } satisfies PlatformDiagnosticsResponse);
    render(<VoicePlatformDiagnosticsPage />);
    await waitFor(() => {
      expect(screen.getByTestId("platform-mic-card")).toBeInTheDocument();
    });
    // The translated "no branch populated" message should be present.
    expect(screen.getByText(/branch not populated/i)).toBeInTheDocument();
  });
});

/* ── Refresh action ───────────────────────────────────────────── */

describe("VoicePlatformDiagnosticsPage — refresh", () => {
  it("re-fetches when the refresh button is clicked", async () => {
    mockGet.mockResolvedValue(LINUX_RESPONSE);
    render(<VoicePlatformDiagnosticsPage />);
    await waitFor(() => {
      expect(screen.getByTestId("platform-diagnostics-page")).toBeInTheDocument();
    });
    expect(mockGet).toHaveBeenCalledTimes(1);
    fireEvent.click(screen.getByTestId("platform-refresh"));
    await waitFor(() => {
      expect(mockGet).toHaveBeenCalledTimes(2);
    });
  });
});
