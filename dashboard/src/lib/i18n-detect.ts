/**
 * First-visit locale detection — Mission v0.30.3 §T3.4 (D6).
 *
 * Runs ONCE at app boot before React renders the tree:
 *   1. If ``localStorage["sovyx_locale"]`` already holds a supported
 *      locale → applies it + returns (operator made an explicit choice
 *      previously; never re-prompt).
 *   2. Else read ``navigator.language``. Map the language tag to a
 *      supported locale via the matching table.
 *   3. If the match resolves to anything other than the i18next default
 *      (en), apply it + flag the toast trigger so
 *      ``<LocaleAutoDetectToast />`` renders the undo affordance for
 *      ~5 s on first paint.
 *
 * D6 rationale (auto-detect + toast vs opt-in prompt):
 *   * Modal prompts on first visit annoy operators who already know
 *     their language.
 *   * Silent switch surprises operators who DON'T want their browser
 *     language used.
 *   * Toast + undo threads the needle: acknowledges the change, gives
 *     instant escape, never blocks.
 *
 * Persistence:
 *   * After auto-detect applies, the chosen locale is persisted to
 *     localStorage so subsequent visits are silent.
 *   * The "I just auto-detected" flag lives in module-scope memory
 *     (does NOT persist across reloads — second visit gets no toast).
 */
import i18n, { SUPPORTED_LOCALES, type SupportedLocale } from "./i18n";
import { LOCALE_STORAGE_KEY } from "@/components/settings/LanguageSelector";

let _autoDetectedLocale: SupportedLocale | null = null;

/**
 * Map ``navigator.language`` to one of the supported locales.
 *
 * Browser language tags follow BCP 47: ``pt-BR``, ``pt-PT``,
 * ``es-MX``, ``es``, ``en-US``, ``en``. We resolve via simple prefix
 * matching:
 *   - exact match (``pt-BR`` → ``pt-BR``) — wins
 *   - language prefix match (``pt-PT`` → ``pt-BR``) — fallback for
 *     close dialects; better than English for a Portuguese speaker
 *   - language prefix match (``es-MX`` → ``es``)
 *   - everything else → null (caller falls back to i18n default ``en``)
 */
export function resolveBrowserLocale(
  navigatorLanguage: string | undefined,
): SupportedLocale | null {
  if (!navigatorLanguage) return null;
  const lang = navigatorLanguage.trim();
  if (!lang) return null;

  // Exact-tag match (case-insensitive, but locales are case-sensitive
  // in the constant — normalise to the exact constant value).
  for (const supported of SUPPORTED_LOCALES) {
    if (lang.toLowerCase() === supported.toLowerCase()) return supported;
  }

  // Language-prefix match (e.g. "pt-PT" → "pt-BR" since pt-BR is the
  // sole Portuguese variant we ship).
  const prefix = lang.split("-")[0]?.toLowerCase();
  if (!prefix) return null;
  if (prefix === "pt") return "pt-BR";
  if (prefix === "es") return "es";
  if (prefix === "en") return "en";

  return null;
}

/**
 * Run the detection at boot. Idempotent — safe to call multiple times
 * (subsequent calls are no-ops if a choice is already persisted).
 */
export function applyLocaleDetection(): void {
  // 1. Honour explicit prior choice.
  let stored: string | null = null;
  try {
    stored = localStorage.getItem(LOCALE_STORAGE_KEY);
  } catch {
    // localStorage unavailable — proceed with detection.
  }
  if (stored !== null) {
    const choice = stored as SupportedLocale;
    if (SUPPORTED_LOCALES.includes(choice) && choice !== i18n.language) {
      void i18n.changeLanguage(choice);
    }
    return;
  }

  // 2. First visit — sniff navigator.language.
  const detected = resolveBrowserLocale(
    typeof navigator !== "undefined" ? navigator.language : undefined,
  );
  if (detected === null || detected === "en") {
    // Default already English; no toast.
    return;
  }

  // 3. Apply + persist + flag the toast.
  void i18n.changeLanguage(detected);
  try {
    localStorage.setItem(LOCALE_STORAGE_KEY, detected);
  } catch {
    // Best-effort; the i18n.changeLanguage above still works for the
    // current session even when localStorage is unavailable.
  }
  _autoDetectedLocale = detected;
}

/**
 * Read-once accessor used by ``<LocaleAutoDetectToast />`` to decide
 * whether to render. Reading clears the flag so subsequent reads
 * (e.g. component re-mount under StrictMode) get null and the toast
 * doesn't reappear.
 */
export function consumeAutoDetectedLocale(): SupportedLocale | null {
  const value = _autoDetectedLocale;
  _autoDetectedLocale = null;
  return value;
}

/** Test seam — reset module state for isolation. */
export function _resetForTests(): void {
  _autoDetectedLocale = null;
}
