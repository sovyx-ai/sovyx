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
_PERMISSION_KEYWORDS = ("permission", "denied", "access", "not authoriz")
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


# ── Open-error classification ─────────────────────────────────────


def _classify_open_error(exc: BaseException) -> Diagnosis:
    """Map a PortAudio / OS exception to a :class:`Diagnosis`.

    Exact string matching is fragile; we match keyword sets instead and
    fall back to :attr:`Diagnosis.DRIVER_ERROR` for anything we don't
    recognise (still actionable — the cascade treats DRIVER_ERROR as a
    retry-with-different-combo signal).

    Ordering rationale — kernel_invalidated checked *after* the
    format-mismatch set so an "invalid sample rate" message (which
    contains the token ``"invalid"``) doesn't false-positive as a
    kernel invalidation. The ``_KERNEL_INVALIDATED_KEYWORDS`` strings
    are narrower than their format counterparts; none of them overlap
    with the format-mismatch tokens, but the priority still matters
    if a future message gains a compound phrase.
    """
    msg = str(exc).lower()
    if any(keyword in msg for keyword in _PERMISSION_KEYWORDS):
        return Diagnosis.PERMISSION_DENIED
    if any(keyword in msg for keyword in _DEVICE_BUSY_KEYWORDS):
        return Diagnosis.DEVICE_BUSY
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
) -> Diagnosis:
    """Cold-mode diagnosis (ADR §4.3 — amended by Voice Windows
    Paranoid Mission Furo W-1).

    The cold probe runs without the VAD attached, so the diagnosis is a
    function of how many audio callbacks the driver delivered and the
    energy of the captured signal:

    * ``callbacks_fired == 0``        →  :attr:`Diagnosis.NO_SIGNAL`
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

    Reuses ``probe_rms_db_no_signal`` (default −70 dBFS) — a level that
    is 4 LSB at int16, well below the ambient room floor (−55 to −45
    dBFS on typical desktops).
    """
    if callbacks_fired == 0:
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
    "_FORMAT_MISMATCH_KEYWORDS",
    "_KERNEL_INVALIDATED_KEYWORDS",
    "_PERMISSION_KEYWORDS",
    "_classify_open_error",
    "_diagnose_cold",
]
