"""Tests for the v0.31.7 CR3 wall-clock heartbeat timer.

Pre-CR3 ``voice_pipeline_heartbeat`` emission was tied to
``feed_frame``: the per-frame ``_track_vad_for_heartbeat`` checked
the heartbeat interval and emitted when crossed. ``feed_frame`` is
awaited serially by the capture-side ``_consume_loop`` — one frame
at a time. When ``_handle_recording → _end_recording`` parked on
STT (Moonshine ONNX, 200-2000 ms) or ``_on_perception →
bridge.process`` parked on the LLM (1-30 s), no further frames
were drained, so the heartbeat STOPPED for that whole window.
Operators interpreted a healthy pipeline as wedged.

CR3 fix: spawn a wall-clock background task at :meth:`start` that
calls :meth:`_emit_heartbeat` every ``_HEARTBEAT_INTERVAL_S``
seconds regardless of consumer-loop progress. These tests pin
that contract:

* :class:`TestHeartbeatContinuesDuringSttParking` — the mission
  scenario: simulate STT parking by NOT calling ``feed_frame`` and
  confirm the timer fires multiple times anyway.
* :class:`TestHeartbeatStopsWhenPipelineStops` — :meth:`stop`
  cancels + drains the timer; no heartbeats fire after stop.
* :class:`TestHeartbeatCarriesSnapshotVadProbability` — the
  per-frame snapshot fields (``_last_vad_probability_snapshot``)
  flow through the timer-driven emission body.

Reference: MISSION-voice-v0_31_7-runtime-conversation-closure-2026-05-08.md §Phase 1 T1.3.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import numpy as np
import pytest

from sovyx.voice.pipeline import _orchestrator as orch_mod
from sovyx.voice.pipeline._config import VoicePipelineConfig
from sovyx.voice.pipeline._orchestrator import VoicePipeline
from sovyx.voice.vad import VADEvent, VADState

# ---------------------------------------------------------------------------
# Helpers / Fixtures
# ---------------------------------------------------------------------------

_FRAME_LEN = 512
_ORCH_LOGGER = "sovyx.voice.pipeline._orchestrator"


def _silence_frame() -> np.ndarray:
    """Create a 512-sample silence frame at int16."""
    return np.zeros(_FRAME_LEN, dtype=np.int16)


def _make_pipeline(*, vad_speech: bool = False) -> VoicePipeline:
    """Construct a minimal pipeline for heartbeat-timer tests."""
    config = VoicePipelineConfig(
        mind_id="test-mind",
        wake_word_enabled=False,
        barge_in_enabled=False,
        fillers_enabled=False,
        filler_delay_ms=100,
        silence_frames_end=3,
        max_recording_frames=10,
    )
    vad = MagicMock()
    vad.process_frame.return_value = VADEvent(
        is_speech=vad_speech,
        probability=0.9 if vad_speech else 0.1,
        state=VADState.SPEECH if vad_speech else VADState.SILENCE,
    )
    ww = MagicMock()
    ww.process_frame.return_value = MagicMock(detected=False)
    return VoicePipeline(
        config=config,
        vad=vad,
        wake_word=ww,
        stt=AsyncMock(),
        tts=AsyncMock(),
        event_bus=AsyncMock(),
    )


def _heartbeats_of(caplog: pytest.LogCaptureFixture) -> list[dict[str, Any]]:
    """Return all ``voice_pipeline_heartbeat`` event payloads from caplog."""
    return [
        r.msg
        for r in caplog.records
        if r.name == _ORCH_LOGGER
        and isinstance(r.msg, dict)
        and r.msg.get("event") == "voice_pipeline_heartbeat"
    ]


# ---------------------------------------------------------------------------
# Test classes
# ---------------------------------------------------------------------------


class TestHeartbeatContinuesDuringSttParking:
    """The wall-clock timer fires regardless of consumer-loop progress.

    The motivating bug: pre-CR3 emission was gated on ``feed_frame``;
    when the consumer parked on STT/LLM awaits, no frames flowed and
    the heartbeat stopped. This test simulates the parking by NOT
    calling ``feed_frame`` at all and asserts that the timer-driven
    emission still fires multiple times in a fixed wall-clock window.
    """

    @pytest.mark.asyncio
    async def test_heartbeat_continues_during_stt_parking(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Drive timer ticks via patched ``asyncio.sleep`` and assert ≥ 2 emissions.

        Simulates STT parking by NEVER calling ``feed_frame``.
        Patches the orchestrator's ``asyncio.sleep`` so the heartbeat
        loop's per-iteration wait returns immediately (single-tick
        yield via the real ``asyncio.sleep`` captured before the
        patch). Without the patch the test would block for
        ``_HEARTBEAT_INTERVAL_S`` real wall-clock seconds per tick.
        """
        caplog.set_level(logging.INFO, logger=_ORCH_LOGGER)
        pipeline = _make_pipeline()

        # Capture the real ``asyncio.sleep`` BEFORE patching so the
        # stub can yield without recursing into itself.
        real_sleep = asyncio.sleep

        sleep_call_count = 0

        async def _fast_sleep(_delay: float) -> None:
            nonlocal sleep_call_count
            sleep_call_count += 1
            # Terminate the loop after enough ticks so the test ends.
            if sleep_call_count >= 5:  # noqa: PLR2004
                pipeline._running = False  # noqa: SLF001 — terminate the loop
            # Yield via the REAL sleep so we don't recurse on the patch.
            await real_sleep(0)

        with patch.object(orch_mod.asyncio, "sleep", _fast_sleep):
            await pipeline.start()
            # Let the heartbeat loop run its full sequence to
            # completion (terminates when sleep_call_count >= 5).
            for _ in range(20):
                await real_sleep(0)
                if pipeline._heartbeat_task is not None and pipeline._heartbeat_task.done():  # noqa: SLF001
                    break

        heartbeats = _heartbeats_of(caplog)
        # The loop sleeps once per iteration; with sleep_call_count
        # incrementing on each call, the loop emits ~3 times before
        # the running flag flips. The contract under test:
        # heartbeats fire WITHOUT any feed_frame call.
        assert len(heartbeats) >= 2, (  # noqa: PLR2004
            f"Expected ≥ 2 timer-driven heartbeats during simulated "
            f"STT parking; got {len(heartbeats)}. The wall-clock "
            f"timer is the contract — feed_frame was never called."
        )
        # Every emission carries the canonical schema fields.
        for hb in heartbeats:
            assert hb["mind_id"] == "test-mind"
            assert hb["state"] == "IDLE"
            assert "max_vad_probability" in hb
            assert "frames_processed" in hb

        # Cleanup: ensure the task is fully drained before teardown.
        if pipeline._heartbeat_task is not None:  # noqa: SLF001
            pipeline._heartbeat_task.cancel()  # noqa: SLF001
            with contextlib.suppress(asyncio.CancelledError):
                await pipeline._heartbeat_task  # noqa: SLF001


class TestHeartbeatStopsWhenPipelineStops:
    """:meth:`stop` cancels + drains the timer task — no post-stop emissions."""

    @pytest.mark.asyncio
    async def test_heartbeat_stops_when_pipeline_stops(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """``stop`` produces zero subsequent heartbeats."""
        caplog.set_level(logging.INFO, logger=_ORCH_LOGGER)
        pipeline = _make_pipeline()
        await pipeline.start()
        # Yield once so the spawn's ``_runner`` reaches its
        # ``await coro`` line — without this, ``stop()`` cancels
        # before the task is actually started and Python emits a
        # ``RuntimeWarning: coroutine ... was never awaited``.
        await asyncio.sleep(0)
        # The heartbeat task is alive at this point.
        assert pipeline._heartbeat_task is not None
        assert not pipeline._heartbeat_task.done()

        await pipeline.stop()
        # The heartbeat task is fully drained.
        assert pipeline._heartbeat_task is None

        # Wait several ticks of real wall-clock time to confirm no
        # late heartbeats arrive after stop. We can't easily mock
        # clock here — instead, rely on the contract that the stop
        # path drains within ``_CANCELLATION_TASK_TIMEOUT_S`` and
        # zeroes the task field.
        for _ in range(5):
            await asyncio.sleep(0)

        # Filter heartbeats emitted AFTER stop_complete.
        # Find the index of the stop_complete record then count
        # heartbeats strictly after it.
        stop_complete_idx = None
        for i, r in enumerate(caplog.records):
            if isinstance(r.msg, dict) and r.msg.get("event") == "voice.pipeline.stop_complete":
                stop_complete_idx = i
                break
        assert stop_complete_idx is not None, (
            "stop() must emit voice.pipeline.stop_complete; the "
            "heartbeat-stop assertion below depends on it."
        )
        post_stop_heartbeats = [
            r.msg
            for r in caplog.records[stop_complete_idx + 1 :]
            if r.name == _ORCH_LOGGER
            and isinstance(r.msg, dict)
            and r.msg.get("event") == "voice_pipeline_heartbeat"
        ]
        assert post_stop_heartbeats == [], (
            f"No heartbeats may fire after stop_complete; "
            f"got {len(post_stop_heartbeats)}: {post_stop_heartbeats}"
        )


class TestHeartbeatCarriesSnapshotVadProbability:
    """Per-frame snapshot fields propagate into the timer-driven emission."""

    @pytest.mark.asyncio
    async def test_heartbeat_carries_snapshot_vad_probability(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Update snapshot fields from a fake feed_frame; assert emit reports them."""
        caplog.set_level(logging.INFO, logger=_ORCH_LOGGER)
        pipeline = _make_pipeline()
        await pipeline.start()
        # Cancel the timer so we control emission timing — the
        # snapshot mechanism is what's under test, not the timer's
        # firing cadence (covered by the parking test).
        await asyncio.sleep(0)
        if pipeline._heartbeat_task is not None:
            pipeline._heartbeat_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await pipeline._heartbeat_task

        # Simulate a frame fed before parking — _track_vad_for_heartbeat
        # updates the snapshot. We bypass the full feed_frame path and
        # call the helper directly since this test pins the snapshot
        # contract, not the orchestrator's frame routing.
        pipeline._track_vad_for_heartbeat(0.42)
        snapshot_at = pipeline._last_vad_probability_snapshot_at

        # Fire the timer-driven emission body directly. The ``now``
        # passed in is later than ``snapshot_at`` so age_s > 0.
        emit_now = snapshot_at + 1.5
        pipeline._emit_heartbeat(emit_now)

        heartbeats = _heartbeats_of(caplog)
        assert len(heartbeats) == 1
        hb = heartbeats[0]
        # Per-frame snapshot fields landed on the emission.
        assert hb["last_vad_probability"] == pytest.approx(0.42)
        assert hb["last_vad_probability_age_s"] == pytest.approx(1.5, abs=0.001)
        # Existing schema preserved (regression guard against future
        # refactors that drop fields).
        assert hb["state"] == "IDLE"
        assert hb["max_vad_probability"] == pytest.approx(0.42)
        assert hb["frames_processed"] == 1

    @pytest.mark.asyncio
    async def test_heartbeat_omits_snapshot_when_never_observed(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Pre-first-frame emission OMITS snapshot fields (no synthetic zeros).

        The contract mirrors the SNR p50/p95 fields' "emit-or-omit"
        semantics: when no per-frame observation has landed yet
        (``_last_vad_probability_snapshot_at == 0.0``), the emission
        OMITS the snapshot fields rather than reporting a synthetic
        ``last_vad_probability=0.0`` that dashboards would graph as
        a real "VAD said zero" reading.
        """
        caplog.set_level(logging.INFO, logger=_ORCH_LOGGER)
        pipeline = _make_pipeline()
        await pipeline.start()
        # Cancel the timer for deterministic control.
        await asyncio.sleep(0)
        if pipeline._heartbeat_task is not None:
            pipeline._heartbeat_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await pipeline._heartbeat_task

        # No _track_vad_for_heartbeat call — snapshot fields stay at
        # their post-init defaults (0.0 / 0.0).
        assert pipeline._last_vad_probability_snapshot_at == 0.0

        pipeline._emit_heartbeat(time.monotonic())

        heartbeats = _heartbeats_of(caplog)
        assert len(heartbeats) == 1
        hb = heartbeats[0]
        assert "last_vad_probability" not in hb
        assert "last_vad_probability_age_s" not in hb
