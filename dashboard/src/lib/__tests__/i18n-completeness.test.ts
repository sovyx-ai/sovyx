/**
 * VAL-23: i18n completeness — verify all translation keys exist,
 * all namespaces load, and no namespace is empty.
 */
import { describe, it, expect } from "vitest";
import i18n from "@/lib/i18n";

const EXPECTED_NAMESPACES = [
  "common", "overview", "conversations", "brain", "logs", "settings",
  "voice", "emotions", "productivity", "plugins", "home", "about",
] as const;

describe("i18n completeness", () => {
  it("initializes with English as default language", () => {
    expect(i18n.language).toBe("en");
  });

  it("has all expected namespaces loaded", () => {
    for (const ns of EXPECTED_NAMESPACES) {
      const bundle = i18n.getResourceBundle("en", ns);
      expect(bundle, `Namespace "${ns}" should be loaded`).toBeDefined();
      expect(Object.keys(bundle).length, `Namespace "${ns}" should not be empty`).toBeGreaterThan(0);
    }
  });

  it("all namespaces are registered", () => {
    const registered = i18n.options.ns;
    for (const ns of EXPECTED_NAMESPACES) {
      expect(registered, `Namespace "${ns}" should be registered`).toContain(ns);
    }
  });

  it("common namespace has essential keys", () => {
    const essential = ["app.name", "nav.overview", "nav.brain", "nav.logs", "nav.conversations", "nav.settings"];
    for (const key of essential) {
      const val = i18n.t(key, { ns: "common" });
      expect(val, `Key "common:${key}" should exist and not be empty`).toBeTruthy();
      expect(val, `Key "common:${key}" should not be the raw key`).not.toBe(key);
    }
  });

  it("no namespace has missing fallback (key === value)", () => {
    for (const ns of EXPECTED_NAMESPACES) {
      const bundle = i18n.getResourceBundle("en", ns);
      const keys = flattenKeys(bundle);
      for (const key of keys) {
        const val = i18n.t(key, { ns });
        // If t() returns the key itself, the key is missing
        expect(val, `Key "${ns}:${key}" should not return raw key`).not.toBe(key);
      }
    }
  });

  it("interpolation is disabled for React", () => {
    expect(i18n.options.interpolation?.escapeValue).toBe(false);
  });

  it("suspense is disabled (bundled resources)", () => {
    expect(i18n.options.react?.useSuspense).toBe(false);
  });
});

/** Flatten nested keys: { a: { b: "x" } } → ["a.b"] */
function flattenKeys(obj: Record<string, unknown>, prefix = ""): string[] {
  const keys: string[] = [];
  for (const [k, v] of Object.entries(obj)) {
    const full = prefix ? `${prefix}.${k}` : k;
    if (typeof v === "object" && v !== null && !Array.isArray(v)) {
      keys.push(...flattenKeys(v as Record<string, unknown>, full));
    } else {
      keys.push(full);
    }
  }
  return keys;
}
