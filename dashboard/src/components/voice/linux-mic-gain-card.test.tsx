/**
 * LinuxMicGainCard tests — Linux ALSA mixer saturation detection +
 * one-click reset.
 *
 * Covers: loading → hidden on non-Linux, healthy state, saturation
 * alert render, amixer-missing warning, successful reset toast, POST
 * failure handling, and the v1.3 §4.5 L0-4 persistence hint that
 * appears only after a successful reset.
 */

import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, waitFor } from "@/test/test-utils";
import { LinuxMicGainCard } from "./linux-mic-gain-card";
import type { LinuxMixerDiagnosticsResponse } from "@/types/api";

const mockFetch = vi.fn();
globalThis.fetch = mockFetch;

const mockToastSuccess = vi.fn();
const mockToastError = vi.fn();

vi.mock("sonner", () => ({
  toast: {
    success: (...args: unknown[]) => mockToastSuccess(...args),
    error: (...args: unknown[]) => mockToastError(...args),
  },
}));

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

function saturatingPayload(
  overrides: Partial<LinuxMixerDiagnosticsResponse> = {},
): LinuxMixerDiagnosticsResponse {
  return {
    platform_supported: true,
    amixer_available: true,
    snapshots: [
      {
        card_index: 1,
        card_id: "PCH",
        card_longname: "HDA Intel PCH",
        aggregated_boost_db: 36,
        saturation_warning: true,
        controls: [
          {
            name: "Capture",
            min_raw: 0,
            max_raw: 31,
            current_raw: 31,
            current_db: 36,
            max_db: 36,
            is_boost_control: true,
            saturation_risk: true,
            asymmetric: false,
          },
        ],
      },
    ],
    aggregated_boost_db_ceiling: 18,
    saturation_ratio_ceiling: 0.5,
    reset_enabled_by_default: true,
    ...overrides,
  };
}

beforeEach(() => {
  mockFetch.mockReset();
  mockToastSuccess.mockReset();
  mockToastError.mockReset();
  sessionStorage.clear();
});

describe("LinuxMicGainCard", () => {
  it("renders nothing on non-Linux hosts", async () => {
    mockFetch.mockResolvedValueOnce(
      jsonResponse(
        saturatingPayload({
          platform_supported: false,
          amixer_available: false,
          snapshots: [],
        }),
      ),
    );
    const { container } = render(<LinuxMicGainCard />);
    await waitFor(() => {
      expect(
        container.querySelector('[data-testid="linux-mic-gain-card"]'),
      ).toBeNull();
    });
  });

  it("shows the amixer-missing warning on Linux without alsa-utils", async () => {
    mockFetch.mockResolvedValueOnce(
      jsonResponse(
        saturatingPayload({ amixer_available: false, snapshots: [] }),
      ),
    );
    render(<LinuxMicGainCard />);
    await waitFor(() => {
      expect(
        screen.getByTestId("linux-mic-gain-amixer-missing"),
      ).toBeInTheDocument();
    });
  });

  it("shows the saturation alert with card details", async () => {
    mockFetch.mockResolvedValueOnce(jsonResponse(saturatingPayload()));
    render(<LinuxMicGainCard />);
    await waitFor(() => {
      expect(screen.getByTestId("linux-mic-gain-alert")).toBeInTheDocument();
    });
    expect(screen.getByText(/HDA Intel PCH/)).toBeInTheDocument();
    expect(screen.getByText(/\+36\.0 dB/)).toBeInTheDocument();
  });

  it("shows the healthy message when no saturation detected", async () => {
    mockFetch.mockResolvedValueOnce(
      jsonResponse(
        saturatingPayload({
          snapshots: [
            {
              card_index: 1,
              card_id: "PCH",
              card_longname: "HDA Intel PCH",
              aggregated_boost_db: 0,
              saturation_warning: false,
              controls: [],
            },
          ],
        }),
      ),
    );
    render(<LinuxMicGainCard />);
    await waitFor(() => {
      expect(
        screen.getByText(/within a safe range/i),
      ).toBeInTheDocument();
    });
  });

  it("POSTs to linux-mixer-reset, toasts success, and surfaces persist hint", async () => {
    mockFetch
      .mockResolvedValueOnce(jsonResponse(saturatingPayload()))
      .mockResolvedValueOnce(
        jsonResponse({
          ok: true,
          card_index: 1,
          card_id: "PCH",
          card_longname: "HDA Intel PCH",
          applied_controls: [["Capture", 15]],
          reverted_controls: [["Capture", 31]],
        }),
      )
      .mockResolvedValueOnce(
        jsonResponse(
          saturatingPayload({
            snapshots: [
              {
                card_index: 1,
                card_id: "PCH",
                card_longname: "HDA Intel PCH",
                aggregated_boost_db: 0,
                saturation_warning: false,
                controls: [],
              },
            ],
          }),
        ),
      );

    render(<LinuxMicGainCard />);
    await waitFor(() => {
      expect(
        screen.getByTestId("reset-linux-mic-gain-button"),
      ).toBeInTheDocument();
    });
    fireEvent.click(screen.getByTestId("reset-linux-mic-gain-button"));
    await waitFor(() => {
      expect(mockToastSuccess).toHaveBeenCalled();
    });

    const postCall = mockFetch.mock.calls.find(
      (c) => c[1]?.method === "POST",
    );
    expect(postCall).toBeDefined();
    expect(JSON.parse(postCall![1].body as string)).toEqual({ card_index: 1 });

    // v1.3 L0-4 — persist hint appears after a successful reset.
    await waitFor(() => {
      expect(
        screen.getByTestId("linux-mic-gain-persist-hint"),
      ).toBeInTheDocument();
    });
    expect(screen.getByText(/sudo alsactl store/)).toBeInTheDocument();
  });

  it("toasts error on POST failure", async () => {
    mockFetch
      .mockResolvedValueOnce(jsonResponse(saturatingPayload()))
      .mockResolvedValueOnce(jsonResponse({ error: "boom" }, 500));

    render(<LinuxMicGainCard />);
    await waitFor(() => {
      expect(
        screen.getByTestId("reset-linux-mic-gain-button"),
      ).toBeInTheDocument();
    });
    fireEvent.click(screen.getByTestId("reset-linux-mic-gain-button"));
    await waitFor(() => {
      expect(mockToastError).toHaveBeenCalled();
    });
  });
});
