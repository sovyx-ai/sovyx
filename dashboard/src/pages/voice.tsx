/**
 * Voice Pipeline page — real-time status of the voice subsystem.
 *
 * Fetches /api/voice/status and /api/voice/models to display:
 * - Pipeline state (running/stopped, latency)
 * - STT engine + model
 * - TTS engine + model
 * - VAD enabled/disabled
 * - Wake word config
 * - Wyoming protocol status
 * - Hardware tier + model matrix
 *
 * Ref: TASK-204 (Credibility Sweep)
 */

import { useEffect, useState, useCallback } from "react";
import { useTranslation } from "react-i18next";
import {
  MicIcon,
  Volume2Icon,
  RadioIcon,
  WifiIcon,
  CpuIcon,
  AudioWaveformIcon,
  Loader2Icon,
  AlertTriangleIcon,
  RefreshCwIcon,
} from "lucide-react";
import { api, isAbortError } from "@/lib/api";
import { VoiceSetupModal } from "@/components/setup-wizard";

/* ── Types ── */

interface PipelineStatus {
  running: boolean;
  state: string;
  latency_ms: number | null;
}

interface STTStatus {
  engine: string | null;
  model: string | null;
  state: string | null;
}

interface TTSStatus {
  engine: string | null;
  model: string | null;
  initialized: boolean;
}

interface WakeWordStatus {
  enabled: boolean;
  phrase: string | null;
}

interface VADStatus {
  enabled: boolean;
}

interface WyomingStatus {
  connected: boolean;
  endpoint: string | null;
}

interface HardwareStatus {
  tier: string | null;
  ram_mb: number | null;
}

interface CaptureStatus {
  running: boolean;
  input_device: number | string | null;
  host_api: string | null;
  sample_rate: number | null;
  frames_delivered: number;
  last_rms_db: number | null;
}

interface VoiceStatus {
  pipeline: PipelineStatus;
  capture?: CaptureStatus;
  stt: STTStatus;
  tts: TTSStatus;
  wake_word: WakeWordStatus;
  vad: VADStatus;
  wyoming: WyomingStatus;
  hardware: HardwareStatus;
}

interface ModelSelection {
  stt_primary: string;
  stt_streaming: string;
  tts_primary: string;
  tts_quality: string;
  wake: string;
  vad: string;
}

interface VoiceModels {
  detected_tier: string | null;
  active: ModelSelection | null;
  available_tiers: Record<string, ModelSelection>;
}

/* ── VU-meter ── */

/**
 * Map dBFS RMS to a 0-100 % bar width.
 *
 * ``-80`` dBFS is the silence floor (matches
 * ``capture_validation_min_rms_db``); ``0`` dBFS is clipping. Anything
 * above ``-20`` lights up the "hot" band so the operator can spot
 * levels the STT will clip.
 */
function dbToPercent(db: number | null | undefined): number {
  if (db == null || !Number.isFinite(db)) return 0;
  const clamped = Math.max(-80, Math.min(0, db));
  return ((clamped + 80) / 80) * 100;
}

function VuMeter({ db }: { db: number | null | undefined }) {
  const pct = dbToPercent(db);
  const hot = db != null && db > -20;
  const live = db != null && db > -80;
  return (
    <div className="space-y-1">
      <div
        role="progressbar"
        aria-label="input level"
        aria-valuemin={-80}
        aria-valuemax={0}
        aria-valuenow={db ?? -80}
        data-testid="vu-meter"
        className="h-2 w-full overflow-hidden rounded-full bg-[var(--svx-color-surface-secondary)]"
      >
        <div
          className={`h-full transition-[width] duration-100 ${
            hot
              ? "bg-[var(--svx-color-status-amber)]"
              : live
                ? "bg-[var(--svx-color-status-green)]"
                : "bg-[var(--svx-color-text-tertiary)]"
          }`}
          style={{ width: `${pct}%` }}
        />
      </div>
      <div className="flex justify-between text-xs text-[var(--svx-color-text-tertiary)]">
        <span>−80 dB</span>
        <span className="font-mono">
          {db != null && Number.isFinite(db) ? `${db.toFixed(1)} dB` : "—"}
        </span>
        <span>0 dB</span>
      </div>
    </div>
  );
}

/* ── Status dot ── */

function StatusDot({ active }: { active: boolean }) {
  return (
    <span
      data-testid={active ? "status-active" : "status-inactive"}
      className={`inline-block size-2.5 rounded-full ${
        active
          ? "bg-[var(--svx-color-status-green)] shadow-[0_0_6px_var(--svx-color-status-green)]"
          : "bg-[var(--svx-color-text-tertiary)]"
      }`}
    />
  );
}

/* ── Info row ── */

function InfoRow({ label, value, mono }: { label: string; value: string; mono?: boolean }) {
  return (
    <div className="flex items-center justify-between py-1.5">
      <span className="text-sm text-[var(--svx-color-text-secondary)]">{label}</span>
      <span className={`text-sm font-medium ${mono ? "font-mono" : ""}`}>{value}</span>
    </div>
  );
}

/* ── Section card ── */

function Section({
  icon,
  title,
  children,
}: {
  icon: React.ReactNode;
  title: string;
  children: React.ReactNode;
}) {
  return (
    <div className="rounded-[var(--svx-radius-lg)] border border-[var(--svx-color-border)] bg-[var(--svx-color-surface-primary)] p-4">
      <div className="mb-3 flex items-center gap-2">
        <span className="text-[var(--svx-color-accent)]">{icon}</span>
        <h3 className="text-sm font-semibold uppercase tracking-wider text-[var(--svx-color-text-secondary)]">
          {title}
        </h3>
      </div>
      <div className="divide-y divide-[var(--svx-color-border)]">{children}</div>
    </div>
  );
}

/* ── Model matrix table ── */

function ModelMatrix({
  models,
  t,
}: {
  models: VoiceModels;
  t: (key: string, opts?: Record<string, unknown>) => string;
}) {
  const tiers = Object.keys(models.available_tiers);
  if (tiers.length === 0) return null;

  const fields: { key: keyof ModelSelection; label: string }[] = [
    { key: "stt_primary", label: t("models.sttPrimary") },
    { key: "stt_streaming", label: t("models.sttStreaming") },
    { key: "tts_primary", label: t("models.ttsPrimary") },
    { key: "tts_quality", label: t("models.ttsQuality") },
    { key: "wake", label: t("models.wake") },
    { key: "vad", label: t("models.vad") },
  ];

  return (
    <div className="rounded-[var(--svx-radius-lg)] border border-[var(--svx-color-border)] bg-[var(--svx-color-surface-primary)] p-4">
      <h3 className="mb-1 text-sm font-semibold uppercase tracking-wider text-[var(--svx-color-text-secondary)]">
        {t("models.title")}
      </h3>
      <p className="mb-3 text-xs text-[var(--svx-color-text-tertiary)]">{t("models.subtitle")}</p>
      <div className="overflow-x-auto">
        <table className="w-full text-left text-sm">
          <thead>
            <tr className="border-b border-[var(--svx-color-border)]">
              <th className="pb-2 pr-4 font-medium text-[var(--svx-color-text-secondary)]" />
              {tiers.map((tier) => (
                <th
                  key={tier}
                  className={`pb-2 pr-4 font-medium ${
                    tier === models.detected_tier
                      ? "text-[var(--svx-color-accent)]"
                      : "text-[var(--svx-color-text-secondary)]"
                  }`}
                >
                  {tier}
                  {tier === models.detected_tier && (
                    <span className="ml-1 text-xs">✦</span>
                  )}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {fields.map(({ key, label }) => (
              <tr key={key} className="border-b border-[var(--svx-color-border)] last:border-0">
                <td className="py-1.5 pr-4 text-[var(--svx-color-text-secondary)]">{label}</td>
                {tiers.map((tier) => (
                  <td key={tier} className="py-1.5 pr-4 font-mono text-xs">
                    {models.available_tiers[tier]?.[key] ?? "—"}
                  </td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}

/* ── Main page ── */

export default function VoicePage() {
  const { t } = useTranslation("voice");
  const [status, setStatus] = useState<VoiceStatus | null>(null);
  const [models, setModels] = useState<VoiceModels | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const fetchData = useCallback(async (signal?: AbortSignal) => {
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
      if (!isAbortError(err)) {
        setError(err instanceof Error ? err.message : String(err));
      }
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    const controller = new AbortController();
    void fetchData(controller.signal);
    return () => controller.abort();
  }, [fetchData]);

  // While the pipeline is running, poll /api/voice/status at ~2 Hz so
  // the VU-meter reflects live capture RMS. A fast fetch avoids the
  // complexity of a second WS endpoint (the existing /voice/test/input
  // stream rejects when the pipeline is active), and 500 ms is enough
  // resolution for a human reading a level bar.
  const captureRunning = status?.capture?.running ?? false;
  useEffect(() => {
    if (!captureRunning) return;
    const controller = new AbortController();
    let cancelled = false;
    const tick = async () => {
      try {
        const s = await api.get<VoiceStatus>("/api/voice/status", {
          signal: controller.signal,
        });
        if (!cancelled) setStatus(s);
      } catch (err) {
        if (isAbortError(err)) return;
        // Swallow transient errors — the main fetch will surface persistent ones.
      }
    };
    const id = setInterval(() => void tick(), 500);
    return () => {
      cancelled = true;
      controller.abort();
      clearInterval(id);
    };
  }, [captureRunning]);

  /* ── Loading state ── */
  if (loading && !status) {
    return (
      <div className="flex min-h-[300px] items-center justify-center gap-2 text-[var(--svx-color-text-secondary)]">
        <Loader2Icon className="size-5 animate-spin" />
        <span>{t("loading")}</span>
      </div>
    );
  }

  /* ── Error state ── */
  if (error && !status) {
    return (
      <div className="flex min-h-[300px] flex-col items-center justify-center gap-3 text-[var(--svx-color-text-secondary)]">
        <AlertTriangleIcon className="size-8 text-[var(--svx-color-status-amber)]" />
        <p>{t("error")}</p>
        <button
          onClick={() => void fetchData()}
          className="rounded-[var(--svx-radius-md)] border border-[var(--svx-color-border)] px-3 py-1.5 text-sm hover:bg-[var(--svx-color-surface-hover)]"
        >
          {t("retry")}
        </button>
      </div>
    );
  }

  if (!status) return null;

  const isConfigured = status.pipeline.state !== "not_configured";

  return (
    <div className="space-y-4">
      {/* Header */}
      <div className="flex items-start justify-between">
        <div>
          <h1 className="text-2xl font-bold">{t("title")}</h1>
          <p className="text-sm text-[var(--svx-color-text-secondary)]">{t("subtitle")}</p>
        </div>
        <button
          onClick={() => void fetchData()}
          disabled={loading}
          className="rounded-[var(--svx-radius-md)] border border-[var(--svx-color-border)] p-2 text-[var(--svx-color-text-secondary)] hover:bg-[var(--svx-color-surface-hover)] disabled:opacity-50"
          aria-label={t("retry")}
        >
          <RefreshCwIcon className={`size-4 ${loading ? "animate-spin" : ""}`} />
        </button>
      </div>

      {/* Not configured banner with setup wizard */}
      {!isConfigured && (
        <div className="rounded-[var(--svx-radius-lg)] border border-[var(--svx-color-border)] bg-[var(--svx-color-surface-secondary)] p-4">
          <p className="text-sm text-[var(--svx-color-text-secondary)]">{t("notConfigured")}</p>
          <div className="mt-3">
            <VoiceSetupModal />
          </div>
        </div>
      )}

      {/* Status grid */}
      <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
        {/* Pipeline */}
        <Section
          icon={<AudioWaveformIcon className="size-4" />}
          title={t("sections.pipeline")}
        >
          <div className="flex items-center justify-between py-1.5">
            <span className="text-sm text-[var(--svx-color-text-secondary)]">{t("pipeline.state")}</span>
            <span className="flex items-center gap-2 text-sm font-medium">
              <StatusDot active={status.pipeline.running} />
              {status.pipeline.running ? t("pipeline.running") : t("pipeline.stopped")}
            </span>
          </div>
          <InfoRow
            label={t("pipeline.latency")}
            value={
              status.pipeline.latency_ms != null
                ? t("pipeline.latencyMs", { ms: status.pipeline.latency_ms })
                : t("pipeline.noLatency")
            }
            mono
          />
        </Section>

        {/* Capture — live mic RMS so silent-mic failures surface
            immediately instead of being visible only in logs. */}
        {status.capture && (
          <Section icon={<MicIcon className="size-4" />} title={t("sections.capture")}>
            <div className="flex items-center justify-between py-1.5">
              <span className="text-sm text-[var(--svx-color-text-secondary)]">
                {t("pipeline.state")}
              </span>
              <span className="flex items-center gap-2 text-sm font-medium">
                <StatusDot active={status.capture.running} />
                {status.capture.running ? t("pipeline.running") : t("pipeline.stopped")}
              </span>
            </div>
            {status.capture.host_api && (
              <InfoRow label={t("capture.hostApi")} value={status.capture.host_api} mono />
            )}
            {status.capture.input_device != null && (
              <InfoRow
                label={t("capture.device")}
                value={String(status.capture.input_device)}
                mono
              />
            )}
            <InfoRow
              label={t("capture.frames")}
              value={String(status.capture.frames_delivered)}
              mono
            />
            <div className="pt-2">
              <VuMeter db={status.capture.last_rms_db} />
            </div>
          </Section>
        )}

        {/* STT */}
        <Section icon={<MicIcon className="size-4" />} title={t("sections.stt")}>
          {status.stt.engine ? (
            <>
              <InfoRow label={t("stt.engine")} value={status.stt.engine} />
              <InfoRow
                label={t("stt.model")}
                value={status.stt.model ?? "—"}
                mono
              />
              {status.stt.state && (
                <InfoRow label={t("stt.state")} value={status.stt.state} />
              )}
            </>
          ) : (
            <p className="py-2 text-sm text-[var(--svx-color-text-tertiary)]">{t("stt.none")}</p>
          )}
        </Section>

        {/* TTS */}
        <Section icon={<Volume2Icon className="size-4" />} title={t("sections.tts")}>
          {status.tts.engine ? (
            <>
              <InfoRow label={t("tts.engine")} value={status.tts.engine} />
              <InfoRow
                label={t("tts.model")}
                value={status.tts.model ?? "—"}
                mono
              />
              <InfoRow
                label={t("tts.initialized")}
                value={status.tts.initialized ? t("tts.yes") : t("tts.no")}
              />
            </>
          ) : (
            <p className="py-2 text-sm text-[var(--svx-color-text-tertiary)]">{t("tts.none")}</p>
          )}
        </Section>

        {/* VAD */}
        <Section icon={<RadioIcon className="size-4" />} title={t("sections.vad")}>
          <div className="flex items-center justify-between py-1.5">
            <span className="text-sm text-[var(--svx-color-text-secondary)]">{t("pipeline.state")}</span>
            <span className="flex items-center gap-2 text-sm font-medium">
              <StatusDot active={status.vad.enabled} />
              {status.vad.enabled ? t("vad.enabled") : t("vad.disabled")}
            </span>
          </div>
        </Section>

        {/* Wake Word */}
        <Section icon={<MicIcon className="size-4" />} title={t("sections.wakeWord")}>
          <div className="flex items-center justify-between py-1.5">
            <span className="text-sm text-[var(--svx-color-text-secondary)]">{t("pipeline.state")}</span>
            <span className="flex items-center gap-2 text-sm font-medium">
              <StatusDot active={status.wake_word.enabled} />
              {status.wake_word.enabled ? t("wakeWord.enabled") : t("wakeWord.disabled")}
            </span>
          </div>
          {status.wake_word.phrase && (
            <InfoRow label={t("wakeWord.phrase")} value={status.wake_word.phrase} mono />
          )}
        </Section>

        {/* Wyoming */}
        <Section icon={<WifiIcon className="size-4" />} title={t("sections.wyoming")}>
          <div className="flex items-center justify-between py-1.5">
            <span className="text-sm text-[var(--svx-color-text-secondary)]">{t("pipeline.state")}</span>
            <span className="flex items-center gap-2 text-sm font-medium">
              <StatusDot active={status.wyoming.connected} />
              {status.wyoming.connected ? t("wyoming.connected") : t("wyoming.disconnected")}
            </span>
          </div>
          {status.wyoming.endpoint && (
            <InfoRow label={t("wyoming.endpoint")} value={status.wyoming.endpoint} mono />
          )}
        </Section>
      </div>

      {/* Hardware tier */}
      {status.hardware.tier && (
        <Section icon={<CpuIcon className="size-4" />} title={t("sections.hardware")}>
          <InfoRow label={t("hardware.tier")} value={status.hardware.tier} />
          {status.hardware.ram_mb != null && (
            <InfoRow
              label={t("hardware.ram")}
              value={t("hardware.ramMb", { mb: status.hardware.ram_mb })}
              mono
            />
          )}
        </Section>
      )}

      {/* Model matrix */}
      {models && <ModelMatrix models={models} t={t} />}
    </div>
  );
}
