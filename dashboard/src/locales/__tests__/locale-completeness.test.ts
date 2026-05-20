/**
 * Locale completeness — key-parity invariant across en / pt-BR / es.
 *
 * Mission C4 §T1.13 §9.1 row "Locale completeness" — extends the
 * dashboard's i18n discipline to guarantee that the new ``degraded.*``
 * namespace (and every existing namespace) has the SAME key tree in
 * all 3 locales. Without this guard, a missing pt-BR translation
 * silently falls back to English copy on the operator's screen even
 * though their mind language is ``pt`` — exactly the kind of silent
 * regression the v0.43.1 "decorative daemon" gap shipped.
 *
 * Generalizes across every voice.json key, but the test was created
 * to cover the C4 ``degraded.*`` additions specifically; future
 * namespaces inherit the check for free.
 */
import { describe, expect, it } from "vitest";

import enVoice from "@/locales/en/voice.json";
import ptVoice from "@/locales/pt-BR/voice.json";
import esVoice from "@/locales/es/voice.json";

type AnyJson = Record<string, unknown>;

function collectKeyPaths(
  obj: AnyJson,
  prefix: string,
  out: Set<string>,
): void {
  for (const [k, v] of Object.entries(obj)) {
    const path = prefix ? `${prefix}.${k}` : k;
    if (v !== null && typeof v === "object" && !Array.isArray(v)) {
      collectKeyPaths(v as AnyJson, path, out);
    } else {
      out.add(path);
    }
  }
}

function pathSet(obj: AnyJson): Set<string> {
  const out = new Set<string>();
  collectKeyPaths(obj, "", out);
  return out;
}

describe("Locale completeness — voice namespace", () => {
  it("en + pt-BR + es have identical key paths across the voice namespace", () => {
    const en = pathSet(enVoice as AnyJson);
    const pt = pathSet(ptVoice as AnyJson);
    const es = pathSet(esVoice as AnyJson);

    const missingInPt = [...en].filter((k) => !pt.has(k));
    const missingInEs = [...en].filter((k) => !es.has(k));
    const extraInPt = [...pt].filter((k) => !en.has(k));
    const extraInEs = [...es].filter((k) => !en.has(k));

    expect(
      { missingInPt, missingInEs, extraInPt, extraInEs },
    ).toEqual({ missingInPt: [], missingInEs: [], extraInPt: [], extraInEs: [] });
  });

  it("Mission C4 §T1.9 — all degraded.* keys present in 3 locales", () => {
    const en = pathSet(enVoice as AnyJson);
    const pt = pathSet(ptVoice as AnyJson);
    const es = pathSet(esVoice as AnyJson);

    const requiredKeys = [
      "degraded.composite.title_one",
      "degraded.composite.title_other",
      "degraded.composite.ack",
      "degraded.voice.ladderExhausted.title",
      "degraded.voice.ladderExhausted.body",
      "degraded.voice.ladderExhausted.viewHistory",
      "degraded.voice.ladderExhausted.reconnectUsb",
      "degraded.llm.noProvider.title",
      "degraded.llm.noProvider.body",
      "degraded.llm.noProvider.installOllama",
      "degraded.llm.noProvider.openSettings",
      "degraded.stt.languageCoerced.title",
      "degraded.stt.languageCoerced.body",
      "degraded.stt.languageCoerced.switchToEnglish",
      "degraded.stt.languageCoerced.learnMore",
    ];
    for (const key of requiredKeys) {
      expect(en.has(key), `EN missing ${key}`).toBe(true);
      expect(pt.has(key), `pt-BR missing ${key}`).toBe(true);
      expect(es.has(key), `ES missing ${key}`).toBe(true);
    }
  });

  it("Mission H4 §T3.5 + §T3.6 — all resources.* + heapSnapshot.* keys present in 3 locales", () => {
    const en = pathSet(enVoice as AnyJson);
    const pt = pathSet(ptVoice as AnyJson);
    const es = pathSet(esVoice as AnyJson);

    const requiredKeys = [
      "resources.title",
      "resources.subtitle",
      "resources.loading",
      "resources.degraded",
      "resources.fieldsLabel",
      "resources.sections.process.title",
      "resources.sections.process.description",
      "resources.sections.asyncio.title",
      "resources.sections.asyncio.description",
      "resources.sections.to_thread.title",
      "resources.sections.to_thread.description",
      "resources.sections.lock_dict.title",
      "resources.sections.lock_dict.description",
      "resources.sections.onnx.title",
      "resources.sections.onnx.description",
      "resources.sections.gc.title",
      "resources.sections.gc.description",
      "resources.sections.tracemalloc.title",
      "resources.sections.tracemalloc.description",
      "resources.sections.exception_cohort.title",
      "resources.sections.exception_cohort.description",
      "heapSnapshot.title",
      "heapSnapshot.subtitle",
      "heapSnapshot.cohortContext",
      "heapSnapshot.loading",
      "heapSnapshot.notFound",
      "heapSnapshot.error",
      "heapSnapshot.col.rank",
      "heapSnapshot.col.size",
      "heapSnapshot.col.count",
      "heapSnapshot.col.traceback",
      // Mission H4 v0.49.23 — per-field operator labels (closes the
      // 40-key i18n promise from spec §6 T3.5 — title + subtitle +
      // loading + degraded + fieldsLabel + 8 × 2 sections + 9 fields =
      // 40 keys per locale).
      "resources.fields.rss_bytes.label",
      "resources.fields.num_threads.label",
      "resources.fields.task_count.label",
      "resources.fields.to_thread_pool.label",
      "resources.fields.lock_dict_cardinality.label",
      "resources.fields.onnx_session_count.label",
      "resources.fields.gc_objects.label",
      "resources.fields.tracemalloc_active.label",
      "resources.fields.exception_retention.label",
      // Mission H4 v0.49.24 — spec-literal reason taxonomy (§0 line 30).
      // 6 reasons × 2 fields (title + body) = 12 keys + 2 action chip
      // labels = 14 entries per locale.
      "degraded.engine_resources.rss_growth_spike.title",
      "degraded.engine_resources.rss_growth_spike.body",
      "degraded.engine_resources.thread_count_spike.title",
      "degraded.engine_resources.thread_count_spike.body",
      "degraded.engine_resources.lock_dict_cardinality_saturated.title",
      "degraded.engine_resources.lock_dict_cardinality_saturated.body",
      "degraded.engine_resources.onnx_session_unexpected_count.title",
      "degraded.engine_resources.onnx_session_unexpected_count.body",
      "degraded.engine_resources.exception_cohort_retention_high.title",
      "degraded.engine_resources.exception_cohort_retention_high.body",
      "degraded.engine_resources.heap_snapshot_triggered.title",
      "degraded.engine_resources.heap_snapshot_triggered.body",
      "degraded.engine_resources.actions.viewResources",
      "degraded.engine_resources.actions.viewHeapSnapshot",
      // Mission H4 v0.49.25 — ADR-D8 per-cohort chip mapping.
      // 11 chip labels total (2 chips × 6 reasons, with overlap on
      // openDoctor + viewHeapSnapshot reused across cohorts).
      "degraded.engine_resources.actions.viewThreadSnapshot",
      "degraded.engine_resources.actions.viewLockDicts",
      "degraded.engine_resources.actions.viewOnnx",
      "degraded.engine_resources.actions.viewExceptionCohort",
      "degraded.engine_resources.actions.viewRecent500s",
      "degraded.engine_resources.actions.viewSnapshot",
      "degraded.engine_resources.actions.openDoctor",
      "degraded.engine_resources.actions.adjustLruDocs",
      "degraded.engine_resources.actions.ack",
      // threadSnapshot.* namespace (sibling of heapSnapshot.* —
      // Mission H4 §4.8 ADR-D8 thread-snapshot deep-link page).
      "threadSnapshot.title",
      "threadSnapshot.subtitle",
      "threadSnapshot.loading",
      "threadSnapshot.notFound",
      "threadSnapshot.error",
    ];
    for (const key of requiredKeys) {
      expect(en.has(key), `EN missing ${key}`).toBe(true);
      expect(pt.has(key), `pt-BR missing ${key}`).toBe(true);
      expect(es.has(key), `ES missing ${key}`).toBe(true);
    }
  });

  it("Mission C5 §T3.6 — all degraded.dashboard.* keys present in 3 locales", () => {
    const en = pathSet(enVoice as AnyJson);
    const pt = pathSet(ptVoice as AnyJson);
    const es = pathSet(esVoice as AnyJson);

    const requiredKeys = [
      "degraded.dashboard.bundle_partial.title",
      "degraded.dashboard.bundle_partial.partial.body",
      "degraded.dashboard.bundle_missing.title",
      "degraded.dashboard.bundle_missing.index_html_missing.body",
      "degraded.dashboard.bundle_missing.static_dir_missing.body",
      "degraded.dashboard.bundle_missing.legacy_index_html_no_assets.body",
      "degraded.dashboard.reinstall",
      "degraded.dashboard.runDoctor",
    ];
    for (const key of requiredKeys) {
      expect(en.has(key), `EN missing ${key}`).toBe(true);
      expect(pt.has(key), `pt-BR missing ${key}`).toBe(true);
      expect(es.has(key), `ES missing ${key}`).toBe(true);
    }
  });

  it("Mission C6 §T3.6 — refined degraded.llm.* taxonomy + providers namespace present in 3 locales", () => {
    const en = pathSet(enVoice as AnyJson);
    const pt = pathSet(ptVoice as AnyJson);
    const es = pathSet(esVoice as AnyJson);

    const refinedReasonKeys = [
      // noProviderConfigured (refined from noProvider)
      "degraded.llm.noProviderConfigured.title",
      "degraded.llm.noProviderConfigured.body",
      "degraded.llm.noProviderConfigured.runSetup",
      "degraded.llm.noProviderConfigured.installOllama",
      // ollamaUnreachable
      "degraded.llm.ollamaUnreachable.title",
      "degraded.llm.ollamaUnreachable.body",
      "degraded.llm.ollamaUnreachable.startOllama",
      "degraded.llm.ollamaUnreachable.runDoctor",
      // ollamaNoModels
      "degraded.llm.ollamaNoModels.title",
      "degraded.llm.ollamaNoModels.body",
      "degraded.llm.ollamaNoModels.pullModel",
      "degraded.llm.ollamaNoModels.runDoctor",
      // cloudKeyInvalid
      "degraded.llm.cloudKeyInvalid.title",
      "degraded.llm.cloudKeyInvalid.body",
      "degraded.llm.cloudKeyInvalid.openSettings",
      "degraded.llm.cloudKeyInvalid.testConnection",
      // allUnhealthy
      "degraded.llm.allUnhealthy.title",
      "degraded.llm.allUnhealthy.body",
      "degraded.llm.allUnhealthy.viewHealth",
      "degraded.llm.allUnhealthy.runDoctor",
      // partialHealth
      "degraded.llm.partialHealth.title",
      "degraded.llm.partialHealth.body",
      "degraded.llm.partialHealth.viewHealth",
      // defaultModelUnavailable
      "degraded.llm.defaultModelUnavailable.title",
      "degraded.llm.defaultModelUnavailable.body",
      "degraded.llm.defaultModelUnavailable.openSettings",
    ];
    for (const key of refinedReasonKeys) {
      expect(en.has(key), `EN missing ${key}`).toBe(true);
      expect(pt.has(key), `pt-BR missing ${key}`).toBe(true);
      expect(es.has(key), `ES missing ${key}`).toBe(true);
    }

    // Mission C6 §T1.3 surface 3 — providers namespace MUST cover every
    // LLMProviderKey member with both label + envVar entries so Quality
    // Gate 12 promotes surface 3 from skipped to checked at v0.49.2.
    const providers = [
      "anthropic",
      "openai",
      "google",
      "xai",
      "deepseek",
      "mistral",
      "groq",
      "together",
      "fireworks",
      "ollama",
    ];
    for (const p of providers) {
      for (const field of ["label", "envVar"]) {
        const key = `degraded.llm.providers.${p}.${field}`;
        expect(en.has(key), `EN missing ${key}`).toBe(true);
        expect(pt.has(key), `pt-BR missing ${key}`).toBe(true);
        expect(es.has(key), `ES missing ${key}`).toBe(true);
      }
    }
  });
});
