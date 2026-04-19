"""L0 — the contract every VCHL layer speaks.

This module defines the vocabulary used across the Voice Capture Health
Lifecycle: enums for diagnoses + probe modes, dataclasses for combos +
probe results + persisted entries + cascade outcomes, plus the small set
of platform-aware sanity validators that gate object creation.

The contract is intentionally validation-strict at construction time so a
malformed :class:`Combo` (e.g. 192 channels, sample rate 12345) cannot
propagate beyond the boundary that built it. Persisted JSON entries
re-validate on load via :class:`~sovyx.voice.health.combo_store.ComboStore`
so a corrupted on-disk file cannot poison the runtime either.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Mapping
    from pathlib import Path


# ── Enums ────────────────────────────────────────────────────────────────


class Diagnosis(StrEnum):
    """Outcome of a single probe.

    Order matches the descending-confidence triage in
    :func:`~sovyx.voice.health.probe.probe`. The cascade treats only
    :attr:`HEALTHY` as a winning combo; every other value triggers
    fallthrough or remediation.
    """

    HEALTHY = "healthy"
    MUTED = "muted"
    NO_SIGNAL = "no_signal"
    LOW_SIGNAL = "low_signal"
    FORMAT_MISMATCH = "format_mismatch"
    APO_DEGRADED = "apo_degraded"
    VAD_INSENSITIVE = "vad_insensitive"
    DRIVER_ERROR = "driver_error"
    DEVICE_BUSY = "device_busy"
    HOT_UNPLUGGED = "hot_unplugged"
    SELF_FEEDBACK = "self_feedback"
    PERMISSION_DENIED = "permission_denied"
    UNKNOWN = "unknown"


class ProbeMode(StrEnum):
    """How a probe should validate the device.

    * :attr:`COLD` — boot-time, no user assumed to be speaking. Validates
      that the stream opens and PortAudio callbacks fire. Cannot detect
      :attr:`Diagnosis.APO_DEGRADED` because a silent room produces the
      same RMS as a destroyed signal.
    * :attr:`WARM` — wizard / first-interaction / fix mode, user is
      asked to speak. Runs the audio through SileroVAD to derive the
      full diagnosis surface.
    """

    COLD = "cold"
    WARM = "warm"


class HotplugEventKind(StrEnum):
    """Classification of an OS-level audio-device hot-plug notification.

    The watchdog (:mod:`~sovyx.voice.health.watchdog`) translates every
    platform-specific event (``WM_DEVICECHANGE`` on Windows,
    ``udev`` on Linux, ``kAudioHardwarePropertyDevices`` on macOS) into
    one of these kinds so downstream logic stays platform-agnostic.

    :attr:`DEFAULT_DEVICE_CHANGED` is emitted by Sprint 2 Task #18 — the
    constant lives here so the enum is complete from the start and the
    watchdog's ``on_hotplug`` handler doesn't need a value-update when
    the default-change listener lands.
    """

    DEVICE_ADDED = "device_added"
    DEVICE_REMOVED = "device_removed"
    DEFAULT_DEVICE_CHANGED = "default_device_changed"


class PowerEventKind(StrEnum):
    """OS-level power-management events observed by the watchdog.

    ADR §4.4.4. Sleep invalidates PortAudio sessions and may re-enumerate
    devices in a different order on resume; the pipeline must checkpoint
    on suspend and re-cascade on resume.

    Platform mapping:

    * Windows — ``WM_POWERBROADCAST``: ``PBT_APMSUSPEND`` →
      :attr:`SUSPEND`, ``PBT_APMRESUMEAUTOMATIC`` → :attr:`RESUME`.
    * Linux — ``org.freedesktop.login1`` D-Bus ``PrepareForSleep``
      (``True`` → suspend, ``False`` → resume). Landed in Sprint 4.
    * macOS — ``IORegisterForSystemPower`` callbacks. Landed in Sprint 4.
    """

    SUSPEND = "suspend"
    RESUME = "resume"


class AudioServiceEventKind(StrEnum):
    """Audio-subsystem service lifecycle events observed by the watchdog.

    ADR §4.4.5. Windows ``audiosrv`` can die (driver crash, Windows
    Update, user-initiated ``net stop``); when it restarts, PortAudio
    streams that were open beforehand are permanently broken. The
    watchdog reacts by stalling probes until :attr:`UP`, then triggers a
    re-cascade.

    Platform mapping:

    * Windows — poll ``Get-Service audiosrv`` equivalent via ``sc query``
      or ``pywin32``. :attr:`DOWN` is surfaced on transition Running→Stopped
      (or on repeated PaHostError patterns that imply the service died).
      :attr:`UP` fires once the service is Running again.
    * Linux — Sprint 4 (``systemctl is-active pipewire.service`` +
      ``pulseaudio.service`` when applicable).
    * macOS — macOS ``coreaudiod`` is managed by launchd and generally
      respawns; this listener is effectively a Noop on darwin.
    """

    DOWN = "down"
    UP = "up"


class WatchdogState(StrEnum):
    """§4.4.1 lifecycle states exposed by :class:`VoiceCaptureWatchdog`.

    * :attr:`IDLE` — baseline; no degradation observed. Hot-plug adds
      are ignored, removes of the active endpoint still trigger a
      re-cascade.
    * :attr:`BACKOFF` — one or more warm re-probes are scheduled after
      a call to :meth:`VoiceCaptureWatchdog.report_deafness`. The
      endpoint transitions back to :attr:`IDLE` on the first HEALTHY
      re-probe.
    * :attr:`DEGRADED` — the backoff schedule exhausted without a
      HEALTHY probe. The pipeline runs in push-to-talk-only mode until
      the user reboots or a new viable device hot-plugs in.
    """

    IDLE = "idle"
    BACKOFF = "backoff"
    DEGRADED = "degraded"


# ── Validation tables (immutable) ───────────────────────────────────────


ALLOWED_SAMPLE_RATES: frozenset[int] = frozenset(
    {8_000, 16_000, 22_050, 24_000, 32_000, 44_100, 48_000, 88_200, 96_000, 192_000},
)
"""Sample rates we accept in a :class:`Combo`. Wider than what we use in
practice so cascade overrides can target unusual hardware."""


ALLOWED_FORMATS: frozenset[str] = frozenset({"int16", "int24", "float32"})
"""PortAudio sample formats we know how to drive end-to-end through the
:class:`~sovyx.voice.FrameNormalizer` resampler."""


_ALLOWED_CHANNELS_MIN = 1
_ALLOWED_CHANNELS_MAX = 8
_ALLOWED_FRAMES_PER_BUFFER_MIN = 64
_ALLOWED_FRAMES_PER_BUFFER_MAX = 8_192


ALLOWED_HOST_APIS_BY_PLATFORM: Mapping[str, frozenset[str]] = {
    "win32": frozenset(
        {
            "WASAPI",
            "Windows WASAPI",
            "WDM-KS",
            "Windows WDM-KS",
            "DirectSound",
            "Windows DirectSound",
            "MME",
        },
    ),
    "linux": frozenset({"ALSA", "PulseAudio", "PipeWire", "JACK"}),
    "darwin": frozenset({"CoreAudio", "Core Audio"}),
}
"""Host APIs we accept on each platform.

Both bare (``"WASAPI"``) and PortAudio-formatted (``"Windows WASAPI"``)
labels are accepted so callers can use whichever the upstream API gave
them without an extra normalization step.
"""


def _platform_key() -> str:
    """Return the current platform key for :data:`ALLOWED_HOST_APIS_BY_PLATFORM`.

    Splits the hairsplitting of ``sys.platform`` ("linux", "linux2",
    "win32", "darwin", …) into the three buckets we care about and
    falls back to ``"linux"`` for unknown POSIX-likes.
    """
    plat = sys.platform
    if plat.startswith("win"):
        return "win32"
    if plat == "darwin":
        return "darwin"
    return "linux"


def _allowed_host_apis_for(platform_key: str) -> frozenset[str]:
    return ALLOWED_HOST_APIS_BY_PLATFORM.get(platform_key, frozenset())


# ── Dataclasses ─────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class Combo:
    """The audio configuration tuple that opens a capture stream.

    Validated at construction so an invalid tuple (out-of-range rate,
    unknown host API for the current platform, channels = 0) raises
    immediately instead of propagating into the cascade.

    Args:
        host_api: Host-API label (``"WASAPI"`` or PortAudio's
            ``"Windows WASAPI"`` — both accepted). Validated against
            :data:`ALLOWED_HOST_APIS_BY_PLATFORM`.
        sample_rate: Device-side sample rate in Hz. Pipeline downstream
            resamples to 16 kHz mono int16 regardless.
        channels: Channel count requested from PortAudio. The
            normalizer mixes down to mono.
        sample_format: PortAudio sample format (``"int16"`` / ``"int24"``
            / ``"float32"``).
        exclusive: WASAPI exclusive mode flag (Windows only meaningful;
            ignored on other platforms).
        auto_convert: Let WASAPI resample / rechannel transparently
            (Windows only meaningful).
        frames_per_buffer: PortAudio buffer size in samples. ``480`` is
            30 ms at 16 kHz and matches the Silero VAD window cadence.
        platform_key: Optional override for the platform key used during
            validation. Tests pass ``"win32"`` to validate Windows
            cascades on a non-Windows host without monkey-patching
            :mod:`sys`.

    Raises:
        ValueError: On any out-of-range field or platform-host_api
            mismatch.
    """

    host_api: str
    sample_rate: int
    channels: int
    sample_format: str
    exclusive: bool
    auto_convert: bool
    frames_per_buffer: int
    platform_key: str = ""  # empty = use current platform; set in tests

    def __post_init__(self) -> None:
        if not self.host_api:
            msg = "host_api must be a non-empty string"
            raise ValueError(msg)
        plat = self.platform_key or _platform_key()
        allowed = _allowed_host_apis_for(plat)
        if allowed and self.host_api not in allowed:
            msg = (
                f"host_api={self.host_api!r} is not allowed on platform "
                f"{plat!r}; expected one of {sorted(allowed)}"
            )
            raise ValueError(msg)
        if self.sample_rate not in ALLOWED_SAMPLE_RATES:
            msg = (
                f"sample_rate={self.sample_rate} is not allowed; "
                f"expected one of {sorted(ALLOWED_SAMPLE_RATES)}"
            )
            raise ValueError(msg)
        if not _ALLOWED_CHANNELS_MIN <= self.channels <= _ALLOWED_CHANNELS_MAX:
            msg = (
                f"channels={self.channels} out of range "
                f"[{_ALLOWED_CHANNELS_MIN}, {_ALLOWED_CHANNELS_MAX}]"
            )
            raise ValueError(msg)
        if self.sample_format not in ALLOWED_FORMATS:
            msg = (
                f"sample_format={self.sample_format!r} not allowed; "
                f"expected one of {sorted(ALLOWED_FORMATS)}"
            )
            raise ValueError(msg)
        if not (
            _ALLOWED_FRAMES_PER_BUFFER_MIN
            <= self.frames_per_buffer
            <= _ALLOWED_FRAMES_PER_BUFFER_MAX
        ):
            msg = (
                f"frames_per_buffer={self.frames_per_buffer} out of range "
                f"[{_ALLOWED_FRAMES_PER_BUFFER_MIN}, {_ALLOWED_FRAMES_PER_BUFFER_MAX}]"
            )
            raise ValueError(msg)


@dataclass(frozen=True, slots=True)
class RemediationHint:
    """Single-source-of-truth pointer to user-facing remediation copy.

    The :attr:`code` is an i18n key; the frontend resolves it via
    ``useTranslation()`` and the CLI resolves it server-side. Backend
    code never inlines a translatable sentence.

    Attributes:
        code: Stable i18n key, e.g. ``"remediation.muted"``. Listed in
            :mod:`sovyx.voice.health._remediation`.
        severity: One of ``"info"`` / ``"warn"`` / ``"error"``.
        cli_action: Optional CLI command that fixes the issue, e.g.
            ``"sovyx doctor voice --fix"``. ``None`` when the
            remediation is purely manual.
    """

    code: str
    severity: str
    cli_action: str | None = None

    def __post_init__(self) -> None:
        if not self.code:
            msg = "remediation code must be non-empty"
            raise ValueError(msg)
        if self.severity not in {"info", "warn", "error"}:
            msg = f"severity={self.severity!r} not in {{info, warn, error}}"
            raise ValueError(msg)


@dataclass(frozen=True, slots=True)
class ProbeResult:
    """Outcome of a single probe.

    Args:
        diagnosis: Triage label.
        mode: Whether this was a cold or warm probe.
        combo: The combo that was probed.
        vad_max_prob: Max SileroVAD speech probability across the probe
            window. Cold probes set this to ``None`` (VAD not run).
        vad_mean_prob: Mean SileroVAD speech probability. ``None`` for
            cold probes.
        rms_db: RMS of the probe window in dBFS.
        callbacks_fired: Number of PortAudio callback invocations
            observed during the probe.
        duration_ms: Actual probe duration (may be < requested when an
            early-exit condition triggered).
        error: PortAudio / OS exception text when the open failed.
        remediation: Optional pointer to user-facing remediation copy.
    """

    diagnosis: Diagnosis
    mode: ProbeMode
    combo: Combo
    vad_max_prob: float | None
    vad_mean_prob: float | None
    rms_db: float
    callbacks_fired: int
    duration_ms: int
    error: str | None = None
    remediation: RemediationHint | None = None


@dataclass(frozen=True, slots=True)
class AudioSubsystemFingerprint:
    """SHA256 snapshot of OS-level audio configuration.

    Used to detect cumulative Windows updates / PulseAudio config
    changes / CoreAudio HAL plugin churn that would otherwise slip past
    a coarse ``platform.version()`` check. Only the SHA fields are
    load-bearing; the timestamp is diagnostic.

    Attributes:
        windows_audio_endpoints_sha: SHA256 over the MMDevices subtree
            on Windows (empty on other platforms).
        windows_fxproperties_global_sha: SHA256 over every active
            endpoint's FxProperties subtree (Windows only).
        linux_pulseaudio_config_sha: SHA256 over PulseAudio /
            PipeWire config files (Linux only).
        macos_coreaudio_plugins_sha: SHA256 over the CoreAudio HAL
            plugin list (macOS only).
        computed_at: ISO-8601 UTC timestamp.
    """

    windows_audio_endpoints_sha: str = ""
    windows_fxproperties_global_sha: str = ""
    linux_pulseaudio_config_sha: str = ""
    macos_coreaudio_plugins_sha: str = ""
    computed_at: str = ""


@dataclass(frozen=True, slots=True)
class ProbeHistoryEntry:
    """One element of :attr:`ComboEntry.probe_history` (ring buffer).

    Bounded to the last 10 probes per endpoint so the on-disk file stays
    small even after months of operation.
    """

    ts: str  # ISO-8601 UTC
    mode: ProbeMode
    diagnosis: Diagnosis
    vad_max_prob: float | None
    rms_db: float
    duration_ms: int


@dataclass(frozen=True, slots=True)
class ComboEntry:
    """One row of :class:`~sovyx.voice.health.combo_store.ComboStore`.

    Pure data; persistence rules + invalidation gates live in the store.
    """

    endpoint_guid: str
    device_friendly_name: str
    device_interface_name: str
    device_class: str
    endpoint_fxproperties_sha: str
    winning_combo: Combo
    validated_at: str
    validation_mode: ProbeMode
    vad_max_prob_at_validation: float | None
    vad_mean_prob_at_validation: float | None
    rms_db_at_validation: float
    probe_duration_ms: int
    detected_apos_at_validation: tuple[str, ...]
    cascade_attempts_before_success: int
    boots_validated: int
    last_boot_validated: str
    last_boot_diagnosis: Diagnosis
    probe_history: tuple[ProbeHistoryEntry, ...] = ()
    pinned: bool = False
    needs_revalidation: bool = False  # set by R6/R7/R8/R9/R10/R11 on load


@dataclass(frozen=True, slots=True)
class OverrideEntry:
    """One row of :class:`~sovyx.voice.health.capture_overrides.CaptureOverrides`."""

    endpoint_guid: str
    device_friendly_name: str
    pinned_combo: Combo
    pinned_at: str
    pinned_by: str  # "user" | "wizard" | "cli"
    reason: str = ""


@dataclass(frozen=True, slots=True)
class HotplugEvent:
    """Platform-agnostic view of one audio-device hot-plug notification.

    Populated by :mod:`~sovyx.voice.health._hotplug_win` /
    :mod:`~sovyx.voice.health._hotplug_linux` /
    :mod:`~sovyx.voice.health._hotplug_mac`; consumed by
    :class:`~sovyx.voice.health.watchdog.VoiceCaptureWatchdog`.

    :attr:`endpoint_guid` mirrors the GUID the cascade uses as its
    :class:`ComboStore` key. It is best-effort: some OS events report a
    device path or an interface name only, in which case this field is
    ``None`` and the watchdog compares :attr:`device_friendly_name`
    against the active endpoint's friendly name instead. When both
    fields are empty the watchdog treats the event as a generic
    add/remove signal (no endpoint-scoped action).
    """

    kind: HotplugEventKind
    endpoint_guid: str | None = None
    device_friendly_name: str | None = None
    device_interface_name: str | None = None


@dataclass(frozen=True, slots=True)
class PowerEvent:
    """Platform-agnostic view of one OS power-management notification.

    ADR §4.4.4. Emitted by :class:`~sovyx.voice.health._power.PowerEventListener`
    and consumed by :meth:`~sovyx.voice.health.watchdog.VoiceCaptureWatchdog.on_power_event`.
    """

    kind: PowerEventKind


@dataclass(frozen=True, slots=True)
class AudioServiceEvent:
    """Platform-agnostic view of one audio-service lifecycle transition.

    ADR §4.4.5. Emitted by
    :class:`~sovyx.voice.health._audio_service.AudioServiceMonitor` and
    consumed by :meth:`~sovyx.voice.health.watchdog.VoiceCaptureWatchdog.on_audio_service_event`.
    """

    kind: AudioServiceEventKind


@dataclass(frozen=True, slots=True)
class CascadeResult:
    """Outcome of one :func:`~sovyx.voice.health.cascade.run_cascade` call.

    Attributes:
        winning_combo: The combo that produced :attr:`Diagnosis.HEALTHY`
            (warm) or callbacks-OK (cold). ``None`` when the cascade
            exhausted without a winner.
        winning_probe: The :class:`ProbeResult` that earned the winning
            combo. ``None`` on exhaustion.
        attempts: Every probe that ran during the cascade, in order.
            Always non-empty after a successful run; ``None`` when the
            cascade short-circuited on a fast-path lookup that produced
            no fresh probe.
        attempts_count: Number of attempts before success (0 = ComboStore
            fast path or pinned override hit). Independent of
            :attr:`attempts` length so callers don't have to subtract 1
            for the winning attempt.
        budget_exhausted: ``True`` when the total wall-clock budget ran
            out before a winner emerged.
        source: Where the winning combo came from —
            ``"pinned"`` / ``"store"`` / ``"cascade"`` / ``"none"``.
        endpoint_guid: GUID of the endpoint the cascade ran on (echoed
            for caller convenience).
    """

    endpoint_guid: str
    winning_combo: Combo | None
    winning_probe: ProbeResult | None
    attempts: tuple[ProbeResult, ...]
    attempts_count: int
    budget_exhausted: bool
    source: str  # "pinned" | "store" | "cascade" | "none"


@dataclass(frozen=True, slots=True)
class LoadReport:
    """Result of :meth:`~sovyx.voice.health.combo_store.ComboStore.load`.

    Attributes:
        rules_applied: Pairs of ``(rule_code, scope)`` where ``scope`` is
            either an endpoint GUID or ``"<global>"``.
        entries_loaded: Number of valid entries available after load.
        entries_dropped: Number of entries discarded by sanity rules
            (R12) or platform mismatch.
        backup_used: ``True`` when the main file was unreadable and the
            ``.bak`` file rescued the load.
        archived_to: Optional path the corrupt file was moved to.
    """

    rules_applied: tuple[tuple[str, str], ...]
    entries_loaded: int
    entries_dropped: int
    backup_used: bool
    archived_to: Path | None


@dataclass(slots=True)
class ComboStoreStats:
    """In-memory hit/miss counters since the last :meth:`load`.

    Mutable on purpose so the store can update counters in place without
    rebuilding a frozen object on every probe. Read-only from the
    outside.
    """

    fast_path_hits: int = 0
    fast_path_misses: int = 0
    invalidations_by_reason: dict[str, int] = field(default_factory=dict)


__all__ = [
    "ALLOWED_FORMATS",
    "ALLOWED_HOST_APIS_BY_PLATFORM",
    "ALLOWED_SAMPLE_RATES",
    "AudioSubsystemFingerprint",
    "CascadeResult",
    "Combo",
    "ComboEntry",
    "ComboStoreStats",
    "Diagnosis",
    "LoadReport",
    "OverrideEntry",
    "ProbeHistoryEntry",
    "ProbeMode",
    "ProbeResult",
    "RemediationHint",
]
