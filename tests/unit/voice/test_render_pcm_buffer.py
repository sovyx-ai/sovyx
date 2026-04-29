"""Tests for :mod:`sovyx.voice._render_pcm_buffer` — Phase 4 / T4.4.b.

Foundation tests for the thread-safe ring buffer that bridges the
TTS playback thread (producer) and the FrameNormalizer's AEC
reference read site (consumer). Coverage:

* Construction bounds — ``buffer_seconds`` rejected outside
  ``[0.1, 30.0]``.
* :class:`RenderPcmProvider` Protocol compliance.
* :meth:`feed` — int16 mono / int16 stereo (downmix) / float32
  mono input formats; resample to 16 kHz at common TTS rates;
  empty input no-op; bad dtype raises.
* :meth:`get_aligned_window` — silence on empty buffer; partial
  fill is zero-padded at the start; full ring returns most
  recent samples; oversize chunk truncates older content;
  ring wraparound preserved.
* :meth:`reset` — clears state; subsequent reads return silence.
* Concurrency — feed from one thread + get from another never
  raises and never returns torn samples.
"""

from __future__ import annotations

import threading
import time

import numpy as np
import pytest

from sovyx.voice._aec import RenderPcmProvider
from sovyx.voice._render_pcm_buffer import RenderPcmBuffer

# ── Construction ─────────────────────────────────────────────────────────


class TestConstruction:
    def test_default_capacity_is_two_seconds(self) -> None:
        buf = RenderPcmBuffer()
        # 2 s @ 16 kHz = 32 000 samples.
        assert buf.capacity_samples == 32_000

    def test_custom_buffer_seconds_sizes_capacity(self) -> None:
        buf = RenderPcmBuffer(buffer_seconds=1.0)
        assert buf.capacity_samples == 16_000

    def test_rejects_buffer_seconds_below_floor(self) -> None:
        with pytest.raises(ValueError, match=r"\[0.1, 30.0\]"):
            RenderPcmBuffer(buffer_seconds=0.05)

    def test_rejects_buffer_seconds_above_ceiling(self) -> None:
        with pytest.raises(ValueError, match=r"\[0.1, 30.0\]"):
            RenderPcmBuffer(buffer_seconds=60.0)

    def test_initial_filled_is_zero(self) -> None:
        buf = RenderPcmBuffer()
        assert buf.filled_samples == 0

    def test_implements_render_pcm_provider_protocol(self) -> None:
        buf = RenderPcmBuffer()
        assert isinstance(buf, RenderPcmProvider)


# ── feed: input formats + resampling ─────────────────────────────────────


class TestFeed:
    def test_feed_int16_mono_at_target_rate(self) -> None:
        buf = RenderPcmBuffer()
        pcm = np.full(160, 1000, dtype=np.int16)
        buf.feed(pcm, sample_rate=16_000)
        assert buf.filled_samples == 160

    def test_feed_int16_stereo_downmixes(self) -> None:
        buf = RenderPcmBuffer()
        # Stereo with constant 1000 in L, 2000 in R → mean = 1500.
        pcm = np.column_stack(
            [
                np.full(160, 1000, dtype=np.int16),
                np.full(160, 2000, dtype=np.int16),
            ],
        )
        buf.feed(pcm, sample_rate=16_000)
        out = buf.get_aligned_window(160)
        # Allow ±1 for int rounding through the float resample path.
        assert np.all(np.abs(out.astype(np.int32) - 1500) <= 1)

    def test_feed_float32_mono(self) -> None:
        buf = RenderPcmBuffer()
        pcm = np.full(160, 0.5, dtype=np.float32)
        buf.feed(pcm, sample_rate=16_000)
        assert buf.filled_samples == 160
        out = buf.get_aligned_window(160)
        # 0.5 * 32768 = 16384.
        assert np.all(np.abs(out.astype(np.int32) - 16_384) <= 1)

    def test_feed_resamples_22050_to_16000(self) -> None:
        # Piper default rate is 22050; verify the buffer accepts it
        # and produces 16000-rate output.
        buf = RenderPcmBuffer()
        # 1 second of constant signal at 22050 Hz.
        pcm = np.full(22_050, 1000, dtype=np.int16)
        buf.feed(pcm, sample_rate=22_050)
        # 22050 → 16000 ratio = 16/22.05; expected ~16000 samples.
        # Filter transients at the boundary cause minor variance;
        # accept ±50 samples.
        assert abs(buf.filled_samples - 16_000) <= 50

    def test_feed_resamples_24000_to_16000(self) -> None:
        # Kokoro default rate is 24000 Hz.
        buf = RenderPcmBuffer()
        pcm = np.full(24_000, 1000, dtype=np.int16)
        buf.feed(pcm, sample_rate=24_000)
        assert abs(buf.filled_samples - 16_000) <= 50

    def test_feed_empty_is_noop(self) -> None:
        buf = RenderPcmBuffer()
        buf.feed(np.array([], dtype=np.int16), sample_rate=16_000)
        assert buf.filled_samples == 0

    def test_feed_rejects_bad_dtype(self) -> None:
        buf = RenderPcmBuffer()
        pcm = np.zeros(160, dtype=np.int32)
        with pytest.raises(ValueError, match="dtype"):
            buf.feed(pcm, sample_rate=16_000)

    def test_feed_rejects_bad_sample_rate(self) -> None:
        buf = RenderPcmBuffer()
        with pytest.raises(ValueError, match="sample_rate"):
            buf.feed(np.zeros(160, dtype=np.int16), sample_rate=0)

    def test_feed_rejects_3d_input(self) -> None:
        buf = RenderPcmBuffer()
        pcm = np.zeros((10, 20, 2), dtype=np.int16)
        with pytest.raises(ValueError, match="ndim"):
            buf.feed(pcm, sample_rate=16_000)


# ── get_aligned_window: empty / partial / full ───────────────────────────


class TestGetAlignedWindow:
    def test_empty_buffer_returns_zeros(self) -> None:
        buf = RenderPcmBuffer()
        out = buf.get_aligned_window(512)
        assert out.shape == (512,)
        assert out.dtype == np.int16
        assert np.all(out == 0)

    def test_partial_fill_zero_pads_at_start(self) -> None:
        buf = RenderPcmBuffer()
        # Feed 100 samples of value 5000.
        buf.feed(np.full(100, 5000, dtype=np.int16), sample_rate=16_000)
        out = buf.get_aligned_window(512)
        # Last 100 samples = 5000; first 412 = zeros.
        assert np.all(out[:412] == 0)
        assert np.all(out[412:] == 5000)

    def test_returns_most_recent_n_samples(self) -> None:
        buf = RenderPcmBuffer()
        # Sequential feed: 0..511, then 512..1023.
        first = np.arange(512, dtype=np.int16)
        second = np.arange(512, 1024, dtype=np.int16)
        buf.feed(first, sample_rate=16_000)
        buf.feed(second, sample_rate=16_000)
        # The 512 most recent are second.
        out = buf.get_aligned_window(512)
        np.testing.assert_array_equal(out, second)

    def test_oversize_chunk_truncates_to_capacity(self) -> None:
        buf = RenderPcmBuffer(buffer_seconds=0.1)  # capacity = 1600
        big = np.arange(5000, dtype=np.int16)
        buf.feed(big, sample_rate=16_000)
        # Buffer holds the trailing 1600 samples (= 3400..4999).
        assert buf.filled_samples == 1600
        out = buf.get_aligned_window(1600)
        np.testing.assert_array_equal(out, big[-1600:])

    def test_ring_wraparound_returns_correct_order(self) -> None:
        # Capacity 1600 (minimum allowed); feed 1200 then 800 —
        # second feed wraps the ring across the boundary.
        buf = RenderPcmBuffer(buffer_seconds=0.1)  # cap = 1600
        a = np.arange(1200, dtype=np.int16)
        b = np.arange(1200, 2000, dtype=np.int16)
        buf.feed(a, sample_rate=16_000)
        buf.feed(b, sample_rate=16_000)
        # Total fed 2000; ring holds last 1600 (= 400..1999).
        out = buf.get_aligned_window(1600)
        expected = np.arange(400, 2000, dtype=np.int16)
        np.testing.assert_array_equal(out, expected)

    def test_rejects_non_positive_window(self) -> None:
        buf = RenderPcmBuffer()
        with pytest.raises(ValueError, match="positive"):
            buf.get_aligned_window(0)
        with pytest.raises(ValueError, match="positive"):
            buf.get_aligned_window(-1)

    def test_rejects_window_larger_than_capacity(self) -> None:
        buf = RenderPcmBuffer(buffer_seconds=0.1)  # cap 1600
        with pytest.raises(ValueError, match="exceeds ring capacity"):
            buf.get_aligned_window(2000)


# ── reset ────────────────────────────────────────────────────────────────


class TestReset:
    def test_reset_clears_filled(self) -> None:
        buf = RenderPcmBuffer()
        buf.feed(np.full(1000, 1234, dtype=np.int16), sample_rate=16_000)
        assert buf.filled_samples == 1000
        buf.reset()
        assert buf.filled_samples == 0

    def test_reset_makes_get_return_zeros(self) -> None:
        buf = RenderPcmBuffer()
        buf.feed(np.full(1000, 1234, dtype=np.int16), sample_rate=16_000)
        buf.reset()
        out = buf.get_aligned_window(512)
        assert np.all(out == 0)

    def test_feed_after_reset_works(self) -> None:
        buf = RenderPcmBuffer()
        buf.feed(np.full(1000, 1234, dtype=np.int16), sample_rate=16_000)
        buf.reset()
        buf.feed(np.full(100, 5678, dtype=np.int16), sample_rate=16_000)
        out = buf.get_aligned_window(100)
        assert np.all(out == 5678)


# ── Concurrency ──────────────────────────────────────────────────────────


class TestConcurrency:
    """Producer thread and consumer thread don't race or tear samples."""

    def test_concurrent_feed_and_get_does_not_raise(self) -> None:
        buf = RenderPcmBuffer()
        stop = threading.Event()
        errors: list[BaseException] = []

        def producer() -> None:
            try:
                while not stop.is_set():
                    buf.feed(
                        np.random.default_rng().integers(
                            -1000,
                            1000,
                            512,
                            dtype=np.int16,
                        ),
                        sample_rate=16_000,
                    )
                    time.sleep(0.001)
            except BaseException as exc:  # noqa: BLE001 — surface to assert
                errors.append(exc)

        def consumer() -> None:
            try:
                end = time.monotonic() + 0.2
                while time.monotonic() < end:
                    out = buf.get_aligned_window(512)
                    assert out.shape == (512,)
                    assert out.dtype == np.int16
            except BaseException as exc:  # noqa: BLE001 — surface to assert
                errors.append(exc)

        prod = threading.Thread(target=producer)
        cons = threading.Thread(target=consumer)
        prod.start()
        cons.start()
        cons.join()
        stop.set()
        prod.join()

        assert errors == [], f"Concurrent access raised: {errors}"

    def test_get_during_feed_returns_consistent_window(self) -> None:
        # Stress test: feed a known constant pattern and verify reads
        # always return that pattern (never partial / torn samples).
        buf = RenderPcmBuffer(buffer_seconds=0.5)  # cap 8000
        pattern = np.full(512, 7777, dtype=np.int16)
        # Pre-fill so reads always have data.
        for _ in range(20):
            buf.feed(pattern, sample_rate=16_000)

        stop = threading.Event()
        errors: list[BaseException] = []

        def producer() -> None:
            try:
                while not stop.is_set():
                    buf.feed(pattern, sample_rate=16_000)
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)

        def consumer() -> None:
            try:
                for _ in range(500):
                    out = buf.get_aligned_window(512)
                    # Every read should see the constant pattern.
                    assert np.all(out == 7777), f"torn read: unique values = {np.unique(out)}"
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)

        prod = threading.Thread(target=producer)
        cons = threading.Thread(target=consumer)
        prod.start()
        cons.start()
        cons.join()
        stop.set()
        prod.join()

        assert errors == [], f"Tear or assert failure: {errors}"
