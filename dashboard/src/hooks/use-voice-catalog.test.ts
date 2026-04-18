/**
 * Tests for :func:`useVoiceCatalog`.
 *
 * Covers the language alias table, fallback handling (undefined → null),
 * and the three derived selectors (``voicesForLanguage``, ``recommendedFor``,
 * ``normaliseLanguage``). The schema validation layer is exercised by
 * passing a well-formed catalog that matches the server contract.
 */
import { describe, it, expect, vi, beforeEach } from "vitest";
import { renderHook, waitFor, act } from "@testing-library/react";
import { useVoiceCatalog } from "./use-voice-catalog";

const mockGet = vi.fn();

vi.mock("@/lib/api", () => ({
  api: {
    get: (...args: unknown[]) => mockGet(...args),
  },
}));

const catalog = {
  supported_languages: ["en-us", "en-gb", "pt-br", "es", "ja"],
  by_language: {
    "en-us": [
      { id: "af_heart", display_name: "Heart", language: "en-us", gender: "female" },
      { id: "am_adam", display_name: "Adam", language: "en-us", gender: "male" },
    ],
    "en-gb": [
      { id: "bf_emma", display_name: "Emma", language: "en-gb", gender: "female" },
    ],
    "pt-br": [
      { id: "pf_dora", display_name: "Dora", language: "pt-br", gender: "female" },
    ],
    es: [
      { id: "ef_luna", display_name: "Luna", language: "es", gender: "female" },
    ],
    ja: [
      { id: "jf_alpha", display_name: "Alpha", language: "ja", gender: "female" },
    ],
  },
  recommended_per_language: {
    "en-us": "af_heart",
    "en-gb": "bf_emma",
    "pt-br": "pf_dora",
    es: "ef_luna",
    ja: "jf_alpha",
  },
};

beforeEach(() => {
  mockGet.mockReset();
  mockGet.mockResolvedValue(catalog);
});

describe("useVoiceCatalog", () => {
  it("fetches /api/voice/voices once on mount", async () => {
    const { result } = renderHook(() => useVoiceCatalog());

    await waitFor(() => {
      expect(result.current.loading).toBe(false);
    });

    expect(mockGet).toHaveBeenCalledTimes(1);
    expect(mockGet.mock.calls[0][0]).toBe("/api/voice/voices");
    expect(result.current.catalog).toEqual(catalog);
  });

  it("aliases bare language codes to canonical catalog keys", async () => {
    const { result } = renderHook(() => useVoiceCatalog());
    await waitFor(() => expect(result.current.catalog).not.toBeNull());

    // `en` → `en-us`, `pt` → `pt-br` (matches server _LANGUAGE_ALIASES)
    expect(result.current.normaliseLanguage("en")).toBe("en-us");
    expect(result.current.normaliseLanguage("EN")).toBe("en-us");
    expect(result.current.normaliseLanguage("en_US")).toBe("en-us");
    expect(result.current.normaliseLanguage("pt")).toBe("pt-br");
    expect(result.current.normaliseLanguage("pt-BR")).toBe("pt-br");
    expect(result.current.normaliseLanguage("es-MX")).toBe("es");
    expect(result.current.normaliseLanguage("ja-JP")).toBe("ja");
    expect(result.current.normaliseLanguage("en-GB")).toBe("en-gb");
  });

  it("returns null for unsupported languages", async () => {
    const { result } = renderHook(() => useVoiceCatalog());
    await waitFor(() => expect(result.current.catalog).not.toBeNull());

    expect(result.current.normaliseLanguage("xx")).toBeNull();
    expect(result.current.normaliseLanguage("klingon")).toBeNull();
  });

  it("voicesForLanguage returns the catalog slice for canonicalised language", async () => {
    const { result } = renderHook(() => useVoiceCatalog());
    await waitFor(() => expect(result.current.catalog).not.toBeNull());

    expect(result.current.voicesForLanguage("pt").map((v) => v.id)).toEqual([
      "pf_dora",
    ]);
    expect(result.current.voicesForLanguage("en-US").map((v) => v.id)).toEqual([
      "af_heart",
      "am_adam",
    ]);
    expect(result.current.voicesForLanguage("unknown")).toEqual([]);
  });

  it("recommendedFor returns the per-language default voice id", async () => {
    const { result } = renderHook(() => useVoiceCatalog());
    await waitFor(() => expect(result.current.catalog).not.toBeNull());

    expect(result.current.recommendedFor("pt")).toBe("pf_dora");
    expect(result.current.recommendedFor("en")).toBe("af_heart");
    expect(result.current.recommendedFor("ja")).toBe("jf_alpha");
    expect(result.current.recommendedFor("xx")).toBeNull();
  });

  it("surfaces fetch errors via the error field and keeps catalog null", async () => {
    mockGet.mockReset();
    mockGet.mockRejectedValue(new Error("network down"));

    const { result } = renderHook(() => useVoiceCatalog());
    await waitFor(() => expect(result.current.loading).toBe(false));

    expect(result.current.catalog).toBeNull();
    expect(result.current.error).toBe("network down");
    expect(result.current.voicesForLanguage("en")).toEqual([]);
    expect(result.current.recommendedFor("en")).toBeNull();
    expect(result.current.normaliseLanguage("en")).toBeNull();
  });

  it("refresh() re-runs the fetch", async () => {
    const { result } = renderHook(() => useVoiceCatalog());
    await waitFor(() => expect(result.current.loading).toBe(false));

    expect(mockGet).toHaveBeenCalledTimes(1);

    await act(async () => {
      await result.current.refresh();
    });

    expect(mockGet).toHaveBeenCalledTimes(2);
  });
});
