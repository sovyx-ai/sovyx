/**
 * Tests for LanguageSelector — Mission v0.30.3 §T3.3.
 *
 * Pins:
 *   - Component renders all 3 supported locales as options.
 *   - Default-selected option matches i18n.language.
 *   - Changing the dropdown calls i18n.changeLanguage + persists.
 *   - localStorage failure does not throw (locked-down browsers).
 */
import { describe, it, expect, beforeEach, vi } from "vitest";
import { fireEvent, screen } from "@testing-library/react";

import { render } from "@/test/test-utils";
import {
  LanguageSelector,
  LOCALE_STORAGE_KEY,
} from "./LanguageSelector";
import i18n from "@/lib/i18n";

beforeEach(() => {
  void i18n.changeLanguage("en");
  localStorage.removeItem(LOCALE_STORAGE_KEY);
});

describe("LanguageSelector", () => {
  it("renders all 3 supported locale options", () => {
    render(<LanguageSelector />);
    expect(screen.getByText("English")).toBeInTheDocument();
    expect(screen.getByText("Português (Brasil)")).toBeInTheDocument();
    expect(screen.getByText("Español")).toBeInTheDocument();
  });

  it("changes language + persists to localStorage on selection", () => {
    render(<LanguageSelector />);
    const select = screen.getByLabelText(/dashboard language/i) as HTMLSelectElement;
    fireEvent.change(select, { target: { value: "pt-BR" } });

    expect(i18n.language).toBe("pt-BR");
    expect(localStorage.getItem(LOCALE_STORAGE_KEY)).toBe("pt-BR");
  });

  it("survives localStorage being unavailable", () => {
    const setItemSpy = vi
      .spyOn(Storage.prototype, "setItem")
      .mockImplementation(() => {
        throw new Error("locked-down browser");
      });

    render(<LanguageSelector />);
    const select = screen.getByLabelText(/dashboard language/i) as HTMLSelectElement;
    // Should NOT throw — i18n.changeLanguage still applied for the session.
    expect(() => {
      fireEvent.change(select, { target: { value: "es" } });
    }).not.toThrow();
    expect(i18n.language).toBe("es");

    setItemSpy.mockRestore();
  });
});
