import { describe, it, expect } from "vitest";
import { readFileSync } from "fs";
import { resolve } from "path";

/**
 * FE-25: Accessibility audit — CSS-level checks
 * Validates WCAG AA compliance in stylesheets.
 */
describe("Accessibility — CSS audit", () => {
  const css = readFileSync(resolve(__dirname, "index.css"), "utf-8");

  it("has skip-nav styles for keyboard users", () => {
    expect(css).toContain(".skip-nav");
    expect(css).toContain("position: absolute");
    expect(css).toContain(":focus");
  });

  it("respects prefers-reduced-motion", () => {
    expect(css).toContain("prefers-reduced-motion: reduce");
    expect(css).toContain("animation-duration: 0.01ms");
    expect(css).toContain("transition-duration: 0.01ms");
  });

  it("has focus-visible ring styles", () => {
    expect(css).toContain(":focus-visible");
    expect(css).toContain("outline:");
    expect(css).toContain("outline-offset:");
  });

  it("html has lang attribute in template", () => {
    const html = readFileSync(resolve(__dirname, "../index.html"), "utf-8");
    expect(html).toMatch(/lang=["']en["']/);
  });

  it("html has viewport meta tag", () => {
    const html = readFileSync(resolve(__dirname, "../index.html"), "utf-8");
    expect(html).toContain("viewport");
    expect(html).toContain("width=device-width");
  });
});
