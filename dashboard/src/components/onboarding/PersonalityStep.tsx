import { useCallback, useState } from "react";
import { useTranslation } from "react-i18next";
import { LoaderIcon } from "lucide-react";
import { api } from "@/lib/api";
import { Button } from "@/components/ui/button";
import { cn } from "@/lib/utils";

/**
 * Personality preset IDs. Names + descriptions come from the i18n
 * `onboarding.personality.presets.<id>` keys (Mission v0.30.5 / D4):
 * the operator reads them in the dashboard locale to decide HOW the
 * companion should communicate, so they ARE translated.
 */
const PRESET_IDS = ["warm", "direct", "playful", "professional"] as const;
type PresetId = (typeof PRESET_IDS)[number];

/**
 * Companion language options — hardcoded NATIVE names per D3.
 * The operator reads these to pick what language the companion will
 * SPEAK in, so the option label uses the LANGUAGE'S own native name
 * regardless of the dashboard locale (matches LanguageSelector D5
 * + Apple/Microsoft conventions for input-language pickers).
 */
const LANGUAGE_OPTIONS: { code: string; nativeLabel: string }[] = [
  { code: "en", nativeLabel: "English" },
  { code: "pt", nativeLabel: "Português" },
  { code: "es", nativeLabel: "Español" },
  { code: "fr", nativeLabel: "Français" },
  { code: "de", nativeLabel: "Deutsch" },
  { code: "it", nativeLabel: "Italiano" },
  { code: "ja", nativeLabel: "日本語" },
  { code: "ko", nativeLabel: "한국어" },
  { code: "zh", nativeLabel: "中文" },
  { code: "ru", nativeLabel: "Русский" },
];

interface PersonalityStepProps {
  mindName: string;
  onConfigured: (newName?: string, lang?: string) => void;
  onSkip: () => void;
}

export function PersonalityStep({ mindName, onConfigured, onSkip }: PersonalityStepProps) {
  const { t } = useTranslation("onboarding");
  const [selected, setSelected] = useState<PresetId>("warm");
  const [companionName, setCompanionName] = useState(mindName);
  const [language, setLanguage] = useState(
    () => navigator.language?.split("-")[0] ?? "en",
  );
  const [userName, setUserName] = useState("");
  const [saving, setSaving] = useState(false);

  const handleContinue = useCallback(async () => {
    setSaving(true);
    try {
      await api.post("/api/onboarding/personality", {
        preset: selected,
        language,
        user_name: userName || undefined,
        companion_name: companionName !== mindName ? companionName : undefined,
      });
      onConfigured(companionName !== mindName ? companionName : undefined, language);
    } catch {
      onConfigured(undefined, language);
    } finally {
      setSaving(false);
    }
  }, [selected, language, userName, companionName, mindName, onConfigured]);

  return (
    <div className="space-y-6">
      <div>
        <h2 className="text-xl font-semibold text-[var(--svx-color-text-primary)]">
          {t("personality.titleWithName", { name: companionName })}
        </h2>
        <p className="mt-1 text-sm text-[var(--svx-color-text-secondary)]">
          {t("personality.subtitle")}
        </p>
      </div>

      {/* Preset cards */}
      <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
        {PRESET_IDS.map((id) => (
          <button
            key={id}
            type="button"
            onClick={() => setSelected(id)}
            className={cn(
              "rounded-[var(--svx-radius-lg)] border p-4 text-left transition-all",
              selected === id
                ? "border-[var(--svx-color-brand-primary)] bg-[var(--svx-color-brand-primary)]/5"
                : "border-[var(--svx-color-border-default)] bg-[var(--svx-color-bg-surface)] hover:border-[var(--svx-color-brand-primary)]/40",
            )}
          >
            <div className="text-sm font-semibold text-[var(--svx-color-text-primary)]">
              {t(`personality.presets.${id}.name`)}
            </div>
            <p className="mt-1 text-xs text-[var(--svx-color-text-secondary)]">
              {t(`personality.presets.${id}.description`)}
            </p>
          </button>
        ))}
      </div>

      {/* Companion name + Language + Your name */}
      <div className="grid grid-cols-1 gap-4 sm:grid-cols-3">
        <div>
          <label className="mb-1 block text-xs font-medium text-[var(--svx-color-text-secondary)]">
            {t("personality.companionNameLabel")}
          </label>
          <input
            type="text"
            value={companionName}
            onChange={(e) => setCompanionName(e.target.value)}
            className="w-full rounded-[var(--svx-radius-md)] border border-[var(--svx-color-border-default)] bg-[var(--svx-color-bg-elevated)] px-3 py-2 text-sm text-[var(--svx-color-text-primary)]"
          />
        </div>
        <div>
          <label className="mb-1 block text-xs font-medium text-[var(--svx-color-text-secondary)]">
            {t("personality.languageLabel")}
          </label>
          <select
            value={language}
            onChange={(e) => setLanguage(e.target.value)}
            className="w-full rounded-[var(--svx-radius-md)] border border-[var(--svx-color-border-default)] bg-[var(--svx-color-bg-elevated)] px-3 py-2 text-sm text-[var(--svx-color-text-primary)]"
          >
            {LANGUAGE_OPTIONS.map((opt) => (
              <option key={opt.code} value={opt.code}>
                {opt.nativeLabel}
              </option>
            ))}
          </select>
        </div>
        <div>
          <label className="mb-1 block text-xs font-medium text-[var(--svx-color-text-secondary)]">
            {t("personality.userNameLabel")}
          </label>
          <input
            type="text"
            value={userName}
            onChange={(e) => setUserName(e.target.value)}
            placeholder={t("personality.userNamePlaceholder")}
            className="w-full rounded-[var(--svx-radius-md)] border border-[var(--svx-color-border-default)] bg-[var(--svx-color-bg-elevated)] px-3 py-2 text-sm text-[var(--svx-color-text-primary)] placeholder:text-[var(--svx-color-text-disabled)]"
          />
        </div>
      </div>

      {/* Actions */}
      <div className="flex items-center justify-between">
        <button
          type="button"
          onClick={onSkip}
          className="text-xs text-[var(--svx-color-text-tertiary)] hover:text-[var(--svx-color-text-secondary)]"
        >
          {t("personality.skipForNow")}
        </button>
        <Button onClick={handleContinue} disabled={saving}>
          {saving && <LoaderIcon className="mr-1.5 size-3.5 animate-spin" />}
          {t("personality.continueButton")}
        </Button>
      </div>
    </div>
  );
}
