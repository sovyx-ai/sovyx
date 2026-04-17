"""Tests for :mod:`sovyx.voice.device_test._source` and ``_sink`` — fakes + classifier."""

from __future__ import annotations

import asyncio

import numpy as np
import pytest

from sovyx.voice.device_test._protocol import ErrorCode
from sovyx.voice.device_test._sink import AudioSinkError, FakeAudioOutputSink
from sovyx.voice.device_test._source import (
    AudioSourceError,
    AudioSourceInfo,
    FakeAudioInputSource,
    _classify_portaudio_error,
)

# --------------------------------------------------------------------------
# FakeAudioInputSource
# --------------------------------------------------------------------------


class TestFakeAudioInputSource:
    """Sanity-check the in-process source used across session + property tests."""

    @pytest.mark.asyncio()
    async def test_open_returns_info(self) -> None:
        frames = [np.zeros(256, dtype=np.int16)]
        src = FakeAudioInputSource(
            frames,
            sample_rate=16_000,
            device_id=3,
            device_name="MyMic",
        )
        info = await src.open()
        assert isinstance(info, AudioSourceInfo)
        assert info.device_id == 3
        assert info.device_name == "MyMic"
        assert info.sample_rate == 16_000
        assert info.channels == 1
        assert info.blocksize == 256

    @pytest.mark.asyncio()
    async def test_open_raises_configured_error(self) -> None:
        boom = AudioSourceError(ErrorCode.DEVICE_BUSY, "held")
        src = FakeAudioInputSource([], error_on_open=boom)
        with pytest.raises(AudioSourceError) as exc_info:
            await src.open()
        assert exc_info.value.code == ErrorCode.DEVICE_BUSY

    @pytest.mark.asyncio()
    async def test_frames_yields_all_then_stops(self) -> None:
        frames = [np.zeros(256, dtype=np.int16) for _ in range(3)]
        src = FakeAudioInputSource(frames, frame_interval_s=0.0)
        collected = []
        async for f in src.frames():
            collected.append(f)
        assert len(collected) == 3

    @pytest.mark.asyncio()
    async def test_close_stops_iteration(self) -> None:
        # Slow frames so we can close in-flight.
        frames = [np.zeros(256, dtype=np.int16) for _ in range(100)]
        src = FakeAudioInputSource(frames, frame_interval_s=0.05)

        async def consume() -> int:
            seen = 0
            async for _ in src.frames():
                seen += 1
                if seen >= 2:
                    await src.close()
            return seen

        count = await asyncio.wait_for(consume(), timeout=2.0)
        assert count >= 2
        assert count < 100

    @pytest.mark.asyncio()
    async def test_blocksize_zero_when_no_frames(self) -> None:
        src = FakeAudioInputSource([])
        info = await src.open()
        assert info.blocksize == 0


# --------------------------------------------------------------------------
# _classify_portaudio_error — dual-used by source and sink
# --------------------------------------------------------------------------


class TestClassifyPortAudioError:
    """Raw exception messages map to stable :class:`ErrorCode` values."""

    def test_invalid_device_maps_to_device_not_found(self) -> None:
        exc = RuntimeError("Error: Invalid device")
        out = _classify_portaudio_error(exc)
        assert out.code == ErrorCode.DEVICE_NOT_FOUND

    def test_device_unavailable_maps_to_device_not_found(self) -> None:
        exc = RuntimeError("Device unavailable")
        out = _classify_portaudio_error(exc)
        assert out.code == ErrorCode.DEVICE_NOT_FOUND

    def test_sample_rate_maps_to_unsupported_samplerate(self) -> None:
        exc = RuntimeError("Invalid sample rate")
        out = _classify_portaudio_error(exc)
        assert out.code == ErrorCode.UNSUPPORTED_SAMPLERATE

    def test_channel_maps_to_unsupported_channels(self) -> None:
        exc = RuntimeError("Invalid number of channels")
        out = _classify_portaudio_error(exc)
        assert out.code == ErrorCode.UNSUPPORTED_CHANNELS

    def test_busy_maps_to_device_busy(self) -> None:
        exc = RuntimeError("Device busy")
        out = _classify_portaudio_error(exc)
        assert out.code == ErrorCode.DEVICE_BUSY

    def test_exclusive_maps_to_device_busy(self) -> None:
        exc = RuntimeError("Exclusive mode held")
        out = _classify_portaudio_error(exc)
        assert out.code == ErrorCode.DEVICE_BUSY

    def test_permission_maps_to_permission_denied(self) -> None:
        exc = RuntimeError("Permission denied for device")
        out = _classify_portaudio_error(exc)
        assert out.code == ErrorCode.PERMISSION_DENIED

    def test_unknown_maps_to_internal_error(self) -> None:
        exc = RuntimeError("Something utterly weird")
        out = _classify_portaudio_error(exc)
        assert out.code == ErrorCode.INTERNAL_ERROR


# --------------------------------------------------------------------------
# FakeAudioOutputSink
# --------------------------------------------------------------------------


class TestFakeAudioOutputSink:
    """The sink double records calls and synthesises a realistic duration."""

    @pytest.mark.asyncio()
    async def test_play_records_call(self) -> None:
        sink = FakeAudioOutputSink()
        # 1 second of silence at 16 kHz.
        audio = np.zeros(16_000, dtype=np.int16)
        duration = await sink.play(audio, sample_rate=16_000, device_id=2)
        assert duration == pytest.approx(1_000.0, abs=0.5)
        assert len(sink.calls) == 1
        call = sink.calls[0]
        assert call["samples"] == 16_000
        assert call["sample_rate"] == 16_000
        assert call["device_id"] == 2

    @pytest.mark.asyncio()
    async def test_empty_audio_is_zero_duration(self) -> None:
        sink = FakeAudioOutputSink()
        audio = np.zeros(0, dtype=np.int16)
        duration = await sink.play(audio, sample_rate=16_000, device_id=None)
        assert duration == 0.0

    @pytest.mark.asyncio()
    async def test_play_raises_configured_error(self) -> None:
        boom = AudioSinkError(ErrorCode.DEVICE_BUSY, "held")
        sink = FakeAudioOutputSink(error=boom)
        audio = np.zeros(128, dtype=np.int16)
        with pytest.raises(AudioSinkError) as exc_info:
            await sink.play(audio, sample_rate=16_000, device_id=None)
        assert exc_info.value.code == ErrorCode.DEVICE_BUSY
