"""Tests for :mod:`sovyx.voice._capture_task`.

Uses dependency injection (``sd_module=fake_sd, enumerate_fn=...``) to
avoid ``sys.modules["sounddevice"]`` patching — see CLAUDE.md
§anti-pattern #2 for why the aliased-import pattern makes that fragile.
"""

from __future__ import annotations

import asyncio
import contextlib
from types import ModuleType
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import numpy as np
import pytest

from sovyx.engine.config import VoiceTuningConfig
from sovyx.voice._capture_task import (
    AlsaHwDirectRestartVerdict,
    AudioCaptureTask,
    ExclusiveRestartVerdict,
    SessionManagerRestartVerdict,
    SharedRestartVerdict,
    _extract_peak_db,
    _resolve_input_entry,
)
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


class TestAudioCallbackUncaughtRaiseT130:
    """T1.30 — ``_audio_callback`` MUST swallow every exception.

    PortAudio invokes the callback on a dedicated audio thread that
    sounddevice manages. A raise propagating out of the callback puts
    sounddevice into ``CallbackAbort`` and stops the entire stream
    silently — the daemon goes deaf without a structured signal
    upstream. Post-T1.30 the body is wrapped in
    ``try/except BaseException``, the error is logged via
    ``voice.audio_callback.uncaught_raise``, and an empty marker
    frame is queued so the consumer's ``await self._queue.get()``
    unblocks (FrameNormalizer.push handles size==0 as a no-op).
    """

    @pytest.mark.asyncio()
    async def test_callback_swallows_unexpected_raise_and_queues_empty_marker(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Callback body raises (simulated via ``indata.copy()`` failure).
        The callback MUST return cleanly + the consumer's queue must
        receive an empty marker frame.
        """
        pipeline = MagicMock()
        pipeline.feed_frame = AsyncMock(return_value={"state": "IDLE"})

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
        # Stop the consumer loop from draining the queue so this test
        # can directly assert the empty marker landed.
        task._queue = asyncio.Queue(maxsize=8)  # noqa: SLF001

        try:
            await task.start()
            # Drain anything the start path may have queued (e.g. the
            # validation bootstrap) so the empty marker assertion below
            # is unambiguous.
            while not task._queue.empty():  # noqa: SLF001
                task._queue.get_nowait()  # noqa: SLF001

            cb = captured["cb"]
            bad_indata = MagicMock()
            bad_indata.copy.side_effect = RuntimeError("simulated callback failure")

            with caplog.at_level("ERROR", logger="sovyx.voice.capture._loop_mixin"):
                # MUST NOT raise — pre-T1.30 this would propagate the
                # RuntimeError up through PortAudio and CallbackAbort
                # the stream.
                cb(bad_indata, 512, None, None)

            # Drain the asyncio loop so the queued empty marker
            # materialises.
            for _ in range(10):
                await asyncio.sleep(0)
                if not task._queue.empty():  # noqa: SLF001
                    break

            # Empty marker frame queued.
            frame = task._queue.get_nowait()  # noqa: SLF001
            assert frame.size == 0, (
                f"expected empty marker frame on the error path, got size {frame.size}"
            )
            assert frame.dtype == np.int16

            # Structured error event logged.
            error_records = [
                r
                for r in caplog.records
                if isinstance(r.msg, dict)
                and r.msg.get("event") == "voice.audio_callback.uncaught_raise"
            ]
            assert len(error_records) == 1
            payload = error_records[0].msg
            assert payload["error_type"] == "RuntimeError"
            assert "simulated callback failure" in str(payload["error"])
        finally:
            await task.stop()


class TestAudioCallbackChaosInjectionT633:
    """Phase 6 / T6.33 — sustained random BaseException injection.

    The pre-T6.33 ``TestAudioCallbackUncaughtRaiseT130`` covered ONE
    callback raise with explicit assertions. Production reality is
    SUSTAINED chaos: USB driver glitches deliver malformed status
    objects, low-level numpy operations occasionally fail under
    memory pressure, etc. T6.33 proves the callback survives
    randomised injection across many frames at the spec's 5 %
    rate, with diverse BaseException subclasses (including the
    ``BaseException`` extremes — ``KeyboardInterrupt`` /
    ``SystemExit`` — that ``except BaseException`` MUST catch but
    that ``except Exception`` would let propagate).

    Seeded ``random.Random`` for determinism — same discipline as
    the T6.31 / T6.32 cascade chaos tests.
    """

    @pytest.mark.asyncio()
    async def test_sustained_five_percent_raise_rate_keeps_stream_alive(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        # 200 callback invocations at 5 % injection → expected ~10
        # raises, ~190 healthy frames. Stream must NEVER receive an
        # exception (would CallbackAbort it). Empty markers queued
        # for each raise + healthy frames queued for each non-raise.
        import logging
        import random

        rng = random.Random(0)  # noqa: S311 — test-only RNG
        injection_rate = 0.05
        total_frames = 200
        expected_raises = 0
        expected_healthy = 0

        captured: dict[str, Any] = {}

        def stream_factory(**kwargs: Any) -> MagicMock:
            captured["cb"] = kwargs["callback"]
            return MagicMock()

        sd = _fake_sd()
        sd.InputStream = stream_factory  # type: ignore[attr-defined]
        entry = _input_entry(index=5)

        task = AudioCaptureTask(
            MagicMock(),
            validate_on_start=False,
            tuning=_tuning_no_wasapi_extra(),
            sd_module=sd,
            enumerate_fn=lambda: [entry],
        )
        task._queue = asyncio.Queue(maxsize=total_frames * 2)  # noqa: SLF001

        try:
            await task.start()
            # Cancel the consumer task so it doesn't drain the chaos
            # frames we're about to enqueue. We're testing the
            # callback's enqueueing contract, not the consumer's
            # downstream pipeline. Without this cancel the consumer
            # would race-drain the queue between callback firings,
            # leaving the assertion non-deterministic.
            consumer = task._consumer  # noqa: SLF001
            if consumer is not None:
                consumer.cancel()
                with contextlib.suppress(asyncio.CancelledError, BaseException):
                    await consumer
            # Drain the validation bootstrap so the chaos sequence
            # starts from an empty queue.
            while not task._queue.empty():  # noqa: SLF001
                task._queue.get_nowait()  # noqa: SLF001

            cb = captured["cb"]
            caplog.set_level(logging.ERROR, logger="sovyx.voice.capture._loop_mixin")

            for _ in range(total_frames):
                if rng.random() < injection_rate:
                    expected_raises += 1
                    bad_indata = MagicMock()
                    bad_indata.copy.side_effect = RuntimeError("chaos: copy failed")
                    # MUST NOT propagate — pre-T1.30 would CallbackAbort.
                    cb(bad_indata, 512, None, None)
                else:
                    expected_healthy += 1
                    good_indata = np.zeros(512, dtype=np.int16)
                    cb(good_indata, 512, None, None)

            # Drain pending threadsafe-call_soon callbacks.
            for _ in range(50):
                await asyncio.sleep(0)

            # Both empty markers (raise path) and zero-content frames
            # (healthy path with all-zero indata) reach the queue.
            # We can't distinguish them by content (both are size==X
            # but different X), so count by size.
            queued: list[Any] = []
            while not task._queue.empty():  # noqa: SLF001
                queued.append(task._queue.get_nowait())  # noqa: SLF001

            # Total queued = expected_raises (empty markers, size 0) +
            # expected_healthy (zero-content frames, size 512).
            empty_markers = [f for f in queued if f.size == 0]
            healthy_frames = [f for f in queued if f.size == 512]
            assert len(empty_markers) == expected_raises, (
                f"chaos delivered {expected_raises} raises but queue "
                f"has {len(empty_markers)} empty markers"
            )
            assert len(healthy_frames) == expected_healthy

            # Structured error event for every raise.
            error_records = [
                r
                for r in caplog.records
                if isinstance(r.msg, dict)
                and r.msg.get("event") == "voice.audio_callback.uncaught_raise"
            ]
            assert len(error_records) == expected_raises
            # 5 % of 200 with seed=0 produces an empirically-bounded
            # raise count. Sanity-check the rate is in a credible
            # neighbourhood (not all-injected, not zero-injected).
            assert 1 <= expected_raises <= 30
        finally:
            await task.stop()

    @pytest.mark.asyncio()
    @pytest.mark.parametrize(
        "exc_class",
        [
            RuntimeError,
            MemoryError,
            AttributeError,
            TypeError,
            ValueError,
            # ``BaseException`` subclasses outside ``Exception`` —
            # the load-bearing reason ``except BaseException`` (NOT
            # ``except Exception``) is in the production code.
            KeyboardInterrupt,
            SystemExit,
        ],
    )
    async def test_diverse_exception_classes_all_swallowed(
        self,
        exc_class: type[BaseException],
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        # T6.33 contract: ``except BaseException`` in the callback
        # catches ANY exception subclass. Property-test-ish coverage
        # via parametrized exception classes — pins that a future
        # refactor narrowing to ``except Exception`` would let
        # ``KeyboardInterrupt`` / ``SystemExit`` propagate to the
        # PortAudio thread (a real prod hazard during shutdown).
        import logging

        captured: dict[str, Any] = {}

        def stream_factory(**kwargs: Any) -> MagicMock:
            captured["cb"] = kwargs["callback"]
            return MagicMock()

        sd = _fake_sd()
        sd.InputStream = stream_factory  # type: ignore[attr-defined]
        entry = _input_entry(index=5)

        task = AudioCaptureTask(
            MagicMock(),
            validate_on_start=False,
            tuning=_tuning_no_wasapi_extra(),
            sd_module=sd,
            enumerate_fn=lambda: [entry],
        )
        task._queue = asyncio.Queue(maxsize=8)  # noqa: SLF001

        try:
            await task.start()
            while not task._queue.empty():  # noqa: SLF001
                task._queue.get_nowait()  # noqa: SLF001

            cb = captured["cb"]
            caplog.set_level(logging.ERROR, logger="sovyx.voice.capture._loop_mixin")

            bad_indata = MagicMock()
            bad_indata.copy.side_effect = exc_class("chaos injection")

            # MUST NOT propagate — even SystemExit / KeyboardInterrupt.
            cb(bad_indata, 512, None, None)

            # Drain pending threadsafe call_soon → wait until the empty
            # marker materialises in the queue. Mirrors the T1.30 break
            # pattern so the consumer task can't race ahead and drain
            # the marker before the assertion. ``feed_frame`` is a
            # plain MagicMock here (not AsyncMock) so awaiting it
            # raises TypeError in the consumer — the consumer's own
            # error handling stalls before the next ``get`` call,
            # giving us a stable snapshot of the queue.
            empty_marker_seen = False
            for _ in range(10):
                await asyncio.sleep(0)
                if not task._queue.empty():  # noqa: SLF001
                    empty_marker_seen = True
                    break
            assert empty_marker_seen, (
                f"empty marker did not materialise in queue for {exc_class.__name__}"
            )
            error_records = [
                r
                for r in caplog.records
                if isinstance(r.msg, dict)
                and r.msg.get("event") == "voice.audio_callback.uncaught_raise"
            ]
            assert len(error_records) == 1
            assert error_records[0].msg["error_type"] == exc_class.__name__
        finally:
            await task.stop()


class TestConsumerLoopHeartbeatDriftT131:
    """T1.31 — pin ``_maybe_emit_heartbeat`` against Windows clock drift.

    Master mission Phase 1 / T1.31 asked for swapping
    ``time.monotonic()`` for ``time.perf_counter()`` on Windows
    because the default Windows monotonic clock ticks at ~15.6 ms
    (CLAUDE.md anti-pattern #22). At HEAD the heartbeat interval is
    2.0 seconds (``_HEARTBEAT_INTERVAL_S = capture_heartbeat_interval_seconds``,
    defaults to 2.0 in :class:`VoiceTuningConfig`), so the worst-
    case 15.6 ms tick boundary represents 0.78 % drift — well
    within tolerance for an INFO-level diagnostic.

    The strict ``<`` comparison in
    ``_maybe_emit_heartbeat`` (``if now - self._last_heartbeat_monotonic < _HEARTBEAT_INTERVAL_S: return``)
    correctly fires when ``now`` reaches exactly the deadline.
    Same-tick repeated reads (the failure mode CLAUDE.md
    anti-pattern #24 calls out for sub-tick TTLs) are not a
    concern at 2.0-second granularity.

    These tests pin the contract so a future refactor can't
    silently regress (e.g. by tightening the interval to a
    sub-tick value or by flipping ``<`` to ``>``).
    """

    def _build_task(self) -> AudioCaptureTask:
        sd = _fake_sd()
        sd.InputStream = MagicMock(return_value=MagicMock())  # type: ignore[attr-defined]
        entry = _input_entry(index=5)
        return AudioCaptureTask(
            MagicMock(),
            validate_on_start=False,
            tuning=_tuning_no_wasapi_extra(),
            sd_module=sd,
            enumerate_fn=lambda: [entry],
        )

    def test_heartbeat_does_not_fire_before_interval_elapsed(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A read inside the interval window MUST NOT fire the
        heartbeat — the strict ``<`` comparison guarantees no early
        emission even when the monotonic clock advances at coarse
        15.6 ms ticks.
        """
        task = self._build_task()
        task._last_heartbeat_monotonic = 1000.0  # noqa: SLF001
        # 1.5s elapsed (well under the 2.0s default interval).
        with (
            patch("sovyx.voice.capture._loop_mixin.time.monotonic", return_value=1001.5),
            caplog.at_level("INFO", logger="sovyx.voice.capture._loop_mixin"),
        ):
            task._maybe_emit_heartbeat()  # noqa: SLF001
        assert not any(
            isinstance(r.msg, dict) and r.msg.get("event") == "audio_capture_heartbeat"
            for r in caplog.records
        )

    def test_heartbeat_fires_at_exact_interval_boundary(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Reading at exactly ``last + _HEARTBEAT_INTERVAL_S`` MUST
        fire the heartbeat. The comparison is ``now - last <
        _HEARTBEAT_INTERVAL_S`` (strict ``<``); ``2.0 < 2.0`` is
        ``False`` so the body runs. Coarse-clock systems where
        ``now`` lands a tick AFTER the deadline (e.g. 2.0156s on
        Windows) also fire — both cases verified.
        """
        from sovyx.voice._capture_task import _HEARTBEAT_INTERVAL_S

        task = self._build_task()
        task._last_heartbeat_monotonic = 1000.0  # noqa: SLF001

        # Exactly at the deadline.
        with (
            patch(
                "sovyx.voice.capture._loop_mixin.time.monotonic",
                return_value=1000.0 + _HEARTBEAT_INTERVAL_S,
            ),
            caplog.at_level("INFO", logger="sovyx.voice.capture._loop_mixin"),
        ):
            task._maybe_emit_heartbeat()  # noqa: SLF001
        events_at_boundary = [
            r.msg
            for r in caplog.records
            if isinstance(r.msg, dict) and r.msg.get("event") == "audio_capture_heartbeat"
        ]
        assert len(events_at_boundary) == 1, (
            f"heartbeat MUST fire at exact interval boundary "
            f"(now - last = {_HEARTBEAT_INTERVAL_S}s, comparison "
            f"is `<`); got {len(events_at_boundary)} events"
        )

    def test_heartbeat_fires_one_windows_tick_past_interval(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """One 15.6 ms Windows tick PAST the interval boundary MUST
        also fire — pins the Windows-coarse-clock case explicitly.
        Worst-case drift: ~0.78 % at 2.0 s interval, well within
        tolerance for INFO-level diagnostic.
        """
        from sovyx.voice._capture_task import _HEARTBEAT_INTERVAL_S

        task = self._build_task()
        task._last_heartbeat_monotonic = 1000.0  # noqa: SLF001

        # Worst-case Windows tick boundary: deadline + 15.6 ms.
        with (
            patch(
                "sovyx.voice.capture._loop_mixin.time.monotonic",
                return_value=1000.0 + _HEARTBEAT_INTERVAL_S + 0.0156,
            ),
            caplog.at_level("INFO", logger="sovyx.voice.capture._loop_mixin"),
        ):
            task._maybe_emit_heartbeat()  # noqa: SLF001
        windows_tick_events = [
            r.msg
            for r in caplog.records
            if isinstance(r.msg, dict) and r.msg.get("event") == "audio_capture_heartbeat"
        ]
        assert len(windows_tick_events) == 1, (
            "heartbeat MUST fire one Windows clock tick past the "
            "interval — 0.78 % drift on a 2.0 s interval is within "
            "tolerance for the INFO-level diagnostic"
        )


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

        # T1.4 step 9b — _consume_loop moved to LoopMixin and imports
        # _RECONNECT_DELAY_S directly from `voice/capture/_constants`.
        # The local binding in `_loop_mixin` is what the consume loop
        # reads, so the monkeypatch must target that path (CLAUDE.md
        # anti-pattern #20). Pre-fix this patched
        # `_capture_task._RECONNECT_DELAY_S` and was a silent no-op:
        # the test passed only because the 5 s poll window was wide
        # enough to cover the unpatched 2 s default + jitter.
        monkeypatch.setattr("sovyx.voice.capture._loop_mixin._RECONNECT_DELAY_S", 0.0)

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


class TestExclusiveRestart:
    """``request_exclusive_restart`` tears down + re-opens with exclusive=True.

    Regression for the Razer BlackShark V2 Pro + Windows Voice Clarity
    (VocaEffectPack) scenario — shared mode delivers a normalized signal
    that has been DSP'd to silence by the capture APO, so the deaf
    orchestrator asks us to re-open in WASAPI exclusive. Exclusive mode
    bypasses the entire APO chain at the IAudioClient level.
    """

    @pytest.mark.asyncio()
    async def test_restart_tears_down_and_reopens_with_exclusive_settings(self) -> None:
        """Happy path — opener honours exclusive=True ⇒ EXCLUSIVE_ENGAGED."""
        pipeline = MagicMock()
        pipeline.feed_frame = AsyncMock()

        streams: list[MagicMock] = []
        stream_kwargs: list[dict[str, Any]] = []

        def stream_factory(**kwargs: Any) -> MagicMock:
            stream = MagicMock()
            streams.append(stream)
            stream_kwargs.append(kwargs)
            return stream

        sd = _fake_sd()
        sd.InputStream = stream_factory  # type: ignore[attr-defined]
        entry = _input_entry(index=5, rate=16_000, host_api="Windows WASAPI")

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
            initial_stream = streams[0]

            result = await task.request_exclusive_restart()

            assert result.verdict is ExclusiveRestartVerdict.EXCLUSIVE_ENGAGED
            assert result.engaged is True
            assert result.host_api == "Windows WASAPI"
            assert result.device == 5  # noqa: PLR2004
            assert result.sample_rate == 16_000  # noqa: PLR2004
            assert result.detail is None
            assert len(streams) >= 2  # noqa: PLR2004
            initial_stream.stop.assert_called()
            initial_stream.close.assert_called()
            exclusive_calls = [
                kw for kw in stream_kwargs if getattr(kw.get("extra_settings"), "exclusive", False)
            ]
            assert len(exclusive_calls) == 1
            assert exclusive_calls[0]["device"] == 5  # noqa: PLR2004
        finally:
            await task.stop()

    @pytest.mark.asyncio()
    async def test_restart_noop_when_not_running(self) -> None:
        """Calling before ``start()`` returns NOT_RUNNING without side effects."""
        task = AudioCaptureTask(MagicMock())

        result = await task.request_exclusive_restart()

        assert result.verdict is ExclusiveRestartVerdict.NOT_RUNNING
        assert result.engaged is False
        assert result.detail == "capture task is not running"
        assert task.is_running is False

    @pytest.mark.asyncio()
    async def test_restart_returns_downgraded_when_wasapi_grants_shared(self) -> None:
        """Exclusive combo fails but shared fallback succeeds within the opener.

        Scenario: another app holds the device exclusively, so the
        ``exclusive=True`` combo raises. The opener's next combo in the
        same call (``exclusive=False``) opens cleanly — returning a
        stream whose ``info.exclusive_used=False``. The deaf-APO
        condition that triggered the request is unchanged, so the
        restart MUST surface this as DOWNGRADED_TO_SHARED, not as a
        successful engagement.
        """
        pipeline = MagicMock()
        pipeline.feed_frame = AsyncMock()

        streams: list[MagicMock] = []
        attempt: dict[str, int] = {"n": 0}

        def stream_factory(**kwargs: Any) -> MagicMock:  # noqa: ARG001
            attempt["n"] += 1
            # 1: initial shared start (ok).
            # 2: restart's exclusive combo (fail — device held elsewhere).
            # 3: restart's shared fallback within same opener (ok).
            if attempt["n"] == 2:  # noqa: PLR2004
                raise sd.PortAudioError("exclusive denied")  # type: ignore[attr-defined]
            stream = MagicMock()
            streams.append(stream)
            return stream

        sd = _fake_sd()
        sd.InputStream = stream_factory  # type: ignore[attr-defined]
        entry = _input_entry(index=5, rate=16_000, host_api="Windows WASAPI")

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

            result = await task.request_exclusive_restart()

            assert result.verdict is ExclusiveRestartVerdict.DOWNGRADED_TO_SHARED
            assert result.engaged is False
            assert result.host_api == "Windows WASAPI"
            assert result.device == 5  # noqa: PLR2004
            assert result.sample_rate == 16_000  # noqa: PLR2004
            assert result.detail is not None
            assert "shared mode" in result.detail
            assert len(streams) >= 2  # noqa: PLR2004
            assert task.is_running is True
        finally:
            await task.stop()

    @pytest.mark.asyncio()
    async def test_restart_open_failed_shared_fallback(self) -> None:
        """Every restart combo fails, but ``_reopen_stream_after_device_error`` recovers.

        Scenario: the restart's exclusive_tuning produces combos
        ``[(excl=True), (excl=False)]`` — both raise in the opener,
        which therefore raises ``StreamOpenError``. The except branch
        then calls :meth:`_reopen_stream_after_device_error` (with the
        non-exclusive base tuning — 1 combo) which opens cleanly. The
        pipeline stays alive but deaf → OPEN_FAILED_SHARED_FALLBACK.
        """
        pipeline = MagicMock()
        pipeline.feed_frame = AsyncMock()

        streams: list[MagicMock] = []
        attempt: dict[str, int] = {"n": 0}

        def stream_factory(**kwargs: Any) -> MagicMock:  # noqa: ARG001
            attempt["n"] += 1
            # 1: initial shared start (ok).
            # 2: restart exclusive combo (fail).
            # 3: restart shared combo within same opener (fail — opener
            #    raises StreamOpenError).
            # 4: _reopen_stream_after_device_error shared combo (ok).
            if attempt["n"] in (2, 3):
                raise sd.PortAudioError("device denied")  # type: ignore[attr-defined]
            stream = MagicMock()
            streams.append(stream)
            return stream

        sd = _fake_sd()
        sd.InputStream = stream_factory  # type: ignore[attr-defined]
        entry = _input_entry(index=5, rate=16_000, host_api="Windows WASAPI")

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

            result = await task.request_exclusive_restart()

            assert result.verdict is ExclusiveRestartVerdict.OPEN_FAILED_SHARED_FALLBACK
            assert result.engaged is False
            assert result.host_api == "Windows WASAPI"
            assert result.detail is not None
            assert "recovered into shared mode" in result.detail
            # Initial + shared-fallback stream.
            assert len(streams) == 2  # noqa: PLR2004
            assert task.is_running is True
        finally:
            await task.stop()

    @pytest.mark.asyncio()
    async def test_restart_open_failed_no_stream(self) -> None:
        """Exclusive open AND shared fallback both fail → OPEN_FAILED_NO_STREAM.

        Contract (v0.20.3 / ultrareview Bug 2): the terminal failure
        path MUST signal the consumer to exit so the supervisor can
        detect the dead state and rebuild. ``_consume_loop`` cannot
        self-recover from ``_stream=None`` — it would park on
        ``queue.get()`` forever since no callback is feeding it, and
        the ``sd.PortAudioError`` reconnect branch only fires from
        live-stream reads.
        """
        pipeline = MagicMock()
        pipeline.feed_frame = AsyncMock()

        streams: list[MagicMock] = []
        attempt: dict[str, int] = {"n": 0}

        def stream_factory(**kwargs: Any) -> MagicMock:  # noqa: ARG001
            attempt["n"] += 1
            # 1: initial shared start (ok).
            # 2+: every combo from the restart opener and the shared
            #     fallback raises — no stream is ever recovered.
            if attempt["n"] == 1:
                stream = MagicMock()
                streams.append(stream)
                return stream
            raise sd.PortAudioError("device gone")  # type: ignore[attr-defined]

        sd = _fake_sd()
        sd.InputStream = stream_factory  # type: ignore[attr-defined]
        entry = _input_entry(index=5, rate=16_000, host_api="Windows WASAPI")

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
            consumer_before = task._consumer
            assert consumer_before is not None

            result = await task.request_exclusive_restart()

            assert result.verdict is ExclusiveRestartVerdict.OPEN_FAILED_NO_STREAM
            assert result.engaged is False
            assert result.detail is not None
            assert "shared fallback" in result.detail
            # Only the initial stream opened successfully.
            assert len(streams) == 1
            # Shutdown signalling: running flipped off + consumer cancelled
            # so _consume_loop exits and the supervisor can detect the
            # dead state. Without this, the loop parks on queue.get().
            assert task.is_running is False
            # Give the event loop one tick so the cancellation lands.
            await asyncio.sleep(0)
            assert consumer_before.done() is True
        finally:
            await task.stop()

    @pytest.mark.asyncio()
    async def test_request_shared_restart_engages_shared_mode(self) -> None:
        """Shared revert path: reopen succeeds → SHARED_ENGAGED.

        Symmetric twin of the exclusive engagement path — the
        coordinator calls this when rolling back an ineffective bypass
        strategy so the pipeline returns to its pre-bypass state.
        """
        pipeline = MagicMock()
        pipeline.feed_frame = AsyncMock()

        streams: list[MagicMock] = []

        def stream_factory(**kwargs: Any) -> MagicMock:  # noqa: ARG001
            stream = MagicMock()
            streams.append(stream)
            return stream

        sd = _fake_sd()
        sd.InputStream = stream_factory  # type: ignore[attr-defined]
        entry = _input_entry(index=5, rate=16_000, host_api="Windows WASAPI")

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

            result = await task.request_shared_restart()

            assert result.verdict is SharedRestartVerdict.SHARED_ENGAGED
            assert result.engaged is True
            assert result.host_api == "Windows WASAPI"
            # Two streams: initial start + shared revert.
            assert len(streams) == 2  # noqa: PLR2004
            assert task.is_running is True
        finally:
            await task.stop()

    @pytest.mark.asyncio()
    async def test_request_shared_restart_not_running(self) -> None:
        """Calling before ``start()`` is a no-op returning NOT_RUNNING."""
        pipeline = MagicMock()
        pipeline.feed_frame = AsyncMock()
        sd = _fake_sd()

        task = AudioCaptureTask(
            pipeline,
            input_device=5,
            validate_on_start=False,
            tuning=_tuning_no_wasapi_extra(),
            sd_module=sd,
            enumerate_fn=lambda: [_input_entry(index=5)],
        )

        result = await task.request_shared_restart()

        assert result.verdict is SharedRestartVerdict.NOT_RUNNING
        assert result.engaged is False

    @pytest.mark.asyncio()
    async def test_request_shared_restart_open_failed_signals_shutdown(self) -> None:
        """Shared reopen fails → OPEN_FAILED_NO_STREAM + consumer signalled to exit.

        Contract (v0.20.3 / ultrareview Bug 2): once ``_close_stream()``
        has run and the replacement ``open_input_stream`` raises, the
        consume loop is irrecoverably parked on ``queue.get()`` —
        nothing can enqueue and the PortAudioError reconnect branch
        cannot fire without a live stream. The terminal path MUST set
        ``_running=False`` and cancel the consumer so upstream
        supervisors observe the dead state and rebuild the task.
        """
        pipeline = MagicMock()
        pipeline.feed_frame = AsyncMock()

        streams: list[MagicMock] = []
        attempt: dict[str, int] = {"n": 0}

        def stream_factory(**kwargs: Any) -> MagicMock:  # noqa: ARG001
            attempt["n"] += 1
            # 1: initial start succeeds.
            # 2+: the shared reopen and every combo in the opener's
            #     fallback pyramid raises — no stream is recovered.
            if attempt["n"] == 1:
                stream = MagicMock()
                streams.append(stream)
                return stream
            raise sd.PortAudioError("shared reopen failed")  # type: ignore[attr-defined]

        sd = _fake_sd()
        sd.InputStream = stream_factory  # type: ignore[attr-defined]
        entry = _input_entry(index=5, rate=16_000, host_api="Windows WASAPI")

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
            consumer_before = task._consumer
            assert consumer_before is not None

            result = await task.request_shared_restart()

            assert result.verdict is SharedRestartVerdict.OPEN_FAILED_NO_STREAM
            assert result.engaged is False
            assert result.detail is not None
            assert "shared reopen failed" in result.detail
            # Only the initial stream ever opened.
            assert len(streams) == 1
            # Shutdown signalling must land synchronously before the
            # verdict is returned, so supervisors see the dead state.
            assert task.is_running is False
            await asyncio.sleep(0)
            assert consumer_before.done() is True
        finally:
            await task.stop()

    @pytest.mark.asyncio()
    async def test_restart_emits_verdict_metric(self) -> None:
        """Each verdict increments the ``exclusive_restart.verdicts`` counter.

        The metric is load-bearing for the dashboard: without it, a
        deploy where 100 % of restarts silently land in
        DOWNGRADED_TO_SHARED looks identical to one where 100 % engage
        exclusive. The counter is how we detect the bad state at scale.
        """
        pipeline = MagicMock()
        pipeline.feed_frame = AsyncMock()

        streams: list[MagicMock] = []

        def stream_factory(**kwargs: Any) -> MagicMock:  # noqa: ARG001
            stream = MagicMock()
            streams.append(stream)
            return stream

        sd = _fake_sd()
        sd.InputStream = stream_factory  # type: ignore[attr-defined]
        entry = _input_entry(index=5, rate=16_000, host_api="Windows WASAPI")

        task = AudioCaptureTask(
            pipeline,
            input_device=5,
            validate_on_start=False,
            tuning=_tuning_no_wasapi_extra(),
            sd_module=sd,
            enumerate_fn=lambda: [entry],
        )

        counter = MagicMock()
        fake_registry = MagicMock()
        fake_registry.voice_capture_exclusive_restart_verdicts = counter

        try:
            await task.start()
            with patch(
                "sovyx.observability.metrics.get_metrics",
                return_value=fake_registry,
            ):
                result = await task.request_exclusive_restart()
            counter.add.assert_called_once()
            args, kwargs = counter.add.call_args
            assert args == (1,)
            attrs = kwargs["attributes"]
            assert attrs["verdict"] == result.verdict.value
            assert attrs["host_api"] == "Windows WASAPI"
            assert "platform" in attrs
        finally:
            await task.stop()


# ─────────────────────────────────────────────────────────────────────
# Phase 6 / T6.24 — untested verdict branches in restart paths
# ─────────────────────────────────────────────────────────────────────


class TestLinuxRestartVerdictGuards:
    """Pin the early-return guard branches in the Linux-specific restart
    methods that the existing capture-task suite never exercised
    directly. Coverage lived only at the bypass-strategy mapping layer
    (``test_linux_pipewire_direct_bypass.py``); the production-site
    branches in ``_restart_mixin.py`` had no direct tests.

    Branches pinned:

    * ``request_alsa_hw_direct_restart``: ``NOT_RUNNING`` (called
      before ``start()``), ``NOT_LINUX`` (running on win32 / darwin),
      ``NO_ALSA_SIBLING`` (Linux but no ALSA-host-API sibling
      enumerable for the current endpoint).
    * ``request_session_manager_restart``: ``NOT_RUNNING``,
      ``NOT_LINUX``.

    Branches not covered here (already covered by other tests):

    * ``ALSA_HW_ENGAGED`` / ``DOWNGRADED_TO_SESSION_MANAGER`` —
      ``test_request_alsa_hw_direct_restart_emits_apo_degraded_frame_tier_2``.
    * ``SESSION_MANAGER_ENGAGED`` / ``DOWNGRADED_TO_ALSA_HW`` —
      ``test_request_session_manager_restart_*`` (3 variants).
    * ``OPEN_FAILED_NO_STREAM`` — would require deep stream-opener
      mocking; out of T6.24 spec scope per ``feedback_no_speculation``.
    """

    @pytest.mark.asyncio()
    async def test_alsa_hw_direct_restart_returns_not_running_before_start(
        self,
    ) -> None:
        # Calling before ``start()`` is a documented no-op (idempotent
        # contract). Mirrors the exclusive/shared NOT_RUNNING guards.
        task = AudioCaptureTask(MagicMock())
        result = await task.request_alsa_hw_direct_restart()
        assert result.verdict is AlsaHwDirectRestartVerdict.NOT_RUNNING
        assert result.engaged is False
        assert result.detail == "capture task is not running"
        assert task.is_running is False

    @pytest.mark.asyncio()
    async def test_alsa_hw_direct_restart_returns_not_linux_on_win32(
        self,
    ) -> None:
        # Idempotent on non-Linux hosts — preserve the running stream
        # rather than thrashing the substrate. Detail field carries the
        # platform string so operators can verify the guard fired.
        sd = _fake_sd()
        sd.InputStream = lambda **_kwargs: MagicMock()  # type: ignore[attr-defined]
        entry = _input_entry(index=11, name="usbmic", host_api="Windows WASAPI")
        task = AudioCaptureTask(
            MagicMock(),
            input_device=11,
            host_api_name="Windows WASAPI",
            validate_on_start=False,
            tuning=_tuning_no_wasapi_extra(),
            sd_module=sd,
            enumerate_fn=lambda: [entry],
        )
        try:
            with patch("sovyx.voice.capture._restart_mixin.sys.platform", "win32"):
                await task.start()
                result = await task.request_alsa_hw_direct_restart()
            assert result.verdict is AlsaHwDirectRestartVerdict.NOT_LINUX
            assert result.engaged is False
            assert "win32" in result.detail
        finally:
            await task.stop()

    @pytest.mark.asyncio()
    async def test_alsa_hw_direct_restart_returns_no_alsa_sibling(self) -> None:
        # On Linux but the device enumeration has no ALSA-host-API
        # entry for the current endpoint — only PulseAudio. The guard
        # must short-circuit with NO_ALSA_SIBLING so the bypass
        # coordinator routes to the next strategy instead of opening
        # a half-baked stream.
        sd = _fake_sd()
        sd.InputStream = lambda **_kwargs: MagicMock()  # type: ignore[attr-defined]
        # Only PulseAudio entry — no ALSA sibling for canonical_name="usbmic".
        pulse_entry = _input_entry(index=11, name="usbmic", host_api="PulseAudio")
        task = AudioCaptureTask(
            MagicMock(),
            input_device=11,
            host_api_name="PulseAudio",
            validate_on_start=False,
            tuning=_tuning_no_wasapi_extra(),
            sd_module=sd,
            enumerate_fn=lambda: [pulse_entry],
        )
        try:
            with patch("sovyx.voice.capture._restart_mixin.sys.platform", "linux"):
                await task.start()
                result = await task.request_alsa_hw_direct_restart()
            assert result.verdict is AlsaHwDirectRestartVerdict.NO_ALSA_SIBLING
            assert result.engaged is False
            assert "no ALSA-host-API sibling" in result.detail
            # Stream stays running (bypass declined cleanly).
            assert task.is_running is True
        finally:
            await task.stop()

    @pytest.mark.asyncio()
    async def test_session_manager_restart_returns_not_running_before_start(
        self,
    ) -> None:
        task = AudioCaptureTask(MagicMock())
        result = await task.request_session_manager_restart()
        assert result.verdict is SessionManagerRestartVerdict.NOT_RUNNING
        assert result.engaged is False
        assert result.detail == "capture task is not running"
        assert task.is_running is False

    @pytest.mark.asyncio()
    async def test_session_manager_restart_returns_not_linux_on_win32(self) -> None:
        sd = _fake_sd()
        sd.InputStream = lambda **_kwargs: MagicMock()  # type: ignore[attr-defined]
        entry = _input_entry(index=11, name="usbmic", host_api="Windows WASAPI")
        task = AudioCaptureTask(
            MagicMock(),
            input_device=11,
            host_api_name="Windows WASAPI",
            validate_on_start=False,
            tuning=_tuning_no_wasapi_extra(),
            sd_module=sd,
            enumerate_fn=lambda: [entry],
        )
        try:
            with patch("sovyx.voice.capture._restart_mixin.sys.platform", "win32"):
                await task.start()
                result = await task.request_session_manager_restart()
            assert result.verdict is SessionManagerRestartVerdict.NOT_LINUX
            assert result.engaged is False
            assert "win32" in result.detail
        finally:
            await task.stop()


# ─────────────────────────────────────────────────────────────────────
# v1.3 §4.2 L4-B — mark-based tap contract
# ─────────────────────────────────────────────────────────────────────


class TestRingStatePackingInvariants:
    """Packed state must keep epoch and samples consistent across the
    single-atomic-assignment contract the :class:`CaptureTaskProto`
    readers depend on."""

    def _fresh_task(self) -> AudioCaptureTask:
        from sovyx.voice._capture_task import AudioCaptureTask

        task = AudioCaptureTask.__new__(AudioCaptureTask)
        task._ring_buffer = None  # noqa: SLF001
        task._ring_capacity = 0  # noqa: SLF001
        task._ring_write_index = 0  # noqa: SLF001
        task._ring_state = 0  # noqa: SLF001
        task._tuning = None  # noqa: SLF001
        return task

    def test_initial_mark_is_zero_zero(self) -> None:
        task = self._fresh_task()
        assert task.samples_written_mark() == (0, 0)

    def test_allocate_bumps_epoch_and_resets_samples(self) -> None:
        from sovyx.engine.config import VoiceTuningConfig

        task = self._fresh_task()
        task._allocate_ring_buffer(VoiceTuningConfig())  # noqa: SLF001
        first_mark = task.samples_written_mark()
        assert first_mark == (1, 0)

        # Simulate prior writes accumulating samples then re-allocate.
        from sovyx.voice._capture_task import _RING_EPOCH_SHIFT

        task._ring_state |= 999  # noqa: SLF001 — pretend 999 samples written
        task._allocate_ring_buffer(VoiceTuningConfig())  # noqa: SLF001
        second_mark = task.samples_written_mark()
        assert second_mark[0] == 2  # epoch bumped
        assert second_mark[1] == 0  # samples reset
        # And the internal state matches the decomposed pair.
        assert task._ring_state == (2 << _RING_EPOCH_SHIFT)  # noqa: SLF001

    def test_ring_write_updates_state_atomically(self) -> None:
        import numpy as np

        from sovyx.engine.config import VoiceTuningConfig

        task = self._fresh_task()
        task._allocate_ring_buffer(VoiceTuningConfig())  # noqa: SLF001
        task._ring_write(np.zeros(512, dtype=np.int16))  # noqa: SLF001
        mark = task.samples_written_mark()
        assert mark[0] == 1
        assert mark[1] == 512


class TestTapFramesSinceMark:
    """Mark/tap contract — pre-apply contamination is impossible and
    ring resets between mark and tap are handled gracefully."""

    @pytest.mark.asyncio()
    async def test_tap_waits_until_min_samples_accumulate(self) -> None:
        """With same epoch and insufficient samples, tap waits until
        either ``min_samples`` accumulate or ``max_wait_s`` expires."""
        import asyncio

        import numpy as np

        from sovyx.engine.config import VoiceTuningConfig
        from sovyx.voice._capture_task import AudioCaptureTask

        task = AudioCaptureTask.__new__(AudioCaptureTask)
        task._ring_buffer = np.zeros(16_000, dtype=np.int16)  # noqa: SLF001
        task._ring_capacity = 16_000  # noqa: SLF001
        task._ring_write_index = 0  # noqa: SLF001
        task._ring_state = 0  # noqa: SLF001
        task._tuning = VoiceTuningConfig(mark_tap_poll_interval_s=0.01)  # noqa: SLF001

        task._allocate_ring_buffer(task._tuning)  # epoch → 1  # noqa: SLF001
        mark = task.samples_written_mark()

        async def feed() -> None:
            await asyncio.sleep(0.02)
            task._ring_write(np.zeros(8_000, dtype=np.int16))  # noqa: SLF001

        feeder = asyncio.create_task(feed())
        frames = await task.tap_frames_since_mark(
            mark=mark,
            min_samples=8_000,
            max_wait_s=1.0,
        )
        await feeder
        assert frames.size == 8_000

    @pytest.mark.asyncio()
    async def test_tap_returns_on_deadline_without_enough_samples(self) -> None:
        import numpy as np

        from sovyx.engine.config import VoiceTuningConfig
        from sovyx.voice._capture_task import AudioCaptureTask

        task = AudioCaptureTask.__new__(AudioCaptureTask)
        task._ring_buffer = np.zeros(16_000, dtype=np.int16)  # noqa: SLF001
        task._ring_capacity = 16_000  # noqa: SLF001
        task._ring_write_index = 0  # noqa: SLF001
        task._ring_state = 0  # noqa: SLF001
        task._tuning = VoiceTuningConfig(mark_tap_poll_interval_s=0.01)  # noqa: SLF001

        task._allocate_ring_buffer(task._tuning)  # epoch → 1  # noqa: SLF001
        mark = task.samples_written_mark()

        frames = await task.tap_frames_since_mark(
            mark=mark,
            min_samples=8_000,
            max_wait_s=0.05,
        )
        # Deadline expired with zero new samples — empty array, never None.
        assert frames.size == 0

    @pytest.mark.asyncio()
    async def test_tap_epoch_mismatch_short_circuits(self) -> None:
        """An epoch advance between mark and tap indicates a ring reset;
        every sample now in the buffer is post-mark."""
        import numpy as np

        from sovyx.engine.config import VoiceTuningConfig
        from sovyx.voice._capture_task import AudioCaptureTask

        task = AudioCaptureTask.__new__(AudioCaptureTask)
        task._ring_buffer = np.zeros(16_000, dtype=np.int16)  # noqa: SLF001
        task._ring_capacity = 16_000  # noqa: SLF001
        task._ring_write_index = 0  # noqa: SLF001
        task._ring_state = 0  # noqa: SLF001
        task._tuning = VoiceTuningConfig()  # noqa: SLF001

        task._allocate_ring_buffer(task._tuning)  # noqa: SLF001
        mark = task.samples_written_mark()
        # Reset happens between mark and tap — new epoch, empty ring.
        task._allocate_ring_buffer(task._tuning)  # noqa: SLF001

        frames = await task.tap_frames_since_mark(
            mark=mark,
            min_samples=1_000,
            max_wait_s=0.5,
        )
        assert frames.size == 0  # Empty ring after reset → empty result.


# ---------------------------------------------------------------------------
# Band-aid #9 — sustained-underrun detection
# ---------------------------------------------------------------------------


def _bare_task_with_underrun_state(*, stream_id: str = "stream-0") -> AudioCaptureTask:
    """Construct an :class:`AudioCaptureTask` with only the state the
    sustained-underrun monitor depends on, bypassing __init__.

    Mirrors the ring-buffer tests' bypass pattern (``__new__`` + manual
    state) so the underrun rate-check can be exercised without the
    full lifecycle (no event loop, no PortAudio, no consumer task)."""
    task = AudioCaptureTask.__new__(AudioCaptureTask)
    task._stream_id = stream_id  # noqa: SLF001
    task._resolved_device_name = "test-mic"  # noqa: SLF001
    task._stream_callback_frames = 0  # noqa: SLF001
    task._stream_underruns = 0  # noqa: SLF001
    task._underrun_window_started_at = None  # noqa: SLF001
    task._underrun_window_callbacks_at_start = 0  # noqa: SLF001
    task._underrun_window_underruns_at_start = 0  # noqa: SLF001
    task._last_underrun_warning_monotonic = None  # noqa: SLF001
    return task


class TestSustainedUnderrunDetection:
    """Band-aid #9: rolling-window WARN when underrun fraction sustains
    above the threshold. The audio thread only increments counters
    (anti-pattern #14); the consumer-loop helper computes the rate
    and emits the structured WARN with operator-actionable details."""

    def test_short_circuits_when_no_stream_open(self) -> None:
        """No active stream → no window start, no WARN."""
        task = _bare_task_with_underrun_state(stream_id="")
        # Even with high counters, no stream means no monitoring.
        task._stream_underruns = 1_000  # noqa: SLF001
        task._stream_callback_frames = 1_000  # noqa: SLF001
        task._check_sustained_underrun_rate()  # noqa: SLF001
        assert task._underrun_window_started_at is None  # noqa: SLF001

    def test_first_call_arms_window_no_warn(self) -> None:
        """First invocation snapshots state; cannot warn yet."""
        task = _bare_task_with_underrun_state()
        task._stream_callback_frames = 5  # noqa: SLF001
        task._check_sustained_underrun_rate()  # noqa: SLF001
        assert task._underrun_window_started_at is not None  # noqa: SLF001
        assert task._underrun_window_callbacks_at_start == 5  # noqa: SLF001
        assert task._last_underrun_warning_monotonic is None  # noqa: SLF001

    def test_window_not_yet_elapsed_no_warn(self) -> None:
        """Within the window, no rate computation runs."""
        task = _bare_task_with_underrun_state()
        # Arm the window.
        task._check_sustained_underrun_rate()  # noqa: SLF001
        armed_at = task._underrun_window_started_at  # noqa: SLF001
        # Same monotonic tick → no elapsed time → no roll, no warn.
        task._stream_callback_frames = 200  # noqa: SLF001
        task._stream_underruns = 100  # noqa: SLF001
        task._check_sustained_underrun_rate()  # noqa: SLF001
        # Window unchanged, no warn.
        assert task._underrun_window_started_at == armed_at  # noqa: SLF001
        assert task._last_underrun_warning_monotonic is None  # noqa: SLF001

    def test_below_min_callbacks_does_not_warn(self) -> None:
        """Tiny window (below MIN_CALLBACKS) cannot trip warn even at
        100% underrun rate — protects against false positives on a
        stream that's just opened."""
        from unittest.mock import patch

        task = _bare_task_with_underrun_state()
        # Arm window at t=0.
        with patch("sovyx.voice.capture._loop_mixin.time.monotonic", return_value=0.0):
            task._check_sustained_underrun_rate()  # noqa: SLF001
        # Roll the window AFTER 11 s (>10 s window) but only 10 callbacks
        # observed, all underruns. Below 50-callback floor → no warn.
        task._stream_callback_frames = 10  # noqa: SLF001
        task._stream_underruns = 10  # noqa: SLF001
        with patch("sovyx.voice.capture._loop_mixin.time.monotonic", return_value=11.0):
            task._check_sustained_underrun_rate()  # noqa: SLF001
        assert task._last_underrun_warning_monotonic is None  # noqa: SLF001

    def test_below_warn_fraction_does_not_warn(self) -> None:
        """Above the min-callbacks floor but underrun fraction below
        threshold → no warn."""
        from unittest.mock import patch

        task = _bare_task_with_underrun_state()
        with patch("sovyx.voice.capture._loop_mixin.time.monotonic", return_value=0.0):
            task._check_sustained_underrun_rate()  # noqa: SLF001
        # 100 callbacks, 1 underrun = 1% — below 5% threshold.
        task._stream_callback_frames = 100  # noqa: SLF001
        task._stream_underruns = 1  # noqa: SLF001
        with patch("sovyx.voice.capture._loop_mixin.time.monotonic", return_value=11.0):
            task._check_sustained_underrun_rate()  # noqa: SLF001
        assert task._last_underrun_warning_monotonic is None  # noqa: SLF001

    def test_sustained_underrun_emits_warn(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Above min-callbacks AND fraction ≥ threshold → warn fires."""
        import logging
        from unittest.mock import patch

        task = _bare_task_with_underrun_state()
        with patch("sovyx.voice.capture._loop_mixin.time.monotonic", return_value=0.0):
            task._check_sustained_underrun_rate()  # noqa: SLF001
        # 200 callbacks, 20 underruns = 10% — above 5% threshold.
        task._stream_callback_frames = 200  # noqa: SLF001
        task._stream_underruns = 20  # noqa: SLF001
        with (
            caplog.at_level(logging.WARNING),
            patch("sovyx.voice.capture._loop_mixin.time.monotonic", return_value=11.0),
        ):
            task._check_sustained_underrun_rate()  # noqa: SLF001
        assert task._last_underrun_warning_monotonic == 11.0  # noqa: SLF001
        events = [
            r.msg
            for r in caplog.records
            if isinstance(r.msg, dict)
            and r.msg.get("event") == "voice.audio.capture_sustained_underrun"
        ]
        assert len(events) == 1
        evt = events[0]
        assert evt["voice.stream_id"] == "stream-0"
        assert evt["voice.device_id"] == "test-mic"
        assert evt["voice.underruns_in_window"] == 20
        assert evt["voice.callbacks_in_window"] == 200
        assert evt["voice.underrun_fraction"] == 0.1
        assert "USB-bus bandwidth" in evt["voice.action_required"]

    def test_warn_rate_limited_per_stream(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """A second sustained breach within the rate-limit interval
        does NOT fire — operator gets one drumbeat per 30 s, not a
        flood."""
        import logging
        from unittest.mock import patch

        task = _bare_task_with_underrun_state()
        # Arm window.
        with patch("sovyx.voice.capture._loop_mixin.time.monotonic", return_value=0.0):
            task._check_sustained_underrun_rate()  # noqa: SLF001
        # First breach → fires.
        task._stream_callback_frames = 200  # noqa: SLF001
        task._stream_underruns = 20  # noqa: SLF001
        with caplog.at_level(logging.WARNING):
            with patch(
                "sovyx.voice.capture._loop_mixin.time.monotonic",
                return_value=11.0,
            ):
                task._check_sustained_underrun_rate()  # noqa: SLF001
            # Second breach 10 s later (still within 30 s rate-limit).
            task._stream_callback_frames = 400  # noqa: SLF001
            task._stream_underruns = 40  # noqa: SLF001
            with patch(
                "sovyx.voice.capture._loop_mixin.time.monotonic",
                return_value=22.0,
            ):
                task._check_sustained_underrun_rate()  # noqa: SLF001
        events = [
            r.msg
            for r in caplog.records
            if isinstance(r.msg, dict)
            and r.msg.get("event") == "voice.audio.capture_sustained_underrun"
        ]
        assert len(events) == 1  # Only the first WARN, not the second.

    def test_warn_fires_again_after_rate_limit_interval(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """After 30 s gap, a continuing breach DOES re-fire."""
        import logging
        from unittest.mock import patch

        task = _bare_task_with_underrun_state()
        with patch("sovyx.voice.capture._loop_mixin.time.monotonic", return_value=0.0):
            task._check_sustained_underrun_rate()  # noqa: SLF001
        task._stream_callback_frames = 200  # noqa: SLF001
        task._stream_underruns = 20  # noqa: SLF001
        with caplog.at_level(logging.WARNING):
            with patch("sovyx.voice.capture._loop_mixin.time.monotonic", return_value=11.0):
                task._check_sustained_underrun_rate()  # noqa: SLF001
            # 35 s after first WARN — past the 30 s rate-limit gap.
            task._stream_callback_frames = 400  # noqa: SLF001
            task._stream_underruns = 40  # noqa: SLF001
            with patch("sovyx.voice.capture._loop_mixin.time.monotonic", return_value=46.0):
                task._check_sustained_underrun_rate()  # noqa: SLF001
        events = [
            r.msg
            for r in caplog.records
            if isinstance(r.msg, dict)
            and r.msg.get("event") == "voice.audio.capture_sustained_underrun"
        ]
        assert len(events) == 2  # Both WARNs fired.

    def test_window_rolls_even_when_warn_skipped(self) -> None:
        """Below-threshold cycle still resets window state so the next
        window observes a fresh interval — no stale snapshot bleed."""
        from unittest.mock import patch

        task = _bare_task_with_underrun_state()
        with patch("sovyx.voice.capture._loop_mixin.time.monotonic", return_value=0.0):
            task._check_sustained_underrun_rate()  # noqa: SLF001
        task._stream_callback_frames = 100  # noqa: SLF001
        task._stream_underruns = 1  # 1% — below threshold.  # noqa: SLF001
        with patch("sovyx.voice.capture._loop_mixin.time.monotonic", return_value=11.0):
            task._check_sustained_underrun_rate()  # noqa: SLF001
        # Window snapshots advance even though no warn fired.
        assert task._underrun_window_started_at == 11.0  # noqa: SLF001
        assert task._underrun_window_callbacks_at_start == 100  # noqa: SLF001
        assert task._underrun_window_underruns_at_start == 1  # noqa: SLF001


class TestUnderrunStormT629:
    """Phase 6 / T6.29 — sustained underrun storm at 50+/sec.

    Existing ``TestSustainedUnderrunDetection`` covers detection
    LOGIC (window arming, threshold, rate-limiting) at the unit level.
    T6.29 stresses the system under STORM conditions: 500+ frames
    delivered with ``input_underflow=True`` to the audio callback,
    sustained over multiple rolling windows.

    Contracts pinned by stress:

    1. Counter accuracy at high rate — every flagged frame increments
       ``_stream_underruns`` exactly once. No drops, no double-counts.
    2. WARN rate-limited under storm — at 50+ underruns/sec sustained,
       only one ``voice.audio.capture_sustained_underrun`` per
       ``_CAPTURE_UNDERRUN_WARN_INTERVAL_S`` (30 s default). Logging
       a WARN per frame would itself starve the audio thread.
    3. Audio callback never raises — even under storm pressure (the
       T1.30 ``except BaseException`` is the safety net here too).
    4. Memory bounded — counters are integers; no accumulating data
       structures. Verified via direct attribute inspection.
    5. Stream stays alive — pre-T6.29 a synthetic underrun storm in
       a real-PortAudio environment could trigger a stream close;
       the in-process counters must not interact with stream
       lifecycle.
    """

    @pytest.mark.asyncio()
    async def test_storm_500_underruns_in_one_second_increments_counter_exactly(
        self,
    ) -> None:
        # Direct callback drive at storm rate. 500 frames with
        # input_underflow=True over a tight loop — verify counter
        # accuracy. The status object is a MagicMock with
        # ``input_underflow=True`` and ``input_overflow=False``;
        # the callback's ``getattr(status, "input_underflow", False)``
        # evaluates True for every call.
        captured: dict[str, Any] = {}

        def stream_factory(**kwargs: Any) -> MagicMock:
            captured["cb"] = kwargs["callback"]
            return MagicMock()

        sd = _fake_sd()
        sd.InputStream = stream_factory  # type: ignore[attr-defined]
        entry = _input_entry(index=5)

        task = AudioCaptureTask(
            MagicMock(),
            validate_on_start=False,
            tuning=_tuning_no_wasapi_extra(),
            sd_module=sd,
            enumerate_fn=lambda: [entry],
        )

        try:
            await task.start()
            cb = captured["cb"]
            # Drain bootstrap.
            while not task._queue.empty():  # noqa: SLF001
                task._queue.get_nowait()  # noqa: SLF001

            # Cancel consumer so its drain doesn't interfere with
            # counter inspection (it doesn't touch underrun counters
            # but the consumer's queue draining is irrelevant here
            # and the explicit cancel keeps the test focused).
            consumer = task._consumer  # noqa: SLF001
            if consumer is not None:
                consumer.cancel()
                with contextlib.suppress(asyncio.CancelledError, BaseException):
                    await consumer

            status_underrun = MagicMock()
            status_underrun.input_underflow = True
            status_underrun.input_overflow = False
            status_underrun.__bool__ = lambda _self: True  # type: ignore[method-assign]

            indata = np.zeros(512, dtype=np.int16)

            storm_size = 500
            initial = task._stream_underruns  # noqa: SLF001
            for _ in range(storm_size):
                cb(indata, 512, None, status_underrun)

            # Counter incremented exactly once per call. No drops.
            assert task._stream_underruns - initial == storm_size  # noqa: SLF001
            # Callback frames also incremented exactly.
            assert task._stream_callback_frames >= storm_size  # noqa: SLF001
        finally:
            await task.stop()

    def test_storm_warn_rate_limited_to_one_per_interval(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        # Under sustained storm conditions, the consumer-loop
        # rate-check method runs every iteration. The WARN must
        # fire ONCE per ``_CAPTURE_UNDERRUN_WARN_INTERVAL_S``
        # interval, not once per call. Without rate-limiting a
        # storm would flood the log feed and could itself
        # contribute to thread starvation (anti-pattern #14).
        import logging
        from unittest.mock import patch

        from sovyx.voice.capture._constants import (
            _CAPTURE_UNDERRUN_WARN_INTERVAL_S,
            _CAPTURE_UNDERRUN_WINDOW_S,
        )

        task = _bare_task_with_underrun_state()

        # Simulate storm spanning 5 windows. Each window: 200 callbacks,
        # 100 underruns (50 % rate, well above 5 % threshold). Window
        # duration = 10 s; 5 windows = 50 s total. Rate-limit interval
        # = 30 s, so exactly 2 WARNs expected (window 1 fires; windows
        # 2-3 rate-limited; window 4 elapsed past 30 s, fires; window
        # 5 rate-limited again).
        with caplog.at_level(logging.WARNING):
            t = 0.0
            with patch(
                "sovyx.voice.capture._loop_mixin.time.monotonic",
                side_effect=lambda: t,
            ):
                # Arm window at t=0.
                task._check_sustained_underrun_rate()  # noqa: SLF001

                # Simulate 5 sustained-storm rolling windows.
                for window_idx in range(5):
                    t = (window_idx + 1) * (_CAPTURE_UNDERRUN_WINDOW_S + 0.1)
                    task._stream_callback_frames += 200  # noqa: SLF001
                    task._stream_underruns += 100  # noqa: SLF001
                    task._check_sustained_underrun_rate()  # noqa: SLF001

        events = [
            r.msg
            for r in caplog.records
            if isinstance(r.msg, dict)
            and r.msg.get("event") == "voice.audio.capture_sustained_underrun"
        ]
        # Window 1 (t=10.1): first WARN — last_warn becomes 10.1.
        # Window 2 (t=20.2): t - last_warn = 10.1 < 30 → SKIP.
        # Window 3 (t=30.3): t - last_warn = 20.2 < 30 → SKIP.
        # Window 4 (t=40.4): t - last_warn = 30.3 ≥ 30 → WARN.
        # Window 5 (t=50.5): t - last_warn = 10.1 < 30 → SKIP.
        # Expected: exactly 2 WARNs across 5 storm windows.
        assert len(events) == 2, (
            f"expected 2 WARNs across 5 storm windows (rate-limit interval "
            f"{_CAPTURE_UNDERRUN_WARN_INTERVAL_S}s vs window {_CAPTURE_UNDERRUN_WINDOW_S}s), "
            f"got {len(events)}"
        )

    def test_storm_counter_state_is_bounded_integers_only(self) -> None:
        # T6.29 contract — under sustained storm, the underrun-tracking
        # state stays bounded: just integer counters + a single
        # monotonic timestamp. No accumulating data structures
        # (no list of past underrun events, no per-frame metadata).
        # Pinning this prevents a future "richer telemetry" refactor
        # from accidentally introducing memory growth proportional
        # to underrun count.
        task = _bare_task_with_underrun_state()

        # Drive the counter via direct attribute assignment (mirrors
        # the audio-callback's pure-increment behaviour at storm rate).
        for _ in range(10_000):
            task._stream_underruns += 1  # noqa: SLF001
            task._stream_callback_frames += 1  # noqa: SLF001

        # Every state field on the underrun monitor is either an int
        # or float (timestamp). No list, no dict, no set.
        underrun_attrs = {
            "_stream_underruns": task._stream_underruns,  # noqa: SLF001
            "_stream_callback_frames": task._stream_callback_frames,  # noqa: SLF001
            "_underrun_window_started_at": task._underrun_window_started_at,  # noqa: SLF001
            "_underrun_window_callbacks_at_start": task._underrun_window_callbacks_at_start,  # noqa: SLF001
            "_underrun_window_underruns_at_start": task._underrun_window_underruns_at_start,  # noqa: SLF001
            "_last_underrun_warning_monotonic": task._last_underrun_warning_monotonic,  # noqa: SLF001
        }
        for name, value in underrun_attrs.items():
            assert value is None or isinstance(value, (int, float)), (
                f"{name} is {type(value).__name__}; underrun state must "
                "stay bounded (int / float / None) under storm pressure"
            )
        # Counters reached 10K without overflow / wraparound.
        assert task._stream_underruns == 10_000  # noqa: SLF001
        assert task._stream_callback_frames == 10_000  # noqa: SLF001


class TestRingFrameLoadStormT627:
    """T6.27 — 10K+ frame load through the capture ring buffer.

    Phase 6 / T6.27 stresses the bounded ring buffer under sustained
    frame load. Production scenario: a long-lived voice session
    (hours of always-listening) accumulates millions of frames
    through the ring at 16 kHz × 16 ms blocks (~62 frames/sec). The
    storm test compresses that load: 10 000 frames written
    sequentially, each at the typical 512-sample window the
    pipeline normalizes to.

    Pinned invariants:
      (a) :attr:`_ring_buffer.nbytes` is bounded by the allocated
          capacity — no growth proportional to frames written;
      (b) :attr:`_ring_state` epoch component never advances
          spontaneously — only :meth:`_allocate_ring_buffer` bumps it;
      (c) the samples-written counter wraps at
          :data:`_RING_SAMPLES_MASK` per the v1.3 packed-state
          contract — a 10K storm at 512 samples/frame writes 5.12M
          samples, which is several orders of magnitude below the
          mask, so the counter advances cleanly without wrap;
      (d) :meth:`tap_recent_frames` after the storm returns at
          most :attr:`_ring_capacity` samples, never more.
    """

    _STORM_FRAMES = 10_000  # noqa: PLR2004 — mission spec § Phase 6 / T6.27
    _FRAME_SAMPLES = 512  # standard pipeline-shaped frame

    def _fresh_task(self) -> AudioCaptureTask:
        """Construct a bare AudioCaptureTask with the packing fields set."""
        task = AudioCaptureTask.__new__(AudioCaptureTask)
        task._ring_buffer = None  # noqa: SLF001
        task._ring_capacity = 0  # noqa: SLF001
        task._ring_write_index = 0  # noqa: SLF001
        task._ring_state = 0  # noqa: SLF001
        task._tuning = None  # noqa: SLF001
        return task

    def test_storm_does_not_grow_ring_buffer_memory(self) -> None:
        """Pin (a): nbytes is bounded by capacity regardless of writes."""
        import numpy as np

        from sovyx.engine.config import VoiceTuningConfig

        task = self._fresh_task()
        task._allocate_ring_buffer(VoiceTuningConfig())  # noqa: SLF001
        baseline_nbytes = task._ring_buffer.nbytes  # noqa: SLF001
        baseline_cap = task._ring_capacity  # noqa: SLF001

        frame = np.zeros(self._FRAME_SAMPLES, dtype=np.int16)
        for _ in range(self._STORM_FRAMES):
            task._ring_write(frame)  # noqa: SLF001

        # Memory is bounded — same buffer object, same nbytes, same cap.
        assert task._ring_buffer.nbytes == baseline_nbytes  # noqa: SLF001
        assert task._ring_capacity == baseline_cap  # noqa: SLF001

    def test_storm_does_not_advance_epoch(self) -> None:
        """Pin (b): epoch only advances on :meth:`_allocate_ring_buffer`."""
        import numpy as np

        from sovyx.engine.config import VoiceTuningConfig

        task = self._fresh_task()
        task._allocate_ring_buffer(VoiceTuningConfig())  # noqa: SLF001
        epoch_before, _ = task.samples_written_mark()

        frame = np.zeros(self._FRAME_SAMPLES, dtype=np.int16)
        for _ in range(self._STORM_FRAMES):
            task._ring_write(frame)  # noqa: SLF001

        epoch_after, _ = task.samples_written_mark()
        assert epoch_after == epoch_before, (
            f"epoch advanced spontaneously: {epoch_before} → {epoch_after} "
            "— only _allocate_ring_buffer should bump it"
        )

    def test_storm_samples_counter_advances_without_wrap(self) -> None:
        """Pin (c): 5.12M samples sit well under :data:`_RING_SAMPLES_MASK`.

        The packed state's samples component has a wide mask (low 32+
        bits per the v1.3 contract). 10K frames × 512 samples = 5.12M;
        the storm's samples-counter advances exactly to that value
        without wrapping. If a future change narrowed the mask below
        ~23 bits (8.4M cap), this test would catch the silent overflow.
        """
        import numpy as np

        from sovyx.engine.config import VoiceTuningConfig

        task = self._fresh_task()
        task._allocate_ring_buffer(VoiceTuningConfig())  # noqa: SLF001

        frame = np.zeros(self._FRAME_SAMPLES, dtype=np.int16)
        for _ in range(self._STORM_FRAMES):
            task._ring_write(frame)  # noqa: SLF001

        _, samples = task.samples_written_mark()
        assert samples == self._STORM_FRAMES * self._FRAME_SAMPLES, (
            f"expected {self._STORM_FRAMES * self._FRAME_SAMPLES} samples, "
            f"got {samples} — samples mask may have been narrowed"
        )

    @pytest.mark.asyncio()
    async def test_tap_after_storm_returns_at_most_capacity(self) -> None:
        """Pin (d): tap_recent_frames is upper-bounded by ring capacity.

        After a 10K-frame storm, the ring has wrapped many times. A
        tap with a duration much larger than the buffer's holding
        time must return at most ``_ring_capacity`` samples — never
        the cumulative count.
        """
        import numpy as np

        from sovyx.engine.config import VoiceTuningConfig

        task = self._fresh_task()
        task._allocate_ring_buffer(VoiceTuningConfig())  # noqa: SLF001
        cap = task._ring_capacity  # noqa: SLF001

        frame = np.zeros(self._FRAME_SAMPLES, dtype=np.int16)
        for _ in range(self._STORM_FRAMES):
            task._ring_write(frame)  # noqa: SLF001

        # Request 10× the buffer's holding window; tap clamps to cap.
        snapshot = await task.tap_recent_frames(duration_s=1000.0)
        assert snapshot.size <= cap
        # And after a storm exceeding capacity, the tap is exactly cap-sized.
        cumulative_samples = self._STORM_FRAMES * self._FRAME_SAMPLES
        if cumulative_samples >= cap:
            assert snapshot.size == cap

    def test_storm_does_not_raise(self) -> None:
        """Pin: the synchronous write path is exception-free under storm."""
        import numpy as np

        from sovyx.engine.config import VoiceTuningConfig

        task = self._fresh_task()
        task._allocate_ring_buffer(VoiceTuningConfig())  # noqa: SLF001

        frame = np.zeros(self._FRAME_SAMPLES, dtype=np.int16)
        for _ in range(self._STORM_FRAMES):
            # Any raise here surfaces directly.
            task._ring_write(frame)  # noqa: SLF001


class TestMixinMroResolution:
    """Pin CLAUDE.md anti-pattern #32 — mixin method-via-MRO stub trap.

    T1.4 step 9b caught a subtle bug where a naive ``def
    _reopen_stream_after_device_error(self) -> None: ...`` stub on
    LoopMixin shadowed the real method on RestartMixin (which sits
    AFTER LoopMixin in ``AudioCaptureTask`` MRO). The stub WINS MRO
    resolution and silently returns ``None`` — the consume-loop
    reconnect path then completes "successfully" without ever
    calling the unified opener.

    Fix shipped in commit ``7e16ad8``: declare the cross-mixin
    reference inside ``if TYPE_CHECKING:`` so the body is type-
    check-only and erased at runtime, letting MRO fall through to
    RestartMixin. These tests pin the contract so a future refactor
    that replaces the TYPE_CHECKING-block declaration with a real
    ``def`` stub is caught immediately.
    """

    def test_reopen_stream_resolves_to_restart_mixin_not_loop_mixin(self) -> None:
        """``AudioCaptureTask._reopen_stream_after_device_error`` MUST
        resolve to :class:`RestartMixin` via MRO — not to a stub on
        :class:`LoopMixin`. If a future change adds a ``def`` stub to
        LoopMixin, MRO would resolve to that stub (since LoopMixin is
        earlier than RestartMixin) and the test catches it.
        """
        from sovyx.voice.capture._restart_mixin import RestartMixin

        method = AudioCaptureTask._reopen_stream_after_device_error  # noqa: SLF001
        # qualname carries the class where the method is DEFINED, so
        # this is the cleanest cross-Python-version check that MRO
        # found RestartMixin's real implementation.
        assert method.__qualname__ == "RestartMixin._reopen_stream_after_device_error", (
            f"Expected MRO to resolve _reopen_stream_after_device_error to "
            f"RestartMixin (real implementation), but got {method.__qualname__!r}. "
            f"This typically means a stub method was added to LoopMixin or another "
            f"mixin earlier in MRO. See CLAUDE.md anti-pattern #32 — cross-mixin "
            f"references whose target lives AFTER the calling mixin in MRO must "
            f"use `if TYPE_CHECKING:` blocks, NOT real `def` stubs."
        )
        # Defence-in-depth: confirm the resolved method is the same
        # function object as RestartMixin's class attribute.
        assert method is RestartMixin._reopen_stream_after_device_error  # noqa: SLF001

    def test_loop_mixin_does_not_define_reopen_stream_at_runtime(self) -> None:
        """:class:`LoopMixin` MUST NOT have a runtime
        ``_reopen_stream_after_device_error`` attribute on its class
        ``__dict__``. The TYPE_CHECKING-block declaration is erased at
        runtime (``TYPE_CHECKING`` is ``False`` outside type-check
        passes), so the attribute should not appear in
        ``LoopMixin.__dict__``. If a refactor accidentally moves the
        declaration outside the TYPE_CHECKING block, this test fails
        and the MRO trap returns.
        """
        from sovyx.voice.capture._loop_mixin import LoopMixin

        assert "_reopen_stream_after_device_error" not in LoopMixin.__dict__, (
            "LoopMixin defines _reopen_stream_after_device_error at runtime; "
            "this WILL shadow RestartMixin's real implementation via MRO. "
            "Move the declaration inside `if TYPE_CHECKING:` per CLAUDE.md "
            "anti-pattern #32."
        )

    def test_audio_capture_task_mro_order_loop_before_restart(self) -> None:
        """The MRO trap detection only matters when LoopMixin precedes
        RestartMixin. Pin the current order so a future inheritance
        re-shuffle that swaps them is caught — at that point the
        TYPE_CHECKING-block trick on LoopMixin becomes unnecessary
        (the regression-test invariants would adjust accordingly).
        """
        from sovyx.voice.capture._lifecycle_mixin import LifecycleMixin
        from sovyx.voice.capture._loop_mixin import LoopMixin
        from sovyx.voice.capture._restart_mixin import RestartMixin

        mro = AudioCaptureTask.__mro__
        loop_idx = mro.index(LoopMixin)
        restart_idx = mro.index(RestartMixin)
        lifecycle_idx = mro.index(LifecycleMixin)
        # LoopMixin precedes RestartMixin → the trap exists, the
        # TYPE_CHECKING-block fix is required.
        assert loop_idx < restart_idx, (
            f"AudioCaptureTask MRO has LoopMixin at {loop_idx} but RestartMixin "
            f"at {restart_idx} — expected LoopMixin first. If the order is now "
            f"reversed, the cross-mixin TYPE_CHECKING-block declaration in "
            f"LoopMixin can be replaced with a regular `def` stub."
        )
        # LifecycleMixin precedes LoopMixin so LoopMixin's `_close_stream`
        # stub is safely shadowed by LifecycleMixin's real method.
        assert lifecycle_idx < loop_idx, (
            f"AudioCaptureTask MRO has LifecycleMixin at {lifecycle_idx} but "
            f"LoopMixin at {loop_idx} — expected LifecycleMixin first so its "
            f"_close_stream / _emit_stream_opened / _signal_consumer_shutdown "
            f"shadow LoopMixin's stubs."
        )


class TestCaptureRestartFrameEmissionT32:
    """T32 — pin the CaptureRestartFrame emission contract on the
    Windows-pair restart methods (``request_exclusive_restart`` +
    ``request_shared_restart``).

    Per CLAUDE.md anti-pattern #29 the frame is observability-only —
    the dashboard's ``GET /api/voice/restart-history`` widget renders
    one timeline of "what happened on the mic" for post-incident
    forensics. The frame's authoritative-state-mutation siblings
    (boolean flags + ``VoicePipelineState``) are unaffected.

    These tests verify the frame is emitted with correct fields at
    the correct moment (BEFORE the ring-buffer epoch increments), so
    a future refactor that drops the emit call OR moves it past the
    epoch boundary is caught immediately.
    """

    @pytest.mark.asyncio()
    async def test_request_exclusive_restart_emits_apo_degraded_frame(
        self,
    ) -> None:
        """Happy path — successful exclusive engagement emits a
        CaptureRestartFrame with reason=APO_DEGRADED + bypass_tier=3 +
        new_signal_processing_mode=exclusive."""
        from sovyx.voice.pipeline._frame_types import (
            CaptureRestartFrame,
            CaptureRestartReason,
        )

        pipeline = MagicMock()
        pipeline.feed_frame = AsyncMock()
        recorded: list[CaptureRestartFrame] = []
        pipeline.record_capture_restart = MagicMock(side_effect=lambda f: recorded.append(f))

        sd = _fake_sd()
        sd.InputStream = lambda **_kwargs: MagicMock()  # type: ignore[attr-defined]
        entry = _input_entry(index=7, rate=16_000, host_api="Windows WASAPI")

        task = AudioCaptureTask(
            pipeline,
            input_device=7,
            validate_on_start=False,
            tuning=_tuning_no_wasapi_extra(),
            sd_module=sd,
            enumerate_fn=lambda: [entry],
        )
        try:
            await task.start()
            recorded.clear()  # discard any frames from start

            result = await task.request_exclusive_restart()

            assert result.verdict is ExclusiveRestartVerdict.EXCLUSIVE_ENGAGED
            assert len(recorded) == 1, (
                f"expected exactly one CaptureRestartFrame; got {len(recorded)}"
            )
            frame = recorded[0]
            assert frame.frame_type == "CaptureRestart"
            assert frame.restart_reason == CaptureRestartReason.APO_DEGRADED.value
            assert frame.bypass_tier == 3  # noqa: PLR2004
            assert frame.new_signal_processing_mode == "exclusive"
            assert frame.old_signal_processing_mode == "shared"
            # Substrate identifiers should be populated.
            assert frame.new_host_api == "Windows WASAPI"
            assert frame.timestamp_monotonic > 0.0
        finally:
            await task.stop()

    @pytest.mark.asyncio()
    async def test_request_shared_restart_emits_manual_revert_frame(self) -> None:
        """Revert path — request_shared_restart emits a
        CaptureRestartFrame with reason=MANUAL + bypass_tier=0 +
        new_signal_processing_mode=shared."""
        from sovyx.voice.pipeline._frame_types import (
            CaptureRestartFrame,
            CaptureRestartReason,
        )

        pipeline = MagicMock()
        pipeline.feed_frame = AsyncMock()
        recorded: list[CaptureRestartFrame] = []
        pipeline.record_capture_restart = MagicMock(side_effect=lambda f: recorded.append(f))

        sd = _fake_sd()
        sd.InputStream = lambda **_kwargs: MagicMock()  # type: ignore[attr-defined]
        entry = _input_entry(index=8, rate=16_000, host_api="Windows WASAPI")

        task = AudioCaptureTask(
            pipeline,
            input_device=8,
            validate_on_start=False,
            tuning=_tuning_no_wasapi_extra(),
            sd_module=sd,
            enumerate_fn=lambda: [entry],
        )
        try:
            await task.start()
            recorded.clear()

            result = await task.request_shared_restart()

            assert result.verdict is SharedRestartVerdict.SHARED_ENGAGED
            assert len(recorded) == 1
            frame = recorded[0]
            assert frame.frame_type == "CaptureRestart"
            assert frame.restart_reason == CaptureRestartReason.MANUAL.value
            assert frame.bypass_tier == 0
            assert frame.new_signal_processing_mode == "shared"
            assert frame.old_signal_processing_mode == "exclusive"
        finally:
            await task.stop()

    @pytest.mark.asyncio()
    async def test_request_alsa_hw_direct_restart_emits_apo_degraded_frame_tier_2(
        self,
    ) -> None:
        """Linux pair — request_alsa_hw_direct_restart on a Linux
        platform emits APO_DEGRADED + bypass_tier=2 (the
        LinuxPipeWireDirectBypass strategy in the bypass coordinator
        is Tier 2)."""
        from sovyx.voice.pipeline._frame_types import (
            CaptureRestartFrame,
            CaptureRestartReason,
        )

        pipeline = MagicMock()
        pipeline.feed_frame = AsyncMock()
        recorded: list[CaptureRestartFrame] = []
        pipeline.record_capture_restart = MagicMock(side_effect=lambda f: recorded.append(f))

        sd = _fake_sd()
        sd.InputStream = lambda **_kwargs: MagicMock()  # type: ignore[attr-defined]
        alsa_entry = _input_entry(index=10, name="usbmic", host_api="ALSA", rate=48_000)
        pulse_entry = _input_entry(index=11, name="usbmic", host_api="PulseAudio", rate=48_000)

        task = AudioCaptureTask(
            pipeline,
            input_device=11,
            host_api_name="PulseAudio",
            validate_on_start=False,
            tuning=_tuning_no_wasapi_extra(),
            sd_module=sd,
            enumerate_fn=lambda: [alsa_entry, pulse_entry],
        )
        try:
            with patch("sovyx.voice.capture._restart_mixin.sys.platform", "linux"):
                await task.start()
                recorded.clear()

                result = await task.request_alsa_hw_direct_restart()

            assert result.verdict in {
                AlsaHwDirectRestartVerdict.ALSA_HW_ENGAGED,
                AlsaHwDirectRestartVerdict.DOWNGRADED_TO_SESSION_MANAGER,
            }
            assert len(recorded) == 1
            frame = recorded[0]
            assert frame.frame_type == "CaptureRestart"
            assert frame.restart_reason == CaptureRestartReason.APO_DEGRADED.value
            assert frame.bypass_tier == 2  # noqa: PLR2004
            assert frame.old_signal_processing_mode == "session_manager"
            assert frame.new_signal_processing_mode in {
                "alsa_hw_direct",
                "session_manager",
            }
        finally:
            await task.stop()

    @pytest.mark.asyncio()
    async def test_request_session_manager_restart_revert_emits_manual_tier_0(
        self,
    ) -> None:
        """Linux pair — request_session_manager_restart called WITHOUT
        target_device is the revert path (operator unpin / coordinator
        revert). MANUAL + bypass_tier=0."""
        from sovyx.voice.pipeline._frame_types import (
            CaptureRestartFrame,
            CaptureRestartReason,
        )

        pipeline = MagicMock()
        pipeline.feed_frame = AsyncMock()
        recorded: list[CaptureRestartFrame] = []
        pipeline.record_capture_restart = MagicMock(side_effect=lambda f: recorded.append(f))

        sd = _fake_sd()
        sd.InputStream = lambda **_kwargs: MagicMock()  # type: ignore[attr-defined]
        alsa_entry = _input_entry(index=12, name="usbmic", host_api="ALSA", rate=48_000)
        pulse_entry = _input_entry(index=13, name="usbmic", host_api="PulseAudio", rate=48_000)

        task = AudioCaptureTask(
            pipeline,
            input_device=12,
            host_api_name="ALSA",
            validate_on_start=False,
            tuning=_tuning_no_wasapi_extra(),
            sd_module=sd,
            enumerate_fn=lambda: [alsa_entry, pulse_entry],
        )
        try:
            with patch("sovyx.voice.capture._restart_mixin.sys.platform", "linux"):
                await task.start()
                recorded.clear()

                result = await task.request_session_manager_restart()

            assert result.verdict in {
                SessionManagerRestartVerdict.SESSION_MANAGER_ENGAGED,
                SessionManagerRestartVerdict.DOWNGRADED_TO_ALSA_HW,
            }
            assert len(recorded) == 1
            frame = recorded[0]
            assert frame.frame_type == "CaptureRestart"
            assert frame.restart_reason == CaptureRestartReason.MANUAL.value
            assert frame.bypass_tier == 0
            assert frame.old_signal_processing_mode == "alsa_hw_direct"
            assert frame.new_signal_processing_mode == "session_manager"
        finally:
            await task.stop()

    @pytest.mark.asyncio()
    async def test_request_session_manager_restart_escape_emits_apo_degraded_tier_1(
        self,
    ) -> None:
        """Linux pair — request_session_manager_restart called WITH
        explicit target_device is the LinuxSessionManagerEscapeBypass
        apply path (T6 voice-linux-cascade-root-fix). APO_DEGRADED +
        bypass_tier=1."""
        from sovyx.voice.pipeline._frame_types import (
            CaptureRestartFrame,
            CaptureRestartReason,
        )

        pipeline = MagicMock()
        pipeline.feed_frame = AsyncMock()
        recorded: list[CaptureRestartFrame] = []
        pipeline.record_capture_restart = MagicMock(side_effect=lambda f: recorded.append(f))

        sd = _fake_sd()
        sd.InputStream = lambda **_kwargs: MagicMock()  # type: ignore[attr-defined]
        alsa_entry = _input_entry(index=14, name="badcard", host_api="ALSA", rate=48_000)
        pipewire_entry = _input_entry(index=15, name="pipewire", host_api="PipeWire", rate=48_000)

        task = AudioCaptureTask(
            pipeline,
            input_device=14,
            host_api_name="ALSA",
            validate_on_start=False,
            tuning=_tuning_no_wasapi_extra(),
            sd_module=sd,
            enumerate_fn=lambda: [alsa_entry, pipewire_entry],
        )
        try:
            with patch("sovyx.voice.capture._restart_mixin.sys.platform", "linux"):
                await task.start()
                recorded.clear()

                result = await task.request_session_manager_restart(
                    target_device=pipewire_entry,
                )

            assert result.verdict in {
                SessionManagerRestartVerdict.SESSION_MANAGER_ENGAGED,
                SessionManagerRestartVerdict.DOWNGRADED_TO_ALSA_HW,
            }
            assert len(recorded) == 1
            frame = recorded[0]
            assert frame.restart_reason == CaptureRestartReason.APO_DEGRADED.value
            assert frame.bypass_tier == 1
            assert frame.old_signal_processing_mode == "session_manager"
            assert frame.new_signal_processing_mode == "session_manager"
        finally:
            await task.stop()

    @pytest.mark.asyncio()
    async def test_no_frame_emitted_when_open_fails(self) -> None:
        """Failed restart MUST NOT emit a CaptureRestartFrame —
        the substrate didn't actually change. The frame is
        observability of completed transitions, not attempts."""
        pipeline = MagicMock()
        pipeline.feed_frame = AsyncMock()
        recorded: list[Any] = []
        pipeline.record_capture_restart = MagicMock(side_effect=lambda f: recorded.append(f))

        attempt = {"n": 0}

        def stream_factory(**_kwargs: Any) -> MagicMock:
            attempt["n"] += 1
            if attempt["n"] == 1:
                return MagicMock()  # initial start succeeds
            raise sd.PortAudioError("device gone")  # type: ignore[attr-defined]

        sd = _fake_sd()
        sd.InputStream = stream_factory  # type: ignore[attr-defined]
        entry = _input_entry(index=9, rate=16_000, host_api="Windows WASAPI")

        task = AudioCaptureTask(
            pipeline,
            input_device=9,
            validate_on_start=False,
            tuning=_tuning_no_wasapi_extra(),
            sd_module=sd,
            enumerate_fn=lambda: [entry],
        )
        try:
            await task.start()
            recorded.clear()

            result = await task.request_exclusive_restart()

            # All open attempts fail → no frame emitted at all.
            assert result.verdict in {
                ExclusiveRestartVerdict.OPEN_FAILED_NO_STREAM,
                ExclusiveRestartVerdict.OPEN_FAILED_SHARED_FALLBACK,
            }
            assert len(recorded) == 0, f"expected zero frames on failure path; got {len(recorded)}"
        finally:
            await task.stop()


class TestRequestHostApiRotateT28:
    """T28 — pin the request_host_api_rotate contract.

    The method drives the Tier 2 ``WindowsHostApiRotateThenExclusive``
    bypass strategy. It pivots the capture stream's host_api by
    resolving a sibling :class:`DeviceEntry` on the target host_api
    and handing it to the unified opener, with
    ``preferred_host_api`` set to the rotation target so the
    sibling-chain fallback respects the strategy's intent.
    """

    @pytest.mark.asyncio()
    async def test_rotate_to_wasapi_succeeds_when_sibling_exists(self) -> None:
        """Happy path — endpoint has a WASAPI sibling, rotation
        engages, ``self._host_api_name`` is updated, frame emitted."""
        from sovyx.voice._capture_task import HostApiRotateVerdict
        from sovyx.voice.pipeline._frame_types import (
            CaptureRestartFrame,
            CaptureRestartReason,
        )

        pipeline = MagicMock()
        pipeline.feed_frame = AsyncMock()
        recorded: list[CaptureRestartFrame] = []
        pipeline.record_capture_restart = MagicMock(side_effect=lambda f: recorded.append(f))

        sd = _fake_sd()
        sd.InputStream = lambda **_kwargs: MagicMock()  # type: ignore[attr-defined]
        # Two siblings — same canonical_name, different host_api.
        # When the unified opener picks one, it returns info.host_api
        # matching that sibling's host_api. We start on MME and
        # request rotation to WASAPI.
        mme_entry = _input_entry(index=20, name="usbmic", host_api="MME", rate=16_000)
        wasapi_entry = _input_entry(
            index=21, name="usbmic", host_api="Windows WASAPI", rate=16_000
        )

        with patch("sovyx.voice.capture._restart_mixin.sys.platform", "win32"):
            task = AudioCaptureTask(
                pipeline,
                input_device=20,
                host_api_name="MME",
                validate_on_start=False,
                tuning=_tuning_no_wasapi_extra(),
                sd_module=sd,
                enumerate_fn=lambda: [mme_entry, wasapi_entry],
            )
            try:
                await task.start()
                recorded.clear()

                result = await task.request_host_api_rotate(
                    target_host_api="Windows WASAPI",
                    target_exclusive=False,
                )

                assert result.verdict is HostApiRotateVerdict.ROTATED_SUCCESS
                assert result.engaged is True
                assert result.target_host_api == "Windows WASAPI"
                assert result.source_host_api == "MME"
                assert result.host_api == "Windows WASAPI"
                # State mutation pinned.
                assert task._host_api_name == "Windows WASAPI"  # noqa: SLF001
                # Frame emitted with bypass_tier=2.
                assert len(recorded) == 1
                frame = recorded[0]
                assert frame.frame_type == "CaptureRestart"
                assert frame.restart_reason == CaptureRestartReason.APO_DEGRADED.value
                assert frame.bypass_tier == 2  # noqa: PLR2004
            finally:
                await task.stop()

    @pytest.mark.asyncio()
    async def test_rotate_returns_no_target_sibling_when_target_absent(
        self,
    ) -> None:
        """Endpoint has no sibling on the target host_api → rotation
        not attempted, existing stream preserved."""
        from sovyx.voice._capture_task import HostApiRotateVerdict

        pipeline = MagicMock()
        pipeline.feed_frame = AsyncMock()
        pipeline.record_capture_restart = MagicMock()

        sd = _fake_sd()
        sd.InputStream = lambda **_kwargs: MagicMock()  # type: ignore[attr-defined]
        # ONLY MME entry — no WASAPI sibling exists.
        mme_entry = _input_entry(index=22, name="usbmic", host_api="MME", rate=16_000)

        with patch("sovyx.voice.capture._restart_mixin.sys.platform", "win32"):
            task = AudioCaptureTask(
                pipeline,
                input_device=22,
                host_api_name="MME",
                validate_on_start=False,
                tuning=_tuning_no_wasapi_extra(),
                sd_module=sd,
                enumerate_fn=lambda: [mme_entry],
            )
            try:
                await task.start()
                pipeline.record_capture_restart.reset_mock()

                result = await task.request_host_api_rotate(
                    target_host_api="Windows WASAPI",
                )

                assert result.verdict is HostApiRotateVerdict.NO_TARGET_SIBLING
                assert result.engaged is False
                # Stream preserved (still on MME).
                assert task._host_api_name == "MME"  # noqa: SLF001
                # No frame emitted because no rotation occurred.
                pipeline.record_capture_restart.assert_not_called()
            finally:
                await task.stop()

    @pytest.mark.asyncio()
    async def test_rotate_returns_not_win32_on_non_windows(self) -> None:
        """Defensive — direct invocation on non-Windows preserves the
        existing stream."""
        from sovyx.voice._capture_task import HostApiRotateVerdict

        pipeline = MagicMock()
        pipeline.feed_frame = AsyncMock()
        pipeline.record_capture_restart = MagicMock()

        sd = _fake_sd()
        sd.InputStream = lambda **_kwargs: MagicMock()  # type: ignore[attr-defined]
        entry = _input_entry(index=23, host_api="ALSA", rate=48_000)

        with patch("sovyx.voice.capture._restart_mixin.sys.platform", "linux"):
            task = AudioCaptureTask(
                pipeline,
                input_device=23,
                host_api_name="ALSA",
                validate_on_start=False,
                tuning=_tuning_no_wasapi_extra(),
                sd_module=sd,
                enumerate_fn=lambda: [entry],
            )
            try:
                await task.start()

                result = await task.request_host_api_rotate(
                    target_host_api="Windows WASAPI",
                )

                assert result.verdict is HostApiRotateVerdict.NOT_WIN32
                assert result.engaged is False
            finally:
                await task.stop()

    @pytest.mark.asyncio()
    async def test_rotate_returns_not_running_when_stopped(self) -> None:
        """Calling before start() returns NOT_RUNNING with no side
        effects."""
        from sovyx.voice._capture_task import HostApiRotateVerdict

        pipeline = MagicMock()
        with patch("sovyx.voice.capture._restart_mixin.sys.platform", "win32"):
            task = AudioCaptureTask(pipeline)

            result = await task.request_host_api_rotate(
                target_host_api="Windows WASAPI",
            )

        assert result.verdict is HostApiRotateVerdict.NOT_RUNNING
        assert result.engaged is False
