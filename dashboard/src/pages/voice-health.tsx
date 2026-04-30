/**
 * Voice Health page — L7 operator surface (ADR §4.7).
 *
 * Renders two panels backed by ``GET /api/voice/health`` and three
 * mutations (``/reprobe``, ``/forget``, ``/pin``):
 *
 *   1. Known combos — one row per validated endpoint, per-row actions.
 *   2. Pinned overrides — user-pinned combos that survive ``--reset``.
 *
 * The backend is stateless on these endpoints (reads + writes go
 * straight to ``ComboStore`` / ``CaptureOverrides`` JSON), so after
 * every mutation the store refetches the snapshot — no optimistic
 * splicing.
 *
 * Warm re-probe requires the voice pipeline to be running; the button
 * is disabled with a tooltip when ``voice_enabled === false``.
 */

import { useEffect, useMemo, useState } from "react";
import { useTranslation } from "react-i18next";
import {
  AudioWaveformIcon,
  RefreshCwIcon,
  Loader2Icon,
  AlertTriangleIcon,
  PinIcon,
  TrashIcon,
  FlameIcon,
  ThermometerSnowflakeIcon,
  ShieldAlertIcon,
  CheckCircle2Icon,
  XCircleIcon,
  DatabaseIcon,
  ChevronDownIcon,
  ChevronRightIcon,
} from "lucide-react";
import { useDashboardStore } from "@/stores/dashboard";
import type {
  MixerKbProfileDetail,
  MixerKbProfileSummary,
  MixerKbValidateResponse,
  VoiceHealthCombo,
  VoiceHealthComboEntry,
  VoiceHealthOverrideEntry,
  VoiceHealthProbeResult,
  VoiceHealthRemediationSeverity,
} from "@/types/api";

/* ── Helpers ── */

/**
 * Render the compact combo string used throughout the page —
 * ``WASAPI 48000Hz 1ch float32 (excl)``.
 */
function formatCombo(combo: VoiceHealthCombo): string {
  const parts = [
    combo.host_api,
    `${combo.sample_rate}Hz`,
    `${combo.channels}ch`,
    combo.sample_format,
  ];
  const flags: string[] = [];
  if (combo.exclusive) flags.push("excl");
  if (combo.auto_convert) flags.push("conv");
  if (flags.length > 0) parts.push(`(${flags.join(",")})`);
  return parts.join(" ");
}

function formatIsoTimestamp(iso: string | null | undefined): string {
  if (!iso) return "—";
  try {
    const d = new Date(iso);
    if (Number.isNaN(d.getTime())) return iso;
    return d.toLocaleString();
  } catch {
    return iso;
  }
}

function formatRms(db: number | null | undefined): string {
  if (db == null || !Number.isFinite(db)) return "—";
  return `${db.toFixed(1)} dBFS`;
}

function formatProb(p: number | null | undefined): string {
  if (p == null || !Number.isFinite(p)) return "—";
  return p.toFixed(3);
}

/* ── Diagnosis pill ── */

/** Map a diagnosis token to a colored pill. Unknown values fall back to "neutral". */
function diagnosisTone(
  diagnosis: string,
): "ok" | "warn" | "error" | "neutral" {
  switch (diagnosis) {
    case "healthy":
      return "ok";
    case "low_signal":
    case "vad_insensitive":
    case "format_mismatch":
    case "apo_degraded":
      return "warn";
    case "muted":
    case "no_signal":
    case "driver_error":
    case "device_busy":
    case "permission_denied":
      return "error";
    default:
      return "neutral";
  }
}

function DiagnosisBadge({ diagnosis }: { diagnosis: string }) {
  const { t } = useTranslation("voice");
  const tone = diagnosisTone(diagnosis);
  const label = t(`health.diagnosis.${diagnosis}`, diagnosis);
  const palette: Record<typeof tone, string> = {
    ok: "bg-[var(--svx-color-status-green)]/15 text-[var(--svx-color-status-green)] border-[var(--svx-color-status-green)]/30",
    warn: "bg-[var(--svx-color-status-amber)]/15 text-[var(--svx-color-status-amber)] border-[var(--svx-color-status-amber)]/30",
    error:
      "bg-[var(--svx-color-status-red)]/15 text-[var(--svx-color-status-red)] border-[var(--svx-color-status-red)]/30",
    neutral:
      "bg-[var(--svx-color-surface-secondary)] text-[var(--svx-color-text-secondary)] border-[var(--svx-color-border)]",
  };
  return (
    <span
      data-testid={`diagnosis-${diagnosis}`}
      className={`inline-flex items-center gap-1 rounded-full border px-2 py-0.5 font-mono text-[11px] ${palette[tone]}`}
    >
      {label}
    </span>
  );
}

/* ── Remediation callout ── */

function RemediationCallout({
  severity,
  code,
  cliAction,
}: {
  severity: VoiceHealthRemediationSeverity;
  code: string;
  cliAction: string | null;
}) {
  const palette: Record<VoiceHealthRemediationSeverity, string> = {
    info: "border-[var(--svx-color-border)] text-[var(--svx-color-text-secondary)]",
    warn: "border-[var(--svx-color-status-amber)]/40 text-[var(--svx-color-status-amber)]",
    error:
      "border-[var(--svx-color-status-red)]/40 text-[var(--svx-color-status-red)]",
  };
  const Icon =
    severity === "error"
      ? XCircleIcon
      : severity === "warn"
        ? ShieldAlertIcon
        : CheckCircle2Icon;
  return (
    <div
      className={`flex items-start gap-2 rounded-[var(--svx-radius-md)] border bg-[var(--svx-color-surface-secondary)] px-3 py-2 text-xs ${palette[severity]}`}
    >
      <Icon className="mt-0.5 size-3.5 shrink-0" />
      <div className="space-y-0.5">
        <div className="font-mono">{code}</div>
        {cliAction && (
          <code className="block font-mono text-[11px] opacity-80">
            {cliAction}
          </code>
        )}
      </div>
    </div>
  );
}

/* ── Latest-probe inline card ── */

function LatestProbeCard({ result }: { result: VoiceHealthProbeResult }) {
  const { t } = useTranslation("voice");
  return (
    <div className="mt-3 space-y-2 rounded-[var(--svx-radius-md)] border border-[var(--svx-color-border)] bg-[var(--svx-color-surface-secondary)] p-3">
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-2 text-xs uppercase tracking-wider text-[var(--svx-color-text-secondary)]">
          <AudioWaveformIcon className="size-3.5" />
          <span>{t("health.result.title")}</span>
        </div>
        <DiagnosisBadge diagnosis={result.diagnosis} />
      </div>
      <dl className="grid grid-cols-2 gap-x-4 gap-y-1 text-xs sm:grid-cols-3">
        <div>
          <dt className="text-[var(--svx-color-text-tertiary)]">
            {t("health.result.mode")}
          </dt>
          <dd className="font-mono">{result.mode}</dd>
        </div>
        <div>
          <dt className="text-[var(--svx-color-text-tertiary)]">
            {t("health.result.rms")}
          </dt>
          <dd className="font-mono">{formatRms(result.rms_db)}</dd>
        </div>
        <div>
          <dt className="text-[var(--svx-color-text-tertiary)]">
            {t("health.result.vadMax")}
          </dt>
          <dd className="font-mono">{formatProb(result.vad_max_prob)}</dd>
        </div>
        <div>
          <dt className="text-[var(--svx-color-text-tertiary)]">
            {t("health.result.vadMean")}
          </dt>
          <dd className="font-mono">{formatProb(result.vad_mean_prob)}</dd>
        </div>
        <div>
          <dt className="text-[var(--svx-color-text-tertiary)]">
            {t("health.result.callbacks")}
          </dt>
          <dd className="font-mono">{result.callbacks_fired}</dd>
        </div>
        <div>
          <dt className="text-[var(--svx-color-text-tertiary)]">
            {t("health.result.duration")}
          </dt>
          <dd className="font-mono">{result.duration_ms} ms</dd>
        </div>
      </dl>
      {result.error && (
        <div className="rounded-[var(--svx-radius-sm)] border border-[var(--svx-color-status-red)]/40 bg-[var(--svx-color-status-red)]/10 px-2 py-1 font-mono text-[11px] text-[var(--svx-color-status-red)]">
          <span className="mr-2 uppercase tracking-wider">
            {t("health.result.error")}
          </span>
          {result.error}
        </div>
      )}
      {result.remediation && (
        <RemediationCallout
          severity={result.remediation.severity}
          code={result.remediation.code}
          cliAction={result.remediation.cli_action}
        />
      )}
    </div>
  );
}

/* ── Combo row ── */

function ComboRow({
  entry,
  voiceEnabled,
  busy,
  latestProbe,
  onReprobeCold,
  onReprobeWarm,
  onForget,
  onPin,
}: {
  entry: VoiceHealthComboEntry;
  voiceEnabled: boolean;
  busy: boolean;
  latestProbe: VoiceHealthProbeResult | undefined;
  onReprobeCold: () => void;
  onReprobeWarm: () => void;
  onForget: () => void;
  onPin: () => void;
}) {
  const { t } = useTranslation("voice");
  return (
    <article
      data-testid={`combo-row-${entry.endpoint_guid}`}
      className="rounded-[var(--svx-radius-lg)] border border-[var(--svx-color-border)] bg-[var(--svx-color-surface-primary)] p-4"
    >
      <header className="mb-3 flex flex-wrap items-start justify-between gap-3">
        <div className="min-w-0 space-y-1">
          <h3 className="flex items-center gap-2 text-sm font-semibold text-[var(--svx-color-text-primary)]">
            <span className="truncate">{entry.device_friendly_name}</span>
            {entry.pinned && (
              <span
                className="inline-flex items-center gap-1 rounded-full bg-[var(--svx-color-brand-primary)]/15 px-2 py-0.5 text-[10px] text-[var(--svx-color-brand-primary)]"
                aria-label={t("health.combo.pinned")}
              >
                <PinIcon className="size-3" />
                {t("health.combo.pinned")}
              </span>
            )}
            {entry.needs_revalidation && (
              <span className="inline-flex items-center gap-1 rounded-full bg-[var(--svx-color-status-amber)]/15 px-2 py-0.5 text-[10px] text-[var(--svx-color-status-amber)]">
                <AlertTriangleIcon className="size-3" />
                {t("health.combo.needsRevalidation")}
              </span>
            )}
          </h3>
          <p className="truncate font-mono text-[11px] text-[var(--svx-color-text-tertiary)]">
            {entry.endpoint_guid}
          </p>
        </div>
        <DiagnosisBadge diagnosis={entry.last_boot_diagnosis} />
      </header>

      <dl className="grid grid-cols-1 gap-x-4 gap-y-1 text-xs sm:grid-cols-2 lg:grid-cols-3">
        <div>
          <dt className="text-[var(--svx-color-text-tertiary)]">
            {t("health.combo.winningCombo")}
          </dt>
          <dd className="font-mono">{formatCombo(entry.winning_combo)}</dd>
        </div>
        <div>
          <dt className="text-[var(--svx-color-text-tertiary)]">
            {t("health.combo.validatedAt")}
          </dt>
          <dd className="font-mono">{formatIsoTimestamp(entry.validated_at)}</dd>
        </div>
        <div>
          <dt className="text-[var(--svx-color-text-tertiary)]">
            {t("health.combo.boots")}
          </dt>
          <dd className="font-mono">{entry.boots_validated}</dd>
        </div>
        <div>
          <dt className="text-[var(--svx-color-text-tertiary)]">
            {t("health.combo.lastDiagnosis")}
          </dt>
          <dd className="font-mono">{entry.last_boot_diagnosis}</dd>
        </div>
      </dl>

      {latestProbe && <LatestProbeCard result={latestProbe} />}

      <footer className="mt-3 flex flex-wrap gap-2">
        <button
          type="button"
          onClick={onReprobeCold}
          disabled={busy}
          className="inline-flex items-center gap-1.5 rounded-[var(--svx-radius-md)] border border-[var(--svx-color-border)] px-3 py-1.5 text-xs text-[var(--svx-color-text-secondary)] hover:bg-[var(--svx-color-surface-hover)] disabled:cursor-not-allowed disabled:opacity-50"
          data-testid={`btn-reprobe-cold-${entry.endpoint_guid}`}
        >
          <ThermometerSnowflakeIcon className="size-3.5" />
          {t("health.actions.reprobeCold")}
        </button>
        <button
          type="button"
          onClick={onReprobeWarm}
          disabled={busy || !voiceEnabled}
          title={!voiceEnabled ? t("health.warmUnavailable") : undefined}
          className="inline-flex items-center gap-1.5 rounded-[var(--svx-radius-md)] border border-[var(--svx-color-border)] px-3 py-1.5 text-xs text-[var(--svx-color-text-secondary)] hover:bg-[var(--svx-color-surface-hover)] disabled:cursor-not-allowed disabled:opacity-50"
          data-testid={`btn-reprobe-warm-${entry.endpoint_guid}`}
        >
          <FlameIcon className="size-3.5" />
          {t("health.actions.reprobeWarm")}
        </button>
        {!entry.pinned && (
          <button
            type="button"
            onClick={onPin}
            disabled={busy}
            className="inline-flex items-center gap-1.5 rounded-[var(--svx-radius-md)] border border-[var(--svx-color-border)] px-3 py-1.5 text-xs text-[var(--svx-color-text-secondary)] hover:bg-[var(--svx-color-surface-hover)] disabled:cursor-not-allowed disabled:opacity-50"
            data-testid={`btn-pin-${entry.endpoint_guid}`}
          >
            <PinIcon className="size-3.5" />
            {t("health.actions.pin")}
          </button>
        )}
        <button
          type="button"
          onClick={onForget}
          disabled={busy}
          className="inline-flex items-center gap-1.5 rounded-[var(--svx-radius-md)] border border-[var(--svx-color-status-red)]/40 px-3 py-1.5 text-xs text-[var(--svx-color-status-red)] hover:bg-[var(--svx-color-status-red)]/10 disabled:cursor-not-allowed disabled:opacity-50"
          data-testid={`btn-forget-${entry.endpoint_guid}`}
        >
          <TrashIcon className="size-3.5" />
          {t("health.actions.forget")}
        </button>
        {busy && (
          <span className="inline-flex items-center gap-1 text-xs text-[var(--svx-color-text-tertiary)]">
            <Loader2Icon className="size-3.5 animate-spin" />
          </span>
        )}
      </footer>
    </article>
  );
}

/* ── Override row ── */

function OverrideRow({ override }: { override: VoiceHealthOverrideEntry }) {
  const { t } = useTranslation("voice");
  const sourceLabel = t(
    `health.overrides.sources.${override.pinned_by}`,
    override.pinned_by,
  );
  return (
    <article
      data-testid={`override-row-${override.endpoint_guid}`}
      className="rounded-[var(--svx-radius-lg)] border border-[var(--svx-color-border)] bg-[var(--svx-color-surface-primary)] p-4"
    >
      <header className="mb-2 flex flex-wrap items-start justify-between gap-3">
        <div className="min-w-0 space-y-1">
          <h3 className="flex items-center gap-2 text-sm font-semibold text-[var(--svx-color-text-primary)]">
            <PinIcon className="size-3.5 text-[var(--svx-color-brand-primary)]" />
            <span className="truncate">{override.device_friendly_name}</span>
          </h3>
          <p className="truncate font-mono text-[11px] text-[var(--svx-color-text-tertiary)]">
            {override.endpoint_guid}
          </p>
        </div>
      </header>
      <dl className="grid grid-cols-1 gap-x-4 gap-y-1 text-xs sm:grid-cols-2">
        <div>
          <dt className="text-[var(--svx-color-text-tertiary)]">
            {t("health.combo.winningCombo")}
          </dt>
          <dd className="font-mono">{formatCombo(override.pinned_combo)}</dd>
        </div>
        <div>
          <dt className="text-[var(--svx-color-text-tertiary)]">
            {t("health.overrides.pinnedAt")}
          </dt>
          <dd className="font-mono">{formatIsoTimestamp(override.pinned_at)}</dd>
        </div>
        <div>
          <dt className="text-[var(--svx-color-text-tertiary)]">
            {t("health.overrides.pinnedBy")}
          </dt>
          <dd className="font-mono">{sourceLabel}</dd>
        </div>
        {override.reason && (
          <div>
            <dt className="text-[var(--svx-color-text-tertiary)]">
              {t("health.overrides.reason")}
            </dt>
            <dd className="font-mono">{override.reason}</dd>
          </div>
        )}
      </dl>
    </article>
  );
}

/* ── Mixer KB card ── */

/** One profile row with lazy-fetched detail on expand. */
function MixerKbProfileRow({
  summary,
  detail,
  expanded,
  onToggle,
}: {
  summary: MixerKbProfileSummary;
  detail: MixerKbProfileDetail | undefined;
  expanded: boolean;
  onToggle: () => void;
}) {
  const { t } = useTranslation("voice");
  const PoolIcon = summary.pool === "shipped" ? ShieldAlertIcon : DatabaseIcon;
  return (
    <div
      className="rounded-[var(--svx-radius-md)] border border-[var(--svx-color-border)] bg-[var(--svx-color-surface-secondary)]"
      data-testid={`mixer-kb-profile-${summary.profile_id}`}
    >
      <button
        type="button"
        onClick={onToggle}
        className="flex w-full items-center gap-2 px-3 py-2 text-left hover:bg-[var(--svx-color-surface-hover)]"
        aria-expanded={expanded}
        aria-controls={`mixer-kb-detail-${summary.profile_id}`}
      >
        {expanded ? (
          <ChevronDownIcon className="size-3.5 text-[var(--svx-color-text-tertiary)]" />
        ) : (
          <ChevronRightIcon className="size-3.5 text-[var(--svx-color-text-tertiary)]" />
        )}
        <PoolIcon
          className={`size-3.5 ${
            summary.pool === "shipped"
              ? "text-[var(--svx-color-status-green)]"
              : "text-[var(--svx-color-status-amber)]"
          }`}
          aria-hidden="true"
        />
        <span className="flex-1 font-mono text-sm text-[var(--svx-color-text-primary)]">
          {summary.profile_id}
        </span>
        <span className="font-mono text-[11px] text-[var(--svx-color-text-tertiary)]">
          {summary.driver_family}
        </span>
        <span className="font-mono text-[11px] text-[var(--svx-color-text-tertiary)]">
          {summary.codec_id_glob}
        </span>
        <span className="font-mono text-[11px] text-[var(--svx-color-text-tertiary)]">
          {t("health.mixerKb.versionPrefix")}
          {summary.profile_version}
        </span>
      </button>

      {expanded && (
        <div
          id={`mixer-kb-detail-${summary.profile_id}`}
          className="border-t border-[var(--svx-color-border)] px-3 py-2 font-mono text-[11px]"
          data-testid={`mixer-kb-detail-${summary.profile_id}`}
        >
          {detail ? (
            <dl className="grid grid-cols-[max-content,1fr] gap-x-3 gap-y-1 text-[var(--svx-color-text-secondary)]">
              <dt>pool</dt>
              <dd>{detail.pool}</dd>
              <dt>schema_version</dt>
              <dd>{detail.schema_version}</dd>
              <dt>match_threshold</dt>
              <dd>{detail.match_threshold.toFixed(3)}</dd>
              <dt>factory_regime</dt>
              <dd>{detail.factory_regime}</dd>
              <dt>system_vendor_glob</dt>
              <dd>{detail.system_vendor_glob ?? "—"}</dd>
              <dt>system_product_glob</dt>
              <dd>{detail.system_product_glob ?? "—"}</dd>
              <dt>distro_family</dt>
              <dd>{detail.distro_family ?? "—"}</dd>
              <dt>audio_stack</dt>
              <dd>{detail.audio_stack ?? "—"}</dd>
              <dt>factory_signature_roles</dt>
              <dd>
                {detail.factory_signature_roles.length > 0
                  ? detail.factory_signature_roles.join(", ")
                  : "—"}
              </dd>
              <dt>verified_on_count</dt>
              <dd>{detail.verified_on_count}</dd>
              <dt>contributed_by</dt>
              <dd>{detail.contributed_by}</dd>
            </dl>
          ) : (
            <span className="text-[var(--svx-color-text-tertiary)]">
              <Loader2Icon className="mr-1 inline-block size-3 animate-spin" />
              {t("health.mixerKb.loadingDetail")}
            </span>
          )}
        </div>
      )}
    </div>
  );
}

/** Collapsible YAML-validate panel. Used by reviewers to sanity-check
 * a PR's contribution before opening the repo. */
function MixerKbValidatePanel() {
  const { t } = useTranslation("voice");
  const validate = useDashboardStore((s) => s.validateMixerKbProfile);
  const [open, setOpen] = useState(false);
  const [body, setBody] = useState("");
  const [result, setResult] = useState<MixerKbValidateResponse | null>(null);
  const [busy, setBusy] = useState(false);

  const handleValidate = async () => {
    if (!body.trim()) return;
    setBusy(true);
    try {
      const resp = await validate({ yaml_body: body });
      setResult(resp);
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="rounded-[var(--svx-radius-md)] border border-dashed border-[var(--svx-color-border)] bg-[var(--svx-color-surface-secondary)]">
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        className="flex w-full items-center gap-2 px-3 py-2 text-left text-xs text-[var(--svx-color-text-secondary)] hover:bg-[var(--svx-color-surface-hover)]"
        aria-expanded={open}
        data-testid="mixer-kb-validate-toggle"
      >
        {open ? (
          <ChevronDownIcon className="size-3.5" />
        ) : (
          <ChevronRightIcon className="size-3.5" />
        )}
        {t("health.mixerKb.validateTitle")}
      </button>
      {open && (
        <div className="space-y-2 border-t border-[var(--svx-color-border)] px-3 py-3">
          <p className="text-[11px] text-[var(--svx-color-text-tertiary)]">
            {t("health.mixerKb.validateHint")}
          </p>
          <textarea
            value={body}
            onChange={(e) => setBody(e.target.value)}
            placeholder={t("health.mixerKb.validatePlaceholder")}
            rows={10}
            className="w-full rounded-[var(--svx-radius-sm)] border border-[var(--svx-color-border)] bg-[var(--svx-color-surface-primary)] px-2 py-1.5 font-mono text-[11px] text-[var(--svx-color-text-primary)]"
            data-testid="mixer-kb-validate-textarea"
          />
          <div className="flex items-center gap-2">
            <button
              type="button"
              onClick={() => void handleValidate()}
              disabled={busy || !body.trim()}
              className="inline-flex items-center gap-1.5 rounded-[var(--svx-radius-md)] border border-[var(--svx-color-border)] px-3 py-1.5 text-xs text-[var(--svx-color-text-secondary)] hover:bg-[var(--svx-color-surface-hover)] disabled:opacity-50"
              data-testid="mixer-kb-validate-submit"
            >
              {busy ? (
                <Loader2Icon className="size-3.5 animate-spin" />
              ) : (
                <CheckCircle2Icon className="size-3.5" />
              )}
              {t("health.mixerKb.validateSubmit")}
            </button>
            {result && (
              <span
                className={`text-xs ${
                  result.ok
                    ? "text-[var(--svx-color-status-green)]"
                    : "text-[var(--svx-color-status-red)]"
                }`}
                data-testid="mixer-kb-validate-verdict"
              >
                {result.ok
                  ? `${t("health.mixerKb.validateOk")}${
                      result.profile_id ? ` — ${result.profile_id}` : ""
                    }`
                  : t("health.mixerKb.validateFail")}
              </span>
            )}
          </div>
          {result && result.issues.length > 0 && (
            <ul
              className="space-y-1 rounded-[var(--svx-radius-sm)] border border-[var(--svx-color-status-red)]/40 bg-[var(--svx-color-status-red)]/10 px-3 py-2 font-mono text-[11px] text-[var(--svx-color-status-red)]"
              data-testid="mixer-kb-validate-issues"
            >
              {result.issues.map((issue, idx) => (
                // Loc + msg are deterministic for a given issue; index
                // is a stable fallback for the rare "duplicate loc"
                // shape pydantic occasionally emits.
                // eslint-disable-next-line react/no-array-index-key
                <li key={`${issue.loc}-${idx}`}>
                  <span className="font-semibold">{issue.loc || "<root>"}</span>: {issue.msg}
                </li>
              ))}
            </ul>
          )}
        </div>
      )}
    </div>
  );
}

/** Mixer-KB panel rendered alongside combos + overrides. */
function MixerKbCard() {
  const { t } = useTranslation("voice");
  const list = useDashboardStore((s) => s.mixerKbList);
  const loading = useDashboardStore((s) => s.mixerKbLoading);
  const error = useDashboardStore((s) => s.mixerKbError);
  const details = useDashboardStore((s) => s.mixerKbDetails);
  const fetchList = useDashboardStore((s) => s.fetchMixerKbList);
  const fetchDetail = useDashboardStore((s) => s.fetchMixerKbDetail);
  const [expanded, setExpanded] = useState<string | null>(null);

  useEffect(() => {
    const controller = new AbortController();
    void fetchList(controller.signal);
    return () => controller.abort();
  }, [fetchList]);

  const handleToggle = (profile_id: string) => {
    const next = expanded === profile_id ? null : profile_id;
    setExpanded(next);
    if (next && !details[profile_id]) {
      void fetchDetail(profile_id);
    }
  };

  const profiles = list?.profiles ?? [];

  return (
    <section aria-labelledby="mixer-kb-heading" className="space-y-3">
      <div className="flex items-baseline justify-between">
        <h2
          id="mixer-kb-heading"
          className="text-sm font-semibold uppercase tracking-wider text-[var(--svx-color-text-secondary)]"
        >
          {t("health.mixerKb.sectionTitle")}
        </h2>
        <span
          className="font-mono text-[11px] text-[var(--svx-color-text-tertiary)]"
          data-testid="mixer-kb-count"
        >
          {list
            ? `${list.shipped_count} / ${list.user_count}`
            : "—"}
        </span>
      </div>
      <p className="text-xs text-[var(--svx-color-text-tertiary)]">
        {t("health.mixerKb.sectionHint")}
      </p>
      {loading && !list && (
        <div
          className="flex items-center gap-2 text-xs text-[var(--svx-color-text-tertiary)]"
          data-testid="mixer-kb-loading"
        >
          <Loader2Icon className="size-3.5 animate-spin" />
          {t("health.mixerKb.loadingList")}
        </div>
      )}
      {error && (
        <div
          className="rounded-[var(--svx-radius-md)] border border-[var(--svx-color-status-red)]/40 bg-[var(--svx-color-status-red)]/10 px-3 py-2 font-mono text-xs text-[var(--svx-color-status-red)]"
          data-testid="mixer-kb-error"
        >
          {error}
        </div>
      )}
      {!loading && list && profiles.length === 0 && (
        <div
          className="rounded-[var(--svx-radius-lg)] border border-dashed border-[var(--svx-color-border)] bg-[var(--svx-color-surface-secondary)] p-6 text-center text-sm text-[var(--svx-color-text-tertiary)]"
          data-testid="mixer-kb-empty"
        >
          {t("health.mixerKb.empty")}
        </div>
      )}
      {profiles.length > 0 && (
        <div className="space-y-2">
          {profiles.map((summary) => (
            <MixerKbProfileRow
              key={`${summary.pool}-${summary.profile_id}`}
              summary={summary}
              detail={details[summary.profile_id]}
              expanded={expanded === summary.profile_id}
              onToggle={() => handleToggle(summary.profile_id)}
            />
          ))}
        </div>
      )}
      <MixerKbValidatePanel />
    </section>
  );
}

/* ── Main page ── */

export default function VoiceHealthPage() {
  const { t } = useTranslation("voice");
  const snapshot = useDashboardStore((s) => s.voiceHealthSnapshot);
  const loading = useDashboardStore((s) => s.voiceHealthLoading);
  const error = useDashboardStore((s) => s.voiceHealthError);
  const latestProbes = useDashboardStore((s) => s.voiceHealthLastProbe);
  const busy = useDashboardStore((s) => s.voiceHealthBusy);
  const fetchVoiceHealth = useDashboardStore((s) => s.fetchVoiceHealth);
  const reprobe = useDashboardStore((s) => s.reprobeVoiceEndpoint);
  const forget = useDashboardStore((s) => s.forgetVoiceEndpoint);
  const pin = useDashboardStore((s) => s.pinVoiceEndpoint);
  const clearError = useDashboardStore((s) => s.clearVoiceHealthError);

  const [confirmForget, setConfirmForget] = useState<string | null>(null);

  useEffect(() => {
    const controller = new AbortController();
    void fetchVoiceHealth(controller.signal);
    return () => controller.abort();
  }, [fetchVoiceHealth]);

  const combos = useMemo(() => snapshot?.combo_store ?? [], [snapshot]);
  const overrides = useMemo(() => snapshot?.overrides ?? [], [snapshot]);
  const voiceEnabled = snapshot?.voice_enabled ?? false;

  const handleReprobe = async (
    entry: VoiceHealthComboEntry,
    mode: "cold" | "warm",
  ) => {
    // ComboStore entries don't persist the numeric device index (it rotates
    // across reboots). Omit it and the backend resolves from the stored
    // friendly name via PortAudio — see routes/voice_health.py.
    await reprobe({
      endpoint_guid: entry.endpoint_guid,
      mode,
      combo: entry.winning_combo,
    });
  };

  const handleForget = async (endpoint_guid: string) => {
    const ok = window.confirm(t("health.actions.confirmForget"));
    if (!ok) {
      setConfirmForget(null);
      return;
    }
    await forget(endpoint_guid);
    setConfirmForget(null);
  };

  const handlePin = async (entry: VoiceHealthComboEntry) => {
    await pin({
      endpoint_guid: entry.endpoint_guid,
      device_friendly_name: entry.device_friendly_name,
      combo: entry.winning_combo,
      source: "user",
      reason: t("health.actions.pinReason"),
    });
  };

  /* ── Loading ── */
  if (loading && !snapshot) {
    return (
      <div
        className="flex min-h-[300px] items-center justify-center gap-2 text-[var(--svx-color-text-secondary)]"
        data-testid="voice-health-loading"
      >
        <Loader2Icon className="size-5 animate-spin" />
        <span>{t("health.loading")}</span>
      </div>
    );
  }

  /* ── Error ── */
  if (error && !snapshot) {
    return (
      <div className="flex min-h-[300px] flex-col items-center justify-center gap-3 text-[var(--svx-color-text-secondary)]">
        <AlertTriangleIcon className="size-8 text-[var(--svx-color-status-amber)]" />
        <p className="max-w-md text-center font-mono text-xs">{error}</p>
        <button
          type="button"
          onClick={() => {
            clearError();
            void fetchVoiceHealth();
          }}
          className="rounded-[var(--svx-radius-md)] border border-[var(--svx-color-border)] px-3 py-1.5 text-sm hover:bg-[var(--svx-color-surface-hover)]"
        >
          {t("health.refresh")}
        </button>
      </div>
    );
  }

  return (
    <div className="space-y-6">
      {/* Header */}
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <h1 className="text-2xl font-bold">{t("health.title")}</h1>
          <p className="text-sm text-[var(--svx-color-text-secondary)]">
            {t("health.subtitle")}
          </p>
        </div>
        <div className="flex items-center gap-3">
          <span
            className={`inline-flex items-center gap-1.5 text-xs ${
              voiceEnabled
                ? "text-[var(--svx-color-status-green)]"
                : "text-[var(--svx-color-text-tertiary)]"
            }`}
            data-testid="voice-enabled-indicator"
          >
            <span
              className={`inline-block size-2 rounded-full ${
                voiceEnabled
                  ? "bg-[var(--svx-color-status-green)]"
                  : "bg-[var(--svx-color-text-tertiary)]"
              }`}
            />
            {voiceEnabled ? t("health.voiceEnabled") : t("health.voiceDisabled")}
          </span>
          <button
            type="button"
            onClick={() => void fetchVoiceHealth()}
            disabled={loading}
            className="inline-flex items-center gap-1.5 rounded-[var(--svx-radius-md)] border border-[var(--svx-color-border)] px-3 py-1.5 text-xs text-[var(--svx-color-text-secondary)] hover:bg-[var(--svx-color-surface-hover)] disabled:opacity-50"
            aria-label={t("health.refresh")}
            data-testid="btn-refresh-voice-health"
          >
            <RefreshCwIcon className={`size-3.5 ${loading ? "animate-spin" : ""}`} />
            {t("health.refresh")}
          </button>
        </div>
      </div>

      {snapshot?.data_dir && (
        <p className="font-mono text-[11px] text-[var(--svx-color-text-tertiary)]">
          {t("health.dataDir")}: {snapshot.data_dir}
        </p>
      )}

      {error && snapshot && (
        <div className="rounded-[var(--svx-radius-md)] border border-[var(--svx-color-status-red)]/40 bg-[var(--svx-color-status-red)]/10 px-3 py-2 font-mono text-xs text-[var(--svx-color-status-red)]">
          {error}
        </div>
      )}

      {/* Combos */}
      <section aria-labelledby="combos-heading" className="space-y-3">
        <div className="flex items-baseline justify-between">
          <h2
            id="combos-heading"
            className="text-sm font-semibold uppercase tracking-wider text-[var(--svx-color-text-secondary)]"
          >
            {t("health.combo.sectionTitle")}
          </h2>
          <span className="font-mono text-[11px] text-[var(--svx-color-text-tertiary)]">
            {combos.length}
          </span>
        </div>
        <p className="text-xs text-[var(--svx-color-text-tertiary)]">
          {t("health.combo.sectionHint")}
        </p>
        {combos.length === 0 ? (
          <div
            className="rounded-[var(--svx-radius-lg)] border border-dashed border-[var(--svx-color-border)] bg-[var(--svx-color-surface-secondary)] p-6 text-center text-sm text-[var(--svx-color-text-tertiary)]"
            data-testid="combos-empty"
          >
            {t("health.empty")}
          </div>
        ) : (
          <div className="space-y-3">
            {combos.map((entry) => (
              <ComboRow
                key={entry.endpoint_guid}
                entry={entry}
                voiceEnabled={voiceEnabled}
                busy={!!busy[entry.endpoint_guid] || confirmForget === entry.endpoint_guid}
                latestProbe={latestProbes[entry.endpoint_guid]}
                onReprobeCold={() => void handleReprobe(entry, "cold")}
                onReprobeWarm={() => void handleReprobe(entry, "warm")}
                onForget={() => void handleForget(entry.endpoint_guid)}
                onPin={() => void handlePin(entry)}
              />
            ))}
          </div>
        )}
      </section>

      {/* Overrides */}
      <section aria-labelledby="overrides-heading" className="space-y-3">
        <div className="flex items-baseline justify-between">
          <h2
            id="overrides-heading"
            className="text-sm font-semibold uppercase tracking-wider text-[var(--svx-color-text-secondary)]"
          >
            {t("health.overrides.sectionTitle")}
          </h2>
          <span className="font-mono text-[11px] text-[var(--svx-color-text-tertiary)]">
            {overrides.length}
          </span>
        </div>
        <p className="text-xs text-[var(--svx-color-text-tertiary)]">
          {t("health.overrides.sectionHint")}
        </p>
        {overrides.length === 0 ? (
          <div
            className="rounded-[var(--svx-radius-lg)] border border-dashed border-[var(--svx-color-border)] bg-[var(--svx-color-surface-secondary)] p-6 text-center text-sm text-[var(--svx-color-text-tertiary)]"
            data-testid="overrides-empty"
          >
            {t("health.overrides.noEntries")}
          </div>
        ) : (
          <div className="space-y-3">
            {overrides.map((override) => (
              <OverrideRow
                key={override.endpoint_guid}
                override={override}
              />
            ))}
          </div>
        )}
      </section>

      {/* Mixer KB — independent of cascade state; shows shipped + user
          profile pools plus an inline YAML-validate panel for reviewers. */}
      <MixerKbCard />
    </div>
  );
}
