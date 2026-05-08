/**
 * OnboardingPage -- full-page first-run wizard.
 *
 * Five steps:
 *   1. Choose LLM provider + enter API key (or select Ollama)
 *   2. Personality preset + companion name + language (skippable)
 *   3. Connect channels — Telegram hot-add (skippable)
 *   4. Set up Voice — hot-enable or install instructions (skippable)
 *   5. First conversation (live chat with dynamic mind name)
 *
 * After completion, marks onboarding_complete and redirects to overview.
 */

import { useCallback, useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import { useNavigate } from "react-router";
import { api } from "@/lib/api";
import {
  ProviderStep,
  PersonalityStep,
  ChannelsStep,
  VoiceStep,
  FirstChatStep,
} from "@/components/onboarding";
import type { OnboardingState } from "@/types/api";
import {
  OnboardingCompleteResponseSchema,
  OnboardingStateSchema,
} from "@/types/schemas";
import { useDashboardStore } from "@/stores/dashboard";

const TOTAL_STEPS = 5;

export default function OnboardingPage() {
  const { t } = useTranslation("onboarding");
  const navigate = useNavigate();
  // v0.31.6 T3.2 (M3.c): surface backend's defensive ``voice_configured: false``
  // signal as a post-onboarding banner on the home page. Without this wire-up
  // the daemon's defense (added in v0.31.4 GAP 8) was a tree-falls-in-the-forest
  // signal — the operator landed on the dashboard with onboarding marked
  // complete but voice silently disabled and zero indication something failed.
  const setVoiceWarning = useDashboardStore((s) => s.setVoiceWarning);
  const [step, setStep] = useState(1);
  const [loading, setLoading] = useState(true);
  const [provider, setProvider] = useState("");
  const [model, setModel] = useState("");
  const [mindName, setMindName] = useState("Sovyx");
  // v0.31.6 C1: resolved active mind id, threaded into VoiceStep so
  // <VoiceCalibrationStep mindId={...} /> stops hardcoding "default".
  // Null until /api/onboarding/state resolves; VoiceStep treats null
  // as "not yet known" and falls back to "default" with a warning.
  const [mindId, setMindId] = useState<string | null>(null);
  const [language, setLanguage] = useState(
    () => navigator.language?.split("-")[0] ?? "en",
  );
  const [ollamaAvailable, setOllamaAvailable] = useState(false);
  const [ollamaModels, setOllamaModels] = useState<string[]>([]);

  useEffect(() => {
    api
      .get<OnboardingState>("/api/onboarding/state", {
        schema: OnboardingStateSchema,
      })
      .then((state) => {
        if (state.complete) {
          navigate("/", { replace: true });
          return;
        }
        setMindName(state.mind_name || "Sovyx");
        setMindId(state.mind_id ?? null);
        setOllamaAvailable(state.ollama_available);
        setOllamaModels(state.ollama_models);
        if (state.provider_configured) {
          setProvider(state.default_provider);
          setModel(state.default_model);
          setStep(2);
        }
      })
      .catch(() => {})
      .finally(() => setLoading(false));
  }, [navigate]);

  const handleProviderConfigured = useCallback((p: string, m: string) => {
    setProvider(p);
    setModel(m);
    setStep(2);
  }, []);

  const handlePersonalityDone = useCallback((newName?: string, lang?: string) => {
    if (newName) setMindName(newName);
    if (lang) setLanguage(lang);
    setStep(3);
  }, []);

  const handleChannelsDone = useCallback(() => {
    setStep(4);
  }, []);

  const handleVoiceDone = useCallback(() => {
    setStep(5);
  }, []);

  const handleComplete = useCallback(async () => {
    try {
      const result = await api.post<{
        ok: boolean;
        voice_configured?: boolean;
      }>(
        "/api/onboarding/complete",
        {},
        { schema: OnboardingCompleteResponseSchema },
      );
      // ``voice_configured: false`` is the daemon's defensive signal that
      // mind.yaml requested voice but the runtime didn't bring it up.
      // ``true`` (or missing — pre-v0.31.4 daemons) means "no warning".
      if (result?.voice_configured === false) {
        setVoiceWarning({ kind: "voice_not_configured" });
      }
    } catch {
      // Best effort — navigation still happens; a network failure here
      // shouldn't trap the operator on the onboarding page.
    }
    navigate("/", { replace: true });
  }, [navigate, setVoiceWarning]);

  const handleSkipAll = useCallback(async () => {
    try {
      const result = await api.post<{
        ok: boolean;
        voice_configured?: boolean;
      }>(
        "/api/onboarding/complete",
        {},
        { schema: OnboardingCompleteResponseSchema },
      );
      if (result?.voice_configured === false) {
        setVoiceWarning({ kind: "voice_not_configured" });
      }
    } catch {
      // Best effort
    }
    navigate("/", { replace: true });
  }, [navigate, setVoiceWarning]);

  if (loading) {
    return (
      <div className="flex min-h-screen items-center justify-center bg-[var(--svx-color-bg-base)]">
        <div className="size-6 animate-spin rounded-full border-2 border-[var(--svx-color-brand-primary)] border-t-transparent" />
      </div>
    );
  }

  return (
    <div className="flex min-h-screen items-center justify-center bg-[var(--svx-color-bg-base)] p-4">
      <div className="w-full max-w-2xl space-y-6">
        <div className="text-center">
          <div className="text-sm font-medium text-[var(--svx-color-text-tertiary)]">
            {t("page.step", { step, total: TOTAL_STEPS })}
          </div>
        </div>

        {step === 1 && (
          <ProviderStep
            ollamaAvailable={ollamaAvailable}
            ollamaModels={ollamaModels}
            onConfigured={handleProviderConfigured}
          />
        )}

        {step === 2 && (
          <PersonalityStep
            mindName={mindName}
            onConfigured={handlePersonalityDone}
            onSkip={() => handlePersonalityDone()}
          />
        )}

        {step === 3 && (
          <ChannelsStep
            mindName={mindName}
            onConfigured={handleChannelsDone}
            onSkip={handleChannelsDone}
          />
        )}

        {step === 4 && (
          <VoiceStep
            language={language}
            mindId={mindId}
            onConfigured={handleVoiceDone}
            onSkip={handleVoiceDone}
          />
        )}

        {step === 5 && (
          <FirstChatStep
            mindName={mindName}
            language={language}
            provider={provider}
            model={model}
            onComplete={handleComplete}
          />
        )}

        {step === 1 && (
          <div className="text-center">
            <button
              type="button"
              onClick={handleSkipAll}
              className="text-xs text-[var(--svx-color-text-tertiary)] hover:text-[var(--svx-color-text-secondary)]"
            >
              {t("page.skipAll")}
            </button>
          </div>
        )}

        <div className="flex justify-center gap-2">
          {Array.from({ length: TOTAL_STEPS }, (_, i) => i + 1).map((s) => (
            <div
              key={s}
              className={`size-2 rounded-full transition-colors ${
                s === step
                  ? "bg-[var(--svx-color-brand-primary)]"
                  : s < step
                    ? "bg-[var(--svx-color-brand-primary)]/40"
                    : "bg-[var(--svx-color-border-default)]"
              }`}
            />
          ))}
        </div>
      </div>
    </div>
  );
}
