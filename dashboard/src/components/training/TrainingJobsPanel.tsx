/**
 * TrainingJobsPanel — Mission v0.30.0 §T1.5 (D2).
 *
 * Renders the live state of the currently subscribed training job.
 * Mounted in ``pages/voice.tsx`` as a Section that's visible when
 * ``useDashboardStore().currentTrainingJob !== null`` (i.e., the
 * operator submitted a training job and the WebSocket subscription
 * is delivering snapshots).
 *
 * UI states:
 * * In-flight (status synthesizing/training): progress bar + phase
 *   pill + samples counter + Cancel button.
 * * Terminal (status complete/failed/cancelled): final pill +
 *   error_summary disclosure (failed) + "Use this model" CTA
 *   (complete) + dismiss button.
 *
 * The panel does NOT manage subscription lifecycle (parent does
 * via ``subscribeToTrainingJob``). It's a pure observer of
 * ``currentTrainingJob`` from the slice.
 */
import type { JSX } from "react";
import { useTranslation } from "react-i18next";

import { useDashboardStore } from "@/stores/dashboard";
import type { TrainingJobStatus } from "@/types/api";

const _TERMINAL_STATUSES: ReadonlySet<TrainingJobStatus> = new Set<TrainingJobStatus>([
  "complete",
  "failed",
  "cancelled",
]);

export function TrainingJobsPanel(): JSX.Element | null {
  const { t } = useTranslation("voice");
  const currentJob = useDashboardStore((s) => s.currentTrainingJob);
  const cancelJob = useDashboardStore((s) => s.cancelTrainingJob);
  const unsubscribe = useDashboardStore((s) => s.unsubscribeFromTrainingJob);
  const trainingError = useDashboardStore((s) => s.trainingError);

  if (currentJob === null) {
    return null;
  }

  const summary = currentJob.summary;
  const status = summary.status;
  const isTerminal = _TERMINAL_STATUSES.has(status);
  const progressPct = Math.round(summary.progress * 100);

  // Status pill tone — mirrors PerMindWakeWordCard's three-state
  // pattern (registered/notRegistered/error).
  let pillTone: "ok" | "warn" | "danger" | "info";
  if (status === "complete") {
    pillTone = "ok";
  } else if (status === "failed") {
    pillTone = "danger";
  } else if (status === "cancelled") {
    pillTone = "warn";
  } else {
    pillTone = "info";
  }
  const pillBgClass = {
    ok: "bg-[var(--svx-color-success-soft)] text-[var(--svx-color-success)]",
    warn: "bg-[var(--svx-color-warning-soft)] text-[var(--svx-color-warning)]",
    danger: "bg-[var(--svx-color-danger-soft)] text-[var(--svx-color-danger)]",
    info: "bg-[var(--svx-color-accent-soft)] text-[var(--svx-color-accent)]",
  }[pillTone];

  return (
    <div
      data-testid="training-jobs-panel"
      className="rounded-[var(--svx-radius-md)] border border-[var(--svx-color-border)] bg-[var(--svx-color-surface-secondary)] p-3"
    >
      {/* Header: job id + status pill */}
      <div className="flex items-center justify-between gap-3">
        <div className="min-w-0 flex-1">
          <div className="text-sm font-semibold text-[var(--svx-color-text-primary)]">
            {summary.wake_word}
          </div>
          <div className="font-mono text-xs text-[var(--svx-color-text-tertiary)]">
            {summary.job_id} · {summary.language}
          </div>
        </div>
        <span className={`rounded-full px-2 py-0.5 text-xs font-medium ${pillBgClass}`}>
          {t(`training.panel.status.${status}`)}
        </span>
      </div>

      {/* Progress bar — only render when not terminal OR mid-progress */}
      {!isTerminal && (
        <div className="mt-3">
          <div
            role="progressbar"
            aria-valuenow={progressPct}
            aria-valuemin={0}
            aria-valuemax={100}
            aria-label={t("training.panel.progressLabel")}
            className="h-2 w-full overflow-hidden rounded-full bg-[var(--svx-color-surface-tertiary)]"
          >
            <div
              className="h-full bg-[var(--svx-color-accent)] transition-all"
              style={{ width: `${progressPct}%` }}
            />
          </div>
          <div className="mt-1 flex justify-between text-xs text-[var(--svx-color-text-tertiary)]">
            <span>
              {t("training.panel.samplesProgress", {
                generated: summary.samples_generated,
                total: summary.target_samples,
              })}
            </span>
            <span>{progressPct}%</span>
          </div>
        </div>
      )}

      {/* Failed: error summary disclosure */}
      {status === "failed" && summary.error_summary !== "" && (
        <details className="mt-2">
          <summary className="cursor-pointer text-xs text-[var(--svx-color-danger)]">
            {t("training.panel.viewError")}
          </summary>
          <pre className="mt-1.5 whitespace-pre-wrap break-words rounded bg-[var(--svx-color-surface-tertiary)] p-2 font-mono text-xs text-[var(--svx-color-text-secondary)]">
            {summary.error_summary}
          </pre>
        </details>
      )}

      {/* Complete: surface output path + a "next steps" hint */}
      {status === "complete" && summary.output_path !== "" && (
        <div className="mt-2 text-xs text-[var(--svx-color-text-secondary)]">
          <div className="text-[var(--svx-color-text-tertiary)]">
            {t("training.panel.outputLabel")}
          </div>
          <div className="mt-0.5 break-all font-mono">{summary.output_path}</div>
          <p className="mt-1.5">{t("training.panel.completeHint")}</p>
        </div>
      )}

      {/* Slice error (e.g., cancel failed) */}
      {trainingError !== null && (
        <div
          role="alert"
          className="mt-2 rounded border border-[var(--svx-color-danger)] bg-[var(--svx-color-danger-soft)] p-2 text-xs text-[var(--svx-color-danger)]"
        >
          {trainingError}
        </div>
      )}

      {/* Footer actions */}
      <div className="mt-3 flex justify-end gap-2 border-t border-[var(--svx-color-border)] pt-2">
        {isTerminal ? (
          <button
            type="button"
            onClick={() => {
              unsubscribe();
              useDashboardStore.setState({ currentTrainingJob: null });
            }}
            className="rounded-[var(--svx-radius-md)] px-3 py-1 text-xs text-[var(--svx-color-text-secondary)] hover:bg-[var(--svx-color-surface-hover)]"
          >
            {t("training.panel.dismiss")}
          </button>
        ) : (
          <button
            type="button"
            onClick={() => void cancelJob(summary.job_id)}
            disabled={summary.cancelled_signalled}
            className="rounded-[var(--svx-radius-md)] border border-[var(--svx-color-danger)] px-3 py-1 text-xs text-[var(--svx-color-danger)] hover:bg-[var(--svx-color-danger-soft)] disabled:opacity-50"
          >
            {summary.cancelled_signalled
              ? t("training.panel.cancelling")
              : t("training.panel.cancel")}
          </button>
        )}
      </div>
    </div>
  );
}
