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
    """Wire voice pipeline to the cognitive loop.

    When ``streaming=True`` (default, mirrors ``LLMConfig.streaming``),
    the bridge feeds LLM token deltas into ``pipeline.stream_text()``
    for real-time TTS (~300 ms perceived latency).

    When ``streaming=False``, the bridge falls back to a full
    ``process_request()`` call and speaks the complete response via
    ``pipeline.speak()`` (3-7 s latency, but simpler and useful when
    the LLM provider doesn't support streaming or the user disables
    it in ``mind.yaml``).

    In both paths, ``start_thinking()`` fires the filler timer so the
    user hears a natural "thinking" cue while the LLM processes.
    """

    def __init__(
        self,
        cogloop: CognitiveLoop,
        pipeline: VoicePipeline,
        *,
        streaming: bool = True,
    ) -> None:
        self._cogloop = cogloop
        self._pipeline = pipeline
        self._streaming = streaming

    async def process(self, request: CognitiveRequest) -> ActionResult:
        """Run one voice request through the cognitive loop.

        Chooses the streaming or non-streaming path based on the
        ``streaming`` flag set at construction time.
        """
        await self._pipeline.start_thinking()

        if self._streaming:
            return await self._process_streaming(request)
        return await self._process_batch(request)

    async def _process_streaming(self, request: CognitiveRequest) -> ActionResult:
        """Streaming path: chunk-by-chunk TTS as the LLM produces tokens."""
        try:
            result = await self._cogloop.process_request_streaming(
                request,
                on_text_chunk=self._pipeline.stream_text,
            )
        except Exception:  # noqa: BLE001
            logger.exception("voice_cognitive_bridge_error")
            await self._pipeline.flush_stream()
            raise

        await self._pipeline.flush_stream()

        logger.debug(
            "voice_request_complete",
            degraded=result.degraded,
            error=result.error,
            streamed=True,
        )
        return result

    async def _process_batch(self, request: CognitiveRequest) -> ActionResult:
        """Non-streaming path: wait for full response, then speak it."""
        try:
            result = await self._cogloop.process_request(request)
        except Exception:  # noqa: BLE001
            logger.exception("voice_cognitive_bridge_error")
            raise

        if result.response_text and not result.filtered:
            await self._pipeline.speak(result.response_text)

        logger.debug(
            "voice_request_complete",
            degraded=result.degraded,
            error=result.error,
            streamed=False,
        )
        return result
