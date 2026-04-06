/**
 * Logs page tests — POLISH-16.
 */

import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor } from "@/test/test-utils";
import LogsPage from "./logs";

vi.mock("@/lib/api", () => ({
  api: {
    get: vi.fn(),
  },
  isAbortError: (err: unknown) =>
    err instanceof DOMException && (err as DOMException).name === "AbortError",
}));

import { api } from "@/lib/api";

const mockApi = api as unknown as { get: ReturnType<typeof vi.fn> };

beforeEach(() => {
  vi.clearAllMocks();
});

describe("LogsPage", () => {
  it("shows empty state when no logs", async () => {
    mockApi.get.mockResolvedValueOnce({ entries: [] });
    render(<LogsPage />);
    await waitFor(() => {
      expect(screen.getByText(/log entries will stream/i)).toBeInTheDocument();
    });
  });

  it("renders log count on success", async () => {
    mockApi.get.mockResolvedValueOnce({
      entries: [
        {
          timestamp: new Date().toISOString(),
          level: "INFO",
          logger: "sovyx.engine",
          event: "Engine started",
        },
      ],
    });
    render(<LogsPage />);
    await waitFor(() => {
      // Shows "1 entries" count header (virtualised rows may not be visible in jsdom)
      expect(screen.getByText(/1/)).toBeInTheDocument();
    });
  });

  it("shows error state on fetch failure", async () => {
    mockApi.get.mockRejectedValueOnce(new Error("Network error"));
    render(<LogsPage />);
    await waitFor(() => {
      expect(screen.getByText(/failed to load/i)).toBeInTheDocument();
    });
  });

  it("renders level filter buttons", async () => {
    mockApi.get.mockResolvedValueOnce({ entries: [] });
    render(<LogsPage />);
    expect(screen.getByText("ALL")).toBeInTheDocument();
    expect(screen.getByText("DEBUG")).toBeInTheDocument();
    expect(screen.getByText("ERROR")).toBeInTheDocument();
  });
});
