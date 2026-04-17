/**
 * HardwareDetection -- shows detected hardware + recommended models.
 *
 * Fetches from GET /api/voice/hardware-detect and displays:
 * - CPU cores, RAM, GPU, tier
 * - Audio device dropdowns for input/output selection
 * - Recommended models with download sizes
 */

import { memo, useCallback, useEffect, useState } from "react";
import { api } from "@/lib/api";
import {
  CpuIcon,
  HardDriveIcon,
  MicIcon,
  Volume2Icon,
  AlertTriangleIcon,
  CheckCircle2Icon,
  LoaderIcon,
  ChevronDownIcon,
} from "lucide-react";
import { cn } from "@/lib/utils";

interface AudioDevice {
  index: number;
  name: string;
  is_default: boolean;
}

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
    input_devices: AudioDevice[];
    output_devices: AudioDevice[];
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

export interface SelectedDevices {
  input_device: number | null;
  output_device: number | null;
}

interface HardwareDetectionProps {
  onDetected?: (info: HardwareInfo) => void;
  onDeviceChange?: (devices: SelectedDevices) => void;
}

function findDefault(devices: AudioDevice[]): number | null {
  const def = devices.find((d) => d.is_default);
  return def?.index ?? devices[0]?.index ?? null;
}

function HardwareDetectionImpl({ onDetected, onDeviceChange }: HardwareDetectionProps) {
  const [loading, setLoading] = useState(true);
  const [info, setInfo] = useState<HardwareInfo | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [selectedInput, setSelectedInput] = useState<number | null>(null);
  const [selectedOutput, setSelectedOutput] = useState<number | null>(null);

  useEffect(() => {
    api
      .get<HardwareInfo>("/api/voice/hardware-detect")
      .then((data) => {
        setInfo(data);
        const defIn = findDefault(data.audio.input_devices);
        const defOut = findDefault(data.audio.output_devices);
        setSelectedInput(defIn);
        setSelectedOutput(defOut);
        onDetected?.(data);
        onDeviceChange?.({ input_device: defIn, output_device: defOut });
      })
      .catch((err) => {
        setError(String(err));
      })
      .finally(() => setLoading(false));
  }, [onDetected, onDeviceChange]);

  const handleInputChange = useCallback(
    (index: number) => {
      setSelectedInput(index);
      onDeviceChange?.({ input_device: index, output_device: selectedOutput });
    },
    [selectedOutput, onDeviceChange],
  );

  const handleOutputChange = useCallback(
    (index: number) => {
      setSelectedOutput(index);
      onDeviceChange?.({ input_device: selectedInput, output_device: index });
    },
    [selectedInput, onDeviceChange],
  );

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
      </div>

      {/* Audio device selectors */}
      <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
        <DeviceSelect
          icon={MicIcon}
          label="Input"
          devices={audio.input_devices}
          selected={selectedInput}
          onChange={handleInputChange}
          warn={!audio.available}
        />
        <DeviceSelect
          icon={Volume2Icon}
          label="Output"
          devices={audio.output_devices}
          selected={selectedOutput}
          onChange={handleOutputChange}
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
              <div className="ml-3 flex shrink-0 items-center gap-2">
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
}: {
  icon: React.ComponentType<{ className?: string }>;
  label: string;
  value: string;
}) {
  return (
    <div className="flex items-center gap-2 rounded-[var(--svx-radius-md)] border border-[var(--svx-color-border-default)] bg-[var(--svx-color-bg-surface)] px-3 py-2">
      <Icon className="size-3.5 shrink-0 text-[var(--svx-color-text-tertiary)]" />
      <div className="min-w-0">
        <div className="text-[10px] text-[var(--svx-color-text-tertiary)]">{label}</div>
        <div className="truncate text-xs font-medium text-[var(--svx-color-text-primary)]">
          {value}
        </div>
      </div>
    </div>
  );
}

function DeviceSelect({
  icon: Icon,
  label,
  devices,
  selected,
  onChange,
  warn,
}: {
  icon: React.ComponentType<{ className?: string }>;
  label: string;
  devices: AudioDevice[];
  selected: number | null;
  onChange: (index: number) => void;
  warn?: boolean;
}) {
  if (devices.length === 0) {
    return (
      <div
        className={cn(
          "flex items-center gap-2 rounded-[var(--svx-radius-md)] border px-3 py-2",
          "border-[var(--svx-color-warning)]/40 bg-[var(--svx-color-warning)]/5",
        )}
      >
        <Icon className="size-3.5 shrink-0 text-[var(--svx-color-warning)]" />
        <div className="min-w-0">
          <div className="text-[10px] text-[var(--svx-color-text-tertiary)]">{label}</div>
          <div className="text-xs font-medium text-[var(--svx-color-warning)]">None</div>
        </div>
      </div>
    );
  }

  return (
    <div className="space-y-1">
      <div
        className={cn(
          "flex items-center gap-1.5 text-[10px]",
          warn ? "text-[var(--svx-color-warning)]" : "text-[var(--svx-color-text-tertiary)]",
        )}
      >
        <Icon className="size-3" />
        <span>{label}</span>
      </div>
      <div className="relative">
        <select
          value={selected ?? ""}
          onChange={(e) => onChange(Number(e.target.value))}
          className={cn(
            "w-full cursor-pointer rounded-[var(--svx-radius-md)] border px-3 py-2 pr-8 text-xs text-[var(--svx-color-text-primary)] outline-none",
            warn
              ? "border-[var(--svx-color-warning)]/40 bg-[var(--svx-color-warning)]/5"
              : "border-[var(--svx-color-border-default)] bg-[var(--svx-color-bg-elevated)]",
          )}
          style={{ colorScheme: "dark" }}
        >
          {devices.map((d) => (
            <option key={d.index} value={d.index}>
              {d.name}
            </option>
          ))}
        </select>
        <ChevronDownIcon className="pointer-events-none absolute right-2.5 top-1/2 size-3.5 -translate-y-1/2 text-[var(--svx-color-text-tertiary)]" />
      </div>
    </div>
  );
}

export const HardwareDetection = memo(HardwareDetectionImpl);
