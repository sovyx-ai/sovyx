/**
 * TtsTestButton — plays a short localised phrase through the selected
 * output device so the user can confirm their speakers work before
 * completing the voice setup.
 *
 * Flow:
 *
 * 1. ``POST /api/voice/test/output`` returns ``{ job_id, status: "queued" }``.
 * 2. The button polls ``GET /api/voice/test/output/{job_id}`` every
 *    400 ms until ``status`` is terminal (``done`` or ``error``).
 * 3. Renders a green ✅ on success, an ❌ + machine-readable hint on
 *    failure. The peak_dB indicator is surfaced when available to
 *    confirm the clip actually had level on the wire.
 *
 * Rate-limiting / concurrency: the backend returns 409 with code
 * ``pipeline_active`` while the live voice pipeline is on. We render
 * that explicitly so the user knows to disable voice before testing.
 */

import { memo, useCallback, useEffect, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import {
  CheckCircle2Icon,
  DownloadIcon,
  LoaderIcon,
  VolumeXIcon,
  XCircleIcon,
} from "lucide-react";
import { api, ApiError } from "@/lib/api";
import { Button } from "@/components/ui/button";
import { useVoiceModels } from "@/hooks/use-voice-models";
import type {
  VoiceTestErrorCode,
  VoiceTestOutputJob,
  VoiceTestOutputResult,
} from "@/types/api";
import {
  VoiceTestOutputJobSchema,
  VoiceTestOutputResultSchema,
} from "@/types/schemas";

const POLL_INTERVAL_MS = 400;
const POLL_TIMEOUT_MS = 15_000;

export interface TtsTestButtonProps {
  /** Selected PortAudio output device index — `null` = system default. */
  deviceId: number | null;
  /** UI language for phrase selection (maps to server _DEFAULT_PHRASES). */
  language?: string;
  /** Optional voice id passed through to the TTS engine. */
  voice?: string | null;
  /** Disable the button (e.g. while the user is still picking a device). */
  disabled?: boolean;
}

type ButtonState =
  | { kind: "idle" }
  | { kind: "running" }
  | { kind: "success"; result: VoiceTestOutputResult }
  | { kind: "error"; code: VoiceTestErrorCode | null; message: string };

function formatPeakDb(
  peak: number | null | undefined,
  t: (key: string, opts?: Record<string, unknown>) => string,
): string | null {
  if (peak === null || peak === undefined) return null;
  return t("ttsTest.peakLabel", { peak: peak.toFixed(1) });
}

/**
 * Map a server-side ``VoiceTestErrorCode`` to the matching i18n key
 * under ``voice:ttsTest.errorCodes``. Pre-v0.32.4 this returned
 * hardcoded English strings; the translation now happens at the
 * component boundary via ``t()`` so pt-BR + es operators get their
 * locale's copy. The error codes themselves are STABLE backend
 * contract identifiers — never translated; only the operator-facing
 * messages are.
 */
function i18nKeyForCode(code: VoiceTestErrorCode | null): string {
  switch (code) {
    case "disabled":
      return "ttsTest.errorCodes.disabled";
    case "pipeline_active":
      return "ttsTest.errorCodes.pipelineActive";
    case "tts_unavailable":
      return "ttsTest.errorCodes.ttsUnavailable";
    case "models_not_downloaded":
      return "ttsTest.errorCodes.modelsNotDownloaded";
    case "device_not_found":
      return "ttsTest.errorCodes.deviceNotFound";
    case "device_busy":
      return "ttsTest.errorCodes.deviceBusy";
    case "permission_denied":
      return "ttsTest.errorCodes.permissionDenied";
    case "unsupported_samplerate":
      return "ttsTest.errorCodes.unsupportedSamplerate";
    case "unsupported_channels":
      return "ttsTest.errorCodes.unsupportedChannels";
    case "unsupported_format":
      return "ttsTest.errorCodes.unsupportedFormat";
    case "buffer_size_invalid":
      return "ttsTest.errorCodes.bufferSizeInvalid";
    case "invalid_request":
      return "ttsTest.errorCodes.invalidRequest";
    default:
      return "ttsTest.errorCodes.fallback";
  }
}

function TtsTestButtonImpl({
  deviceId,
  language = "en",
  voice,
  disabled,
}: TtsTestButtonProps) {
  const { t } = useTranslation("voice");
  const [state, setState] = useState<ButtonState>({ kind: "idle" });
  const cancelRef = useRef(false);

  const poll = useCallback(
    async (jobId: string): Promise<void> => {
      const start = performance.now();
      while (!cancelRef.current) {
        if (performance.now() - start > POLL_TIMEOUT_MS) {
          setState({
            kind: "error",
            code: "internal_error",
            message: t("ttsTest.timeoutMessage"),
          });
          return;
        }
        try {
          const result = await api.get<VoiceTestOutputResult>(
            `/api/voice/test/output/${jobId}`,
            { schema: VoiceTestOutputResultSchema },
          );
          if (result.status === "done") {
            setState({ kind: "success", result });
            return;
          }
          if (result.status === "error") {
            const errorCode = (result.code ?? null) as VoiceTestErrorCode | null;
            setState({
              kind: "error",
              code: errorCode,
              message: result.detail || t(i18nKeyForCode(errorCode)),
            });
            return;
          }
        } catch {
          // Transient GET failure — retry on next tick.
        }
        await new Promise((r) => setTimeout(r, POLL_INTERVAL_MS));
      }
    },
    [t],
  );

  const onClick = useCallback(async () => {
    cancelRef.current = false;
    setState({ kind: "running" });
    try {
      const job = await api.post<VoiceTestOutputJob>(
        "/api/voice/test/output",
        {
          device_id: deviceId,
          voice: voice ?? null,
          phrase_key: "default",
          language,
        },
        { schema: VoiceTestOutputJobSchema },
      );
      await poll(job.job_id);
    } catch (err) {
      const fallbackMsg =
        err instanceof Error ? err.message : t("ttsTest.networkError");
      // Try to pull the server's machine-readable code AND human-readable
      // detail from the body. Prefer ``detail`` over the canned message
      // whenever the backend attached one — it's the only place a device-
      // specific error (e.g. "Output device 7 busy") actually reaches the
      // UI, so collapsing it to t(i18nKeyForCode(code)) would hide real info.
      let code: VoiceTestErrorCode | null = null;
      let detail: string | null = null;
      if (err instanceof ApiError && err.body) {
        const raw = err.body as { code?: unknown; detail?: unknown };
        if (typeof raw.code === "string") code = raw.code as VoiceTestErrorCode;
        if (typeof raw.detail === "string" && raw.detail) detail = raw.detail;
      }
      const message = detail ?? (code ? t(i18nKeyForCode(code)) : fallbackMsg);
      setState({ kind: "error", code, message });
    }
  }, [deviceId, language, voice, poll, t]);

  // Signal the poll loop to bail if the component unmounts mid-test —
  // otherwise the interval keeps firing, setState fires on a dead tree,
  // and the network keeps churning through /output/{id} GETs.
  useEffect(() => {
    return () => {
      cancelRef.current = true;
    };
  }, []);

  const isRunning = state.kind === "running";
  return (
    <div className="space-y-2">
      <Button
        variant="outline"
        size="sm"
        onClick={onClick}
        disabled={disabled || isRunning}
        className="w-full"
      >
        {isRunning ? (
          <>
            <LoaderIcon className="mr-2 size-3.5 animate-spin" />
            {t("ttsTest.playing")}
          </>
        ) : (
          <>
            <VolumeXIcon className="mr-2 size-3.5" />
            {t("ttsTest.testSpeakers")}
          </>
        )}
      </Button>

      {state.kind === "success" && (
        <div
          role="status"
          className="flex items-center gap-2 rounded-[var(--svx-radius-md)] bg-[var(--svx-color-success)]/10 px-3 py-2 text-xs text-[var(--svx-color-success)]"
        >
          <CheckCircle2Icon className="size-3.5 shrink-0" />
          <span>
            {(() => {
              const peakLabel = formatPeakDb(state.result.peak_db, t);
              return peakLabel
                ? t("ttsTest.successWithPeak", { peak: peakLabel })
                : t("ttsTest.success");
            })()}
          </span>
        </div>
      )}

      {state.kind === "error" && (
        <div className="space-y-1.5">
          <div
            role="alert"
            data-testid="tts-test-error"
            className="flex items-center gap-2 rounded-[var(--svx-radius-md)] bg-[var(--svx-color-error)]/10 px-3 py-2 text-xs text-[var(--svx-color-error)]"
          >
            <XCircleIcon className="size-3.5 shrink-0" />
            <span>{state.message}</span>
          </div>
          {state.code === "models_not_downloaded" && <MissingModelsCTA />}
        </div>
      )}
    </div>
  );
}

/**
 * MissingModelsCTA — rendered only when the server reports
 * ``models_not_downloaded``. Isolated into its own component so the
 * parent's hook tree doesn't fire an initial ``models/status`` GET on
 * every Test-Speakers render (the hook lives under an error branch).
 */
function MissingModelsCTA() {
  const { t } = useTranslation("voice");
  const { startDownload, downloading, download } = useVoiceModels();
  return (
    <Button
      type="button"
      variant="outline"
      size="sm"
      disabled={downloading}
      onClick={() => {
        void startDownload();
      }}
      className="w-full"
      data-testid="tts-test-download-cta"
    >
      {downloading ? (
        <>
          <LoaderIcon className="mr-2 size-3.5 animate-spin" />
          {download?.current_model
            ? t("ttsTest.downloading", { model: download.current_model })
            : t("ttsTest.downloadingNoModel")}
        </>
      ) : (
        <>
          <DownloadIcon className="mr-2 size-3.5" />
          {t("ttsTest.downloadCta")}
        </>
      )}
    </Button>
  );
}

export const TtsTestButton = memo(TtsTestButtonImpl);
