import { useCallback, useState } from "react";
import {
  MicIcon,
  LoaderIcon,
  CheckCircle2Icon,
  CopyIcon,
  CheckIcon,
  PackageIcon,
} from "lucide-react";
import { api, ApiError } from "@/lib/api";
import { Button } from "@/components/ui/button";
import { HardwareDetection, type SelectedDevices } from "@/components/setup-wizard";

interface VoiceStepProps {
  onConfigured: () => void;
  onSkip: () => void;
}

interface EnableResult {
  ok: boolean;
  status?: string;
  error?: string;
  missing_deps?: Array<{ module: string; package: string }>;
  install_command?: string;
  tts_engine?: string;
}

export function VoiceStep({ onConfigured, onSkip }: VoiceStepProps) {
  const [detected, setDetected] = useState(false);
  const [enabling, setEnabling] = useState(false);
  const [enabled, setEnabled] = useState(false);
  const [missingDeps, setMissingDeps] = useState<{
    deps: Array<{ module: string; package: string }>;
    command: string;
  } | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [copied, setCopied] = useState(false);
  const [devices, setDevices] = useState<SelectedDevices>({
    input_device: null,
    output_device: null,
  });

  const handleEnable = useCallback(async () => {
    setEnabling(true);
    setMissingDeps(null);
    setError(null);
    try {
      const result = await api.post<EnableResult>("/api/voice/enable", devices);
      if (result.ok) {
        setEnabled(true);
      }
    } catch (err) {
      if (err instanceof ApiError) {
        try {
          const body = JSON.parse(err.message) as EnableResult;
          if (body.error === "missing_deps" && body.missing_deps) {
            setMissingDeps({
              deps: body.missing_deps,
              command: body.install_command ?? "pip install sovyx[voice]",
            });
          } else if (typeof body.error === "string" && body.error.toLowerCase().includes("audio")) {
            setError("No audio hardware detected. Connect a microphone and speakers.");
          } else {
            setError(body.error ?? "Failed to enable voice");
          }
        } catch {
          setError(err.message || "Failed to enable voice pipeline");
        }
      } else {
        setError("Failed to enable voice pipeline");
      }
    } finally {
      setEnabling(false);
    }
  }, [devices]);

  const handleCopy = useCallback((cmd: string) => {
    void navigator.clipboard.writeText(cmd);
    setCopied(true);
    setTimeout(() => setCopied(false), 2000);
  }, []);

  return (
    <div className="space-y-6">
      <div>
        <h2 className="text-xl font-semibold text-[var(--svx-color-text-primary)]">
          Set up Voice
        </h2>
        <p className="mt-1 text-sm text-[var(--svx-color-text-secondary)]">
          Optional — enable local speech-to-text and text-to-speech.
        </p>
      </div>

      <HardwareDetection onDetected={() => setDetected(true)} onDeviceChange={setDevices} />

      {/* Success state */}
      {enabled && (
        <div className="flex items-center gap-2 rounded-[var(--svx-radius-md)] bg-[var(--svx-color-success)]/10 px-4 py-3 text-xs text-[var(--svx-color-success)]">
          <CheckCircle2Icon className="size-4 shrink-0" />
          <span>Voice pipeline enabled. You can talk to your companion.</span>
        </div>
      )}

      {/* Missing deps — install instructions */}
      {missingDeps && (
        <div className="rounded-[var(--svx-radius-lg)] border border-[var(--svx-color-warning)]/40 bg-[var(--svx-color-warning)]/5 p-4 space-y-3">
          <div className="flex items-center gap-2 text-xs font-medium text-[var(--svx-color-text-primary)]">
            <PackageIcon className="size-4 text-[var(--svx-color-warning)]" />
            Voice packages not installed
          </div>
          <div className="space-y-2">
            <p className="text-[11px] text-[var(--svx-color-text-secondary)]">
              Run this command in your terminal, then restart Sovyx:
            </p>
            <div className="flex items-center gap-2 rounded-[var(--svx-radius-md)] border border-[var(--svx-color-border-default)] bg-[var(--svx-color-bg-surface)] px-3 py-2">
              <code className="flex-1 text-xs font-mono text-[var(--svx-color-text-primary)]">
                {missingDeps.command}
              </code>
              <button
                type="button"
                onClick={() => handleCopy(missingDeps.command)}
                className="shrink-0 rounded-[var(--svx-radius-sm)] p-1 text-[var(--svx-color-text-tertiary)] hover:text-[var(--svx-color-text-primary)] transition-colors"
                aria-label="Copy command"
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

      {/* Generic error */}
      {error && !missingDeps && (
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
          Skip for now
        </button>
        <div className="flex gap-2">
          {detected && !enabled && !missingDeps && (
            <Button onClick={handleEnable} disabled={enabling}>
              {enabling ? (
                <LoaderIcon className="mr-1.5 size-3.5 animate-spin" />
              ) : (
                <MicIcon className="mr-1.5 size-3.5" />
              )}
              {enabling ? "Enabling..." : "Enable Voice"}
            </Button>
          )}
          {(enabled || missingDeps) && (
            <Button onClick={onConfigured}>Continue</Button>
          )}
        </div>
      </div>
    </div>
  );
}
