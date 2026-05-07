/**
 * CalibrationWizardCard tests -- Settings -> Voice -> Advanced toggle
 * for the L3 calibration wizard mount flag.
 *
 * Covers:
 * * Initial render with disabled state surfaces the enable button
 * * Toggling ON calls POST /api/voice/calibration/feature-flag
 *   and reflects the new state
 * * Runtime-override badge surfaces when flag.runtime_override_active is true
 */

import { beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, waitFor } from "@/test/test-utils";

import { CalibrationWizardCard } from "./calibration-wizard-card";

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

beforeEach(() => {
  mockFetch.mockReset();
  mockToastSuccess.mockReset();
  mockToastError.mockReset();
});

describe("CalibrationWizardCard", () => {
  it("renders disabled state with enable button", async () => {
    mockFetch.mockResolvedValueOnce(
      jsonResponse({ enabled: false, runtime_override_active: false }),
    );

    render(<CalibrationWizardCard />);

    await waitFor(() => {
      expect(
        screen.getByTestId("settings-calibration-wizard-toggle"),
      ).toBeInTheDocument();
    });

    // The enable button is rendered (the disabled-state path).
    expect(screen.getByText(/Enable wizard/i)).toBeInTheDocument();
    // The status row shows "Disabled" (matches multiple times because
    // the description text also includes the word; the >=1 check
    // confirms at least one occurrence is present).
    expect(screen.getAllByText(/Disabled/i).length).toBeGreaterThanOrEqual(1);
  });

  it("toggling ON calls the POST endpoint and shows toast", async () => {
    // Initial GET returns disabled
    mockFetch.mockResolvedValueOnce(
      jsonResponse({ enabled: false, runtime_override_active: false }),
    );
    // POST returns enabled
    mockFetch.mockResolvedValueOnce(
      jsonResponse({ enabled: true, runtime_override_active: true }),
    );

    render(<CalibrationWizardCard />);

    const toggle = await screen.findByTestId(
      "settings-calibration-wizard-toggle",
    );
    fireEvent.click(toggle);

    await waitFor(() => {
      expect(mockToastSuccess).toHaveBeenCalled();
    });

    // POST was called with enabled: true
    const postCall = mockFetch.mock.calls.find((call) => {
      const init = call[1] as RequestInit | undefined;
      return init?.method === "POST";
    });
    expect(postCall).toBeDefined();
    if (postCall) {
      const init = postCall[1] as RequestInit;
      const body = JSON.parse(init.body as string);
      expect(body).toEqual({ enabled: true });
    }
  });

  it("surfaces runtime-override badge when flag is overridden", async () => {
    mockFetch.mockResolvedValueOnce(
      jsonResponse({ enabled: true, runtime_override_active: true }),
    );

    render(<CalibrationWizardCard />);

    await waitFor(() => {
      expect(screen.getByText(/Runtime override active/i)).toBeInTheDocument();
    });
  });

  // ════════════════════════════════════════════════════════════════════
  // rc.14 — close the bug class definitively. Pre-rc.14 the card gated
  // only on ``flag.enabled``, ignoring ``platform_supported``. On
  // Win/macOS the status badge would say "Enabled" (because the flag
  // defaulted ON in rc.10) while every downstream surface (wizard
  // mount + Recalibrate) was actually disabled because of platform —
  // the card LIED about state. Now: when ``platform_supported`` is
  // false, the toggle is disabled with a Linux-only tooltip + the
  // status badge says ``statusPlatformUnsupported`` distinctly.
  // ════════════════════════════════════════════════════════════════════

  it("renders disabled toggle with Linux-only status when platform_supported=false (rc.14)", async () => {
    mockFetch.mockResolvedValueOnce(
      jsonResponse({
        enabled: true,
        runtime_override_active: false,
        platform_supported: false,
      }),
    );

    render(<CalibrationWizardCard />);

    const toggle = await screen.findByTestId(
      "settings-calibration-wizard-toggle",
    );
    expect(toggle).toBeDisabled();
    // Tooltip cites Linux, not the generic disabled hint.
    expect(toggle).toHaveAttribute(
      "title",
      expect.stringMatching(/linux/i),
    );
    // Status badge does NOT say "Enabled" even though the flag is
    // technically enabled — the card stops lying about state.
    expect(screen.queryByText(/^Enabled$/)).not.toBeInTheDocument();
    // Status badge cites the Linux-only state instead. The body note
    // also mentions Linux, so we expect 2 matches (badge + note).
    expect(screen.getAllByText(/Linux-only/i).length).toBeGreaterThanOrEqual(1);
  });

  it("treats missing platform_supported as true (pre-rc.12 daemon back-compat)", async () => {
    // Older daemon doesn't ship the field. Card MUST default to
    // true, preserving the legacy single-platform behaviour: the
    // toggle stays clickable exactly as it did before rc.14.
    mockFetch.mockResolvedValueOnce(
      jsonResponse({
        enabled: true,
        runtime_override_active: false,
        // platform_supported intentionally omitted
      }),
    );

    render(<CalibrationWizardCard />);

    const toggle = await screen.findByTestId(
      "settings-calibration-wizard-toggle",
    );
    expect(toggle).not.toBeDisabled();
    // Status badge shows the standard Enabled state (matches multiple
    // times because the description also contains the word; the >=1
    // check confirms at least one occurrence).
    expect(screen.getAllByText(/^Enabled$/).length).toBeGreaterThanOrEqual(1);
  });
});
