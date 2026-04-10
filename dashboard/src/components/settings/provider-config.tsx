/**
 * ProviderConfig — LLM provider configuration card.
 *
 * Fetches GET /api/providers, displays cloud + local provider status,
 * allows switching active provider/model via PUT /api/providers.
 *
 * Design: dark theme, svx design tokens, no animation libraries.
 */

import { useCallback, useEffect, useMemo, useState } from "react";
import { useTranslation } from "react-i18next";
import {
  AlertTriangleIcon,
  CheckCircle2Icon,
  CircleIcon,
  Loader2Icon,
  RefreshCwIcon,
  ServerIcon,
  WifiOffIcon,
} from "lucide-react";
import { toast } from "sonner";

import { api } from "@/lib/api";
import { Button } from "@/components/ui/button";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";

/* ── Types ── */

interface ProviderInfo {
  name: string;
  configured: boolean;
  available: boolean;
  reachable?: boolean;
  models?: string[];
  base_url?: string;
}

interface ActiveProvider {
  provider: string;
  model: string;
  fast_model: string;
}

interface ProvidersResponse {
  providers: ProviderInfo[];
  active: ActiveProvider;
}

/* ── Helpers ── */

const CLOUD_PROVIDERS = new Set(["anthropic", "openai", "google"]);

function providerLabel(name: string): string {
  const labels: Record<string, string> = {
    anthropic: "Anthropic",
    openai: "OpenAI",
    google: "Google",
    ollama: "Ollama",
  };
  return labels[name] ?? name.charAt(0).toUpperCase() + name.slice(1);
}

/* ── Component ── */

export function ProviderConfig() {
  const { t } = useTranslation("settings");

  const [data, setData] = useState<ProvidersResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // Selection state (user picks before saving)
  const [selectedProvider, setSelectedProvider] = useState("");
  const [selectedModel, setSelectedModel] = useState("");

  const fetchProviders = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const res = await api.get<ProvidersResponse>("/api/providers");
      setData(res);
      setSelectedProvider(res.active.provider);
      setSelectedModel(res.active.model);
    } catch {
      setError(t("providers.loadFailed", "Failed to load providers"));
    } finally {
      setLoading(false);
    }
  }, [t]);

  useEffect(() => {
    void fetchProviders();
  }, [fetchProviders]);

  const cloudProviders = useMemo(
    () => data?.providers.filter((p) => CLOUD_PROVIDERS.has(p.name)) ?? [],
    [data],
  );

  const ollamaProvider = useMemo(
    () => data?.providers.find((p) => p.name === "ollama") ?? null,
    [data],
  );

  const hasAnyAvailable = useMemo(() => {
    if (!data) return false;
    return data.providers.some((p) => p.available || p.reachable);
  }, [data]);

  const isDirty = useMemo(() => {
    if (!data) return false;
    return (
      selectedProvider !== data.active.provider ||
      selectedModel !== data.active.model
    );
  }, [data, selectedProvider, selectedModel]);

  const handleSelectCloud = useCallback(
    (name: string) => {
      const provider = data?.providers.find((p) => p.name === name);
      if (!provider?.available) return;
      setSelectedProvider(name);
      // Cloud providers don't expose model list — keep current model or clear
      setSelectedModel(data?.active.provider === name ? data.active.model : "");
    },
    [data],
  );

  const handleSelectOllama = useCallback(
    (model: string) => {
      setSelectedProvider("ollama");
      setSelectedModel(model);
    },
    [],
  );

  const handleSave = useCallback(async () => {
    if (!isDirty || saving) return;
    setSaving(true);
    try {
      const res = await api.put<{ ok: boolean; error?: string }>(
        "/api/providers",
        { provider: selectedProvider, model: selectedModel },
      );
      if (res.ok) {
        toast.success(t("providers.saved", "Provider updated"));
        await fetchProviders(); // refresh to sync
      } else {
        toast.error(res.error ?? t("providers.saveFailed", "Failed to save"));
      }
    } catch {
      toast.error(t("providers.saveFailed", "Failed to save"));
    } finally {
      setSaving(false);
    }
  }, [isDirty, saving, selectedProvider, selectedModel, t, fetchProviders]);

  /* ── Loading / Error states ── */

  if (loading && !data) {
    return (
      <Card>
        <CardContent className="flex items-center justify-center py-12">
          <Loader2Icon className="size-5 animate-spin text-muted-foreground" />
        </CardContent>
      </Card>
    );
  }

  if (error && !data) {
    return (
      <Card>
        <CardContent className="flex flex-col items-center gap-3 py-12">
          <WifiOffIcon className="size-6 text-muted-foreground" />
          <p className="text-sm text-muted-foreground">{error}</p>
          <Button variant="outline" size="sm" onClick={() => void fetchProviders()}>
            <RefreshCwIcon className="mr-1.5 size-3.5" />
            {t("providers.retry", "Retry")}
          </Button>
        </CardContent>
      </Card>
    );
  }

  return (
    <Card data-testid="provider-config">
      <CardHeader>
        <CardTitle className="flex items-center gap-2">
          <ServerIcon className="size-4" />
          {t("providers.title", "LLM Providers")}
        </CardTitle>
        <CardDescription>
          {t(
            "providers.description",
            "Configure which AI provider powers your mind.",
          )}
        </CardDescription>
      </CardHeader>

      {/* ── Empty state: no providers available ── */}
      {data && !hasAnyAvailable && (
        <CardContent>
          <div className="rounded-lg border border-amber-500/30 bg-amber-500/5 p-4" data-testid="provider-empty-state">
            <div className="flex items-start gap-3">
              <AlertTriangleIcon className="mt-0.5 size-5 shrink-0 text-amber-500" />
              <div className="space-y-3 text-sm">
                <p className="font-medium text-foreground">
                  {t("providers.noProviderTitle", "No provider configured")}
                </p>

                <div>
                  <p className="text-muted-foreground">
                    {t("providers.cloudSetup", "Cloud (requires API key):")}
                  </p>
                  <p className="mt-1 text-xs text-muted-foreground">
                    {t("providers.cloudInstructions", "Set one as environment variable and restart:")}
                  </p>
                  <ul className="mt-1 list-inside list-disc text-xs text-muted-foreground">
                    <li><code className="rounded bg-muted px-1 font-mono">ANTHROPIC_API_KEY</code> — Claude</li>
                    <li><code className="rounded bg-muted px-1 font-mono">OPENAI_API_KEY</code> — GPT-4o</li>
                    <li><code className="rounded bg-muted px-1 font-mono">GOOGLE_API_KEY</code> — Gemini</li>
                  </ul>
                </div>

                <div>
                  <p className="text-muted-foreground">
                    {t("providers.localSetup", "Local (free):")}
                  </p>
                  <ol className="mt-1 list-inside list-decimal text-xs text-muted-foreground">
                    <li>{t("providers.ollamaStep1", "Install Ollama from ollama.com")}</li>
                    <li><code className="rounded bg-muted px-1 font-mono">ollama pull llama3.1</code></li>
                    <li>{t("providers.ollamaStep3", "Click \"Refresh\" below")}</li>
                  </ol>
                </div>
              </div>
            </div>

            <div className="mt-3 flex justify-end">
              <Button
                variant="outline"
                size="sm"
                onClick={() => void fetchProviders()}
                disabled={loading}
              >
                <RefreshCwIcon className={`mr-1.5 size-3.5 ${loading ? "animate-spin" : ""}`} />
                {t("providers.refresh", "Refresh")}
              </Button>
            </div>
          </div>
        </CardContent>
      )}

      {/* ── Provider list ── */}
      {data && (
      <CardContent className="space-y-6">
        {/* ── Cloud Providers ── */}
        {cloudProviders.length > 0 && (
          <div className="space-y-2">
            <h4 className="text-xs font-medium uppercase tracking-wider text-muted-foreground">
              {t("providers.cloudProviders", "Cloud Providers")}
            </h4>
            <div className="space-y-1">
              {cloudProviders.map((p) => (
                <button
                  key={p.name}
                  type="button"
                  disabled={!p.available}
                  onClick={() => handleSelectCloud(p.name)}
                  className={`flex w-full items-center justify-between rounded-lg border px-3 py-2 text-sm transition-colors ${
                    selectedProvider === p.name
                      ? "border-primary/50 bg-primary/10"
                      : p.available
                        ? "border-border hover:border-primary/30 hover:bg-muted/50"
                        : "cursor-not-allowed border-border/50 opacity-50"
                  }`}
                  data-testid={`provider-${p.name}`}
                >
                  <span className="flex items-center gap-2">
                    {p.available ? (
                      <CheckCircle2Icon className="size-3.5 text-emerald-500" />
                    ) : (
                      <CircleIcon className="size-3.5 text-muted-foreground" />
                    )}
                    {providerLabel(p.name)}
                  </span>
                  <Badge variant={p.available ? "default" : "outline"}>
                    {p.available
                      ? t("providers.configured", "configured")
                      : t("providers.notConfigured", "not configured")}
                  </Badge>
                </button>
              ))}
            </div>
          </div>
        )}

        {/* ── Local Provider (Ollama) ── */}
        {ollamaProvider && (
          <div className="space-y-2">
            <h4 className="text-xs font-medium uppercase tracking-wider text-muted-foreground">
              {t("providers.localProvider", "Local Provider")}
            </h4>
            <div
              className={`rounded-lg border px-3 py-3 ${
                ollamaProvider.reachable
                  ? "border-border"
                  : "border-destructive/30"
              }`}
              data-testid="provider-ollama"
            >
              <div className="flex items-center justify-between">
                <span className="flex items-center gap-2 text-sm">
                  {ollamaProvider.reachable ? (
                    <CheckCircle2Icon className="size-3.5 text-emerald-500" />
                  ) : (
                    <WifiOffIcon className="size-3.5 text-destructive" />
                  )}
                  Ollama
                </span>
                <Badge
                  variant={ollamaProvider.reachable ? "default" : "destructive"}
                >
                  {ollamaProvider.reachable
                    ? t("providers.running", "running") +
                      ` (${ollamaProvider.models?.length ?? 0} ${t("providers.models", "models")})`
                    : t("providers.notRunning", "not running")}
                </Badge>
              </div>

              {/* Model selector */}
              {ollamaProvider.reachable &&
                ollamaProvider.models &&
                ollamaProvider.models.length > 0 && (
                  <div className="mt-3">
                    <label
                      htmlFor="ollama-model"
                      className="mb-1 block text-xs text-muted-foreground"
                    >
                      {t("providers.selectModel", "Model")}
                    </label>
                    <select
                      id="ollama-model"
                      value={
                        selectedProvider === "ollama" ? selectedModel : ""
                      }
                      onChange={(e) => handleSelectOllama(e.target.value)}
                      className="w-full rounded-md border border-input bg-background px-3 py-1.5 text-sm text-foreground outline-none transition-colors focus:border-ring focus:ring-2 focus:ring-ring/50"
                      data-testid="ollama-model-select"
                    >
                      <option value="" disabled>
                        {t("providers.chooseModel", "Choose a model...")}
                      </option>
                      {ollamaProvider.models.map((m) => (
                        <option key={m} value={m}>
                          {m}
                        </option>
                      ))}
                    </select>
                  </div>
                )}

              {/* Base URL info */}
              {ollamaProvider.base_url && (
                <p className="mt-2 text-xs text-muted-foreground">
                  {ollamaProvider.base_url}
                </p>
              )}

              {/* Not running instructions */}
              {!ollamaProvider.reachable && (
                <p className="mt-2 text-xs text-muted-foreground">
                  {t(
                    "providers.ollamaInstructions",
                    "Install Ollama from ollama.com, then run: ollama serve",
                  )}
                </p>
              )}
            </div>
          </div>
        )}

        {/* ── Active summary + Save ── */}
        <div className="flex items-center justify-between border-t border-border pt-4">
          <div className="text-sm text-muted-foreground">
            <span className="font-medium text-foreground">
              {t("providers.active", "Active")}:
            </span>{" "}
            {selectedProvider
              ? `${providerLabel(selectedProvider)} / ${selectedModel || "—"}`
              : t("providers.noneSelected", "none selected")}
          </div>
          <div className="flex gap-2">
            <Button
              variant="outline"
              size="sm"
              onClick={() => void fetchProviders()}
              disabled={loading}
            >
              <RefreshCwIcon
                className={`mr-1.5 size-3.5 ${loading ? "animate-spin" : ""}`}
              />
              {t("providers.refresh", "Refresh")}
            </Button>
            <Button
              size="sm"
              onClick={() => void handleSave()}
              disabled={!isDirty || saving || !selectedModel}
            >
              {saving && (
                <Loader2Icon className="mr-1.5 size-3.5 animate-spin" />
              )}
              {t("providers.save", "Save Changes")}
            </Button>
          </div>
        </div>
      </CardContent>
      )}
    </Card>
  );
}
