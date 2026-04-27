"""Probe outcome + abstract capture interface dataclasses.

Split from the legacy ``contract.py`` (CLAUDE.md anti-pattern #16
hygiene) — see ``MISSION-voice-godfile-splits-v0.24.1.md`` Part 3 / T01.

Owns the cold/warm probe result types
(:class:`ProbeResult`, :class:`ProbeHistoryEntry`,
:class:`RemediationHint`, :class:`AudioSubsystemFingerprint`) plus the
:class:`CaptureTaskProto` Protocol the bypass strategies depend on.

All public names re-exported from :mod:`sovyx.voice.health.contract`.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:
    import numpy as np
    import numpy.typing as npt

    from sovyx.voice._capture_task import (
        AlsaHwDirectRestartResult,
        ExclusiveRestartResult,
        SessionManagerRestartResult,
        SharedRestartResult,
    )
    from sovyx.voice.device_enum import DeviceEntry
    from sovyx.voice.health.contract._combo import Combo, ProbeMode
    from sovyx.voice.health.contract._diagnosis import Diagnosis


__all__ = [
    "AudioSubsystemFingerprint",
    "CaptureTaskProto",
    "ProbeHistoryEntry",
    "ProbeResult",
    "RemediationHint",
]


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
    def active_device_index(self) -> int: ...

    @property
    def active_device_kind(self) -> str: ...

    @property
    def host_api_name(self) -> str | None: ...

    async def request_exclusive_restart(self) -> ExclusiveRestartResult: ...

    async def request_shared_restart(self) -> SharedRestartResult: ...

    async def request_alsa_hw_direct_restart(self) -> AlsaHwDirectRestartResult: ...

    async def request_session_manager_restart(
        self,
        target_device: DeviceEntry | None = None,
    ) -> SessionManagerRestartResult: ...

    async def tap_recent_frames(
        self,
        duration_s: float,
    ) -> npt.NDArray[np.int16]: ...

    def samples_written_mark(self) -> tuple[int, int]:
        """Return an opaque ``(epoch, samples_written)`` pair.

        The tuple is opaque: pass it unchanged to
        :meth:`tap_frames_since_mark`; do not compare, do arithmetic
        on, or log the individual components without acknowledging they
        are implementation details of the ring-buffer state machine.

        Implementations MUST produce the pair from a single atomic read
        of the internal packed state so both ints are guaranteed to
        correspond to the same state generation (same epoch). See
        :mod:`sovyx.voice._capture_task` for the packed encoding
        rationale.

        v1.3 §4.2.2 rationale — the contract is ``tuple[int, int]``
        rather than a single packed ``int`` so the pair survives every
        JSON / Prometheus / structlog serialization boundary without
        silently truncating (packed marks can exceed ``2**53``, the
        JavaScript ``Number`` safe range). The packed representation
        remains private to the capture task.

        Synchronous. Safe to call from any async context.
        """
        ...

    async def tap_frames_since_mark(
        self,
        mark: tuple[int, int],
        min_samples: int,
        max_wait_s: float,
    ) -> npt.NDArray[np.int16]:
        """Return frames written AFTER ``mark`` was captured.

        Polls the ring-buffer state until at least ``min_samples`` new
        frames have accumulated post-``mark`` or ``max_wait_s`` elapses,
        then returns the tail slice via :meth:`tap_recent_frames`. The
        returned array always corresponds to audio the capture task
        delivered strictly after :meth:`samples_written_mark` was
        called — the primary fix for the v0.21.2 probe-window
        contamination bug (see dossier ``SVX-VOICE-LINUX-20260422``).

        Ring-buffer reset handling: if the epoch bundled in ``mark``
        differs from the current epoch, the buffer was reallocated
        mid-session (e.g. a WASAPI exclusive restart zeroed the ring).
        Implementations MUST treat every sample currently in the buffer
        as post-mark and return whatever is available without waiting
        for the absent pre-reset frame count.

        Args:
            mark: Opaque pair returned by
                :meth:`samples_written_mark`. Pass unchanged; never
                synthesize one.
            min_samples: Target accumulation threshold — the method
                returns as soon as this many new frames are available.
            max_wait_s: Hard timeout. On expiry the method returns
                whatever fresh frames exist (possibly empty) rather
                than blocking the coordinator's bypass loop.

        Returns:
            Zero-copy-free slice of the ring buffer, at most
            ``min_samples`` long. Empty array on timeout with no new
            frames.
        """
        ...

    def apply_mic_ducking_db(self, gain_db: float) -> None: ...
