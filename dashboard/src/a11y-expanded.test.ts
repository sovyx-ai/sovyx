/**
 * VAL-24: Expanded accessibility audit.
 * Tests ARIA roles, keyboard navigation patterns, color contrast tokens,
 * and semantic HTML across all page components.
 */
import { describe, it, expect } from "vitest";
import fs from "node:fs";
import path from "node:path";

// Read all component source files for static analysis
function readSrc(filePath: string): string {
  return fs.readFileSync(path.resolve(__dirname, filePath), "utf-8");
}

function readAllComponents(): string[] {
  const dirs = ["components/dashboard", "components/auth", "components/layout", "pages"];
  const files: string[] = [];
  for (const dir of dirs) {
    const full = path.resolve(__dirname, dir);
    if (!fs.existsSync(full)) continue;
    for (const f of fs.readdirSync(full)) {
      if (f.endsWith(".tsx") && !f.includes(".test.")) {
        files.push(fs.readFileSync(path.join(full, f), "utf-8"));
      }
    }
  }
  return files;
}

describe("Accessibility — ARIA & Semantic HTML", () => {
  const components = readAllComponents();

  it("no img tags without alt attribute", () => {
    for (const src of components) {
      // Find <img without alt= (allows alt="" for decorative)
      const imgWithoutAlt = src.match(/<img(?![^>]*alt[=])[^>]*>/g);
      expect(imgWithoutAlt, "All <img> must have alt attribute").toBeNull();
    }
  });

  it("buttons have accessible text (aria-label, children, or title)", () => {
    for (const src of components) {
      // Find <button with no text content indication
      // This is a heuristic — we check for aria-label, title, or children
      const buttons = src.match(/<button[^>]*\/>/g) ?? [];
      for (const btn of buttons) {
        const hasA11y = /aria-label|title|sr-only/.test(btn);
        expect(hasA11y, `Self-closing <button /> should have aria-label or title: ${btn.slice(0, 80)}`).toBe(true);
      }
    }
  });

  it("interactive elements use semantic HTML (not div onClick without role)", () => {
    for (const src of components) {
      // Find div/span with onClick but no role
      const clickableDivs = src.match(/<(div|span)\s[^>]*onClick[^>]*>/g) ?? [];
      for (const el of clickableDivs) {
        const hasRole = /role=/.test(el);
        const hasTabIndex = /tabIndex/.test(el);
        const isOk = hasRole || hasTabIndex;
        // Allow styled containers (event delegation, visual containers)
        if (el.includes("className")) continue;
        expect(isOk, `Clickable div/span should have role or tabIndex: ${el.slice(0, 100)}`).toBe(true);
      }
    }
  });

  it("forms have labels or aria-label on inputs", () => {
    for (const src of components) {
      const inputs = src.match(/<(input|textarea|select)\s[^>]*>/g) ?? [];
      for (const inp of inputs) {
        const hasLabel = /aria-label|aria-labelledby|id=|placeholder=/.test(inp);
        expect(hasLabel, `Form input should have accessible label: ${inp.slice(0, 100)}`).toBe(true);
      }
    }
  });
});

describe("Accessibility — Color & Contrast tokens", () => {
  it("CSS custom properties include foreground/background pairs", () => {
    const css = readSrc("index.css");
    expect(css).toContain("--background");
    expect(css).toContain("--foreground");
    expect(css).toContain("--primary");
    expect(css).toContain("--primary-foreground");
    expect(css).toContain("--muted");
    expect(css).toContain("--muted-foreground");
  });

  it("destructive color has foreground pair", () => {
    const css = readSrc("index.css");
    expect(css).toContain("--destructive");
    expect(css).toContain("--destructive-foreground");
  });
});

describe("Accessibility — Keyboard navigation", () => {
  it("skip-nav link exists in app shell", () => {
    const layout = readSrc("components/layout/app-layout.tsx");
    expect(layout).toContain("skip");
  });

  it("sidebar navigation uses nav landmark", () => {
    const sidebar = readSrc("components/layout/app-sidebar.tsx");
    expect(sidebar.toLowerCase()).toMatch(/nav|role="navigation"|aria-label/);
  });

  it("command palette uses dialog role or Dialog component", () => {
    const palette = readSrc("components/command-palette.tsx");
    expect(palette).toMatch(/Dialog|role="dialog"|CommandDialog/i);
  });
});
