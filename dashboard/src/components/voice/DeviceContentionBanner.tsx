import { useCallback, useState } from "react";
import { Trans, useTranslation } from "react-i18next";
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
 * * v0.32.5 Phase 4.B.2: i18n migrated. Strings live under
 *   ``voice.deviceContention.*`` (en/pt-BR/es). The body uses
 *   ``<Trans>`` for the inline ``font-mono`` spans on processHint +
 *   hostApi so per-locale phrasing keeps the typography intact.
 */
export function DeviceContentionBanner({
  payload,
  onSelectAlternative,
}: DeviceContentionBannerProps) {
  const { t } = useTranslation("voice");
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
  const hostApiLabel =
    host_api ?? t("deviceContention.selectedDeviceFallback");

  return (
    <div
      role="alert"
      className="rounded-[var(--svx-radius-lg)] border border-[var(--svx-color-warning)]/40 bg-[var(--svx-color-warning)]/5 p-4 space-y-3"
    >
      <div className="flex items-center gap-2 text-xs font-medium text-[var(--svx-color-text-primary)]">
        <AlertTriangleIcon className="size-4 text-[var(--svx-color-warning)]" />
        {t("deviceContention.title")}
      </div>
      <p className="text-[11px] text-[var(--svx-color-text-secondary)]">
        {contending_process_hint ? (
          <Trans
            i18nKey="deviceContention.namedBody"
            ns="voice"
            values={{ processHint: contending_process_hint, hostApi: hostApiLabel }}
            components={[
              <span className="font-mono" key="proc" />,
              <span className="font-mono" key="api" />,
            ]}
          />
        ) : (
          <Trans
            i18nKey="deviceContention.anonymousBody"
            ns="voice"
            values={{ hostApi: hostApiLabel }}
            components={[<span className="font-mono" key="api" />]}
          />
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
                aria-label={t("deviceContention.retryWith", {
                  deviceName: device.name,
                })}
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
                    {t("deviceContention.retrying")}
                  </span>
                ) : null}
              </button>
            );
          })}
        </div>
      ) : (
        <p className="text-[11px] text-[var(--svx-color-text-tertiary)]">
          {t("deviceContention.noAlternatives")}
        </p>
      )}
    </div>
  );
}
