/**
 * _SlowPathProgress -- 5-10 min stage-by-stage timeline for the
 * SLOW_PATH branch.
 *
 * Renders the full diag + triage + apply states (slow_path_diag,
 * slow_path_calibrate, slow_path_apply). Long-running by design;
 * the timeline orientation makes the wait feel structured rather
 * than indeterminate.
 *
 * Subcomponent of VoiceCalibrationStep per spec §6.3 (T3.4 split).
 * History: introduced in v0.30.25 as the slow-path render branch.
 */

import { useTranslation } from "react-i18next";
import { CheckCircle2Icon, CircleIcon, LoaderIcon, XIcon } from "lucide-react";

import { Button } from "@/components/ui/button";

interface SlowPathProgressProps {
  /** Current calibration status from the orchestrator (raw enum value). */
  rawStatus: string;
  /** Localized status string from calibration.status.* */
  status: string;
  /** 0..100 percent for the progress bar. */
  progressPct: number;
  onCancel: () => void;
  cancelling: boolean;
}

const _STAGE_ORDER = ["slow_path_diag", "slow_path_calibrate", "slow_path_apply"];

export function SlowPathProgress({
  rawStatus,
  status,
  progressPct,
  onCancel,
  cancelling,
}: SlowPathProgressProps) {
  const { t } = useTranslation("voice");
  const currentStageIdx = _STAGE_ORDER.indexOf(rawStatus);
  return (
    <div
      className="space-y-4"
      data-testid="voice-calibration-slow-path-progress"
    >
      <div className="space-y-2">
        <p className="text-sm font-medium">{t("calibration.slow_path.title")}</p>
        <p className="text-xs text-muted-foreground">
          {t("calibration.slow_path.subtitle")}
        </p>
      </div>
      <ol className="space-y-2 text-sm">
        {_STAGE_ORDER.map((stage, idx) => {
          const isPast = currentStageIdx > idx;
          const isCurrent = currentStageIdx === idx;
          const stageKey = `calibration.slow_path.${stage.replace("slow_path_", "stage_")}`;
          return (
            <li
              key={stage}
              className={
                "flex items-center gap-2 " +
                (isCurrent
                  ? "text-foreground font-medium"
                  : isPast
                    ? "text-muted-foreground"
                    : "text-muted-foreground/60")
              }
            >
              {isPast ? (
                <CheckCircle2Icon className="size-4 text-green-600" />
              ) : isCurrent ? (
                <LoaderIcon className="size-4 animate-spin" />
              ) : (
                <CircleIcon className="size-4" />
              )}
              <span>{t(stageKey)}</span>
            </li>
          );
        })}
      </ol>
      <div className="space-y-2">
        <p className="text-sm">{status}</p>
        <div className="h-2 w-full overflow-hidden rounded-full bg-muted">
          <div
            className="h-full bg-primary transition-all duration-500"
            style={{ width: `${progressPct}%` }}
            role="progressbar"
            aria-valuenow={progressPct}
            aria-valuemin={0}
            aria-valuemax={100}
          />
        </div>
        <p className="text-xs text-muted-foreground">
          {progressPct}% {t("calibration.progress.suffix")}
        </p>
      </div>
      <Button
        variant="outline"
        size="sm"
        onClick={onCancel}
        disabled={cancelling}
        data-testid="voice-calibration-slow-cancel"
      >
        {cancelling ? (
          <LoaderIcon className="mr-2 size-4 animate-spin" />
        ) : (
          <XIcon className="mr-2 size-4" />
        )}
        {t("calibration.button.cancel")}
      </Button>
    </div>
  );
}
