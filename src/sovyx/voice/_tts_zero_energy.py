"""Shared TTS output-energy validation primitives (T2 / Ring 5).

Defines the canonical "audio is gone" RMS threshold + the int16-PCM
RMS-dBFS computation used by every TTS engine to detect zero-energy
synthesis (corrupt voice file, ONNX session degeneration, runtime
producing zeros). Originally local to :mod:`sovyx.voice.tts_kokoro`;
extracted here so :mod:`sovyx.voice.tts_piper` (mission Phase 1 /
T1.36) and any future TTS backend can apply identical validation
without copy-paste drift.

Public surface:

* :data:`TTS_RMS_FLOOR_DBFS` — the threshold below which output is
  perceptually silent. Engines compare ``compute_rms_dbfs(audio)``
  against this value; a sub-threshold reading sets the
  :class:`~sovyx.voice.tts_piper.AudioChunk.synthesis_health` field
  to ``"zero_energy"`` so the orchestrator can trigger fallback (or
  text-only mode) instead of pushing silence to the user.
* :func:`compute_rms_dbfs` — pure function, fully unit-testable.

Both names are also re-exported from :mod:`sovyx.voice.tts_kokoro`
under their underscore-prefixed legacy aliases (``_TTS_RMS_FLOOR_DBFS``
+ ``_compute_rms_dbfs``) so the existing test suite at
``tests/unit/voice/test_tts_kokoro.py`` keeps working without any
import-path migration.
"""

from __future__ import annotations

import math

__all__ = ["TTS_RMS_FLOOR_DBFS", "compute_rms_dbfs"]


TTS_RMS_FLOOR_DBFS = -60.0
"""Below this RMS the output is perceptually silent. -60 dBFS is the
canonical "audio is gone" threshold from EBU R128 / ITU-R BS.1770
loudness measurement and matches the noise floor of consumer
playback devices — anything quieter is indistinguishable from
silence in normal listening conditions. RMS is computed on the
int16 PCM after saturation clip, so the value reflects what the
playback path will actually emit, not the pre-clip float buffer."""


def compute_rms_dbfs(samples: object) -> float:
    """Compute peak-normalised RMS in dBFS for an int16 PCM buffer.

    Returns ``-inf`` for an empty or all-zero buffer (canonical
    silence representation in dBFS-space). Pure function — fully
    unit-testable in isolation. Callers feed this value into the
    :data:`TTS_RMS_FLOOR_DBFS` gate; sub-threshold readings indicate
    a structural synthesis failure (corrupt voice file, ONNX session
    degeneration) the orchestrator must fall back from instead of
    pushing silence to the user.

    Args:
        samples: int16 PCM buffer. Anything with a ``size`` attribute
            (numpy ndarray) is the expected shape; ``None`` and
            other shapeless objects return ``-inf`` (interpreted as
            "no signal").

    Returns:
        RMS in dBFS, or ``-inf`` for empty / all-zero / invalid input.
        Full-scale (``2**15``) → ``0.0`` dBFS.
    """
    import numpy as np

    if not hasattr(samples, "size") or _safe_size(samples) == 0:
        return float("-inf")
    arr = np.asarray(samples, dtype=np.float64)
    rms = float(np.sqrt(np.mean(arr * arr)))
    if rms <= 0.0:
        return float("-inf")
    # int16 full-scale = 32768. Normalise so 0 dBFS = full-scale sine.
    normalised = rms / 32768.0
    if normalised <= 0.0:
        return float("-inf")
    return 20.0 * math.log10(normalised)


def _safe_size(samples: object) -> int:
    """Return ``len(samples.size)`` -ish guarded against odd duck-types.

    The original :func:`compute_rms_dbfs` checked ``samples.size == 0``
    which raises ``TypeError`` if ``size`` is a method (sized=array
    duck-types do this). Read it via ``int(...)`` with a fallback so a
    misshaped input returns "empty" rather than crashing the synthesise
    path mid-frame.
    """
    size = getattr(samples, "size", 0)
    try:
        return int(size)
    except (TypeError, ValueError):
        # ``samples.size`` was a method or otherwise non-int; treat
        # the input as empty so the caller falls into the -inf branch
        # rather than raising mid-synthesis. The Sized protocol fall-
        # back is intentionally narrow — we don't want to interpret
        # arbitrary objects.
        if isinstance(samples, (bytes, bytearray, memoryview, list, tuple)):
            return len(samples)
        return 0
