/**
 * VoiceClarityCard — Windows capture-APO health + one-click exclusive mode.
 *
 * Polls `GET /api/voice/capture-diagnostics` once on mount; if Windows
 * Voice Clarity is active on the live microphone, renders a warning
 * card with an "Enable exclusive mode" button that POSTs
 * `/api/voice/capture-exclusive` to persist + hot-apply the bypass.
 *
 * Hidden on platforms where the detector returns no endpoints (Linux,
 * macOS) so non-Windows users don't see an irrelevant diagnostic.
 */

import { useCallback, useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import {
  AlertTriangleIcon,
  CheckCircle2Icon,
  Loader2Icon,
  MicIcon,
  ShieldCheckIcon,
} from "lucide-react";
import { toast } from "sonner";

import { Button } from "@/components/ui/button";
import { api, isAbortError } from "@/lib/api";
import {
  CaptureDiagnosticsResponseSchema,
  CaptureExclusiveResponseSchema,
} from "@/types/schemas";
import type {
  CaptureDiagnosticsResponse,
  CaptureExclusiveResponse,
} from "@/types/api";

export function VoiceClarityCard() {
  const { t } = useTranslation(["settings"]);
  const [diag, setDiag] = useState<CaptureDiagnosticsResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [applying, setApplying] = useState(false);

  const load = useCallback(async (signal?: AbortSignal) => {
    try {
      const data = await api.get<CaptureDiagnosticsResponse>(
        "/api/voice/capture-diagnostics",
        { signal, schema: CaptureDiagnosticsResponseSchema },
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

  const handleToggleExclusive = useCallback(
    async (enabled: boolean) => {
      setApplying(true);
      try {
        const resp = await api.post<CaptureExclusiveResponse>(
          "/api/voice/capture-exclusive",
          { enabled },
          { schema: CaptureExclusiveResponseSchema },
        );
        if (enabled && resp.applied_immediately) {
          toast.success(t("settings:voiceClarity.enableSuccess"));
        } else if (enabled) {
          toast.success(t("settings:voiceClarity.enableSuccessRestart"));
        } else {
          toast.success(t("settings:voiceClarity.disableSuccess"));
        }
        await load();
      } catch (err) {
        const msg = err instanceof Error ? err.message : "Request failed";
        toast.error(t("settings:voiceClarity.failed", { error: msg }));
      } finally {
        setApplying(false);
      }
    },
    [load, t],
  );

  // Hide entirely on platforms with no endpoint data (non-Windows).
  if (!loading && diag && diag.endpoints.length === 0) {
    return null;
  }

  const clarityActive = diag?.voice_clarity_active ?? false;
  const activeDevice = diag?.active_device_name ?? diag?.active_endpoint?.endpoint_name ?? null;
  const knownApos = diag?.active_endpoint?.known_apos ?? [];

  return (
    <section
      className="rounded-[var(--svx-radius-lg)] border border-[var(--svx-color-border-default)] bg-[var(--svx-color-bg-surface)] p-4"
      data-testid="voice-clarity-card"
    >
      <div className="flex items-center gap-2">
        <MicIcon className="size-4 text-[var(--svx-color-brand-primary)]" />
        <h2 className="text-sm font-medium text-[var(--svx-color-text-primary)]">
          {t("settings:voiceClarity.title")}
        </h2>
      </div>

      {loading ? (
        <p className="mt-3 flex items-center gap-2 text-xs text-[var(--svx-color-text-tertiary)]">
          <Loader2Icon className="size-3 animate-spin" />
          {t("settings:voiceClarity.checking")}
        </p>
      ) : clarityActive ? (
        <div
          className="mt-3 rounded-[var(--svx-radius-md)] border border-amber-500/40 bg-amber-500/10 p-3"
          data-testid="voice-clarity-alert"
        >
          <div className="flex items-start gap-2">
            <AlertTriangleIcon className="mt-0.5 size-4 shrink-0 text-amber-500" />
            <div className="flex-1 space-y-2">
              <p className="text-xs font-medium text-[var(--svx-color-text-primary)]">
                {t("settings:voiceClarity.alertTitle")}
              </p>
              <p className="text-xs text-[var(--svx-color-text-secondary)]">
                {t("settings:voiceClarity.alertBody", {
                  device: activeDevice ?? "?",
                })}
              </p>
              {knownApos.length > 0 && (
                <p className="text-[10px] text-[var(--svx-color-text-tertiary)]">
                  {t("settings:voiceClarity.knownApos", {
                    list: knownApos.join(", "),
                  })}
                </p>
              )}
              <Button
                size="sm"
                onClick={() => void handleToggleExclusive(true)}
                disabled={applying}
                className="gap-2"
                data-testid="enable-exclusive-button"
              >
                {applying ? (
                  <Loader2Icon className="size-4 animate-spin" />
                ) : (
                  <ShieldCheckIcon className="size-4" />
                )}
                {t("settings:voiceClarity.enableExclusive")}
              </Button>
            </div>
          </div>
        </div>
      ) : (
        <div className="mt-3 flex items-center gap-2 text-xs text-[var(--svx-color-text-tertiary)]">
          <CheckCircle2Icon className="size-4 text-emerald-500" />
          <span>{t("settings:voiceClarity.noIssues")}</span>
        </div>
      )}

      {!loading && diag && activeDevice && !clarityActive && diag.endpoints.length > 0 && (
        <p className="mt-2 text-[10px] text-[var(--svx-color-text-disabled)]">
          {t("settings:voiceClarity.activeDevice", { device: activeDevice })}
        </p>
      )}
    </section>
  );
}
