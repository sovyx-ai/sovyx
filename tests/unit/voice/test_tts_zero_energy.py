"""Tests for the shared TTS zero-energy validation primitives.

The module :mod:`sovyx.voice._tts_zero_energy` extracts the canonical
RMS-dBFS gate originally local to ``tts_kokoro``. These tests pin the
public surface (``TTS_RMS_FLOOR_DBFS`` + ``compute_rms_dbfs``) so a
future Piper-side wire-up (mission Phase 1 / T1.36) lands against a
fully-tested foundation.
"""

from __future__ import annotations

import numpy as np

from sovyx.voice._tts_zero_energy import TTS_RMS_FLOOR_DBFS, compute_rms_dbfs


class TestComputeRMSDbfs:
    """Pure-function RMS dBFS computation."""

    def test_empty_array_returns_neg_inf(self) -> None:
        assert compute_rms_dbfs(np.zeros(0, dtype=np.int16)) == float("-inf")

    def test_all_zero_returns_neg_inf(self) -> None:
        assert compute_rms_dbfs(np.zeros(1000, dtype=np.int16)) == float("-inf")

    def test_full_scale_sine_near_zero_dbfs(self) -> None:
        """A full-scale int16 sine produces RMS near 0 dBFS (within ~3 dB)."""
        t = np.arange(8000, dtype=np.float64) / 8000
        sine = (32000 * np.sin(2 * np.pi * 440 * t)).astype(np.int16)
        rms = compute_rms_dbfs(sine)
        # Sine RMS = peak / sqrt(2) → ~ -3 dBFS for full-scale.
        assert rms > -10.0
        assert rms < 0.0

    def test_quiet_signal_well_below_floor(self) -> None:
        """A signal at ~ -80 dBFS is well below the -60 floor."""
        t = np.arange(8000, dtype=np.float64) / 8000
        quiet_sine = (3 * np.sin(2 * np.pi * 440 * t)).astype(np.int16)
        rms = compute_rms_dbfs(quiet_sine)
        assert rms < TTS_RMS_FLOOR_DBFS

    def test_object_without_size_returns_neg_inf(self) -> None:
        """Defensive — non-array input doesn't crash, returns silence."""
        assert compute_rms_dbfs(None) == float("-inf")

    def test_bytes_fallback_when_size_attr_missing(self) -> None:
        """``bytes`` has no ``.size`` attribute — fall back to ``len()``-=0 → -inf."""
        # Empty bytes have no size attr → returns -inf (treated as empty).
        assert compute_rms_dbfs(b"") == float("-inf")

    def test_size_method_falls_back_gracefully(self) -> None:
        """An object whose ``size`` is a method (not an int) is treated as empty."""

        class MethodSize:
            def size(self) -> int:  # ``size`` as a method, not an int property
                return 100

        # ``int(method)`` raises TypeError → falls into the bytes/list isinstance
        # branch → not a bytes-like → returns 0 → caller sees empty → -inf.
        assert compute_rms_dbfs(MethodSize()) == float("-inf")


class TestTTSRMSFloorDbfs:
    def test_floor_value(self) -> None:
        """Public-surface tuning constant — bumps must be deliberate."""
        assert TTS_RMS_FLOOR_DBFS == -60.0
