/**
 * Tests for ``useVoiceModels`` — disk-truth fetch + download polling.
 *
 * We exercise the three branches the UI depends on:
 *   1. Happy-path status fetch on mount.
 *   2. startDownload() that resolves immediately (nothing missing).
 *   3. startDownload() that runs then polls to ``done``.
 */
import { describe, it, expect, vi, beforeEach } from "vitest";
import { renderHook, act, waitFor } from "@testing-library/react";
import { useVoiceModels } from "./use-voice-models";

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

const emptyStatus = {
  model_dir: "/tmp/models",
  all_installed: false,
  missing_count: 2,
  missing_download_mb: 115,
  models: [
    {
      name: "kokoro-v1.0-int8",
      category: "tts",
      description: "Kokoro int8 TTS",
      installed: false,
      path: "/tmp/models/kokoro/kokoro-v1.0.int8.onnx",
      size_mb: 0,
      expected_size_mb: 88,
      download_available: true,
    },
  ],
};

const allInstalledStatus = {
  ...emptyStatus,
  all_installed: true,
  missing_count: 0,
  missing_download_mb: 0,
  models: [
    {
      ...emptyStatus.models[0]!,
      installed: true,
      size_mb: 88,
    },
  ],
};

describe("useVoiceModels", () => {
  it("fetches status on mount", async () => {
    mockGet.mockResolvedValueOnce(emptyStatus);

    const { result } = renderHook(() => useVoiceModels());

    await waitFor(() => {
      expect(result.current.status).not.toBeNull();
    });
    expect(result.current.status?.missing_count).toBe(2);
    expect(mockGet).toHaveBeenCalledWith(
      "/api/voice/models/status",
      expect.objectContaining({ schema: expect.anything() }),
    );
  });

  it("surfaces fetch errors on the statusError field", async () => {
    mockGet.mockRejectedValueOnce(new Error("offline"));

    const { result } = renderHook(() => useVoiceModels());

    await waitFor(() => {
      expect(result.current.statusError).toBe("offline");
    });
    expect(result.current.status).toBeNull();
  });

  it("startDownload short-circuits when the backend reports done", async () => {
    mockGet.mockResolvedValueOnce(emptyStatus);
    mockPost.mockResolvedValueOnce({
      task_id: "noop",
      status: "done",
      total_models: 0,
      completed_models: 0,
      current_model: null,
      error: null,
    });
    // After short-circuit, the hook refreshes — return the installed state.
    mockGet.mockResolvedValueOnce(allInstalledStatus);

    const { result } = renderHook(() => useVoiceModels());
    await waitFor(() => expect(result.current.status).not.toBeNull());

    await act(async () => {
      await result.current.startDownload();
    });

    expect(mockPost).toHaveBeenCalledWith(
      "/api/voice/models/download",
      {},
      expect.objectContaining({ schema: expect.anything() }),
    );
    await waitFor(() => {
      expect(result.current.status?.all_installed).toBe(true);
    });
    expect(result.current.downloading).toBe(false);
  });

  it("polls download progress until terminal and refreshes status", async () => {
    mockGet.mockResolvedValueOnce(emptyStatus);
    mockPost.mockResolvedValueOnce({
      task_id: "abc",
      status: "running",
      total_models: 2,
      completed_models: 0,
      current_model: "silero-vad-v5",
      error: null,
    });
    // First poll: still running. Second poll: done. Then refresh() fires.
    mockGet
      .mockResolvedValueOnce({
        task_id: "abc",
        status: "running",
        total_models: 2,
        completed_models: 1,
        current_model: "kokoro-v1.0-int8",
        error: null,
      })
      .mockResolvedValueOnce({
        task_id: "abc",
        status: "done",
        total_models: 2,
        completed_models: 2,
        current_model: null,
        error: null,
      })
      .mockResolvedValueOnce(allInstalledStatus);

    const { result } = renderHook(() => useVoiceModels());
    await waitFor(() => expect(result.current.status).not.toBeNull());

    await act(async () => {
      await result.current.startDownload();
    });

    await waitFor(
      () => {
        expect(result.current.download?.status).toBe("done");
      },
      { timeout: 4000 },
    );
    await waitFor(() => {
      expect(result.current.status?.all_installed).toBe(true);
    });
  });
});
