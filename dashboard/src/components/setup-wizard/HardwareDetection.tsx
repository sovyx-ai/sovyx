/**
 * HardwareDetection -- shows detected hardware + recommended models.
 *
 * Fetches from GET /api/voice/hardware-detect and displays:
 * - CPU cores, RAM, GPU, tier
 * - Audio device dropdowns for input/output selection
 * - Recommended models with download sizes
 */

import { memo, useCallback, useEffect, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
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
  /**
   * v0.31.6 M2: the human-readable name of the selected input device
   * (e.g. ``"Razer Seiren Mini"``). Optional for back-compat with
   * callers that ignore it; HardwareDetection always emits it when a
   * default / user-picked device exists, so the parent (VoiceStep)
   * can persist ``voice_input_device_name`` to mind.yaml on
   * ``/api/voice/enable`` — without it ``_active_mic.resolve_active_mic_card``
   * returns None on the next calibration run.
   */
  input_device_name?: string | null;
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
  const { t } = useTranslation("voice");
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

  // v0.31.6 M2: cache the detected input device list so handleInputChange
  // can resolve a name from the freshly-picked index without round-tripping
  // through state (the index→name lookup must be synchronous).
  const inputDevicesRef = useRef<AudioDevice[]>([]);

  useEffect(() => {
    api
      .get<HardwareInfo>("/api/voice/hardware-detect")
      .then((data) => {
        setInfo(data);
        setError(null);
        inputDevicesRef.current = data.audio.input_devices;
        const defIn = findDefault(data.audio.input_devices);
        const defOut = findDefault(data.audio.output_devices);
        setSelectedInput(defIn);
        setSelectedOutput(defOut);
        const defInName =
          data.audio.input_devices.find((d) => d.index === defIn)?.name ?? null;
        onDetectedRef.current?.(data);
        onDeviceChangeRef.current?.({
          input_device: defIn,
          output_device: defOut,
          input_device_name: defInName,
        });
      })
      .catch((err) => {
        setError(String(err));
      })
      .finally(() => setLoading(false));
  }, []);

  const handleInputChange = useCallback(
    (index: number) => {
      setSelectedInput(index);
      const name =
        inputDevicesRef.current.find((d) => d.index === index)?.name ?? null;
      onDeviceChange?.({
        input_device: index,
        output_device: selectedOutput,
        input_device_name: name,
      });
    },
    [selectedOutput, onDeviceChange],
  );

  const handleOutputChange = useCallback(
    (index: number) => {
      setSelectedOutput(index);
      const name =
        inputDevicesRef.current.find((d) => d.index === selectedInput)?.name ??
        null;
      onDeviceChange?.({
        input_device: selectedInput,
        output_device: index,
        input_device_name: name,
      });
    },
    [selectedInput, onDeviceChange],
  );

  if (loading) {
    return (
      <div className="flex items-center justify-center py-6 text-sm text-[var(--svx-color-text-tertiary)]">
        <LoaderIcon className="mr-2 size-4 animate-spin" />
        {t("hardwareDetection.detecting")}
      </div>
    );
  }

  if (!info) {
    return (
      <div className="rounded-[var(--svx-radius-md)] bg-[var(--svx-color-error)]/10 px-4 py-3 text-xs text-[var(--svx-color-error)]">
        <AlertTriangleIcon className="mr-1.5 inline size-3.5" />
        {error ?? t("hardwareDetection.detectionFailed")}
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
        <InfoChip
          icon={CpuIcon}
          label={t("hardwareDetection.infoChip.cpu")}
          value={t("hardwareDetection.infoChip.cpuValue", {
            cores: hardware.cpu_cores,
          })}
        />
        <InfoChip
          icon={HardDriveIcon}
          label={t("hardwareDetection.infoChip.ram")}
          value={t("hardwareDetection.infoChip.ramValue", {
            gb: Math.round(hardware.ram_mb / 1024),
          })}
        />
      </div>

      {/* Audio device selectors */}
      <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
        <div className="space-y-2">
          <DeviceSelect
            icon={MicIcon}
            label={t("hardwareDetection.deviceSelect.input")}
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
            label={t("hardwareDetection.deviceSelect.output")}
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

      {/* Tier badge — ``hardware.tier`` is a stable backend identifier
          (e.g. "RPI4", "DESKTOP_GPU", "WORKSTATION") rendered verbatim;
          tier names are NOT translated to keep the operator's mental
          model aligned with the docs that reference the same constant. */}
      <div className="flex items-center gap-2">
        <span className="rounded-full bg-[var(--svx-color-brand-primary)]/10 px-3 py-1 text-xs font-medium text-[var(--svx-color-brand-primary)]">
          {hardware.tier}
        </span>
        {hardware.has_gpu && (
          <span className="rounded-full bg-[var(--svx-color-success)]/10 px-3 py-1 text-xs font-medium text-[var(--svx-color-success)]">
            {t("hardwareDetection.tier.gpuLabel", {
              vram: hardware.gpu_vram_mb,
            })}
          </span>
        )}
      </div>

      {/* Audio warning */}
      {!audio.available && (
        <div className="flex items-center gap-2 rounded-[var(--svx-radius-md)] bg-[var(--svx-color-warning)]/10 px-3 py-2.5 text-xs text-[var(--svx-color-warning)]">
          <AlertTriangleIcon className="size-3.5 shrink-0" />
          <span>{t("hardwareDetection.audioWarning")}</span>
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
  const { t } = useTranslation("voice");
  const { status, statusLoading, statusError, download, downloading, startDownload } =
    useVoiceModels();

  if (statusLoading && !status) {
    return (
      <div className="flex items-center gap-2 rounded-[var(--svx-radius-md)] border border-[var(--svx-color-border-default)] px-3 py-2.5 text-xs text-[var(--svx-color-text-tertiary)]">
        <LoaderIcon className="size-3.5 animate-spin" />
        <span>{t("hardwareDetection.models.checking")}</span>
      </div>
    );
  }

  if (statusError || !status) {
    // Fallback: render the static tier list so the wizard isn't blank.
    return (
      <div className="space-y-2">
        <h4 className="text-xs font-medium text-[var(--svx-color-text-secondary)]">
          {t("hardwareDetection.models.fallbackTitle", {
            totalMb: fallbackTotalMb,
          })}
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
          {t("hardwareDetection.models.title")}
        </h4>
        {status.all_installed ? (
          <span className="flex items-center gap-1 text-[10px] text-[var(--svx-color-success)]">
            <CheckCircle2Icon className="size-3" />{" "}
            {t("hardwareDetection.models.allInstalledBadge")}
          </span>
        ) : (
          <span className="text-[10px] text-[var(--svx-color-warning)]">
            {t("hardwareDetection.models.missingBadge", {
              count: status.missing_count,
              mb: status.missing_download_mb,
            })}
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
                  aria-label={t("hardwareDetection.models.installedAria")}
                  className="size-3.5 text-[var(--svx-color-success)]"
                />
              ) : m.download_available ? (
                <CloudDownloadIcon
                  aria-label={t("hardwareDetection.models.availableAria")}
                  className="size-3.5 text-[var(--svx-color-text-tertiary)]"
                />
              ) : (
                <span className="text-[10px] text-[var(--svx-color-warning)]">
                  {t("hardwareDetection.models.manualBadge")}
                </span>
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
                  {t("hardwareDetection.models.downloadingState", {
                    currentSuffix: download?.current_model
                      ? t("hardwareDetection.models.currentSuffix", {
                          model: download.current_model,
                        })
                      : "…",
                    done: download?.completed_models ?? 0,
                    total: download?.total_models ?? 0,
                  })}
                </span>
              </>
            ) : (
              <>
                <DownloadIcon className="size-3.5" />
                {t("hardwareDetection.models.downloadCta", {
                  mb: status.missing_download_mb,
                })}
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
              errorMessage={
                download.error ??
                t("hardwareDetection.models.fallbackErrorMessage")
              }
              retryAfterSeconds={download.retry_after_seconds ?? null}
              onRetry={startDownload}
            />
          )}
        </div>
      )}
    </div>
  );
}

// v0.32.5 Phase 4.B.2 — error code → i18n key maps. Backend sends a
// stable code identifier (cooldown / all_mirrors_exhausted /
// checksum_mismatch / network / unknown); the UI resolves to the
// per-locale title + hint at render time. Codes themselves are NEVER
// translated (they're contract identifiers); only operator-facing
// titles + hints migrate.
const _ERROR_CODE_TITLE_KEYS: Record<string, string> = {
  cooldown: "hardwareDetection.downloadError.title.cooldown",
  all_mirrors_exhausted:
    "hardwareDetection.downloadError.title.allMirrorsExhausted",
  checksum_mismatch:
    "hardwareDetection.downloadError.title.checksumMismatch",
  network: "hardwareDetection.downloadError.title.network",
  unknown: "hardwareDetection.downloadError.title.unknown",
};

const _ERROR_CODE_HINT_KEYS: Record<string, string> = {
  cooldown: "hardwareDetection.downloadError.hint.cooldown",
  all_mirrors_exhausted:
    "hardwareDetection.downloadError.hint.allMirrorsExhausted",
  checksum_mismatch:
    "hardwareDetection.downloadError.hint.checksumMismatch",
  network: "hardwareDetection.downloadError.hint.network",
  unknown: "hardwareDetection.downloadError.hint.unknown",
};

function _formatCountdown(
  seconds: number,
  t: (key: string, opts?: Record<string, unknown>) => string,
): string {
  if (seconds <= 0) return t("hardwareDetection.downloadError.countdownNow");
  const m = Math.floor(seconds / 60);
  const s = seconds % 60;
  if (m === 0)
    return t("hardwareDetection.downloadError.countdownSecondsOnly", { s });
  return t("hardwareDetection.downloadError.countdownMinutesAndSeconds", {
    m,
    s: s.toString().padStart(2, "0"),
  });
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
  const { t } = useTranslation("voice");
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

  // Type-safe key resolution: ``Record<string, string>`` index access
  // returns ``string | undefined`` under strict TS, so the fallback to
  // ``_ERROR_CODE_*_KEYS.unknown`` doesn't narrow on its own. Pin the
  // ``unknown`` literal as the ultimate fallback so the type narrows
  // back to ``string`` for the t() call.
  const titleKey: string =
    _ERROR_CODE_TITLE_KEYS[errorCode] ??
    "hardwareDetection.downloadError.title.unknown";
  const hintKey: string =
    _ERROR_CODE_HINT_KEYS[errorCode] ??
    "hardwareDetection.downloadError.hint.unknown";
  const title = t(titleKey);
  // The "unknown" hint key resolves to an empty string per the
  // localisation map; ``t()`` returns "" so we suppress the rendered
  // <div> wrapper via the truthy check below.
  const hint = t(hintKey);
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
            {t("hardwareDetection.downloadError.retryCountdown", {
              countdown: _formatCountdown(countdown, t),
            })}
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
          {t("hardwareDetection.downloadError.retryButton")}
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
  const { t } = useTranslation("voice");
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
          <div className="text-xs font-medium text-[var(--svx-color-warning)]">
            {t("hardwareDetection.deviceSelect.none")}
          </div>
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
  const { t } = useTranslation("voice");
  const ready = catalog.catalog !== null && selectedLanguage !== null;
  const voices = selectedLanguage
    ? catalog.voicesForLanguage(selectedLanguage)
    : [];
  const languages = catalog.catalog?.supported_languages ?? [];

  // v0.32.5 Phase 4.B.2 — language label resolver. The locale file
  // defines names for each known BCP-47 code under
  // ``hardwareDetection.languageLabels.*``. Unknown codes fall through
  // to the raw code (matches the legacy behaviour) so a future Moonshine
  // addition surfaces visibly until the translation lands.
  const labelForLanguage = (code: string): string => {
    const key = `hardwareDetection.languageLabels.${code}`;
    const label = t(key);
    return label === key ? code : label;
  };

  return (
    <div className="space-y-2">
      <div className="grid grid-cols-2 gap-2">
        <div className="relative">
          <select
            aria-label={t("hardwareDetection.voicePicker.languageAria")}
            value={selectedLanguage ?? ""}
            onChange={(e) => onLanguageChange(e.target.value)}
            disabled={!ready}
            className={cn(
              "w-full cursor-pointer rounded-[var(--svx-radius-md)] border border-[var(--svx-color-border-default)] bg-[var(--svx-color-bg-elevated)] px-2.5 py-1.5 pr-7 text-[11px] text-[var(--svx-color-text-primary)] outline-none",
              !ready && "cursor-not-allowed opacity-60",
            )}
            style={{ colorScheme: "dark" }}
          >
            {!ready && (
              <option value="">
                {t("hardwareDetection.voicePicker.loadingOption")}
              </option>
            )}
            {languages.map((code) => (
              <option key={code} value={code}>
                {labelForLanguage(code)}
              </option>
            ))}
          </select>
          <ChevronDownIcon className="pointer-events-none absolute right-2 top-1/2 size-3 -translate-y-1/2 text-[var(--svx-color-text-tertiary)]" />
        </div>
        <div className="relative">
          <select
            aria-label={t("hardwareDetection.voicePicker.voiceAria")}
            value={selectedVoice ?? ""}
            onChange={(e) => onVoiceChange(e.target.value)}
            disabled={!ready || voices.length === 0}
            className={cn(
              "w-full cursor-pointer rounded-[var(--svx-radius-md)] border border-[var(--svx-color-border-default)] bg-[var(--svx-color-bg-elevated)] px-2.5 py-1.5 pr-7 text-[11px] text-[var(--svx-color-text-primary)] outline-none",
              (!ready || voices.length === 0) && "cursor-not-allowed opacity-60",
            )}
            style={{ colorScheme: "dark" }}
          >
            {!ready && (
              <option value="">
                {t("hardwareDetection.voicePicker.loadingOption")}
              </option>
            )}
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
  const { t } = useTranslation("voice");
  const [enabled, setEnabled] = useState(false);
  const stream = useAudioLevelStream({ deviceId, enabled });

  // Backend-supplied ``errorDetail`` / ``errorCode`` are passed through
  // unchanged when present (operator's own audio stack often supplies
  // a more device-specific message than any localised fallback).
  const statusText =
    stream.state === "error"
      ? stream.errorDetail ??
        stream.errorCode ??
        t("hardwareDetection.micTest.fallbackError")
      : stream.state === "connecting"
      ? t("hardwareDetection.micTest.connecting")
      : stream.state === "ready"
      ? t("hardwareDetection.micTest.ready")
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
          {t("hardwareDetection.micTest.startButton")}
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
            {t("hardwareDetection.micTest.stopButton")}
          </button>
        </>
      )}
    </div>
  );
}

export const HardwareDetection = memo(HardwareDetectionImpl);
