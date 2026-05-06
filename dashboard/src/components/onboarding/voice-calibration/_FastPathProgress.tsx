/**
 * _FastPathProgress -- ~5s polished progress for the FAST_PATH branch.
 *
 * Renders the cached-profile replay states (fast_path_lookup,
 * fast_path_apply, fast_path_validate). Distinct visual treatment
 * from _SlowPathProgress because the operator is waiting for ~5
 * seconds, not 8-12 minutes -- a tighter spinner + single-line
 * status reads better than a multi-stage timeline.
 *
 * Subcomponent of VoiceCalibrationStep per spec §6.3 (T3.4 split).
 * History: introduced in v0.30.25 as the fast-path render branch.
 */

import { useTranslation } from "react-i18next";
import { LoaderIcon, XIcon } from "lucide-react";

import { Button } from "@/components/ui/button";

interface FastPathProgressProps {
  /** Localized status string from calibration.status.* */
  status: string;
  /** 0..100 percent for the progress bar. */
  progressPct: number;
  onCancel: () => void;
  cancelling: boolean;
}

export function FastPathProgress({
  status,
  progressPct,
  onCancel,
  cancelling,
}: FastPathProgressProps) {
  const { t } = useTranslation("voice");
  return (
    <div
      className="space-y-4"
      data-testid="voice-calibration-fast-path-progress"
    >
      <div className="space-y-2">
        <p className="text-sm font-medium">{t("calibration.fast_path.title")}</p>
        <p className="text-xs text-muted-foreground">
          {t("calibration.fast_path.subtitle")}
        </p>
      </div>
      <div className="space-y-2">
        <p className="text-sm">{status}</p>
        <div className="h-2 w-full overflow-hidden rounded-full bg-muted">
          <div
            className="h-full bg-primary transition-all duration-300"
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
        data-testid="voice-calibration-fast-cancel"
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
