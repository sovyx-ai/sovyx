/**
 * Logs page tests — Phase 12.11.
 *
 * Pins the rewritten Phase-10 page: primary FTS endpoint,
 * 503-driven legacy fallback, empty/error/loading states, header
 * count rendering, refresh + clear actions.
 *
 * The page also opens a WebSocket; we provide a no-op global so the
 * `new WebSocket(...)` call doesn't blow up under jsdom (which lacks
 * a real implementation). React 19 + a setFilters re-sync on the
 * search-params effect cause two render passes per mount, so tests
 * use `mockImplementation` (URL-based dispatch) instead of
 * `mockResolvedValueOnce` to stay robust to call-count shifts.
 */

import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen, waitFor, fireEvent } from "@/test/test-utils";

// ── Module mocks ─────────────────────────────────────────────────────
//
// We mock `@/lib/api` instead of stubbing globalThis.fetch because the
// page validates responses with zod schemas inside `api.get`; bypassing
// it keeps the test focused on UI behavior, not transport plumbing.
vi.mock("@/lib/api", () => {
  class MockApiError extends Error {
    status: number;
    constructor(status: number, message: string) {
      super(message);
      this.status = status;
      this.name = "ApiError";
    }
  }
  return {
    api: { get: vi.fn() },
    ApiError: MockApiError,
    getToken: () => null,
    isAbortError: (err: unknown) =>
      err instanceof DOMException && err.name === "AbortError",
    BASE_URL: "",
  };
});

import { api, ApiError } from "@/lib/api";
import LogsPage from "./logs";

const mockApi = api as unknown as { get: ReturnType<typeof vi.fn> };
const Apie = ApiError as unknown as new (s: number, m: string) => Error;

// ── Mock WebSocket ───────────────────────────────────────────────────
//
// jsdom doesn't ship one. The page's WS effect just needs an object
// with the right method shape so the constructor + close() succeed.
class MockWebSocket {
  static instances: MockWebSocket[] = [];
  url: string;
  readyState = 0;
  onopen: (() => void) | null = null;
  onclose: (() => void) | null = null;
  onerror: (() => void) | null = null;
  onmessage: ((ev: { data: string }) => void) | null = null;

  constructor(url: string) {
    this.url = url;
    MockWebSocket.instances.push(this);
  }
  close(): void {
    this.readyState = 3;
  }
  send(_: string): void {}
}

beforeEach(() => {
  vi.clearAllMocks();
  MockWebSocket.instances = [];
  (globalThis as unknown as { WebSocket: typeof WebSocket }).WebSocket =
    MockWebSocket as unknown as typeof WebSocket;
});

afterEach(() => {
  vi.restoreAllMocks();
});

/** Steady-state mock: every search call returns `entries`, every legacy call too. */
function dispatchSearch(entries: unknown[]) {
  mockApi.get.mockImplementation(async (url: string) => {
    if (url.startsWith("/api/logs/search") || url.startsWith("/api/logs?")) {
      return { entries };
    }
    return { entries: [] };
  });
}

describe("LogsPage", () => {
  it("renders the empty-state copy when /api/logs/search returns no entries", async () => {
    dispatchSearch([]);
    render(<LogsPage />);
    await waitFor(() => {
      expect(screen.getByText(/log entries will stream/i)).toBeInTheDocument();
    });
  });

  it("renders the entry count in the header (singular)", async () => {
    dispatchSearch([
      {
        timestamp: new Date("2026-04-20T12:00:00Z").toISOString(),
        level: "INFO",
        logger: "sovyx.engine",
        event: "Engine started",
      },
    ]);
    render(<LogsPage />);
    await waitFor(() => {
      expect(screen.getByText(/^1 entry$/)).toBeInTheDocument();
    });
  });

  it("renders the entry count in the header (plural)", async () => {
    dispatchSearch(
      Array.from({ length: 3 }, (_, i) => ({
        timestamp: new Date(`2026-04-20T12:00:0${i}Z`).toISOString(),
        level: "INFO",
        logger: "sovyx.engine",
        event: `entry-${i}`,
      })),
    );
    render(<LogsPage />);
    await waitFor(() => {
      expect(screen.getByText(/^3 entries$/)).toBeInTheDocument();
    });
  });

  it("surfaces an error state when the search endpoint throws", async () => {
    mockApi.get.mockRejectedValue(new Error("Network error"));
    render(<LogsPage />);
    await waitFor(() => {
      expect(screen.getByText(/failed to load/i)).toBeInTheDocument();
    });
  });

  it("renders all five level filter buttons + the All pill", async () => {
    dispatchSearch([]);
    render(<LogsPage />);
    expect(await screen.findByRole("button", { name: "All" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "DEBUG" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "INFO" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "WARNING" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "ERROR" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "CRITICAL" })).toBeInTheDocument();
  });

  it("initial fetch hits /api/logs/search with limit=500 (Phase-10 primary)", async () => {
    dispatchSearch([]);
    render(<LogsPage />);
    await waitFor(() => {
      expect(mockApi.get).toHaveBeenCalled();
    });
    const url = mockApi.get.mock.calls[0]?.[0] as string;
    expect(url).toMatch(/^\/api\/logs\/search\?/);
    expect(url).toContain("limit=500");
    expect(url).not.toContain("after=");
  });

  it("falls back to /api/logs when the search endpoint replies 503", async () => {
    mockApi.get.mockImplementation(async (url: string) => {
      if (url.startsWith("/api/logs/search")) {
        throw new Apie(503, "FTS unavailable");
      }
      // Legacy file-scan endpoint succeeds.
      return {
        entries: [
          {
            timestamp: new Date("2026-04-20T12:00:00Z").toISOString(),
            level: "INFO",
            logger: "sovyx.engine",
            event: "legacy entry",
          },
        ],
      };
    });
    render(<LogsPage />);
    // The fallback badge surfaces only after the legacy fetch resolves.
    expect(
      await screen.findByText(/legacy fallback/i),
    ).toBeInTheDocument();
    const calls = mockApi.get.mock.calls.map((c) => c[0] as string);
    expect(calls.some((u) => u.startsWith("/api/logs/search"))).toBe(true);
    expect(calls.some((u) => u.startsWith("/api/logs?"))).toBe(true);
  });

  it("opens a WebSocket against /api/logs/stream after the initial fetch", async () => {
    dispatchSearch([]);
    render(<LogsPage />);
    await waitFor(() => {
      expect(MockWebSocket.instances.length).toBeGreaterThan(0);
    });
    expect(MockWebSocket.instances[0]?.url).toMatch(/\/api\/logs\/stream/);
  });

  it("does NOT open a streaming WS once the legacy fallback is active", async () => {
    mockApi.get.mockImplementation(async (url: string) => {
      if (url.startsWith("/api/logs/search")) {
        throw new Apie(503, "FTS unavailable");
      }
      return { entries: [] };
    });
    render(<LogsPage />);
    await screen.findByText(/legacy fallback/i);
    // The page closes any previously-opened streaming WS once it
    // commits to the legacy path. Verify every captured instance is
    // closed (readyState 3 = CLOSED) — none are still live.
    expect(
      MockWebSocket.instances.every((ws) => ws.readyState === 3),
    ).toBe(true);
  });

  it("clear button empties the entries panel", async () => {
    dispatchSearch([
      {
        timestamp: new Date("2026-04-20T12:00:00Z").toISOString(),
        level: "INFO",
        logger: "sovyx.engine",
        event: "entry",
      },
    ]);
    render(<LogsPage />);
    await waitFor(() => {
      expect(screen.getByText(/^1 entry$/)).toBeInTheDocument();
    });
    fireEvent.click(screen.getByTitle("Clear logs"));
    await waitFor(() => {
      expect(screen.getByText(/^0 entries$/)).toBeInTheDocument();
    });
  });

  it("retry button re-issues the search fetch", async () => {
    dispatchSearch([]);
    render(<LogsPage />);
    // Wait for the empty state to settle so the initial fetch chain is done.
    await screen.findByText(/log entries will stream/i);
    const initial = mockApi.get.mock.calls.length;
    fireEvent.click(screen.getByTitle(/retry/i));
    await waitFor(() => {
      expect(mockApi.get.mock.calls.length).toBeGreaterThan(initial);
    });
    const next = mockApi.get.mock.calls.at(-1)?.[0] as string;
    expect(next).toMatch(/^\/api\/logs\/search\?/);
  });

  it("renders the page title from the i18n bundle", async () => {
    dispatchSearch([]);
    render(<LogsPage />);
    expect(
      await screen.findByRole("heading", { name: "Logs", level: 1 }),
    ).toBeInTheDocument();
  });
});
