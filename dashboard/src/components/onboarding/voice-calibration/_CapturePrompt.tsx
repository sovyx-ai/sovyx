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
 * Active in v0.30.25 alpha as a render-only surface; the orchestrator
 * does not yet emit a structured "prompt" event so the parent
 * conditionally renders it via local heuristics. Wire-up to a
 * dedicated `voice.calibration.wizard.prompt` event lands when the
 * bash diag emits structured prompts upstream (post-v0.30.25).
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
