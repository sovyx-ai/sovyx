/**
 * LanguageSelector — dropdown that switches the dashboard locale.
 *
 * Mission ``MISSION-claude-autonomous-batch-2026-05-03.md`` §Phase 3
 * (T3.3 / D5). Mounted in pages/settings.tsx under "Display & Language".
 *
 * Persistence contract: writes to ``localStorage["sovyx_locale"]``
 * (NOT auth-token-grade — locale is not a credential, so the
 * sessionStorage rule of CLAUDE.md anti-pattern #19 doesn't apply).
 * The detection layer in i18n-detect.ts reads this key on every
 * boot — operator's choice survives across tabs + sessions.
 *
 * Lookup is bilingual-friendly: option labels are NOT translated
 * (operators see "English" / "Português (Brasil)" / "Español"
 * regardless of current locale, which prevents the "I broke my
 * dashboard, can't read anything to fix it" trap).
 */
import type { JSX } from "react";
import { useTranslation } from "react-i18next";

import { SUPPORTED_LOCALES, type SupportedLocale } from "@/lib/i18n";

const LOCALE_STORAGE_KEY = "sovyx_locale";

/**
 * Native option labels — kept untranslated by design (D5 rationale).
 * Adding a locale: extend SUPPORTED_LOCALES + add the native label here.
 */
const LOCALE_LABELS: Record<SupportedLocale, string> = {
  en: "English",
  "pt-BR": "Português (Brasil)",
  es: "Español",
};

export function LanguageSelector(): JSX.Element {
  const { t, i18n } = useTranslation("settings");

  const handleChange = (e: React.ChangeEvent<HTMLSelectElement>): void => {
    const next = e.target.value as SupportedLocale;
    if (!SUPPORTED_LOCALES.includes(next)) return;
    void i18n.changeLanguage(next);
    try {
      localStorage.setItem(LOCALE_STORAGE_KEY, next);
    } catch {
      // localStorage may be disabled in locked-down browsers; the
      // i18n.changeLanguage() above still works for the current session.
    }
  };

  return (
    <div
      data-testid="language-selector"
      className="rounded-[var(--svx-radius-md)] border border-[var(--svx-color-border-default)] bg-[var(--svx-color-surface-secondary)] p-3"
    >
      <div className="mb-1 text-sm font-semibold text-[var(--svx-color-text-primary)]">
        {t("displayLanguage.title")}
      </div>
      <div className="mb-2 text-xs text-[var(--svx-color-text-tertiary)]">
        {t("displayLanguage.description")}
      </div>
      <label className="block text-xs">
        <span className="text-[var(--svx-color-text-secondary)]">
          {t("displayLanguage.selectorLabel")}
        </span>
        <select
          aria-label={t("displayLanguage.selectorLabel")}
          value={i18n.language}
          onChange={handleChange}
          className="mt-1 block w-full rounded-[var(--svx-radius-md)] border border-[var(--svx-color-border)] bg-[var(--svx-color-surface-primary)] px-2 py-1 text-xs"
        >
          {SUPPORTED_LOCALES.map((locale) => (
            <option key={locale} value={locale}>
              {LOCALE_LABELS[locale]}
            </option>
          ))}
        </select>
      </label>
    </div>
  );
}

export { LOCALE_STORAGE_KEY };
