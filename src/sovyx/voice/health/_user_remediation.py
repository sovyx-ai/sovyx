"""Diagnosis → user-facing remediation hints.

Phase 6 / T6.12 — single source of truth for the mapping
:class:`Diagnosis` value → operator-actionable hint string. Consumed
by:

* :mod:`sovyx.voice.health.cascade._executor` — emits
  ``voice_cascade_user_actionable`` at exhaustion when the
  ``diagnosis_histogram`` (T6.11) is homogeneous (exactly one
  diagnosis key). Homogeneous failures are the high-confidence
  case: every cascade attempt died the same way, so the
  remediation applies unambiguously.
* :mod:`sovyx.dashboard.routes.voice` ``GET /api/voice/service-health``
  — surfaces ``user_remediation`` when the last stored diagnosis is
  in the map. Best-effort hint for dashboard banner rendering;
  ``None`` when the diagnosis is unknown / not user-actionable.

Design decisions:

* **Single source of truth.** The mapping lives here, NOT inlined
  at emission sites. Adding a new ``Diagnosis`` value forces
  exactly one update site (this module) instead of N scattered
  ``if/elif`` chains.
* **String-keyed (not enum-keyed).** Callers pass the wire-form
  ``.value`` string. Avoids importing the enum at every call-site
  and keeps the module a leaf — no circular-import risk.
* **Operator-actionable, not technical.** Hints describe what the
  USER does, not what the daemon detected. The technical detail
  is already in the structured log fields; this is the friendly
  layer.
* **None for non-actionable.** ``HEALTHY``, ``UNKNOWN``, and the
  L2.5 mixer-sanity diagnoses (``MIXER_*``) return ``None`` —
  either the cause is internal to sovyx (mixer auto-heals) or the
  diagnosis is too vague to route a confident hint.
"""

from __future__ import annotations

_REMEDIATION_BY_DIAGNOSIS: dict[str, str] = {
    "device_busy": (
        "Another application has exclusive access to your microphone. "
        "Close the conflicting app (typical culprits: Discord, Microsoft "
        "Teams, Zoom, Google Meet). On Windows, check the volume mixer "
        "for active capture sessions; on Linux, run "
        "`fuser -v /dev/snd/*` to identify the holding process."
    ),
    "exclusive_mode_not_available": (
        "The audio device fundamentally does not permit exclusive-mode "
        "capture (driver doesn't expose an exclusive endpoint, or "
        "hardware doesn't support it). Sovyx will fall back to shared "
        "mode automatically — no operator action needed for normal use. "
        "If shared mode also fails, try a different microphone OR "
        "update the audio driver to a version that exposes exclusive "
        "endpoints."
    ),
    "insufficient_buffer_size": (
        "The audio driver rejected the requested buffer size (the "
        "audio format itself was fine — only the buffer alignment / "
        "size violates the driver's constraints). Sovyx will retry "
        "with a different buffer size automatically. If the cascade "
        "still fails after multiple attempts, the driver may have an "
        "unusually strict buffer-period alignment; update the driver "
        "or report the model + firmware to your vendor."
    ),
    "permission_denied": (
        "The operating system blocked microphone access for sovyx. "
        "Grant permission in Settings → Privacy → Microphone (Windows / "
        "macOS) or check the Flatpak/Snap portal permissions on Linux."
    ),
    "muted": (
        "The microphone is muted at the OS / hardware level. Check the "
        "physical mute switch on the device, then the OS volume mixer "
        "to confirm the capture stream isn't muted."
    ),
    "no_signal": (
        "No audio is reaching the microphone. Confirm the device is "
        "plugged in, selected as the default capture device, and that "
        "the physical mute switch is off. On USB devices, try a "
        "different port to rule out hub power issues."
    ),
    "low_signal": (
        "The microphone signal is too quiet for reliable transcription. "
        "Increase the input level in OS sound settings, move closer to "
        "the microphone, or check that automatic gain control isn't "
        "aggressively attenuating the input."
    ),
    "apo_degraded": (
        "An audio enhancement (Voice Clarity APO on Windows, "
        "module-echo-cancel on Linux, Voice Isolation on macOS) is "
        "destroying the microphone signal. Disable per-device audio "
        "enhancements in your OS sound settings — sovyx's bypass "
        "strategies could not recover the signal automatically on "
        "this hardware."
    ),
    "vad_insensitive": (
        "Voice activity detection cannot reliably hear speech on this "
        "input. Likely causes: heavy background noise, a microphone "
        "with very low sensitivity, or audio enhancement filtering. "
        "Try a different microphone or disable noise suppression."
    ),
    "format_mismatch": (
        "The microphone does not support the formats sovyx expects "
        "(48 kHz / 16-bit / mono). This is rare on modern hardware; "
        "try selecting a different default input device, or replug "
        "the device to refresh its supported-formats list."
    ),
    "invalid_sample_rate_no_auto_convert": (
        "The microphone is locked at a sample rate sovyx didn't "
        "request, and software resampling is disabled for this combo. "
        "Sovyx will retry with auto-conversion enabled — no operator "
        "action needed for normal use. If the cascade still fails, "
        "the device's native rate is incompatible with the pipeline; "
        "check the device's supported-formats list (Windows Sound "
        "Settings → Properties → Advanced; Linux ``arecord -L``) and "
        "select a device that exposes 16 kHz or 48 kHz natively."
    ),
    "driver_error": (
        "The audio driver refused to open the device. Try replugging "
        "the device, or update the driver via Device Manager (Windows) "
        "/ your distribution's package manager (Linux) / System "
        "Settings (macOS)."
    ),
    "kernel_invalidated": (
        "The audio driver is in a wedged state that no user-mode "
        "recovery can clear. Physically unplug and replug the device, "
        "or reboot the machine if the issue persists. This is typically "
        "caused by a USB resource timeout or a mid-session driver "
        "update."
    ),
    "stream_open_timeout": (
        "The audio driver accepted the stream but never started "
        "delivering audio (5+ seconds elapsed without a single "
        "callback). The driver is stuck mid-init — typically a USB "
        "resource timeout or kernel-side IAudioClient wedge. Cure: "
        "physically unplug and replug the device. If the symptom "
        "recurs after replug, reboot to clear the kernel state."
    ),
}


def diagnosis_user_remediation(diagnosis_value: str) -> str | None:
    """Return the user-facing remediation hint for a diagnosis, or ``None``.

    Args:
        diagnosis_value: Wire-form :class:`Diagnosis` value
            (``"healthy"``, ``"device_busy"``, ...). Pass the
            ``.value`` of a Diagnosis instance, NOT the enum itself.

    Returns:
        Operator-actionable hint string when the diagnosis maps to a
        known remediation, ``None`` otherwise. ``None`` covers:

        * ``"healthy"`` — nothing to remediate.
        * ``"unknown"`` — too vague to route a confident hint.
        * The ``"mixer_*"`` family — L2.5 mixer-sanity is
          internally auto-healed; surfacing a hint to the user
          before sovyx finishes its own retry would be premature.
        * Any future diagnosis that lands without a paired entry in
          :data:`_REMEDIATION_BY_DIAGNOSIS`.
    """
    return _REMEDIATION_BY_DIAGNOSIS.get(diagnosis_value)


def homogeneous_diagnosis_remediation(
    histogram: dict[str, int],
) -> tuple[str, str] | None:
    """Return ``(diagnosis, remediation)`` when the histogram is homogeneous.

    A homogeneous histogram has exactly one diagnosis key (every
    attempt died the same way). The :func:`diagnosis_user_remediation`
    map then drives a high-confidence user-facing hint. Heterogeneous
    histograms (multiple keys) — or histograms whose single
    diagnosis isn't in the map — return ``None``.

    Args:
        histogram: ``{diagnosis_value: count}`` dict produced by
            :func:`sovyx.voice.health.cascade._executor._compute_diagnosis_histogram`.

    Returns:
        ``(diagnosis_value, remediation_text)`` tuple when:

        * The histogram has exactly one key.
        * That key is in :data:`_REMEDIATION_BY_DIAGNOSIS`.

        ``None`` otherwise — caller emits the routine non-actionable
        path (existing ``voice_cascade_exhausted`` log).
    """
    if len(histogram) != 1:
        return None
    diagnosis_value = next(iter(histogram))
    remediation = diagnosis_user_remediation(diagnosis_value)
    if remediation is None:
        return None
    return diagnosis_value, remediation


__all__ = [
    "diagnosis_user_remediation",
    "homogeneous_diagnosis_remediation",
]
