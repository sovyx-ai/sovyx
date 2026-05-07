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
  it("renders disabled trigger when feature flag is null", () => {
    // P6 (v0.30.34) — Mission §10.2 #12: the button stays visible
    // when the flag is null/off, just disabled with a tooltip
    // pointing at the flag toggle. Pre-P6 returned null (hidden).
    render(<RecalibrateButton />);
    const trigger = screen.getByTestId("settings-recalibrate-toggle");
    expect(trigger).toBeInTheDocument();
    expect(trigger).toBeDisabled();
  });

  it("renders disabled trigger when feature flag is disabled", () => {
    useDashboardStore.setState({
      calibrationFeatureFlag: { enabled: false, runtime_override_active: false },
    });
    render(<RecalibrateButton />);
    const trigger = screen.getByTestId("settings-recalibrate-toggle");
    expect(trigger).toBeInTheDocument();
    expect(trigger).toBeDisabled();
  });

  it("renders enabled trigger button when feature flag is enabled", () => {
    useDashboardStore.setState({
      calibrationFeatureFlag: { enabled: true, runtime_override_active: false },
    });
    render(<RecalibrateButton />);
    const trigger = screen.getByTestId("settings-recalibrate-toggle");
    expect(trigger).toBeInTheDocument();
    expect(trigger).not.toBeDisabled();
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

  // ════════════════════════════════════════════════════════════════════
  // rc.13 — close the rc.11 EIXO 2 bug-class miss on this sibling
  // surface. Pre-rc.13 the button gated on ``flag.enabled`` only,
  // ignoring ``platform_supported``. Win/macOS operators with the
  // (default) flag enabled saw the button enabled, clicked it, and
  // landed in a silent FALLBACK from DiagPrerequisiteError. Now:
  // gate on the conjunction ``enabled AND platform_supported``;
  // tooltip explains the limitation.
  // ════════════════════════════════════════════════════════════════════

  it("renders disabled trigger when platform_supported=false (rc.13 EIXO 2 sibling)", () => {
    useDashboardStore.setState({
      calibrationFeatureFlag: {
        enabled: true,
        runtime_override_active: false,
        platform_supported: false,
      },
    });
    render(<RecalibrateButton />);
    const trigger = screen.getByTestId("settings-recalibrate-toggle");
    expect(trigger).toBeInTheDocument();
    expect(trigger).toBeDisabled();
    // Tooltip cites the Linux-only limitation, not the flag-off
    // explanation (which was the pre-rc.13 misleading copy).
    expect(trigger).toHaveAttribute(
      "title",
      expect.stringMatching(/linux/i),
    );
  });

  it("treats missing platform_supported as true (pre-rc.12 daemon back-compat)", () => {
    // Older daemon doesn't ship the field. Component MUST default to
    // true, preserving the legacy single-platform behaviour: a flag-
    // enabled button stays clickable exactly as it did before rc.13.
    useDashboardStore.setState({
      calibrationFeatureFlag: {
        enabled: true,
        runtime_override_active: false,
        // platform_supported intentionally omitted
      },
    });
    render(<RecalibrateButton />);
    const trigger = screen.getByTestId("settings-recalibrate-toggle");
    expect(trigger).not.toBeDisabled();
  });
});
