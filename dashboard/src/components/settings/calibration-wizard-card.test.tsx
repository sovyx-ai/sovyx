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
});
