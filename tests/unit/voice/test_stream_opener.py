"""Regression tests for :mod:`sovyx.voice._stream_opener` (Phase 0 — red-first).

These tests codify the failure modes reported by real users on Windows with
WASAPI-preferred devices whose shared-mode mixer format does not match
``channels=1, dtype=int16`` (e.g. Razer BlackShark V2 Pro). They assert the
behaviour of the *unified stream opener* introduced in Phase 1 — the module
does not exist yet when these tests are written, so the whole file is
expected to fail with :class:`ModuleNotFoundError` until Phase 1 lands.

Key invariants:

1. ``open_input_stream`` iterates a pyramid of
   (host_api × rate × channels × auto_convert) attempts and returns the
   first one that opens cleanly.
2. ``sd.WasapiSettings(auto_convert=True)`` is passed via ``extra_settings``
   only when the effective host API is ``"Windows WASAPI"``.
3. ``AUDCLNT_E_UNSUPPORTED_FORMAT`` and its numeric HRESULT variants are
   classified as :attr:`ErrorCode.UNSUPPORTED_FORMAT` (new enum value
   introduced in Phase 4).
4. Each opened stream is validated for ~600 ms before being handed back;
   a silent stream triggers the next pyramid step (parity with the
   production capture task).
5. Every attempt — succeeded or failed — is recorded in ``StreamInfo`` /
   ``StreamOpenError.attempts`` for observability.
"""

from __future__ import annotations

from types import ModuleType
from typing import Any
from unittest.mock import MagicMock

import numpy as np
import pytest

from sovyx.engine.config import VoiceTuningConfig
from sovyx.voice.device_enum import DeviceEntry
from sovyx.voice.device_test._protocol import ErrorCode


def _wasapi_entry(
    *,
    index: int = 18,
    channels: int = 2,
    rate: int = 48_000,
    name: str = "Microfone (Razer BlackShark V2 Pro)",
) -> DeviceEntry:
    return DeviceEntry(
        index=index,
        name=name,
        canonical_name=name.strip().lower()[:30],
        host_api_index=3,
        host_api_name="Windows WASAPI",
        max_input_channels=channels,
        max_output_channels=0,
        default_samplerate=rate,
        is_os_default=True,
    )


def _directsound_entry(
    *,
    index: int = 8,
    channels: int = 1,
    rate: int = 44_100,
    name: str = "Microfone (Razer BlackShark V2 Pro)",
) -> DeviceEntry:
    return DeviceEntry(
        index=index,
        name=name,
        canonical_name=name.strip().lower()[:30],
        host_api_index=1,
        host_api_name="Windows DirectSound",
        max_input_channels=channels,
        max_output_channels=0,
        default_samplerate=rate,
        is_os_default=False,
    )


def _mme_entry(
    *,
    index: int = 1,
    channels: int = 1,
    rate: int = 44_100,
) -> DeviceEntry:
    name = "Microfone (Razer BlackShark V2 "
    return DeviceEntry(
        index=index,
        name=name,
        canonical_name=name.strip().lower()[:30],
        host_api_index=0,
        host_api_name="MME",
        max_input_channels=channels,
        max_output_channels=0,
        default_samplerate=rate,
        is_os_default=False,
    )


def _fake_sd_module() -> ModuleType:
    """Return a minimal ``sounddevice`` stand-in suitable for DI."""
    module = ModuleType("sounddevice")

    class _FakePortAudioError(Exception):
        pass

    class _FakeWasapiSettings:
        def __init__(
            self,
            *,
            exclusive: bool = False,
            auto_convert: bool = False,
            explicit_sample_format: bool = False,
        ) -> None:
            self.exclusive = exclusive
            self.auto_convert = auto_convert
            self.explicit_sample_format = explicit_sample_format

    module.PortAudioError = _FakePortAudioError  # type: ignore[attr-defined]
    module.WasapiSettings = _FakeWasapiSettings  # type: ignore[attr-defined]
    module.InputStream = MagicMock()  # type: ignore[attr-defined]
    module.OutputStream = MagicMock()  # type: ignore[attr-defined]
    module.play = MagicMock()  # type: ignore[attr-defined]
    module.query_devices = MagicMock()  # type: ignore[attr-defined]
    return module


# ---------------------------------------------------------------------------
# Phase 4 — classifier knows AUDCLNT_* patterns
# ---------------------------------------------------------------------------


class TestErrorClassifierRecognisesAudclntFormat:
    """The Windows WASAPI-specific format error must map to a dedicated code.

    Raw PortAudio exception text (observed in production):
        ``"Error starting stream: Unanticipated host error [PaErrorCode -9999]:
        'AUDCLNT_E_UNSUPPORTED_FORMAT' [Windows WASAPI error -2004287480]"``

    Hitting the generic ``INTERNAL_ERROR`` bucket hides an actionable UX —
    the frontend cannot offer the "change mic format in Windows Sound
    settings" hint if it does not know the axis that failed.
    """

    def test_audclnt_unsupported_format_maps_to_unsupported_format(self) -> None:
        from sovyx.voice.device_test._source import _classify_portaudio_error

        exc = RuntimeError(
            "Error starting stream: Unanticipated host error [PaErrorCode -9999]: "
            "'AUDCLNT_E_UNSUPPORTED_FORMAT' [Windows WASAPI error -2004287480]",
        )
        out = _classify_portaudio_error(exc)
        assert out.code == ErrorCode.UNSUPPORTED_FORMAT

    def test_audclnt_exclusive_mode_maps_to_device_busy(self) -> None:
        from sovyx.voice.device_test._source import _classify_portaudio_error

        exc = RuntimeError("'AUDCLNT_E_EXCLUSIVE_MODE_NOT_ALLOWED'")
        out = _classify_portaudio_error(exc)
        assert out.code == ErrorCode.DEVICE_BUSY

    def test_audclnt_device_in_use_maps_to_device_busy(self) -> None:
        from sovyx.voice.device_test._source import _classify_portaudio_error

        exc = RuntimeError("'AUDCLNT_E_DEVICE_IN_USE'")
        out = _classify_portaudio_error(exc)
        assert out.code == ErrorCode.DEVICE_BUSY

    def test_audclnt_buffer_size_error_maps_to_buffer_size_invalid(self) -> None:
        from sovyx.voice.device_test._source import _classify_portaudio_error

        exc = RuntimeError("'AUDCLNT_E_BUFFER_SIZE_ERROR'")
        out = _classify_portaudio_error(exc)
        assert out.code == ErrorCode.BUFFER_SIZE_INVALID

    def test_hresult_numeric_variant_maps_to_unsupported_format(self) -> None:
        """Some PortAudio builds surface only the numeric HRESULT, not the macro name."""
        from sovyx.voice.device_test._source import _classify_portaudio_error

        exc = RuntimeError("Windows WASAPI error -2004287480 (0x88890008)")
        out = _classify_portaudio_error(exc)
        assert out.code == ErrorCode.UNSUPPORTED_FORMAT


# ---------------------------------------------------------------------------
# Phase 1 — unified opener: WASAPI auto_convert is always attempted first
# ---------------------------------------------------------------------------


class TestOpenInputStreamWasapiAutoConvert:
    """When host API is WASAPI, the opener must offer auto_convert to PortAudio."""

    @pytest.mark.asyncio()
    async def test_auto_convert_passed_on_wasapi_first_attempt(self) -> None:
        from sovyx.voice._stream_opener import open_input_stream

        sd = _fake_sd_module()
        captured: list[dict[str, Any]] = []

        def stream_factory(**kwargs: Any) -> MagicMock:
            captured.append(kwargs)
            stream = MagicMock()
            stream.start = MagicMock()
            return stream

        sd.InputStream = stream_factory  # type: ignore[attr-defined]

        tuning = VoiceTuningConfig()
        stream, info = await open_input_stream(
            device=_wasapi_entry(),
            target_rate=16_000,
            blocksize=512,
            callback=lambda *_, **__: None,
            tuning=tuning,
            sd_module=sd,
            enumerate_fn=lambda: [_wasapi_entry()],
            validate_fn=None,
        )

        assert len(captured) == 1
        kwargs = captured[0]
        assert "extra_settings" in kwargs
        settings = kwargs["extra_settings"]
        assert settings is not None
        assert settings.auto_convert is True
        assert info.host_api == "Windows WASAPI"
        assert info.auto_convert_used is True
        assert info.fallback_depth == 0
        # Cleanup — opener should return a real stream object.
        stream.stop()  # type: ignore[attr-defined]

    @pytest.mark.asyncio()
    async def test_auto_convert_disabled_when_tuning_opts_out(self) -> None:
        """Operators can force-disable auto_convert for buggy drivers."""
        from sovyx.voice._stream_opener import open_input_stream

        sd = _fake_sd_module()
        captured: list[dict[str, Any]] = []

        def stream_factory(**kwargs: Any) -> MagicMock:
            captured.append(kwargs)
            return MagicMock()

        sd.InputStream = stream_factory  # type: ignore[attr-defined]

        tuning = VoiceTuningConfig(capture_wasapi_auto_convert=False)
        _stream, info = await open_input_stream(
            device=_wasapi_entry(channels=1, rate=16_000),
            target_rate=16_000,
            blocksize=512,
            callback=lambda *_, **__: None,
            tuning=tuning,
            sd_module=sd,
            enumerate_fn=lambda: [_wasapi_entry(channels=1, rate=16_000)],
            validate_fn=None,
        )
        first = captured[0]
        assert first.get("extra_settings") is None
        assert info.auto_convert_used is False

    @pytest.mark.asyncio()
    async def test_auto_convert_not_passed_on_directsound(self) -> None:
        """WasapiSettings must never leak to non-WASAPI host APIs — PortAudio rejects it."""
        from sovyx.voice._stream_opener import open_input_stream

        sd = _fake_sd_module()
        captured: list[dict[str, Any]] = []

        def stream_factory(**kwargs: Any) -> MagicMock:
            captured.append(kwargs)
            return MagicMock()

        sd.InputStream = stream_factory  # type: ignore[attr-defined]

        tuning = VoiceTuningConfig()
        await open_input_stream(
            device=_directsound_entry(channels=1, rate=16_000),
            target_rate=16_000,
            blocksize=512,
            callback=lambda *_, **__: None,
            tuning=tuning,
            sd_module=sd,
            enumerate_fn=lambda: [_directsound_entry(channels=1, rate=16_000)],
            validate_fn=None,
        )
        assert captured[0].get("extra_settings") is None


# ---------------------------------------------------------------------------
# Phase 1 — pyramid: host_api × rate × channels × auto_convert
# ---------------------------------------------------------------------------


class TestOpenInputStreamPyramidFallback:
    """The real-world Razer BlackShark V2 Pro failure mode, end-to-end."""

    @pytest.mark.asyncio()
    async def test_wasapi_stereo_rejects_channels_1_then_retries_channels_2(self) -> None:
        """Channel-count upgrade must recover AUDCLNT_E_UNSUPPORTED_FORMAT."""
        from sovyx.voice._stream_opener import open_input_stream

        sd = _fake_sd_module()
        attempts: list[dict[str, Any]] = []

        def stream_factory(**kwargs: Any) -> MagicMock:
            attempts.append(kwargs)
            if kwargs["channels"] == 1:
                raise RuntimeError(
                    "Unanticipated host error [PaErrorCode -9999]: 'AUDCLNT_E_UNSUPPORTED_FORMAT'",
                )
            stream = MagicMock()
            stream.start = MagicMock()
            return stream

        sd.InputStream = stream_factory  # type: ignore[attr-defined]

        tuning = VoiceTuningConfig(capture_wasapi_auto_convert=False)
        _stream, info = await open_input_stream(
            device=_wasapi_entry(channels=2, rate=48_000),
            target_rate=16_000,
            blocksize=512,
            callback=lambda *_, **__: None,
            tuning=tuning,
            sd_module=sd,
            enumerate_fn=lambda: [_wasapi_entry(channels=2, rate=48_000)],
            validate_fn=None,
        )
        assert info.channels == 2
        assert info.host_api == "Windows WASAPI"
        assert info.fallback_depth >= 1

    @pytest.mark.asyncio()
    async def test_rate_fallback_when_target_rate_rejected(self) -> None:
        """``Invalid sample rate`` → retry at ``default_samplerate``."""
        from sovyx.voice._stream_opener import open_input_stream

        sd = _fake_sd_module()
        attempts: list[int] = []

        def stream_factory(**kwargs: Any) -> MagicMock:
            attempts.append(int(kwargs["samplerate"]))
            if kwargs["samplerate"] == 16_000:
                raise RuntimeError("Invalid sample rate [PaErrorCode -9997]")
            return MagicMock()

        sd.InputStream = stream_factory  # type: ignore[attr-defined]

        tuning = VoiceTuningConfig(capture_wasapi_auto_convert=False)
        entry = _wasapi_entry(channels=1, rate=48_000)
        _stream, info = await open_input_stream(
            device=entry,
            target_rate=16_000,
            blocksize=512,
            callback=lambda *_, **__: None,
            tuning=tuning,
            sd_module=sd,
            enumerate_fn=lambda: [entry],
            validate_fn=None,
        )
        assert info.sample_rate == 48_000
        assert 16_000 in attempts and 48_000 in attempts

    @pytest.mark.asyncio()
    async def test_host_api_fallback_when_wasapi_rejects_every_combo(self) -> None:
        """After exhausting WASAPI variants, opener walks to the next host API."""
        from sovyx.voice._stream_opener import open_input_stream

        sd = _fake_sd_module()
        attempts: list[str] = []

        def stream_factory(**kwargs: Any) -> MagicMock:
            # The fake factory keys on ``device`` index to identify host API.
            dev_idx = int(kwargs["device"])
            attempts.append(f"dev={dev_idx}")
            if dev_idx == 18:  # WASAPI variant — always fails
                raise RuntimeError(
                    "Unanticipated host error [PaErrorCode -9999]: 'AUDCLNT_E_UNSUPPORTED_FORMAT'",
                )
            return MagicMock()

        sd.InputStream = stream_factory  # type: ignore[attr-defined]

        tuning = VoiceTuningConfig(capture_wasapi_auto_convert=False)
        wasapi = _wasapi_entry(channels=2, rate=48_000)
        ds = _directsound_entry(channels=1, rate=44_100)
        _stream, info = await open_input_stream(
            device=wasapi,
            target_rate=16_000,
            blocksize=512,
            callback=lambda *_, **__: None,
            tuning=tuning,
            sd_module=sd,
            enumerate_fn=lambda: [wasapi, ds, _mme_entry()],
            validate_fn=None,
        )
        assert info.host_api == "Windows DirectSound"
        assert any("dev=18" in a for a in attempts), "should have tried WASAPI first"
        assert any("dev=8" in a for a in attempts), "should have escalated to DirectSound"

    @pytest.mark.asyncio()
    async def test_all_variants_fail_raises_aggregated_error(self) -> None:
        """When nothing opens, surface every attempt to the caller."""
        from sovyx.voice._stream_opener import StreamOpenError, open_input_stream

        sd = _fake_sd_module()
        sd.InputStream = MagicMock(  # type: ignore[attr-defined]
            side_effect=RuntimeError(
                "Unanticipated host error [PaErrorCode -9999]: 'AUDCLNT_E_UNSUPPORTED_FORMAT'",
            ),
        )

        tuning = VoiceTuningConfig(capture_wasapi_auto_convert=False)
        with pytest.raises(StreamOpenError) as exc_info:
            await open_input_stream(
                device=_wasapi_entry(channels=2, rate=48_000),
                target_rate=16_000,
                blocksize=512,
                callback=lambda *_, **__: None,
                tuning=tuning,
                sd_module=sd,
                enumerate_fn=lambda: [
                    _wasapi_entry(channels=2, rate=48_000),
                    _directsound_entry(),
                    _mme_entry(),
                ],
                validate_fn=None,
            )
        err = exc_info.value
        assert err.code == ErrorCode.UNSUPPORTED_FORMAT
        # Every viable (host_api, rate, channels) combo should be represented.
        assert len(err.attempts) >= 3
        # All attempts are failed.
        assert all(a.error_code is not None for a in err.attempts)


# ---------------------------------------------------------------------------
# Phase 5 — silence validation parity
# ---------------------------------------------------------------------------


class TestOpenInputStreamSilenceValidation:
    """A stream that opens but delivers zeros must trigger the next pyramid step."""

    @pytest.mark.asyncio()
    async def test_silent_wasapi_falls_back_to_directsound(self) -> None:
        from sovyx.voice._stream_opener import open_input_stream

        sd = _fake_sd_module()
        opened: list[int] = []

        def stream_factory(**kwargs: Any) -> MagicMock:
            opened.append(int(kwargs["device"]))
            stream = MagicMock()
            stream.start = MagicMock()
            return stream

        sd.InputStream = stream_factory  # type: ignore[attr-defined]

        async def validate(stream: Any, *, device_index: int) -> float:
            # WASAPI = silent; DirectSound = alive.
            return -120.0 if device_index == 18 else -30.0

        tuning = VoiceTuningConfig(capture_wasapi_auto_convert=False)
        wasapi = _wasapi_entry(channels=1, rate=16_000)
        ds = _directsound_entry(channels=1, rate=16_000)
        _stream, info = await open_input_stream(
            device=wasapi,
            target_rate=16_000,
            blocksize=512,
            callback=lambda *_, **__: None,
            tuning=tuning,
            sd_module=sd,
            enumerate_fn=lambda: [wasapi, ds],
            validate_fn=validate,
        )
        assert info.host_api == "Windows DirectSound"
        assert opened == [18, 8]


# ---------------------------------------------------------------------------
# Phase 5 — output opener parity
# ---------------------------------------------------------------------------


class TestOpenOutputStreamParity:
    """Speakers need the same pyramid: rate + channels + WASAPI auto_convert."""

    @pytest.mark.asyncio()
    async def test_output_retries_at_native_rate_with_auto_convert(self) -> None:
        from sovyx.voice._stream_opener import play_audio

        sd = _fake_sd_module()
        calls: list[tuple[int, int, Any]] = []

        def fake_play(audio: np.ndarray, *, samplerate: int, **kw: Any) -> None:
            calls.append((int(audio.size), samplerate, kw.get("extra_settings")))

        sd.play = fake_play  # type: ignore[attr-defined]

        speaker = DeviceEntry(
            index=15,
            name="Alto-falantes (Razer BlackShark V2 Pro)",
            canonical_name="alto-falantes (razer blacksha",
            host_api_index=3,
            host_api_name="Windows WASAPI",
            max_input_channels=0,
            max_output_channels=2,
            default_samplerate=48_000,
            is_os_default=True,
        )
        tuning = VoiceTuningConfig()
        audio = np.ones(24_000, dtype=np.int16)  # 1 s @ 24 kHz
        await play_audio(
            audio,
            source_rate=24_000,
            device=speaker,
            tuning=tuning,
            sd_module=sd,
            enumerate_fn=lambda: [speaker],
        )
        assert len(calls) == 1
        _size, rate, extra = calls[0]
        # With auto_convert the opener does not need to resample client-side;
        # if it does resample it must still pass WasapiSettings.
        assert rate in {24_000, 48_000}
        assert extra is not None and extra.auto_convert is True


# ---------------------------------------------------------------------------
# Phase 8 — observability counter for each open attempt
# ---------------------------------------------------------------------------


class TestStreamOpenAttemptsMetric:
    """Each ``OpenAttempt`` bumps the ``voice_stream_open_attempts`` counter.

    Guarantees the counter stays low-cardinality: ``host_api`` + ``auto_convert``
    + ``kind`` + ``result`` + ``error_code``. Device index / sample rate /
    channels are deliberately absent from labels (cardinality would explode).
    """

    @pytest.mark.asyncio()
    async def test_successful_input_open_records_ok_attempt(self) -> None:
        from sovyx.voice._stream_opener import open_input_stream

        sd = _fake_sd_module()
        stream = MagicMock()
        sd.InputStream = MagicMock(return_value=stream)  # type: ignore[attr-defined]
        entry = _wasapi_entry(index=18, channels=1, rate=48_000)

        recorded: list[dict[str, Any]] = []

        class _Counter:
            def add(self, value: int, *, attributes: dict[str, str]) -> None:
                recorded.append({"value": value, **attributes})

        class _Registry:
            voice_stream_open_attempts = _Counter()

        import sovyx.observability.metrics as metrics_mod

        def fake_get_metrics() -> _Registry:
            return _Registry()

        original = metrics_mod.get_metrics
        metrics_mod.get_metrics = fake_get_metrics  # type: ignore[assignment]
        try:
            _stream, _info = await open_input_stream(
                device=entry,
                target_rate=16_000,
                blocksize=512,
                callback=lambda *a, **kw: None,
                tuning=VoiceTuningConfig(capture_wasapi_auto_convert=False),
                sd_module=sd,
                enumerate_fn=lambda: [entry],
            )
        finally:
            metrics_mod.get_metrics = original  # type: ignore[assignment]

        assert len(recorded) >= 1
        ok_events = [r for r in recorded if r["result"] == "ok"]
        assert len(ok_events) == 1
        event = ok_events[0]
        assert event["host_api"] == "Windows WASAPI"
        assert event["kind"] == "input"
        assert event["auto_convert"] == "false"
        assert event["error_code"] == "none"
        # Cardinality guard.
        assert "device_index" not in event
        assert "sample_rate" not in event
        assert "channels" not in event
