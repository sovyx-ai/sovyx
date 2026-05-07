/**
 * RollbackButton -- Settings -> Voice surface for restoring the
 * most-recent prior calibration profile (rc.12).
 *
 * Pre-rc.12 the rollback was CLI-only (``sovyx doctor voice
 * --calibrate --rollback``). The rc.11 final-audit flagged this as
 * P3 operator-debt: non-technical operators have no way to recover
 * from a bad calibration without dropping to a terminal. rc.12 adds
 * this dashboard surface as the canonical operator path; the CLI
 * remains the headless fallback per ``feedback_canonical_setup_paths``.
 *
 * Operator clicks -> two-step confirm (avoids accidental rollback
 * of a working calibration) -> POST /api/voice/calibration/rollback
 * -> on HTTP 200, toast success + update remaining-generations
 * counter + refresh the calibration backups list.
 *
 * Backed by the rc.12 multi-generation backup chain (max 3 prior
 * profiles in ``<data_dir>/<mind_id>/calibration.json.bak.{1,2,3}``);
 * each click consumes one generation. When the chain is empty
 * (counter = 0), the button is disabled with a tooltip pointing at
 * the Recalibrate sibling.
 *
 * History: introduced in v0.31.0-rc.12 closing rc.11 final-audit
 * P1 + P3 operator-debt items.
 */

import { useCallback, useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import { Loader2Icon, RotateCcwIcon } from "lucide-react";
import { toast } from "sonner";

import { Button } from "@/components/ui/button";
import { useDashboardStore } from "@/stores/dashboard";

export function RollbackButton() {
  const { t } = useTranslation(["settings"]);
  const backupCount = useDashboardStore((s) => s.calibrationBackupCount);
  const loadBackups = useDashboardStore((s) => s.loadCalibrationBackups);
  const rollbackCalibration = useDashboardStore((s) => s.rollbackCalibration);
  const [busy, setBusy] = useState(false);
  const [confirming, setConfirming] = useState(false);

  // Load backup count on mount + after every rollback. Idempotent +
  // cheap (read-only enumeration).
  useEffect(() => {
    void loadBackups();
  }, [loadBackups]);

  const handleRollback = useCallback(async () => {
    setBusy(true);
    try {
      const result = await rollbackCalibration();
      if (result === null) {
        toast.error(t("settings:rollback.failed"));
        return;
      }
      toast.success(
        t("settings:rollback.success", {
          remaining: result.backup_generations_remaining,
        }),
      );
    } finally {
      setBusy(false);
      setConfirming(false);
    }
  }, [rollbackCalibration, t]);

  // Conservative gate: render disabled when the count hasn't loaded
  // yet (null) OR is zero. Operators see the surface but can't
  // accidentally trigger a 409.
  const enabled = typeof backupCount === "number" && backupCount > 0;
  const tooltip = !enabled ? t("settings:rollback.emptyChainTooltip") : undefined;

  return (
    <section
      data-testid="settings-rollback-card"
      className="rounded-[var(--svx-radius-lg)] border border-[var(--svx-color-border-default)] bg-[var(--svx-color-bg-surface)] p-4"
    >
      <header className="flex items-start gap-3">
        <RotateCcwIcon className="size-5 shrink-0 text-[var(--svx-color-text-secondary)]" />
        <div className="flex-1">
          <h2 className="text-sm font-semibold text-[var(--svx-color-text-primary)]">
            {t("settings:rollback.title")}
          </h2>
          <p className="mt-1 text-xs text-[var(--svx-color-text-tertiary)]">
            {t("settings:rollback.description")}
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
              data-testid="settings-rollback-cancel"
            >
              {t("settings:rollback.cancelButton")}
            </Button>
            <Button
              type="button"
              variant="default"
              disabled={busy}
              onClick={() => void handleRollback()}
              data-testid="settings-rollback-confirm"
            >
              {busy ? (
                <Loader2Icon className="size-4 animate-spin" />
              ) : (
                t("settings:rollback.confirmButton")
              )}
            </Button>
          </>
        ) : (
          <Button
            type="button"
            variant="outline"
            onClick={() => setConfirming(true)}
            disabled={!enabled}
            title={tooltip}
            aria-disabled={!enabled}
            data-testid="settings-rollback-toggle"
          >
            {t("settings:rollback.button")}
          </Button>
        )}
      </div>

      <p className="mt-3 text-[11px] text-[var(--svx-color-text-tertiary)]">
        {typeof backupCount === "number"
          ? t("settings:rollback.countNote", { count: backupCount })
          : t("settings:rollback.loadingNote")}
      </p>
    </section>
  );
}
