import { useCallback, useState } from "react";
import { AlertTriangleIcon, SpeakerIcon } from "lucide-react";

import type { z } from "zod";
import type {
  VoiceAlternativeDeviceSchema,
  VoiceCaptureDeviceContendedErrorSchema,
} from "@/types/schemas";

export type AlternativeDevice = z.infer<typeof VoiceAlternativeDeviceSchema>;
export type CaptureDeviceContendedPayload = z.infer<
  typeof VoiceCaptureDeviceContendedErrorSchema
>;

interface DeviceContentionBannerProps {
  /** Payload parsed from the ``/api/voice/enable`` 503 response. */
  payload: CaptureDeviceContendedPayload;
  /**
   * Callback fired when the user picks an alternative device chip.
   * The parent component must re-dispatch ``/api/voice/enable`` with
   * ``input_device_name`` pinned to ``device.name`` (and optionally
   * ``input_device`` to ``device.index`` for legacy resolve).
   *
   * When ``null`` the retry is in flight and every chip is disabled.
   */
  onSelectAlternative: ((device: AlternativeDevice) => void) | null;
}

/**
 * Banner shown when ``/api/voice/enable`` returns HTTP 503 with
 * ``error: "capture_device_contended"``. Surfaced by the session-
 * manager contention pattern in :mod:`sovyx.voice._capture_task` —
 * see ``voice-linux-cascade-root-fix`` T9 and T7.
 *
 * Design rationale:
 *
 * * The message is explanatory, not apologetic — the user deserves
 *   to know that *another app* is holding their mic, not that
 *   "something went wrong".
 * * Chips are clickable and ordered by preference (backend sent
 *   them that way already).
 * * The ``suggested_actions`` tokens get rendered as an advice
 *   line beneath the chips for users with no enumerated alternative.
 * * No i18n yet — matches the rest of ``VoiceStep`` which is still
 *   English-only in the onboarding path. Will be migrated wholesale
 *   when the onboarding page picks up ``useTranslation``.
 */
export function DeviceContentionBanner({
  payload,
  onSelectAlternative,
}: DeviceContentionBannerProps) {
  const [retryingIndex, setRetryingIndex] = useState<number | null>(null);

  const handleSelect = useCallback(
    (device: AlternativeDevice) => {
      if (onSelectAlternative === null) return;
      setRetryingIndex(device.index);
      onSelectAlternative(device);
    },
    [onSelectAlternative],
  );

  const disabled = onSelectAlternative === null;
  const { alternative_devices: alternatives, contending_process_hint, host_api } = payload;

  return (
    <div
      role="alert"
      className="rounded-[var(--svx-radius-lg)] border border-[var(--svx-color-warning)]/40 bg-[var(--svx-color-warning)]/5 p-4 space-y-3"
    >
      <div className="flex items-center gap-2 text-xs font-medium text-[var(--svx-color-text-primary)]">
        <AlertTriangleIcon className="size-4 text-[var(--svx-color-warning)]" />
        Microphone is held by another audio client
      </div>
      <p className="text-[11px] text-[var(--svx-color-text-secondary)]">
        {contending_process_hint ? (
          <>
            <span className="font-mono">{contending_process_hint}</span> is
            currently capturing on{" "}
            <span className="font-mono">{host_api ?? "the selected device"}</span>
            . Sovyx can try a session-manager virtual device instead.
          </>
        ) : (
          <>
            Another audio app (usually PipeWire, PulseAudio, or a videoconf
            client) is capturing on{" "}
            <span className="font-mono">{host_api ?? "the selected device"}</span>
            . Sovyx can switch to a session-manager virtual device instead —
            pick one below to retry.
          </>
        )}
      </p>

      {alternatives.length > 0 ? (
        <div className="flex flex-wrap gap-2">
          {alternatives.map((device) => {
            const isRetrying = retryingIndex === device.index && disabled;
            return (
              <button
                key={`${device.index}-${device.host_api}`}
                type="button"
                onClick={() => handleSelect(device)}
                disabled={disabled}
                className={[
                  "inline-flex items-center gap-1.5",
                  "rounded-[var(--svx-radius-md)] border",
                  "border-[var(--svx-color-border-default)]",
                  "bg-[var(--svx-color-bg-surface)]",
                  "px-2.5 py-1.5 text-[11px] font-medium",
                  "text-[var(--svx-color-text-primary)]",
                  "transition-colors",
                  disabled
                    ? "opacity-60 cursor-not-allowed"
                    : "hover:border-[var(--svx-color-warning)] hover:text-[var(--svx-color-warning)]",
                ].join(" ")}
                aria-label={`Retry with ${device.name}`}
                data-testid={`device-contention-chip-${device.index}`}
              >
                <SpeakerIcon className="size-3" />
                <span className="font-mono truncate max-w-[160px]">
                  {device.name}
                </span>
                <span className="text-[10px] text-[var(--svx-color-text-tertiary)]">
                  ({device.kind.replace("_", " ")})
                </span>
                {isRetrying ? (
                  <span className="text-[10px] text-[var(--svx-color-info)]">
                    retrying…
                  </span>
                ) : null}
              </button>
            );
          })}
        </div>
      ) : (
        <p className="text-[11px] text-[var(--svx-color-text-tertiary)]">
          No enumerated alternatives — try closing the app that is holding the
          mic (commonly a videoconference client) and retry.
        </p>
      )}
    </div>
  );
}
