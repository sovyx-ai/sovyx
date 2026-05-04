/**
 * LocaleAutoDetectToast — first-visit auto-detect acknowledgement.
 *
 * Mission ``MISSION-claude-autonomous-batch-2026-05-03.md`` §Phase 3
 * (T3.4 / D6). Mounted once at the application root in main.tsx /
 * App.tsx. Renders only when ``i18n-detect.ts`` flagged an auto-detect
 * on the current visit.
 *
 * UX:
 *   - Auto-dismisses after 5 s.
 *   - "Use English" button reverts to ``en`` + persists the override
 *     so subsequent visits are silent.
 *   - Toast does NOT block any interaction (fixed positioning, low z).
 *
 * StrictMode safety: ``consumeAutoDetectedLocale()`` clears the flag
 * on first read so React's double-mount in StrictMode doesn't render
 * the toast twice.
 */
import type { JSX } from "react";
import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import { XIcon } from "lucide-react";

import i18n, { type SupportedLocale } from "@/lib/i18n";
import { consumeAutoDetectedLocale } from "@/lib/i18n-detect";
import { LOCALE_STORAGE_KEY } from "@/components/settings/LanguageSelector";

const NATIVE_NAMES: Record<SupportedLocale, string> = {
  en: "English",
  "pt-BR": "Português (Brasil)",
  es: "Español",
};

const AUTO_DISMISS_MS = 5_000;

export function LocaleAutoDetectToast(): JSX.Element | null {
  const { t } = useTranslation("settings");
  const [detected, setDetected] = useState<SupportedLocale | null>(null);

  useEffect(() => {
    setDetected(consumeAutoDetectedLocale());
  }, []);

  useEffect(() => {
    if (detected === null) return;
    const timer = window.setTimeout(() => setDetected(null), AUTO_DISMISS_MS);
    return () => window.clearTimeout(timer);
  }, [detected]);

  if (detected === null) return null;

  const handleUndo = (): void => {
    void i18n.changeLanguage("en");
    try {
      localStorage.setItem(LOCALE_STORAGE_KEY, "en");
    } catch {
      // Best-effort persistence.
    }
    setDetected(null);
  };

  const handleDismiss = (): void => {
    setDetected(null);
  };

  return (
    <div
      data-testid="locale-auto-detect-toast"
      role="status"
      aria-live="polite"
      className="fixed bottom-4 right-4 z-40 flex max-w-sm items-start gap-3 rounded-[var(--svx-radius-md)] border border-[var(--svx-color-border)] bg-[var(--svx-color-surface-primary)] p-3 shadow-lg"
    >
      <div className="flex-1 text-xs text-[var(--svx-color-text-secondary)]">
        {t("displayLanguage.autoDetected", {
          name: NATIVE_NAMES[detected],
        })}
      </div>
      <button
        type="button"
        onClick={handleUndo}
        className="shrink-0 rounded-[var(--svx-radius-md)] border border-[var(--svx-color-accent)] px-2 py-0.5 text-xs font-medium text-[var(--svx-color-accent)] hover:bg-[var(--svx-color-accent)] hover:text-white"
      >
        {t("displayLanguage.autoDetectedUndo")}
      </button>
      <button
        type="button"
        aria-label="Close"
        onClick={handleDismiss}
        className="shrink-0 rounded-full p-0.5 text-[var(--svx-color-text-tertiary)] hover:bg-[var(--svx-color-surface-hover)]"
      >
        <XIcon className="size-3.5" />
      </button>
    </div>
  );
}
