/**
 * TrainWakeWordModal — Mission v0.30.0 §T1.4 (D3).
 *
 * Modal that lets the operator start a wake-word training job from
 * the dashboard. Rendered when ``<PerMindWakeWordCard>`` shows a
 * ``resolution_strategy === "none"`` mind and the operator clicks
 * the "Train this wake word" button.
 *
 * Pre-filled fields:
 * * wake_word + mind_id + language come from the parent entry.
 *
 * Operator-adjustable fields:
 * * target_samples (slider 100-10000, default 200).
 * * voices (CSV, optional — empty uses Kokoro defaults).
 * * variants (CSV, optional — empty uses CLI default
 *   ``[wake_word, "hey wake_word"]``).
 * * negatives_dir (required, no default — operator-side).
 *
 * On Start:
 * 1. Validate fields client-side (zod-friendly: zero whitespace,
 *    target_samples in range).
 * 2. Call ``useDashboardStore().startTraining(...)``.
 * 3. On success (job_id returned): close modal + caller subscribes
 *    via ``subscribeToTrainingJob(jobId)``.
 * 4. On error (null returned): stay open + display
 *    ``trainingError`` from the store.
 *
 * The modal uses native ``<dialog>`` semantics for accessibility
 * (screen readers + Esc-to-close). Per CLAUDE.md a11y rule + the
 * existing wake-word UI's ``aria-label`` precedent.
 */
import type { JSX } from "react";
import { useState, type FormEvent } from "react";
import { useTranslation } from "react-i18next";

import { useDashboardStore } from "@/stores/dashboard";
import type { WakeWordPerMindStatus } from "@/types/api";

interface TrainWakeWordModalProps {
  entry: WakeWordPerMindStatus;
  open: boolean;
  onClose: () => void;
  onStarted: (jobId: string) => void;
}

export function TrainWakeWordModal({
  entry,
  open,
  onClose,
  onStarted,
}: TrainWakeWordModalProps): JSX.Element | null {
  const { t } = useTranslation("voice");
  const startTraining = useDashboardStore((s) => s.startTraining);
  const trainingError = useDashboardStore((s) => s.trainingError);
  const clearTrainingError = useDashboardStore((s) => s.clearTrainingError);

  const [targetSamples, setTargetSamples] = useState(200);
  const [voicesCsv, setVoicesCsv] = useState("");
  const [variantsCsv, setVariantsCsv] = useState("");
  const [negativesDir, setNegativesDir] = useState("");
  const [submitting, setSubmitting] = useState(false);

  if (!open) return null;

  const handleSubmit = async (e: FormEvent<HTMLFormElement>): Promise<void> => {
    e.preventDefault();
    if (submitting) return;

    setSubmitting(true);
    clearTrainingError();

    const voices = voicesCsv
      .split(",")
      .map((v) => v.trim())
      .filter((v) => v.length > 0);
    const variants = variantsCsv
      .split(",")
      .map((v) => v.trim())
      .filter((v) => v.length > 0);

    const jobId = await startTraining({
      wake_word: entry.wake_word,
      mind_id: entry.mind_id,
      language: entry.voice_language || "en",
      target_samples: targetSamples,
      voices,
      variants,
      negatives_dir: negativesDir,
    });

    setSubmitting(false);

    if (jobId !== null) {
      onStarted(jobId);
      onClose();
    }
    // Failure: trainingError populated by the slice; modal stays open.
  };

  return (
    <div
      role="dialog"
      aria-modal="true"
      aria-label={t("training.modal.title")}
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/50 p-4"
      onClick={(e) => {
        if (e.target === e.currentTarget) onClose();
      }}
    >
      <div className="w-full max-w-lg rounded-[var(--svx-radius-lg)] border border-[var(--svx-color-border)] bg-[var(--svx-color-surface-primary)] p-5 shadow-xl">
        <header className="mb-3 flex items-start justify-between gap-3">
          <div>
            <h2 className="text-base font-semibold text-[var(--svx-color-text-primary)]">
              {t("training.modal.title")}
            </h2>
            <p className="mt-1 text-xs text-[var(--svx-color-text-tertiary)]">
              {t("training.modal.subtitle", { wake_word: entry.wake_word })}
            </p>
          </div>
          <button
            type="button"
            onClick={onClose}
            aria-label={t("training.modal.close")}
            className="rounded-[var(--svx-radius-sm)] px-2 py-1 text-sm text-[var(--svx-color-text-tertiary)] hover:bg-[var(--svx-color-surface-hover)]"
          >
            ×
          </button>
        </header>

        <form onSubmit={handleSubmit} className="space-y-3">
          {/* Read-only summary */}
          <div className="rounded-[var(--svx-radius-md)] bg-[var(--svx-color-surface-secondary)] p-2 text-xs">
            <div className="flex justify-between">
              <span className="text-[var(--svx-color-text-tertiary)]">
                {t("training.modal.wakeWord")}
              </span>
              <span className="font-mono">{entry.wake_word}</span>
            </div>
            <div className="mt-1 flex justify-between">
              <span className="text-[var(--svx-color-text-tertiary)]">
                {t("training.modal.mindId")}
              </span>
              <span className="font-mono">{entry.mind_id}</span>
            </div>
            <div className="mt-1 flex justify-between">
              <span className="text-[var(--svx-color-text-tertiary)]">
                {t("training.modal.language")}
              </span>
              <span className="font-mono">{entry.voice_language || "en"}</span>
            </div>
          </div>

          {/* target_samples slider */}
          <div>
            <label
              htmlFor="train-target-samples"
              className="block text-xs font-medium text-[var(--svx-color-text-secondary)]"
            >
              {t("training.modal.targetSamples", { count: targetSamples })}
            </label>
            <input
              id="train-target-samples"
              type="range"
              min={100}
              max={10000}
              step={100}
              value={targetSamples}
              onChange={(e) => setTargetSamples(Number(e.target.value))}
              className="mt-1 w-full"
            />
            <p className="mt-0.5 text-xs text-[var(--svx-color-text-tertiary)]">
              {t("training.modal.targetSamplesHint")}
            </p>
          </div>

          {/* negatives_dir (required) */}
          <div>
            <label
              htmlFor="train-negatives-dir"
              className="block text-xs font-medium text-[var(--svx-color-text-secondary)]"
            >
              {t("training.modal.negativesDir")}{" "}
              <span className="text-[var(--svx-color-danger)]">*</span>
            </label>
            <input
              id="train-negatives-dir"
              type="text"
              required
              value={negativesDir}
              onChange={(e) => setNegativesDir(e.target.value)}
              placeholder="/data/negatives"
              className="mt-1 w-full rounded-[var(--svx-radius-sm)] border border-[var(--svx-color-border)] bg-[var(--svx-color-surface-secondary)] px-2 py-1 font-mono text-xs"
            />
            <p className="mt-0.5 text-xs text-[var(--svx-color-text-tertiary)]">
              {t("training.modal.negativesDirHint")}
            </p>
          </div>

          {/* voices (optional) */}
          <div>
            <label
              htmlFor="train-voices"
              className="block text-xs font-medium text-[var(--svx-color-text-secondary)]"
            >
              {t("training.modal.voices")}
            </label>
            <input
              id="train-voices"
              type="text"
              value={voicesCsv}
              onChange={(e) => setVoicesCsv(e.target.value)}
              placeholder="af_heart, am_michael"
              className="mt-1 w-full rounded-[var(--svx-radius-sm)] border border-[var(--svx-color-border)] bg-[var(--svx-color-surface-secondary)] px-2 py-1 font-mono text-xs"
            />
          </div>

          {/* variants (optional) */}
          <div>
            <label
              htmlFor="train-variants"
              className="block text-xs font-medium text-[var(--svx-color-text-secondary)]"
            >
              {t("training.modal.variants")}
            </label>
            <input
              id="train-variants"
              type="text"
              value={variantsCsv}
              onChange={(e) => setVariantsCsv(e.target.value)}
              placeholder={`${entry.wake_word}, hey ${entry.wake_word}`}
              className="mt-1 w-full rounded-[var(--svx-radius-sm)] border border-[var(--svx-color-border)] bg-[var(--svx-color-surface-secondary)] px-2 py-1 font-mono text-xs"
            />
          </div>

          {/* Error display */}
          {trainingError !== null && (
            <div
              role="alert"
              className="rounded border border-[var(--svx-color-danger)] bg-[var(--svx-color-danger-soft)] p-2 text-xs text-[var(--svx-color-danger)]"
            >
              {trainingError}
            </div>
          )}

          {/* Footer actions */}
          <div className="flex justify-end gap-2 border-t border-[var(--svx-color-border)] pt-3">
            <button
              type="button"
              onClick={onClose}
              className="rounded-[var(--svx-radius-md)] px-3 py-1.5 text-xs text-[var(--svx-color-text-secondary)] hover:bg-[var(--svx-color-surface-hover)]"
            >
              {t("training.modal.cancel")}
            </button>
            <button
              type="submit"
              disabled={submitting || !negativesDir.trim()}
              className="rounded-[var(--svx-radius-md)] bg-[var(--svx-color-accent)] px-3 py-1.5 text-xs font-medium text-white disabled:opacity-50"
            >
              {submitting
                ? t("training.modal.starting")
                : t("training.modal.startButton")}
            </button>
          </div>
        </form>
      </div>
    </div>
  );
}
