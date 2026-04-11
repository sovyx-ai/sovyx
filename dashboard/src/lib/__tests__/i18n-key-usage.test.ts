/**
 * VAL-24: i18n key usage — verify every t("key") call in source code
 * resolves to a real translation in the locale JSON files.
 *
 * This prevents case-mismatch bugs like "channels.setup" vs "channels.setUp"
 * that cause raw keys to render in the UI.
 *
 * Strategy:
 *   1. Scan all .tsx/.ts source files (excluding tests, node_modules, .d.ts)
 *   2. Extract t("key") calls and determine their namespace from useTranslation("ns")
 *   3. Verify each key exists in the corresponding locale JSON
 *
 * This is the inverse of i18n-completeness.test.ts (which checks JSON → resolution).
 * Together they form a bidirectional i18n safety net.
 */
import { describe, it, expect } from "vitest";
import * as fs from "node:fs";
import * as path from "node:path";

// ── Load all locale bundles ──

const LOCALES_DIR = path.resolve(__dirname, "../../locales/en");

function loadBundle(ns: string): Record<string, unknown> {
  const filePath = path.join(LOCALES_DIR, `${ns}.json`);
  if (!fs.existsSync(filePath)) return {};
  return JSON.parse(fs.readFileSync(filePath, "utf-8")) as Record<string, unknown>;
}

const NAMESPACES = [
  "common", "overview", "conversations", "brain", "logs",
  "settings", "voice", "about", "chat", "plugins",
] as const;

const bundles: Record<string, Record<string, unknown>> = {};
for (const ns of NAMESPACES) {
  bundles[ns] = loadBundle(ns);
}

// ── Helpers ──

/** Resolve a dot-separated key in a nested object.
 *  Also checks i18next pluralization suffixes (_one, _other, _zero, _two, _few, _many). */
function resolveKey(obj: Record<string, unknown>, key: string): boolean {
  const parts = key.split(".");
  let current: unknown = obj;
  for (const part of parts) {
    if (current === null || current === undefined || typeof current !== "object") return false;
    current = (current as Record<string, unknown>)[part];
  }
  if (current !== undefined) return true;

  // Check pluralization: "foo.bar" may exist as "foo.bar_one" / "foo.bar_other"
  const PLURAL_SUFFIXES = ["_one", "_other", "_zero", "_two", "_few", "_many"];
  const parentParts = key.split(".");
  const lastPart = parentParts.pop()!;
  let parent: unknown = obj;
  for (const part of parentParts) {
    if (parent === null || parent === undefined || typeof parent !== "object") return false;
    parent = (parent as Record<string, unknown>)[part];
  }
  if (parent === null || parent === undefined || typeof parent !== "object") return false;
  const parentObj = parent as Record<string, unknown>;
  return PLURAL_SUFFIXES.some((suffix) => parentObj[lastPart + suffix] !== undefined);
}

/** Recursively find all .tsx/.ts source files */
function findSourceFiles(dir: string): string[] {
  const results: string[] = [];
  if (!fs.existsSync(dir)) return results;

  for (const entry of fs.readdirSync(dir, { withFileTypes: true })) {
    const full = path.join(dir, entry.name);
    if (entry.isDirectory()) {
      if (entry.name === "node_modules" || entry.name === "__tests__" || entry.name === "coverage") continue;
      results.push(...findSourceFiles(full));
    } else if (
      (entry.name.endsWith(".tsx") || entry.name.endsWith(".ts")) &&
      !entry.name.endsWith(".test.ts") &&
      !entry.name.endsWith(".test.tsx") &&
      !entry.name.endsWith(".d.ts")
    ) {
      results.push(full);
    }
  }
  return results;
}

/**
 * Extract i18n key usages from a source file.
 * Returns array of { key, namespace, line }.
 */
function extractKeyUsages(
  filePath: string,
): Array<{ key: string; namespace: string; line: number }> {
  const content = fs.readFileSync(filePath, "utf-8");
  const lines = content.split("\n");
  const usages: Array<{ key: string; namespace: string; line: number }> = [];

  // Detect namespace from useTranslation("ns") or useTranslation(["ns1", "ns2"])
  // When an array is passed, the first element is the default namespace.
  const singleNsMatch = content.match(/useTranslation\(\s*["']([^"']+)["']\s*\)/);
  const arrayNsMatch = content.match(/useTranslation\(\s*\[\s*["']([^"']+)["']/);
  const fileNamespace = singleNsMatch?.[1] ?? arrayNsMatch?.[1] ?? "common";

  // Collect all namespaces in the array for fallback resolution
  const allFileNamespaces: string[] = [fileNamespace];
  if (arrayNsMatch) {
    const arrayMatch = content.match(/useTranslation\(\s*\[([^\]]+)\]/);
    if (arrayMatch) {
      const nsEntries = arrayMatch[1].matchAll(/["']([^"']+)["']/g);
      for (const m of nsEntries) {
        if (!allFileNamespaces.includes(m[1])) {
          allFileNamespaces.push(m[1]);
        }
      }
    }
  }

  // Match t("key") and t("key", ...) patterns
  // Also handles t('key') with single quotes
  const tCallRegex = /\bt\(\s*["']([^"']+)["']/g;

  for (let i = 0; i < lines.length; i++) {
    const line = lines[i];
    let match: RegExpExecArray | null;
    tCallRegex.lastIndex = 0;

    while ((match = tCallRegex.exec(line)) !== null) {
      const rawKey = match[1];

      // Skip non-i18n patterns (imports, paths, CSS, test IDs, URLs)
      if (
        rawKey.startsWith("@/") ||
        rawKey.startsWith("./") ||
        rawKey.startsWith("../") ||
        rawKey.includes("/") ||
        rawKey.startsWith("--") ||
        rawKey.startsWith("var(") ||
        rawKey.startsWith("data-") ||
        rawKey === "button" ||
        rawKey.length > 80
      ) {
        continue;
      }

      // Handle cross-namespace references like "common:errors.generic"
      if (rawKey.includes(":")) {
        const [ns, key] = rawKey.split(":", 2);
        if (NAMESPACES.includes(ns as (typeof NAMESPACES)[number])) {
          usages.push({ key, namespace: ns, line: i + 1 });
        }
        continue;
      }

      // When useTranslation(["ns1", "ns2"]) is used, i18next tries all namespaces
      // in order. We resolve to the first namespace that contains the key.
      let resolvedNs = fileNamespace;
      if (allFileNamespaces.length > 1) {
        for (const ns of allFileNamespaces) {
          const b = bundles[ns];
          if (b && resolveKey(b, rawKey)) {
            resolvedNs = ns;
            break;
          }
        }
      }
      usages.push({ key: rawKey, namespace: resolvedNs, line: i + 1 });
    }
  }

  return usages;
}

// ── Test ──

const SRC_DIR = path.resolve(__dirname, "../..");

describe("i18n key usage (VAL-24)", () => {
  const sourceFiles = findSourceFiles(SRC_DIR);
  const allUsages: Array<{ key: string; namespace: string; file: string; line: number }> = [];

  for (const file of sourceFiles) {
    const usages = extractKeyUsages(file);
    for (const u of usages) {
      allUsages.push({ ...u, file: path.relative(SRC_DIR, file) });
    }
  }

  it("finds translation key usages in source files", () => {
    expect(allUsages.length).toBeGreaterThan(0);
  });

  it("all t() keys resolve to existing translations", () => {
    const missing: string[] = [];

    for (const { key, namespace, file, line } of allUsages) {
      const bundle = bundles[namespace];
      if (!bundle) {
        missing.push(`${file}:${line} — t("${key}") references unknown namespace "${namespace}"`);
        continue;
      }
      if (!resolveKey(bundle, key)) {
        missing.push(
          `${file}:${line} — t("${namespace}:${key}") not found in locales/en/${namespace}.json`,
        );
      }
    }

    if (missing.length > 0) {
      const report = [
        `Found ${missing.length} missing i18n key(s):`,
        "",
        ...missing.map((m) => `  ✗ ${m}`),
        "",
        "Fix: add the key to the locale JSON or correct the case in the t() call.",
      ].join("\n");
      expect.fail(report);
    }
  });

  it("no duplicate keys with different casing in same namespace", () => {
    // Build a map of lowercase → original keys per namespace
    const caseConflicts: string[] = [];

    for (const ns of NAMESPACES) {
      const bundle = bundles[ns];
      if (!bundle) continue;
      const allKeys = flattenKeys(bundle);
      const lowerMap = new Map<string, string[]>();

      for (const key of allKeys) {
        const lower = key.toLowerCase();
        const existing = lowerMap.get(lower) ?? [];
        existing.push(key);
        lowerMap.set(lower, existing);
      }

      for (const [lower, variants] of lowerMap) {
        if (variants.length > 1) {
          caseConflicts.push(
            `${ns}: keys ${variants.map((v) => `"${v}"`).join(" vs ")} differ only in case`,
          );
        }
      }
    }

    if (caseConflicts.length > 0) {
      expect.fail(
        `Case-ambiguous keys found:\n${caseConflicts.map((c) => `  ✗ ${c}`).join("\n")}`,
      );
    }
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
