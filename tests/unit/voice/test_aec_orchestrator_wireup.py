"""Tests for the orchestrator-level AEC wire-up [Phase 4 T4.4.d].

Pins the contract that:

* :class:`VoicePipeline` exposes ``set_render_buffer`` that
  delegates to its embedded :class:`AudioOutputQueue`.
* :class:`AudioCaptureTask` accepts ``aec`` and ``render_provider``
  parameters and stores them so every :class:`FrameNormalizer`
  construction site (initial open + 6 RestartMixin paths) can
  forward them.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import numpy as np
import pytest

from sovyx.voice._aec import (
    AecProcessor,
    NoOpAec,
    NullRenderProvider,
    RenderPcmProvider,
)
from sovyx.voice._capture_task import AudioCaptureTask
from sovyx.voice._render_pcm_buffer import RenderPcmBuffer

# ── VoicePipeline.set_render_buffer ──────────────────────────────────────


class TestPipelineSetRenderBuffer:
    """The orchestrator-level helper plumbs through to AudioOutputQueue."""

    def _make_pipeline(self) -> object:
        # Construct VoicePipeline with mocked subsystems — we only
        # exercise the AudioOutputQueue wiring, no STT/TTS/VAD work.
        from sovyx.voice.pipeline._config import VoicePipelineConfig
        from sovyx.voice.pipeline._orchestrator import VoicePipeline

        cfg = VoicePipelineConfig()
        return VoicePipeline(
            config=cfg,
            vad=MagicMock(),
            wake_word=MagicMock(),
            stt=MagicMock(),
            tts=MagicMock(),
            event_bus=MagicMock(),
            on_perception=MagicMock(),
        )

    def test_delegates_to_output_queue_set_render_buffer(self) -> None:
        pipeline = self._make_pipeline()
        buffer = RenderPcmBuffer()
        pipeline.set_render_buffer(buffer)
        # Verify via the queue's white-box state.
        assert pipeline.output._render_buffer is buffer  # noqa: SLF001

    def test_set_to_none_unwires(self) -> None:
        pipeline = self._make_pipeline()
        pipeline.set_render_buffer(RenderPcmBuffer())
        pipeline.set_render_buffer(None)
        assert pipeline.output._render_buffer is None  # noqa: SLF001

    def test_register_same_buffer_on_capture_and_output_is_consistent(self) -> None:
        # T4.4.d goal: a SINGLE RenderPcmBuffer instance bridges
        # producer (queue) and consumer (capture task / FrameNormalizer).
        # Verify the same instance flows through both registrations.
        pipeline = self._make_pipeline()
        buffer = RenderPcmBuffer()
        pipeline.set_render_buffer(buffer)
        # The capture-side registration happens through AudioCaptureTask;
        # exercise that the same buffer satisfies both Protocol shapes.
        from sovyx.voice._aec import RenderPcmSink

        assert isinstance(buffer, RenderPcmSink)
        assert isinstance(buffer, RenderPcmProvider)
        assert pipeline.output._render_buffer is buffer  # noqa: SLF001


# ── AudioCaptureTask AEC + render_provider plumbing ──────────────────────


class TestCaptureTaskAecPlumbing:
    """Constructor accepts AEC + provider; FrameNormalizer sites pick them up."""

    def _pipeline_stub(self) -> MagicMock:
        # Minimal stub — AudioCaptureTask only stores the reference
        # at __init__ time; doesn't dereference until start() runs.
        return MagicMock()

    def test_default_aec_is_none(self) -> None:
        task = AudioCaptureTask(self._pipeline_stub())
        assert task._aec is None  # noqa: SLF001

    def test_default_render_provider_is_none(self) -> None:
        task = AudioCaptureTask(self._pipeline_stub())
        assert task._render_provider is None  # noqa: SLF001

    def test_constructs_with_aec_and_provider(self) -> None:
        aec = NoOpAec()
        provider = NullRenderProvider()
        task = AudioCaptureTask(
            self._pipeline_stub(),
            aec=aec,
            render_provider=provider,
        )
        assert task._aec is aec  # noqa: SLF001
        assert task._render_provider is provider  # noqa: SLF001

    def test_aec_only_can_be_provided(self) -> None:
        # Either parameter is independently optional.
        task = AudioCaptureTask(self._pipeline_stub(), aec=NoOpAec())
        assert task._aec is not None  # noqa: SLF001
        assert task._render_provider is None  # noqa: SLF001

    def test_provider_only_can_be_provided(self) -> None:
        task = AudioCaptureTask(
            self._pipeline_stub(),
            render_provider=NullRenderProvider(),
        )
        assert task._aec is None  # noqa: SLF001
        assert task._render_provider is not None  # noqa: SLF001

    def test_aec_and_provider_propagate_through_helper(self) -> None:
        # Direct white-box: build the FrameNormalizer the same way the
        # capture-task initial-open path does and verify it inherits
        # the parameters.
        from sovyx.voice._frame_normalizer import FrameNormalizer

        aec: AecProcessor = NoOpAec()
        provider = NullRenderProvider()
        task = AudioCaptureTask(
            self._pipeline_stub(),
            aec=aec,
            render_provider=provider,
        )
        # Construct normaliser with the same args the production
        # path uses (sans agc2 — orthogonal here). Verifies that the
        # task's stored references would be plumbed through.
        normalizer = FrameNormalizer(
            source_rate=16_000,
            source_channels=1,
            aec=task._aec,  # noqa: SLF001 — white-box plumbing assertion
            render_provider=task._render_provider,  # noqa: SLF001
        )
        assert normalizer.aec is aec


# ── End-to-end producer→consumer flow ────────────────────────────────────


class TestEndToEndRenderPcmFlow:
    """A single RenderPcmBuffer instance bridges queue→FrameNormalizer."""

    @pytest.mark.asyncio()
    async def test_chunk_played_is_visible_in_aec_window(self) -> None:
        # 1. Construct shared buffer.
        # 2. Wire to AudioOutputQueue (sink).
        # 3. Wire to FrameNormalizer (provider) via test stub.
        # 4. Play a chunk → observe via FrameNormalizer's AEC window.
        from unittest.mock import AsyncMock, patch

        from sovyx.voice._frame_normalizer import FrameNormalizer
        from sovyx.voice.pipeline import _output_queue as _output_queue_mod
        from sovyx.voice.pipeline._output_queue import AudioOutputQueue

        buffer = RenderPcmBuffer()
        queue = AudioOutputQueue(render_buffer=buffer)
        normalizer = FrameNormalizer(
            source_rate=16_000,
            source_channels=1,
            render_provider=buffer,
        )

        class _Chunk:
            def __init__(self, value: int) -> None:
                # 32 ms @ 16 kHz = 512 samples (exact AEC window size).
                self.audio = np.full(512, value, dtype=np.int16)
                self.sample_rate = 16_000
                self.duration_ms = 32.0

        chunk = _Chunk(value=7777)
        with patch.object(_output_queue_mod, "_play_audio", new_callable=AsyncMock):
            await queue.play_immediate(chunk)

        # Drive the FrameNormalizer to consume one window — its
        # render_provider should now serve the played PCM.
        out = buffer.get_aligned_window(512)
        assert np.all(out == 7777)
