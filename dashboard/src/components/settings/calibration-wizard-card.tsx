/**
 * CalibrationWizardCard -- Settings -> Voice -> Advanced toggle for the
 * onboarding calibration wizard mount flag.
 *
 * Reads the runtime feature-flag state from the Zustand calibration
 * slice (which mirrors `EngineConfig.voice.calibration_wizard_enabled`
 * on the running daemon). Operator clicks toggle the in-memory copy
 * via `POST /api/voice/calibration/feature-flag`; persistent change
 * still requires editing env / system.yaml + daemon restart.
 *
 * History: introduced in v0.30.22 as T3.10 wire-up of mission
 * `MISSION-voice-self-calibrating-system-2026-05-05.md` Layer 3.
 */

import { useCallback, useEffect } from "react";
import { useTranslation } from "react-i18next";
import { InfoIcon, Loader2Icon, SlidersIcon } from "lucide-react";
import { toast } from "sonner";

import { Button } from "@/components/ui/button";
import { useDashboardStore } from "@/stores/dashboard";

export function CalibrationWizardCard() {
  const { t } = useTranslation(["settings"]);
  const flag = useDashboardStore((s) => s.calibrationFeatureFlag);
  const loading = useDashboardStore((s) => s.calibrationLoading);
  const error = useDashboardStore((s) => s.calibrationError);
  const load = useDashboardStore((s) => s.loadCalibrationFeatureFlag);
  const setEnabled = useDashboardStore((s) => s.setCalibrationFeatureFlag);

  // Load on mount; idempotent so safe even if VoiceStep already loaded it.
  useEffect(() => {
    void load();
  }, [load]);

  const handleToggle = useCallback(
    async (newValue: boolean) => {
      const result = await setEnabled(newValue);
      if (result === null) {
        toast.error(error ?? t("settings:calibrationWizard.toggleError"));
        return;
      }
      toast.success(
        newValue
          ? t("settings:calibrationWizard.enabledToast")
          : t("settings:calibrationWizard.disabledToast"),
      );
    },
    [setEnabled, error, t],
  );

  const enabled = flag?.enabled ?? false;
  const overrideActive = flag?.runtime_override_active ?? false;
  // rc.14 (closes the bug class — same lesson as rc.11/rc.13): the
  // wizard mount + Recalibrate button surfaces both gate on
  // ``platform_supported``, but pre-rc.14 this CARD did not. Result:
  // a Win/macOS operator saw status "Enabled" + a clickable toggle
  // here while every downstream surface was actually disabled
  // because of platform — the card LIED about state. Now: when
  // ``platform_supported`` is false, the toggle is disabled with a
  // Linux-only tooltip + the status badge says
  // ``statusPlatformUnsupported`` instead of ``statusEnabled``.
  // Pre-rc.12 daemons that don't ship the field default to True via
  // the zod schema, preserving legacy single-platform behaviour.
  const platformSupported = flag?.platform_supported ?? true;

  return (
    <section
      data-testid="settings-calibration-wizard-card"
      className="rounded-[var(--svx-radius-lg)] border border-[var(--svx-color-border-default)] bg-[var(--svx-color-bg-surface)] p-4"
    >
      <header className="flex items-start gap-3">
        <SlidersIcon className="size-5 shrink-0 text-[var(--svx-color-text-secondary)]" />
        <div className="flex-1">
          <h2 className="text-sm font-semibold text-[var(--svx-color-text-primary)]">
            {t("settings:calibrationWizard.title")}
          </h2>
          <p className="mt-1 text-xs text-[var(--svx-color-text-tertiary)]">
            {t("settings:calibrationWizard.description")}
          </p>
        </div>
      </header>

      <div className="mt-4 flex items-center justify-between gap-4">
        <div className="text-xs text-[var(--svx-color-text-tertiary)]">
          <span className="font-medium text-[var(--svx-color-text-primary)]">
            {!platformSupported
              ? t("settings:calibrationWizard.statusPlatformUnsupported")
              : enabled
                ? t("settings:calibrationWizard.statusEnabled")
                : t("settings:calibrationWizard.statusDisabled")}
          </span>
          {overrideActive && (
            <span className="ml-2 inline-flex items-center gap-1 text-[var(--svx-color-status-warning)]">
              <InfoIcon className="size-3" />
              {t("settings:calibrationWizard.runtimeOverride")}
            </span>
          )}
        </div>
        <Button
          type="button"
          variant={enabled ? "outline" : "default"}
          disabled={loading || flag === null || !platformSupported}
          onClick={() => void handleToggle(!enabled)}
          title={
            !platformSupported
              ? t("settings:calibrationWizard.platformUnsupportedTooltip")
              : undefined
          }
          aria-disabled={loading || flag === null || !platformSupported}
          data-testid="settings-calibration-wizard-toggle"
        >
          {loading ? (
            <Loader2Icon className="size-4 animate-spin" />
          ) : enabled ? (
            t("settings:calibrationWizard.disableButton")
          ) : (
            t("settings:calibrationWizard.enableButton")
          )}
        </Button>
      </div>

      <p className="mt-3 text-[11px] text-[var(--svx-color-text-tertiary)]">
        {!platformSupported
          ? t("settings:calibrationWizard.platformUnsupportedNote")
          : t("settings:calibrationWizard.persistenceNote")}
      </p>
    </section>
  );
}
