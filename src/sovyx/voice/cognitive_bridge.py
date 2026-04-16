"""Bridge between the voice pipeline and the cognitive loop.

Coordinates the real-time flow:
    perception → start_thinking → filler timer → LLM stream →
    stream_text (per chunk) → flush_stream → output guard (final)

The bridge does NOT own the voice pipeline or the cognitive loop —
it's a stateless adapter that wires them together for one request.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from sovyx.observability.logging import get_logger

if TYPE_CHECKING:
    from sovyx.cognitive.act import ActionResult
    from sovyx.cognitive.gate import CognitiveRequest
    from sovyx.cognitive.loop import CognitiveLoop
    from sovyx.voice.pipeline._orchestrator import VoicePipeline

logger = get_logger(__name__)


class VoiceCognitiveBridge:
    """Wire voice pipeline streaming to the cognitive loop.

    Usage (called by the voice pipeline's ``on_perception`` callback)::

        bridge = VoiceCognitiveBridge(cogloop, pipeline)
        result = await bridge.process(request)

    The bridge calls ``pipeline.start_thinking()`` to arm the filler
    timer, then ``cogloop.process_request_streaming()`` with a callback
    that feeds each text delta into ``pipeline.stream_text()``, and
    finally ``pipeline.flush_stream()`` to drain the last TTS buffer.
    """

    def __init__(
        self,
        cogloop: CognitiveLoop,
        pipeline: VoicePipeline,
    ) -> None:
        self._cogloop = cogloop
        self._pipeline = pipeline

    async def process(self, request: CognitiveRequest) -> ActionResult:
        """Run one voice request through the streaming cognitive loop.

        Lifecycle:
            1. ``start_thinking()`` — filler timer starts.
            2. ``process_request_streaming()`` — LLM streams, each text
               delta goes to ``stream_text()``.
            3. ``flush_stream()`` — drains remaining TTS buffer.
            4. Returns ``ActionResult`` for channel delivery / logging.
        """
        await self._pipeline.start_thinking()

        try:
            result = await self._cogloop.process_request_streaming(
                request,
                on_text_chunk=self._pipeline.stream_text,
            )
        except Exception:
            logger.exception("voice_cognitive_bridge_error")
            await self._pipeline.flush_stream()
            raise

        await self._pipeline.flush_stream()

        logger.debug(
            "voice_request_complete",
            degraded=result.degraded,
            error=result.error,
        )
        return result
