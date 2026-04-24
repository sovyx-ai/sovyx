"""Voice Capture Health Lifecycle (VCHL) — see ADR-voice-capture-health-lifecycle.md.

This subpackage owns end-to-end resilience for microphone capture across
Windows / Linux / macOS:

* L0 :mod:`~sovyx.voice.health.contract` — the dataclass + enum vocabulary
  every other layer speaks (``Diagnosis``, ``ProbeMode``, ``Combo``,
  ``ProbeResult``, ``RemediationHint``, ``ComboEntry``, ``OverrideEntry``).
* L1 :mod:`~sovyx.voice.health.combo_store` — persistent JSON memoization
  of (endpoint × winning_combo) tuples with 13 invalidation rules and
  atomic + locked writes.
* L1 :mod:`~sovyx.voice.health.capture_overrides` — sibling file for
  user-pinned combos that survive ``--reset``.
* L2 :mod:`~sovyx.voice.health.cascade` — cascading open strategies per
  platform, gated by the lifecycle lock and a wall-clock budget.
* L3 :mod:`~sovyx.voice.health.probe` — the single probe entry point
  with cold / warm modes.
* L4 :mod:`~sovyx.voice.health.watchdog` — Sprint 2 runtime resilience:
  exponential-backoff warm re-probes on sustained deafness plus the
  platform-agnostic hot-plug reaction surface. Swaps in Windows
  ``WM_DEVICECHANGE`` / Linux ``udev`` / macOS CoreAudio listeners
  via :func:`~sovyx.voice.health.watchdog.build_platform_hotplug_listener`.

Internal helpers (underscore-prefixed) are not part of the public surface
and may move between releases.
"""

from __future__ import annotations

from sovyx.voice.health._audio_service import (
    AudioServiceMonitor,
    NoopAudioServiceMonitor,
)
from sovyx.voice.health._default_device import (
    DefaultDeviceWatcher,
    NoopDefaultDeviceWatcher,
    PollingDefaultDeviceWatcher,
)
from sovyx.voice.health._hotplug import HotplugListener, NoopHotplugListener
from sovyx.voice.health._kernel_invalidated_recheck import (
    KernelInvalidatedRechecker,
    RecheckProbeCallable,
)
from sovyx.voice.health._linux_mixer_check import check_linux_mixer_sanity
from sovyx.voice.health._mixer_kb import MixerKBLookup, MixerKBMatch
from sovyx.voice.health._mixer_roles import MixerControlRoleResolver
from sovyx.voice.health._mixer_sanity import (
    check_and_maybe_heal,
    default_persist_via_alsactl,
    detect_user_customization,
)
from sovyx.voice.health._power import NoopPowerEventListener, PowerEventListener
from sovyx.voice.health._quarantine import (
    EndpointQuarantine,
    QuarantineEntry,
    get_default_quarantine,
    reset_default_quarantine,
)
from sovyx.voice.health._self_feedback import SelfFeedbackGate, SelfFeedbackMode
from sovyx.voice.health.capture_overrides import CaptureOverrides
from sovyx.voice.health.cascade import (
    LINUX_CASCADE,
    MACOS_CASCADE,
    WINDOWS_CASCADE,
    WINDOWS_CASCADE_AGGRESSIVE,
    ProbeCallable,
    run_cascade,
)
from sovyx.voice.health.combo_store import ComboStore
from sovyx.voice.health.contract import (
    ALLOWED_FORMATS,
    ALLOWED_HOST_APIS_BY_PLATFORM,
    ALLOWED_SAMPLE_RATES,
    AudioServiceEvent,
    AudioServiceEventKind,
    AudioSubsystemFingerprint,
    BypassContext,
    BypassOutcome,
    BypassVerdict,
    CaptureTaskProto,
    CascadeResult,
    Combo,
    ComboEntry,
    ComboStoreStats,
    Diagnosis,
    Eligibility,
    FactorySignature,
    HardwareContext,
    HotplugEvent,
    HotplugEventKind,
    IntegrityResult,
    IntegrityVerdict,
    LoadReport,
    MixerApplySnapshot,
    MixerCardSnapshot,
    MixerControlRole,
    MixerControlSnapshot,
    MixerKBProfile,
    MixerPresetControl,
    MixerPresetSpec,
    MixerPresetValue,
    MixerPresetValueDb,
    MixerPresetValueFraction,
    MixerPresetValueRaw,
    MixerSanityDecision,
    MixerSanityResult,
    MixerValidationMetrics,
    OverrideEntry,
    PowerEvent,
    PowerEventKind,
    ProbeHistoryEntry,
    ProbeMode,
    ProbeResult,
    RemediationHint,
    ValidationGates,
    VerificationRecord,
    WatchdogState,
)
from sovyx.voice.health.preflight import (
    BootPreflightWarningsStore,
    PreflightCheck,
    PreflightReport,
    PreflightStep,
    PreflightStepCode,
    PreflightStepSpec,
    check_portaudio,
    check_tts_synthesize,
    check_wake_word_smoke,
    clear_preflight_warnings_file,
    current_platform_key,
    default_step_names,
    preflight_warnings_file_path,
    read_preflight_warnings_file,
    run_preflight,
    write_preflight_warnings_file,
)
from sovyx.voice.health.probe import probe
from sovyx.voice.health.watchdog import (
    VoiceCaptureWatchdog,
    build_platform_audio_service_monitor,
    build_platform_default_device_watcher,
    build_platform_hotplug_listener,
    build_platform_power_listener,
)
from sovyx.voice.health.wizard import (
    CascadeFn,
    ProbeFn,
    VoiceSetupWizard,
    WizardOutcome,
    WizardReport,
)

__all__ = [
    "ALLOWED_FORMATS",
    "ALLOWED_HOST_APIS_BY_PLATFORM",
    "ALLOWED_SAMPLE_RATES",
    "AudioServiceEvent",
    "AudioServiceEventKind",
    "AudioServiceMonitor",
    "AudioSubsystemFingerprint",
    "BootPreflightWarningsStore",
    "BypassContext",
    "BypassOutcome",
    "BypassVerdict",
    "CaptureOverrides",
    "CaptureTaskProto",
    "CascadeFn",
    "CascadeResult",
    "Combo",
    "ComboEntry",
    "ComboStore",
    "ComboStoreStats",
    "DefaultDeviceWatcher",
    "Diagnosis",
    "Eligibility",
    "EndpointQuarantine",
    "FactorySignature",
    "HardwareContext",
    "HotplugEvent",
    "HotplugEventKind",
    "HotplugListener",
    "IntegrityResult",
    "IntegrityVerdict",
    "KernelInvalidatedRechecker",
    "LINUX_CASCADE",
    "LoadReport",
    "MACOS_CASCADE",
    "MixerApplySnapshot",
    "MixerCardSnapshot",
    "MixerControlRole",
    "MixerControlRoleResolver",
    "MixerControlSnapshot",
    "MixerKBLookup",
    "MixerKBMatch",
    "MixerKBProfile",
    "MixerPresetControl",
    "MixerPresetSpec",
    "MixerPresetValue",
    "MixerPresetValueDb",
    "MixerPresetValueFraction",
    "MixerPresetValueRaw",
    "MixerSanityDecision",
    "MixerSanityResult",
    "MixerValidationMetrics",
    "NoopAudioServiceMonitor",
    "NoopDefaultDeviceWatcher",
    "NoopHotplugListener",
    "NoopPowerEventListener",
    "OverrideEntry",
    "PollingDefaultDeviceWatcher",
    "PowerEvent",
    "PowerEventKind",
    "PowerEventListener",
    "PreflightCheck",
    "PreflightReport",
    "PreflightStep",
    "PreflightStepCode",
    "PreflightStepSpec",
    "ProbeCallable",
    "ProbeFn",
    "ProbeHistoryEntry",
    "ProbeMode",
    "ProbeResult",
    "QuarantineEntry",
    "RecheckProbeCallable",
    "RemediationHint",
    "SelfFeedbackGate",
    "SelfFeedbackMode",
    "ValidationGates",
    "VerificationRecord",
    "VoiceCaptureWatchdog",
    "VoiceSetupWizard",
    "WINDOWS_CASCADE",
    "WINDOWS_CASCADE_AGGRESSIVE",
    "WatchdogState",
    "WizardOutcome",
    "WizardReport",
    "build_platform_audio_service_monitor",
    "build_platform_default_device_watcher",
    "build_platform_hotplug_listener",
    "build_platform_power_listener",
    "check_and_maybe_heal",
    "check_linux_mixer_sanity",
    "check_portaudio",
    "check_tts_synthesize",
    "check_wake_word_smoke",
    "clear_preflight_warnings_file",
    "current_platform_key",
    "default_persist_via_alsactl",
    "default_step_names",
    "detect_user_customization",
    "get_default_quarantine",
    "preflight_warnings_file_path",
    "probe",
    "read_preflight_warnings_file",
    "reset_default_quarantine",
    "run_cascade",
    "run_preflight",
    "write_preflight_warnings_file",
]
