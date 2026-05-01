"""Cold-mode probe diagnosis + open-error classification.

ADR §4.3 cold-mode diagnosis amended by Voice Windows Paranoid Mission
Furo W-1: ``_diagnose_cold`` validates RMS energy in addition to
callback count, closing the silent-combo persistence loop.

This module contains:

* Open-error keyword sets for :func:`_classify_open_error` —
  case-insensitive substring matching mapping PortAudio / OS exception
  text to :class:`~sovyx.voice.health.contract.Diagnosis` values.
* :func:`_classify_open_error` — the exception-to-Diagnosis mapper.
* :data:`_COLD_STRICT_VALIDATION_ENABLED` — the Furo W-1 feature flag.
* :func:`_diagnose_cold` — the cold-mode diagnosis function.
"""

from __future__ import annotations

from sovyx.engine.config import VoiceTuningConfig as _VoiceTuning
from sovyx.observability.logging import get_logger
from sovyx.voice.health._metrics import record_cold_silence_rejected
from sovyx.voice.health.contract import Combo, Diagnosis
from sovyx.voice.health.probe._classifier import _RMS_DB_NO_SIGNAL_CEILING

logger = get_logger(__name__)


# ── Open-error keyword sets ───────────────────────────────────────


# Keywords mapped to the ADR's open-error diagnoses. Matching is done
# case-insensitively against the exception message after classification
# attempts via the exception type.
#
# AUDCLNT_E_DEVICE_IN_USE / 0x8889000a / -2004287478 belongs to the
# busy family: another process (or our own voice-test session) is
# holding the endpoint in exclusive mode. Recovery is wait-and-retry
# or close the competing owner — NOT the §4.4.7 fail-over path
# (quarantining a busy device would falsely mark healthy hardware).
_DEVICE_BUSY_KEYWORDS = (
    "device unavailable",
    "busy",
    "exclusive",
    "in use",
    "audclnt_e_device_in_use",
    "0x8889000a",
    "-2004287478",
)
# Phase 6 / T6.3 — AUDCLNT_E_EXCLUSIVE_MODE_NOT_ALLOWED tokens. The
# endpoint declines exclusive mode for a non-busy reason: driver
# doesn't expose an exclusive endpoint, hardware doesn't support
# it, OR Windows surfaces the substring without an
# ``access denied`` companion (the GP-blocked variant lands in
# PERMISSION_DENIED via the higher-priority permission keywords).
#
# Hex 0x88890017 ↔ signed-decimal -2004287465 — sounddevice may
# surface either depending on its message-format version. We match
# both to stay resilient to format drift.
_EXCLUSIVE_MODE_NOT_AVAILABLE_KEYWORDS = (
    "audclnt_e_exclusive_mode_not_allowed",
    "exclusive mode not allowed",
    "exclusive_mode_not_allowed",
    "0x88890017",
    "-2004287465",
)
# Phase 6 / T6.4 — WASAPI buffer-size rejection. The format
# (rate / channels / sample_format) is fine; only the
# ``frames_per_buffer`` value doesn't match the driver's expected
# alignment or exceeds the device's max buffer. Cascade should
# retry with a different ``frames_per_buffer`` for the same combo.
#
# AUDCLNT_E_BUFFER_SIZE_NOT_ALIGNED = 0x88890019 (-2004287463)
# AUDCLNT_E_BUFFER_TOO_LARGE        = 0x88890011 (-2004287471)
# AUDCLNT_E_BUFFER_SIZE_ERROR       = 0x88890018 (-2004287464)
# Substring families also catch the spelled-out forms in
# sounddevice messages.
_INSUFFICIENT_BUFFER_SIZE_KEYWORDS = (
    "audclnt_e_buffer_size_not_aligned",
    "audclnt_e_buffer_too_large",
    "audclnt_e_buffer_size_error",
    "audclnt_e_buffer_size",
    "buffer size",
    "buffer_size",
    "0x88890019",
    "-2004287463",
    "0x88890011",
    "-2004287471",
    "0x88890018",
    "-2004287464",
)
_PERMISSION_KEYWORDS = ("permission", "denied", "access", "not authoriz")
# Phase 6 / T6.5 — sample-rate-specific subset of the FORMAT_MISMATCH
# keywords. When one of these matches AND ``combo.auto_convert`` is
# ``False``, the cascade can recover by re-running the same combo with
# ``auto_convert=True`` (PortAudio/WASAPI does the resample) — a path
# distinct from "try a different rate" which is what plain
# FORMAT_MISMATCH signals. The subset is deliberately strict: it only
# contains rate-only tokens so a "format" / "channels" message lands
# in FORMAT_MISMATCH instead.
_INVALID_SAMPLE_RATE_KEYWORDS = (
    "invalid sample rate",
    "invalid samplerate",
    "sample rate",
    "samplerate",
)
_FORMAT_MISMATCH_KEYWORDS = (
    "invalid sample rate",
    "invalid samplerate",
    "sample rate",
    "samplerate",
    "format",
    "channels",
    "invalid number of channels",
    "unsupported",
)
# Kernel-invalidated IAudioClient state — see ADR §4.4.7 + the
# forensic report in ``docs-internal/voice-capture-kernel-invalidated.md``.
# PortAudio surfaces this as ``paInvalidDevice`` (-9996) because
# ``IAudioClient::Initialize`` returns one of the AUDCLNT_E_DEVICE_*
# HRESULTs, and sounddevice re-wraps that as "Invalid device". The PnP
# layer still reports the endpoint as healthy (ConfigManagerErrorCode=0),
# so this is *not* a hot-unplug — it's a stuck audio engine that no
# user-mode call can revive. Cure is physical (replug / reboot). We
# match text, hex and signed-decimal forms so we're resilient to
# sounddevice message format drift.
_KERNEL_INVALIDATED_KEYWORDS = (
    "invalid device",
    "paerrorcode -9996",
    "pa_invalid_device",
    "audclnt_e_device_invalidated",
    "0x88890004",
    "-2004287484",
)


# ── Furo W-1 feature flag ─────────────────────────────────────────


_COLD_STRICT_VALIDATION_ENABLED = _VoiceTuning().probe_cold_strict_validation_enabled
"""Voice Windows Paranoid Mission Furo W-1 — gate the strict-RMS path
in :func:`_diagnose_cold`.

When ``False`` (legacy v0.23.x behaviour, foundation-phase default in
v0.24.0) the cold-probe accepts any combo with at least one callback
even when ``rms_db < _RMS_DB_NO_SIGNAL_CEILING`` — which is exactly
what lets a Microsoft Voice Clarity APO destroy the signal upstream of
PortAudio yet have the silent combo persist as the winning ComboStore
entry, replicating the failure deterministically on every boot.

When ``True`` (default-flip planned for v0.25.0) silent cold probes
return :attr:`Diagnosis.NO_SIGNAL` so the cascade advances to the next
combo and the silent winner never persists.

Lenient mode (``False``) still emits a structured
``voice.probe.cold_silence_rejected{mode=lenient_passthrough}`` event
so operators can calibrate the rejection rate before flipping the flag.
"""


_STREAM_OPEN_TIMEOUT_THRESHOLD_MS = _VoiceTuning().probe_stream_open_timeout_threshold_ms
"""T6.2 — threshold for distinguishing :attr:`Diagnosis.STREAM_OPEN_TIMEOUT`
from :attr:`Diagnosis.NO_SIGNAL` when ``callbacks_fired == 0``.

Sourced from :attr:`VoiceTuningConfig.probe_stream_open_timeout_threshold_ms`
at import time so ``SOVYX_TUNING__VOICE__PROBE_STREAM_OPEN_TIMEOUT_THRESHOLD_MS``
overrides without code changes (anti-pattern #17). Default 5 000 ms
matches the master mission spec — short enough that a wedged USB
driver surfaces in one cascade attempt; long enough that the default
1.5 s cold probe doesn't false-positive."""


# ── Open-error classification ─────────────────────────────────────


def _classify_open_error(
    exc: BaseException,
    combo: Combo | None = None,
) -> Diagnosis:
    """Map a PortAudio / OS exception to a :class:`Diagnosis`.

    Exact string matching is fragile; we match keyword sets instead and
    fall back to :attr:`Diagnosis.DRIVER_ERROR` for anything we don't
    recognise (still actionable — the cascade treats DRIVER_ERROR as a
    retry-with-different-combo signal).

    Priority order (first match wins):

    1. ``PERMISSION_DENIED`` — captures the GP-blocked exclusive-mode
       case (``access denied`` companion to AUDCLNT_E_EXCLUSIVE_MODE_NOT_ALLOWED)
       BEFORE the new T6.3 EXCLUSIVE_MODE_NOT_AVAILABLE check, so the
       Windows admin / Group Policy remediation path stays distinct.
    2. ``EXCLUSIVE_MODE_NOT_AVAILABLE`` (T6.3) — standalone
       ``audclnt_e_exclusive_mode_not_allowed`` without permission
       companion. The ``"exclusive"`` substring in DEVICE_BUSY would
       otherwise catch it; checking the more-specific set first
       routes correctly to the permanent-not-supported diagnosis
       instead of the wait-and-retry one.
    3. ``INSUFFICIENT_BUFFER_SIZE`` (T6.4) — buffer-size-specific
       AUDCLNT_E_BUFFER_* errors. The ``"buffer size"`` /
       ``"buffer_size"`` substrings overlap with the
       FORMAT_MISMATCH set's broad ``"format"`` token in some
       compound messages, so the more-specific buffer check runs
       BEFORE format. Cascade retry with a different
       ``frames_per_buffer`` value is the right path.
    4. ``DEVICE_BUSY`` — ``audclnt_e_device_in_use`` etc. Wait + retry
       is meaningful here (another app might release the lock).
    5. ``INVALID_SAMPLE_RATE_NO_AUTO_CONVERT`` (T6.5) — rate-only
       error AND ``combo.auto_convert is False``. Cascade should
       enable auto_convert and retry the same rate, OR pick a
       device-native rate. Distinct from FORMAT_MISMATCH which
       signals "try a different rate" without the auto_convert
       hint. Requires the optional ``combo`` argument; legacy
       callers (combo=None) still land in FORMAT_MISMATCH for
       backwards compatibility.
    6. ``FORMAT_MISMATCH`` — invalid sample rate / channels / format
       (the broader catch-all when T6.5 doesn't apply).
    7. ``KERNEL_INVALIDATED`` — checked AFTER format-mismatch so an
       ``invalid sample rate`` message (containing ``"invalid"``)
       doesn't false-positive as kernel invalidation.

    The ``_KERNEL_INVALIDATED_KEYWORDS`` strings are narrower than
    their format counterparts; none of them overlap with the
    format-mismatch tokens, but the priority still matters if a
    future message gains a compound phrase.

    Args:
        exc: The exception raised by PortAudio open / start.
        combo: The combo being attempted, if known. Required to
            distinguish T6.5 INVALID_SAMPLE_RATE_NO_AUTO_CONVERT
            from generic FORMAT_MISMATCH; unsupplied callers fall
            through to FORMAT_MISMATCH on rate-only errors.
    """
    msg = str(exc).lower()
    if any(keyword in msg for keyword in _PERMISSION_KEYWORDS):
        return Diagnosis.PERMISSION_DENIED
    if any(keyword in msg for keyword in _EXCLUSIVE_MODE_NOT_AVAILABLE_KEYWORDS):
        return Diagnosis.EXCLUSIVE_MODE_NOT_AVAILABLE
    if any(keyword in msg for keyword in _INSUFFICIENT_BUFFER_SIZE_KEYWORDS):
        return Diagnosis.INSUFFICIENT_BUFFER_SIZE
    if any(keyword in msg for keyword in _DEVICE_BUSY_KEYWORDS):
        return Diagnosis.DEVICE_BUSY
    # T6.5 — rate-only error WITH a known combo whose auto_convert is
    # False routes to the new diagnosis. The cascade can fix this
    # without changing rate (just enable conversion). When combo is
    # absent (legacy call) or auto_convert is True, fall through to
    # FORMAT_MISMATCH which is the broader retry-different-rate path.
    if (
        combo is not None
        and not combo.auto_convert
        and any(keyword in msg for keyword in _INVALID_SAMPLE_RATE_KEYWORDS)
    ):
        return Diagnosis.INVALID_SAMPLE_RATE_NO_AUTO_CONVERT
    if any(keyword in msg for keyword in _FORMAT_MISMATCH_KEYWORDS):
        return Diagnosis.FORMAT_MISMATCH
    if any(keyword in msg for keyword in _KERNEL_INVALIDATED_KEYWORDS):
        return Diagnosis.KERNEL_INVALIDATED
    return Diagnosis.DRIVER_ERROR


# ── Cold-mode diagnosis ───────────────────────────────────────────


def _diagnose_cold(
    *,
    callbacks_fired: int,
    rms_db: float,
    combo: Combo,
    vad_max_prob: float | None = None,
    elapsed_ms: int | None = None,
) -> Diagnosis:
    """Cold-mode diagnosis (ADR §4.3 — amended by Voice Windows
    Paranoid Mission Furo W-1, Phase 6 / T6.2).

    The cold probe runs without the VAD attached, so the diagnosis is a
    function of how many audio callbacks the driver delivered and the
    energy of the captured signal:

    * T6.2: ``callbacks_fired == 0`` AND ``elapsed_ms ≥
      _STREAM_OPEN_TIMEOUT_THRESHOLD_MS`` (default 5 000 ms) →
      :attr:`Diagnosis.STREAM_OPEN_TIMEOUT`. The driver accepted the
      stream + start but never delivered audio in a meaningful
      window. Distinguishes from NO_SIGNAL (where the probe didn't
      wait long enough to claim timeout).
    * ``callbacks_fired == 0`` (default short probe) → :attr:`Diagnosis.NO_SIGNAL`.
    * silent (``rms_db < _RMS_DB_NO_SIGNAL_CEILING``):

      * strict mode (post-fix, ``_COLD_STRICT_VALIDATION_ENABLED=True``)
        → :attr:`Diagnosis.NO_SIGNAL` and emit
        ``voice.probe.cold_silence_rejected{mode=strict_reject}``.
      * lenient mode (legacy v0.23.x, foundation-phase default in
        v0.24.0) → :attr:`Diagnosis.HEALTHY` (preserves prior
        acceptance) and emit
        ``voice.probe.cold_silence_rejected{mode=lenient_passthrough}``
        for telemetry-only calibration.

    * any other case → :attr:`Diagnosis.HEALTHY`.

    The ``vad_max_prob`` keyword is accepted but ignored on the cold
    path — the cold probe never runs the VAD (probe.py call site
    explicitly skips it). The kwarg keeps the signature symmetric with
    :func:`_diagnose_warm` so future refactoring can collapse the
    branches without touching call sites.

    The ``elapsed_ms`` keyword (T6.2) defaults to ``None`` for
    backwards compatibility — pre-T6.2 callers that don't pass it
    fall through to the legacy ``NO_SIGNAL`` classification. Production
    callers in :mod:`sovyx.voice.health.probe._dispatch` pass the
    actual probe duration.

    Reuses ``probe_rms_db_no_signal`` (default −70 dBFS) — a level that
    is 4 LSB at int16, well below the ambient room floor (−55 to −45
    dBFS on typical desktops).
    """
    if callbacks_fired == 0:
        if elapsed_ms is not None and elapsed_ms >= _STREAM_OPEN_TIMEOUT_THRESHOLD_MS:
            return Diagnosis.STREAM_OPEN_TIMEOUT
        return Diagnosis.NO_SIGNAL

    if rms_db >= _RMS_DB_NO_SIGNAL_CEILING:
        return Diagnosis.HEALTHY

    # Silent cold probe — Voice Clarity-style upstream destruction
    # leaves callbacks firing while PCM is exact zero. Strict mode
    # rejects; lenient mode keeps legacy acceptance for one minor cycle
    # but still surfaces telemetry so operators can validate the rate
    # before flipping the flag.
    if _COLD_STRICT_VALIDATION_ENABLED:
        logger.warning(
            "voice.probe.cold_silence_rejected",
            mode="strict_reject",
            rms_db=rms_db,
            callbacks_fired=callbacks_fired,
            host_api=combo.host_api,
            sample_rate=combo.sample_rate,
            channels=combo.channels,
            sample_format=combo.sample_format,
            exclusive=combo.exclusive,
        )
        record_cold_silence_rejected(mode="strict_reject", host_api=combo.host_api)
        return Diagnosis.NO_SIGNAL

    logger.warning(
        "voice.probe.cold_silence_rejected",
        mode="lenient_passthrough",
        rms_db=rms_db,
        callbacks_fired=callbacks_fired,
        host_api=combo.host_api,
        sample_rate=combo.sample_rate,
        channels=combo.channels,
        sample_format=combo.sample_format,
        exclusive=combo.exclusive,
    )
    record_cold_silence_rejected(mode="lenient_passthrough", host_api=combo.host_api)
    return Diagnosis.HEALTHY


__all__ = [
    "_COLD_STRICT_VALIDATION_ENABLED",
    "_DEVICE_BUSY_KEYWORDS",
    "_EXCLUSIVE_MODE_NOT_AVAILABLE_KEYWORDS",
    "_FORMAT_MISMATCH_KEYWORDS",
    "_INSUFFICIENT_BUFFER_SIZE_KEYWORDS",
    "_INVALID_SAMPLE_RATE_KEYWORDS",
    "_KERNEL_INVALIDATED_KEYWORDS",
    "_PERMISSION_KEYWORDS",
    "_classify_open_error",
    "_diagnose_cold",
]
