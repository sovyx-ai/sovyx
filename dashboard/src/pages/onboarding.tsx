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
import { OnboardingCompleteResponseSchema } from "@/types/schemas";
import { useDashboardStore } from "@/stores/dashboard";
import {
  useResolvedMindId,
  useOnboardingState,
} from "@/hooks/use-resolved-mind-id";

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
  // v0.32.0 BT.B.1 — both the resolved mind id AND the broader
  // onboarding-state payload now come from shared singleton hooks
  // (``useResolvedMindId`` + ``useOnboardingState``). They share one
  // module-level fetch — only one ``/api/onboarding/state`` request
  // is fired per page lifetime regardless of how many consumers
  // mount. Closes the structural side of CLAUDE.md anti-pattern #35:
  // previous releases duplicated the fetch+state+warn dance per
  // consumer, and each new ``mindId`` prop site was at risk of
  // hardcoding ``"default"``. The mind id hook normalises the
  // snapshot to a non-null string + exposes ``isFallback`` so
  // consumers can branch defensively. ``VoiceStep`` keeps accepting
  // ``string | null`` for back-compat with pre-hook tests that pass
  // a literal id.
  const { mindId: resolvedMindId, isFallback: mindIdIsFallback } =
    useResolvedMindId();
  const mindId = mindIdIsFallback ? null : resolvedMindId;
  const onboardingSnapshot = useOnboardingState();
  const [language, setLanguage] = useState(
    () => navigator.language?.split("-")[0] ?? "en",
  );
  const [ollamaAvailable, setOllamaAvailable] = useState(false);
  const [ollamaModels, setOllamaModels] = useState<string[]>([]);

  // Once the singleton snapshot resolves (success OR error), drain
  // the broader fields into local component state + lift the loading
  // gate. Errors are swallowed defensively — same semantics as the
  // pre-BT.B.1 ``.catch(() => {})`` so a transient backend hiccup
  // doesn't trap the operator on the loading spinner.
  //
  // The set-state-in-effect rule deliberately disabled here: the
  // initial population of these fields IS a one-shot hydration from
  // an external store (the singleton), which is exactly the pattern
  // ``useEffect`` is designed for. Cascading renders are not a
  // concern because the snapshot reference is stable post-resolution
  // (the singleton mutates by reference but ``useSyncExternalStore``
  // returns the same object until ``notifyAll`` fires, and ``notifyAll``
  // fires at most once per page lifetime — at fetch resolution).
  /* eslint-disable react-hooks/set-state-in-effect */
  useEffect(() => {
    if (onboardingSnapshot.isLoading) return;
    const data = onboardingSnapshot.state;
    if (data) {
      if (data.complete) {
        navigate("/", { replace: true });
        return;
      }
      setMindName(data.mind_name || "Sovyx");
      setOllamaAvailable(data.ollama_available);
      setOllamaModels(data.ollama_models);
      if (data.provider_configured) {
        setProvider(data.default_provider);
        setModel(data.default_model);
        setStep(2);
      }
    }
    setLoading(false);
  }, [onboardingSnapshot, navigate]);
  /* eslint-enable react-hooks/set-state-in-effect */

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
