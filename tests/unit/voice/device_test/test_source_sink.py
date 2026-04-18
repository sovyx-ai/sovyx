"""Tests for :mod:`sovyx.voice.device_test._source` and ``_sink`` — fakes + classifier."""

from __future__ import annotations

import asyncio
import sys
from types import ModuleType
from unittest.mock import MagicMock, patch

import numpy as np
import pytest

from sovyx.voice.device_test._protocol import ErrorCode
from sovyx.voice.device_test._sink import (
    AudioSinkError,
    FakeAudioOutputSink,
    SoundDeviceOutputSink,
    _resample_int16,
)
from sovyx.voice.device_test._source import (
    AudioSourceError,
    AudioSourceInfo,
    FakeAudioInputSource,
    SoundDeviceInputSource,
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


# --------------------------------------------------------------------------
# Resample helper
# --------------------------------------------------------------------------


class TestResampleInt16:
    def test_identity_when_rates_match(self) -> None:
        audio = np.arange(256, dtype=np.int16)
        out = _resample_int16(audio, 16_000, 16_000)
        np.testing.assert_array_equal(out, audio)

    def test_upsample_doubles_length(self) -> None:
        audio = np.arange(100, dtype=np.int16)
        out = _resample_int16(audio, 24_000, 48_000)
        assert out.dtype == np.int16
        assert out.size == 200  # noqa: PLR2004

    def test_empty_stays_empty(self) -> None:
        audio = np.zeros(0, dtype=np.int16)
        out = _resample_int16(audio, 24_000, 48_000)
        assert out.size == 0


# --------------------------------------------------------------------------
# SoundDeviceOutputSink — rate fallback on Invalid sample rate
# --------------------------------------------------------------------------


def _fake_sounddevice() -> ModuleType:
    module = ModuleType("sounddevice")

    class _FakePortAudioError(Exception):
        pass

    module.PortAudioError = _FakePortAudioError  # type: ignore[attr-defined]
    module.InputStream = MagicMock()  # type: ignore[attr-defined]
    module.play = MagicMock()  # type: ignore[attr-defined]
    module.query_devices = MagicMock()  # type: ignore[attr-defined]
    return module


@pytest.fixture()
def fake_sd() -> ModuleType:
    fake = _fake_sounddevice()
    with patch.dict(sys.modules, {"sounddevice": fake}):
        yield fake


class TestSoundDeviceOutputSinkRateFallback:
    """Windows MME devices reject non-native rates — sink must resample and retry."""

    @pytest.mark.asyncio()
    async def test_first_open_fail_triggers_resample_and_retry(
        self,
        fake_sd: ModuleType,
    ) -> None:
        fake_sd.query_devices = MagicMock(  # type: ignore[attr-defined]
            return_value={"default_samplerate": 48_000.0},
        )
        calls: list[tuple[int, int]] = []

        def fake_play(audio: np.ndarray, *, samplerate: int, **_: object) -> None:
            calls.append((int(audio.size), samplerate))
            if len(calls) == 1:
                raise RuntimeError(
                    "Error opening OutputStream: Invalid sample rate [PaErrorCode -9997]",
                )

        fake_sd.play = fake_play  # type: ignore[attr-defined]

        audio = np.ones(24_000, dtype=np.int16)  # 1 s at 24 kHz
        sink = SoundDeviceOutputSink()
        elapsed_ms = await sink.play(audio, sample_rate=24_000, device_id=7)
        assert elapsed_ms >= 0.0
        assert len(calls) == 2  # noqa: PLR2004
        first_size, first_rate = calls[0]
        retry_size, retry_rate = calls[1]
        assert first_rate == 24_000
        assert first_size == 24_000
        # Retry runs at the device's native rate with a resampled buffer (2x).
        assert retry_rate == 48_000
        assert retry_size == 48_000

    @pytest.mark.asyncio()
    async def test_non_rate_error_raises_without_retry(
        self,
        fake_sd: ModuleType,
    ) -> None:
        fake_sd.play = MagicMock(  # type: ignore[attr-defined]
            side_effect=RuntimeError("Device unavailable"),
        )
        audio = np.ones(100, dtype=np.int16)
        sink = SoundDeviceOutputSink()
        with pytest.raises(AudioSinkError) as exc_info:
            await sink.play(audio, sample_rate=24_000, device_id=7)
        assert exc_info.value.code == ErrorCode.DEVICE_NOT_FOUND
        assert fake_sd.play.call_count == 1  # type: ignore[attr-defined]

    @pytest.mark.asyncio()
    async def test_empty_audio_skips_play_entirely(self, fake_sd: ModuleType) -> None:
        fake_sd.play = MagicMock()  # type: ignore[attr-defined]
        audio = np.zeros(0, dtype=np.int16)
        sink = SoundDeviceOutputSink()
        elapsed = await sink.play(audio, sample_rate=24_000, device_id=None)
        assert elapsed == 0.0
        fake_sd.play.assert_not_called()  # type: ignore[attr-defined]

    @pytest.mark.asyncio()
    async def test_retry_also_fails_surfaces_classified_error(
        self,
        fake_sd: ModuleType,
    ) -> None:
        fake_sd.query_devices = MagicMock(  # type: ignore[attr-defined]
            return_value={"default_samplerate": 48_000.0},
        )
        fake_sd.play = MagicMock(  # type: ignore[attr-defined]
            side_effect=RuntimeError("Invalid sample rate"),
        )
        audio = np.ones(100, dtype=np.int16)
        sink = SoundDeviceOutputSink()
        with pytest.raises(AudioSinkError) as exc_info:
            await sink.play(audio, sample_rate=24_000, device_id=7)
        assert exc_info.value.code == ErrorCode.UNSUPPORTED_SAMPLERATE


# --------------------------------------------------------------------------
# SoundDeviceInputSource — rate fallback on Invalid sample rate
# --------------------------------------------------------------------------


class TestSoundDeviceInputSourceRateFallback:
    """Same class of bug on the input side: retry at the device's native rate."""

    @pytest.mark.asyncio()
    async def test_open_falls_back_to_native_rate_on_unsupported(
        self,
        fake_sd: ModuleType,
    ) -> None:
        fake_sd.query_devices = MagicMock(  # type: ignore[attr-defined]
            return_value={"name": "FakeMic", "default_samplerate": 48_000.0},
        )
        attempts: list[int] = []

        def fake_stream_factory(*, samplerate: int, **_: object) -> MagicMock:
            attempts.append(samplerate)
            if samplerate == 16_000:
                raise RuntimeError("Invalid sample rate [PaErrorCode -9997]")
            stream = MagicMock()
            stream.start = MagicMock()
            return stream

        fake_sd.InputStream = fake_stream_factory  # type: ignore[attr-defined]

        src = SoundDeviceInputSource(device_id=18, sample_rate=16_000)
        info = await src.open()
        assert info.sample_rate == 48_000  # noqa: PLR2004
        assert attempts == [16_000, 48_000]
        await src.close()

    @pytest.mark.asyncio()
    async def test_open_uses_requested_rate_when_device_accepts(
        self,
        fake_sd: ModuleType,
    ) -> None:
        fake_sd.query_devices = MagicMock(  # type: ignore[attr-defined]
            return_value={"name": "FakeMic", "default_samplerate": 48_000.0},
        )
        fake_sd.InputStream = MagicMock(return_value=MagicMock())  # type: ignore[attr-defined]
        src = SoundDeviceInputSource(device_id=18, sample_rate=16_000)
        info = await src.open()
        assert info.sample_rate == 16_000  # noqa: PLR2004
        assert fake_sd.InputStream.call_count == 1  # type: ignore[attr-defined]
        await src.close()

    @pytest.mark.asyncio()
    async def test_non_rate_error_raises_without_retry(
        self,
        fake_sd: ModuleType,
    ) -> None:
        fake_sd.query_devices = MagicMock(  # type: ignore[attr-defined]
            return_value={"name": "FakeMic", "default_samplerate": 48_000.0},
        )
        fake_sd.InputStream = MagicMock(  # type: ignore[attr-defined]
            side_effect=RuntimeError("Device unavailable"),
        )
        src = SoundDeviceInputSource(device_id=18, sample_rate=16_000)
        with pytest.raises(AudioSourceError) as exc_info:
            await src.open()
        assert exc_info.value.code == ErrorCode.DEVICE_NOT_FOUND
        assert fake_sd.InputStream.call_count == 1  # type: ignore[attr-defined]
