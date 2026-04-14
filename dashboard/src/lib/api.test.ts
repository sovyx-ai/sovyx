/**
 * API client tests — POLISH-18.
 *
 * Tests token management, request building, error handling, AbortController.
 */

import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { setToken, clearToken, api, isAbortError, buildQuery } from "./api";

// Mock fetch
const mockFetch = vi.fn();
global.fetch = mockFetch;

beforeEach(() => {
  vi.clearAllMocks();
  clearToken();
});

afterEach(() => {
  clearToken();
});

describe("Token management", () => {
  it("setToken stores token in sessionStorage (not localStorage)", () => {
    setToken("test-token");
    expect(sessionStorage.getItem("sovyx_token")).toBe("test-token");
    expect(localStorage.getItem("sovyx_token")).toBeNull();
  });

  it("clearToken removes token from sessionStorage", () => {
    setToken("test-token");
    clearToken();
    expect(sessionStorage.getItem("sovyx_token")).toBeNull();
  });
});

describe("isAbortError", () => {
  it("returns true for AbortError DOMException", () => {
    const err = new DOMException("Aborted", "AbortError");
    expect(isAbortError(err)).toBe(true);
  });

  it("returns false for other errors", () => {
    expect(isAbortError(new Error("Network"))).toBe(false);
    expect(isAbortError(null)).toBe(false);
  });
});

describe("api.get", () => {
  it("sends GET request with auth header", async () => {
    setToken("my-token");
    mockFetch.mockResolvedValueOnce({
      ok: true,
      status: 200,
      json: () => Promise.resolve({ data: "ok" }),
    });

    const result = await api.get("/api/status");
    expect(result).toEqual({ data: "ok" });

    const call = mockFetch.mock.calls[0] as [string, RequestInit & { headers: Record<string, string> }];
    expect(call[0]).toContain("/api/status");
    expect(call[1].headers.Authorization).toBe("Bearer my-token");
    // GET is the default method (no explicit method property)
    expect(call[1].method).toBeUndefined();
  });

  it("does not send Content-Type on GET", async () => {
    setToken("tok");
    mockFetch.mockResolvedValueOnce({
      ok: true,
      status: 200,
      json: () => Promise.resolve({}),
    });

    await api.get("/api/status");
    const call = mockFetch.mock.calls[0] as [string, RequestInit & { headers: Record<string, string> }];
    expect(call[1].headers["Content-Type"]).toBeUndefined();
  });

  it("propagates caller AbortSignal abort into the internal fetch signal", async () => {
    const controller = new AbortController();
    let capturedSignal: AbortSignal | null = null;
    mockFetch.mockImplementation((_url: string, init: RequestInit) => {
      capturedSignal = init.signal ?? null;
      return new Promise(() => {}); // never resolves
    });

    void api.get("/api/status", { signal: controller.signal, retries: 0 }).catch(() => {});
    // Let microtasks run so fetchOnce attaches its listener
    await Promise.resolve();
    expect(capturedSignal).not.toBeNull();
    expect(capturedSignal!.aborted).toBe(false);
    controller.abort();
    expect(capturedSignal!.aborted).toBe(true);
  });
});

describe("api.put", () => {
  it("sends Content-Type on PUT with body", async () => {
    setToken("tok");
    mockFetch.mockResolvedValueOnce({
      ok: true,
      status: 200,
      json: () => Promise.resolve({ log_level: "DEBUG" }),
    });

    await api.put("/api/settings", { log_level: "DEBUG" });
    const call = mockFetch.mock.calls[0] as [string, RequestInit & { headers: Record<string, string> }];
    expect(call[1].headers["Content-Type"]).toBe("application/json");
    expect(call[1].method).toBe("PUT");
    expect(JSON.parse(call[1].body as string)).toEqual({ log_level: "DEBUG" });
  });
});

describe("Error handling", () => {
  it("throws ApiError on non-ok response", async () => {
    setToken("tok");
    mockFetch.mockResolvedValueOnce({
      ok: false,
      status: 500,
      text: () => Promise.resolve("Internal error"),
      headers: new Headers(),
    });

    await expect(api.get("/api/status")).rejects.toThrow("Internal error");
  });

  it("clears token on 401", async () => {
    setToken("bad-token");
    mockFetch.mockResolvedValueOnce({
      ok: false,
      status: 401,
      text: () => Promise.resolve("Unauthorized"),
      headers: new Headers(),
    });

    await expect(api.get("/api/status")).rejects.toThrow();
    expect(sessionStorage.getItem("sovyx_token")).toBeNull();
  });
});

describe("api.patch", () => {
  it("sends PATCH with JSON body + Content-Type", async () => {
    setToken("tok");
    mockFetch.mockResolvedValueOnce({
      ok: true,
      status: 200,
      json: () => Promise.resolve({ ok: true }),
    });
    await api.patch("/api/settings", { tone: "warm" });
    const call = mockFetch.mock.calls[0] as [string, RequestInit & { headers: Record<string, string> }];
    expect(call[1].method).toBe("PATCH");
    expect(call[1].headers["Content-Type"]).toBe("application/json");
    expect(JSON.parse(call[1].body as string)).toEqual({ tone: "warm" });
  });
});

describe("buildQuery", () => {
  it("serializes string/number/boolean values", () => {
    expect(buildQuery({ a: "x", b: 2, c: true })).toBe("?a=x&b=2&c=true");
  });
  it("drops undefined and null entries", () => {
    expect(buildQuery({ a: "x", b: undefined, c: null })).toBe("?a=x");
  });
  it("returns empty string when every entry is dropped", () => {
    expect(buildQuery({ a: undefined, b: null })).toBe("");
  });
  it("URL-encodes keys and values", () => {
    expect(buildQuery({ "q key": "a b&c" })).toBe("?q+key=a+b%26c");
  });
});

describe("Retry / backoff", () => {
  beforeEach(() => {
    vi.useFakeTimers();
  });
  afterEach(() => {
    vi.useRealTimers();
  });

  it("retries a GET after 503 and returns the retried response", async () => {
    setToken("tok");
    mockFetch
      .mockResolvedValueOnce({ ok: false, status: 503, text: () => Promise.resolve("busy"), headers: new Headers() })
      .mockResolvedValueOnce({ ok: true, status: 200, json: () => Promise.resolve({ retried: true }) });

    const promise = api.get("/api/status");
    await vi.runAllTimersAsync();
    await expect(promise).resolves.toEqual({ retried: true });
    expect(mockFetch).toHaveBeenCalledTimes(2);
  });

  it("honors Retry-After seconds on 429", async () => {
    setToken("tok");
    mockFetch
      .mockResolvedValueOnce({
        ok: false,
        status: 429,
        text: () => Promise.resolve("slow down"),
        headers: new Headers({ "Retry-After": "1" }),
      })
      .mockResolvedValueOnce({ ok: true, status: 200, json: () => Promise.resolve({ ok: true }) });

    const promise = api.get("/api/status");
    await vi.advanceTimersByTimeAsync(1001);
    await expect(promise).resolves.toEqual({ ok: true });
    expect(mockFetch).toHaveBeenCalledTimes(2);
  });

  it("does NOT retry POST by default", async () => {
    setToken("tok");
    mockFetch.mockResolvedValueOnce({
      ok: false,
      status: 503,
      text: () => Promise.resolve("busy"),
      headers: new Headers(),
    });

    const promise = api.post("/api/chat", { message: "x" });
    const assertion = expect(promise).rejects.toThrow();
    await vi.runAllTimersAsync();
    await assertion;
    expect(mockFetch).toHaveBeenCalledTimes(1);
  });

  it("retries POST when caller opts in via retries: N", async () => {
    setToken("tok");
    mockFetch
      .mockResolvedValueOnce({ ok: false, status: 503, text: () => Promise.resolve(""), headers: new Headers() })
      .mockResolvedValueOnce({ ok: true, status: 200, json: () => Promise.resolve({ ok: true }) });

    const promise = api.post("/api/chat", { message: "x" }, { retries: 1 });
    await vi.runAllTimersAsync();
    await expect(promise).resolves.toEqual({ ok: true });
    expect(mockFetch).toHaveBeenCalledTimes(2);
  });

  it("stops retrying on 400 (non-retryable status)", async () => {
    setToken("tok");
    mockFetch.mockResolvedValueOnce({
      ok: false,
      status: 400,
      text: () => Promise.resolve("bad"),
      headers: new Headers(),
    });

    const promise = api.get("/api/status");
    const assertion = expect(promise).rejects.toThrow("bad");
    await vi.runAllTimersAsync();
    await assertion;
    expect(mockFetch).toHaveBeenCalledTimes(1);
  });
});

describe("Timeout", () => {
  beforeEach(() => {
    vi.useFakeTimers();
  });
  afterEach(() => {
    vi.useRealTimers();
  });

  it("passes the internal AbortSignal to fetch so timeouts can cancel", async () => {
    setToken("tok");
    let capturedSignal: AbortSignal | null = null;
    mockFetch.mockImplementation((_url: string, init: RequestInit) => {
      capturedSignal = init.signal ?? null;
      // Never resolve — we only want to observe the signal.
      return new Promise(() => {});
    });

    // Don't await the promise — it never resolves. Just let the timer fire
    // and verify the signal we handed to fetch got aborted.
    void api.get("/api/slow", { timeout: 100, retries: 0 }).catch(() => {});
    await vi.advanceTimersByTimeAsync(150);
    expect(capturedSignal).not.toBeNull();
    expect(capturedSignal!.aborted).toBe(true);
  });
});
