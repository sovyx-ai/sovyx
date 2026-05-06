/**
 * Voice calibration wizard step (L3 frontend wiring).
 *
 * Mission: MISSION-voice-self-calibrating-system-2026-05-05.md Layer 3
 * v0.30.17 patch 4. Mounts inside VoiceStep behind the
 * CALIBRATION_WIZARD_ENABLED feature flag (default-false).
 *
 * UX flow (v0.30.17 alpha -- always SLOW_PATH; FAST_PATH KB lookup
 * lands in v0.30.18+):
 *
 *   1. Capture fingerprint via /preview-fingerprint (~1s)
 *   2. Operator clicks "Start calibration"
 *   3. POST /start -> spawn job in backend, subscribe to WS
 *   4. Render progress bar + status message + cancel button while
 *      the orchestrator runs through PROBING -> SLOW_PATH_DIAG ->
 *      SLOW_PATH_CALIBRATE -> SLOW_PATH_APPLY (8-12 min total)
 *   5. Terminal:
 *      - done    -> success view + "Continue" -> onCompleted()
 *      - failed  -> error view + "Use simple setup" -> onFallback()
 *      - fallback -> banner explaining + "Use simple setup" -> onFallback()
 *      - cancelled -> notice + "Try again" -> reset
 *
 * The component subscribes to the WS on start and unsubscribes on
 * unmount (or when the WS closes). Polling fetchCalibrationJob is
 * used as a fallback if the WS fails.
 */
import { useCallback, useEffect, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import {
  AlertCircleIcon,
  CheckCircle2Icon,
  LoaderIcon,
  MicIcon,
  XIcon,
} from "lucide-react";

import { Button } from "@/components/ui/button";
import { useDashboardStore } from "@/stores/dashboard";
import { isWizardCalibrationTerminal } from "@/types/api";

interface VoiceCalibrationStepProps {
  /** Mind whose calibration to compute. */
  mindId: string;
  /**
   * Called when the calibration reaches DONE terminal state and the
   * operator clicks "Continue". The parent VoiceStep advances the
   * onboarding wizard.
   */
  onCompleted: () => void;
  /**
   * Called when the calibration reaches FAILED / FALLBACK terminal
   * state OR the operator clicks "Use simple setup". The parent
   * VoiceStep falls back to the v0.30.x device-test wizard.
   */
  onFallback: () => void;
}

export function VoiceCalibrationStep({
  mindId,
  onCompleted,
  onFallback,
}: VoiceCalibrationStepProps) {
  const { t } = useTranslation("voice");

  const calibrationPreview = useDashboardStore((s) => s.calibrationPreview);
  const currentJob = useDashboardStore((s) => s.currentCalibrationJob);
  const calibrationLoading = useDashboardStore((s) => s.calibrationLoading);
  const calibrationError = useDashboardStore((s) => s.calibrationError);
  const fetchCalibrationPreview = useDashboardStore(
    (s) => s.fetchCalibrationPreview,
  );
  const startCalibration = useDashboardStore((s) => s.startCalibration);
  const cancelCalibrationJob = useDashboardStore((s) => s.cancelCalibrationJob);
  const subscribeToCalibrationJob = useDashboardStore(
    (s) => s.subscribeToCalibrationJob,
  );
  const unsubscribeFromCalibrationJob = useDashboardStore(
    (s) => s.unsubscribeFromCalibrationJob,
  );
  const clearCalibrationError = useDashboardStore(
    (s) => s.clearCalibrationError,
  );

  const [phase, setPhase] = useState<"idle" | "running" | "terminal">("idle");
  const [cancelling, setCancelling] = useState(false);
  // Remember the active job id so cancellation + cleanup target the
  // right job even if the operator navigates away briefly.
  const activeJobIdRef = useRef<string | null>(null);

  // Capture the preview fingerprint on first mount so the operator
  // sees "we detected your hardware" before clicking Start.
  useEffect(() => {
    if (calibrationPreview === null) {
      void fetchCalibrationPreview();
    }
  }, [calibrationPreview, fetchCalibrationPreview]);

  // When the job reaches a terminal state, transition the local
  // phase + clean up the WS subscription.
  useEffect(() => {
    if (currentJob === null) return;
    if (isWizardCalibrationTerminal(currentJob.status)) {
      setPhase("terminal");
      // The server closes the WS on terminal; this is defensive cleanup.
      unsubscribeFromCalibrationJob();
    }
  }, [currentJob, unsubscribeFromCalibrationJob]);

  // Always unsubscribe on unmount.
  useEffect(() => {
    return () => {
      unsubscribeFromCalibrationJob();
    };
  }, [unsubscribeFromCalibrationJob]);

  const handleStart = useCallback(async () => {
    clearCalibrationError();
    const jobId = await startCalibration({ mind_id: mindId });
    if (jobId === null) {
      // Error already populated in calibrationError; phase stays idle.
      return;
    }
    activeJobIdRef.current = jobId;
    setPhase("running");
    subscribeToCalibrationJob(jobId);
  }, [
    clearCalibrationError,
    mindId,
    startCalibration,
    subscribeToCalibrationJob,
  ]);

  const handleCancel = useCallback(async () => {
    const jobId = activeJobIdRef.current;
    if (jobId === null) return;
    setCancelling(true);
    await cancelCalibrationJob(jobId);
    setCancelling(false);
    // The orchestrator will emit CANCELLED at the next checkpoint;
    // the WS handler updates currentJob.status accordingly.
  }, [cancelCalibrationJob]);

  const handleRetry = useCallback(() => {
    activeJobIdRef.current = null;
    clearCalibrationError();
    setPhase("idle");
    // currentCalibrationJob is left in place so the operator sees
    // the prior terminal state in the UI history; will be replaced
    // when they Start again.
  }, [clearCalibrationError]);

  // ── Render ──

  const progressPct = currentJob ? Math.round(currentJob.progress * 100) : 0;
  const statusKey = currentJob ? `calibration.status.${currentJob.status}` : "";
  // i18n returns the key when missing -> fall back to current_stage_message.
  const localizedStatus = currentJob
    ? (t(statusKey, { defaultValue: currentJob.current_stage_message }) as string)
    : "";

  return (
    <div className="space-y-4 rounded-lg border bg-background p-6">
      <div className="flex items-center gap-2">
        <MicIcon className="size-5" />
        <h3 className="text-lg font-semibold">{t("calibration.title")}</h3>
      </div>

      {phase === "idle" && (
        <IdleView
          preview={calibrationPreview}
          onStart={handleStart}
          loading={calibrationLoading}
          t={t}
        />
      )}

      {phase === "running" && currentJob !== null && (
        <RunningView
          status={localizedStatus}
          progressPct={progressPct}
          onCancel={handleCancel}
          cancelling={cancelling}
          t={t}
        />
      )}

      {phase === "terminal" && currentJob !== null && (
        <TerminalView
          status={currentJob.status}
          errorSummary={currentJob.error_summary}
          fallbackReason={currentJob.fallback_reason}
          profilePath={currentJob.profile_path}
          triageWinnerHid={currentJob.triage_winner_hid}
          onCompleted={onCompleted}
          onFallback={onFallback}
          onRetry={handleRetry}
          t={t}
        />
      )}

      {calibrationError !== null && phase === "idle" && (
        <div className="flex items-start gap-2 rounded-md border border-red-200 bg-red-50 p-3 text-sm text-red-900">
          <AlertCircleIcon className="size-4 flex-shrink-0 mt-0.5" />
          <span>{calibrationError}</span>
        </div>
      )}
    </div>
  );
}

// ── Subcomponents ──

interface IdleViewProps {
  preview: ReturnType<typeof useDashboardStore.getState>["calibrationPreview"];
  onStart: () => void;
  loading: boolean;
  t: ReturnType<typeof useTranslation>["t"];
}

function IdleView({ preview, onStart, loading, t }: IdleViewProps) {
  return (
    <div className="space-y-3">
      <p className="text-sm text-muted-foreground">{t("calibration.subtitle")}</p>
      {preview !== null && (
        <div className="rounded-md bg-muted/40 p-3 text-xs space-y-1">
          <p>
            <span className="font-medium">{t("calibration.detected.system")}:</span>{" "}
            {preview.system_vendor} {preview.system_product}
          </p>
          <p>
            <span className="font-medium">{t("calibration.detected.audio_stack")}:</span>{" "}
            {preview.audio_stack || "—"}
          </p>
        </div>
      )}
      <Button onClick={onStart} disabled={loading} size="lg">
        {loading ? (
          <LoaderIcon className="mr-2 size-4 animate-spin" />
        ) : (
          <MicIcon className="mr-2 size-4" />
        )}
        {t("calibration.button.start")}
      </Button>
      <p className="text-xs text-muted-foreground">
        {t("calibration.estimated_duration")}
      </p>
    </div>
  );
}

interface RunningViewProps {
  status: string;
  progressPct: number;
  onCancel: () => void;
  cancelling: boolean;
  t: ReturnType<typeof useTranslation>["t"];
}

function RunningView({
  status,
  progressPct,
  onCancel,
  cancelling,
  t,
}: RunningViewProps) {
  return (
    <div className="space-y-4">
      <div className="space-y-2">
        <p className="text-sm font-medium">{status}</p>
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

interface TerminalViewProps {
  status: string;
  errorSummary: string | null;
  fallbackReason: string | null;
  profilePath: string | null;
  triageWinnerHid: string | null;
  onCompleted: () => void;
  onFallback: () => void;
  onRetry: () => void;
  t: ReturnType<typeof useTranslation>["t"];
}

function TerminalView({
  status,
  errorSummary,
  fallbackReason,
  profilePath,
  triageWinnerHid,
  onCompleted,
  onFallback,
  onRetry,
  t,
}: TerminalViewProps) {
  if (status === "done") {
    return (
      <div className="space-y-4">
        <div className="flex items-start gap-2 rounded-md border border-green-200 bg-green-50 p-3 text-sm text-green-900">
          <CheckCircle2Icon className="size-5 flex-shrink-0 mt-0.5" />
          <div className="space-y-1">
            <p className="font-medium">{t("calibration.terminal.done.title")}</p>
            {triageWinnerHid !== null && (
              <p className="text-xs">
                {t("calibration.terminal.done.winner", {
                  hid: triageWinnerHid,
                })}
              </p>
            )}
            {profilePath !== null && (
              <p className="text-xs font-mono break-all">{profilePath}</p>
            )}
          </div>
        </div>
        <Button onClick={onCompleted} size="lg">
          {t("calibration.button.continue")}
        </Button>
      </div>
    );
  }

  if (status === "fallback") {
    return (
      <div className="space-y-4">
        <div className="flex items-start gap-2 rounded-md border border-amber-200 bg-amber-50 p-3 text-sm text-amber-900">
          <AlertCircleIcon className="size-5 flex-shrink-0 mt-0.5" />
          <div className="space-y-1">
            <p className="font-medium">{t("calibration.terminal.fallback.title")}</p>
            <p className="text-xs">
              {t("calibration.terminal.fallback.subtitle", {
                reason: fallbackReason ?? "—",
              })}
            </p>
          </div>
        </div>
        <Button onClick={onFallback} size="lg" variant="outline">
          {t("calibration.button.use_simple_setup")}
        </Button>
      </div>
    );
  }

  if (status === "failed") {
    return (
      <div className="space-y-4">
        <div className="flex items-start gap-2 rounded-md border border-red-200 bg-red-50 p-3 text-sm text-red-900">
          <AlertCircleIcon className="size-5 flex-shrink-0 mt-0.5" />
          <div className="space-y-1">
            <p className="font-medium">{t("calibration.terminal.failed.title")}</p>
            {errorSummary !== null && (
              <p className="text-xs font-mono break-all">{errorSummary}</p>
            )}
          </div>
        </div>
        <div className="flex gap-2">
          <Button onClick={onRetry} size="sm" variant="outline">
            {t("calibration.button.try_again")}
          </Button>
          <Button onClick={onFallback} size="sm" variant="outline">
            {t("calibration.button.use_simple_setup")}
          </Button>
        </div>
      </div>
    );
  }

  // Cancelled (or any other terminal state).
  return (
    <div className="space-y-4">
      <div className="flex items-start gap-2 rounded-md border border-muted bg-muted/40 p-3 text-sm text-muted-foreground">
        <XIcon className="size-5 flex-shrink-0 mt-0.5" />
        <p>{t("calibration.terminal.cancelled.title")}</p>
      </div>
      <Button onClick={onRetry} size="sm" variant="outline">
        {t("calibration.button.try_again")}
      </Button>
    </div>
  );
}
