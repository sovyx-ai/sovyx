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

import { memo, useCallback, useRef, useState } from "react";
import { CheckCircle2Icon, LoaderIcon, VolumeXIcon, XCircleIcon } from "lucide-react";
import { api } from "@/lib/api";
import { Button } from "@/components/ui/button";
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

function formatPeakDb(peak: number | null | undefined): string | null {
  if (peak === null || peak === undefined) return null;
  return `${peak.toFixed(1)} dBFS peak`;
}

function messageForCode(code: VoiceTestErrorCode | null): string {
  switch (code) {
    case "disabled":
      return "Voice device test is disabled by configuration.";
    case "pipeline_active":
      return "Voice pipeline is running — disable it to run the test.";
    case "tts_unavailable":
      return "No TTS model available. Download a voice model first.";
    case "device_not_found":
      return "Output device not found.";
    case "device_busy":
      return "Output device is in use by another app.";
    case "permission_denied":
      return "Permission denied for output device.";
    case "invalid_request":
      return "Request rejected by the server.";
    default:
      return "Playback failed.";
  }
}

function TtsTestButtonImpl({
  deviceId,
  language = "en",
  voice,
  disabled,
}: TtsTestButtonProps) {
  const [state, setState] = useState<ButtonState>({ kind: "idle" });
  const cancelRef = useRef(false);

  const poll = useCallback(async (jobId: string): Promise<void> => {
    const start = performance.now();
    while (!cancelRef.current) {
      if (performance.now() - start > POLL_TIMEOUT_MS) {
        setState({
          kind: "error",
          code: "internal_error",
          message: "Timed out waiting for playback to finish.",
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
          setState({
            kind: "error",
            code: (result.code ?? null) as VoiceTestErrorCode | null,
            message:
              result.detail ||
              messageForCode((result.code ?? null) as VoiceTestErrorCode | null),
          });
          return;
        }
      } catch {
        // Transient GET failure — retry on next tick.
      }
      await new Promise((r) => setTimeout(r, POLL_INTERVAL_MS));
    }
  }, []);

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
      const msg = err instanceof Error ? err.message : "Network error";
      // Try to pull the server's machine-readable code from the body.
      let code: VoiceTestErrorCode | null = null;
      try {
        const body = (err as unknown as { body?: { code?: string } }).body;
        if (body?.code) {
          code = body.code as VoiceTestErrorCode;
        }
      } catch {
        /* ignore */
      }
      setState({
        kind: "error",
        code,
        message: code ? messageForCode(code) : msg,
      });
    }
  }, [deviceId, language, voice, poll]);

  // Clean up the polling flag if the component unmounts mid-test.
  // Not a useEffect because we only need it at unmount.
  const cancelIfRunning = useCallback(() => {
    cancelRef.current = true;
  }, []);
  (onClick as unknown as { cancel?: () => void }).cancel = cancelIfRunning;

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
            Playing test…
          </>
        ) : (
          <>
            <VolumeXIcon className="mr-2 size-3.5" />
            Test speakers
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
            Played successfully
            {formatPeakDb(state.result.peak_db)
              ? ` — ${formatPeakDb(state.result.peak_db)}`
              : ""}
          </span>
        </div>
      )}

      {state.kind === "error" && (
        <div
          role="alert"
          data-testid="tts-test-error"
          className="flex items-center gap-2 rounded-[var(--svx-radius-md)] bg-[var(--svx-color-error)]/10 px-3 py-2 text-xs text-[var(--svx-color-error)]"
        >
          <XCircleIcon className="size-3.5 shrink-0" />
          <span>{state.message}</span>
        </div>
      )}
    </div>
  );
}

export const TtsTestButton = memo(TtsTestButtonImpl);
