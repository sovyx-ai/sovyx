/**
 * Settings page — Engine settings + Mind config (personality, OCEAN, safety).
 *
 * Two API connections:
 * - GET/PUT /api/settings → log_level (mutable) + engine info (read-only)
 * - GET/PUT /api/config → personality, OCEAN, safety (mutable) + brain, LLM (read-only)
 *
 * Ref: V05-P04
 */

import { useEffect, useState, useCallback } from "react";
import { useTranslation } from "react-i18next";
import {
  SaveIcon,
  Loader2Icon,
  UserIcon,
  ShieldIcon,
  BrainIcon,
  SparklesIcon,
  AlertTriangleIcon,
} from "lucide-react";
import { Input } from "@/components/ui/input";
import { Button } from "@/components/ui/button";
import { Label } from "@/components/ui/label";
import { useDashboardStore } from "@/stores/dashboard";
import { api, isAbortError } from "@/lib/api";
import { toast } from "sonner";
import { ExportImportSection } from "@/components/settings/export-import";
import type {
  Settings,
  MindConfigResponse,
  MindConfigUpdate,
  MindConfigUpdateResponse,
  ToneType,
  ContentFilter,
} from "@/types/api";
import { cn } from "@/lib/utils";
import { ProviderConfig } from "@/components/settings/provider-config";

type LogLevel = Settings["log_level"];
const LOG_LEVELS: LogLevel[] = ["DEBUG", "INFO", "WARNING", "ERROR"];
const TONES: ToneType[] = ["warm", "neutral", "direct", "playful"];
const CONTENT_FILTERS: ContentFilter[] = ["none", "standard", "strict"];

// ── Tone presets — clicking a tone adjusts sliders for immediate feedback ──

type TraitPreset = {
  formality: number;
  humor: number;
  assertiveness: number;
  curiosity: number;
  empathy: number;
  verbosity: number;
};

const TONE_PRESETS: Record<ToneType, TraitPreset> = {
  warm: { formality: 0.3, humor: 0.5, assertiveness: 0.4, curiosity: 0.7, empathy: 0.9, verbosity: 0.6 },
  neutral: { formality: 0.5, humor: 0.4, assertiveness: 0.6, curiosity: 0.7, empathy: 0.8, verbosity: 0.5 },
  direct: { formality: 0.6, humor: 0.2, assertiveness: 0.85, curiosity: 0.5, empathy: 0.4, verbosity: 0.25 },
  playful: { formality: 0.2, humor: 0.85, assertiveness: 0.5, curiosity: 0.9, empathy: 0.7, verbosity: 0.65 },
};

// ── Personality trait metadata ──

/** Personality trait keys — labels resolved via i18n at render time. */
const PERSONALITY_TRAIT_KEYS = [
  "formality", "humor", "assertiveness", "curiosity", "empathy", "verbosity",
] as const;

/** OCEAN trait keys — labels resolved via i18n at render time. */
const OCEAN_TRAIT_KEYS = [
  "openness", "conscientiousness", "extraversion", "agreeableness", "neuroticism",
] as const;

export default function SettingsPage() {
  const { t } = useTranslation(["settings", "common"]);
  const settings = useDashboardStore((s) => s.settings);
  const setSettings = useDashboardStore((s) => s.setSettings);

  // Engine settings state
  const [selectedLevel, setSelectedLevel] = useState<LogLevel>("INFO");
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);

  // Mind config state
  const [mindConfig, setMindConfig] = useState<MindConfigResponse | null>(null);
  const [editedConfig, setEditedConfig] = useState<MindConfigUpdate>({});
  const [configLoading, setConfigLoading] = useState(true);
  const [configSaving, setConfigSaving] = useState(false);

  // Dirty checks
  const settingsDirty = settings != null && selectedLevel !== settings.log_level;
  const configDirty = Object.keys(editedConfig).length > 0;

  // ── Fetch engine settings ──
  const fetchSettings = useCallback(async (signal?: AbortSignal) => {
    try {
      setLoading(true);
      const data = await api.get<Settings>("/api/settings", { signal });
      setSettings(data);
      setSelectedLevel(data.log_level);
    } catch (err) {
      if (isAbortError(err)) return;
      toast.error(t("general.loadFailed"));
    } finally {
      setLoading(false);
    }
  }, [setSettings, t]);

  // ── Fetch mind config ──
  const fetchConfig = useCallback(async (signal?: AbortSignal) => {
    try {
      setConfigLoading(true);
      const data = await api.get<MindConfigResponse>("/api/config", { signal });
      setMindConfig(data);
      setEditedConfig({});
    } catch (err) {
      if (isAbortError(err)) return;
      // 503 = no mind loaded, expected in some setups
      if (err instanceof Error && "status" in err && (err as { status: number }).status === 503) {
        setMindConfig(null);
      }
    } finally {
      setConfigLoading(false);
    }
  }, []);

  useEffect(() => {
    const controller = new AbortController();
    void fetchSettings(controller.signal);
    void fetchConfig(controller.signal);
    return () => controller.abort();
  }, [fetchSettings, fetchConfig]);

  // ── Save engine settings ──
  const handleSaveSettings = async () => {
    if (!settingsDirty) return;
    try {
      setSaving(true);
      const res = await api.put<{ ok: boolean; changes?: Record<string, string>; error?: string }>(
        "/api/settings",
        { log_level: selectedLevel },
      );
      if (res.ok !== false) {
        if (settings) setSettings({ ...settings, log_level: selectedLevel });
        toast.success(t("general.saved"));
      } else {
        toast.error(res.error ?? t("general.saveFailed"));
      }
    } catch {
      toast.error(t("general.saveFailed"));
    } finally {
      setSaving(false);
    }
  };

  // ── Save mind config ──
  const handleSaveConfig = async () => {
    if (!configDirty) return;
    try {
      setConfigSaving(true);
      const res = await api.put<MindConfigUpdateResponse>("/api/config", editedConfig);
      if (res.ok) {
        // Re-fetch to get updated state
        await fetchConfig();
        toast.success(t("general.saved"));
      } else {
        toast.error(res.error ?? t("general.saveFailed"));
      }
    } catch {
      toast.error(t("general.saveFailed"));
    } finally {
      setConfigSaving(false);
    }
  };

  // ── Config update helpers ──
  const updatePersonality = (field: string, value: number | string) => {
    setEditedConfig((prev) => ({
      ...prev,
      personality: {
        ...mindConfig?.personality,
        ...prev.personality,
        [field]: value,
      },
    }));
  };

  /** Apply tone preset — sets tone + adjusts all trait sliders. */
  const applyTonePreset = (tone: ToneType) => {
    const preset = TONE_PRESETS[tone];
    setEditedConfig((prev) => ({
      ...prev,
      personality: {
        ...mindConfig?.personality,
        ...prev.personality,
        tone,
        ...preset,
      },
    }));
  };

  const updateOcean = (field: string, value: number) => {
    setEditedConfig((prev) => ({
      ...prev,
      ocean: {
        ...mindConfig?.ocean,
        ...prev.ocean,
        [field]: value,
      },
    }));
  };

  const updateSafety = (field: string, value: boolean | string) => {
    setEditedConfig((prev) => ({
      ...prev,
      safety: {
        ...mindConfig?.safety,
        ...prev.safety,
        [field]: value,
      },
    }));
  };

  // Get current value (edited or original)
  const getPersonalityValue = (field: string): number | string => {
    const edited = editedConfig.personality?.[field as keyof typeof editedConfig.personality];
    if (edited !== undefined) return edited;
    return mindConfig?.personality?.[field as keyof typeof mindConfig.personality] ?? 0;
  };

  /** Derive the effective tone: only highlight a preset button when current sliders match it. */
  const getEffectiveTone = (): ToneType | null => {
    // If user explicitly picked a tone in this edit session, check it still matches
    const storedTone = (editedConfig.personality?.tone ?? mindConfig?.personality?.tone ?? "warm") as ToneType;
    const preset = TONE_PRESETS[storedTone];
    const matches = PERSONALITY_TRAIT_KEYS.every((key) => {
      const current = getPersonalityValue(key) as number;
      return Math.abs(current - preset[key]) < 0.01;
    });
    return matches ? storedTone : null;
  };

  const getOceanValue = (field: string): number => {
    const edited = editedConfig.ocean?.[field as keyof typeof editedConfig.ocean];
    if (edited !== undefined) return edited as number;
    return mindConfig?.ocean?.[field as keyof typeof mindConfig.ocean] ?? 0;
  };

  if (loading || !settings) {
    return (
      <div className="flex h-64 items-center justify-center">
        <div className="size-6 animate-spin rounded-full border-2 border-[var(--svx-color-brand-primary)] border-t-transparent" />
      </div>
    );
  }

  return (
    <div className="space-y-6">
      {/* Header */}
      <div className="flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
        <div>
          <h1 className="text-2xl font-bold text-[var(--svx-color-text-primary)]">
            {t("title")}
          </h1>
          <p className="text-sm text-[var(--svx-color-text-secondary)]">
            {t("general.engineConfigDesc")}
          </p>
        </div>
        <div className="flex gap-2">
          {configDirty && (
            <Button
              onClick={() => void handleSaveConfig()}
              disabled={configSaving}
              className="gap-2"
              variant="default"
            >
              {configSaving ? (
                <Loader2Icon className="size-4 animate-spin" />
              ) : (
                <SparklesIcon className="size-4" />
              )}
              {t("personality.savePersonality")}
            </Button>
          )}
          <Button
            onClick={() => void handleSaveSettings()}
            disabled={!settingsDirty || saving}
            className="gap-2"
          >
            {saving ? (
              <Loader2Icon className="size-4 animate-spin" />
            ) : (
              <SaveIcon className="size-4" />
            )}
            {t("common:actions.save")}
          </Button>
        </div>
      </div>

      {/* ── Mind Identity ── */}
      {mindConfig && (
        <section className="rounded-[var(--svx-radius-lg)] border border-[var(--svx-color-brand-primary)]/30 bg-[var(--svx-color-bg-surface)] p-4">
          <div className="flex items-center gap-2">
            <UserIcon className="size-4 text-[var(--svx-color-brand-primary)]" />
            <h2 className="text-sm font-medium text-[var(--svx-color-text-primary)]">
              {t("mind.identity")}
            </h2>
          </div>
          <div className="mt-4 grid gap-4 sm:grid-cols-3">
            <ReadOnlyField label={t("mind.name")} value={mindConfig.name} />
            <ReadOnlyField label={t("mind.language")} value={mindConfig.language} />
            <ReadOnlyField label={t("mind.timezone")} value={mindConfig.timezone} />
          </div>
        </section>
      )}

      {/* ── Personality (MUTABLE) ── */}
      {mindConfig && (
        <section className="rounded-[var(--svx-radius-lg)] border border-[var(--svx-color-border-default)] bg-[var(--svx-color-bg-surface)] p-4">
          <div className="flex items-center gap-2">
            <SparklesIcon className="size-4 text-[var(--svx-color-brand-primary)]" />
            <h2 className="text-sm font-medium text-[var(--svx-color-text-primary)]">
              {t("personality.title")}
            </h2>
          </div>
          <p className="mt-1 text-xs text-[var(--svx-color-text-tertiary)]">
            {t("personality.description")}
          </p>

          {/* Tone selector */}
          <div className="mt-4 space-y-2">
            <Label className="text-xs">{t("personality.tone")}</Label>
            <div className="flex rounded-[var(--svx-radius-md)] border border-[var(--svx-color-border-strong)]">
              {TONES.map((tone) => (
                <button
                  key={tone}
                  type="button"
                  onClick={() => applyTonePreset(tone)}
                  className={cn(
                    "flex-1 px-3 py-1.5 text-xs font-medium capitalize transition-colors",
                    getEffectiveTone() === tone
                      ? "bg-[var(--svx-color-brand-primary)] text-[var(--svx-color-text-inverse)]"
                      : "hover:bg-[var(--svx-color-bg-hover)] text-[var(--svx-color-text-secondary)]",
                  )}
                >
                  {t(`personality.tones.${tone}`)}
                </button>
              ))}
            </div>
          </div>

          {/* Float trait sliders */}
          <div className="mt-4 space-y-4">
            {PERSONALITY_TRAIT_KEYS.map((key) => (
              <TraitSlider
                key={key}
                label={t(`personality.traits.${key}`)}
                lowLabel={t(`personality.traitLow.${key}`)}
                highLabel={t(`personality.traitHigh.${key}`)}
                value={getPersonalityValue(key) as number}
                onChange={(v) => updatePersonality(key, v)}
              />
            ))}
          </div>
        </section>
      )}

      {/* ── OCEAN (MUTABLE) ── */}
      {mindConfig && (
        <section className="rounded-[var(--svx-radius-lg)] border border-[var(--svx-color-border-default)] bg-[var(--svx-color-bg-surface)] p-4">
          <div className="flex items-center gap-2">
            <BrainIcon className="size-4 text-[var(--svx-color-brand-primary)]" />
            <h2 className="text-sm font-medium text-[var(--svx-color-text-primary)]">
              {t("ocean.title")}
            </h2>
          </div>
          <p className="mt-1 text-xs text-[var(--svx-color-text-tertiary)]">
            {t("ocean.description")}
          </p>

          <div className="mt-4 space-y-4">
            {OCEAN_TRAIT_KEYS.map((key) => (
              <TraitSlider
                key={key}
                label={t(`ocean.traits.${key}`)}
                lowLabel={t(`ocean.traitLow.${key}`)}
                highLabel={t(`ocean.traitHigh.${key}`)}
                value={getOceanValue(key)}
                onChange={(v) => updateOcean(key, v)}
              />
            ))}
          </div>
        </section>
      )}

      {/* ── Safety (MUTABLE) ── */}
      {mindConfig && (
        <section className="rounded-[var(--svx-radius-lg)] border border-[var(--svx-color-border-default)] bg-[var(--svx-color-bg-surface)] p-4">
          <div className="flex items-center gap-2">
            <ShieldIcon className="size-4 text-[var(--svx-color-brand-primary)]" />
            <h2 className="text-sm font-medium text-[var(--svx-color-text-primary)]">
              {t("safety.title")}
            </h2>
          </div>
          <p className="mt-1 text-xs text-[var(--svx-color-text-tertiary)]">
            {t("safety.description")}
          </p>

          <div className="mt-4 space-y-4">
            {/* Content Filter */}
            <div className="space-y-2">
              <Label className="text-xs">{t("safety.contentFilter")}</Label>
              <div className="flex rounded-[var(--svx-radius-md)] border border-[var(--svx-color-border-strong)]">
                {CONTENT_FILTERS.map((filter) => {
                  const currentFilter = editedConfig.safety?.content_filter ?? mindConfig.safety.content_filter;
                  return (
                    <button
                      key={filter}
                      type="button"
                      onClick={() => updateSafety("content_filter", filter)}
                      className={cn(
                        "flex-1 px-3 py-1.5 text-xs font-medium capitalize transition-colors",
                        currentFilter === filter
                          ? "bg-[var(--svx-color-brand-primary)] text-[var(--svx-color-text-inverse)]"
                          : "hover:bg-[var(--svx-color-bg-hover)] text-[var(--svx-color-text-secondary)]",
                      )}
                    >
                      {t(`safety.filters.${filter}`)}
                    </button>
                  );
                })}
              </div>
            </div>

            {/* Toggles */}
            <ToggleField
              label={t("safety.childSafeMode")}
              description={t("safety.childSafeModeDesc")}
              checked={editedConfig.safety?.child_safe_mode ?? mindConfig.safety.child_safe_mode}
              onChange={(v) => updateSafety("child_safe_mode", v)}
            />
            <ToggleField
              label={t("safety.financialConfirmation")}
              description={t("safety.financialConfirmationDesc")}
              checked={editedConfig.safety?.financial_confirmation ?? mindConfig.safety.financial_confirmation}
              onChange={(v) => updateSafety("financial_confirmation", v)}
            />
          </div>
        </section>
      )}

      {/* ── General: Log Level (MUTABLE) ── */}
      <section className="rounded-[var(--svx-radius-lg)] border border-[var(--svx-color-border-default)] bg-[var(--svx-color-bg-surface)] p-4">
        <h2 className="text-sm font-medium text-[var(--svx-color-text-primary)]">
          {t("tabs.general")}
        </h2>
        <p className="mt-1 text-xs text-[var(--svx-color-text-tertiary)]">
          {t("general.runtimeSettings")}
        </p>

        <div className="mt-4 space-y-4">
          <div className="space-y-2">
            <Label className="text-xs">{t("general.logLevel")}</Label>
            <div className="flex rounded-[var(--svx-radius-md)] border border-[var(--svx-color-border-strong)]">
              {LOG_LEVELS.map((level) => (
                <button
                  key={level}
                  type="button"
                  onClick={() => setSelectedLevel(level)}
                  className={cn(
                    "flex-1 px-3 py-1.5 text-xs font-medium transition-colors",
                    level === selectedLevel
                      ? "bg-[var(--svx-color-brand-primary)] text-[var(--svx-color-text-inverse)]"
                      : "hover:bg-[var(--svx-color-bg-hover)] text-[var(--svx-color-text-secondary)]",
                  )}
                >
                  {level}
                </button>
              ))}
            </div>
          </div>
        </div>
      </section>

      {/* ── Engine Info (READ-ONLY) ── */}
      <section className="rounded-[var(--svx-radius-lg)] border border-[var(--svx-color-border-default)] bg-[var(--svx-color-bg-surface)] p-4">
        <h2 className="text-sm font-medium text-[var(--svx-color-text-primary)]">
          {t("general.engineConfig")}
        </h2>
        <p className="mt-1 text-xs text-[var(--svx-color-text-tertiary)]">
          {t("general.engineConfigDesc")}
        </p>

        <div className="mt-4 grid gap-4 sm:grid-cols-2">
          <ReadOnlyField label={t("general.dataDir")} value={settings.data_dir} mono />
          <ReadOnlyField label={t("general.logFormat")} value={settings.log_format} />
          <ReadOnlyField label={t("general.logFile")} value={settings.log_file ?? "stdout"} mono />
          <ReadOnlyField label={t("general.telemetryLabel")} value={settings.telemetry_enabled ? t("general.enabled") : t("general.disabled")} />
          <ReadOnlyField label={t("general.apiEndpoint")} value={`${settings.api_host}:${settings.api_port}`} mono />
          <ReadOnlyField label={t("general.relayLabel")} value={settings.relay_enabled ? t("general.enabled") : t("general.disabled")} />
        </div>
      </section>

      {/* ── LLM Provider Config (interactive) ── */}
      <ProviderConfig />

      {/* ── LLM Parameters + Brain Info (READ-ONLY, from /api/config) ── */}
      {mindConfig && (
        <section className="rounded-[var(--svx-radius-lg)] border border-[var(--svx-color-border-default)] bg-[var(--svx-color-bg-surface)] p-4">
          <h2 className="text-sm font-medium text-[var(--svx-color-text-primary)]">
            {t("llmBrain.title")}
          </h2>
          <p className="mt-1 text-xs text-[var(--svx-color-text-tertiary)]">
            {t("llmBrain.description")}
          </p>

          <div className="mt-4 grid gap-4 sm:grid-cols-2">
            <ReadOnlyField label={t("llmBrain.temperature")} value={String(mindConfig.llm.temperature)} />
            <ReadOnlyField label={t("llmBrain.dailyBudget")} value={`$${mindConfig.llm.budget_daily_usd.toFixed(2)}`} />
            <ReadOnlyField label={t("llmBrain.perConvBudget")} value={`$${mindConfig.llm.budget_per_conversation_usd.toFixed(2)}`} />
            <ReadOnlyField label={t("llmBrain.brainMaxConcepts")} value={String(mindConfig.brain.max_concepts)} />
            <ReadOnlyField label={t("llmBrain.consolidation")} value={t("llmBrain.consolidationValue", { hours: mindConfig.brain.consolidation_interval_hours })} />
          </div>
        </section>
      )}

      {/* ── Config loading state ── */}
      {configLoading && !mindConfig && (
        <section className="rounded-[var(--svx-radius-lg)] border border-dashed border-[var(--svx-color-border-default)] bg-[var(--svx-color-bg-surface)] p-6">
          <div className="flex items-center justify-center gap-2 text-[var(--svx-color-text-disabled)]">
            <Loader2Icon className="size-4 animate-spin" />
            <span className="text-sm">{t("mind.loadingConfig")}</span>
          </div>
        </section>
      )}

      {/* ── No mind config warning ── */}
      {!configLoading && !mindConfig && (
        <section className="rounded-[var(--svx-radius-lg)] border border-dashed border-[var(--svx-color-status-warning)]/30 bg-[var(--svx-color-bg-surface)] p-4">
          <div className="flex items-center gap-2 text-[var(--svx-color-status-warning)]">
            <AlertTriangleIcon className="size-4" />
            <span className="text-sm font-medium">{t("mind.noMindLoaded")}</span>
          </div>
          <p className="mt-2 text-xs text-[var(--svx-color-text-tertiary)]">
            {t("mind.noMindDescription")}
          </p>
        </section>
      )}

      {/* ── Export / Import ── */}
      <ExportImportSection />
    </div>
  );
}

// ── Trait slider component ──

function TraitSlider({
  label,
  lowLabel,
  highLabel,
  value,
  onChange,
}: {
  label: string;
  lowLabel: string;
  highLabel: string;
  value: number;
  onChange: (v: number) => void;
}) {
  const pct = Math.round(value * 100);

  return (
    <div className="space-y-1.5">
      <div className="flex items-center justify-between">
        <Label className="text-xs">{label}</Label>
        <span className="text-xs font-mono text-[var(--svx-color-text-tertiary)]">{pct}%</span>
      </div>
      <input
        type="range"
        aria-label={label}
        min={0}
        max={100}
        value={pct}
        onChange={(e) => onChange(Number(e.target.value) / 100)}
        className="w-full accent-[var(--svx-color-brand-primary)]"
      />
      <div className="flex justify-between">
        <span className="text-[10px] text-[var(--svx-color-text-disabled)]">{lowLabel}</span>
        <span className="text-[10px] text-[var(--svx-color-text-disabled)]">{highLabel}</span>
      </div>
    </div>
  );
}

// ── Toggle field component ──

function ToggleField({
  label,
  description,
  checked,
  onChange,
}: {
  label: string;
  description: string;
  checked: boolean;
  onChange: (v: boolean) => void;
}) {
  return (
    <div className="flex items-center justify-between">
      <div>
        <Label className="text-xs">{label}</Label>
        <p className="text-[10px] text-[var(--svx-color-text-disabled)]">{description}</p>
      </div>
      <button
        type="button"
        role="switch"
        aria-checked={checked}
        onClick={() => onChange(!checked)}
        className={cn(
          "relative inline-flex h-5 w-9 shrink-0 cursor-pointer rounded-full border-2 border-transparent transition-colors",
          checked ? "bg-[var(--svx-color-brand-primary)]" : "bg-[var(--svx-color-border-strong)]",
        )}
      >
        <span
          className={cn(
            "pointer-events-none inline-block size-4 transform rounded-full bg-white shadow-sm transition-transform",
            checked ? "translate-x-4" : "translate-x-0",
          )}
        />
      </button>
    </div>
  );
}

// ── Read-only field display ──

function ReadOnlyField({ label, value, mono }: { label: string; value: string; mono?: boolean }) {
  return (
    <div className="space-y-1">
      <Label className="text-xs text-[var(--svx-color-text-tertiary)]">{label}</Label>
      <Input
        value={value}
        disabled
        className={cn(
          "h-8 text-xs opacity-60",
          mono && "font-code",
        )}
      />
    </div>
  );
}


