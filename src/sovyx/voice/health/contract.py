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
from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Mapping
    from datetime import datetime
    from pathlib import Path

    import numpy as np
    import numpy.typing as npt

    from sovyx.voice._capture_task import ExclusiveRestartResult, SharedRestartResult


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
    # Kernel-side IAudioClient invalidated state: device enumerates as
    # healthy (PnP status=OK, ConfigManager=0) but every host API returns
    # paInvalidDevice (-9996) on stream open because the IMMDevice's
    # internal ``IAudioClient::Initialize`` path is stuck. Triggered by
    # USB resource timeouts (LiveKernelEvent 0x1cc), driver hot-swaps,
    # or mid-stream PnP churn. No user-mode cure exists — sovyx must
    # quarantine the endpoint and fail-over to the next available
    # capture device. Cure is physical: replug or reboot.
    KERNEL_INVALIDATED = "kernel_invalidated"
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
            ``"pinned"`` / ``"store"`` / ``"cascade"`` / ``"none"`` /
            ``"quarantined"``. ``"quarantined"`` means the endpoint is in
            the §4.4.7 kernel-invalidated quarantine and no probe ran;
            callers should fail-over to the next viable endpoint.
        endpoint_guid: GUID of the endpoint the cascade ran on (echoed
            for caller convenience).
    """

    endpoint_guid: str
    winning_combo: Combo | None
    winning_probe: ProbeResult | None
    attempts: tuple[ProbeResult, ...]
    attempts_count: int
    budget_exhausted: bool
    source: str  # "pinned" | "store" | "cascade" | "none" | "quarantined"


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


# ── Capture-integrity + bypass strategy (Phase 1) ───────────────────────
#
# These types generalise the Windows-only Voice-Clarity auto-bypass into an
# OS-agnostic detection layer (:class:`IntegrityVerdict` / :class:`IntegrityResult`)
# paired with a platform-specific strategy pattern
# (:class:`BypassVerdict` / :class:`BypassContext` / :class:`BypassOutcome`
# + :class:`PlatformBypassStrategy` Protocol in
# :mod:`sovyx.voice.health.bypass._strategy`).
#
# See ``docs-internal/plans/voice-apo-os-agnostic-fix.md`` §2.3 for the
# derivation of each field + threshold.


class IntegrityVerdict(StrEnum):
    """OS-agnostic classification of a live capture stream's signal quality.

    Distinct from :class:`Diagnosis` because :class:`Diagnosis` mixes
    stream-open outcomes (``DRIVER_ERROR``, ``DEVICE_BUSY``) with signal
    analysis. :class:`IntegrityVerdict` is pure-signal: it is only ever
    computed against a *live* capture stream whose ring buffer already
    carries frames.

    Members:
        HEALTHY: RMS alive, VAD responsive, spectral envelope intact.
        APO_DEGRADED: RMS alive but VAD dead AND spectral envelope
            flattened — capture-side DSP (Windows Voice Clarity,
            PulseAudio ``module-echo-cancel``, CoreAudio VPIO) is
            destroying the signal before it reaches user space.
        DRIVER_SILENT: RMS near zero / flat DC — the driver is open but
            not delivering audio. Distinct from APO_DEGRADED because
            the fix is different (reopen / re-enumerate vs APO bypass).
        VAD_MUTE: VAD dead but spectrum intact and RMS in the noise
            floor band — user is genuinely not speaking. Re-probe
            later; not a fault.
        INCONCLUSIVE: Probe aborted (timeout, teardown, insufficient
            frames in ring buffer). Caller retries.
    """

    HEALTHY = "healthy"
    APO_DEGRADED = "apo_degraded"
    DRIVER_SILENT = "driver_silent"
    VAD_MUTE = "vad_mute"
    INCONCLUSIVE = "inconclusive"


@dataclass(frozen=True, slots=True)
class IntegrityResult:
    """Outcome of one :class:`CaptureIntegrityProbe` run.

    Populated by :meth:`~sovyx.voice.health.capture_integrity.CaptureIntegrityProbe.probe_warm`.
    Every field is deterministic-from-input so tests can assert exact
    values against synthesised signals.

    Args:
        verdict: Classification label.
        endpoint_guid: The endpoint the probe ran against — echoed so
            coordinators don't drift if the active endpoint rebinds
            mid-probe.
        rms_db: RMS of the probe window in dBFS. ``-inf`` normalised to
            ``-120.0`` for finite-JSON friendliness.
        vad_max_prob: Peak SileroVAD speech probability across the
            window. In ``[0.0, 1.0]``.
        spectral_flatness: Wiener entropy of the magnitude spectrum in
            ``[0.0, 1.0]``. White noise ≈ 1.0, pure tone ≈ 0.0,
            clean speech ≈ 0.10–0.15, APO-destroyed speech ≈ 0.28–0.35.
        spectral_rolloff_hz: 85 %-energy roll-off frequency in Hz.
            Voice-Clarity's low-pass pulls this below 4 kHz; clean
            speech sits at 6–8 kHz.
        duration_s: Actual probe duration (may be shorter than
            requested when the buffer had fewer frames).
        probed_at_utc: Timestamp of probe completion.
        raw_frames: Number of int16 samples actually analysed.
        detail: Optional diagnostic string — set on ``INCONCLUSIVE``
            to explain *why* (e.g. ``"ring_buffer_underrun"``).
    """

    verdict: IntegrityVerdict
    endpoint_guid: str
    rms_db: float
    vad_max_prob: float
    spectral_flatness: float
    spectral_rolloff_hz: float
    duration_s: float
    probed_at_utc: datetime
    raw_frames: int
    detail: str = ""


class BypassVerdict(StrEnum):
    """Outcome of one :meth:`PlatformBypassStrategy.apply` invocation.

    A strategy reports exactly one verdict per ``apply``. The
    coordinator reads the verdict + the post-apply :class:`IntegrityResult`
    to decide whether to stop, advance to the next strategy, or revert.

    Members:
        APPLIED_HEALTHY: Strategy applied AND the subsequent integrity
            re-probe returned HEALTHY. Terminal success.
        APPLIED_STILL_DEAD: Strategy applied cleanly but the re-probe
            still classifies the signal as APO_DEGRADED / DRIVER_SILENT.
            Coordinator advances.
        NOT_APPLICABLE: Strategy's :meth:`probe_eligibility` returned
            :attr:`Eligibility.applicable=False` (e.g. Windows exclusive
            mode disabled by policy, Linux ALSA hw node not exposed).
        FAILED_TO_APPLY: ``apply`` itself raised or the underlying
            restart verdict reported failure. Coordinator advances.
        REVERTED: ``apply`` succeeded but ``revert`` was called
            subsequently (strategy B proved strictly better than A).
    """

    APPLIED_HEALTHY = "applied_healthy"
    APPLIED_STILL_DEAD = "applied_still_dead"
    NOT_APPLICABLE = "not_applicable"
    FAILED_TO_APPLY = "failed_to_apply"
    REVERTED = "reverted"


@dataclass(frozen=True, slots=True)
class Eligibility:
    """Feasibility report from :meth:`PlatformBypassStrategy.probe_eligibility`.

    A strategy whose eligibility check returns ``applicable=False`` is
    skipped by the coordinator without counting toward
    ``bypass_strategy_max_attempts`` — a non-applicable strategy is not
    an attempt.

    Args:
        applicable: ``True`` iff the strategy's preconditions are met
            on the current endpoint + OS + tuning configuration.
        reason: Machine-readable reason token. Stable across minor
            versions so dashboards can key on it. Examples:
            ``"exclusive_mode_disabled_by_policy"``,
            ``"not_wasapi_endpoint"``, ``"alsa_hw_node_unavailable"``,
            ``"not_implemented_phase_3_pipewire"``.
        estimated_cost_ms: Informational forecast of how long the
            subsequent ``apply`` is expected to take. Used by the
            coordinator only for telemetry, never for sequencing.
    """

    applicable: bool
    reason: str = ""
    estimated_cost_ms: int = 0


@runtime_checkable
class CaptureTaskProto(Protocol):
    """Minimum surface :class:`CaptureIntegrityCoordinator` needs from
    :class:`sovyx.voice._capture_task.AudioCaptureTask`.

    Defined here (not in ``_capture_task``) so bypass strategies depend
    on the abstract Protocol, not the concrete class — keeps the
    dependency direction clean and makes testing strategies trivial
    (fake capture task is a plain object).
    """

    @property
    def active_device_guid(self) -> str: ...

    @property
    def active_device_name(self) -> str: ...

    @property
    def host_api_name(self) -> str | None: ...

    async def request_exclusive_restart(self) -> ExclusiveRestartResult: ...

    async def request_shared_restart(self) -> SharedRestartResult: ...

    async def tap_recent_frames(
        self,
        duration_s: float,
    ) -> npt.NDArray[np.int16]: ...

    def apply_mic_ducking_db(self, gain_db: float) -> None: ...


@dataclass(frozen=True, slots=True)
class BypassContext:
    """Per-apply context handed to a :class:`PlatformBypassStrategy`.

    Pure-data; no mutable references. The coordinator rebuilds this
    object each attempt so strategies cannot race on shared state.

    Args:
        endpoint_guid: GUID of the endpoint the coordinator is
            operating on.
        endpoint_friendly_name: Human-readable mic name. Used for log
            messages and dashboards, never for logic.
        host_api_name: PortAudio host-API label (``"Windows WASAPI"``,
            ``"ALSA"``, ``"CoreAudio"`` …). Strategies use this for
            their eligibility checks.
        platform_key: Normalised ``sys.platform`` bucket (``"win32"``
            / ``"linux"`` / ``"darwin"``). Pre-computed so tests can
            pin the bucket without monkey-patching :mod:`sys`.
        capture_task: :class:`CaptureTaskProto` reference — the only
            mutating edge a strategy has into the running pipeline.
        probe_fn: Callable that re-runs the warm integrity probe
            against the live capture stream. Strategies invoke it
            after ``apply`` to validate effectiveness.
    """

    endpoint_guid: str
    endpoint_friendly_name: str
    host_api_name: str
    platform_key: str
    capture_task: CaptureTaskProto
    probe_fn: Callable[[], Awaitable[IntegrityResult]]


@dataclass(frozen=True, slots=True)
class BypassOutcome:
    """Record of one full strategy attempt — emitted as a log event.

    Args:
        strategy_name: The :attr:`PlatformBypassStrategy.name` of the
            strategy that ran.
        attempt_index: Position in the coordinator's per-session
            iteration (0 = first strategy tried).
        verdict: Overall outcome.
        integrity_before: Integrity probe result captured immediately
            before ``apply``.
        integrity_after: Integrity probe result captured after the
            post-apply settle window. ``None`` iff ``verdict`` is
            :attr:`BypassVerdict.NOT_APPLICABLE` or
            :attr:`BypassVerdict.FAILED_TO_APPLY` (no post-apply probe
            was possible).
        elapsed_ms: Wall-clock time from entering ``apply`` to emitting
            the outcome.
        detail: Free-form diagnostic string. Populated on failure paths
            with the classified error.
    """

    strategy_name: str
    attempt_index: int
    verdict: BypassVerdict
    integrity_before: IntegrityResult
    integrity_after: IntegrityResult | None
    elapsed_ms: float
    detail: str = ""


__all__ = [
    "ALLOWED_FORMATS",
    "ALLOWED_HOST_APIS_BY_PLATFORM",
    "ALLOWED_SAMPLE_RATES",
    "AudioSubsystemFingerprint",
    "BypassContext",
    "BypassOutcome",
    "BypassVerdict",
    "CaptureTaskProto",
    "CascadeResult",
    "Combo",
    "ComboEntry",
    "ComboStoreStats",
    "Diagnosis",
    "Eligibility",
    "IntegrityResult",
    "IntegrityVerdict",
    "LoadReport",
    "OverrideEntry",
    "ProbeHistoryEntry",
    "ProbeMode",
    "ProbeResult",
    "RemediationHint",
]
