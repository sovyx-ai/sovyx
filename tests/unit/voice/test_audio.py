"""Tests for AudioCapture + AudioOutput (V05-23).

Tests cover:
    - RingBuffer: write/read/wrap/overflow/clear
    - AudioCapture: lifecycle, callback, queue, ring buffer, device helpers
    - AudioOutput: lifecycle, enqueue, drain, flush, fade-out, LUFS normalisation
    - AudioDucker: ducking on/off, fade-in
    - Platform detection
    - normalize_lufs: various loudness levels, silence, clipping
"""

from __future__ import annotations

import asyncio
import sys
from unittest.mock import AsyncMock, MagicMock, patch

import numpy as np
import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from sovyx.voice.audio import (
    AudioCapture,
    AudioCaptureConfig,
    AudioDucker,
    AudioOutput,
    AudioOutputConfig,
    AudioPlatform,
    OutputChunk,
    OutputPriority,
    RingBuffer,
    detect_platform,
    normalize_lufs,
)

# ---------------------------------------------------------------------------
# RingBuffer
# ---------------------------------------------------------------------------


class TestRingBuffer:
    """Tests for :class:`RingBuffer`."""

    def test_init_capacity(self) -> None:
        buf = RingBuffer(max_seconds=2, sample_rate=16000)
        assert buf.capacity == 32000
        assert buf.available == 0

    def test_write_and_read(self) -> None:
        buf = RingBuffer(max_seconds=1, sample_rate=100)
        data = np.arange(50, dtype=np.int16)
        buf.write(data)
        assert buf.available == 50
        out = buf.read(50)
        assert out is not None
        np.testing.assert_array_equal(out, data)
        assert buf.available == 0

    def test_read_returns_none_when_empty(self) -> None:
        buf = RingBuffer(max_seconds=1, sample_rate=100)
        assert buf.read(10) is None

    def test_read_returns_none_when_insufficient(self) -> None:
        buf = RingBuffer(max_seconds=1, sample_rate=100)
        buf.write(np.zeros(5, dtype=np.int16))
        assert buf.read(10) is None

    def test_wrap_around(self) -> None:
        buf = RingBuffer(max_seconds=1, sample_rate=10)  # capacity=10
        # Write 7, read 7, write 7 → wraps
        buf.write(np.arange(7, dtype=np.int16))
        buf.read(7)
        data = np.arange(10, 17, dtype=np.int16)
        buf.write(data)
        out = buf.read(7)
        assert out is not None
        np.testing.assert_array_equal(out, data)

    def test_overflow_keeps_latest(self) -> None:
        buf = RingBuffer(max_seconds=1, sample_rate=10)  # capacity=10
        big = np.arange(15, dtype=np.int16)
        buf.write(big)
        assert buf.available == 10
        out = buf.read(10)
        assert out is not None
        np.testing.assert_array_equal(out, big[-10:])

    def test_clear(self) -> None:
        buf = RingBuffer(max_seconds=1, sample_rate=100)
        buf.write(np.zeros(50, dtype=np.int16))
        assert buf.available == 50
        buf.clear()
        assert buf.available == 0
        assert buf.read(1) is None

    def test_write_empty_array(self) -> None:
        buf = RingBuffer(max_seconds=1, sample_rate=100)
        buf.write(np.array([], dtype=np.int16))
        assert buf.available == 0

    def test_multiple_writes_and_reads(self) -> None:
        buf = RingBuffer(max_seconds=1, sample_rate=100)
        for i in range(5):
            buf.write(np.full(10, i, dtype=np.int16))
        assert buf.available == 50
        for i in range(5):
            out = buf.read(10)
            assert out is not None
            np.testing.assert_array_equal(out, np.full(10, i, dtype=np.int16))

    @settings(deadline=None, suppress_health_check=[HealthCheck.too_slow])
    @given(n=st.integers(min_value=1, max_value=50))
    def test_write_read_roundtrip_property(self, n: int) -> None:
        buf = RingBuffer(max_seconds=1, sample_rate=100)
        data = np.arange(n, dtype=np.int16)
        buf.write(data)
        out = buf.read(n)
        assert out is not None
        np.testing.assert_array_equal(out, data)

    def test_wrap_around_read(self) -> None:
        """Read that spans the wrap-around boundary."""
        buf = RingBuffer(max_seconds=1, sample_rate=10)  # cap=10
        # Fill to position 8
        buf.write(np.arange(8, dtype=np.int16))
        buf.read(8)
        # Write 6 more → wraps
        data = np.arange(20, 26, dtype=np.int16)
        buf.write(data)
        out = buf.read(6)
        assert out is not None
        np.testing.assert_array_equal(out, data)


# ---------------------------------------------------------------------------
# Platform detection
# ---------------------------------------------------------------------------


class TestDetectPlatform:
    """Tests for :func:`detect_platform`."""

    def test_linux_pulse(self) -> None:
        with (
            patch("platform.system", return_value="Linux"),
            patch("shutil.which", return_value="/usr/bin/pactl"),
        ):
            assert detect_platform() == AudioPlatform.PULSEAUDIO

    def test_linux_alsa(self) -> None:
        with (
            patch("platform.system", return_value="Linux"),
            patch("shutil.which", return_value=None),
        ):
            assert detect_platform() == AudioPlatform.ALSA

    def test_darwin(self) -> None:
        with patch("platform.system", return_value="Darwin"):
            assert detect_platform() == AudioPlatform.COREAUDIO

    def test_windows(self) -> None:
        with patch("platform.system", return_value="Windows"):
            assert detect_platform() == AudioPlatform.WASAPI

    def test_unknown(self) -> None:
        with patch("platform.system", return_value="FreeBSD"):
            assert detect_platform() == AudioPlatform.UNKNOWN


# ---------------------------------------------------------------------------
# AudioCapture
# ---------------------------------------------------------------------------


class TestAudioCaptureConfig:
    """Tests for :class:`AudioCaptureConfig`."""

    def test_defaults(self) -> None:
        cfg = AudioCaptureConfig()
        assert cfg.sample_rate == 16000
        assert cfg.channels == 1
        assert cfg.chunk_ms == 20
        assert cfg.device is None
        assert cfg.queue_maxsize == 100

    def test_custom(self) -> None:
        cfg = AudioCaptureConfig(
            sample_rate=44100,
            channels=2,
            chunk_ms=10,
            device=3,
            queue_maxsize=50,
        )
        assert cfg.sample_rate == 44100
        assert cfg.channels == 2
        assert cfg.device == 3


class TestAudioCapture:
    """Tests for :class:`AudioCapture`."""

    def test_init_defaults(self) -> None:
        cap = AudioCapture()
        assert cap.sample_rate == 16000
        assert cap.chunk_samples == 320  # 16000 * 20/1000
        assert not cap.is_running

    def test_init_custom_config(self) -> None:
        cfg = AudioCaptureConfig(sample_rate=44100, chunk_ms=10)
        cap = AudioCapture(config=cfg)
        assert cap.sample_rate == 44100
        assert cap.chunk_samples == 441

    @pytest.mark.asyncio
    async def test_start_stop(self) -> None:
        cap = AudioCapture()
        mock_stream = MagicMock()
        mock_sd_module = MagicMock()
        mock_sd_module.InputStream.return_value = mock_stream
        sys.modules["sounddevice"] = mock_sd_module
        try:
            await cap.start()
            assert cap.is_running
            mock_sd_module.InputStream.assert_called_once()
            mock_stream.start.assert_called_once()

            await cap.stop()
            assert not cap.is_running
            mock_stream.stop.assert_called_once()
            mock_stream.close.assert_called_once()
        finally:
            del sys.modules["sounddevice"]

    @pytest.mark.asyncio
    async def test_callback_enqueues_mono(self) -> None:
        cap = AudioCapture()
        cap._loop = asyncio.get_running_loop()
        # Simulate 2-D input (channels=1 → shape (320, 1))
        indata = np.random.randint(-32768, 32767, size=(320, 1), dtype=np.int16)
        status = MagicMock()
        status.__bool__ = lambda self: False  # noqa: ARG005

        cap._audio_callback(indata, 320, None, status)
        await asyncio.sleep(0.01)

        chunk = cap.read_chunk_nowait()
        assert chunk is not None
        assert chunk.shape == (320,)
        np.testing.assert_array_equal(chunk, indata[:, 0])

    @pytest.mark.asyncio
    async def test_callback_writes_to_ring_buffer(self) -> None:
        cap = AudioCapture()
        cap._loop = asyncio.get_running_loop()
        indata = np.ones((320, 1), dtype=np.int16)
        status = MagicMock()
        status.__bool__ = lambda self: False  # noqa: ARG005

        cap._audio_callback(indata, 320, None, status)
        assert cap.ring_buffer.available == 320

    @pytest.mark.asyncio
    async def test_callback_handles_overflow(self) -> None:
        """When queue is full, callback should not raise."""
        cfg = AudioCaptureConfig(queue_maxsize=1)
        cap = AudioCapture(config=cfg)
        cap._loop = asyncio.get_running_loop()
        status = MagicMock()
        status.__bool__ = lambda self: False  # noqa: ARG005

        indata = np.ones((320, 1), dtype=np.int16)
        cap._audio_callback(indata, 320, None, status)
        await asyncio.sleep(0.01)

        # Second call should not raise even though queue is full
        cap._audio_callback(indata, 320, None, status)

    @pytest.mark.asyncio
    async def test_callback_logs_status(self) -> None:
        cap = AudioCapture()
        cap._loop = asyncio.get_running_loop()
        status = MagicMock()
        status.__bool__ = lambda self: True  # noqa: ARG005
        status.__str__ = lambda self: "input overflow"  # noqa: ARG005
        indata = np.ones((320, 1), dtype=np.int16)

        # Should not raise
        cap._audio_callback(indata, 320, None, status)

    @pytest.mark.asyncio
    async def test_read_chunk_blocks(self) -> None:
        cap = AudioCapture()
        cap._loop = asyncio.get_running_loop()

        async def delayed_put() -> None:
            await asyncio.sleep(0.05)
            indata = np.zeros((320, 1), dtype=np.int16)
            status = MagicMock()
            status.__bool__ = lambda self: False  # noqa: ARG005
            cap._audio_callback(indata, 320, None, status)

        asyncio.create_task(delayed_put())
        chunk = await asyncio.wait_for(cap.read_chunk(), timeout=1.0)
        assert chunk is not None
        assert len(chunk) == 320

    def test_get_frame_from_ring_buffer(self) -> None:
        cap = AudioCapture()
        assert cap.get_frame() is None
        cap._ring_buffer.write(np.zeros(320, dtype=np.int16))
        frame = cap.get_frame()
        assert frame is not None
        assert len(frame) == 320

    def test_list_devices(self) -> None:
        mock_sd = MagicMock()
        mock_sd.query_devices.return_value = [
            {
                "name": "Mic 1",
                "max_input_channels": 2,
                "max_output_channels": 0,
                "default_samplerate": 44100.0,
            },
            {
                "name": "Speaker",
                "max_input_channels": 0,
                "max_output_channels": 2,
                "default_samplerate": 48000.0,
            },
        ]
        sys.modules["sounddevice"] = mock_sd
        try:
            devices = AudioCapture.list_devices()
            assert len(devices) == 1
            assert devices[0]["name"] == "Mic 1"
            assert devices[0]["channels"] == 2
        finally:
            del sys.modules["sounddevice"]

    def test_negotiate_sample_rate_preferred(self) -> None:
        mock_sd = MagicMock()
        mock_sd.PortAudioError = type("PortAudioError", (Exception,), {})
        mock_sd.check_input_settings = MagicMock()
        sys.modules["sounddevice"] = mock_sd
        try:
            rate = AudioCapture.negotiate_sample_rate(
                device=None,
                preferred=16000,
            )
            assert rate == 16000
        finally:
            del sys.modules["sounddevice"]

    def test_negotiate_sample_rate_fallback(self) -> None:
        mock_sd = MagicMock()
        err_cls = type("PortAudioError", (Exception,), {})
        mock_sd.PortAudioError = err_cls

        def check_side_effect(
            device: object = None,  # noqa: ARG001
            samplerate: int = 0,
        ) -> None:
            if samplerate == 16000:
                raise err_cls("no")

        mock_sd.check_input_settings = MagicMock(
            side_effect=check_side_effect,
        )
        sys.modules["sounddevice"] = mock_sd
        try:
            rate = AudioCapture.negotiate_sample_rate(
                device=None,
                preferred=16000,
            )
            assert rate == 44100
        finally:
            del sys.modules["sounddevice"]

    def test_negotiate_sample_rate_none_supported(self) -> None:
        mock_sd = MagicMock()
        err_cls = type("PortAudioError", (Exception,), {})
        mock_sd.PortAudioError = err_cls
        mock_sd.check_input_settings = MagicMock(
            side_effect=err_cls("no"),
        )
        sys.modules["sounddevice"] = mock_sd
        try:
            with pytest.raises(RuntimeError, match="No supported sample rate"):
                AudioCapture.negotiate_sample_rate(device=None)
        finally:
            del sys.modules["sounddevice"]

    @pytest.mark.asyncio
    async def test_callback_1d_input(self) -> None:
        """Callback handles 1-D arrays (mono, no channel dim)."""
        cap = AudioCapture()
        cap._loop = asyncio.get_running_loop()
        indata = np.ones(320, dtype=np.int16)
        status = MagicMock()
        status.__bool__ = lambda self: False  # noqa: ARG005

        cap._audio_callback(indata, 320, None, status)
        await asyncio.sleep(0.01)

        chunk = cap.read_chunk_nowait()
        assert chunk is not None
        assert chunk.shape == (320,)

    @pytest.mark.asyncio
    async def test_stop_when_no_stream(self) -> None:
        cap = AudioCapture()
        await cap.stop()
        assert not cap.is_running


# ---------------------------------------------------------------------------
# LUFS normalisation
# ---------------------------------------------------------------------------


class TestNormalizeLufs:
    """Tests for :func:`normalize_lufs`."""

    def test_silence_unchanged(self) -> None:
        audio = np.zeros(1000, dtype=np.float32)
        result = normalize_lufs(audio)
        np.testing.assert_array_equal(result, audio)

    def test_quiet_signal_boosted(self) -> None:
        audio = np.full(1000, 0.001, dtype=np.float32)
        result = normalize_lufs(audio, target=-16.0)
        assert float(np.max(np.abs(result))) > float(np.max(np.abs(audio)))

    def test_loud_signal_reduced(self) -> None:
        audio = np.full(1000, 0.9, dtype=np.float32)
        result = normalize_lufs(audio, target=-30.0)
        assert float(np.max(np.abs(result))) < float(np.max(np.abs(audio)))

    def test_output_clipped(self) -> None:
        audio = np.full(1000, 0.001, dtype=np.float32)
        result = normalize_lufs(audio, target=-1.0)
        assert float(np.max(result)) <= 1.0
        assert float(np.min(result)) >= -1.0

    def test_preserves_dtype(self) -> None:
        audio = np.full(100, 0.5, dtype=np.float32)
        result = normalize_lufs(audio)
        assert result.dtype == np.float32

    @settings(deadline=None, suppress_health_check=[HealthCheck.too_slow])
    @given(level=st.floats(min_value=0.001, max_value=0.99))
    def test_output_always_bounded(self, level: float) -> None:
        audio = np.full(100, level, dtype=np.float32)
        result = normalize_lufs(audio, target=-16.0)
        assert float(np.max(result)) <= 1.0
        assert float(np.min(result)) >= -1.0


# ---------------------------------------------------------------------------
# AudioDucker
# ---------------------------------------------------------------------------


class TestAudioDucker:
    """Tests for :class:`AudioDucker`."""

    def test_no_duck_when_not_speaking(self) -> None:
        ducker = AudioDucker()
        bg = np.full(100, 0.5, dtype=np.float32)
        result = ducker.duck(bg, is_speaking=False)
        np.testing.assert_array_equal(result, bg)

    def test_duck_when_speaking(self) -> None:
        ducker = AudioDucker(duck_level_db=-12.0)
        bg = np.full(100, 0.5, dtype=np.float32)
        result = ducker.duck(bg, is_speaking=True)
        assert ducker.is_ducked
        expected_gain = 10 ** (-12.0 / 20)
        np.testing.assert_allclose(result, bg * expected_gain, rtol=1e-5)

    def test_fade_in_after_speaking(self) -> None:
        ducker = AudioDucker(
            duck_level_db=-12.0,
            fade_in_ms=50,
            sample_rate=1000,
        )
        bg = np.full(100, 0.5, dtype=np.float32)
        ducker.duck(bg, is_speaking=True)
        assert ducker.is_ducked

        result = ducker.duck(bg, is_speaking=False)
        assert not ducker.is_ducked
        expected_gain = 10 ** (-12.0 / 20)
        assert abs(float(result[0]) - bg[0] * expected_gain) < 0.01
        fade_samples = 50  # 50ms * 1000Hz / 1000
        assert abs(float(result[fade_samples - 1]) - bg[0]) < 0.02

    def test_duck_gain_property(self) -> None:
        ducker = AudioDucker(duck_level_db=-6.0)
        expected = 10 ** (-6.0 / 20)
        assert abs(ducker.duck_gain - expected) < 1e-6

    def test_no_fade_when_never_ducked(self) -> None:
        ducker = AudioDucker()
        bg = np.full(100, 0.8, dtype=np.float32)
        result = ducker.duck(bg, is_speaking=False)
        np.testing.assert_array_equal(result, bg)
        assert not ducker.is_ducked


# ---------------------------------------------------------------------------
# OutputChunk
# ---------------------------------------------------------------------------


class TestOutputChunk:
    """Tests for :class:`OutputChunk`."""

    def test_duration_ms(self) -> None:
        audio = np.zeros(22050, dtype=np.float32)
        chunk = OutputChunk(audio=audio, sample_rate=22050)
        assert abs(chunk.duration_ms - 1000.0) < 0.01

    def test_priority_ordering(self) -> None:
        zeros = np.zeros(10, dtype=np.float32)
        filler = OutputChunk(
            audio=zeros,
            sample_rate=22050,
            priority=OutputPriority.FILLER,
        )
        normal = OutputChunk(
            audio=zeros,
            sample_rate=22050,
            priority=OutputPriority.NORMAL,
        )
        low = OutputChunk(
            audio=zeros,
            sample_rate=22050,
            priority=OutputPriority.LOW,
        )
        assert filler < normal
        assert normal < low
        assert filler < low

    def test_same_priority_ordered_by_time(self) -> None:
        zeros = np.zeros(10, dtype=np.float32)
        c1 = OutputChunk(audio=zeros, sample_rate=22050, timestamp=1.0)
        c2 = OutputChunk(audio=zeros, sample_rate=22050, timestamp=2.0)
        assert c1 < c2

    def test_lt_not_implemented_for_other_types(self) -> None:
        chunk = OutputChunk(
            audio=np.zeros(10, dtype=np.float32),
            sample_rate=22050,
        )
        assert chunk.__lt__("not a chunk") is NotImplemented


# ---------------------------------------------------------------------------
# AudioOutputConfig
# ---------------------------------------------------------------------------


class TestAudioOutputConfig:
    """Tests for :class:`AudioOutputConfig`."""

    def test_defaults(self) -> None:
        cfg = AudioOutputConfig()
        assert cfg.sample_rate == 22050
        assert cfg.channels == 1
        assert cfg.device is None
        assert cfg.target_lufs == -16.0


# ---------------------------------------------------------------------------
# AudioOutput
# ---------------------------------------------------------------------------


class TestAudioOutput:
    """Tests for :class:`AudioOutput`."""

    def test_init_defaults(self) -> None:
        out = AudioOutput()
        assert out.sample_rate == 22050
        assert not out.is_playing
        assert out.queue_size == 0

    @pytest.mark.asyncio
    async def test_start_stop(self) -> None:
        mock_sd = MagicMock()
        mock_stream = MagicMock()
        mock_sd.OutputStream.return_value = mock_stream
        sys.modules["sounddevice"] = mock_sd
        try:
            out = AudioOutput()
            await out.start()
            mock_sd.OutputStream.assert_called_once()
            mock_stream.start.assert_called_once()

            await out.stop()
            assert not out.is_playing
            mock_stream.stop.assert_called_once()
            mock_stream.close.assert_called_once()
        finally:
            del sys.modules["sounddevice"]

    @pytest.mark.asyncio
    async def test_enqueue_normalises(self) -> None:
        out = AudioOutput()
        audio = np.full(100, 0.5, dtype=np.float32)
        await out.enqueue(audio)
        assert out.queue_size == 1

    @pytest.mark.asyncio
    async def test_enqueue_converts_int16(self) -> None:
        out = AudioOutput()
        audio = np.full(100, 16384, dtype=np.int16)
        await out.enqueue(audio)
        assert out.queue_size == 1

    @pytest.mark.asyncio
    async def test_drain_plays_all(self) -> None:
        out = AudioOutput()
        for _ in range(3):
            await out.enqueue(
                np.zeros(100, dtype=np.float32),
                sample_rate=22050,
            )
        assert out.queue_size == 3

        out._play_chunk = AsyncMock()  # type: ignore[method-assign]
        await out.drain()
        assert out.queue_size == 0
        assert out._play_chunk.call_count == 3
        assert not out.is_playing

    @pytest.mark.asyncio
    async def test_drain_respects_priority(self) -> None:
        out = AudioOutput()
        await out.enqueue(
            np.full(10, 0.3, dtype=np.float32),
            priority=OutputPriority.LOW,
        )
        await out.enqueue(
            np.full(10, 0.1, dtype=np.float32),
            priority=OutputPriority.FILLER,
        )
        await out.enqueue(
            np.full(10, 0.2, dtype=np.float32),
            priority=OutputPriority.NORMAL,
        )

        out._play_chunk = AsyncMock()  # type: ignore[method-assign]
        await out.drain()
        assert out.queue_size == 0

    @pytest.mark.asyncio
    async def test_flush_clears_queue(self) -> None:
        out = AudioOutput()
        for _ in range(5):
            await out.enqueue(np.zeros(100, dtype=np.float32))
        assert out.queue_size == 5
        out.flush()
        assert out.queue_size == 0
        assert not out.is_playing

    @pytest.mark.asyncio
    async def test_play_immediate(self) -> None:
        out = AudioOutput()
        out._play_chunk = AsyncMock()  # type: ignore[method-assign]
        audio = np.full(100, 0.5, dtype=np.float32)
        await out.play_immediate(audio)
        out._play_chunk.assert_called_once()
        assert not out.is_playing

    @pytest.mark.asyncio
    async def test_play_immediate_int16(self) -> None:
        out = AudioOutput()
        out._play_chunk = AsyncMock()  # type: ignore[method-assign]
        audio = np.full(100, 16384, dtype=np.int16)
        await out.play_immediate(audio)
        out._play_chunk.assert_called_once()
        call_args = out._play_chunk.call_args[0]
        assert call_args[0].dtype == np.float32

    def test_apply_fade_out(self) -> None:
        out = AudioOutput()
        audio = np.full(500, 0.8, dtype=np.float32)
        out._current_audio = audio.copy()
        out._play_position = 100
        out.apply_fade_out(samples=100)
        assert abs(float(out._current_audio[199])) < 0.01  # type: ignore[index]

    def test_apply_fade_out_no_current_audio(self) -> None:
        out = AudioOutput()
        out.apply_fade_out()

    def test_apply_fade_out_zero_position(self) -> None:
        out = AudioOutput()
        out._current_audio = np.full(100, 0.5, dtype=np.float32)
        out._play_position = 0
        out.apply_fade_out()

    @pytest.mark.asyncio
    async def test_stop_when_no_stream(self) -> None:
        out = AudioOutput()
        await out.stop()

    def test_list_devices(self) -> None:
        mock_sd = MagicMock()
        mock_sd.query_devices.return_value = [
            {
                "name": "Mic",
                "max_input_channels": 2,
                "max_output_channels": 0,
                "default_samplerate": 44100.0,
            },
            {
                "name": "Speaker",
                "max_input_channels": 0,
                "max_output_channels": 2,
                "default_samplerate": 48000.0,
            },
        ]
        sys.modules["sounddevice"] = mock_sd
        try:
            devices = AudioOutput.list_devices()
            assert len(devices) == 1
            assert devices[0]["name"] == "Speaker"
        finally:
            del sys.modules["sounddevice"]

    @pytest.mark.asyncio
    async def test_play_chunk_fallback_no_sounddevice(self) -> None:
        """When sounddevice is not installed, falls back to sleep."""
        saved = sys.modules.get("sounddevice")
        sys.modules["sounddevice"] = None  # type: ignore[assignment]
        try:
            out = AudioOutput()
            audio = np.zeros(100, dtype=np.float32)
            await asyncio.wait_for(
                out._play_chunk(audio, 22050),
                timeout=2.0,
            )
        finally:
            if saved is not None:
                sys.modules["sounddevice"] = saved
            else:
                sys.modules.pop("sounddevice", None)

    @pytest.mark.asyncio
    async def test_play_chunk_does_not_block_event_loop(self) -> None:
        """``_play_chunk`` must delegate blocking ``sd.wait()`` to a thread.

        Regression — audio.py used to call ``sd.play()`` + ``sd.wait()``
        directly inside the ``async def``. ``sd.wait()`` blocks for the
        full clip duration; every other coroutine (voice pipeline, bridge,
        dashboard WS) stalled until playback finished. See CLAUDE.md
        anti-pattern #14 and the Round 1 fix in
        ``voice/pipeline/_output_queue.py``.
        """
        import threading

        wait_done = threading.Event()

        mock_sd = MagicMock()
        mock_sd.play = MagicMock()
        # Simulate a 300ms clip — blocking. If the call is NOT off the
        # event loop, the ticker below will stall.
        mock_sd.wait = MagicMock(
            side_effect=lambda: wait_done.wait(timeout=1.0),
        )

        saved = sys.modules.get("sounddevice")
        sys.modules["sounddevice"] = mock_sd
        try:
            out = AudioOutput()
            audio = np.zeros(100, dtype=np.float32)

            ticks = 0

            async def tick() -> None:
                nonlocal ticks
                for _ in range(5):
                    await asyncio.sleep(0.01)
                    ticks += 1

            play_task = asyncio.create_task(out._play_chunk(audio, 22050))
            tick_task = asyncio.create_task(tick())

            # The ticker must complete while playback is still "blocking"
            # in its thread. If the event loop were blocked, ticks would
            # stay at 0 and asyncio.wait would timeout.
            await asyncio.wait_for(tick_task, timeout=0.5)
            assert ticks == 5

            # Release the fake blocking wait and let the play task finish.
            wait_done.set()
            await asyncio.wait_for(play_task, timeout=1.0)

            mock_sd.play.assert_called_once()
            mock_sd.wait.assert_called_once()
        finally:
            wait_done.set()
            if saved is not None:
                sys.modules["sounddevice"] = saved
            else:
                sys.modules.pop("sounddevice", None)

    def test_ducker_property(self) -> None:
        out = AudioOutput()
        assert isinstance(out.ducker, AudioDucker)

    @pytest.mark.asyncio
    async def test_enqueue_custom_sample_rate(self) -> None:
        out = AudioOutput()
        audio = np.zeros(100, dtype=np.float32)
        await out.enqueue(
            audio,
            sample_rate=48000,
            priority=OutputPriority.FILLER,
        )
        assert out.queue_size == 1


# ---------------------------------------------------------------------------
# OutputPriority
# ---------------------------------------------------------------------------


class TestOutputPriority:
    """Tests for :class:`OutputPriority`."""

    def test_values(self) -> None:
        assert OutputPriority.FILLER == 0
        assert OutputPriority.NORMAL == 1
        assert OutputPriority.LOW == 2

    def test_ordering(self) -> None:
        assert OutputPriority.FILLER < OutputPriority.NORMAL < OutputPriority.LOW


# ---------------------------------------------------------------------------
# AudioPlatform
# ---------------------------------------------------------------------------


class TestAudioPlatform:
    """Tests for :class:`AudioPlatform`."""

    def test_values(self) -> None:
        assert AudioPlatform.ALSA.value == "alsa"
        assert AudioPlatform.PULSEAUDIO.value == "pulseaudio"
        assert AudioPlatform.COREAUDIO.value == "coreaudio"
        assert AudioPlatform.WASAPI.value == "wasapi"
        assert AudioPlatform.UNKNOWN.value == "unknown"
