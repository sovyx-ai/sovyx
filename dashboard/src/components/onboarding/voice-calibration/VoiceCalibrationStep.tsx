/**
 * VoiceCalibrationStep -- top-level orchestrator for the L3 voice
 * calibration onboarding step.
 *
 * Composition over inheritance: this file owns the state machine
 * (idle / running / terminal phase) + the WebSocket + cancel
 * lifecycle, and dispatches rendering to one of six subcomponents:
 *
 *   _FastPathProgress   -- ~5s cached-profile replay branch
 *   _SlowPathProgress   -- 5-10min stage-by-stage timeline branch
 *   _CapturePrompt      -- inline "Say <phrase>" / "Stay silent"
 *   _ProfileReview      -- DONE terminal render
 *   _FallbackBanner     -- FALLBACK terminal render
 *   _CancelDialog       -- two-step cancel confirmation
 *
 * Mission: MISSION-voice-self-calibrating-system-2026-05-05.md §6.3
 * (T3.4 split). v0.30.25 supersedes the prior monolithic
 * VoiceCalibrationStep.tsx with this subpackage layout; the public
 * API (`<VoiceCalibrationStep mindId onCompleted onFallback />`) is
 * preserved verbatim so callers don't migrate.
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

import { CancelDialog } from "./_CancelDialog";
import { FallbackBanner } from "./_FallbackBanner";
import { FastPathProgress } from "./_FastPathProgress";
import { ProfileReview } from "./_ProfileReview";
import { SlowPathProgress } from "./_SlowPathProgress";

interface VoiceCalibrationStepProps {
  mindId: string;
  onCompleted: () => void;
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
  const [cancelDialogOpen, setCancelDialogOpen] = useState(false);
  const activeJobIdRef = useRef<string | null>(null);

  // P6 (v0.30.34) — Mission §10.2 #13: preview-fingerprint is fetched
  // LAZILY on operator action, not eagerly on mount. The previous
  // useEffect kicked off a fingerprint probe + KB lookup every time
  // the wizard step rendered — even on operators who never start a
  // calibration — wasting probe cycles + telemetry noise. Operators
  // click the "Show detected hardware" button (in IdleView) when
  // they want to see what would be calibrated BEFORE committing.

  useEffect(() => {
    if (currentJob === null) return;
    if (isWizardCalibrationTerminal(currentJob.status)) {
      setPhase("terminal");
      setCancelDialogOpen(false);
      unsubscribeFromCalibrationJob();
    }
  }, [currentJob, unsubscribeFromCalibrationJob]);

  useEffect(() => {
    return () => {
      unsubscribeFromCalibrationJob();
    };
  }, [unsubscribeFromCalibrationJob]);

  const handleStart = useCallback(async () => {
    clearCalibrationError();
    const jobId = await startCalibration({ mind_id: mindId });
    if (jobId === null) return;
    activeJobIdRef.current = jobId;
    setPhase("running");
    subscribeToCalibrationJob(jobId);
  }, [
    clearCalibrationError,
    mindId,
    startCalibration,
    subscribeToCalibrationJob,
  ]);

  const handleRequestCancel = useCallback(() => {
    setCancelDialogOpen(true);
  }, []);

  const handleConfirmCancel = useCallback(async () => {
    const jobId = activeJobIdRef.current;
    if (jobId === null) {
      setCancelDialogOpen(false);
      return;
    }
    setCancelling(true);
    await cancelCalibrationJob(jobId);
    setCancelling(false);
    setCancelDialogOpen(false);
  }, [cancelCalibrationJob]);

  const handleDismissCancel = useCallback(() => {
    setCancelDialogOpen(false);
  }, []);

  const handleRetry = useCallback(() => {
    activeJobIdRef.current = null;
    clearCalibrationError();
    setPhase("idle");
  }, [clearCalibrationError]);

  // ── Render ──

  const progressPct = currentJob ? Math.round(currentJob.progress * 100) : 0;
  const statusKey = currentJob ? `calibration.status.${currentJob.status}` : "";
  const localizedStatus = currentJob
    ? (t(statusKey, { defaultValue: currentJob.current_stage_message }) as string)
    : "";

  const isFastPathStatus =
    currentJob !== null && currentJob.status.startsWith("fast_path");
  const isSlowPathStatus =
    currentJob !== null && currentJob.status.startsWith("slow_path");

  return (
    <div
      className="space-y-4 rounded-lg border bg-background p-6"
      data-testid="voice-calibration-step"
    >
      <div className="flex items-center gap-2">
        <MicIcon className="size-5" />
        <h3 className="text-lg font-semibold">{t("calibration.title")}</h3>
      </div>

      {phase === "idle" && (
        <_IdleView
          preview={calibrationPreview}
          onStart={handleStart}
          onShowPreview={() => void fetchCalibrationPreview()}
          onUseSimpleSetup={onFallback}
          loading={calibrationLoading}
        />
      )}

      {phase === "running" && currentJob !== null && (
        <>
          {isFastPathStatus ? (
            <FastPathProgress
              status={localizedStatus}
              progressPct={progressPct}
              onCancel={handleRequestCancel}
              cancelling={cancelling}
            />
          ) : isSlowPathStatus ? (
            <SlowPathProgress
              rawStatus={currentJob.status}
              status={localizedStatus}
              progressPct={progressPct}
              onCancel={handleRequestCancel}
              cancelling={cancelling}
              currentPrompt={currentJob.extras?.current_prompt ?? null}
            />
          ) : (
            <_GenericRunningView
              status={localizedStatus}
              progressPct={progressPct}
              onCancel={handleRequestCancel}
              cancelling={cancelling}
            />
          )}
          {cancelDialogOpen && (
            <CancelDialog
              cancelling={cancelling}
              onConfirm={() => void handleConfirmCancel()}
              onDismiss={handleDismissCancel}
            />
          )}
        </>
      )}

      {phase === "terminal" && currentJob !== null && (
        <_TerminalDispatch
          status={currentJob.status}
          errorSummary={currentJob.error_summary}
          fallbackReason={currentJob.fallback_reason}
          profilePath={currentJob.profile_path}
          triageWinnerHid={currentJob.triage_winner_hid}
          rolledBack={currentJob.extras?.rolled_back === true}
          onCompleted={onCompleted}
          onFallback={onFallback}
          onRetry={handleRetry}
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

interface IdleViewProps {
  preview: ReturnType<typeof useDashboardStore.getState>["calibrationPreview"];
  onStart: () => void;
  onShowPreview: () => void;
  onUseSimpleSetup: () => void;
  loading: boolean;
}

function _IdleView({
  preview,
  onStart,
  onShowPreview,
  onUseSimpleSetup,
  loading,
}: IdleViewProps) {
  const { t } = useTranslation("voice");
  return (
    <div className="space-y-3">
      <p className="text-sm text-muted-foreground">{t("calibration.subtitle")}</p>
      {preview !== null ? (
        <div className="rounded-md bg-muted/40 p-3 text-xs space-y-1">
          <p>
            <span className="font-medium">
              {t("calibration.detected.system")}:
            </span>{" "}
            {preview.system_vendor} {preview.system_product}
          </p>
          <p>
            <span className="font-medium">
              {t("calibration.detected.audio_stack")}:
            </span>{" "}
            {preview.audio_stack || "—"}
          </p>
        </div>
      ) : (
        <Button
          onClick={onShowPreview}
          variant="ghost"
          size="sm"
          disabled={loading}
          data-testid="voice-calibration-show-preview"
        >
          {loading ? (
            <LoaderIcon className="mr-2 size-3.5 animate-spin" />
          ) : null}
          {t("calibration.button.show_preview")}
        </Button>
      )}
      <div className="flex flex-col gap-2 sm:flex-row sm:items-center">
        <Button onClick={onStart} disabled={loading} size="lg">
          {loading ? (
            <LoaderIcon className="mr-2 size-4 animate-spin" />
          ) : (
            <MicIcon className="mr-2 size-4" />
          )}
          {t("calibration.button.start")}
        </Button>
        <Button
          onClick={onUseSimpleSetup}
          variant="ghost"
          size="sm"
          disabled={loading}
        >
          {t("calibration.button.use_simple_setup")}
        </Button>
      </div>
      <p className="text-xs text-muted-foreground">
        {t("calibration.estimated_duration")}
      </p>
    </div>
  );
}

interface GenericRunningViewProps {
  status: string;
  progressPct: number;
  onCancel: () => void;
  cancelling: boolean;
}

function _GenericRunningView({
  status,
  progressPct,
  onCancel,
  cancelling,
}: GenericRunningViewProps) {
  const { t } = useTranslation("voice");
  return (
    <div className="space-y-4">
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

interface TerminalDispatchProps {
  status: string;
  errorSummary: string | null;
  fallbackReason: string | null;
  profilePath: string | null;
  triageWinnerHid: string | null;
  rolledBack: boolean;
  onCompleted: () => void;
  onFallback: () => void;
  onRetry: () => void;
}

function _TerminalDispatch({
  status,
  errorSummary,
  fallbackReason,
  profilePath,
  triageWinnerHid,
  rolledBack,
  onCompleted,
  onFallback,
  onRetry,
}: TerminalDispatchProps) {
  const { t } = useTranslation("voice");
  if (status === "done") {
    return (
      <ProfileReview
        triageWinnerHid={triageWinnerHid}
        profilePath={profilePath}
        onCompleted={onCompleted}
      />
    );
  }
  if (status === "fallback") {
    return (
      <FallbackBanner fallbackReason={fallbackReason} onFallback={onFallback} />
    );
  }
  if (status === "failed") {
    return (
      <div className="space-y-4" data-testid="voice-calibration-terminal-failed">
        {rolledBack && (
          // P6 (v0.30.34) — Mission §10.2 #11: surface auto-rollback so
          // operators understand the apply chain failed mid-way but the
          // applier's LIFO rollback restored prior state. They aren't
          // left wondering "what state am I in now?"
          <div
            className="flex items-start gap-2 rounded-md border border-amber-200 bg-amber-50 p-3 text-sm text-amber-900"
            data-testid="voice-calibration-rollback-banner"
          >
            <AlertCircleIcon className="size-5 flex-shrink-0 mt-0.5" />
            <div className="space-y-1">
              <p className="font-medium">
                {t("calibration.terminal.rolled_back.title")}
              </p>
              <p className="text-xs">
                {t("calibration.terminal.rolled_back.description")}
              </p>
            </div>
          </div>
        )}
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
  // CANCELLED or any other terminal state.
  return (
    <div
      className="space-y-4"
      data-testid="voice-calibration-terminal-cancelled"
    >
      <div className="flex items-start gap-2 rounded-md border border-muted bg-muted/40 p-3 text-sm text-muted-foreground">
        <CheckCircle2Icon className="size-5 flex-shrink-0 mt-0.5" />
        <p>{t("calibration.terminal.cancelled.title")}</p>
      </div>
      <Button onClick={onRetry} size="sm" variant="outline">
        {t("calibration.button.try_again")}
      </Button>
    </div>
  );
}
