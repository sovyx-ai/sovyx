/**
 * LinuxMicGainCard — Linux ALSA mixer saturation diagnostic + one-click reset.
 *
 * Mirror of VoiceClarityCard for the Linux pre-ADC gain saturation
 * pattern (Internal Mic Boost + Capture stages pinned at +40 dB by
 * default on laptop codecs). Polls
 * `GET /api/voice/linux-mixer-diagnostics` once on mount; when any
 * card reports `saturation_warning`, renders a warning card with a
 * "Reset microphone gain" button that POSTs `/api/voice/linux-mixer-reset`
 * to drive the saturated controls back into the analog range.
 *
 * Hidden on non-Linux platforms (`platform_supported=false`) so
 * Windows / macOS users don't see an irrelevant diagnostic. Also
 * surfaces a distinct warning when Linux is detected but `amixer` is
 * missing from PATH — installing `alsa-utils` is the prerequisite.
 *
 * v1.3 §4.3 L5a — moved from ``components/settings/`` to
 * ``components/voice/`` so the Voice page can render it inline (and
 * the Settings page continues to render the same module from the new
 * path). The i18n namespace moves alongside: ``voice:linuxMicGain.*``.
 * Rendering both pages points to the same component — there is no
 * duplication.
 */

import { useCallback, useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import {
  AlertTriangleIcon,
  CheckCircle2Icon,
  InfoIcon,
  Loader2Icon,
  MicIcon,
  SlidersHorizontalIcon,
  WrenchIcon,
} from "lucide-react";
import { toast } from "sonner";

import { Button } from "@/components/ui/button";
import { api, isAbortError } from "@/lib/api";
import {
  LinuxMixerDiagnosticsResponseSchema,
  LinuxMixerResetResponseSchema,
} from "@/types/schemas";
import type {
  LinuxMixerDiagnosticsResponse,
  LinuxMixerResetResponse,
} from "@/types/api";

export function LinuxMicGainCard() {
  const { t } = useTranslation(["voice"]);
  const [diag, setDiag] = useState<LinuxMixerDiagnosticsResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [applying, setApplying] = useState(false);
  // v1.3 L0-4 — once the reset applied successfully, surface the
  // inline persistence hint so a technical user can optionally run
  // ``sudo alsactl store`` to persist the safe values across reboots.
  // Defaults to collapsed; the <details> element exposes the hint on
  // demand without dominating the card for users who don't need it.
  const [resetApplied, setResetApplied] = useState(false);

  const load = useCallback(async (signal?: AbortSignal) => {
    try {
      const data = await api.get<LinuxMixerDiagnosticsResponse>(
        "/api/voice/linux-mixer-diagnostics",
        { signal, schema: LinuxMixerDiagnosticsResponseSchema },
      );
      setDiag(data);
    } catch (err) {
      if (isAbortError(err)) return;
      setDiag(null);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    const ctrl = new AbortController();
    void load(ctrl.signal);
    return () => ctrl.abort();
  }, [load]);

  const saturating = diag?.snapshots.filter((s) => s.saturation_warning) ?? [];

  const handleReset = useCallback(async () => {
    setApplying(true);
    try {
      const uniqueCard = saturating.length === 1 ? saturating[0] : undefined;
      const body = uniqueCard ? { card_index: uniqueCard.card_index } : {};
      const resp = await api.post<LinuxMixerResetResponse>(
        "/api/voice/linux-mixer-reset",
        body,
        { schema: LinuxMixerResetResponseSchema },
      );
      if (resp.ok) {
        toast.success(
          t("voice:linuxMicGain.resetSuccess", {
            count: resp.applied_controls?.length ?? 0,
          }),
        );
        setResetApplied(true);
      } else {
        toast.error(
          t("voice:linuxMicGain.resetFailed", {
            reason: resp.reason ?? "unknown",
            detail: resp.detail ?? "",
          }),
        );
      }
      await load();
    } catch (err) {
      const msg = err instanceof Error ? err.message : "Request failed";
      toast.error(t("voice:linuxMicGain.failed", { error: msg }));
    } finally {
      setApplying(false);
    }
  }, [load, saturating, t]);

  // Hide entirely on non-Linux hosts.
  if (!loading && diag && !diag.platform_supported) {
    return null;
  }

  const amixerMissing = !!diag && diag.platform_supported && !diag.amixer_available;
  const hasSaturation = saturating.length > 0;

  return (
    <section
      className="rounded-[var(--svx-radius-lg)] border border-[var(--svx-color-border-default)] bg-[var(--svx-color-bg-surface)] p-4"
      data-testid="linux-mic-gain-card"
    >
      <div className="flex items-center gap-2">
        <SlidersHorizontalIcon className="size-4 text-[var(--svx-color-brand-primary)]" />
        <h2 className="text-sm font-medium text-[var(--svx-color-text-primary)]">
          {t("voice:linuxMicGain.title")}
        </h2>
      </div>

      {loading ? (
        <p className="mt-3 flex items-center gap-2 text-xs text-[var(--svx-color-text-tertiary)]">
          <Loader2Icon className="size-3 animate-spin" />
          {t("voice:linuxMicGain.checking")}
        </p>
      ) : amixerMissing ? (
        <div
          className="mt-3 rounded-[var(--svx-radius-md)] border border-amber-500/40 bg-amber-500/10 p-3"
          data-testid="linux-mic-gain-amixer-missing"
        >
          <div className="flex items-start gap-2">
            <AlertTriangleIcon className="mt-0.5 size-4 shrink-0 text-amber-500" />
            <div className="flex-1 space-y-1">
              <p className="text-xs font-medium text-[var(--svx-color-text-primary)]">
                {t("voice:linuxMicGain.amixerMissingTitle")}
              </p>
              <p className="text-xs text-[var(--svx-color-text-secondary)]">
                {t("voice:linuxMicGain.amixerMissingBody")}
              </p>
            </div>
          </div>
        </div>
      ) : hasSaturation ? (
        <div
          className="mt-3 rounded-[var(--svx-radius-md)] border border-amber-500/40 bg-amber-500/10 p-3"
          data-testid="linux-mic-gain-alert"
        >
          <div className="flex items-start gap-2">
            <AlertTriangleIcon className="mt-0.5 size-4 shrink-0 text-amber-500" />
            <div className="flex-1 space-y-2">
              <p className="text-xs font-medium text-[var(--svx-color-text-primary)]">
                {t("voice:linuxMicGain.alertTitle")}
              </p>
              <p className="text-xs text-[var(--svx-color-text-secondary)]">
                {t("voice:linuxMicGain.alertBody")}
              </p>
              <ul className="space-y-1 text-[11px] text-[var(--svx-color-text-tertiary)]">
                {saturating.map((card) => (
                  <li key={card.card_index}>
                    <MicIcon className="mr-1 inline size-3" />
                    <span className="font-medium text-[var(--svx-color-text-secondary)]">
                      {card.card_longname || card.card_id}
                    </span>
                    {" — "}
                    {t("voice:linuxMicGain.cardBoost", {
                      boost: card.aggregated_boost_db.toFixed(1),
                      controls: card.controls
                        .filter((c) => c.saturation_risk)
                        .map((c) => c.name)
                        .join(", "),
                    })}
                  </li>
                ))}
              </ul>
              <Button
                size="sm"
                onClick={() => void handleReset()}
                disabled={applying}
                className="gap-2"
                data-testid="reset-linux-mic-gain-button"
              >
                {applying ? (
                  <Loader2Icon className="size-4 animate-spin" />
                ) : (
                  <WrenchIcon className="size-4" />
                )}
                {t("voice:linuxMicGain.resetButton")}
              </Button>
            </div>
          </div>
        </div>
      ) : (
        <div className="mt-3 flex items-center gap-2 text-xs text-[var(--svx-color-text-tertiary)]">
          <CheckCircle2Icon className="size-4 text-emerald-500" />
          <span>{t("voice:linuxMicGain.noIssues")}</span>
        </div>
      )}

      {resetApplied && !hasSaturation ? (
        <details
          className="mt-3 text-xs text-[var(--svx-color-text-tertiary)]"
          data-testid="linux-mic-gain-persist-hint"
        >
          <summary className="flex cursor-pointer items-center gap-1">
            <InfoIcon className="size-3" />
            {t("voice:linuxMicGain.persistHintSummary")}
          </summary>
          <p className="mt-2">
            {t("voice:linuxMicGain.persistHintBody")}
          </p>
          <pre className="mt-2 overflow-x-auto rounded-[var(--svx-radius-sm)] bg-[var(--svx-color-bg-muted)] p-2 font-mono text-[11px]">
            sudo alsactl store
          </pre>
        </details>
      ) : null}
    </section>
  );
}
