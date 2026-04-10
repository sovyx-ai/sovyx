/**
 * Voice Pipeline — Real-time status dashboard for voice components.
 *
 * Fetches from:
 * - GET /api/voice/status  → pipeline state, STT/TTS/VAD/WakeWord/Wyoming
 * - GET /api/voice/models  → model matrix per hardware tier
 *
 * Read-only display. No mutations.
 *
 * Ref: TASK-204 (Dashboard Credibility Sweep)
 */

import { useEffect, useState, useCallback } from "react";
import { useTranslation } from "react-i18next";
import {
  MicIcon,
  Volume2Icon,
  BrainIcon,
  RadioIcon,
  CpuIcon,
  Loader2Icon,
  AlertTriangleIcon,
  CircleIcon,
  AudioWaveformIcon,
  WifiIcon,
} from "lucide-react";
import { api, isAbortError } from "@/lib/api";

// ── Types ──

interface VoiceStatus {
  pipeline: {
    running: boolean;
    state: string;
    latency_ms: number | null;
  };
  stt: {
    engine: string | null;
    model: string | null;
    state: string | null;
  };
  tts: {
    engine: string | null;
    model: string | null;
    initialized: boolean;
  };
  wake_word: {
    enabled: boolean;
    phrase: string | null;
  };
  vad: {
    enabled: boolean;
  };
  wyoming: {
    connected: boolean;
    endpoint: string | null;
  };
  hardware: {
    tier: string | null;
    ram_mb: number | null;
  };
}

interface VoiceModels {
  detected_tier: string | null;
  active: Record<string, string> | null;
  available_tiers: Record<string, Record<string, string>>;
}

// ── Page ──

export default function VoicePage() {
  const { t } = useTranslation("voice");
  const [status, setStatus] = useState<VoiceStatus | null>(null);
  const [models, setModels] = useState<VoiceModels | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const fetchData = useCallback(
    async (signal?: AbortSignal) => {
      setLoading(true);
      setError(null);
      try {
        const [s, m] = await Promise.all([
          api.get<VoiceStatus>("/api/voice/status", { signal }),
          api.get<VoiceModels>("/api/voice/models", { signal }),
        ]);
        setStatus(s);
        setModels(m);
      } catch (err) {
        if (isAbortError(err)) return;
        setError(t("error"));
      } finally {
        setLoading(false);
      }
    },
    [t],
  );

  useEffect(() => {
    const ctrl = new AbortController();
    void fetchData(ctrl.signal);
    return () => ctrl.abort();
  }, [fetchData]);

  // Loading
  if (loading) {
    return (
      <div className="flex items-center justify-center py-20 text-[var(--svx-color-text-disabled)]">
        <Loader2Icon className="mr-2 size-5 animate-spin" />
        <span className="text-sm">{t("loading")}</span>
      </div>
    );
  }

  // Error
  if (error || !status) {
    return (
      <div className="flex flex-col items-center justify-center gap-3 py-20 text-[var(--svx-color-status-warning)]">
        <AlertTriangleIcon className="size-6" />
        <span className="text-sm">{error ?? t("error")}</span>
        <button
          type="button"
          onClick={() => void fetchData()}
          className="text-xs underline hover:no-underline"
        >
          {t("retry")}
        </button>
      </div>
    );
  }

  return (
    <div className="space-y-6">
      {/* Header */}
      <div>
        <h1 className="text-2xl font-bold text-[var(--svx-color-text-primary)]">
          {t("title")}
        </h1>
        <p className="text-sm text-[var(--svx-color-text-secondary)]">
          {t("subtitle")}
        </p>
      </div>

      {/* Status Grid */}
      <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
        {/* Pipeline */}
        <StatusCard
          icon={<MicIcon className="size-4" />}
          title={t("sections.pipeline")}
        >
          <StatusRow label={t("pipeline.state")}>
            <StatusDot active={status.pipeline.running} />
            <span>
              {status.pipeline.running ? t("pipeline.running") : t("pipeline.stopped")}
            </span>
          </StatusRow>
          {status.pipeline.state !== "not_configured" && (
            <StatusRow label={t("pipeline.latency")}>
              {status.pipeline.latency_ms != null
                ? t("pipeline.latencyMs", { ms: status.pipeline.latency_ms })
                : t("pipeline.noLatency")}
            </StatusRow>
          )}
        </StatusCard>

        {/* STT */}
        <StatusCard
          icon={<AudioWaveformIcon className="size-4" />}
          title={t("sections.stt")}
        >
          {status.stt.engine ? (
            <>
              <StatusRow label={t("stt.engine")}>{status.stt.engine}</StatusRow>
              <StatusRow label={t("stt.model")}>{status.stt.model ?? "—"}</StatusRow>
              {status.stt.state && (
                <StatusRow label={t("stt.state")}>
                  <StatusDot active={status.stt.state === "ready"} />
                  <span className="capitalize">{status.stt.state}</span>
                </StatusRow>
              )}
            </>
          ) : (
            <p className="text-xs text-[var(--svx-color-text-disabled)]">{t("stt.none")}</p>
          )}
        </StatusCard>

        {/* TTS */}
        <StatusCard
          icon={<Volume2Icon className="size-4" />}
          title={t("sections.tts")}
        >
          {status.tts.engine ? (
            <>
              <StatusRow label={t("tts.engine")}>{status.tts.engine}</StatusRow>
              <StatusRow label={t("tts.model")}>
                {status.tts.model ? status.tts.model.split("/").pop() : "—"}
              </StatusRow>
              <StatusRow label={t("tts.initialized")}>
                <StatusDot active={status.tts.initialized} />
                <span>{status.tts.initialized ? t("tts.yes") : t("tts.no")}</span>
              </StatusRow>
            </>
          ) : (
            <p className="text-xs text-[var(--svx-color-text-disabled)]">{t("tts.none")}</p>
          )}
        </StatusCard>

        {/* VAD */}
        <StatusCard
          icon={<RadioIcon className="size-4" />}
          title={t("sections.vad")}
        >
          <StatusRow label="">
            <StatusDot active={status.vad.enabled} />
            <span>{status.vad.enabled ? t("vad.enabled") : t("vad.disabled")}</span>
          </StatusRow>
        </StatusCard>

        {/* Wake Word */}
        <StatusCard
          icon={<BrainIcon className="size-4" />}
          title={t("sections.wakeWord")}
        >
          <StatusRow label="">
            <StatusDot active={status.wake_word.enabled} />
            <span>
              {status.wake_word.enabled ? t("wakeWord.enabled") : t("wakeWord.disabled")}
            </span>
          </StatusRow>
          {status.wake_word.phrase && (
            <StatusRow label={t("wakeWord.phrase")}>
              <code className="rounded bg-[var(--svx-color-bg-subtle)] px-1.5 py-0.5 text-xs">
                {status.wake_word.phrase}
              </code>
            </StatusRow>
          )}
        </StatusCard>

        {/* Wyoming */}
        <StatusCard
          icon={<WifiIcon className="size-4" />}
          title={t("sections.wyoming")}
        >
          <StatusRow label="">
            <StatusDot active={status.wyoming.connected} />
            <span>
              {status.wyoming.connected
                ? t("wyoming.connected")
                : t("wyoming.disconnected")}
            </span>
          </StatusRow>
          {status.wyoming.endpoint && (
            <StatusRow label={t("wyoming.endpoint")}>
              <code className="rounded bg-[var(--svx-color-bg-subtle)] px-1.5 py-0.5 text-xs">
                {status.wyoming.endpoint}
              </code>
            </StatusRow>
          )}
        </StatusCard>
      </div>

      {/* Hardware */}
      <section className="rounded-[var(--svx-radius-lg)] border border-[var(--svx-color-border-default)] bg-[var(--svx-color-bg-surface)] p-4">
        <div className="flex items-center gap-2">
          <CpuIcon className="size-4 text-[var(--svx-color-brand-primary)]" />
          <h2 className="text-sm font-medium text-[var(--svx-color-text-primary)]">
            {t("sections.hardware")}
          </h2>
        </div>
        <div className="mt-3 flex flex-wrap gap-6 text-xs text-[var(--svx-color-text-secondary)]">
          <div>
            <span className="text-[var(--svx-color-text-tertiary)]">{t("hardware.tier")}: </span>
            <span className="font-medium">{status.hardware.tier ?? t("hardware.unknown")}</span>
          </div>
          {status.hardware.ram_mb != null && (
            <div>
              <span className="text-[var(--svx-color-text-tertiary)]">{t("hardware.ram")}: </span>
              <span className="font-medium">
                {t("hardware.ramMb", { mb: status.hardware.ram_mb })}
              </span>
            </div>
          )}
        </div>
      </section>

      {/* Model Matrix */}
      {models && Object.keys(models.available_tiers).length > 0 && (
        <section className="rounded-[var(--svx-radius-lg)] border border-[var(--svx-color-border-default)] bg-[var(--svx-color-bg-surface)] p-4">
          <h2 className="text-sm font-medium text-[var(--svx-color-text-primary)]">
            {t("models.title")}
          </h2>
          <p className="mt-1 text-xs text-[var(--svx-color-text-tertiary)]">
            {t("models.subtitle")}
            {models.detected_tier && (
              <span className="ml-2 rounded bg-[var(--svx-color-brand-primary)]/10 px-1.5 py-0.5 text-[10px] font-medium text-[var(--svx-color-brand-primary)]">
                {t("models.activeTier")}: {models.detected_tier}
              </span>
            )}
          </p>

          <div className="mt-3 overflow-x-auto">
            <table className="w-full text-xs">
              <thead>
                <tr className="border-b border-[var(--svx-color-border-subtle)]">
                  <th className="py-2 pr-4 text-left font-medium text-[var(--svx-color-text-tertiary)]">
                    Tier
                  </th>
                  <th className="py-2 pr-4 text-left font-medium text-[var(--svx-color-text-tertiary)]">
                    {t("models.sttPrimary")}
                  </th>
                  <th className="py-2 pr-4 text-left font-medium text-[var(--svx-color-text-tertiary)]">
                    {t("models.ttsPrimary")}
                  </th>
                  <th className="py-2 pr-4 text-left font-medium text-[var(--svx-color-text-tertiary)]">
                    {t("models.wake")}
                  </th>
                </tr>
              </thead>
              <tbody>
                {Object.entries(models.available_tiers).map(([tier, sel]) => (
                  <tr
                    key={tier}
                    className={`border-b border-[var(--svx-color-border-subtle)] ${
                      tier === models.detected_tier
                        ? "bg-[var(--svx-color-brand-primary)]/5"
                        : ""
                    }`}
                  >
                    <td className="py-2 pr-4 font-medium text-[var(--svx-color-text-secondary)]">
                      {tier}
                      {tier === models.detected_tier && (
                        <span className="ml-1 text-[var(--svx-color-brand-primary)]">●</span>
                      )}
                    </td>
                    <td className="py-2 pr-4 font-mono text-[var(--svx-color-text-tertiary)]">
                      {sel.stt_primary}
                    </td>
                    <td className="py-2 pr-4 font-mono text-[var(--svx-color-text-tertiary)]">
                      {sel.tts_primary}
                    </td>
                    <td className="py-2 pr-4 font-mono text-[var(--svx-color-text-tertiary)]">
                      {sel.wake}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </section>
      )}
    </div>
  );
}

// ── Sub-components ──

function StatusCard({
  icon,
  title,
  children,
}: {
  icon: React.ReactNode;
  title: string;
  children: React.ReactNode;
}) {
  return (
    <div className="rounded-[var(--svx-radius-lg)] border border-[var(--svx-color-border-default)] bg-[var(--svx-color-bg-surface)] p-4">
      <div className="flex items-center gap-2">
        <span className="text-[var(--svx-color-brand-primary)]">{icon}</span>
        <h3 className="text-sm font-medium text-[var(--svx-color-text-primary)]">{title}</h3>
      </div>
      <div className="mt-3 space-y-2">{children}</div>
    </div>
  );
}

function StatusRow({
  label,
  children,
}: {
  label: string;
  children: React.ReactNode;
}) {
  return (
    <div className="flex items-center justify-between text-xs">
      {label && (
        <span className="text-[var(--svx-color-text-tertiary)]">{label}</span>
      )}
      <span className="flex items-center gap-1.5 font-medium text-[var(--svx-color-text-secondary)]">
        {children}
      </span>
    </div>
  );
}

function StatusDot({ active }: { active: boolean }) {
  return (
    <CircleIcon
      className={`size-2 fill-current ${
        active
          ? "text-[var(--svx-color-status-success)]"
          : "text-[var(--svx-color-text-disabled)]"
      }`}
    />
  );
}
