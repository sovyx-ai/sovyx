/**
 * _ProfileReview -- terminal-state DONE render for the calibration
 * pipeline.
 *
 * Replaces the inline "done" branch of the prior monolithic
 * TerminalView. Surfaces the operator-actionable summary:
 *
 * * The detected hypothesis (triage_winner_hid) when one was crowned;
 * * The persisted profile path so operators can locate + audit the
 *   serialized state on disk;
 * * A localized explanation that points at the CLI commands the
 *   operator can run for deeper inspection (`--show`) or to undo
 *   the apply (`--rollback`); rollback is NOT a UI button — the
 *   `calibration.review.decision_explanation` i18n string carries
 *   the CLI breadcrumb. rc.7 (Agent 2 NEW.4) closed the prior gap
 *   where the i18n promised an in-UI rollback affordance that
 *   didn't exist.
 * * The continue button advancing the onboarding flow.
 *
 * Subcomponent of VoiceCalibrationStep per spec §6.3 (T3.4 split).
 * History: introduced in v0.30.25; rc.7 docstring synced with the
 * actual rendered surface (no rollback button — CLI command only).
 */

import { useTranslation } from "react-i18next";
import { CheckCircle2Icon } from "lucide-react";

import { Button } from "@/components/ui/button";

interface ProfileReviewProps {
  triageWinnerHid: string | null;
  profilePath: string | null;
  onCompleted: () => void;
}

export function ProfileReview({
  triageWinnerHid,
  profilePath,
  onCompleted,
}: ProfileReviewProps) {
  const { t } = useTranslation("voice");
  return (
    <div className="space-y-4" data-testid="voice-calibration-profile-review">
      <div className="flex items-start gap-2 rounded-md border border-green-200 bg-green-50 p-3 text-sm text-green-900">
        <CheckCircle2Icon className="size-5 flex-shrink-0 mt-0.5" />
        <div className="space-y-1">
          <p className="font-medium">{t("calibration.terminal.done.title")}</p>
          {triageWinnerHid !== null && (
            <p className="text-xs">
              {t("calibration.terminal.done.winner", { hid: triageWinnerHid })}
            </p>
          )}
          {profilePath !== null && (
            <p className="text-xs font-mono break-all">{profilePath}</p>
          )}
        </div>
      </div>
      <div className="rounded-md border bg-background/50 p-3 text-xs text-muted-foreground">
        <p className="font-medium text-foreground">
          {t("calibration.review.title")}
        </p>
        <p className="mt-1">{t("calibration.review.decision_explanation")}</p>
      </div>
      <Button onClick={onCompleted} size="lg">
        {t("calibration.review.confirm")}
      </Button>
    </div>
  );
}
