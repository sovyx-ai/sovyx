"""Tests for :mod:`sovyx.voice._capture_task`.

Uses dependency injection (``sd_module=fake_sd, enumerate_fn=...``) to
avoid ``sys.modules["sounddevice"]`` patching — see CLAUDE.md
§anti-pattern #2 for why the aliased-import pattern makes that fragile.
"""

from __future__ import annotations

import asyncio
from types import ModuleType
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import numpy as np
import pytest

from sovyx.engine.config import VoiceTuningConfig
from sovyx.voice._capture_task import AudioCaptureTask, _extract_peak_db, _resolve_input_entry
from sovyx.voice.device_enum import DeviceEntry


def _input_entry(
    *,
    index: int = 18,
    name: str = "FakeMic",
    host_api: str = "Windows WASAPI",
    rate: int = 48_000,
    channels: int = 1,
    is_default: bool = True,
) -> DeviceEntry:
    return DeviceEntry(
        index=index,
        name=name,
        canonical_name=name.strip().lower()[:30],
        host_api_index=3,
        host_api_name=host_api,
        max_input_channels=channels,
        max_output_channels=0,
        default_samplerate=rate,
        is_os_default=is_default,
    )


def _fake_sd() -> ModuleType:
    """Minimal ``sounddevice`` stand-in for :class:`AudioCaptureTask` DI.

    The real ``AudioCaptureTask`` (a) constructs ``sd.InputStream(...)``
    through the opener, (b) catches ``sd.PortAudioError`` in the consume
    loop. Everything else is accessed via the returned stream mock.
    """
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
    return module


def _tuning_no_wasapi_extra() -> VoiceTuningConfig:
    """Disable WASAPI ``auto_convert`` so InputStream kwargs stay minimal.

    Keeps assertions on ``call_args.kwargs`` robust — otherwise the
    first pyramid combo carries ``extra_settings`` and then falls back.
    """
    return VoiceTuningConfig(
        capture_wasapi_auto_convert=False,
        capture_allow_channel_upgrade=False,
    )


# ---------------------------------------------------------------------------
# _resolve_input_entry + _extract_peak_db — pure helpers
# ---------------------------------------------------------------------------


class TestResolveInputEntry:
    """The capture-task resolver handles int/str/None selectors."""

    def test_resolves_by_int_index(self) -> None:
        a = _input_entry(index=3, name="A", is_default=False)
        b = _input_entry(index=7, name="B", is_default=True)
        got = _resolve_input_entry(
            input_device=3,
            enumerate_fn=lambda: [a, b],
            host_api_name=None,
        )
        assert got.index == 3  # noqa: PLR2004

    def test_resolves_by_canonical_name_and_host_api(self) -> None:
        wasapi = _input_entry(index=3, name="Razer", host_api="Windows WASAPI")
        mme = _input_entry(index=4, name="Razer", host_api="MME", is_default=False)
        got = _resolve_input_entry(
            input_device="Razer",
            enumerate_fn=lambda: [mme, wasapi],
            host_api_name="Windows WASAPI",
        )
        assert got.index == 3  # noqa: PLR2004
        assert got.host_api_name == "Windows WASAPI"

    def test_falls_back_to_os_default_when_int_unknown(self) -> None:
        other = _input_entry(index=1, name="Other", is_default=False)
        default = _input_entry(index=9, name="Default", is_default=True)
        got = _resolve_input_entry(
            input_device=99,
            enumerate_fn=lambda: [other, default],
            host_api_name=None,
        )
        assert got.index == 9  # noqa: PLR2004

    def test_raises_when_no_inputs_available(self) -> None:
        with pytest.raises(RuntimeError, match="No audio input devices"):
            _resolve_input_entry(
                input_device=None,
                enumerate_fn=lambda: [],
                host_api_name=None,
            )


class TestExtractPeakDb:
    """The silence-detail parser handles well-formed and malformed inputs."""

    def test_parses_silence_detail(self) -> None:
        detail = "silent stream (peak -96.0 dBFS < threshold -80.0 dBFS)"
        assert _extract_peak_db(detail) == -96.0  # noqa: PLR2004

    def test_handles_empty_detail(self) -> None:
        assert _extract_peak_db("") < -100.0  # noqa: PLR2004
        assert _extract_peak_db(None) < -100.0  # noqa: PLR2004

    def test_handles_no_peak_match(self) -> None:
        assert _extract_peak_db("device unavailable") < -100.0  # noqa: PLR2004


# ---------------------------------------------------------------------------
# Lifecycle — through the opener, with DI-injected fake sounddevice
# ---------------------------------------------------------------------------


class TestAudioCaptureTaskLifecycle:
    """start/stop lifecycle of the capture task."""

    @pytest.mark.asyncio()
    async def test_start_opens_stream_and_spawns_consumer(self) -> None:
        pipeline = MagicMock()
        pipeline.feed_frame = AsyncMock()

        sd = _fake_sd()
        stream = MagicMock()
        sd.InputStream = MagicMock(return_value=stream)  # type: ignore[attr-defined]
        entry = _input_entry(index=5, rate=16_000)

        task = AudioCaptureTask(
            pipeline,
            input_device=5,
            validate_on_start=False,
            tuning=_tuning_no_wasapi_extra(),
            sd_module=sd,
            enumerate_fn=lambda: [entry],
        )
        try:
            await task.start()
            assert task.is_running is True
            assert task.input_device == 5  # noqa: PLR2004
            kwargs = sd.InputStream.call_args.kwargs  # type: ignore[attr-defined]
            assert kwargs["device"] == 5  # noqa: PLR2004
            assert kwargs["dtype"] == "int16"
            assert kwargs["samplerate"] == 16_000  # noqa: PLR2004
            stream.start.assert_called_once()
        finally:
            await task.stop()

    @pytest.mark.asyncio()
    async def test_start_is_idempotent(self) -> None:
        pipeline = MagicMock()
        pipeline.feed_frame = AsyncMock()

        sd = _fake_sd()
        sd.InputStream = MagicMock(return_value=MagicMock())  # type: ignore[attr-defined]
        entry = _input_entry(index=5)

        task = AudioCaptureTask(
            pipeline,
            validate_on_start=False,
            tuning=_tuning_no_wasapi_extra(),
            sd_module=sd,
            enumerate_fn=lambda: [entry],
        )
        try:
            await task.start()
            await task.start()  # second call must be a no-op
            assert sd.InputStream.call_count == 1  # type: ignore[attr-defined]
        finally:
            await task.stop()

    @pytest.mark.asyncio()
    async def test_stop_closes_stream_and_cancels_task(self) -> None:
        pipeline = MagicMock()
        pipeline.feed_frame = AsyncMock()

        sd = _fake_sd()
        stream = MagicMock()
        sd.InputStream = MagicMock(return_value=stream)  # type: ignore[attr-defined]
        entry = _input_entry(index=5)

        task = AudioCaptureTask(
            pipeline,
            validate_on_start=False,
            tuning=_tuning_no_wasapi_extra(),
            sd_module=sd,
            enumerate_fn=lambda: [entry],
        )
        await task.start()
        await task.stop()
        assert task.is_running is False
        stream.stop.assert_called_once()
        stream.close.assert_called_once()

    @pytest.mark.asyncio()
    async def test_stop_when_not_started_is_noop(self) -> None:
        task = AudioCaptureTask(MagicMock())
        await task.stop()  # must not raise
        assert task.is_running is False


class TestAudioCaptureTaskFrameDelivery:
    """Frames from the sounddevice callback reach pipeline.feed_frame."""

    @pytest.mark.asyncio()
    async def test_callback_enqueues_and_consumer_delivers(self) -> None:
        pipeline = MagicMock()
        delivered: list[np.ndarray] = []

        async def capture_frame(frame: np.ndarray) -> dict[str, str]:
            delivered.append(frame)
            return {"state": "IDLE"}

        pipeline.feed_frame = capture_frame

        captured: dict[str, Any] = {}

        def stream_factory(**kwargs: Any) -> MagicMock:
            captured["cb"] = kwargs["callback"]
            return MagicMock()

        sd = _fake_sd()
        sd.InputStream = stream_factory  # type: ignore[attr-defined]
        entry = _input_entry(index=5)

        task = AudioCaptureTask(
            pipeline,
            validate_on_start=False,
            tuning=_tuning_no_wasapi_extra(),
            sd_module=sd,
            enumerate_fn=lambda: [entry],
        )
        try:
            await task.start()
            frame = np.arange(512, dtype=np.int16)
            cb = captured["cb"]
            cb(frame.reshape(-1, 1), 512, None, None)
            for _ in range(10):
                await asyncio.sleep(0)
                if delivered:
                    break
            assert len(delivered) == 1
            np.testing.assert_array_equal(delivered[0], frame)
        finally:
            await task.stop()

    @pytest.mark.asyncio()
    async def test_overflow_drops_oldest(self) -> None:
        pipeline = MagicMock()

        async def _block(_frame: np.ndarray) -> None:
            await asyncio.sleep(10)

        pipeline.feed_frame = _block

        captured: dict[str, Any] = {}

        def stream_factory(**kwargs: Any) -> MagicMock:
            captured["cb"] = kwargs["callback"]
            return MagicMock()

        sd = _fake_sd()
        sd.InputStream = stream_factory  # type: ignore[attr-defined]
        entry = _input_entry(index=5)

        task = AudioCaptureTask(
            pipeline,
            validate_on_start=False,
            tuning=_tuning_no_wasapi_extra(),
            sd_module=sd,
            enumerate_fn=lambda: [entry],
        )
        task._queue = asyncio.Queue(maxsize=2)  # noqa: SLF001
        try:
            await task.start()
            cb = captured["cb"]
            for i in range(5):
                frame = np.full(512, i, dtype=np.int16)
                cb(frame.reshape(-1, 1), 512, None, None)
                await asyncio.sleep(0)
            assert task._queue.qsize() <= 2  # noqa: PLR2004, SLF001
        finally:
            await task.stop()


class TestAudioCaptureTaskReconnect:
    """Device disconnection in the consume loop triggers reopen via the opener."""

    @pytest.mark.asyncio()
    async def test_port_audio_error_triggers_reopen(self, monkeypatch: pytest.MonkeyPatch) -> None:
        pipeline = MagicMock()
        call_count = {"n": 0}

        sd = _fake_sd()

        async def flaky_feed(frame: np.ndarray) -> dict[str, str]:  # noqa: ARG001
            call_count["n"] += 1
            if call_count["n"] == 1:
                raise sd.PortAudioError("device unplugged")  # type: ignore[attr-defined]
            return {"state": "IDLE"}

        pipeline.feed_frame = flaky_feed

        captured: dict[str, Any] = {}
        streams: list[MagicMock] = []

        def stream_factory(**kwargs: Any) -> MagicMock:
            stream = MagicMock()
            streams.append(stream)
            captured["cb"] = kwargs["callback"]
            return stream

        sd.InputStream = stream_factory  # type: ignore[attr-defined]
        entry = _input_entry(index=5)

        monkeypatch.setattr("sovyx.voice._capture_task._RECONNECT_DELAY_S", 0.0)

        task = AudioCaptureTask(
            pipeline,
            validate_on_start=False,
            tuning=_tuning_no_wasapi_extra(),
            sd_module=sd,
            enumerate_fn=lambda: [entry],
        )
        try:
            await task.start()
            cb = captured["cb"]
            frame = np.zeros(512, dtype=np.int16)
            cb(frame.reshape(-1, 1), 512, None, None)
            # asyncio.to_thread dispatch can be slow on CI — give the
            # close→sleep→open chain up to 5 s.
            for _ in range(500):
                await asyncio.sleep(0.01)
                if len(streams) >= 2:  # noqa: PLR2004
                    break
            assert len(streams) >= 2, "reconnect should have opened a fresh stream"  # noqa: PLR2004
        finally:
            await task.stop()


class TestCaptureEndToEndFrameNormalisation:
    """The end-to-end capture path resamples + downmixes + rewindows correctly.

    Regression for the silent-VAD bug: PortAudio delivered 48 kHz stereo
    blocks (WASAPI shared mode) and the capture task forwarded them
    unchanged. VAD (expects 16 kHz mono 512) silently rejected every
    frame. This test drives a 48 kHz / 2 ch callback and asserts the
    pipeline sees only ``(512,) int16`` frames at 16 kHz rate.
    """

    @pytest.mark.asyncio()
    async def test_48k_stereo_callback_reaches_pipeline_as_16k_mono_512(self) -> None:
        pipeline = MagicMock()
        delivered: list[np.ndarray] = []

        async def capture(frame: np.ndarray) -> dict[str, str]:
            delivered.append(frame)
            return {"state": "IDLE"}

        pipeline.feed_frame = capture

        captured: dict[str, Any] = {}

        sd = _fake_sd()

        # Model a Windows shared-mode mic whose mixer format is fixed at
        # 48 kHz / 2 ch. Any pyramid attempt for a different (rate, ch)
        # combo fails with PortAudioError, forcing the opener down to the
        # native variant — exactly the path that triggers resampling.
        def stream_factory(**kwargs: Any) -> MagicMock:
            if kwargs["samplerate"] != 48_000 or kwargs["channels"] != 2:  # noqa: PLR2004
                raise sd.PortAudioError(  # type: ignore[attr-defined]
                    "Invalid sample rate or channel count for this device",
                )
            captured["cb"] = kwargs["callback"]
            captured["samplerate"] = kwargs["samplerate"]
            captured["channels"] = kwargs["channels"]
            return MagicMock()

        sd.InputStream = stream_factory  # type: ignore[attr-defined]
        entry = _input_entry(index=5, rate=48_000, channels=2)

        task = AudioCaptureTask(
            pipeline,
            input_device=5,
            validate_on_start=False,
            tuning=VoiceTuningConfig(
                capture_wasapi_auto_convert=False,
                capture_allow_channel_upgrade=True,
            ),
            sd_module=sd,
            enumerate_fn=lambda: [entry],
        )
        try:
            await task.start()
            assert captured["samplerate"] == 48_000  # noqa: PLR2004
            assert captured["channels"] == 2  # noqa: PLR2004

            cb = captured["cb"]
            # 48 kHz / 2 ch block of 1536 samples = 32 ms → after resampling
            # to 16 kHz yields 512 samples → exactly one pipeline frame.
            t = np.linspace(0, 0.032, 1536, endpoint=False, dtype=np.float64)
            tone = (np.sin(2 * np.pi * 440 * t) * 10_000).astype(np.int16)
            stereo = np.column_stack([tone, tone])
            for _ in range(4):
                cb(stereo, 1536, None, None)
                await asyncio.sleep(0)
            for _ in range(20):
                await asyncio.sleep(0)
                if delivered:
                    break

            assert delivered, "at least one normalised frame must reach feed_frame"
            for frame in delivered:
                assert frame.shape == (512,), f"expected (512,) got {frame.shape}"
                assert frame.dtype == np.int16
        finally:
            await task.stop()


class TestSilenceValidatorModes:
    """``_validate_stream`` branches between presence-only and signal-gated."""

    @pytest.mark.asyncio()
    async def test_presence_mode_accepts_quiet_frames(self) -> None:
        """Default: any frames arriving = liveness, regardless of RMS level.

        Frames must arrive *after* validation starts so the drain step
        does not swallow them — simulating the PortAudio callback firing
        on schedule while the user is quiet.
        """
        task = AudioCaptureTask(
            MagicMock(),
            tuning=VoiceTuningConfig(
                capture_validation_require_signal=False,
                capture_validation_min_frames=2,
                capture_validation_seconds=0.5,
            ),
        )
        task._loop = asyncio.get_running_loop()  # noqa: SLF001

        async def feeder() -> None:
            silent = np.zeros(512, dtype=np.int16)
            for _ in range(3):
                await asyncio.sleep(0.02)
                await task._queue.put(silent)  # noqa: SLF001

        feeder_task = asyncio.create_task(feeder())
        try:
            peak_db = await task._validate_stream()  # noqa: SLF001
        finally:
            await feeder_task

        assert peak_db == 0.0  # presence-mode sentinel

    @pytest.mark.asyncio()
    async def test_presence_mode_returns_floor_when_no_frames(self) -> None:
        """No callback activity for the full window = floor RMS => opener rejects."""
        task = AudioCaptureTask(
            MagicMock(),
            tuning=VoiceTuningConfig(
                capture_validation_require_signal=False,
                capture_validation_min_frames=1,
                capture_validation_seconds=0.15,
            ),
        )
        task._loop = asyncio.get_running_loop()  # noqa: SLF001

        peak_db = await task._validate_stream()  # noqa: SLF001

        assert peak_db < -100.0  # floor

    @pytest.mark.asyncio()
    async def test_signal_mode_rejects_silent_frames(self) -> None:
        """Opt-in signal mode still gates on capture_validation_min_rms_db."""
        task = AudioCaptureTask(
            MagicMock(),
            tuning=VoiceTuningConfig(
                capture_validation_require_signal=True,
                capture_validation_min_rms_db=-80.0,
                capture_validation_seconds=0.2,
            ),
        )
        task._loop = asyncio.get_running_loop()  # noqa: SLF001

        async def feeder() -> None:
            silent = np.zeros(512, dtype=np.int16)
            for _ in range(5):
                await asyncio.sleep(0.01)
                await task._queue.put(silent)  # noqa: SLF001

        feeder_task = asyncio.create_task(feeder())
        try:
            peak_db = await task._validate_stream()  # noqa: SLF001
        finally:
            await feeder_task

        assert peak_db < -80.0

    @pytest.mark.asyncio()
    async def test_signal_mode_accepts_loud_frames(self) -> None:
        """Opt-in signal mode accepts when RMS crosses the threshold."""
        task = AudioCaptureTask(
            MagicMock(),
            tuning=VoiceTuningConfig(
                capture_validation_require_signal=True,
                capture_validation_min_rms_db=-40.0,
                capture_validation_seconds=0.5,
            ),
        )
        task._loop = asyncio.get_running_loop()  # noqa: SLF001

        async def feeder() -> None:
            # Constant-amplitude signal at ~-10 dBFS — well above the threshold.
            samples = (np.ones(512, dtype=np.int16) * 10_000).astype(np.int16)
            await asyncio.sleep(0.01)
            await task._queue.put(samples)  # noqa: SLF001

        feeder_task = asyncio.create_task(feeder())
        try:
            peak_db = await task._validate_stream()  # noqa: SLF001
        finally:
            await feeder_task

        assert peak_db >= -40.0

    @pytest.mark.asyncio()
    async def test_drains_stale_frames_before_measuring(self) -> None:
        """Frames from a rejected pyramid variant must not count toward this validation."""
        task = AudioCaptureTask(
            MagicMock(),
            tuning=VoiceTuningConfig(
                capture_validation_require_signal=False,
                capture_validation_min_frames=2,
                capture_validation_seconds=0.15,
            ),
        )
        task._loop = asyncio.get_running_loop()  # noqa: SLF001
        # Stale frame from a previous rejected variant.
        stale = np.zeros(512, dtype=np.int16)
        await task._queue.put(stale)  # noqa: SLF001

        # No new frames will arrive; drain should wipe the stale one,
        # leaving the validator without any frames seen ⇒ floor RMS.
        peak_db = await task._validate_stream()  # noqa: SLF001

        assert peak_db < -100.0
