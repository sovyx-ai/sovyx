/**
 * API client tests — POLISH-18.
 *
 * Tests token management, request building, error handling, AbortController.
 */

import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { setToken, clearToken, api, isAbortError } from "./api";

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

  it("passes AbortSignal through", async () => {
    const controller = new AbortController();
    mockFetch.mockResolvedValueOnce({
      ok: true,
      status: 200,
      json: () => Promise.resolve({}),
    });

    await api.get("/api/status", { signal: controller.signal });
    const call = mockFetch.mock.calls[0] as [string, RequestInit & { headers: Record<string, string> }];
    expect(call[1].signal).toBe(controller.signal);
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
    });

    await expect(api.get("/api/status")).rejects.toThrow("Internal error");
  });

  it("clears token on 401", async () => {
    setToken("bad-token");
    mockFetch.mockResolvedValueOnce({
      ok: false,
      status: 401,
      text: () => Promise.resolve("Unauthorized"),
    });

    await expect(api.get("/api/status")).rejects.toThrow();
    expect(sessionStorage.getItem("sovyx_token")).toBeNull();
  });
});
