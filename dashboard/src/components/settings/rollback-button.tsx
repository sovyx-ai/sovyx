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

import { useCallback, useEffect, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import { Loader2Icon, RotateCcwIcon } from "lucide-react";
import { toast } from "sonner";

import { Button } from "@/components/ui/button";
import { useDashboardStore } from "@/stores/dashboard";
import { isWizardCalibrationTerminal } from "@/types/api";

// rc.15 LOW.4 retry-on-failure delay. Module-level constant so tests
// can read it (and assert it stays bounded) without monkey-patching.
const _RETRY_DELAY_MS = 1500;

export function RollbackButton() {
  const { t } = useTranslation(["settings"]);
  const backupCount = useDashboardStore((s) => s.calibrationBackupCount);
  const currentJob = useDashboardStore((s) => s.currentCalibrationJob);
  const loadBackups = useDashboardStore((s) => s.loadCalibrationBackups);
  const rollbackCalibration = useDashboardStore((s) => s.rollbackCalibration);
  // v0.31.2 F4: closes the audit gap where RollbackButton was the only
  // calibration surface NOT gating on ``platform_supported``. Same bug
  // class as rc.11 EIXO 2 (VoiceStep) / rc.13 (RecalibrateButton) /
  // rc.14 (CalibrationWizardCard) — the prior closure protocol stopped
  // at three siblings and missed this fourth. Defense-in-depth: backend
  // ``list_calibration_backups_endpoint`` also refuses on non-Linux,
  // so even if this gate is ever accidentally weakened, the operator
  // still sees ``backupCount=0`` and the button stays disabled.
  const featureFlag = useDashboardStore((s) => s.calibrationFeatureFlag);
  const platformSupported = featureFlag?.platform_supported ?? true;
  const [busy, setBusy] = useState(false);
  const [confirming, setConfirming] = useState(false);
  const retriedRef = useRef(false);

  // Load backup count on mount. Idempotent + cheap (read-only
  // enumeration).
  useEffect(() => {
    void loadBackups();
  }, [loadBackups]);

  // rc.15 LOW.1 — auto-refresh the backup count when a calibration
  // run reaches a terminal state. Pre-rc.15 the count loaded only at
  // mount, so an operator who clicked Recalibrate (8-12 min) and then
  // came back to the Rollback button saw a stale count (didn't include
  // the just-created .bak.1 from the new save). Now: when
  // currentCalibrationJob.status flips to terminal, re-fetch backups.
  // Only fires on the LEADING edge of terminal status (the dependency
  // array is the snapshot's job_id + status; identical snapshots
  // don't re-trigger).
  useEffect(() => {
    if (currentJob === null) return;
    if (!isWizardCalibrationTerminal(currentJob.status)) return;
    void loadBackups();
  }, [currentJob?.job_id, currentJob?.status, loadBackups]);

  // rc.15 LOW.4 — single retry on initial-mount load failure. The
  // api.get layer already retries 429/503/5xx; this catch covers the
  // dashboard-load-races where the daemon is starting up and returns
  // 4xx-but-eventually-recovers, OR a brief network blip on first
  // load. The ref guards against infinite-retry loops on persistent
  // failure (operator can refresh the page if needed). 1500ms is
  // long enough to survive transient blips and short enough to feel
  // responsive on a daemon that comes up cleanly.
  useEffect(() => {
    if (backupCount !== null || retriedRef.current) return;
    const id = setTimeout(() => {
      retriedRef.current = true;
      void loadBackups();
    }, _RETRY_DELAY_MS);
    return () => clearTimeout(id);
  }, [backupCount, loadBackups]);

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
  // yet (null), is zero, OR the platform doesn't support calibration
  // (Win/macOS). Operators see the surface but can't accidentally
  // trigger a 409. The platform check uses ?? true so legacy
  // (pre-rc.11) zod schemas without the field fall through to
  // legacy single-platform behaviour.
  const enabled =
    platformSupported && typeof backupCount === "number" && backupCount > 0;
  const tooltip = (() => {
    if (!platformSupported) {
      return t("settings:rollback.platformUnsupportedTooltip");
    }
    if (!enabled) {
      return t("settings:rollback.emptyChainTooltip");
    }
    return undefined;
  })();

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
