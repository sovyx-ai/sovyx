/**
 * Tests for LocaleAutoDetectToast — Mission v0.30.3 §T3.4.
 *
 * Pins:
 *   - Toast does NOT render when no auto-detect flag is set.
 *   - Toast renders with the detected locale's native name when
 *     consumeAutoDetectedLocale returns a value on mount.
 *   - "Use English" reverts i18n + persists override.
 *   - Toast auto-dismisses after 5 s.
 */
import { describe, it, expect, beforeEach, vi } from "vitest";
import { fireEvent, screen, act } from "@testing-library/react";

import { render } from "@/test/test-utils";
import { LocaleAutoDetectToast } from "./LocaleAutoDetectToast";
import i18n from "@/lib/i18n";
import { _resetForTests, applyLocaleDetection } from "@/lib/i18n-detect";
import { LOCALE_STORAGE_KEY } from "@/components/settings/LanguageSelector";

beforeEach(() => {
  void i18n.changeLanguage("en");
  localStorage.removeItem(LOCALE_STORAGE_KEY);
  _resetForTests();
});

describe("LocaleAutoDetectToast", () => {
  it("does NOT render when no auto-detect occurred", () => {
    render(<LocaleAutoDetectToast />);
    expect(
      screen.queryByTestId("locale-auto-detect-toast"),
    ).not.toBeInTheDocument();
  });

  it("renders with native locale name after auto-detect fires", () => {
    vi.spyOn(navigator, "language", "get").mockReturnValue("pt-BR");
    applyLocaleDetection();

    render(<LocaleAutoDetectToast />);

    const toast = screen.getByTestId("locale-auto-detect-toast");
    expect(toast).toBeInTheDocument();
    expect(toast.textContent).toContain("Português (Brasil)");
  });

  it("'Use English' reverts i18n + persists override", async () => {
    vi.spyOn(navigator, "language", "get").mockReturnValue("es");
    applyLocaleDetection();
    expect(i18n.language).toBe("es");

    render(<LocaleAutoDetectToast />);
    fireEvent.click(screen.getByRole("button", { name: /english/i }));

    expect(i18n.language).toBe("en");
    expect(localStorage.getItem(LOCALE_STORAGE_KEY)).toBe("en");
    expect(
      screen.queryByTestId("locale-auto-detect-toast"),
    ).not.toBeInTheDocument();
  });

  it("auto-dismisses after 5 seconds", () => {
    vi.useFakeTimers();
    vi.spyOn(navigator, "language", "get").mockReturnValue("pt-BR");
    applyLocaleDetection();

    render(<LocaleAutoDetectToast />);
    expect(
      screen.getByTestId("locale-auto-detect-toast"),
    ).toBeInTheDocument();

    act(() => {
      vi.advanceTimersByTime(5_000);
    });

    expect(
      screen.queryByTestId("locale-auto-detect-toast"),
    ).not.toBeInTheDocument();

    vi.useRealTimers();
  });
});
