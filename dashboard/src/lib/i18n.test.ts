/**
 * Translation-completeness gate — Mission v0.30.3 §T3.5 (D4).
 *
 * Asserts that every key in en/*.json has a corresponding key in
 * pt-BR/*.json AND es/*.json. Catches translation drift at CI:
 * adding a new EN key without translating it surfaces here as a
 * concrete diff list of missing keys per locale.
 *
 * The walk is recursive — nested namespaces (mind.forget.*,
 * wizard.results.diagnosis.*, etc.) are flattened to dotted paths
 * and the assertion fails with the exact missing path so the fix is
 * a one-line edit in the matching JSON.
 *
 * The gate is INTENTIONALLY directional: en is the source of truth.
 * A pt-BR or es key with no en counterpart is also surfaced (extra
 * keys = stale translation that needs cleanup), but the priority is
 * "every en key MUST be translated".
 */
import { describe, it, expect } from "vitest";
import i18n from "./i18n";

const NAMESPACES = [
  "common",
  "overview",
  "conversations",
  "brain",
  "logs",
  "settings",
  "voice",
  "about",
  "chat",
  "plugins",
] as const;

type ResourceTree = Record<string, unknown>;

function flatten(tree: ResourceTree, prefix = ""): string[] {
  const keys: string[] = [];
  for (const [k, v] of Object.entries(tree)) {
    const path = prefix === "" ? k : `${prefix}.${k}`;
    if (v !== null && typeof v === "object" && !Array.isArray(v)) {
      keys.push(...flatten(v as ResourceTree, path));
    } else {
      keys.push(path);
    }
  }
  return keys.sort();
}

function getResource(locale: string, namespace: string): ResourceTree {
  const bundle = i18n.getResourceBundle(locale, namespace) as
    | ResourceTree
    | undefined;
  if (bundle === undefined) {
    throw new Error(`Missing bundle: ${locale}/${namespace}`);
  }
  return bundle;
}

describe("translation completeness — every en key has pt-BR + es counterpart", () => {
  for (const ns of NAMESPACES) {
    const enKeys = flatten(getResource("en", ns));

    it(`pt-BR has every en key for namespace "${ns}"`, () => {
      const ptKeys = new Set(flatten(getResource("pt-BR", ns)));
      const missing = enKeys.filter((k) => !ptKeys.has(k));
      expect(
        missing,
        `Missing pt-BR keys in ${ns}: ${missing.join(", ")}`,
      ).toEqual([]);
    });

    it(`es has every en key for namespace "${ns}"`, () => {
      const esKeys = new Set(flatten(getResource("es", ns)));
      const missing = enKeys.filter((k) => !esKeys.has(k));
      expect(
        missing,
        `Missing es keys in ${ns}: ${missing.join(", ")}`,
      ).toEqual([]);
    });

    it(`pt-BR does not carry stale keys absent from en for namespace "${ns}"`, () => {
      const ptKeys = flatten(getResource("pt-BR", ns));
      const enSet = new Set(enKeys);
      const stale = ptKeys.filter((k) => !enSet.has(k));
      expect(
        stale,
        `Stale pt-BR keys in ${ns} (not in en): ${stale.join(", ")}`,
      ).toEqual([]);
    });

    it(`es does not carry stale keys absent from en for namespace "${ns}"`, () => {
      const esKeys = flatten(getResource("es", ns));
      const enSet = new Set(enKeys);
      const stale = esKeys.filter((k) => !enSet.has(k));
      expect(
        stale,
        `Stale es keys in ${ns} (not in en): ${stale.join(", ")}`,
      ).toEqual([]);
    });
  }
});
