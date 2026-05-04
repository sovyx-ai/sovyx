/**
 * Tests for i18n-detect — Mission v0.30.3 §T3.4.
 *
 * Pins:
 *   - resolveBrowserLocale: exact + prefix matches; null fallback.
 *   - applyLocaleDetection: localStorage choice wins; navigator
 *     fallback applies + persists; English navigator stays silent.
 */
import { describe, it, expect, beforeEach, vi } from "vitest";

import {
  resolveBrowserLocale,
  applyLocaleDetection,
  consumeAutoDetectedLocale,
  _resetForTests,
} from "./i18n-detect";
import i18n from "./i18n";
import { LOCALE_STORAGE_KEY } from "@/components/settings/LanguageSelector";

beforeEach(() => {
  void i18n.changeLanguage("en");
  localStorage.removeItem(LOCALE_STORAGE_KEY);
  _resetForTests();
});

describe("resolveBrowserLocale", () => {
  it("returns exact-tag matches verbatim", () => {
    expect(resolveBrowserLocale("pt-BR")).toBe("pt-BR");
    expect(resolveBrowserLocale("es")).toBe("es");
    expect(resolveBrowserLocale("en")).toBe("en");
  });

  it("matches non-canonical Portuguese variants to pt-BR", () => {
    expect(resolveBrowserLocale("pt-PT")).toBe("pt-BR");
    expect(resolveBrowserLocale("pt")).toBe("pt-BR");
  });

  it("matches LATAM/Spain Spanish variants to es", () => {
    expect(resolveBrowserLocale("es-MX")).toBe("es");
    expect(resolveBrowserLocale("es-ES")).toBe("es");
    expect(resolveBrowserLocale("es-AR")).toBe("es");
  });

  it("returns null for unsupported languages", () => {
    expect(resolveBrowserLocale("fr")).toBeNull();
    expect(resolveBrowserLocale("de-DE")).toBeNull();
    expect(resolveBrowserLocale("ja")).toBeNull();
  });

  it("returns null for empty / undefined input", () => {
    expect(resolveBrowserLocale(undefined)).toBeNull();
    expect(resolveBrowserLocale("")).toBeNull();
    expect(resolveBrowserLocale("   ")).toBeNull();
  });
});

describe("applyLocaleDetection", () => {
  it("honours an explicit localStorage choice over navigator.language", () => {
    localStorage.setItem(LOCALE_STORAGE_KEY, "es");
    vi.spyOn(navigator, "language", "get").mockReturnValue("pt-BR");

    applyLocaleDetection();

    expect(i18n.language).toBe("es");
    // No auto-detect flag — operator chose explicitly.
    expect(consumeAutoDetectedLocale()).toBeNull();
  });

  it("auto-detects pt-BR from navigator on first visit + flags toast", () => {
    vi.spyOn(navigator, "language", "get").mockReturnValue("pt-BR");
    applyLocaleDetection();

    expect(i18n.language).toBe("pt-BR");
    expect(localStorage.getItem(LOCALE_STORAGE_KEY)).toBe("pt-BR");
    expect(consumeAutoDetectedLocale()).toBe("pt-BR");
  });

  it("does NOT flag toast when navigator already says English", () => {
    vi.spyOn(navigator, "language", "get").mockReturnValue("en-US");
    applyLocaleDetection();

    expect(i18n.language).toBe("en");
    expect(consumeAutoDetectedLocale()).toBeNull();
  });

  it("does NOT flag toast when navigator language is unsupported", () => {
    vi.spyOn(navigator, "language", "get").mockReturnValue("fr-FR");
    applyLocaleDetection();

    expect(i18n.language).toBe("en");
    expect(consumeAutoDetectedLocale()).toBeNull();
  });
});

describe("consumeAutoDetectedLocale", () => {
  it("clears the flag on first read (StrictMode safety)", () => {
    vi.spyOn(navigator, "language", "get").mockReturnValue("es");
    applyLocaleDetection();

    expect(consumeAutoDetectedLocale()).toBe("es");
    // Second read returns null — toast won't render twice under
    // React StrictMode's double-mount.
    expect(consumeAutoDetectedLocale()).toBeNull();
  });
});
