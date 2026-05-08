/**
 * Tests for ``useResolvedMindId`` + ``useOnboardingState``.
 *
 * Mission: BT.B.1 — closes the structural side of CLAUDE.md
 * anti-pattern #35 by funneling every consumer through one
 * resolver + emitting a single warn breadcrumb on fallback.
 *
 * Coverage:
 *   1. Happy path — returns resolved id from /api/onboarding/state.
 *   2. Null mind_id — returns "default" + isFallback + single warn.
 *   3. Fetch error — returns "default" + isFallback + single warn.
 *   4. Loading transitions — isLoading flips false post-resolution.
 *   5. Single-fire console.warn — only one breadcrumb across multiple
 *      consumers + multiple renders.
 *   6. Singleton dedup — multiple consumers share ONE fetch.
 *   7. ``useOnboardingState`` parity — same singleton, full payload.
 *   8. Reset for tests — ``__resetResolvedMindIdCacheForTests``
 *      restores singleton to its loading state.
 */
import { describe, it, expect, vi, beforeEach } from "vitest";
import { renderHook, waitFor, act } from "@testing-library/react";

// Mock the API surface BEFORE importing the hook so the singleton's
// import-time access reaches the mocked module.
vi.mock("@/lib/api", () => {
  return {
    api: {
      get: vi.fn(),
    },
    isAbortError: (err: unknown) =>
      err instanceof DOMException && (err as DOMException).name === "AbortError",
    BASE_URL: "",
    getToken: () => "test-token",
    setToken: vi.fn(),
    clearToken: vi.fn(),
  };
});

import { api } from "@/lib/api";
import {
  useResolvedMindId,
  useOnboardingState,
  __resetResolvedMindIdCacheForTests,
} from "./use-resolved-mind-id";

const mockApi = api as unknown as { get: ReturnType<typeof vi.fn> };

beforeEach(() => {
  __resetResolvedMindIdCacheForTests();
  mockApi.get.mockReset();
});

describe("useResolvedMindId — happy path", () => {
  it("resolves the mind id from /api/onboarding/state", async () => {
    mockApi.get.mockResolvedValueOnce({
      complete: true,
      mind_name: "Real",
      mind_id: "meu-mind",
      provider_configured: true,
      default_provider: "anthropic",
      default_model: "claude-3",
      ollama_available: false,
      ollama_models: [],
    });

    const { result } = renderHook(() => useResolvedMindId());

    // Initial render: still loading.
    expect(result.current.isLoading).toBe(true);
    expect(result.current.isFallback).toBe(true);
    expect(result.current.mindId).toBe("default");

    await waitFor(() => {
      expect(result.current.isLoading).toBe(false);
    });

    expect(result.current.mindId).toBe("meu-mind");
    expect(result.current.isFallback).toBe(false);
    expect(mockApi.get).toHaveBeenCalledTimes(1);
    expect(mockApi.get).toHaveBeenCalledWith(
      "/api/onboarding/state",
      expect.objectContaining({ schema: expect.anything() }),
    );
  });
});

describe("useResolvedMindId — null mind_id fallback", () => {
  it("falls back to 'default' when daemon yields null mind_id", async () => {
    const warnSpy = vi.spyOn(console, "warn").mockImplementation(() => {});
    try {
      mockApi.get.mockResolvedValueOnce({
        complete: false,
        mind_name: "Sovyx",
        mind_id: null,
        provider_configured: false,
        default_provider: "",
        default_model: "",
        ollama_available: false,
        ollama_models: [],
      });

      const { result } = renderHook(() => useResolvedMindId());

      await waitFor(() => {
        expect(result.current.isLoading).toBe(false);
      });

      expect(result.current.mindId).toBe("default");
      expect(result.current.isFallback).toBe(true);
      // Single warn fired with the "null mind_id" reason.
      expect(warnSpy).toHaveBeenCalledTimes(1);
      const msg = warnSpy.mock.calls[0]![0] as string;
      expect(msg).toContain("[useResolvedMindId]");
      expect(msg).toContain("null mind_id");
    } finally {
      warnSpy.mockRestore();
    }
  });

  it("falls back when daemon yields literal 'default' string", async () => {
    const warnSpy = vi.spyOn(console, "warn").mockImplementation(() => {});
    try {
      mockApi.get.mockResolvedValueOnce({
        complete: false,
        mind_name: "Sovyx",
        mind_id: "default",
        provider_configured: false,
        default_provider: "",
        default_model: "",
        ollama_available: false,
        ollama_models: [],
      });

      const { result } = renderHook(() => useResolvedMindId());

      await waitFor(() => {
        expect(result.current.isLoading).toBe(false);
      });

      // Even though the daemon returned a non-null string, the literal
      // "default" still triggers the fallback flag — it's the sentinel,
      // not a real mind id.
      expect(result.current.mindId).toBe("default");
      expect(result.current.isFallback).toBe(true);
      expect(warnSpy).toHaveBeenCalledTimes(1);
      const msg = warnSpy.mock.calls[0]![0] as string;
      expect(msg).toContain('"default"');
    } finally {
      warnSpy.mockRestore();
    }
  });
});

describe("useResolvedMindId — fetch error fallback", () => {
  it("falls back to 'default' on network error", async () => {
    const warnSpy = vi.spyOn(console, "warn").mockImplementation(() => {});
    try {
      mockApi.get.mockRejectedValueOnce(new Error("network down"));

      const { result } = renderHook(() => useResolvedMindId());

      await waitFor(() => {
        expect(result.current.isLoading).toBe(false);
      });

      expect(result.current.mindId).toBe("default");
      expect(result.current.isFallback).toBe(true);
      expect(warnSpy).toHaveBeenCalledTimes(1);
      const msg = warnSpy.mock.calls[0]![0] as string;
      expect(msg).toContain("fetch failed");
      expect(msg).toContain("network down");
    } finally {
      warnSpy.mockRestore();
    }
  });
});

describe("useResolvedMindId — single-fire console.warn", () => {
  it("warns exactly ONCE across multiple consumers + renders", async () => {
    const warnSpy = vi.spyOn(console, "warn").mockImplementation(() => {});
    try {
      mockApi.get.mockResolvedValueOnce({
        complete: false,
        mind_name: "Sovyx",
        mind_id: null,
        provider_configured: false,
        default_provider: "",
        default_model: "",
        ollama_available: false,
        ollama_models: [],
      });

      // Mount 3 consumers in parallel.
      const a = renderHook(() => useResolvedMindId());
      const b = renderHook(() => useResolvedMindId());
      const c = renderHook(() => useResolvedMindId());

      await waitFor(() => {
        expect(a.result.current.isLoading).toBe(false);
        expect(b.result.current.isLoading).toBe(false);
        expect(c.result.current.isLoading).toBe(false);
      });

      // Re-render each consumer to ensure no second warn fires
      // post-resolution.
      a.rerender();
      b.rerender();
      c.rerender();

      expect(warnSpy).toHaveBeenCalledTimes(1);
    } finally {
      warnSpy.mockRestore();
    }
  });
});

describe("useResolvedMindId — singleton dedup", () => {
  it("multiple consumers share ONE /api/onboarding/state fetch", async () => {
    mockApi.get.mockResolvedValueOnce({
      complete: true,
      mind_name: "Real",
      mind_id: "alpha",
      provider_configured: true,
      default_provider: "anthropic",
      default_model: "claude-3",
      ollama_available: false,
      ollama_models: [],
    });

    const a = renderHook(() => useResolvedMindId());
    const b = renderHook(() => useResolvedMindId());
    const c = renderHook(() => useResolvedMindId());

    await waitFor(() => {
      expect(a.result.current.isLoading).toBe(false);
      expect(b.result.current.isLoading).toBe(false);
      expect(c.result.current.isLoading).toBe(false);
    });

    expect(a.result.current.mindId).toBe("alpha");
    expect(b.result.current.mindId).toBe("alpha");
    expect(c.result.current.mindId).toBe("alpha");
    // Only ONE network call was fired.
    expect(mockApi.get).toHaveBeenCalledTimes(1);
  });

  it("late-mounting consumer reads cached value without new fetch", async () => {
    mockApi.get.mockResolvedValueOnce({
      complete: true,
      mind_name: "Real",
      mind_id: "beta",
      provider_configured: true,
      default_provider: "anthropic",
      default_model: "claude-3",
      ollama_available: false,
      ollama_models: [],
    });

    const a = renderHook(() => useResolvedMindId());
    await waitFor(() => {
      expect(a.result.current.isLoading).toBe(false);
    });
    expect(a.result.current.mindId).toBe("beta");

    // Mount a second consumer AFTER resolution.
    const b = renderHook(() => useResolvedMindId());
    // Cached snapshot should be served IMMEDIATELY — no extra fetch.
    expect(b.result.current.isLoading).toBe(false);
    expect(b.result.current.mindId).toBe("beta");
    expect(mockApi.get).toHaveBeenCalledTimes(1);
  });
});

describe("useOnboardingState — full payload via shared singleton", () => {
  it("returns the full state once resolved", async () => {
    mockApi.get.mockResolvedValueOnce({
      complete: false,
      mind_name: "MyMind",
      mind_id: "mymind",
      provider_configured: true,
      default_provider: "anthropic",
      default_model: "claude-3-5",
      ollama_available: true,
      ollama_models: ["llama3"],
    });

    const { result } = renderHook(() => useOnboardingState());

    await waitFor(() => {
      expect(result.current.isLoading).toBe(false);
    });

    expect(result.current.state).not.toBeNull();
    expect(result.current.state?.mind_name).toBe("MyMind");
    expect(result.current.state?.ollama_available).toBe(true);
    expect(result.current.state?.ollama_models).toEqual(["llama3"]);
    expect(result.current.isError).toBe(false);
  });

  it("shares the singleton with useResolvedMindId — one fetch total", async () => {
    mockApi.get.mockResolvedValueOnce({
      complete: false,
      mind_name: "Mind",
      mind_id: "gamma",
      provider_configured: false,
      default_provider: "",
      default_model: "",
      ollama_available: false,
      ollama_models: [],
    });

    const idHook = renderHook(() => useResolvedMindId());
    const stateHook = renderHook(() => useOnboardingState());

    await waitFor(() => {
      expect(idHook.result.current.isLoading).toBe(false);
      expect(stateHook.result.current.isLoading).toBe(false);
    });

    expect(idHook.result.current.mindId).toBe("gamma");
    expect(stateHook.result.current.state?.mind_id).toBe("gamma");
    expect(mockApi.get).toHaveBeenCalledTimes(1);
  });

  it("surfaces isError=true when fetch fails", async () => {
    const warnSpy = vi.spyOn(console, "warn").mockImplementation(() => {});
    try {
      mockApi.get.mockRejectedValueOnce(new Error("boom"));

      const { result } = renderHook(() => useOnboardingState());

      await waitFor(() => {
        expect(result.current.isLoading).toBe(false);
      });

      expect(result.current.state).toBeNull();
      expect(result.current.isError).toBe(true);
    } finally {
      warnSpy.mockRestore();
    }
  });
});

describe("__resetResolvedMindIdCacheForTests", () => {
  it("restores singleton to loading state + clears warn flag", async () => {
    const warnSpy = vi.spyOn(console, "warn").mockImplementation(() => {});
    try {
      mockApi.get.mockResolvedValueOnce({
        complete: false,
        mind_name: "Sovyx",
        mind_id: null,
        provider_configured: false,
        default_provider: "",
        default_model: "",
        ollama_available: false,
        ollama_models: [],
      });

      const a = renderHook(() => useResolvedMindId());
      await waitFor(() => {
        expect(a.result.current.isLoading).toBe(false);
      });
      expect(warnSpy).toHaveBeenCalledTimes(1);

      // Reset.
      act(() => {
        __resetResolvedMindIdCacheForTests();
      });

      // Set up a fresh mock + remount.
      mockApi.get.mockResolvedValueOnce({
        complete: true,
        mind_name: "Real",
        mind_id: "post-reset",
        provider_configured: true,
        default_provider: "anthropic",
        default_model: "claude-3",
        ollama_available: false,
        ollama_models: [],
      });

      const b = renderHook(() => useResolvedMindId());
      await waitFor(() => {
        expect(b.result.current.isLoading).toBe(false);
      });
      expect(b.result.current.mindId).toBe("post-reset");
      expect(b.result.current.isFallback).toBe(false);
      // The reset cleared the warn flag, so no NEW warn is expected.
      expect(warnSpy).toHaveBeenCalledTimes(1);
    } finally {
      warnSpy.mockRestore();
    }
  });
});
