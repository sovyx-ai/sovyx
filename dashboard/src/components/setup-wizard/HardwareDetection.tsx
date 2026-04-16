/**
 * HardwareDetection -- shows detected hardware + recommended models.
 *
 * Fetches from GET /api/voice/hardware-detect and displays:
 * - CPU cores, RAM, GPU, tier
 * - Audio device availability (gate for voice enable)
 * - Recommended models with download sizes
 */

import { memo, useEffect, useState } from "react";
import { api } from "@/lib/api";
import {
  CpuIcon,
  HardDriveIcon,
  MicIcon,
  Volume2Icon,
  AlertTriangleIcon,
  CheckCircle2Icon,
  LoaderIcon,
} from "lucide-react";
import { cn } from "@/lib/utils";

interface HardwareInfo {
  hardware: {
    cpu_cores: number;
    ram_mb: number;
    has_gpu: boolean;
    gpu_vram_mb: number;
    tier: string;
  };
  audio: {
    available: boolean;
    input_devices: string[];
    output_devices: string[];
  };
  recommended_models: Array<{
    name: string;
    category: string;
    size_mb: number;
    download_available: boolean;
    description: string;
  }>;
  total_download_mb: number;
}

interface HardwareDetectionProps {
  onDetected?: (info: HardwareInfo) => void;
}

function HardwareDetectionImpl({ onDetected }: HardwareDetectionProps) {
  const [loading, setLoading] = useState(true);
  const [info, setInfo] = useState<HardwareInfo | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    api
      .get<HardwareInfo>("/api/voice/hardware-detect")
      .then((data) => {
        setInfo(data);
        onDetected?.(data);
      })
      .catch((err) => {
        setError(String(err));
      })
      .finally(() => setLoading(false));
  }, [onDetected]);

  if (loading) {
    return (
      <div className="flex items-center justify-center py-6 text-sm text-[var(--svx-color-text-tertiary)]">
        <LoaderIcon className="mr-2 size-4 animate-spin" />
        Detecting hardware...
      </div>
    );
  }

  if (error || !info) {
    return (
      <div className="rounded-[var(--svx-radius-md)] bg-[var(--svx-color-error)]/10 px-4 py-3 text-xs text-[var(--svx-color-error)]">
        <AlertTriangleIcon className="mr-1.5 inline size-3.5" />
        {error ?? "Hardware detection failed"}
      </div>
    );
  }

  const { hardware, audio, recommended_models, total_download_mb } = info;

  return (
    <div className="space-y-4">
      {/* Hardware summary */}
      <div className="grid grid-cols-2 gap-3">
        <InfoChip icon={CpuIcon} label="CPU" value={`${hardware.cpu_cores} cores`} />
        <InfoChip
          icon={HardDriveIcon}
          label="RAM"
          value={`${Math.round(hardware.ram_mb / 1024)} GB`}
        />
        <InfoChip
          icon={MicIcon}
          label="Input"
          value={audio.input_devices.length > 0 ? (audio.input_devices[0] ?? "Unknown") : "None"}
          warn={!audio.available}
        />
        <InfoChip
          icon={Volume2Icon}
          label="Output"
          value={audio.output_devices.length > 0 ? (audio.output_devices[0] ?? "Unknown") : "None"}
          warn={!audio.available}
        />
      </div>

      {/* Tier badge */}
      <div className="flex items-center gap-2">
        <span className="rounded-full bg-[var(--svx-color-brand-primary)]/10 px-3 py-1 text-xs font-medium text-[var(--svx-color-brand-primary)]">
          {hardware.tier}
        </span>
        {hardware.has_gpu && (
          <span className="rounded-full bg-[var(--svx-color-success)]/10 px-3 py-1 text-xs font-medium text-[var(--svx-color-success)]">
            GPU {hardware.gpu_vram_mb} MB
          </span>
        )}
      </div>

      {/* Audio warning */}
      {!audio.available && (
        <div className="flex items-center gap-2 rounded-[var(--svx-radius-md)] bg-[var(--svx-color-warning)]/10 px-3 py-2.5 text-xs text-[var(--svx-color-warning)]">
          <AlertTriangleIcon className="size-3.5 shrink-0" />
          <span>
            No audio devices detected. Voice pipeline requires a microphone and speaker.
          </span>
        </div>
      )}

      {/* Recommended models */}
      <div className="space-y-2">
        <h4 className="text-xs font-medium text-[var(--svx-color-text-secondary)]">
          Recommended models ({total_download_mb} MB total)
        </h4>
        <div className="space-y-1.5">
          {recommended_models.map((m) => (
            <div
              key={m.name}
              className="flex items-center justify-between rounded-[var(--svx-radius-md)] border border-[var(--svx-color-border-default)] px-3 py-2"
            >
              <div className="min-w-0">
                <span className="text-xs font-medium text-[var(--svx-color-text-primary)]">
                  {m.name}
                </span>
                <p className="text-[11px] text-[var(--svx-color-text-tertiary)]">
                  {m.description}
                </p>
              </div>
              <div className="flex items-center gap-2 shrink-0 ml-3">
                <span className="text-[11px] text-[var(--svx-color-text-tertiary)]">
                  {m.size_mb} MB
                </span>
                {m.download_available ? (
                  <CheckCircle2Icon className="size-3.5 text-[var(--svx-color-success)]" />
                ) : (
                  <span className="text-[10px] text-[var(--svx-color-warning)]">manual</span>
                )}
              </div>
            </div>
          ))}
        </div>
      </div>
    </div>
  );
}

function InfoChip({
  icon: Icon,
  label,
  value,
  warn,
}: {
  icon: React.ComponentType<{ className?: string }>;
  label: string;
  value: string;
  warn?: boolean;
}) {
  return (
    <div
      className={cn(
        "flex items-center gap-2 rounded-[var(--svx-radius-md)] border px-3 py-2",
        warn
          ? "border-[var(--svx-color-warning)]/40 bg-[var(--svx-color-warning)]/5"
          : "border-[var(--svx-color-border-default)] bg-[var(--svx-color-bg-surface)]",
      )}
    >
      <Icon
        className={cn(
          "size-3.5 shrink-0",
          warn ? "text-[var(--svx-color-warning)]" : "text-[var(--svx-color-text-tertiary)]",
        )}
      />
      <div className="min-w-0">
        <div className="text-[10px] text-[var(--svx-color-text-tertiary)]">{label}</div>
        <div className="truncate text-xs font-medium text-[var(--svx-color-text-primary)]">
          {value}
        </div>
      </div>
    </div>
  );
}

export const HardwareDetection = memo(HardwareDetectionImpl);
