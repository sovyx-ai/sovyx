/**
 * _CancelDialog -- inline confirmation row that fires before the
 * orchestrator's POST /cancel call.
 *
 * Replaces the bare "Cancel" button click with a two-step
 * confirmation: clicking once shows two buttons (Confirm + Dismiss);
 * confirming triggers the actual cancel. Prevents accidental aborts
 * during the 8-12 minute slow-path run where one click on the wrong
 * button costs the operator the full diag investment.
 *
 * Subcomponent of VoiceCalibrationStep per spec §6.3 (T3.4 split).
 * History: introduced in v0.30.25.
 */

import { useTranslation } from "react-i18next";
import { LoaderIcon, XIcon } from "lucide-react";

import { Button } from "@/components/ui/button";

interface CancelDialogProps {
  /** True while the cancel request is in flight. */
  cancelling: boolean;
  onConfirm: () => void;
  onDismiss: () => void;
}

export function CancelDialog({ cancelling, onConfirm, onDismiss }: CancelDialogProps) {
  const { t } = useTranslation("voice");
  return (
    <div
      className="space-y-2 rounded-md border border-amber-200 bg-amber-50 p-3 text-sm text-amber-900"
      data-testid="voice-calibration-cancel-dialog"
      role="alertdialog"
      aria-labelledby="voice-calibration-cancel-title"
    >
      <p id="voice-calibration-cancel-title" className="font-medium">
        {t("calibration.cancel.title")}
      </p>
      <div className="flex gap-2 justify-end">
        <Button
          type="button"
          size="sm"
          variant="ghost"
          onClick={onDismiss}
          disabled={cancelling}
          data-testid="voice-calibration-cancel-dismiss"
        >
          {t("calibration.cancel.cancel")}
        </Button>
        <Button
          type="button"
          size="sm"
          variant="outline"
          onClick={onConfirm}
          disabled={cancelling}
          data-testid="voice-calibration-cancel-confirm"
        >
          {cancelling ? (
            <LoaderIcon className="mr-2 size-4 animate-spin" />
          ) : (
            <XIcon className="mr-2 size-4" />
          )}
          {t("calibration.cancel.confirm")}
        </Button>
      </div>
    </div>
  );
}
