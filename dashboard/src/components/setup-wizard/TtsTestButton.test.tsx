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
import { ApiError } from "@/lib/api";

/* ── Mock API ── */

const mockGet = vi.fn();
const mockPost = vi.fn();

vi.mock("@/lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/api")>();
  return {
    ...actual,
    api: {
      get: (...args: unknown[]) => mockGet(...args),
      post: (...args: unknown[]) => mockPost(...args),
    },
  };
});

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
        /no tts python package/i,
      );
    });
  });

  it("renders the Download voice models CTA when models_not_downloaded", async () => {
    // Arrange: the POST fails with a structured 503 carrying the new
    // ``models_not_downloaded`` code. The component should show the
    // download CTA instead of leaving the user at a dead end.
    //
    // Use a real ApiError — in production, api.ts throws
    // ``new ApiError(status, responseText)`` and the constructor parses
    // the JSON text into ``.body``. The hand-crafted ``{ body: ... }``
    // shape that the test used before only existed in the test, so
    // callers reading ``err.body.code`` silently missed in production.
    const err = new ApiError(
      503,
      JSON.stringify({
        code: "models_not_downloaded",
        detail: "install them",
        missing_models: ["kokoro-v1.0-int8", "kokoro-voices-v1.0"],
      }),
    );
    mockPost.mockRejectedValueOnce(err);
    // The CTA's useVoiceModels() fires an initial GET — return a clean
    // "nothing missing" response so the hook settles.
    mockGet.mockResolvedValueOnce({
      model_dir: "/tmp",
      all_installed: false,
      missing_count: 2,
      missing_download_mb: 115,
      models: [],
    });

    render(<TtsTestButton deviceId={0} />);
    fireEvent.click(screen.getByRole("button", { name: /test speakers/i }));

    await waitFor(() => {
      expect(screen.getByTestId("tts-test-error")).toHaveTextContent(
        /model files are not on disk/i,
      );
    });
    // The CTA must render alongside the error message.
    expect(await screen.findByTestId("tts-test-download-cta")).toBeEnabled();
    expect(screen.getByTestId("tts-test-download-cta")).toHaveTextContent(
      /download voice models/i,
    );
  });

  it("maps a 409 pipeline_active body into the user-facing message", async () => {
    const err = new ApiError(409, JSON.stringify({ code: "pipeline_active" }));
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

  it("stops polling when the component unmounts mid-test", async () => {
    // POST resolves; the first poll is still running. After unmount, no
    // further GETs should fire — otherwise the loop leaks network traffic
    // and setState-after-unmount warnings pile up.
    mockPost.mockResolvedValueOnce({ job_id: "job-unmount", status: "queued" });
    mockGet.mockResolvedValue({ status: "running" });

    const { unmount } = render(<TtsTestButton deviceId={0} />);
    fireEvent.click(screen.getByRole("button", { name: /test speakers/i }));

    // Let the poll loop fire at least once.
    await waitFor(() => {
      expect(mockGet).toHaveBeenCalled();
    });
    const callsAtUnmount = mockGet.mock.calls.length;

    unmount();

    // Give the background interval well beyond POLL_INTERVAL_MS (400ms)
    // to catch any leak that would bump the call count.
    await new Promise((r) => setTimeout(r, 700));

    expect(mockGet.mock.calls.length).toBe(callsAtUnmount);
  });
});
