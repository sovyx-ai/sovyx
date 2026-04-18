/**
 * use-voice-catalog — Kokoro voice catalog for the setup wizard.
 *
 * The catalog is static across a release (54 voices × 9 languages) so
 * we fetch once on mount, cache in hook state, and expose cheap
 * selectors the picker UI uses:
 *
 *   - ``voicesForLanguage(lang)`` → voices matching a language tag,
 *     with internal language aliasing (``pt`` → ``pt-br`` etc.) so the
 *     UI can hand us whatever shape ``navigator.language`` produced.
 *   - ``recommendedFor(lang)`` → the hand-picked default voice id per
 *     language, matching ``voice_catalog._RECOMMENDED`` on the server.
 *   - ``normaliseLanguage(lang)`` → returns the canonical catalog code
 *     or ``null`` when the language isn't supported.
 *
 * A single fetch on mount keeps the wizard snappy — no spinner per
 * language change. Failures fall back to ``null`` so the picker
 * renders a disabled state rather than blocking the whole step.
 */

import { useCallback, useEffect, useMemo, useState } from "react";
import { api } from "@/lib/api";
import type { VoiceCatalogEntry, VoiceCatalogResponse } from "@/types/api";
import { VoiceCatalogResponseSchema } from "@/types/schemas";

export interface UseVoiceCatalogResult {
  catalog: VoiceCatalogResponse | null;
  loading: boolean;
  error: string | null;
  voicesForLanguage: (language: string) => VoiceCatalogEntry[];
  recommendedFor: (language: string) => string | null;
  normaliseLanguage: (language: string) => string | null;
  refresh: () => Promise<void>;
}

// Matches ``voice_catalog._LANGUAGE_ALIASES`` on the server. The
// duplication is deliberate — the client still needs to canonicalise
// an incoming ``navigator.language`` before it can index into
// ``by_language`` (the server's ``supported_languages`` set only
// contains canonical codes, no aliases).
const LANGUAGE_ALIASES: Record<string, string> = {
  en: "en-us",
  "en-us": "en-us",
  en_us: "en-us",
  "en-gb": "en-gb",
  en_gb: "en-gb",
  pt: "pt-br",
  "pt-br": "pt-br",
  pt_br: "pt-br",
  es: "es",
  "es-es": "es",
  "es-mx": "es",
  es_mx: "es",
  fr: "fr",
  "fr-fr": "fr",
  hi: "hi",
  "hi-in": "hi",
  it: "it",
  "it-it": "it",
  ja: "ja",
  "ja-jp": "ja",
  zh: "zh",
  "zh-cn": "zh",
  "zh-tw": "zh",
};

function canonicalise(raw: string): string {
  const low = raw.trim().toLowerCase().replace(/_/g, "-");
  return LANGUAGE_ALIASES[low] ?? low;
}

export function useVoiceCatalog(): UseVoiceCatalogResult {
  const [catalog, setCatalog] = useState<VoiceCatalogResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const data = await api.get<VoiceCatalogResponse>("/api/voice/voices", {
        schema: VoiceCatalogResponseSchema,
      });
      setCatalog(data);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Catalog fetch failed");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  const supported = useMemo(
    () => new Set(catalog?.supported_languages ?? []),
    [catalog],
  );

  const normaliseLanguage = useCallback(
    (language: string): string | null => {
      const canon = canonicalise(language);
      return supported.has(canon) ? canon : null;
    },
    [supported],
  );

  const voicesForLanguage = useCallback(
    (language: string): VoiceCatalogEntry[] => {
      if (!catalog) return [];
      const canon = normaliseLanguage(language);
      if (canon === null) return [];
      return catalog.by_language[canon] ?? [];
    },
    [catalog, normaliseLanguage],
  );

  const recommendedFor = useCallback(
    (language: string): string | null => {
      if (!catalog) return null;
      const canon = normaliseLanguage(language);
      if (canon === null) return null;
      return catalog.recommended_per_language[canon] ?? null;
    },
    [catalog, normaliseLanguage],
  );

  return {
    catalog,
    loading,
    error,
    voicesForLanguage,
    recommendedFor,
    normaliseLanguage,
    refresh,
  };
}
