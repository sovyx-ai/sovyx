/**
 * HardwareDetection -- shows detected hardware + recommended models.
 *
 * Fetches from GET /api/voice/hardware-detect and displays:
 * - CPU cores, RAM, GPU, tier
 * - Audio device dropdowns for input/output selection
 * - Recommended models with download sizes
 */

import { memo, useCallback, useEffect, useRef, useState } from "react";
import { api } from "@/lib/api";
import {
  CpuIcon,
  HardDriveIcon,
  MicIcon,
  Volume2Icon,
  AlertTriangleIcon,
  CheckCircle2Icon,
  CloudDownloadIcon,
  DownloadIcon,
  LoaderIcon,
  ChevronDownIcon,
  ActivityIcon,
} from "lucide-react";
import { cn } from "@/lib/utils";
import { useAudioLevelStream } from "@/hooks/use-audio-level-stream";
import { useVoiceCatalog } from "@/hooks/use-voice-catalog";
import { useVoiceModels } from "@/hooks/use-voice-models";
import { AudioLevelMeter } from "./AudioLevelMeter";
import { TtsTestButton } from "./TtsTestButton";

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

/**
 * Voice selection surfaced by the in-wizard picker. ``language`` is a
 * catalog-canonical code (``pt-br``, ``en-us``, …) and ``voice`` is a
 * Kokoro voice id (``pf_dora``, ``af_heart``, …) or ``null`` while the
 * catalog is still loading.
 */
export interface SelectedVoice {
  language: string | null;
  voice: string | null;
}

interface HardwareDetectionProps {
  onDetected?: (info: HardwareInfo) => void;
  onDeviceChange?: (devices: SelectedDevices) => void;
  /**
   * Fired whenever the user (or the initial auto-seed) changes the
   * voice-test language or voice. Parents that persist the selection
   * (``VoiceStep`` → ``POST /api/voice/enable``) listen to this so the
   * pick flows end-to-end instead of dying inside the picker.
   */
  onVoiceChange?: (selection: SelectedVoice) => void;
  /**
   * UI language picked earlier in onboarding (typically from the
   * personality step or ``navigator.language``). Used as the initial
   * value for the voice-test language selector and to seed the
   * recommended voice. If omitted the picker falls back to ``en``.
   */
  initialLanguage?: string;
}

function findDefault(devices: AudioDevice[]): number | null {
  const def = devices.find((d) => d.is_default);
  return def?.index ?? devices[0]?.index ?? null;
}

function HardwareDetectionImpl({
  onDetected,
  onDeviceChange,
  onVoiceChange,
  initialLanguage,
}: HardwareDetectionProps) {
  const [loading, setLoading] = useState(true);
  const [info, setInfo] = useState<HardwareInfo | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [selectedInput, setSelectedInput] = useState<number | null>(null);
  const [selectedOutput, setSelectedOutput] = useState<number | null>(null);

  // Voice-test language + voice pickers. The catalog loads on mount;
  // once it arrives we canonicalise the prop and set the recommended
  // voice so the user can immediately press "Test speakers" in their
  // own language without touching the dropdowns.
  const catalog = useVoiceCatalog();
  const [selectedLanguage, setSelectedLanguage] = useState<string | null>(null);
  const [selectedVoice, setSelectedVoice] = useState<string | null>(null);

  // Stash the voice-change callback in a ref so the seed effect stays
  // `[catalog, initialLanguage, selectedLanguage]`-deps — parents that
  // pass an inline callback would otherwise re-fire the effect and re-
  // emit the seed on every render.
  const onVoiceChangeRef = useRef(onVoiceChange);
  useEffect(() => {
    onVoiceChangeRef.current = onVoiceChange;
  }, [onVoiceChange]);

  useEffect(() => {
    if (!catalog.catalog || selectedLanguage !== null) return;
    const canon = catalog.normaliseLanguage(initialLanguage ?? "en");
    const fallback = catalog.catalog.supported_languages[0] ?? null;
    const lang = canon ?? fallback;
    setSelectedLanguage(lang);
    const recommended = lang !== null ? catalog.recommendedFor(lang) : null;
    if (recommended !== null) {
      setSelectedVoice(recommended);
    }
    onVoiceChangeRef.current?.({ language: lang, voice: recommended });
  }, [catalog, initialLanguage, selectedLanguage]);

  const handleLanguageChange = useCallback(
    (lang: string) => {
      setSelectedLanguage(lang);
      const recommended = catalog.recommendedFor(lang);
      setSelectedVoice(recommended);
      onVoiceChangeRef.current?.({ language: lang, voice: recommended });
    },
    [catalog],
  );

  const handleVoiceChange = useCallback(
    (voice: string) => {
      setSelectedVoice(voice);
      onVoiceChangeRef.current?.({ language: selectedLanguage, voice });
    },
    [selectedLanguage],
  );

  // Stash callbacks in refs so the fetch effect can stay `[]`-deps.
  // Without this, callers that pass inline callbacks (the common case —
  // see VoiceStep) change the prop identity on every render, which
  // re-fires the effect and pounds /api/voice/hardware-detect into a
  // 429 within seconds.
  const onDetectedRef = useRef(onDetected);
  const onDeviceChangeRef = useRef(onDeviceChange);
  useEffect(() => {
    onDetectedRef.current = onDetected;
    onDeviceChangeRef.current = onDeviceChange;
  }, [onDetected, onDeviceChange]);

  useEffect(() => {
    api
      .get<HardwareInfo>("/api/voice/hardware-detect")
      .then((data) => {
        setInfo(data);
        setError(null);
        const defIn = findDefault(data.audio.input_devices);
        const defOut = findDefault(data.audio.output_devices);
        setSelectedInput(defIn);
        setSelectedOutput(defOut);
        onDetectedRef.current?.(data);
        onDeviceChangeRef.current?.({ input_device: defIn, output_device: defOut });
      })
      .catch((err) => {
        setError(String(err));
      })
      .finally(() => setLoading(false));
  }, []);

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

  if (!info) {
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
      {/* Transient error — keep the wizard usable even when a later
          refresh or adjacent call (e.g. /enable) returned 429. */}
      {error && (
        <div className="rounded-[var(--svx-radius-md)] bg-[var(--svx-color-error)]/10 px-3 py-2 text-[11px] text-[var(--svx-color-error)]">
          <AlertTriangleIcon className="mr-1.5 inline size-3" />
          {error}
        </div>
      )}

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
        <div className="space-y-2">
          <DeviceSelect
            icon={MicIcon}
            label="Input"
            devices={audio.input_devices}
            selected={selectedInput}
            onChange={handleInputChange}
            warn={!audio.available}
          />
          {audio.available && audio.input_devices.length > 0 && (
            <MicTestPanel deviceId={selectedInput} />
          )}
        </div>
        <div className="space-y-2">
          <DeviceSelect
            icon={Volume2Icon}
            label="Output"
            devices={audio.output_devices}
            selected={selectedOutput}
            onChange={handleOutputChange}
            warn={!audio.available}
          />
          {audio.available && audio.output_devices.length > 0 && (
            <VoiceTestPicker
              deviceId={selectedOutput}
              catalog={catalog}
              selectedLanguage={selectedLanguage}
              selectedVoice={selectedVoice}
              onLanguageChange={handleLanguageChange}
              onVoiceChange={handleVoiceChange}
            />
          )}
        </div>
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

      {/* Recommended models — real disk presence */}
      <ModelsDiskStatusPanel fallbackTotalMb={total_download_mb} fallback={recommended_models} />
    </div>
  );
}

/**
 * ModelsDiskStatusPanel — renders real on-disk model presence.
 *
 * Falls back to the static recommended list if the status endpoint
 * fails, so the wizard still renders something useful on an offline
 * dashboard. The green check is gated on ``installed === true``; a
 * missing model renders a cloud icon and is enrolled into the
 * "Download missing models" CTA at the bottom of the card.
 */
function ModelsDiskStatusPanel({
  fallbackTotalMb,
  fallback,
}: {
  fallbackTotalMb: number;
  fallback: HardwareInfo["recommended_models"];
}) {
  const { status, statusLoading, statusError, download, downloading, startDownload } =
    useVoiceModels();

  if (statusLoading && !status) {
    return (
      <div className="flex items-center gap-2 rounded-[var(--svx-radius-md)] border border-[var(--svx-color-border-default)] px-3 py-2.5 text-xs text-[var(--svx-color-text-tertiary)]">
        <LoaderIcon className="size-3.5 animate-spin" />
        <span>Checking installed models…</span>
      </div>
    );
  }

  if (statusError || !status) {
    // Fallback: render the static tier list so the wizard isn't blank.
    return (
      <div className="space-y-2">
        <h4 className="text-xs font-medium text-[var(--svx-color-text-secondary)]">
          Recommended models ({fallbackTotalMb} MB total)
        </h4>
        <div className="space-y-1.5">
          {fallback.map((m) => (
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
              <span className="ml-3 text-[11px] text-[var(--svx-color-text-tertiary)]">
                {m.size_mb} MB
              </span>
            </div>
          ))}
        </div>
      </div>
    );
  }

  const progressPct =
    download && download.total_models > 0
      ? Math.min(100, Math.round((download.completed_models / download.total_models) * 100))
      : 0;

  return (
    <div className="space-y-2">
      <div className="flex items-center justify-between">
        <h4 className="text-xs font-medium text-[var(--svx-color-text-secondary)]">
          Voice models
        </h4>
        {status.all_installed ? (
          <span className="flex items-center gap-1 text-[10px] text-[var(--svx-color-success)]">
            <CheckCircle2Icon className="size-3" /> All installed
          </span>
        ) : (
          <span className="text-[10px] text-[var(--svx-color-warning)]">
            {status.missing_count} missing · {status.missing_download_mb} MB
          </span>
        )}
      </div>

      <div className="space-y-1.5">
        {status.models.map((m) => (
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
                {m.installed ? `${m.size_mb} MB` : `${m.expected_size_mb} MB`}
              </span>
              {m.installed ? (
                <CheckCircle2Icon
                  aria-label="installed"
                  className="size-3.5 text-[var(--svx-color-success)]"
                />
              ) : m.download_available ? (
                <CloudDownloadIcon
                  aria-label="available to download"
                  className="size-3.5 text-[var(--svx-color-text-tertiary)]"
                />
              ) : (
                <span className="text-[10px] text-[var(--svx-color-warning)]">manual</span>
              )}
            </div>
          </div>
        ))}
      </div>

      {!status.all_installed && (
        <div className="space-y-1.5">
          <button
            type="button"
            onClick={startDownload}
            disabled={downloading}
            className={cn(
              "flex w-full items-center justify-center gap-2 rounded-[var(--svx-radius-md)] border px-3 py-2 text-xs font-medium transition-colors",
              downloading
                ? "cursor-not-allowed border-[var(--svx-color-border-default)] bg-[var(--svx-color-bg-elevated)] text-[var(--svx-color-text-tertiary)]"
                : "border-[var(--svx-color-brand-primary)]/40 bg-[var(--svx-color-brand-primary)]/10 text-[var(--svx-color-brand-primary)] hover:bg-[var(--svx-color-brand-primary)]/20",
            )}
          >
            {downloading ? (
              <>
                <LoaderIcon className="size-3.5 animate-spin" />
                <span>
                  Downloading{download?.current_model ? ` ${download.current_model}` : "…"}
                  {" "}({download?.completed_models ?? 0}/{download?.total_models ?? 0})
                </span>
              </>
            ) : (
              <>
                <DownloadIcon className="size-3.5" />
                Download missing models ({status.missing_download_mb} MB)
              </>
            )}
          </button>
          {downloading && (
            <div
              role="progressbar"
              aria-valuenow={progressPct}
              aria-valuemin={0}
              aria-valuemax={100}
              className="h-1 w-full overflow-hidden rounded-full bg-[var(--svx-color-bg-elevated)]"
            >
              <div
                className="h-full bg-[var(--svx-color-brand-primary)] transition-[width]"
                style={{ width: `${progressPct}%` }}
              />
            </div>
          )}
          {download?.status === "error" && (
            <DownloadErrorPanel
              errorCode={download.error_code ?? "unknown"}
              errorMessage={download.error ?? "Download failed"}
              retryAfterSeconds={download.retry_after_seconds ?? null}
              onRetry={startDownload}
            />
          )}
        </div>
      )}
    </div>
  );
}

const _ERROR_CODE_TITLES: Record<string, string> = {
  cooldown: "Download blocked — recent failure cooldown active",
  all_mirrors_exhausted: "All mirror sources are currently unreachable",
  checksum_mismatch: "Download rejected — file integrity check failed",
  network: "Network error during download",
  unknown: "Download failed",
};

const _ERROR_CODE_HINTS: Record<string, string> = {
  cooldown:
    "We wait before retrying to avoid hammering a source that was just failing. You can force-retry below or wait for the countdown.",
  all_mirrors_exhausted:
    "Primary + every fallback mirror failed. Check your internet connection; if persistent, the upstream release may be offline.",
  checksum_mismatch:
    "The mirror served a file whose contents don't match the pinned SHA-256. The mirror is probably drift-ing — we'll try another source on retry.",
  network: "Transient network glitch. Retry in a moment.",
  unknown: "",
};

function _formatCountdown(seconds: number): string {
  if (seconds <= 0) return "now";
  const m = Math.floor(seconds / 60);
  const s = seconds % 60;
  if (m === 0) return `${s}s`;
  return `${m}m ${s.toString().padStart(2, "0")}s`;
}

function DownloadErrorPanel({
  errorCode,
  errorMessage,
  retryAfterSeconds,
  onRetry,
}: {
  errorCode: string;
  errorMessage: string;
  retryAfterSeconds: number | null;
  onRetry: () => void;
}) {
  const [countdown, setCountdown] = useState<number>(retryAfterSeconds ?? 0);

  useEffect(() => {
    setCountdown(retryAfterSeconds ?? 0);
  }, [retryAfterSeconds]);

  useEffect(() => {
    if (countdown <= 0) return;
    const id = window.setInterval(() => {
      setCountdown((prev) => (prev > 0 ? prev - 1 : 0));
    }, 1000);
    return () => window.clearInterval(id);
  }, [countdown]);

  const title = _ERROR_CODE_TITLES[errorCode] ?? _ERROR_CODE_TITLES.unknown;
  const hint = _ERROR_CODE_HINTS[errorCode] ?? "";
  const retryDisabled = errorCode === "cooldown" && countdown > 0;

  return (
    <div
      role="alert"
      className="flex flex-col gap-2 rounded-[var(--svx-radius-md)] bg-[var(--svx-color-error)]/10 px-3 py-2 text-[11px] text-[var(--svx-color-error)]"
    >
      <div className="flex items-start gap-2">
        <AlertTriangleIcon className="mt-0.5 size-3.5 shrink-0" />
        <div className="flex-1">
          <div className="font-medium">{title}</div>
          {hint && (
            <div className="mt-1 text-[10px] text-[var(--svx-color-error)]/80">{hint}</div>
          )}
          <div className="mt-1 font-mono text-[10px] text-[var(--svx-color-error)]/70">
            {errorMessage}
          </div>
        </div>
      </div>
      <div className="flex items-center justify-between gap-2">
        {retryDisabled ? (
          <span className="text-[10px] text-[var(--svx-color-error)]/80">
            Retry available in {_formatCountdown(countdown)}
          </span>
        ) : (
          <span />
        )}
        <button
          type="button"
          onClick={onRetry}
          disabled={retryDisabled}
          className={cn(
            "rounded-[var(--svx-radius-md)] border px-3 py-1 text-[10px] font-medium transition",
            retryDisabled
              ? "cursor-not-allowed border-[var(--svx-color-border-default)] text-[var(--svx-color-text-tertiary)]"
              : "border-[var(--svx-color-error)]/40 bg-[var(--svx-color-error)]/10 hover:bg-[var(--svx-color-error)]/20",
          )}
        >
          Retry download
        </button>
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

/**
 * VoiceTestPicker — language + voice dropdowns + Test-speakers button.
 *
 * Renders disabled placeholders while the catalog fetch is in flight so
 * the layout doesn't jump when the dropdowns appear. ``selectedLanguage``
 * is allowed to be ``null`` between mount and the first catalog success;
 * we forward ``"en"`` to the button in that case so its post body is
 * always schema-valid, and disable the button until the catalog arrives.
 */
const LANGUAGE_LABELS: Record<string, string> = {
  "en-us": "English (US)",
  "en-gb": "English (UK)",
  "pt-br": "Portuguese (BR)",
  es: "Spanish",
  fr: "French",
  hi: "Hindi",
  it: "Italian",
  ja: "Japanese",
  zh: "Chinese",
};

function VoiceTestPicker({
  deviceId,
  catalog,
  selectedLanguage,
  selectedVoice,
  onLanguageChange,
  onVoiceChange,
}: {
  deviceId: number | null;
  catalog: ReturnType<typeof useVoiceCatalog>;
  selectedLanguage: string | null;
  selectedVoice: string | null;
  onLanguageChange: (lang: string) => void;
  onVoiceChange: (voice: string) => void;
}) {
  const ready = catalog.catalog !== null && selectedLanguage !== null;
  const voices = selectedLanguage
    ? catalog.voicesForLanguage(selectedLanguage)
    : [];
  const languages = catalog.catalog?.supported_languages ?? [];

  return (
    <div className="space-y-2">
      <div className="grid grid-cols-2 gap-2">
        <div className="relative">
          <select
            aria-label="Voice-test language"
            value={selectedLanguage ?? ""}
            onChange={(e) => onLanguageChange(e.target.value)}
            disabled={!ready}
            className={cn(
              "w-full cursor-pointer rounded-[var(--svx-radius-md)] border border-[var(--svx-color-border-default)] bg-[var(--svx-color-bg-elevated)] px-2.5 py-1.5 pr-7 text-[11px] text-[var(--svx-color-text-primary)] outline-none",
              !ready && "cursor-not-allowed opacity-60",
            )}
            style={{ colorScheme: "dark" }}
          >
            {!ready && <option value="">Loading…</option>}
            {languages.map((code) => (
              <option key={code} value={code}>
                {LANGUAGE_LABELS[code] ?? code}
              </option>
            ))}
          </select>
          <ChevronDownIcon className="pointer-events-none absolute right-2 top-1/2 size-3 -translate-y-1/2 text-[var(--svx-color-text-tertiary)]" />
        </div>
        <div className="relative">
          <select
            aria-label="Voice"
            value={selectedVoice ?? ""}
            onChange={(e) => onVoiceChange(e.target.value)}
            disabled={!ready || voices.length === 0}
            className={cn(
              "w-full cursor-pointer rounded-[var(--svx-radius-md)] border border-[var(--svx-color-border-default)] bg-[var(--svx-color-bg-elevated)] px-2.5 py-1.5 pr-7 text-[11px] text-[var(--svx-color-text-primary)] outline-none",
              (!ready || voices.length === 0) && "cursor-not-allowed opacity-60",
            )}
            style={{ colorScheme: "dark" }}
          >
            {!ready && <option value="">Loading…</option>}
            {voices.map((v) => (
              <option key={v.id} value={v.id}>
                {v.display_name}
              </option>
            ))}
          </select>
          <ChevronDownIcon className="pointer-events-none absolute right-2 top-1/2 size-3 -translate-y-1/2 text-[var(--svx-color-text-tertiary)]" />
        </div>
      </div>
      {catalog.error && (
        <p className="text-[10px] text-[var(--svx-color-error)]">
          <AlertTriangleIcon className="mr-1 inline size-3" />
          {catalog.error}
        </p>
      )}
      <TtsTestButton
        deviceId={deviceId}
        language={selectedLanguage ?? "en"}
        voice={selectedVoice}
        disabled={!ready}
      />
    </div>
  );
}

/**
 * MicTestPanel — opt-in live mic meter.
 *
 * The WebSocket is only opened when the user clicks "Test microphone",
 * so we don't stream audio on every wizard mount. Stops cleanly on
 * unmount via the hook's own teardown.
 */
function MicTestPanel({ deviceId }: { deviceId: number | null }) {
  const [enabled, setEnabled] = useState(false);
  const stream = useAudioLevelStream({ deviceId, enabled });

  const statusText =
    stream.state === "error"
      ? stream.errorDetail ?? stream.errorCode ?? "Mic test failed"
      : stream.state === "connecting"
      ? "Connecting…"
      : stream.state === "ready"
      ? "Ready — speak to see your levels"
      : stream.state === "streaming"
      ? null
      : null;

  return (
    <div className="space-y-1.5">
      {!enabled ? (
        <button
          type="button"
          onClick={() => setEnabled(true)}
          className="flex w-full items-center justify-center gap-1.5 rounded-[var(--svx-radius-md)] border border-[var(--svx-color-border-default)] bg-[var(--svx-color-bg-elevated)] px-3 py-1.5 text-[11px] text-[var(--svx-color-text-secondary)] transition-colors hover:text-[var(--svx-color-text-primary)]"
        >
          <ActivityIcon className="size-3" />
          Test microphone
        </button>
      ) : (
        <>
          <AudioLevelMeter level={stream.level} height={28} />
          {statusText && (
            <p
              className={cn(
                "text-[10px]",
                stream.state === "error"
                  ? "text-[var(--svx-color-error)]"
                  : "text-[var(--svx-color-text-tertiary)]",
              )}
            >
              {statusText}
            </p>
          )}
          <button
            type="button"
            onClick={() => setEnabled(false)}
            className="text-[10px] text-[var(--svx-color-text-tertiary)] hover:text-[var(--svx-color-text-primary)]"
          >
            Stop test
          </button>
        </>
      )}
    </div>
  );
}

export const HardwareDetection = memo(HardwareDetectionImpl);
