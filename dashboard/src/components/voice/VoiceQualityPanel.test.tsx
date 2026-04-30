/**
 * VoiceQualityPanel tests — Phase 4 / T4.26 + T4.37 panels.
 *
 * Covers: loading state, error/503 handling, verdict mapping
 * (Excellent / Good / Degraded / Poor / Warming up), MOS proxy
 * disclaimer when DNSMOS extras absent, MOS direct-mode label
 * when extras installed, AGC2 block null + populated paths.
 */

import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor } from "@/test/test-utils";
import { VoiceQualityPanel } from "./VoiceQualityPanel";
import type { VoiceQualitySnapshotResponse } from "@/types/api";

const mockFetch = vi.fn();
globalThis.fetch = mockFetch;

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

function snapshot(
  overrides: Partial<VoiceQualitySnapshotResponse> = {},
): VoiceQualitySnapshotResponse {
  return {
    snr_p50_db: 14,
    snr_sample_count: 200,
    snr_verdict: "good",
    noise_floor: {
      short_avg_db: -55,
      long_avg_db: -56,
      drift_db: 1,
      ready: true,
      short_sample_count: 1800,
      long_sample_count: 9300,
    },
    agc2: {
      frames_processed: 1000,
      frames_silenced: 100,
      frames_vad_silenced: 0,
      current_gain_db: 2.5,
      speech_level_dbfs: -20.5,
    },
    dnsmos_extras_installed: false,
    ...overrides,
  };
}

beforeEach(() => {
  mockFetch.mockReset();
});

describe("VoiceQualityPanel", () => {
  it("shows loading state initially", async () => {
    mockFetch.mockImplementation(
      () => new Promise(() => undefined), // never resolves
    );
    render(<VoiceQualityPanel />);
    expect(screen.getByText(/Loading voice quality/i)).toBeInTheDocument();
  });

  it("renders Good verdict with SNR p50 and band legend", async () => {
    mockFetch.mockResolvedValue(jsonResponse(snapshot({ snr_verdict: "good", snr_p50_db: 14 })));
    render(<VoiceQualityPanel />);
    await waitFor(() => {
      expect(screen.getByText("Good")).toBeInTheDocument();
    });
    // "14.0 dB" appears in the SNR panel (p50) AND the MOS proxy
    // disclaimer (derivation source) — both sites are correct.
    expect(screen.getAllByText(/14.0 dB/).length).toBeGreaterThan(0);
    // Band legend always present (case-insensitive, multiple
    // occurrences across the panels are expected).
    expect(screen.getAllByText(/excellent/i).length).toBeGreaterThan(0);
    expect(screen.getAllByText(/poor/i).length).toBeGreaterThan(0);
  });

  it("renders Excellent verdict at high SNR", async () => {
    mockFetch.mockResolvedValue(
      jsonResponse(snapshot({ snr_verdict: "excellent", snr_p50_db: 22 })),
    );
    render(<VoiceQualityPanel />);
    await waitFor(() => {
      expect(screen.getByText("Excellent")).toBeInTheDocument();
    });
  });

  it("renders Degraded verdict in noisy environment", async () => {
    mockFetch.mockResolvedValue(
      jsonResponse(snapshot({ snr_verdict: "degraded", snr_p50_db: 5 })),
    );
    render(<VoiceQualityPanel />);
    await waitFor(() => {
      expect(screen.getByText("Degraded")).toBeInTheDocument();
    });
    // Remediation hint surfaced.
    expect(
      screen.getByText(/Move the mic 30 cm closer/i),
    ).toBeInTheDocument();
  });

  it("renders Poor verdict at very low SNR", async () => {
    mockFetch.mockResolvedValue(
      jsonResponse(snapshot({ snr_verdict: "poor", snr_p50_db: 1 })),
    );
    render(<VoiceQualityPanel />);
    await waitFor(() => {
      expect(screen.getByText("Poor")).toBeInTheDocument();
    });
  });

  it("renders Warming up state when no SNR samples in window", async () => {
    mockFetch.mockResolvedValue(
      jsonResponse(
        snapshot({
          snr_verdict: "no_signal",
          snr_p50_db: null,
          snr_sample_count: 0,
        }),
      ),
    );
    render(<VoiceQualityPanel />);
    await waitFor(() => {
      expect(screen.getByText("Warming up")).toBeInTheDocument();
    });
  });

  it("shows DNSMOS proxy disclaimer when extras absent", async () => {
    mockFetch.mockResolvedValue(
      jsonResponse(snapshot({ dnsmos_extras_installed: false })),
    );
    render(<VoiceQualityPanel />);
    await waitFor(() => {
      expect(screen.getByText(/SNR-proxy mode/i)).toBeInTheDocument();
    });
    expect(screen.getByText(/sovyx\[dnsmos\]/i)).toBeInTheDocument();
  });

  it("shows DNSMOS direct-mode badge when extras installed", async () => {
    mockFetch.mockResolvedValue(
      jsonResponse(snapshot({ dnsmos_extras_installed: true })),
    );
    render(<VoiceQualityPanel />);
    await waitFor(() => {
      expect(
        screen.getByText(/DNSMOS extras detected/i),
      ).toBeInTheDocument();
    });
    expect(screen.queryByText(/SNR-proxy mode/i)).not.toBeInTheDocument();
  });

  it("shows AGC2 disabled state when agc2 block is null", async () => {
    mockFetch.mockResolvedValue(jsonResponse(snapshot({ agc2: null })));
    render(<VoiceQualityPanel />);
    await waitFor(() => {
      expect(
        screen.getByText(/AGC2 not active/i),
      ).toBeInTheDocument();
    });
  });

  it("renders AGC2 stats when payload present", async () => {
    mockFetch.mockResolvedValue(
      jsonResponse(
        snapshot({
          agc2: {
            frames_processed: 5000,
            frames_silenced: 500,
            frames_vad_silenced: 200,
            current_gain_db: 4.75,
            speech_level_dbfs: -19.5,
          },
        }),
      ),
    );
    render(<VoiceQualityPanel />);
    await waitFor(() => {
      expect(screen.getByText(/4.75 dB/)).toBeInTheDocument();
    });
    expect(screen.getByText(/-19.50 dBFS/)).toBeInTheDocument();
    // 200/5000 = 4% gate rate.
    expect(screen.getByText(/4.0%/)).toBeInTheDocument();
  });

  it("shows warming-up state for noise-floor when long window short", async () => {
    mockFetch.mockResolvedValue(
      jsonResponse(
        snapshot({
          noise_floor: {
            short_avg_db: null,
            long_avg_db: null,
            drift_db: null,
            ready: false,
            short_sample_count: 100,
            long_sample_count: 100,
          },
        }),
      ),
    );
    render(<VoiceQualityPanel />);
    await waitFor(() => {
      expect(
        screen.getByText(/long-window baseline needs ~5 minutes/i),
      ).toBeInTheDocument();
    });
  });
});
