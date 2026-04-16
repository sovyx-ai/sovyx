import { useCallback, useState } from "react";
import {
  CheckCircle2Icon,
  LoaderIcon,
  ExternalLinkIcon,
  XCircleIcon,
  ServerIcon,
} from "lucide-react";
import { api } from "@/lib/api";
import { PROVIDERS, type ProviderMeta } from "@/lib/providers-data";
import { Button } from "@/components/ui/button";
import { cn } from "@/lib/utils";

interface ProviderStepProps {
  ollamaAvailable: boolean;
  ollamaModels: string[];
  onConfigured: (provider: string, model: string) => void;
}

export function ProviderStep({
  ollamaAvailable,
  ollamaModels,
  onConfigured,
}: ProviderStepProps) {
  const [selected, setSelected] = useState<ProviderMeta | null>(
    ollamaAvailable ? PROVIDERS.find((p) => p.id === "ollama") ?? null : null,
  );
  const [apiKey, setApiKey] = useState("");
  const [ollamaModel, setOllamaModel] = useState(ollamaModels[0] ?? "");
  const [testing, setTesting] = useState(false);
  const [testResult, setTestResult] = useState<{
    ok: boolean;
    message: string;
  } | null>(null);
  const [configuring, setConfiguring] = useState(false);

  const handleTest = useCallback(async () => {
    if (!selected) return;
    setTesting(true);
    setTestResult(null);
    try {
      const body: Record<string, string> = { provider: selected.id };
      if (selected.id !== "ollama") body.api_key = apiKey;
      if (selected.id === "ollama" && ollamaModel) body.model = ollamaModel;

      const result = await api.post<{ ok: boolean; provider: string; model: string }>(
        "/api/onboarding/provider",
        body,
      );
      if (result.ok) {
        setTestResult({ ok: true, message: `Connected - ${result.model}` });
        onConfigured(result.provider, result.model);
      }
    } catch (err) {
      const msg = err instanceof Error ? err.message : String(err);
      try {
        const parsed = JSON.parse(msg) as { error?: string };
        setTestResult({ ok: false, message: parsed.error ?? msg });
      } catch {
        setTestResult({ ok: false, message: msg });
      }
    } finally {
      setTesting(false);
    }
  }, [selected, apiKey, ollamaModel, onConfigured]);

  const handleConfigure = useCallback(async () => {
    if (!selected) return;
    setConfiguring(true);
    try {
      const body: Record<string, string> = { provider: selected.id };
      if (selected.id !== "ollama") body.api_key = apiKey;
      if (selected.id === "ollama" && ollamaModel) body.model = ollamaModel;
      else body.model = selected.defaultModel;

      const result = await api.post<{ ok: boolean; provider: string; model: string }>(
        "/api/onboarding/provider",
        body,
      );
      if (result.ok) {
        onConfigured(result.provider, result.model);
      }
    } catch {
      // Error handled by test flow
    } finally {
      setConfiguring(false);
    }
  }, [selected, apiKey, ollamaModel, onConfigured]);

  const isOllama = selected?.id === "ollama";
  const canTest = isOllama ? ollamaAvailable : apiKey.length > 5;

  return (
    <div className="space-y-6">
      <div>
        <h2 className="text-xl font-semibold text-[var(--svx-color-text-primary)]">
          Choose Your Brain
        </h2>
        <p className="mt-1 text-sm text-[var(--svx-color-text-secondary)]">
          Select an LLM provider to power your companion.
        </p>
      </div>

      {/* Provider grid */}
      <div className="grid grid-cols-2 gap-2 sm:grid-cols-3 md:grid-cols-5">
        {PROVIDERS.map((p) => (
          <button
            key={p.id}
            type="button"
            onClick={() => {
              setSelected(p);
              setTestResult(null);
              setApiKey("");
            }}
            className={cn(
              "relative rounded-[var(--svx-radius-lg)] border p-3 text-left transition-all",
              selected?.id === p.id
                ? "border-[var(--svx-color-brand-primary)] bg-[var(--svx-color-brand-primary)]/5"
                : "border-[var(--svx-color-border-default)] bg-[var(--svx-color-bg-surface)] hover:border-[var(--svx-color-brand-primary)]/40",
            )}
          >
            <div className="text-sm font-medium text-[var(--svx-color-text-primary)]">
              {p.name}
            </div>
            {p.local && ollamaAvailable && (
              <span className="mt-1 inline-block rounded-full bg-[var(--svx-color-success)]/10 px-2 py-0.5 text-[10px] font-medium text-[var(--svx-color-success)]">
                Detected
              </span>
            )}
            {p.local && !ollamaAvailable && (
              <span className="mt-1 inline-block text-[10px] text-[var(--svx-color-text-tertiary)]">
                Local
              </span>
            )}
            {!p.local && (
              <span className="mt-1 inline-block text-[10px] text-[var(--svx-color-text-tertiary)]">
                Cloud
              </span>
            )}
          </button>
        ))}
      </div>

      {/* Selected provider detail */}
      {selected && (
        <div className="rounded-[var(--svx-radius-lg)] border border-[var(--svx-color-border-default)] bg-[var(--svx-color-bg-surface)] p-5 space-y-4">
          <div>
            <h3 className="text-sm font-semibold text-[var(--svx-color-text-primary)]">
              {selected.name}
            </h3>
            <p className="mt-0.5 text-xs text-[var(--svx-color-text-secondary)]">
              {selected.description}
            </p>
            <div className="mt-2 flex items-center gap-4 text-xs text-[var(--svx-color-text-tertiary)]">
              <span>Model: {isOllama ? (ollamaModel || "auto") : selected.defaultModel}</span>
              <span>{selected.pricing}</span>
            </div>
          </div>

          {/* Ollama: model picker */}
          {isOllama && ollamaAvailable && ollamaModels.length > 0 && (
            <div>
              <label className="mb-1 block text-xs font-medium text-[var(--svx-color-text-secondary)]">
                Model
              </label>
              <select
                value={ollamaModel}
                onChange={(e) => setOllamaModel(e.target.value)}
                className="w-full rounded-[var(--svx-radius-md)] border border-[var(--svx-color-border-default)] bg-[var(--svx-color-bg-elevated)] px-3 py-2 text-sm text-[var(--svx-color-text-primary)]"
              >
                {ollamaModels.map((m) => (
                  <option key={m} value={m}>
                    {m}
                  </option>
                ))}
              </select>
            </div>
          )}

          {/* Ollama: not running */}
          {isOllama && !ollamaAvailable && (
            <div className="rounded-[var(--svx-radius-md)] bg-[var(--svx-color-warning)]/10 px-3 py-2.5 text-xs text-[var(--svx-color-warning)]">
              <ServerIcon className="mr-1.5 inline size-3.5" />
              Ollama is not running. Install and start it, then refresh.
              <a
                href="https://ollama.com"
                target="_blank"
                rel="noopener noreferrer"
                className="ml-1 underline"
              >
                Download
                <ExternalLinkIcon className="ml-0.5 inline size-3" />
              </a>
            </div>
          )}

          {/* Cloud: API key input */}
          {!isOllama && (
            <div>
              <label className="mb-1 block text-xs font-medium text-[var(--svx-color-text-secondary)]">
                API Key
              </label>
              <input
                type="password"
                value={apiKey}
                onChange={(e) => {
                  setApiKey(e.target.value);
                  setTestResult(null);
                }}
                placeholder={selected.keyPrefix ? `${selected.keyPrefix}...` : "Paste your API key"}
                className="w-full rounded-[var(--svx-radius-md)] border border-[var(--svx-color-border-default)] bg-[var(--svx-color-bg-elevated)] px-3 py-2 font-mono text-sm text-[var(--svx-color-text-primary)] placeholder:text-[var(--svx-color-text-disabled)]"
              />
              <a
                href={selected.keyUrl}
                target="_blank"
                rel="noopener noreferrer"
                className="mt-1 inline-flex items-center gap-1 text-xs text-[var(--svx-color-brand-primary)] hover:underline"
              >
                Get your key
                <ExternalLinkIcon className="size-3" />
              </a>
            </div>
          )}

          {/* Test result */}
          {testResult && (
            <div
              className={cn(
                "flex items-center gap-2 rounded-[var(--svx-radius-md)] px-3 py-2 text-xs",
                testResult.ok
                  ? "bg-[var(--svx-color-success)]/10 text-[var(--svx-color-success)]"
                  : "bg-[var(--svx-color-error)]/10 text-[var(--svx-color-error)]",
              )}
            >
              {testResult.ok ? (
                <CheckCircle2Icon className="size-3.5 shrink-0" />
              ) : (
                <XCircleIcon className="size-3.5 shrink-0" />
              )}
              <span>{testResult.message}</span>
            </div>
          )}

          {/* Action buttons */}
          <div className="flex items-center gap-3">
            {!testResult?.ok && (
              <Button onClick={handleTest} disabled={!canTest || testing} size="sm">
                {testing && <LoaderIcon className="mr-1.5 size-3.5 animate-spin" />}
                {testing ? "Testing..." : "Test Connection"}
              </Button>
            )}
            {testResult?.ok && (
              <Button onClick={handleConfigure} disabled={configuring}>
                {configuring && <LoaderIcon className="mr-1.5 size-3.5 animate-spin" />}
                Continue
              </Button>
            )}
          </div>
        </div>
      )}
    </div>
  );
}
