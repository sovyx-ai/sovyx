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
import { useVoiceStatusPoller } from "@/hooks/use-voice-status-poller";
import {
  VoiceModelsResponseSchema,
  VoiceStatusResponseSchema,
} from "@/types/schemas";
import { VoiceSetupModal } from "@/components/setup-wizard";
import { LinuxMicGainCard } from "@/components/voice/linux-mic-gain-card";
import { VoiceQualityPanel } from "@/components/voice/VoiceQualityPanel";
import { VoiceSetupWizard } from "@/components/setup-wizard/VoiceSetupWizard";
import { TrainingJobsPanel } from "@/components/training/TrainingJobsPanel";
import { TrainWakeWordModal } from "@/components/training/TrainWakeWordModal";
import { PerMindForgetCard } from "@/components/mind-management/PerMindForgetCard";
import { PerMindRetentionCard } from "@/components/mind-management/PerMindRetentionCard";
import { useDashboardStore } from "@/stores/dashboard";
import type { WakeWordPerMindStatus } from "@/types/api";
import { DegradedBannerPerPageMount } from "@/components/voice/DegradedBannerPerPageMount";

/* ── Types ── */

interface PipelineStatus {
  running: boolean;
  state: string;
  latency_ms: number | null;
}

// LIVE-2 Phase 3 (P0-1) — `health` distinguishes "actually usable" from the
// pre-existing registration/config flags. Optional because older daemons
// don't emit it; treated as "unknown" when absent (never assumed healthy).
interface STTStatus {
  engine: string | null;
  model: string | null;
  state: string | null;
  health?: string;
}

interface TTSStatus {
  engine: string | null;
  model: string | null;
  initialized: boolean;
  health?: string;
}

interface WakeWordStatus {
  enabled: boolean;
  phrase: string | null;
  health?: string;
}

interface VADStatus {
  enabled: boolean;
  health?: string;
}

interface WyomingStatus {
  // LIVE-2 P1-10 — True only when a Wyoming server is registered. The card
  // is hidden unless configured, so an unwired server never shows a
  // misleading "Disconnected". Optional: older daemons don't emit it.
  configured?: boolean;
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

/* ── Data-freshness honesty (LIVE-2 P1-7 / P1-8) ── */

/**
 * Classify how trustworthy the displayed status snapshot is, so the page
 * never looks "fresh" when it isn't. Four mutually-exclusive states,
 * highest-priority first:
 *
 * - ``fetch_failed`` — the last full fetch errored; we're showing the
 *   prior (stale) snapshot. Covers a failed manual refresh AND the
 *   audit's C-12 case (errors swallowed once a first snapshot landed).
 * - ``paused`` — capture is stopped, so the circuit-breaker poller is
 *   disabled (P1-7 / B-1): the snapshot is static and will NOT auto-update.
 * - ``poll_stale`` — capture is running but the latest poll(s) failed
 *   (P1-8 / B-3): we keep the last good data while retrying, so it's stale.
 * - ``live`` — capture running, polling succeeding: the data is fresh.
 *
 * Pure + exported so the precedence is unit-tested without timer flakiness.
 */
export type VoiceFreshness = "live" | "paused" | "poll_stale" | "fetch_failed";

export function computeVoiceFreshness(args: {
  fetchError: boolean;
  captureRunning: boolean;
  consecutive5xx: number;
}): VoiceFreshness {
  if (args.fetchError) return "fetch_failed";
  if (!args.captureRunning) return "paused";
  if (args.consecutive5xx > 0) return "poll_stale";
  return "live";
}

function FreshnessIndicator({
  freshness,
  t,
}: {
  freshness: VoiceFreshness;
  t: (key: string, opts?: Record<string, unknown>) => string;
}) {
  const tone: Record<VoiceFreshness, "ok" | "warn" | "danger"> = {
    live: "ok",
    paused: "warn",
    poll_stale: "warn",
    fetch_failed: "danger",
  };
  const toneClass = {
    ok: "text-[var(--svx-color-status-green)]",
    warn: "text-[var(--svx-color-status-amber)]",
    danger: "text-[var(--svx-color-status-red)]",
  }[tone[freshness]];
  const key = {
    live: "freshness.live",
    paused: "freshness.paused",
    poll_stale: "freshness.pollStale",
    fetch_failed: "freshness.fetchFailed",
  }[freshness];
  return (
    <span
      role="status"
      aria-live="polite"
      data-testid={`voice-freshness-${freshness}`}
      className={`inline-flex items-center gap-1.5 text-xs ${toneClass}`}
    >
      {freshness === "live" ? (
        <span className="inline-block size-2 rounded-full bg-[var(--svx-color-status-green)]" />
      ) : (
        <AlertTriangleIcon className="size-3.5" />
      )}
      {t(key)}
    </span>
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

/* ── Health badge (LIVE-2 Phase 3 / P0-1) ── */

const VOICE_HEALTH_TONE: Record<string, "ok" | "warn" | "danger" | "neutral"> = {
  healthy: "ok",
  degraded: "warn",
  failed: "danger",
  unknown: "warn",
  unavailable: "neutral",
};

/**
 * Render a subsystem's real health, distinct from its registration/config
 * state. ``showHealthy`` controls whether a green "Healthy" pill renders:
 * STT/TTS surface it always (they have no status dot); VAD/Wake pass
 * ``false`` because their dot already conveys the healthy case and only the
 * problem states (degraded/failed/unknown) need a word.
 *
 * An absent/unrecognised value renders as the neutral "unknown" tone —
 * never as healthy. This is the anti-presence-only-lie guarantee on the
 * frontend side: nothing here can paint a registered-but-broken engine green.
 */
function HealthBadge({
  health,
  t,
  showHealthy = false,
}: {
  health: string | undefined;
  t: (key: string, opts?: Record<string, unknown>) => string;
  showHealthy?: boolean;
}) {
  if (!health) return null;
  if (!showHealthy && (health === "healthy" || health === "unavailable")) {
    return null;
  }
  const tone = VOICE_HEALTH_TONE[health] ?? "neutral";
  const toneClass = {
    ok: "bg-[var(--svx-color-status-green)]/15 text-[var(--svx-color-status-green)]",
    warn: "bg-[var(--svx-color-status-amber)]/15 text-[var(--svx-color-status-amber)]",
    danger: "bg-[var(--svx-color-status-red)]/15 text-[var(--svx-color-status-red)]",
    neutral:
      "bg-[var(--svx-color-surface-secondary)] text-[var(--svx-color-text-secondary)]",
  }[tone];
  return (
    <span
      data-testid={`health-${health}`}
      className={`inline-flex items-center rounded-full px-2 py-0.5 text-xs font-medium ${toneClass}`}
    >
      {t(`subsystemHealth.${health}`, { defaultValue: health })}
    </span>
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
  // Mission v0.30.0 §T1.4 (D3): broken-state minds get a "Train this
  // wake word" button. Local state drives modal open/close. On
  // successful start, subscribe to the stream so the panel reflects
  // live progress.
  const [trainModalOpen, setTrainModalOpen] = useState(false);
  const subscribeToTrainingJob = useDashboardStore((s) => s.subscribeToTrainingJob);

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
  const showTrainButton =
    entry.resolution_strategy === "none" && entry.wake_word_enabled;

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

      {/* PHONETIC match disclosure (Mission MISSION-v0.29.1-tightening §T1).
          Only render when the resolver took the phonetic path —
          EXACT case is redundant with the file name. Operator sees
          "Matched as <file>.onnx (distance: N)" so a future
          wake_word edit doesn't silently drift to a different match. */}
      {entry.resolution_strategy === "phonetic" &&
        entry.matched_name !== null &&
        entry.phoneme_distance !== null && (
          <div className="mt-1.5 text-xs text-[var(--svx-color-text-tertiary)]">
            {t("perMindWakeWord.phoneticMatch", {
              file: `${entry.matched_name}.onnx`,
              distance: entry.phoneme_distance,
            })}
          </div>
        )}

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

      {/* Mission v0.30.0 §T1.4 (D3): Train Wake Word button — visible
          ONLY when the mind is in the broken-state path (configured
          but no model resolved). Click opens the training modal;
          on Start, the page subscribes to the live progress stream. */}
      {showTrainButton && (
        <div className="mt-2 flex justify-end">
          <button
            type="button"
            onClick={() => setTrainModalOpen(true)}
            className="rounded-[var(--svx-radius-md)] border border-[var(--svx-color-accent)] bg-[var(--svx-color-accent-soft)] px-3 py-1 text-xs font-medium text-[var(--svx-color-accent)] hover:bg-[var(--svx-color-accent)] hover:text-white"
          >
            {t("training.button")}
          </button>
        </div>
      )}

      <TrainWakeWordModal
        entry={entry}
        open={trainModalOpen}
        onClose={() => setTrainModalOpen(false)}
        onStarted={(jobId) => {
          subscribeToTrainingJob(jobId);
        }}
      />
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
  // T1.5 of v0.30.0 mission: training-panel visibility flag — needed
  // hoisted to component-top because hooks can't be called inside JSX.
  const hasActiveTraining = useDashboardStore((s) => s.currentTrainingJob !== null);
  // T2.1 of v0.30.0 mission: setup wizard collapsible state.
  const [wizardOpen, setWizardOpen] = useState(false);

  const fetchData = useCallback(async (signal?: AbortSignal) => {
    setLoading(true);
    setError(null);
    try {
      const [s, m] = await Promise.all([
        api.get<VoiceStatus>("/api/voice/status", {
          signal,
          schema: VoiceStatusResponseSchema,
        }),
        api.get<VoiceModels>("/api/voice/models", {
          signal,
          schema: VoiceModelsResponseSchema,
        }),
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

  // Mission C2 §T2.3 — circuit-breaker poller replaces the
  // pre-mission ``setInterval(... 500)`` block that hammered the
  // backend 960× over 480 s with NO backoff when /api/voice/status
  // 500'd every poll (operator forensic log v0.43.1 §C2 + §H8).
  // The hook returns the latest status + an ``error: "degraded"``
  // banner after 11 consecutive 5xx; a single 2xx resets to baseline.
  const captureRunning = status?.capture?.running ?? false;
  const poller = useVoiceStatusPoller({ enabled: captureRunning });
  useEffect(() => {
    if (poller.status !== null) {
      setStatus(poller.status as unknown as VoiceStatus);
      // LIVE-2 P1-8 — a successful poll supersedes any prior fetch error,
      // so the freshness indicator returns to "live" instead of being
      // pinned at "fetch_failed" by a stale manual-refresh failure.
      setError(null);
    }
  }, [poller.status]);

  // LIVE-2 P1-7 / P1-8 — honest freshness of the displayed snapshot.
  const freshness = computeVoiceFreshness({
    fetchError: error !== null,
    captureRunning,
    consecutive5xx: poller.consecutive5xx,
  });

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
      {/* Mission C4 §T1.11 — per-page composite degraded banner.
          Registers as mounted via DegradedBannerMountedContext so the
          app-shell global mount yields. */}
      <DegradedBannerPerPageMount />
      {/* Header */}
      <div className="flex items-start justify-between">
        <div>
          <h1 className="text-2xl font-bold">{t("title")}</h1>
          <p className="text-sm text-[var(--svx-color-text-secondary)]">{t("subtitle")}</p>
          {/* LIVE-2 P1-7 / P1-8 — honest freshness affordance: never let
              the page look fresh when it's a stale snapshot, a failed
              fetch, or paused because capture is stopped. */}
          <div className="mt-1.5">
            <FreshnessIndicator freshness={freshness} t={t} />
          </div>
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

      {/* Mission C2 §T2.3 — circuit-breaker degraded banner. Surfaces
          when the /api/voice/status poller hits 11 consecutive 5xx.
          The poller continues at a 10 s cadence so recovery still
          self-detects without operator action. */}
      {poller.error === "degraded" && (
        <div
          role="alert"
          aria-live="polite"
          className="rounded-[var(--svx-radius-lg)] border border-[var(--svx-color-status-amber)] bg-[var(--svx-color-surface-secondary)] p-4"
        >
          <div className="flex items-start gap-3">
            <AlertTriangleIcon className="mt-0.5 size-5 text-[var(--svx-color-status-amber)]" />
            <div className="flex-1">
              <p className="text-sm font-medium">{t("poller.degraded.title")}</p>
              <p className="mt-1 text-xs text-[var(--svx-color-text-secondary)]">
                {t("poller.degraded.detail", { count: poller.consecutive5xx })}
              </p>
            </div>
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
          {/* LIVE-2 P1-3 — show real per-turn latency once measured; when
              null, explain WHY (no utterance processed yet) rather than a
              bare "—" that reads like "0 / instant". */}
          <InfoRow
            label={t("pipeline.latency")}
            value={
              status.pipeline.latency_ms != null
                ? t("pipeline.latencyMs", { ms: status.pipeline.latency_ms })
                : t("pipeline.latencyPending")
            }
            mono={status.pipeline.latency_ms != null}
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
              {/* LIVE-2 P0-1 — real health, not mere registration. */}
              <div className="flex items-center justify-between py-1.5">
                <span className="text-sm text-[var(--svx-color-text-secondary)]">
                  {t("subsystemHealth.label")}
                </span>
                <HealthBadge health={status.stt.health} t={t} showHealthy />
              </div>
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
              {/* LIVE-2 P0-1 — real health, not mere registration. */}
              <div className="flex items-center justify-between py-1.5">
                <span className="text-sm text-[var(--svx-color-text-secondary)]">
                  {t("subsystemHealth.label")}
                </span>
                <HealthBadge health={status.tts.health} t={t} showHealthy />
              </div>
            </>
          ) : (
            <p className="py-2 text-sm text-[var(--svx-color-text-tertiary)]">{t("tts.none")}</p>
          )}
        </Section>

        {/* VAD — LIVE-2 P0-1: the dot reflects real health, not mere
            registration. "Enabled/disabled" still reports configuration;
            the badge surfaces degraded/failed/unknown so a registered-but-
            broken VAD never reads as a healthy green dot. */}
        <Section icon={<RadioIcon className="size-4" />} title={t("sections.vad")}>
          <div className="flex items-center justify-between py-1.5">
            <span className="text-sm text-[var(--svx-color-text-secondary)]">{t("pipeline.state")}</span>
            <span className="flex items-center gap-2 text-sm font-medium">
              <StatusDot active={status.vad.health === "healthy"} />
              {status.vad.enabled ? t("vad.enabled") : t("vad.disabled")}
              <HealthBadge health={status.vad.health} t={t} />
            </span>
          </div>
        </Section>

        {/* Wake Word — LIVE-2 P0-1: dot reflects real health (see VAD). */}
        <Section icon={<MicIcon className="size-4" />} title={t("sections.wakeWord")}>
          <div className="flex items-center justify-between py-1.5">
            <span className="text-sm text-[var(--svx-color-text-secondary)]">{t("pipeline.state")}</span>
            <span className="flex items-center gap-2 text-sm font-medium">
              <StatusDot active={status.wake_word.health === "healthy"} />
              {status.wake_word.enabled ? t("wakeWord.enabled") : t("wakeWord.disabled")}
              <HealthBadge health={status.wake_word.health} t={t} />
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

        {/* Per-mind management — Mission v0.30.2 §T2.3 (D3). Two cards
            per mind, both consume the mindManagement Zustand slice:
            * PerMindForgetCard — destructive right-to-erasure
              (typed-confirm UX matching backend's defense-in-depth at
              routes/mind.py:173).
            * PerMindRetentionCard — preview-then-apply scheduled
              prune (no confirm; only removes AGED records).
            Section visible only when at least one mind exists in the
            wake-word per-mind status list (proxy for "minds onboarded
            on this daemon"). Empty state otherwise. */}
        {perMindStatus.length > 0 && (
          <Section
            icon={<MicIcon className="size-4" />}
            title={t("mind.forget.title")}
          >
            <div className="flex flex-col gap-2">
              {perMindStatus.map((entry) => (
                <div key={entry.mind_id} className="flex flex-col gap-2">
                  <PerMindForgetCard mindId={entry.mind_id} />
                  <PerMindRetentionCard mindId={entry.mind_id} />
                </div>
              ))}
            </div>
          </Section>
        )}

        {/* Training Jobs Panel — Mission v0.30.0 §T1.5. Renders only
            when an active training subscription exists; pure observer
            of currentTrainingJob from the slice. */}
        <Section
          icon={<MicIcon className="size-4" />}
          title={t("training.panel.title")}
        >
          <TrainingJobsPanel />
          {/* Empty-state placeholder — no active training */}
          {!hasActiveTraining && (
            <div className="py-2 text-sm text-[var(--svx-color-text-tertiary)]">
              {t("training.panel.empty")}
            </div>
          )}
        </Section>

        {/* Voice Setup Wizard — Mission v0.30.0 §T2 (Phase 7 / T7.25-T7.30).
            Collapsible: operator clicks "Open Setup Wizard" → 5-step
            mic selection + test recording + APO diagnostic flow. */}
        <Section
          icon={<MicIcon className="size-4" />}
          title={t("wizard.title")}
        >
          {wizardOpen ? (
            <VoiceSetupWizard
              onComplete={() => {
                // v0.31.4 GAP 6 closure: wizard's handleSave (post-
                // GAP 1) calls /api/voice/enable; onComplete fires
                // only on 200. Trigger an immediate page refresh of
                // voice status so the "not configured" banner
                // disappears + the live pipeline cards populate
                // without the operator having to refresh.
                setWizardOpen(false);
                void fetchData();
              }}
              onCancel={() => setWizardOpen(false)}
            />
          ) : (
            <div className="flex items-center justify-between py-1.5">
              <span className="text-sm text-[var(--svx-color-text-secondary)]">
                {t("wizard.openHint")}
              </span>
              <button
                type="button"
                onClick={() => setWizardOpen(true)}
                className="rounded-[var(--svx-radius-md)] border border-[var(--svx-color-accent)] bg-[var(--svx-color-accent-soft)] px-3 py-1 text-xs font-medium text-[var(--svx-color-accent)] hover:bg-[var(--svx-color-accent)] hover:text-white"
              >
                {t("wizard.openButton")}
              </button>
            </div>
          )}
        </Section>

        {/* Wyoming — LIVE-2 P1-10: rendered ONLY when a Wyoming server is
            registered (``configured``). The default daemon never wires
            one, so this card stays hidden rather than showing a
            permanently-"Disconnected" state that implies a recoverable
            connection the operator has no way to establish. */}
        {status.wyoming.configured && (
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
        )}
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
