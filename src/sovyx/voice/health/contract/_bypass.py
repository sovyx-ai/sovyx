"""Capture-integrity + bypass + watchdog event dataclasses.

Split from the legacy ``contract.py`` (CLAUDE.md anti-pattern #16
hygiene) — see ``MISSION-voice-godfile-splits-v0.24.1.md`` Part 3 / T01.

Owns the OS-agnostic capture-integrity types
(:class:`IntegrityVerdict`, :class:`IntegrityResult`,
:class:`BypassVerdict`, :class:`BypassContext`, :class:`BypassOutcome`)
and the watchdog event lifecycle types
(:class:`HotplugEvent`, :class:`PowerEvent`, :class:`AudioServiceEvent`,
:class:`WatchdogState`).

These types generalise the Windows-only Voice-Clarity auto-bypass into
an OS-agnostic detection layer paired with a platform-specific strategy
pattern (``PlatformBypassStrategy`` Protocol in
:mod:`sovyx.voice.health.bypass._strategy`).

See ``docs-internal/plans/voice-apo-os-agnostic-fix.md`` §2.3 for the
derivation of each field + threshold.

All public names re-exported from :mod:`sovyx.voice.health.contract`.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable
    from datetime import datetime

    from sovyx.voice.health.contract._probe_result import CaptureTaskProto


__all__ = [
    "AudioServiceEvent",
    "AudioServiceEventKind",
    "BypassContext",
    "BypassOutcome",
    "BypassVerdict",
    "HotplugEvent",
    "HotplugEventKind",
    "IntegrityResult",
    "IntegrityVerdict",
    "PowerEvent",
    "PowerEventKind",
    "WatchdogState",
]


# ── Watchdog event lifecycle ────────────────────────────────────────────


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


# ── Capture-integrity + bypass strategy (Phase 1) ───────────────────────


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
    current_device_index: int = -1
    current_device_kind: str = "unknown"


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
