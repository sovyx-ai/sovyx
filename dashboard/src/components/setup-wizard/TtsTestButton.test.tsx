/**
 * Tests for :class:`TtsTestButton`.
 *
 * Covers the happy poll-to-done path, error code mapping (including
 * ``pipeline_active`` from a 409), terminal polling errors, and the
 * ``disabled`` prop behaviour.
 */
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { TtsTestButton } from "./TtsTestButton";

/* ── Mock API ── */

const mockGet = vi.fn();
const mockPost = vi.fn();

vi.mock("@/lib/api", () => ({
  api: {
    get: (...args: unknown[]) => mockGet(...args),
    post: (...args: unknown[]) => mockPost(...args),
  },
}));

beforeEach(() => {
  mockGet.mockReset();
  mockPost.mockReset();
});

describe("TtsTestButton", () => {
  it("renders the idle label and is enabled by default", () => {
    render(<TtsTestButton deviceId={0} />);
    expect(
      screen.getByRole("button", { name: /test speakers/i }),
    ).toBeEnabled();
  });

  it("honours the disabled prop", () => {
    render(<TtsTestButton deviceId={null} disabled />);
    expect(
      screen.getByRole("button", { name: /test speakers/i }),
    ).toBeDisabled();
  });

  it("POSTs the test job then polls until done and shows the peak", async () => {
    mockPost.mockResolvedValueOnce({ job_id: "job-1", status: "queued" });
    mockGet
      .mockResolvedValueOnce({ status: "running" })
      .mockResolvedValueOnce({
        status: "done",
        peak_db: -6.5,
        duration_ms: 820,
      });

    render(<TtsTestButton deviceId={2} language="en" voice="en_US-amy" />);
    fireEvent.click(screen.getByRole("button", { name: /test speakers/i }));

    await waitFor(() => {
      expect(screen.getByRole("status")).toHaveTextContent(
        /played successfully/i,
      );
    });
    expect(screen.getByRole("status")).toHaveTextContent(/-6\.5 dBFS peak/);

    // POST payload carries the wizard inputs verbatim.
    expect(mockPost).toHaveBeenCalledWith(
      "/api/voice/test/output",
      expect.objectContaining({
        device_id: 2,
        voice: "en_US-amy",
        phrase_key: "default",
        language: "en",
      }),
      expect.objectContaining({ schema: expect.anything() }),
    );
  });

  it("surfaces an error when the poll result is error with a known code", async () => {
    mockPost.mockResolvedValueOnce({ job_id: "job-2", status: "queued" });
    mockGet.mockResolvedValueOnce({
      status: "error",
      code: "device_busy",
      detail: "locked by other app",
    });

    render(<TtsTestButton deviceId={0} />);
    fireEvent.click(screen.getByRole("button", { name: /test speakers/i }));

    await waitFor(() => {
      expect(screen.getByTestId("tts-test-error")).toHaveTextContent(
        /locked by other app/,
      );
    });
  });

  it("falls back to a code-derived message when detail is missing", async () => {
    mockPost.mockResolvedValueOnce({ job_id: "job-3", status: "queued" });
    mockGet.mockResolvedValueOnce({
      status: "error",
      code: "tts_unavailable",
      detail: "",
    });

    render(<TtsTestButton deviceId={0} />);
    fireEvent.click(screen.getByRole("button", { name: /test speakers/i }));

    await waitFor(() => {
      expect(screen.getByTestId("tts-test-error")).toHaveTextContent(
        /download a voice model/i,
      );
    });
  });

  it("maps a 409 pipeline_active body into the user-facing message", async () => {
    const err = Object.assign(new Error("conflict"), {
      body: { code: "pipeline_active" },
    });
    mockPost.mockRejectedValueOnce(err);

    render(<TtsTestButton deviceId={0} />);
    fireEvent.click(screen.getByRole("button", { name: /test speakers/i }));

    await waitFor(() => {
      expect(screen.getByTestId("tts-test-error")).toHaveTextContent(
        /voice pipeline is running/i,
      );
    });
  });

  it("shows a generic network-error message when the POST fails without a body code", async () => {
    mockPost.mockRejectedValueOnce(new Error("boom"));

    render(<TtsTestButton deviceId={0} />);
    fireEvent.click(screen.getByRole("button", { name: /test speakers/i }));

    await waitFor(() => {
      expect(screen.getByTestId("tts-test-error")).toHaveTextContent(/boom/);
    });
  });

  it("disables the button while a test is in flight", async () => {
    mockPost.mockReturnValueOnce(new Promise(() => {}));
    render(<TtsTestButton deviceId={0} />);
    const btn = screen.getByRole("button", { name: /test speakers/i });
    fireEvent.click(btn);
    await waitFor(() => expect(btn).toBeDisabled());
    expect(btn).toHaveTextContent(/playing test/i);
  });
});
