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
from sovyx.voice.health._power import NoopPowerEventListener, PowerEventListener
from sovyx.voice.health._self_feedback import SelfFeedbackGate, SelfFeedbackMode
from sovyx.voice.health.capture_overrides import CaptureOverrides
from sovyx.voice.health.cascade import (
    LINUX_CASCADE,
    MACOS_CASCADE,
    WINDOWS_CASCADE,
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
    CascadeResult,
    Combo,
    ComboEntry,
    ComboStoreStats,
    Diagnosis,
    HotplugEvent,
    HotplugEventKind,
    LoadReport,
    OverrideEntry,
    PowerEvent,
    PowerEventKind,
    ProbeHistoryEntry,
    ProbeMode,
    ProbeResult,
    RemediationHint,
    WatchdogState,
)
from sovyx.voice.health.preflight import (
    PreflightCheck,
    PreflightReport,
    PreflightStep,
    PreflightStepCode,
    PreflightStepSpec,
    check_portaudio,
    check_tts_synthesize,
    check_wake_word_smoke,
    current_platform_key,
    default_step_names,
    run_preflight,
)
from sovyx.voice.health.probe import probe
from sovyx.voice.health.watchdog import (
    VoiceCaptureWatchdog,
    build_platform_audio_service_monitor,
    build_platform_default_device_watcher,
    build_platform_hotplug_listener,
    build_platform_power_listener,
)

__all__ = [
    "ALLOWED_FORMATS",
    "ALLOWED_HOST_APIS_BY_PLATFORM",
    "ALLOWED_SAMPLE_RATES",
    "AudioServiceEvent",
    "AudioServiceEventKind",
    "AudioServiceMonitor",
    "AudioSubsystemFingerprint",
    "CaptureOverrides",
    "CascadeResult",
    "Combo",
    "ComboEntry",
    "ComboStore",
    "ComboStoreStats",
    "DefaultDeviceWatcher",
    "Diagnosis",
    "HotplugEvent",
    "HotplugEventKind",
    "HotplugListener",
    "LINUX_CASCADE",
    "LoadReport",
    "MACOS_CASCADE",
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
    "ProbeHistoryEntry",
    "ProbeMode",
    "ProbeResult",
    "RemediationHint",
    "SelfFeedbackGate",
    "SelfFeedbackMode",
    "VoiceCaptureWatchdog",
    "WINDOWS_CASCADE",
    "WatchdogState",
    "build_platform_audio_service_monitor",
    "build_platform_default_device_watcher",
    "build_platform_hotplug_listener",
    "build_platform_power_listener",
    "check_portaudio",
    "check_tts_synthesize",
    "check_wake_word_smoke",
    "current_platform_key",
    "default_step_names",
    "probe",
    "run_cascade",
    "run_preflight",
]
