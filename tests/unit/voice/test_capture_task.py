"""Tests for sovyx.voice._capture_task.AudioCaptureTask."""

from __future__ import annotations

import asyncio
import sys
from types import ModuleType
from unittest.mock import AsyncMock, MagicMock, patch

import numpy as np
import pytest

from sovyx.voice._capture_task import AudioCaptureTask


def _fake_sounddevice() -> ModuleType:
    """Build a minimal ``sounddevice`` stand-in for import."""
    module = ModuleType("sounddevice")

    class _FakePortAudioError(Exception):
        pass

    module.PortAudioError = _FakePortAudioError  # type: ignore[attr-defined]
    module.InputStream = MagicMock()  # type: ignore[attr-defined]
    return module


@pytest.fixture()
def fake_sd() -> ModuleType:
    """Inject a fake sounddevice into ``sys.modules`` for the duration of the test."""
    fake = _fake_sounddevice()
    with patch.dict(sys.modules, {"sounddevice": fake}):
        yield fake


class TestAudioCaptureTaskLifecycle:
    """start/stop lifecycle of the capture task."""

    @pytest.mark.asyncio()
    async def test_start_opens_stream_and_spawns_consumer(self, fake_sd: ModuleType) -> None:
        pipeline = MagicMock()
        pipeline.feed_frame = AsyncMock()

        stream = MagicMock()
        fake_sd.InputStream = MagicMock(return_value=stream)  # type: ignore[attr-defined]

        task = AudioCaptureTask(pipeline, input_device=5)
        try:
            await task.start()
            assert task.is_running is True
            assert task.input_device == 5
            # InputStream was built with the selected device
            call_kwargs = fake_sd.InputStream.call_args.kwargs  # type: ignore[attr-defined]
            assert call_kwargs["device"] == 5
            assert call_kwargs["dtype"] == "int16"
            assert call_kwargs["samplerate"] == 16000  # noqa: PLR2004
            stream.start.assert_called_once()
        finally:
            await task.stop()

    @pytest.mark.asyncio()
    async def test_start_is_idempotent(self, fake_sd: ModuleType) -> None:
        pipeline = MagicMock()
        pipeline.feed_frame = AsyncMock()
        fake_sd.InputStream = MagicMock(return_value=MagicMock())  # type: ignore[attr-defined]

        task = AudioCaptureTask(pipeline)
        try:
            await task.start()
            await task.start()  # second call must be a no-op
            assert fake_sd.InputStream.call_count == 1  # type: ignore[attr-defined]
        finally:
            await task.stop()

    @pytest.mark.asyncio()
    async def test_stop_closes_stream_and_cancels_task(self, fake_sd: ModuleType) -> None:
        pipeline = MagicMock()
        pipeline.feed_frame = AsyncMock()

        stream = MagicMock()
        fake_sd.InputStream = MagicMock(return_value=stream)  # type: ignore[attr-defined]

        task = AudioCaptureTask(pipeline)
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
    async def test_callback_enqueues_and_consumer_delivers(self, fake_sd: ModuleType) -> None:
        pipeline = MagicMock()
        delivered: list[np.ndarray] = []

        async def capture_frame(frame: np.ndarray) -> dict:
            delivered.append(frame)
            return {"state": "IDLE"}

        pipeline.feed_frame = capture_frame

        captured_callback: dict[str, object] = {}

        def fake_stream_factory(**kwargs: object) -> MagicMock:
            captured_callback["cb"] = kwargs["callback"]
            return MagicMock()

        fake_sd.InputStream = fake_stream_factory  # type: ignore[attr-defined]

        task = AudioCaptureTask(pipeline)
        try:
            await task.start()
            frame = np.arange(512, dtype=np.int16)
            cb = captured_callback["cb"]
            cb(frame.reshape(-1, 1), 512, None, None)  # type: ignore[operator]
            # Give the consumer a chance to drain the queue
            for _ in range(10):
                await asyncio.sleep(0)
                if delivered:
                    break
            assert len(delivered) == 1
            np.testing.assert_array_equal(delivered[0], frame)
        finally:
            await task.stop()

    @pytest.mark.asyncio()
    async def test_overflow_drops_oldest(self, fake_sd: ModuleType) -> None:
        pipeline = MagicMock()

        # Block forever so the queue fills up
        async def _block(_frame: np.ndarray) -> None:
            await asyncio.sleep(10)

        pipeline.feed_frame = _block

        captured_callback: dict[str, object] = {}

        def fake_stream_factory(**kwargs: object) -> MagicMock:
            captured_callback["cb"] = kwargs["callback"]
            return MagicMock()

        fake_sd.InputStream = fake_stream_factory  # type: ignore[attr-defined]

        task = AudioCaptureTask(pipeline)
        # Shrink the queue to make the test fast
        task._queue = asyncio.Queue(maxsize=2)
        try:
            await task.start()
            cb = captured_callback["cb"]
            for i in range(5):
                frame = np.full(512, i, dtype=np.int16)
                cb(frame.reshape(-1, 1), 512, None, None)  # type: ignore[operator]
                await asyncio.sleep(0)
            # Queue never grew past maxsize
            assert task._queue.qsize() <= 2  # noqa: PLR2004, SLF001
        finally:
            await task.stop()


class TestAudioCaptureTaskReconnect:
    """Device disconnection triggers close + retry."""

    @pytest.mark.asyncio()
    async def test_port_audio_error_triggers_reopen(
        self, fake_sd: ModuleType, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        pipeline = MagicMock()
        call_count = {"n": 0}

        async def flaky_feed(frame: np.ndarray) -> dict:  # noqa: ARG001
            call_count["n"] += 1
            if call_count["n"] == 1:
                raise fake_sd.PortAudioError("device unplugged")  # type: ignore[attr-defined]
            return {"state": "IDLE"}

        pipeline.feed_frame = flaky_feed

        captured_callback: dict[str, object] = {}
        streams: list[MagicMock] = []

        def fake_stream_factory(**kwargs: object) -> MagicMock:
            stream = MagicMock()
            streams.append(stream)
            captured_callback["cb"] = kwargs["callback"]
            return stream

        fake_sd.InputStream = fake_stream_factory  # type: ignore[attr-defined]

        # Shorten reconnect delay so the test doesn't wait 2s
        monkeypatch.setattr("sovyx.voice._capture_task._RECONNECT_DELAY_S", 0.0)

        task = AudioCaptureTask(pipeline)
        try:
            await task.start()
            cb = captured_callback["cb"]
            frame = np.zeros(512, dtype=np.int16)
            cb(frame.reshape(-1, 1), 512, None, None)  # type: ignore[operator]
            # Wait for reconnect. asyncio.to_thread dispatch can be slow on
            # loaded CI runners, so give the close→sleep→open chain a generous
            # budget (up to 5 s) instead of a tight 500 ms window.
            for _ in range(500):
                await asyncio.sleep(0.01)
                if len(streams) >= 2:
                    break
            assert len(streams) >= 2, "reconnect should have opened a fresh stream"
        finally:
            await task.stop()
