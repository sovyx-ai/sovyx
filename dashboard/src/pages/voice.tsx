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
import { LinuxMicGainCard } from "@/components/voice/linux-mic-gain-card";
import { VoiceQualityPanel } from "@/components/voice/VoiceQualityPanel";
import { useDashboardStore } from "@/stores/dashboard";
import type { WakeWordPerMindStatus } from "@/types/api";

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
  // v1.3 §4.6 L6 — boot preflight warnings forwarded from the backend
  // registry-resolved store. Optional because older daemons (pre-v1.3)
  // never emit the field.
  preflight_warnings?: import("@/types/api").PreflightWarning[];
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

/* ── Per-mind wake-word card (Mission MISSION-wake-word-ui §T4) ── */

function PerMindWakeWordCard({
  entry,
  onToggle,
  t,
}: {
  entry: WakeWordPerMindStatus;
  onToggle: (mindId: string, enabled: boolean) => void;
  t: (key: string, opts?: Record<string, unknown>) => string;
}) {
  // Three pill states (D3): registered (green), not-registered (yellow),
  // error (red). Stale-config + NONE-strategy minds get the red error
  // pill; configured-but-cold-start minds get yellow.
  let pillKey: "registered" | "notRegistered" | "error";
  let pillTone: "ok" | "warn" | "danger";
  if (entry.resolution_strategy === "none" && entry.wake_word_enabled) {
    pillKey = "error";
    pillTone = "danger";
  } else if (entry.runtime_registered) {
    pillKey = "registered";
    pillTone = "ok";
  } else {
    pillKey = "notRegistered";
    pillTone = "warn";
  }

  const pillBgClass = {
    ok: "bg-[var(--svx-color-success-soft)] text-[var(--svx-color-success)]",
    warn: "bg-[var(--svx-color-warning-soft)] text-[var(--svx-color-warning)]",
    danger: "bg-[var(--svx-color-danger-soft)] text-[var(--svx-color-danger)]",
  }[pillTone];

  return (
    <div className="rounded-[var(--svx-radius-md)] border border-[var(--svx-color-border)] bg-[var(--svx-color-surface-secondary)] p-3">
      <div className="flex items-center justify-between gap-3">
        <div className="min-w-0 flex-1">
          <div className="text-sm font-semibold text-[var(--svx-color-text-primary)]">
            {entry.mind_id}
          </div>
          <div className="font-mono text-xs text-[var(--svx-color-text-tertiary)]">
            {entry.wake_word}
            {entry.voice_language ? ` · ${entry.voice_language}` : ""}
          </div>
        </div>

        <span className={`rounded-full px-2 py-0.5 text-xs font-medium ${pillBgClass}`}>
          {t(`perMindWakeWord.${pillKey}`)}
        </span>

        <label className="inline-flex cursor-pointer items-center gap-2">
          <span className="sr-only">{t("perMindWakeWord.toggleLabel")}</span>
          <input
            type="checkbox"
            role="switch"
            aria-label={t("perMindWakeWord.toggleLabel")}
            checked={entry.wake_word_enabled}
            onChange={(e) => onToggle(entry.mind_id, e.target.checked)}
            className="h-4 w-7 cursor-pointer appearance-none rounded-full bg-[var(--svx-color-surface-tertiary)] transition-colors checked:bg-[var(--svx-color-accent)] relative after:absolute after:left-0.5 after:top-0.5 after:h-3 after:w-3 after:rounded-full after:bg-white after:transition-transform checked:after:translate-x-3"
          />
        </label>
      </div>

      {entry.last_error !== null && (
        <details className="mt-2">
          <summary className="cursor-pointer text-xs text-[var(--svx-color-danger)]">
            {t("perMindWakeWord.viewDetails")}
          </summary>
          <pre className="mt-1.5 whitespace-pre-wrap break-words rounded bg-[var(--svx-color-surface-tertiary)] p-2 font-mono text-xs text-[var(--svx-color-text-secondary)]">
            {entry.last_error}
          </pre>
        </details>
      )}
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

  // Per-mind wake-word state (Mission MISSION-wake-word-ui §T3+T4).
  // Selectors are individual to keep referential stability (avoid re-
  // render cascade when other slices' fields change).
  const perMindStatus = useDashboardStore((s) => s.perMindStatus);
  const wakeWordError = useDashboardStore((s) => s.wakeWordError);
  const fetchPerMindStatus = useDashboardStore((s) => s.fetchPerMindStatus);
  const toggleMind = useDashboardStore((s) => s.toggleMind);
  const clearWakeWordError = useDashboardStore((s) => s.clearWakeWordError);

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

  // Mission MISSION-wake-word-ui §T4 — fetch per-mind wake-word status
  // on mount. The slice handles loading/error state internally; the
  // page just kicks off the fetch and consumes the result via the
  // selectors above.
  useEffect(() => {
    void fetchPerMindStatus();
  }, [fetchPerMindStatus]);

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

      {/*
        v1.3 §4.3 L5a — LinuxMicGainCard surface alignment.
        The card self-hides on non-Linux hosts and when the mixer is
        within a safe range, so it is a zero-visual-cost addition for
        the common case. For the saturated case — the dossier-captured
        v0.21.2 incident — it surfaces the warning on the Voice page
        where users actually look when speech fails, rather than only
        under Settings where the banner historically lived.
      */}
      <LinuxMicGainCard />

      {/* Phase 4 / T4.26 + T4.37 — voice quality observables. SNR
          distribution + MOS proxy + noise-floor drift + AGC2
          controller state. Polls /api/voice/quality-snapshot every
          5 s; falls back gracefully when the engine registry isn't
          ready (cold boot before voice enable). */}
      <VoiceQualityPanel />

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

        {/* Per-mind wake word — Mission MISSION-wake-word-ui §T4 ── */}
        <Section
          icon={<MicIcon className="size-4" />}
          title={t("perMindWakeWord.title")}
        >
          {wakeWordError !== null && (
            <div
              role="alert"
              className="mb-2 flex items-start justify-between gap-2 rounded border border-[var(--svx-color-danger)] bg-[var(--svx-color-danger-soft)] p-2 text-xs text-[var(--svx-color-danger)]"
            >
              <span className="break-words">{wakeWordError}</span>
              <button
                type="button"
                onClick={clearWakeWordError}
                className="shrink-0 underline"
              >
                {t("perMindWakeWord.dismiss")}
              </button>
            </div>
          )}
          {perMindStatus.length === 0 ? (
            <div className="py-2 text-sm text-[var(--svx-color-text-tertiary)]">
              {t("perMindWakeWord.empty")}
            </div>
          ) : (
            <div className="flex flex-col gap-2">
              {perMindStatus.map((entry) => (
                <PerMindWakeWordCard
                  key={entry.mind_id}
                  entry={entry}
                  onToggle={(mindId, enabled) => void toggleMind(mindId, enabled)}
                  t={t}
                />
              ))}
            </div>
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
