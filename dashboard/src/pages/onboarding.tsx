/**
 * OnboardingPage — full-page first-run wizard.
 *
 * Three steps:
 *   1. Choose LLM provider + enter API key (or select Ollama)
 *   2. Personality preset + language + name (optional, skippable)
 *   3. First conversation with Aria (live chat)
 *
 * After completion, marks onboarding_complete and redirects to overview.
 */

import { useCallback, useEffect, useState } from "react";
import { useNavigate } from "react-router";
import { api } from "@/lib/api";
import { ProviderStep, PersonalityStep, FirstChatStep } from "@/components/onboarding";

interface OnboardingState {
  complete: boolean;
  provider_configured: boolean;
  default_provider: string;
  default_model: string;
  ollama_available: boolean;
  ollama_models: string[];
}

export default function OnboardingPage() {
  const navigate = useNavigate();
  const [step, setStep] = useState(1);
  const [loading, setLoading] = useState(true);
  const [provider, setProvider] = useState("");
  const [model, setModel] = useState("");

  useEffect(() => {
    api
      .get<OnboardingState>("/api/onboarding/state")
      .then((state) => {
        if (state.complete) {
          navigate("/", { replace: true });
          return;
        }
        if (state.provider_configured) {
          setProvider(state.default_provider);
          setModel(state.default_model);
          setStep(2);
        }
      })
      .catch(() => {})
      .finally(() => setLoading(false));
  }, [navigate]);

  const [ollamaAvailable, setOllamaAvailable] = useState(false);
  const [ollamaModels, setOllamaModels] = useState<string[]>([]);

  useEffect(() => {
    api
      .get<OnboardingState>("/api/onboarding/state")
      .then((state) => {
        setOllamaAvailable(state.ollama_available);
        setOllamaModels(state.ollama_models);
      })
      .catch(() => {});
  }, []);

  const handleProviderConfigured = useCallback(
    (p: string, m: string) => {
      setProvider(p);
      setModel(m);
      setStep(2);
    },
    [],
  );

  const handlePersonalityDone = useCallback(() => {
    setStep(3);
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
        {/* Header */}
        <div className="text-center">
          <div className="text-sm font-medium text-[var(--svx-color-text-tertiary)]">
            Step {step} of 3
          </div>
        </div>

        {/* Steps */}
        {step === 1 && (
          <ProviderStep
            ollamaAvailable={ollamaAvailable}
            ollamaModels={ollamaModels}
            onConfigured={handleProviderConfigured}
          />
        )}

        {step === 2 && (
          <PersonalityStep
            onConfigured={handlePersonalityDone}
            onSkip={handlePersonalityDone}
          />
        )}

        {step === 3 && (
          <FirstChatStep
            provider={provider}
            model={model}
            onComplete={handleComplete}
          />
        )}

        {/* Skip all */}
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

        {/* Progress dots */}
        <div className="flex justify-center gap-2">
          {[1, 2, 3].map((s) => (
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
