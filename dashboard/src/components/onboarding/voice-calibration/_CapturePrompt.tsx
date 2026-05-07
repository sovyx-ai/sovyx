/**
 * _CapturePrompt -- inline "Say <phrase>" prompt rendered during
 * slow-path capture windows.
 *
 * The orchestrator's slow_path_diag stage captures multiple short
 * speech windows. The diag's bash side handles the actual capture;
 * this component surfaces the prompt to the operator so they know
 * what to say + when. Distinct from a modal because the running
 * progress UI stays visible -- the operator sees both the prompt
 * AND the multi-stage timeline.
 *
 * Subcomponent of VoiceCalibrationStep per spec §6.3 (T3.4 split).
 * Wire-up shipped in P3 v0.30.31: the bash diag's
 * ``prompt_emit_structured`` writes one JSONL line per operator-facing
 * prompt to ``<job_dir>/prompts.jsonl``; the orchestrator's
 * ``_tail_prompts_file`` polls that file every 500 ms and pushes each
 * parsed prompt into ``state.extras["current_prompt"]``. The parent
 * (``VoiceCalibrationStep``) reads ``currentJob.extras?.current_prompt``
 * and conditionally renders this component during slow_path_diag (per
 * sibling ``_SlowPathProgress.tsx`` flow).
 */

import { useTranslation } from "react-i18next";
import { MicIcon } from "lucide-react";

interface CapturePromptProps {
  /** Phrase the operator should speak (e.g. "Hello Sovyx"). */
  phrase: string;
  /** Optional silence-window duration; renders the silence prompt. */
  silenceSeconds?: number;
}

export function CapturePrompt({ phrase, silenceSeconds }: CapturePromptProps) {
  const { t } = useTranslation("voice");
  if (silenceSeconds !== undefined) {
    return (
      <div
        className="flex items-center gap-3 rounded-md border border-amber-200 bg-amber-50 p-3 text-sm text-amber-900"
        data-testid="voice-calibration-capture-prompt-silence"
        role="status"
        aria-live="polite"
      >
        <MicIcon className="size-4" />
        <span>
          {t("calibration.slow_path.prompt_silence", { seconds: silenceSeconds })}
        </span>
      </div>
    );
  }
  return (
    <div
      className="flex items-center gap-3 rounded-md border border-blue-200 bg-blue-50 p-3 text-sm text-blue-900"
      data-testid="voice-calibration-capture-prompt-speak"
      role="status"
      aria-live="polite"
    >
      <MicIcon className="size-4" />
      <span>{t("calibration.slow_path.prompt_speak", { phrase })}</span>
    </div>
  );
}
