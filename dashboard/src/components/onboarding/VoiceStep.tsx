import { useCallback, useState } from "react";
import { useTranslation } from "react-i18next";
import {
  MicIcon,
  LoaderIcon,
  CheckCircle2Icon,
  CopyIcon,
  CheckIcon,
  PackageIcon,
} from "lucide-react";
import { CALIBRATION_WIZARD_ENABLED } from "@/lib/feature-flags";
import { api, ApiError } from "@/lib/api";
import { VoiceCalibrationStep } from "@/components/onboarding/VoiceCalibrationStep";
import { Button } from "@/components/ui/button";
import {
  HardwareDetection,
  type SelectedDevices,
  type SelectedVoice,
} from "@/components/setup-wizard";
import { VoiceSetupWizard } from "@/components/setup-wizard/VoiceSetupWizard";
import {
  DeviceContentionBanner,
  type AlternativeDevice,
  type CaptureDeviceContendedPayload,
} from "@/components/voice/DeviceContentionBanner";
import { VoiceCaptureDeviceContendedErrorSchema } from "@/types/schemas";

interface VoiceStepProps {
  onConfigured: () => void;
  onSkip: () => void;
  /**
   * UI language chosen in the personality step (or ``navigator.language``
   * fallback). Forwarded to HardwareDetection so the voice-test picker
   * seeds the recommended voice in the user's language — avoids the
   * English-default-voice coherence bug.
   */
  language?: string;
}

interface EnableResult {
  ok: boolean;
  status?: string;
  error?: string;
  missing_deps?: Array<{ module: string; package: string }>;
  install_command?: string;
  tts_engine?: string;
}

export function VoiceStep({ onConfigured, onSkip, language }: VoiceStepProps) {
  // Two namespaces: ``onboarding`` is the dominant context (page copy
  // + error messages); ``voice`` is reused for wizard-related strings
  // shared with voice.tsx (Mission v0.30.4 §wizard.* keys).
  const { t } = useTranslation("onboarding");
  const { t: tVoice } = useTranslation("voice");
  const [detected, setDetected] = useState(false);
  const [enabling, setEnabling] = useState(false);
  const [enabled, setEnabled] = useState(false);
  const [missingDeps, setMissingDeps] = useState<{
    deps: Array<{ module: string; package: string }>;
    command: string;
  } | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [contention, setContention] = useState<CaptureDeviceContendedPayload | null>(
    null,
  );
  const [copied, setCopied] = useState(false);
  const [wizardOpen, setWizardOpen] = useState(false);
  const [wizardTested, setWizardTested] = useState(false);
  const [devices, setDevices] = useState<SelectedDevices>({
    input_device: null,
    output_device: null,
  });
  const [voiceSelection, setVoiceSelection] = useState<SelectedVoice>({
    language: null,
    voice: null,
  });

  const enableWithDevices = useCallback(
    async (deviceSpec: SelectedDevices, inputDeviceName?: string) => {
      setEnabling(true);
      setMissingDeps(null);
      setError(null);
      setContention(null);
      try {
        // Only send voice_id / language when the picker actually resolved —
        // the backend validates against the catalog, so passing a stale
        // `null` dropdown value would 400. The effective language still
        // falls back to MindConfig on the server if we omit it here.
        const body: Record<string, unknown> = { ...deviceSpec };
        if (voiceSelection.voice) body.voice_id = voiceSelection.voice;
        if (voiceSelection.language) body.language = voiceSelection.language;
        if (inputDeviceName) {
          body.input_device_name = inputDeviceName;
        }
        const result = await api.post<EnableResult>("/api/voice/enable", body);
        if (result.ok) {
          setEnabled(true);
        }
      } catch (err) {
        if (err instanceof ApiError) {
          if (err.status === 429) {
            setError(t("voice.errors.tooManyRequests"));
          } else {
            try {
              const parsed = JSON.parse(err.message) as Record<string, unknown>;
              // T9 — capture_device_contended takes priority: render
              // the chip banner instead of the generic error toast.
              const contentionParse =
                VoiceCaptureDeviceContendedErrorSchema.safeParse(parsed);
              if (contentionParse.success) {
                setContention(contentionParse.data);
              } else {
                const body = parsed as unknown as EnableResult;
                if (body.error === "missing_deps" && body.missing_deps) {
                  setMissingDeps({
                    deps: body.missing_deps,
                    command:
                      body.install_command ?? "pip install sovyx[voice]",
                  });
                } else if (
                  typeof body.error === "string" &&
                  body.error.toLowerCase().includes("audio")
                ) {
                  setError(t("voice.errors.noAudio"));
                } else {
                  setError(body.error ?? t("voice.errors.enableFailed"));
                }
              }
            } catch {
              setError(err.message || t("voice.errors.pipelineFailed"));
            }
          }
        } else {
          setError(t("voice.errors.pipelineFailed"));
        }
      } finally {
        setEnabling(false);
      }
    },
    [voiceSelection, t],
  );

  const handleEnable = useCallback(async () => {
    await enableWithDevices(devices);
  }, [devices, enableWithDevices]);

  const handleSelectAlternative = useCallback(
    (device: AlternativeDevice) => {
      const nextDevices: SelectedDevices = {
        ...devices,
        input_device: device.index,
      };
      setDevices(nextDevices);
      void enableWithDevices(nextDevices, device.name);
    },
    [devices, enableWithDevices],
  );

  const handleCopy = useCallback((cmd: string) => {
    void navigator.clipboard.writeText(cmd);
    setCopied(true);
    setTimeout(() => setCopied(false), 2000);
  }, []);

  const handleDetected = useCallback(() => setDetected(true), []);
  const handleDeviceChange = useCallback((d: SelectedDevices) => setDevices(d), []);
  const handleVoiceChange = useCallback((v: SelectedVoice) => setVoiceSelection(v), []);

  return (
    <div className="space-y-6">
      <div>
        <h2 className="text-xl font-semibold text-[var(--svx-color-text-primary)]">
          {t("voice.title")}
        </h2>
        <p className="mt-1 text-sm text-[var(--svx-color-text-secondary)]">
          {t("voice.subtitle")}
        </p>
      </div>

      <HardwareDetection
        onDetected={handleDetected}
        onDeviceChange={handleDeviceChange}
        onVoiceChange={handleVoiceChange}
        initialLanguage={language}
      />

      {/* L3 voice calibration wizard step — gated on
          CALIBRATION_WIZARD_ENABLED (default false until v0.31.0).
          When enabled, replaces the legacy VoiceSetupWizard inline
          mount below. Operator falls back to the legacy wizard via
          the FALLBACK terminal state (rendered with a "Use simple
          setup" button by VoiceCalibrationStep). */}
      {CALIBRATION_WIZARD_ENABLED && !enabled && !missingDeps && (
        <VoiceCalibrationStep
          mindId="default"
          onCompleted={onConfigured}
          onFallback={() => {
            // Re-open the legacy wizard inline for operators who
            // hit the FALLBACK terminal. The wizardOpen toggle
            // re-uses the existing UI below.
            setWizardOpen(true);
          }}
        />
      )}

      {/* Optional pre-enable mic test — wizard mounts inline. The wizard
          itself is application-scope (does not persist to mind.yaml in
          v0.30.x); operator still completes the enable flow below to
          activate voice. Mirror of the voice.tsx Section pattern. */}
      {!enabled && !missingDeps && (
        <div className="rounded-[var(--svx-radius-md)] border border-[var(--svx-color-border-default)] p-3">
          {wizardOpen ? (
            <VoiceSetupWizard
              onComplete={() => {
                setWizardTested(true);
                setWizardOpen(false);
              }}
              onCancel={() => setWizardOpen(false)}
            />
          ) : (
            <div className="flex items-center justify-between gap-3">
              <p className="text-xs text-[var(--svx-color-text-secondary)]">
                {wizardTested
                  ? tVoice("wizard.testedProceedHint")
                  : tVoice("wizard.openHintOptional")}
              </p>
              <button
                type="button"
                onClick={() => setWizardOpen(true)}
                className="shrink-0 rounded-[var(--svx-radius-md)] border border-[var(--svx-color-accent)] bg-[var(--svx-color-accent-soft)] px-3 py-1 text-xs font-medium text-[var(--svx-color-accent)] hover:bg-[var(--svx-color-accent)] hover:text-white"
              >
                {wizardTested
                  ? tVoice("wizard.reopenButton")
                  : tVoice("wizard.openButton")}
              </button>
            </div>
          )}
        </div>
      )}

      {/* Success state */}
      {enabled && (
        <div className="flex items-center gap-2 rounded-[var(--svx-radius-md)] bg-[var(--svx-color-success)]/10 px-4 py-3 text-xs text-[var(--svx-color-success)]">
          <CheckCircle2Icon className="size-4 shrink-0" />
          <span>{t("voice.successMessage")}</span>
        </div>
      )}

      {/* Missing deps — install instructions */}
      {missingDeps && (
        <div className="rounded-[var(--svx-radius-lg)] border border-[var(--svx-color-warning)]/40 bg-[var(--svx-color-warning)]/5 p-4 space-y-3">
          <div className="flex items-center gap-2 text-xs font-medium text-[var(--svx-color-text-primary)]">
            <PackageIcon className="size-4 text-[var(--svx-color-warning)]" />
            {t("voice.missingDepsTitle")}
          </div>
          <div className="space-y-2">
            <p className="text-[11px] text-[var(--svx-color-text-secondary)]">
              {t("voice.missingDepsHint")}
            </p>
            <div className="flex items-center gap-2 rounded-[var(--svx-radius-md)] border border-[var(--svx-color-border-default)] bg-[var(--svx-color-bg-surface)] px-3 py-2">
              <code className="flex-1 text-xs font-mono text-[var(--svx-color-text-primary)]">
                {missingDeps.command}
              </code>
              <button
                type="button"
                onClick={() => handleCopy(missingDeps.command)}
                className="shrink-0 rounded-[var(--svx-radius-sm)] p-1 text-[var(--svx-color-text-tertiary)] hover:text-[var(--svx-color-text-primary)] transition-colors"
                aria-label={t("voice.copyCommandAria")}
              >
                {copied ? (
                  <CheckIcon className="size-3.5 text-[var(--svx-color-success)]" />
                ) : (
                  <CopyIcon className="size-3.5" />
                )}
              </button>
            </div>
          </div>
        </div>
      )}

      {/* T9 — session-manager-contention banner with clickable chips */}
      {contention && (
        <DeviceContentionBanner
          payload={contention}
          onSelectAlternative={enabling ? null : handleSelectAlternative}
        />
      )}

      {/* Generic error */}
      {error && !missingDeps && !contention && (
        <div className="rounded-[var(--svx-radius-md)] bg-[var(--svx-color-error)]/10 px-3 py-2.5 text-xs text-[var(--svx-color-error)]">
          {error}
        </div>
      )}

      {/* Actions */}
      <div className="flex items-center justify-between">
        <button
          type="button"
          onClick={onSkip}
          className="text-xs text-[var(--svx-color-text-tertiary)] hover:text-[var(--svx-color-text-secondary)]"
        >
          {t("voice.skipForNow")}
        </button>
        <div className="flex gap-2">
          {detected && !enabled && !missingDeps && (
            <Button onClick={handleEnable} disabled={enabling}>
              {enabling ? (
                <LoaderIcon className="mr-1.5 size-3.5 animate-spin" />
              ) : (
                <MicIcon className="mr-1.5 size-3.5" />
              )}
              {enabling ? t("voice.enablingButton") : t("voice.enableButton")}
            </Button>
          )}
          {(enabled || missingDeps) && (
            <Button onClick={onConfigured}>{t("voice.continueButton")}</Button>
          )}
        </div>
      </div>
    </div>
  );
}
