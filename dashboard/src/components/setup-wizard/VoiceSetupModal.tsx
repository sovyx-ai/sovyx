/**
 * VoiceSetupModal -- specialized setup wizard for the voice pipeline.
 *
 * Flow:
 *   1. Hardware detection (CPU, RAM, GPU, audio devices)
 *   2. Show recommended models for detected tier
 *   3. User clicks "Enable Voice"
 *   4. Backend checks deps, creates pipeline, registers in ServiceRegistry
 *   5. If deps missing: show install instructions with copy button
 *   6. If success: close modal, voice is active
 */

import { useCallback, useState } from "react";
import { toast } from "sonner";
import {
  MicIcon,
  LoaderIcon,
  CopyIcon,
  CheckIcon,
  XCircleIcon,
  PackageIcon,
  Volume2Icon,
} from "lucide-react";
import { api, ApiError } from "@/lib/api";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogTrigger,
  DialogContent,
  DialogHeader,
  DialogTitle,
  DialogDescription,
  DialogFooter,
} from "@/components/ui/dialog";
import { HardwareDetection, type SelectedDevices } from "./HardwareDetection";

interface MissingDep {
  module: string;
  package: string;
}

interface EnableResponse {
  ok: boolean;
  status?: string;
  error?: string;
  missing_deps?: MissingDep[];
  missing_models?: Array<{ name: string; install_command: string }>;
  install_command?: string;
  tts_engine?: string;
}

interface VoiceSetupModalProps {
  trigger?: React.ReactNode;
  onEnabled?: () => void;
}

export function VoiceSetupModal({ trigger, onEnabled }: VoiceSetupModalProps) {
  const [open, setOpen] = useState(false);
  const [enabling, setEnabling] = useState(false);
  const [detected, setDetected] = useState(false);
  const [depsIssue, setDepsIssue] = useState<{
    missing: MissingDep[];
    command: string;
  } | null>(null);
  const [audioError, setAudioError] = useState(false);
  const [enableError, setEnableError] = useState<string | null>(null);
  const [copied, setCopied] = useState(false);
  const [devices, setDevices] = useState<SelectedDevices>({
    input_device: null,
    output_device: null,
  });

  const handleDetected = useCallback(() => {
    setDetected(true);
  }, []);

  const handleEnable = useCallback(async () => {
    setEnabling(true);
    setDepsIssue(null);
    setAudioError(false);
    setEnableError(null);

    try {
      const result = await api.post<EnableResponse>("/api/voice/enable", devices);
      if (result.ok) {
        toast.success(
          `Voice pipeline enabled${result.tts_engine ? ` (${result.tts_engine} TTS)` : ""}`,
        );
        setOpen(false);
        onEnabled?.();
      }
    } catch (err) {
      if (err instanceof ApiError) {
        try {
          const body = JSON.parse(err.message) as EnableResponse;
          if (body.error === "missing_deps" && body.missing_deps) {
            setDepsIssue({
              missing: body.missing_deps,
              command: body.install_command ?? "pip install sovyx[voice]",
            });
          } else if (
            typeof body.error === "string" &&
            body.error.toLowerCase().includes("audio")
          ) {
            setAudioError(true);
          } else {
            setEnableError(body.error ?? "Enable failed");
          }
        } catch {
          setEnableError(err.message || "Failed to enable voice pipeline");
        }
      } else {
        setEnableError("Failed to enable voice pipeline");
      }
    } finally {
      setEnabling(false);
    }
  }, [onEnabled, devices]);

  const handleCopy = useCallback(
    (command: string) => {
      void navigator.clipboard.writeText(command);
      setCopied(true);
      setTimeout(() => setCopied(false), 2000);
    },
    [],
  );

  return (
    <Dialog open={open} onOpenChange={setOpen}>
      <DialogTrigger
        render={
          (trigger as React.ReactElement) ?? (
            <Button variant="outline" size="sm">
              <MicIcon className="mr-1.5 size-3.5" />
              Set up Voice
            </Button>
          )
        }
      />
      <DialogContent className="sm:max-w-lg">
        <DialogHeader>
          <DialogTitle>Set up Voice</DialogTitle>
          <DialogDescription>
            Detect your hardware and enable the voice pipeline for local
            speech-to-text, text-to-speech, and voice activity detection.
          </DialogDescription>
        </DialogHeader>

        <div className="py-2 space-y-4">
          <HardwareDetection onDetected={handleDetected} onDeviceChange={setDevices} />

          {/* Dependency issue panel */}
          {depsIssue && (
            <div className="rounded-[var(--svx-radius-lg)] border border-[var(--svx-color-warning)]/40 bg-[var(--svx-color-warning)]/5 p-4 space-y-3">
              <div className="flex items-center gap-2 text-xs font-medium text-[var(--svx-color-text-primary)]">
                <PackageIcon className="size-4 text-[var(--svx-color-warning)]" />
                Voice Dependencies
              </div>

              <div className="space-y-1.5">
                {depsIssue.missing.map((dep) => (
                  <div
                    key={dep.module}
                    className="flex items-center gap-2 text-xs"
                  >
                    <XCircleIcon className="size-3 text-[var(--svx-color-error)] shrink-0" />
                    <span className="font-mono text-[var(--svx-color-text-secondary)]">
                      {dep.package}
                    </span>
                    <span className="text-[var(--svx-color-text-tertiary)]">
                      (not installed)
                    </span>
                  </div>
                ))}
              </div>

              <div className="space-y-2">
                <p className="text-[11px] text-[var(--svx-color-text-secondary)]">
                  One-time install:
                </p>
                <div className="flex items-center gap-2 rounded-[var(--svx-radius-md)] border border-[var(--svx-color-border-default)] bg-[var(--svx-color-bg-surface)] px-3 py-2">
                  <code className="flex-1 text-xs font-mono text-[var(--svx-color-text-primary)]">
                    {depsIssue.command}
                  </code>
                  <button
                    type="button"
                    onClick={() => handleCopy(depsIssue.command)}
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
                <p className="text-[11px] text-[var(--svx-color-text-tertiary)]">
                  After installing, restart the Sovyx daemon and click Enable
                  Voice again.
                </p>
              </div>
            </div>
          )}

          {/* Audio hardware error panel */}
          {audioError && (
            <div className="rounded-[var(--svx-radius-lg)] border border-[var(--svx-color-error)]/40 bg-[var(--svx-color-error)]/5 p-4 space-y-3">
              <div className="flex items-center gap-2 text-xs font-medium text-[var(--svx-color-text-primary)]">
                <Volume2Icon className="size-4 text-[var(--svx-color-error)]" />
                No Audio Hardware Detected
              </div>
              <p className="text-xs text-[var(--svx-color-text-secondary)] leading-relaxed">
                Voice requires a microphone and speakers. Connect audio hardware, then click Enable Voice again.
              </p>
            </div>
          )}

          {/* Generic error */}
          {enableError && !depsIssue && !audioError && (
            <div className="flex items-center gap-2 rounded-[var(--svx-radius-md)] bg-[var(--svx-color-error)]/10 px-3 py-2.5 text-xs text-[var(--svx-color-error)]">
              <XCircleIcon className="size-3.5 shrink-0" />
              <span>{enableError}</span>
            </div>
          )}
        </div>

        <DialogFooter showCloseButton>
          {detected && (
            <Button
              onClick={handleEnable}
              disabled={enabling}
              className="min-w-[140px]"
            >
              {enabling ? (
                <LoaderIcon className="mr-2 size-3.5 animate-spin" />
              ) : (
                <MicIcon className="mr-2 size-3.5" />
              )}
              {enabling ? "Enabling..." : "Enable Voice"}
            </Button>
          )}
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
