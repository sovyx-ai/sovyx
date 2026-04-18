"""Tests for :mod:`sovyx.voice.device_test._source` and ``_sink` — fakes + classifier."""

from __future__ import annotations

import asyncio
from types import ModuleType
from typing import Any
from unittest.mock import MagicMock

import numpy as np
import pytest

from sovyx.engine.config import VoiceTuningConfig
from sovyx.voice._stream_opener import _resample_int16
from sovyx.voice.device_enum import DeviceEntry
from sovyx.voice.device_test._protocol import ErrorCode
from sovyx.voice.device_test._sink import (
    AudioSinkError,
    FakeAudioOutputSink,
    SoundDeviceOutputSink,
)
from sovyx.voice.device_test._source import (
    AudioSourceError,
    AudioSourceInfo,
    FakeAudioInputSource,
    SoundDeviceInputSource,
    _classify_portaudio_error,
)


def _wasapi_input_entry(
    *,
    index: int = 18,
    channels: int = 1,
    rate: int = 48_000,
    name: str = "FakeMic",
) -> DeviceEntry:
    return DeviceEntry(
        index=index,
        name=name,
        canonical_name=name.lower(),
        host_api_index=3,
        host_api_name="Windows WASAPI",
        max_input_channels=channels,
        max_output_channels=0,
        default_samplerate=rate,
        is_os_default=True,
    )


def _fake_sd() -> ModuleType:
    module = ModuleType("sounddevice")

    class _FakePortAudioError(Exception):
        pass

    class _FakeWasapiSettings:
        def __init__(self, *, auto_convert: bool = False, exclusive: bool = False) -> None:
            self.auto_convert = auto_convert
            self.exclusive = exclusive

    module.PortAudioError = _FakePortAudioError  # type: ignore[attr-defined]
    module.WasapiSettings = _FakeWasapiSettings  # type: ignore[attr-defined]
    module.InputStream = MagicMock()  # type: ignore[attr-defined]
    module.OutputStream = MagicMock()  # type: ignore[attr-defined]
    module.play = MagicMock()  # type: ignore[attr-defined]
    module.query_devices = MagicMock()  # type: ignore[attr-defined]
    return module


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
# SoundDeviceOutputSink — integration with _stream_opener.play_audio
# --------------------------------------------------------------------------


class TestSoundDeviceOutputSinkIntegration:
    """The sink delegates to ``_stream_opener.play_audio`` under the hood.

    The pyramid's rate/channels/auto_convert fallback logic is unit-tested
    in ``test_stream_opener.py``; here we only verify the wiring (device
    resolution + error propagation + empty-audio short-circuit).
    """

    @pytest.mark.asyncio()
    async def test_empty_audio_skips_play_entirely(self) -> None:
        sd = _fake_sd()
        sd.OutputStream = MagicMock()  # type: ignore[attr-defined]
        audio = np.zeros(0, dtype=np.int16)
        sink = SoundDeviceOutputSink(sd_module=sd, enumerate_fn=lambda: [])
        elapsed = await sink.play(audio, sample_rate=24_000, device_id=None)
        assert elapsed == 0.0
        sd.OutputStream.assert_not_called()  # type: ignore[attr-defined]

    @pytest.mark.asyncio()
    async def test_play_delegates_to_opener_with_resolved_entry(self) -> None:
        sd = _fake_sd()
        calls: list[dict[str, Any]] = []

        def stream_factory(**kwargs: Any) -> MagicMock:
            calls.append(dict(kwargs))
            return MagicMock()

        sd.OutputStream = stream_factory  # type: ignore[attr-defined]
        out_entry = DeviceEntry(
            index=15,
            name="FakeSpeaker",
            canonical_name="fakespeaker",
            host_api_index=3,
            host_api_name="Windows WASAPI",
            max_input_channels=0,
            max_output_channels=2,
            default_samplerate=48_000,
            is_os_default=True,
        )
        sink = SoundDeviceOutputSink(sd_module=sd, enumerate_fn=lambda: [out_entry])
        audio = np.ones(24_000, dtype=np.int16)
        elapsed = await sink.play(audio, sample_rate=24_000, device_id=15)
        assert elapsed >= 0.0
        assert len(calls) == 1
        assert calls[0]["device"] == 15
        # WASAPI + default tuning means auto_convert is on and extra_settings is set.
        assert calls[0].get("extra_settings") is not None

    @pytest.mark.asyncio()
    async def test_play_raises_audio_sink_error_on_failure(self) -> None:
        sd = _fake_sd()
        sd.OutputStream = MagicMock(  # type: ignore[attr-defined]
            side_effect=RuntimeError("Device unavailable"),
        )
        out_entry = DeviceEntry(
            index=15,
            name="FakeSpeaker",
            canonical_name="fakespeaker",
            host_api_index=3,
            host_api_name="Windows WASAPI",
            max_input_channels=0,
            max_output_channels=2,
            default_samplerate=48_000,
            is_os_default=True,
        )
        sink = SoundDeviceOutputSink(sd_module=sd, enumerate_fn=lambda: [out_entry])
        with pytest.raises(AudioSinkError) as exc_info:
            await sink.play(np.ones(100, dtype=np.int16), sample_rate=24_000, device_id=15)
        assert exc_info.value.code == ErrorCode.DEVICE_NOT_FOUND

    @pytest.mark.asyncio()
    async def test_play_resolves_to_default_when_device_id_unknown(self) -> None:
        sd = _fake_sd()
        captured: list[dict[str, Any]] = []

        def stream_factory(**kwargs: Any) -> MagicMock:
            captured.append(dict(kwargs))
            return MagicMock()

        sd.OutputStream = stream_factory  # type: ignore[attr-defined]
        default = DeviceEntry(
            index=5,
            name="DefaultSpeaker",
            canonical_name="defaultspeaker",
            host_api_index=1,
            host_api_name="Windows DirectSound",
            max_input_channels=0,
            max_output_channels=2,
            default_samplerate=44_100,
            is_os_default=True,
        )
        other = DeviceEntry(
            index=9,
            name="Other",
            canonical_name="other",
            host_api_index=1,
            host_api_name="Windows DirectSound",
            max_input_channels=0,
            max_output_channels=2,
            default_samplerate=44_100,
            is_os_default=False,
        )
        sink = SoundDeviceOutputSink(sd_module=sd, enumerate_fn=lambda: [default, other])
        await sink.play(
            np.ones(100, dtype=np.int16),
            sample_rate=44_100,
            device_id=99,  # nonexistent — falls back to OS default
        )
        assert captured and captured[0]["device"] == 5


# --------------------------------------------------------------------------
# SoundDeviceInputSource — integration with _stream_opener.open_input_stream
# --------------------------------------------------------------------------


class TestSoundDeviceInputSourceIntegration:
    """The source delegates to ``_stream_opener.open_input_stream`` under the hood."""

    @pytest.mark.asyncio()
    async def test_open_resolves_device_and_delegates_to_opener(self) -> None:
        sd = _fake_sd()
        captured: list[dict[str, Any]] = []

        def stream_factory(**kwargs: Any) -> MagicMock:
            captured.append(kwargs)
            stream = MagicMock()
            stream.start = MagicMock()
            return stream

        sd.InputStream = stream_factory  # type: ignore[attr-defined]

        entry = _wasapi_input_entry(index=18, channels=1, rate=48_000)
        src = SoundDeviceInputSource(
            device_id=18,
            sample_rate=16_000,
            tuning=VoiceTuningConfig(capture_wasapi_auto_convert=False),
            sd_module=sd,
            enumerate_fn=lambda: [entry],
        )
        info = await src.open()
        assert isinstance(info, AudioSourceInfo)
        assert info.device_id == 18  # noqa: PLR2004
        assert info.device_name == "FakeMic"
        assert info.sample_rate == 16_000  # noqa: PLR2004
        assert info.channels == 1
        assert len(captured) == 1
        assert captured[0]["device"] == 18  # noqa: PLR2004
        await src.close()

    @pytest.mark.asyncio()
    async def test_open_raises_when_no_input_devices_available(self) -> None:
        sd = _fake_sd()
        src = SoundDeviceInputSource(
            device_id=None,
            sample_rate=16_000,
            sd_module=sd,
            enumerate_fn=lambda: [],
        )
        with pytest.raises(AudioSourceError) as exc_info:
            await src.open()
        assert exc_info.value.code == ErrorCode.DEVICE_NOT_FOUND

    @pytest.mark.asyncio()
    async def test_open_maps_stream_open_error_to_audio_source_error(self) -> None:
        sd = _fake_sd()
        sd.InputStream = MagicMock(  # type: ignore[attr-defined]
            side_effect=RuntimeError("Unanticipated host error: 'AUDCLNT_E_UNSUPPORTED_FORMAT'"),
        )
        entry = _wasapi_input_entry(index=18, channels=1, rate=48_000)
        src = SoundDeviceInputSource(
            device_id=18,
            sample_rate=16_000,
            tuning=VoiceTuningConfig(capture_wasapi_auto_convert=False),
            sd_module=sd,
            enumerate_fn=lambda: [entry],
        )
        with pytest.raises(AudioSourceError) as exc_info:
            await src.open()
        assert exc_info.value.code == ErrorCode.UNSUPPORTED_FORMAT

    @pytest.mark.asyncio()
    async def test_open_falls_back_to_os_default_for_unknown_device_id(self) -> None:
        sd = _fake_sd()
        sd.InputStream = MagicMock(return_value=MagicMock())  # type: ignore[attr-defined]
        default = _wasapi_input_entry(index=5, channels=1, rate=48_000, name="Default")
        other = _wasapi_input_entry(index=9, channels=1, rate=48_000, name="Other")
        # Only the first is OS default — the other is not.
        other_entry = DeviceEntry(
            index=9,
            name="Other",
            canonical_name="other",
            host_api_index=other.host_api_index,
            host_api_name=other.host_api_name,
            max_input_channels=1,
            max_output_channels=0,
            default_samplerate=48_000,
            is_os_default=False,
        )
        src = SoundDeviceInputSource(
            device_id=99,  # nonexistent
            sample_rate=16_000,
            tuning=VoiceTuningConfig(capture_wasapi_auto_convert=False),
            sd_module=sd,
            enumerate_fn=lambda: [default, other_entry],
        )
        info = await src.open()
        assert info.device_name == "Default"
        await src.close()
