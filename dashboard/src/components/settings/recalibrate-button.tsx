/**
 * RecalibrateButton -- Settings -> Voice surface for triggering a
 * fresh calibration run on the current daemon (per spec §8.5).
 *
 * Operator clicks -> confirm dialog -> POST /api/voice/calibration/start
 * with mind_id="default" -> on HTTP 202, emit a toast pointing the
 * operator at the onboarding wizard step (where progress is rendered).
 *
 * Returns 409 Conflict when a calibration is already in flight; the
 * UI surfaces the conflict gracefully without queuing.
 *
 * History: introduced in v0.30.24 as T3.10/§8.5 wire-up of mission
 * `MISSION-voice-self-calibrating-system-2026-05-05.md`.
 */

import { useCallback, useState } from "react";
import { useTranslation } from "react-i18next";
import { Loader2Icon, RefreshCcwIcon } from "lucide-react";
import { toast } from "sonner";

import { Button } from "@/components/ui/button";
import { useDashboardStore } from "@/stores/dashboard";

export function RecalibrateButton({ mindId = "default" }: { mindId?: string }) {
  const { t } = useTranslation(["settings"]);
  const startCalibration = useDashboardStore((s) => s.startCalibration);
  const featureFlag = useDashboardStore((s) => s.calibrationFeatureFlag);
  const [busy, setBusy] = useState(false);
  const [confirming, setConfirming] = useState(false);

  const handleStart = useCallback(async () => {
    setBusy(true);
    try {
      const jobId = await startCalibration({ mind_id: mindId });
      if (jobId !== null) {
        toast.success(t("settings:recalibrate.startSuccess"));
      } else {
        toast.error(t("settings:recalibrate.startFailed"));
      }
    } finally {
      setBusy(false);
      setConfirming(false);
    }
  }, [startCalibration, mindId, t]);

  // P6 (v0.30.34) — Mission §10.2 #12: the recalibrate button stays
  // visible regardless of the wizard-mount flag. When the flag is
  // OFF the button is disabled with a tooltip pointing at the
  // CalibrationWizardCard sibling toggle, so operators see the
  // surface exists + understand what gates it (instead of guessing
  // why a section disappeared).
  const flagEnabled = featureFlag !== null && featureFlag.enabled;
  const flagOffTooltip = !flagEnabled ? t("settings:recalibrate.flagOffTooltip") : undefined;

  return (
    <section
      data-testid="settings-recalibrate-card"
      className="rounded-[var(--svx-radius-lg)] border border-[var(--svx-color-border-default)] bg-[var(--svx-color-bg-surface)] p-4"
    >
      <header className="flex items-start gap-3">
        <RefreshCcwIcon className="size-5 shrink-0 text-[var(--svx-color-text-secondary)]" />
        <div className="flex-1">
          <h2 className="text-sm font-semibold text-[var(--svx-color-text-primary)]">
            {t("settings:recalibrate.title")}
          </h2>
          <p className="mt-1 text-xs text-[var(--svx-color-text-tertiary)]">
            {t("settings:recalibrate.description")}
          </p>
        </div>
      </header>

      <div className="mt-4 flex items-center justify-end gap-2">
        {confirming ? (
          <>
            <Button
              type="button"
              variant="ghost"
              onClick={() => setConfirming(false)}
              data-testid="settings-recalibrate-cancel"
            >
              {t("settings:recalibrate.cancelButton")}
            </Button>
            <Button
              type="button"
              variant="default"
              disabled={busy}
              onClick={() => void handleStart()}
              data-testid="settings-recalibrate-confirm"
            >
              {busy ? (
                <Loader2Icon className="size-4 animate-spin" />
              ) : (
                t("settings:recalibrate.confirmButton")
              )}
            </Button>
          </>
        ) : (
          <Button
            type="button"
            variant="outline"
            onClick={() => setConfirming(true)}
            disabled={!flagEnabled}
            title={flagOffTooltip}
            aria-disabled={!flagEnabled}
            data-testid="settings-recalibrate-toggle"
          >
            {t("settings:recalibrate.button")}
          </Button>
        )}
      </div>

      <p className="mt-3 text-[11px] text-[var(--svx-color-text-tertiary)]">
        {flagEnabled
          ? t("settings:recalibrate.note")
          : t("settings:recalibrate.flagOffNote")}
      </p>
    </section>
  );
}
