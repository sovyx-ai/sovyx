"""Probe outcome enum.

Split from the legacy ``contract.py`` (CLAUDE.md anti-pattern #16
hygiene) — see ``MISSION-voice-godfile-splits-v0.24.1.md`` Part 3 / T01.
This sub-module is leaf: no internal contract dependencies. Every
public name is re-exported from :mod:`sovyx.voice.health.contract`
for backward compatibility.
"""

from __future__ import annotations

from enum import StrEnum

__all__ = ["Diagnosis"]


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
    # Phase 6 / T6.3 — distinct from DEVICE_BUSY: the endpoint
    # fundamentally doesn't permit exclusive mode (driver doesn't
    # expose an exclusive endpoint, hardware doesn't support it,
    # OR the OS-level GP-blocked path triggers
    # AUDCLNT_E_EXCLUSIVE_MODE_NOT_ALLOWED without an
    # ``access denied`` companion). Cascade should immediately
    # advance past every exclusive-mode combo for this endpoint;
    # waiting + retry is futile (unlike DEVICE_BUSY where another
    # app might release the lock).
    EXCLUSIVE_MODE_NOT_AVAILABLE = "exclusive_mode_not_available"
    # Phase 6 / T6.4 — WASAPI rejected the requested buffer size
    # (AUDCLNT_E_BUFFER_SIZE_NOT_ALIGNED / AUDCLNT_E_BUFFER_TOO_LARGE /
    # AUDCLNT_E_BUFFER_SIZE_ERROR). Distinct from FORMAT_MISMATCH:
    # format (rate / channels / sample_format) is fine, only the
    # ``frames_per_buffer`` is wrong. Cascade should retry with a
    # different ``frames_per_buffer`` (typically the device's
    # ``DefaultDevicePeriod``); same combo retry is futile.
    INSUFFICIENT_BUFFER_SIZE = "insufficient_buffer_size"
    PERMISSION_DENIED = "permission_denied"
    # Phase 6 / T6.2 — stream opened + started but ZERO callbacks fired
    # within ``probe_stream_open_timeout_threshold_ms`` (default 5 s).
    # Distinguishes from NO_SIGNAL: NO_SIGNAL means callbacks fired with
    # silent/empty PCM (mic muted, signal-destroyed APO); STREAM_OPEN_TIMEOUT
    # means the driver accepted the stream but never started delivering
    # audio at all (USB resource timeout, IAudioClient stuck mid-init,
    # kernel-side wedge that doesn't surface as an open-time error).
    # Cure is physical — replug or reboot.
    STREAM_OPEN_TIMEOUT = "stream_open_timeout"
    # Kernel-side IAudioClient invalidated state: device enumerates as
    # healthy (PnP status=OK, ConfigManager=0) but every host API returns
    # paInvalidDevice (-9996) on stream open because the IMMDevice's
    # internal ``IAudioClient::Initialize`` path is stuck. Triggered by
    # USB resource timeouts (LiveKernelEvent 0x1cc), driver hot-swaps,
    # or mid-stream PnP churn. No user-mode cure exists — sovyx must
    # quarantine the endpoint and fail-over to the next available
    # capture device. Cure is physical: replug or reboot.
    KERNEL_INVALIDATED = "kernel_invalidated"
    # L2.5 mixer sanity diagnoses — emitted by
    # :func:`sovyx.voice.health._mixer_sanity.check_and_maybe_heal`. Distinct
    # from ``APO_DEGRADED`` because the fix path is mixer-layer (not APO
    # bypass) and the root cause is pre-PortAudio gain misconfiguration,
    # not capture-side DSP. See ADR-voice-mixer-sanity-l2.5-bidirectional §2.
    MIXER_ZEROED = "mixer_zeroed"
    """Attenuation regime — factory mixer gain too low, KB match found.
    Fix is applied in-place by L2.5 via :class:`MixerPresetSpec`.
    """

    MIXER_SATURATED = "mixer_saturated"
    """Saturation regime — factory mixer gain + boosts clipping internally.
    Previously a sub-case of ``APO_DEGRADED``; split out as first-class so
    the bypass coordinator routes to the mixer reset path instead of APO
    bypass strategies. Existing ``LinuxALSAMixerResetBypass`` eligibility
    extends to this diagnosis.
    """

    MIXER_UNKNOWN_PATTERN = "mixer_unknown_pattern"
    """Mixer state outside the healthy range but no KB profile matches.
    L2.5 defers; cascade proceeds to the platform walk. Telemetry flags
    the hardware for prioritisation in KB growth.
    """

    MIXER_CUSTOMIZED = "mixer_customized"
    """User customization detected (6-signal heuristic score > 0.75).
    L2.5 is a no-op; the user's intentional tuning is preserved per
    invariant I4.
    """

    UNKNOWN = "unknown"
