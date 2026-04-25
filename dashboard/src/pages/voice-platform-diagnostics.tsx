/**
 * Voice Platform Diagnostics — operator surface for the cross-OS
 * diagnostic endpoint at ``GET /api/voice/platform-diagnostics``.
 *
 * Renders one panel per probe surface (microphone permission +
 * platform-specific cards for Linux / Windows / macOS branches).
 * The endpoint is read-only and never raises on probe failures —
 * the page therefore degrades gracefully into "(not populated)"
 * placeholders rather than blocking the entire panel.
 *
 * Auth-required via the standard ``api.get`` flow (same as every
 * other dashboard page); the backend returns 401 if the bearer
 * token is missing or wrong, and the page falls into its error
 * state with a Refresh button.
 */

import { useCallback, useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import {
  AlertTriangleIcon,
  Loader2Icon,
  MicIcon,
  RefreshCwIcon,
} from "lucide-react";
import { ApiError, api, isAbortError } from "@/lib/api";
import type {
  EtwChannelPayload,
  EtwEventLevelToken,
  HalPayload,
  PlatformDiagnosticsResponse,
  PlatformLinuxBranch,
  PlatformMacOSBranch,
  PlatformMicPermissionPayload,
  PlatformWindowsBranch,
  WindowsAudioServicePayload,
} from "@/types/api";
import { PlatformDiagnosticsResponseSchema } from "@/types/schemas";

/* ── Helpers ───────────────────────────────────────────────────── */

type Tone = "ok" | "warn" | "error" | "neutral";

function micPermissionTone(
  status: PlatformMicPermissionPayload["status"],
): Tone {
  switch (status) {
    case "granted":
      return "ok";
    case "denied":
      return "error";
    case "unknown":
    default:
      return "warn";
  }
}

function windowsServiceTone(
  payload: WindowsAudioServicePayload,
): Tone {
  if (payload.all_healthy) return "ok";
  if (payload.degraded_services.length > 0) return "error";
  return "warn";
}

function etwLevelTone(level: EtwEventLevelToken): Tone {
  switch (level) {
    case "critical":
    case "error":
      return "error";
    case "warning":
      return "warn";
    case "information":
      return "neutral";
    case "verbose":
    case "unknown":
    default:
      return "neutral";
  }
}

function halTone(payload: HalPayload): Tone {
  // A virtual-audio device or audio-enhancement APO present is the
  // signal that a third-party plug-in is between Sovyx and the OS
  // mic — flag as warn so operators investigate on capture issues.
  if (payload.virtual_audio_active || payload.audio_enhancement_active) {
    return "warn";
  }
  return "neutral";
}

function toneClasses(tone: Tone): string {
  switch (tone) {
    case "ok":
      return "border-[var(--svx-color-status-green)]/40 bg-[var(--svx-color-status-green)]/10 text-[var(--svx-color-status-green)]";
    case "warn":
      return "border-[var(--svx-color-status-amber)]/40 bg-[var(--svx-color-status-amber)]/10 text-[var(--svx-color-status-amber)]";
    case "error":
      return "border-[var(--svx-color-status-red)]/40 bg-[var(--svx-color-status-red)]/10 text-[var(--svx-color-status-red)]";
    case "neutral":
    default:
      return "border-[var(--svx-color-border)] bg-[var(--svx-color-surface-secondary)] text-[var(--svx-color-text-secondary)]";
  }
}

function StatusPill({
  tone,
  label,
}: {
  tone: Tone;
  label: string;
}) {
  return (
    <span
      className={`inline-flex items-center gap-1 rounded-full border px-2 py-0.5 text-xs font-mono ${toneClasses(tone)}`}
      data-testid="platform-status-pill"
    >
      {label}
    </span>
  );
}

function NotesList({ notes }: { notes: string[] }) {
  if (notes.length === 0) return null;
  return (
    <ul className="mt-2 space-y-1 text-xs text-[var(--svx-color-text-tertiary)]">
      {notes.map((n, i) => (
        // eslint-disable-next-line react/no-array-index-key — notes are
        // value-stable for the lifetime of one snapshot; index is fine.
        <li key={i} className="font-mono">
          • {n}
        </li>
      ))}
    </ul>
  );
}

/* ── Section: microphone permission ───────────────────────────── */

function MicPermissionCard({
  payload,
}: {
  payload: PlatformMicPermissionPayload;
}) {
  const { t } = useTranslation("voice");
  const tone = micPermissionTone(payload.status);
  const label =
    payload.status === "granted"
      ? t("platform.micSection.statusGranted")
      : payload.status === "denied"
        ? t("platform.micSection.statusDenied")
        : t("platform.micSection.statusUnknown");

  return (
    <section
      aria-labelledby="mic-section-heading"
      className="rounded-[var(--svx-radius-lg)] border border-[var(--svx-color-border)] bg-[var(--svx-color-surface-primary)] p-4"
      data-testid="platform-mic-card"
    >
      <div className="flex items-center justify-between gap-3">
        <h2
          id="mic-section-heading"
          className="flex items-center gap-2 text-sm font-semibold uppercase tracking-wider text-[var(--svx-color-text-secondary)]"
        >
          <MicIcon className="size-4" />
          {t("platform.micSection.heading")}
        </h2>
        <StatusPill tone={tone} label={label} />
      </div>
      {(payload.machine_value || payload.user_value) && (
        <dl className="mt-3 grid grid-cols-1 gap-1 text-xs sm:grid-cols-2">
          {payload.machine_value && (
            <div>
              <dt className="text-[var(--svx-color-text-tertiary)]">
                {t("platform.micSection.machineValue")}
              </dt>
              <dd className="font-mono">{payload.machine_value}</dd>
            </div>
          )}
          {payload.user_value && (
            <div>
              <dt className="text-[var(--svx-color-text-tertiary)]">
                {t("platform.micSection.userValue")}
              </dt>
              <dd className="font-mono">{payload.user_value}</dd>
            </div>
          )}
        </dl>
      )}
      {payload.remediation_hint && (
        <p className="mt-3 text-sm text-[var(--svx-color-text-secondary)]">
          {payload.remediation_hint}
        </p>
      )}
      <NotesList notes={payload.notes} />
    </section>
  );
}

/* ── Section: Linux branch ────────────────────────────────────── */

function LinuxBranchCard({ branch }: { branch: PlatformLinuxBranch }) {
  const { t } = useTranslation("voice");
  return (
    <section
      aria-labelledby="linux-heading"
      className="space-y-3"
      data-testid="platform-linux-card"
    >
      <h2
        id="linux-heading"
        className="text-sm font-semibold uppercase tracking-wider text-[var(--svx-color-text-secondary)]"
      >
        {t("platform.linuxSection.heading")}
      </h2>

      <div className="rounded-[var(--svx-radius-lg)] border border-[var(--svx-color-border)] bg-[var(--svx-color-surface-primary)] p-4">
        <div className="flex items-center justify-between">
          <h3 className="text-sm font-semibold">
            {t("platform.linuxSection.pipewireTitle")}
          </h3>
          <StatusPill
            tone={branch.pipewire.status === "active" ? "ok" : "neutral"}
            label={branch.pipewire.status}
          />
        </div>
        <dl className="mt-3 grid grid-cols-2 gap-x-4 gap-y-1 text-xs">
          <dt className="text-[var(--svx-color-text-tertiary)]">
            {t("platform.linuxSection.pipewireSocket")}
          </dt>
          <dd className="font-mono">
            {branch.pipewire.socket_present ? "yes" : "no"}
          </dd>
          <dt className="text-[var(--svx-color-text-tertiary)]">
            {t("platform.linuxSection.pipewirePactl")}
          </dt>
          <dd className="font-mono">
            {branch.pipewire.pactl_available ? "yes" : "no"}
          </dd>
          {branch.pipewire.server_name && (
            <>
              <dt className="text-[var(--svx-color-text-tertiary)]">
                {t("platform.linuxSection.pipewireServer")}
              </dt>
              <dd className="font-mono">{branch.pipewire.server_name}</dd>
            </>
          )}
          <dt className="text-[var(--svx-color-text-tertiary)]">
            {t("platform.linuxSection.pipewireEchoCancel")}
          </dt>
          <dd className="font-mono">
            {branch.pipewire.echo_cancel_loaded ? "loaded" : "not loaded"}
          </dd>
          {branch.pipewire.modules_loaded.length > 0 && (
            <>
              <dt className="text-[var(--svx-color-text-tertiary)]">
                {t("platform.linuxSection.pipewireModules")}
              </dt>
              <dd className="font-mono">
                {branch.pipewire.modules_loaded.join(", ")}
              </dd>
            </>
          )}
        </dl>
        <NotesList notes={branch.pipewire.notes} />
      </div>

      <div className="rounded-[var(--svx-radius-lg)] border border-[var(--svx-color-border)] bg-[var(--svx-color-surface-primary)] p-4">
        <div className="flex items-center justify-between">
          <h3 className="text-sm font-semibold">
            {t("platform.linuxSection.ucmTitle")}
          </h3>
          <StatusPill
            tone={branch.alsa_ucm.status === "available" ? "ok" : "neutral"}
            label={branch.alsa_ucm.status}
          />
        </div>
        <dl className="mt-3 grid grid-cols-2 gap-x-4 gap-y-1 text-xs">
          <dt className="text-[var(--svx-color-text-tertiary)]">
            {t("platform.linuxSection.ucmCard")}
          </dt>
          <dd className="font-mono">{branch.alsa_ucm.card_id}</dd>
          <dt className="text-[var(--svx-color-text-tertiary)]">
            {t("platform.linuxSection.ucmAlsaucm")}
          </dt>
          <dd className="font-mono">
            {branch.alsa_ucm.alsaucm_available ? "yes" : "no"}
          </dd>
          {branch.alsa_ucm.active_verb && (
            <>
              <dt className="text-[var(--svx-color-text-tertiary)]">
                {t("platform.linuxSection.ucmActiveVerb")}
              </dt>
              <dd className="font-mono">{branch.alsa_ucm.active_verb}</dd>
            </>
          )}
          {branch.alsa_ucm.verbs.length > 0 && (
            <>
              <dt className="text-[var(--svx-color-text-tertiary)]">
                {t("platform.linuxSection.ucmVerbs")}
              </dt>
              <dd className="font-mono">{branch.alsa_ucm.verbs.join(", ")}</dd>
            </>
          )}
        </dl>
        <NotesList notes={branch.alsa_ucm.notes} />
      </div>
    </section>
  );
}

/* ── Section: Windows branch ──────────────────────────────────── */

function WindowsBranchCard({ branch }: { branch: PlatformWindowsBranch }) {
  const { t } = useTranslation("voice");
  const svcTone = windowsServiceTone(branch.audio_service);

  return (
    <section
      aria-labelledby="windows-heading"
      className="space-y-3"
      data-testid="platform-windows-card"
    >
      <h2
        id="windows-heading"
        className="text-sm font-semibold uppercase tracking-wider text-[var(--svx-color-text-secondary)]"
      >
        {t("platform.windowsSection.heading")}
      </h2>

      <div className="rounded-[var(--svx-radius-lg)] border border-[var(--svx-color-border)] bg-[var(--svx-color-surface-primary)] p-4">
        <div className="flex items-center justify-between">
          <h3 className="text-sm font-semibold">
            {t("platform.windowsSection.audioServiceTitle")}
          </h3>
          <StatusPill
            tone={svcTone}
            label={
              branch.audio_service.all_healthy
                ? t("platform.windowsSection.audioServiceAllHealthy")
                : (branch.audio_service.degraded_services.join(", ") ||
                  "degraded")
            }
          />
        </div>
        <dl className="mt-3 grid grid-cols-2 gap-x-4 gap-y-1 text-xs">
          <dt className="text-[var(--svx-color-text-tertiary)]">
            {branch.audio_service.audiosrv.name}
          </dt>
          <dd className="font-mono">{branch.audio_service.audiosrv.state}</dd>
          <dt className="text-[var(--svx-color-text-tertiary)]">
            {branch.audio_service.audio_endpoint_builder.name}
          </dt>
          <dd className="font-mono">
            {branch.audio_service.audio_endpoint_builder.state}
          </dd>
        </dl>
        {!branch.audio_service.all_healthy && (
          <p className="mt-3 text-sm text-[var(--svx-color-status-amber)]">
            {t("platform.windowsSection.audioServiceDegraded")}
          </p>
        )}
        <NotesList
          notes={[
            ...branch.audio_service.audiosrv.notes,
            ...branch.audio_service.audio_endpoint_builder.notes,
          ]}
        />
      </div>

      <div className="rounded-[var(--svx-radius-lg)] border border-[var(--svx-color-border)] bg-[var(--svx-color-surface-primary)] p-4">
        <h3 className="text-sm font-semibold">
          {t("platform.windowsSection.etwTitle")}
        </h3>
        {branch.etw_audio_events.every((c) => c.events.length === 0) ? (
          <p className="mt-2 text-xs text-[var(--svx-color-text-tertiary)]">
            {t("platform.windowsSection.etwEmpty")}
          </p>
        ) : (
          <div className="mt-3 space-y-3">
            {branch.etw_audio_events.map((channel) => (
              <EtwChannelBlock key={channel.channel} channel={channel} />
            ))}
          </div>
        )}
      </div>
    </section>
  );
}

function EtwChannelBlock({ channel }: { channel: EtwChannelPayload }) {
  const { t } = useTranslation("voice");
  if (channel.events.length === 0 && channel.notes.length === 0) return null;
  return (
    <div className="rounded-[var(--svx-radius-md)] border border-[var(--svx-color-border)] p-3">
      <div className="flex items-center justify-between">
        <p className="font-mono text-xs">{channel.channel}</p>
        <p className="font-mono text-[10px] text-[var(--svx-color-text-tertiary)]">
          {t("platform.windowsSection.etwLookback", {
            seconds: channel.lookback_seconds,
          })}
        </p>
      </div>
      {channel.events.length > 0 && (
        <ul className="mt-2 space-y-1.5">
          {channel.events.slice(0, 10).map((ev) => (
            <li
              key={`${ev.event_id}-${ev.timestamp_iso}`}
              className="flex items-start gap-2 text-xs"
            >
              <StatusPill tone={etwLevelTone(ev.level)} label={ev.level} />
              <div className="min-w-0 flex-1">
                <p className="font-mono">
                  #{ev.event_id} {ev.timestamp_iso}
                </p>
                {ev.description && (
                  <p className="text-[var(--svx-color-text-secondary)]">
                    {ev.description}
                  </p>
                )}
              </div>
            </li>
          ))}
        </ul>
      )}
      <NotesList notes={channel.notes} />
    </div>
  );
}

/* ── Section: macOS branch ────────────────────────────────────── */

function MacOSBranchCard({ branch }: { branch: PlatformMacOSBranch }) {
  const { t } = useTranslation("voice");
  return (
    <section
      aria-labelledby="macos-heading"
      className="space-y-3"
      data-testid="platform-macos-card"
    >
      <h2
        id="macos-heading"
        className="text-sm font-semibold uppercase tracking-wider text-[var(--svx-color-text-secondary)]"
      >
        {t("platform.macosSection.heading")}
      </h2>

      <div className="rounded-[var(--svx-radius-lg)] border border-[var(--svx-color-border)] bg-[var(--svx-color-surface-primary)] p-4">
        <div className="flex items-center justify-between">
          <h3 className="text-sm font-semibold">
            {t("platform.macosSection.halTitle")}
          </h3>
          <StatusPill
            tone={halTone(branch.hal_plugins)}
            label={`${branch.hal_plugins.plugins.length}`}
          />
        </div>
        {branch.hal_plugins.plugins.length === 0 ? (
          <p className="mt-2 text-xs text-[var(--svx-color-text-tertiary)]">
            {t("platform.macosSection.halEmpty")}
          </p>
        ) : (
          <ul className="mt-3 space-y-2 text-xs">
            {branch.hal_plugins.plugins.map((p) => (
              <li
                key={p.path}
                className="rounded-[var(--svx-radius-md)] border border-[var(--svx-color-border)] p-2"
              >
                <p className="font-semibold">
                  {p.friendly_label || p.bundle_name}
                </p>
                <p className="font-mono text-[10px] text-[var(--svx-color-text-tertiary)]">
                  {p.path}
                </p>
                <p className="text-[var(--svx-color-text-secondary)]">
                  {t("platform.macosSection.halCategory")}: {p.category}
                </p>
              </li>
            ))}
          </ul>
        )}
        <NotesList notes={branch.hal_plugins.notes} />
      </div>

      <div className="rounded-[var(--svx-radius-lg)] border border-[var(--svx-color-border)] bg-[var(--svx-color-surface-primary)] p-4">
        <h3 className="text-sm font-semibold">
          {t("platform.macosSection.bluetoothTitle")}
        </h3>
        {branch.bluetooth.devices.length === 0 ? (
          <p className="mt-2 text-xs text-[var(--svx-color-text-tertiary)]">
            {t("platform.macosSection.bluetoothEmpty")}
          </p>
        ) : (
          <ul className="mt-3 space-y-2 text-xs">
            {branch.bluetooth.devices.map((d) => (
              <li
                key={d.address || d.name}
                className="flex items-center justify-between gap-3 rounded-[var(--svx-radius-md)] border border-[var(--svx-color-border)] p-2"
              >
                <div>
                  <p className="font-semibold">{d.name}</p>
                  <p className="font-mono text-[10px] text-[var(--svx-color-text-tertiary)]">
                    {d.address}
                  </p>
                </div>
                <StatusPill
                  tone={d.profile === "a2dp" ? "warn" : "neutral"}
                  label={d.profile}
                />
              </li>
            ))}
          </ul>
        )}
        <NotesList notes={branch.bluetooth.notes} />
      </div>

      <div className="rounded-[var(--svx-radius-lg)] border border-[var(--svx-color-border)] bg-[var(--svx-color-surface-primary)] p-4">
        <div className="flex items-center justify-between">
          <h3 className="text-sm font-semibold">
            {t("platform.macosSection.codeSigningTitle")}
          </h3>
          <StatusPill
            tone={
              branch.code_signing.verdict === "present"
                ? "ok"
                : branch.code_signing.verdict === "absent"
                  ? "error"
                  : "neutral"
            }
            label={branch.code_signing.verdict}
          />
        </div>
        {branch.code_signing.executable_path && (
          <p className="mt-2 font-mono text-[10px] text-[var(--svx-color-text-tertiary)]">
            {branch.code_signing.executable_path}
          </p>
        )}
        {branch.code_signing.remediation_hint && (
          <p className="mt-2 text-sm text-[var(--svx-color-text-secondary)]">
            {branch.code_signing.remediation_hint}
          </p>
        )}
        <NotesList notes={branch.code_signing.notes} />
      </div>
    </section>
  );
}

/* ── Page ──────────────────────────────────────────────────────── */

export default function VoicePlatformDiagnosticsPage() {
  const { t } = useTranslation("voice");
  const [snapshot, setSnapshot] = useState<PlatformDiagnosticsResponse | null>(
    null,
  );
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const fetchSnapshot = useCallback(async (signal?: AbortSignal) => {
    setLoading(true);
    setError(null);
    try {
      const data = await api.get<PlatformDiagnosticsResponse>(
        "/api/voice/platform-diagnostics",
        { signal, schema: PlatformDiagnosticsResponseSchema },
      );
      setSnapshot(data);
    } catch (err) {
      if (isAbortError(err)) return;
      const msg =
        err instanceof ApiError
          ? `HTTP ${err.status}: ${err.message}`
          : err instanceof Error
            ? err.message
            : String(err);
      setError(msg);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    const controller = new AbortController();
    void fetchSnapshot(controller.signal);
    return () => controller.abort();
  }, [fetchSnapshot]);

  if (loading && !snapshot) {
    return (
      <div
        className="flex min-h-[300px] items-center justify-center gap-2 text-[var(--svx-color-text-secondary)]"
        data-testid="platform-loading"
      >
        <Loader2Icon className="size-5 animate-spin" />
        <span>{t("platform.loading")}</span>
      </div>
    );
  }

  if (error && !snapshot) {
    return (
      <div
        className="flex min-h-[300px] flex-col items-center justify-center gap-3 text-[var(--svx-color-text-secondary)]"
        data-testid="platform-error"
      >
        <AlertTriangleIcon className="size-8 text-[var(--svx-color-status-amber)]" />
        <p className="max-w-md text-center font-mono text-xs">{error}</p>
        <button
          type="button"
          onClick={() => void fetchSnapshot()}
          className="rounded-[var(--svx-radius-md)] border border-[var(--svx-color-border)] px-3 py-1.5 text-sm hover:bg-[var(--svx-color-surface-hover)]"
        >
          {t("platform.refresh")}
        </button>
      </div>
    );
  }

  if (!snapshot) return null;

  return (
    <div className="space-y-6" data-testid="platform-diagnostics-page">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <h1 className="text-2xl font-bold">{t("platform.title")}</h1>
          <p className="text-sm text-[var(--svx-color-text-secondary)]">
            {t("platform.subtitle")}
          </p>
          <p className="mt-1 font-mono text-[11px] text-[var(--svx-color-text-tertiary)]">
            {t("platform.platformLabel")}: {snapshot.platform}
          </p>
        </div>
        <button
          type="button"
          onClick={() => void fetchSnapshot()}
          disabled={loading}
          className="inline-flex items-center gap-1.5 rounded-[var(--svx-radius-md)] border border-[var(--svx-color-border)] px-3 py-1.5 text-xs text-[var(--svx-color-text-secondary)] hover:bg-[var(--svx-color-surface-hover)] disabled:opacity-50"
          aria-label={t("platform.refresh")}
          data-testid="platform-refresh"
        >
          <RefreshCwIcon className={`size-3.5 ${loading ? "animate-spin" : ""}`} />
          {t("platform.refresh")}
        </button>
      </div>

      {error && snapshot && (
        <div className="rounded-[var(--svx-radius-md)] border border-[var(--svx-color-status-red)]/40 bg-[var(--svx-color-status-red)]/10 px-3 py-2 font-mono text-xs text-[var(--svx-color-status-red)]">
          {error}
        </div>
      )}

      <MicPermissionCard payload={snapshot.mic_permission} />

      {snapshot.linux && <LinuxBranchCard branch={snapshot.linux} />}
      {snapshot.windows && <WindowsBranchCard branch={snapshot.windows} />}
      {snapshot.macos && <MacOSBranchCard branch={snapshot.macos} />}

      {!snapshot.linux && !snapshot.windows && !snapshot.macos && (
        <div className="rounded-[var(--svx-radius-lg)] border border-dashed border-[var(--svx-color-border)] bg-[var(--svx-color-surface-secondary)] p-6 text-center text-sm text-[var(--svx-color-text-tertiary)]">
          {t("platform.noBranch")}
        </div>
      )}
    </div>
  );
}

/* ── Side-effect-free named re-exports for the test file ─────── */

export { MicPermissionCard, LinuxBranchCard, WindowsBranchCard, MacOSBranchCard };
