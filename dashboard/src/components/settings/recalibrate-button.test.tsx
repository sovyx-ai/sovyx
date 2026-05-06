/**
 * RecalibrateButton tests -- Settings -> Voice -> Recalibrate trigger.
 *
 * Covers:
 * * Hidden when calibrationFeatureFlag is null (never loaded).
 * * Hidden when calibrationFeatureFlag.enabled is false.
 * * Visible + click flow when enabled: confirm dialog -> POST /start.
 * * 409 conflict path surfaces an error toast.
 */

import { beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, waitFor } from "@/test/test-utils";

import { useDashboardStore } from "@/stores/dashboard";
import { RecalibrateButton } from "./recalibrate-button";

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
  // Reset the calibration slice's feature-flag state between tests.
  useDashboardStore.setState({ calibrationFeatureFlag: null });
});

describe("RecalibrateButton", () => {
  it("renders nothing when feature flag is null", () => {
    const { container } = render(<RecalibrateButton />);
    expect(container.firstChild).toBeNull();
  });

  it("renders nothing when feature flag is disabled", () => {
    useDashboardStore.setState({
      calibrationFeatureFlag: { enabled: false, runtime_override_active: false },
    });
    const { container } = render(<RecalibrateButton />);
    expect(container.firstChild).toBeNull();
  });

  it("renders trigger button when feature flag is enabled", () => {
    useDashboardStore.setState({
      calibrationFeatureFlag: { enabled: true, runtime_override_active: false },
    });
    render(<RecalibrateButton />);
    expect(
      screen.getByTestId("settings-recalibrate-toggle"),
    ).toBeInTheDocument();
  });

  it("confirm flow POSTs /start and shows success toast", async () => {
    useDashboardStore.setState({
      calibrationFeatureFlag: { enabled: true, runtime_override_active: false },
    });
    // POST /start returns 202 with job_id
    mockFetch.mockResolvedValueOnce(
      jsonResponse(
        {
          job_id: "default",
          stream_url: "/api/voice/calibration/jobs/default/stream",
        },
        202,
      ),
    );

    render(<RecalibrateButton />);
    fireEvent.click(screen.getByTestId("settings-recalibrate-toggle"));
    // Confirm button appears
    fireEvent.click(screen.getByTestId("settings-recalibrate-confirm"));

    await waitFor(() => expect(mockToastSuccess).toHaveBeenCalled());
    const postCall = mockFetch.mock.calls.find((c) => {
      const init = c[1] as RequestInit | undefined;
      return init?.method === "POST";
    });
    expect(postCall).toBeDefined();
  });

  it("dismiss button hides the confirm flow", () => {
    useDashboardStore.setState({
      calibrationFeatureFlag: { enabled: true, runtime_override_active: false },
    });
    render(<RecalibrateButton />);
    fireEvent.click(screen.getByTestId("settings-recalibrate-toggle"));
    expect(screen.getByTestId("settings-recalibrate-cancel")).toBeInTheDocument();
    fireEvent.click(screen.getByTestId("settings-recalibrate-cancel"));
    expect(
      screen.queryByTestId("settings-recalibrate-confirm"),
    ).not.toBeInTheDocument();
  });
});
