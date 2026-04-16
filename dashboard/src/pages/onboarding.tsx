/**
 * OnboardingPage -- full-page first-run wizard.
 *
 * Four steps:
 *   1. Choose LLM provider + enter API key (or select Ollama)
 *   2. Personality preset + companion name + language (skippable)
 *   3. Connect channels — Telegram hot-add (skippable)
 *   4. First conversation (live chat with dynamic mind name)
 *
 * After completion, marks onboarding_complete and redirects to overview.
 */

import { useCallback, useEffect, useState } from "react";
import { useNavigate } from "react-router";
import { api } from "@/lib/api";
import {
  ProviderStep,
  PersonalityStep,
  ChannelsStep,
  FirstChatStep,
} from "@/components/onboarding";

interface OnboardingState {
  complete: boolean;
  mind_name: string;
  provider_configured: boolean;
  default_provider: string;
  default_model: string;
  ollama_available: boolean;
  ollama_models: string[];
}

const TOTAL_STEPS = 4;

export default function OnboardingPage() {
  const navigate = useNavigate();
  const [step, setStep] = useState(1);
  const [loading, setLoading] = useState(true);
  const [provider, setProvider] = useState("");
  const [model, setModel] = useState("");
  const [mindName, setMindName] = useState("Sovyx");
  const [ollamaAvailable, setOllamaAvailable] = useState(false);
  const [ollamaModels, setOllamaModels] = useState<string[]>([]);

  useEffect(() => {
    api
      .get<OnboardingState>("/api/onboarding/state")
      .then((state) => {
        if (state.complete) {
          navigate("/", { replace: true });
          return;
        }
        setMindName(state.mind_name || "Sovyx");
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

  const handlePersonalityDone = useCallback((newName?: string) => {
    if (newName) setMindName(newName);
    setStep(3);
  }, []);

  const handleChannelsDone = useCallback(() => {
    setStep(4);
  }, []);

  const handleComplete = useCallback(async () => {
    try {
      await api.post("/api/onboarding/complete", {});
    } catch {
      // Best effort
    }
    navigate("/", { replace: true });
  }, [navigate]);

  const handleSkipAll = useCallback(async () => {
    try {
      await api.post("/api/onboarding/complete", {});
    } catch {
      // Best effort
    }
    navigate("/", { replace: true });
  }, [navigate]);

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
            Step {step} of {TOTAL_STEPS}
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
          <FirstChatStep
            mindName={mindName}
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
              I'll configure manually
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
