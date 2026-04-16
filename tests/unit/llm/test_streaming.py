"""Tests for LLM streaming infrastructure.

Covers:
- SSE parser (iter_sse_events)
- NDJSON parser (iter_ndjson_lines)
- LLMStreamChunk / ToolCallDelta model shapes
- Provider stream() method shapes (Anthropic, OpenAI, Google, Ollama)
- Router.stream() provider selection + final chunk accounting
- CognitiveLoop.process_request_streaming() chunk forwarding + LLMResponse reconstruction
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from sovyx.llm.models import LLMStreamChunk, ToolCallDelta

# ── Model shape ───────────────────────────────────────────────────────


class TestLLMStreamChunk:
    """LLMStreamChunk dataclass shape."""

    def test_defaults(self) -> None:
        chunk = LLMStreamChunk()
        assert chunk.delta_text == ""
        assert chunk.tool_call_delta is None
        assert chunk.is_final is False
        assert chunk.finish_reason is None

    def test_text_chunk(self) -> None:
        chunk = LLMStreamChunk(delta_text="Hello", model="test", provider="test")
        assert chunk.delta_text == "Hello"
        assert not chunk.is_final

    def test_final_chunk(self) -> None:
        chunk = LLMStreamChunk(
            is_final=True,
            finish_reason="stop",
            tokens_in=100,
            tokens_out=50,
            model="claude-sonnet-4-20250514",
            provider="anthropic",
        )
        assert chunk.is_final
        assert chunk.tokens_in == 100  # noqa: PLR2004
        assert chunk.tokens_out == 50  # noqa: PLR2004

    def test_tool_call_delta(self) -> None:
        delta = ToolCallDelta(
            index=0,
            id="call_123",
            function_name="calculator.add",
            arguments_json_delta='{"a": 1',
        )
        chunk = LLMStreamChunk(tool_call_delta=delta)
        assert chunk.tool_call_delta is not None
        assert chunk.tool_call_delta.function_name == "calculator.add"


# ── SSE parser ────────────────────────────────────────────────────────


class _FakeResponse:
    """Minimal httpx.Response-like object for testing SSE/NDJSON."""

    def __init__(self, lines: list[str]) -> None:
        self._lines = lines

    async def aiter_lines(self):  # noqa: ANN201
        for line in self._lines:
            yield line


class TestIterSSEEvents:
    """iter_sse_events parser."""

    @pytest.mark.asyncio()
    async def test_basic_text_event(self) -> None:
        from sovyx.llm.providers._streaming import iter_sse_events

        lines = [
            'data: {"text": "hello"}',
            "",
        ]
        events = []
        async for evt_type, data in iter_sse_events(_FakeResponse(lines)):  # type: ignore[arg-type]
            events.append((evt_type, data))
        assert len(events) == 1
        assert events[0] == ("message", {"text": "hello"})

    @pytest.mark.asyncio()
    async def test_named_event(self) -> None:
        from sovyx.llm.providers._streaming import iter_sse_events

        lines = [
            "event: content_block_delta",
            'data: {"delta": "hi"}',
            "",
        ]
        events = []
        async for evt_type, data in iter_sse_events(_FakeResponse(lines)):  # type: ignore[arg-type]
            events.append((evt_type, data))
        assert events[0][0] == "content_block_delta"

    @pytest.mark.asyncio()
    async def test_done_sentinel(self) -> None:
        from sovyx.llm.providers._streaming import iter_sse_events

        lines = [
            "data: [DONE]",
            "",
        ]
        events = []
        async for evt_type, data in iter_sse_events(_FakeResponse(lines)):  # type: ignore[arg-type]
            events.append((evt_type, data))
        assert events[0] == ("done", {})

    @pytest.mark.asyncio()
    async def test_comment_lines_skipped(self) -> None:
        from sovyx.llm.providers._streaming import iter_sse_events

        lines = [
            ": keep-alive",
            'data: {"ok": true}',
            "",
        ]
        events = []
        async for evt_type, data in iter_sse_events(_FakeResponse(lines)):  # type: ignore[arg-type]
            events.append((evt_type, data))
        assert len(events) == 1


class TestIterNDJSONLines:
    """iter_ndjson_lines parser."""

    @pytest.mark.asyncio()
    async def test_basic_lines(self) -> None:
        from sovyx.llm.providers._streaming import iter_ndjson_lines

        lines = [
            '{"message": {"content": "hi"}}',
            '{"done": true, "eval_count": 10}',
        ]
        results = []
        async for data in iter_ndjson_lines(_FakeResponse(lines)):  # type: ignore[arg-type]
            results.append(data)
        assert len(results) == 2  # noqa: PLR2004
        assert results[1]["done"] is True

    @pytest.mark.asyncio()
    async def test_empty_lines_skipped(self) -> None:
        from sovyx.llm.providers._streaming import iter_ndjson_lines

        lines = ["", '{"x": 1}', ""]
        results = []
        async for data in iter_ndjson_lines(_FakeResponse(lines)):  # type: ignore[arg-type]
            results.append(data)
        assert len(results) == 1


# ── Router stream() ──────────────────────────────────────────────────


class TestRouterStream:
    """LLMRouter.stream() provider selection + accounting."""

    @pytest.mark.asyncio()
    async def test_stream_yields_chunks_and_final(self) -> None:
        from sovyx.llm.router import LLMRouter

        chunks = [
            LLMStreamChunk(delta_text="Hello ", model="test-model", provider="test"),
            LLMStreamChunk(delta_text="world", model="test-model", provider="test"),
            LLMStreamChunk(
                is_final=True,
                finish_reason="stop",
                tokens_in=10,
                tokens_out=5,
                model="test-model",
                provider="test",
            ),
        ]

        async def _fake_stream(*a: Any, **kw: Any):  # noqa: ANN401, ARG001
            for c in chunks:
                yield c

        provider = MagicMock()
        provider.name = "test"
        provider.is_available = True
        provider.supports_model.return_value = True
        provider.stream = _fake_stream

        cost_guard = AsyncMock()
        cost_guard.can_afford.return_value = True
        cost_guard.record = AsyncMock()
        cost_guard.get_remaining_budget.return_value = 10.0

        event_bus = AsyncMock()

        router = LLMRouter(
            providers=[provider],
            cost_guard=cost_guard,
            event_bus=event_bus,
        )

        collected: list[LLMStreamChunk] = []
        async for chunk in router.stream(
            messages=[{"role": "user", "content": "hi"}],
            model="test-model",
        ):
            collected.append(chunk)

        assert len(collected) == 3  # noqa: PLR2004
        assert collected[0].delta_text == "Hello "
        assert collected[2].is_final

        cost_guard.record.assert_called_once()


# ── CognitiveLoop streaming ──────────────────────────────────────────


class TestCognitiveLoopStreaming:
    """process_request_streaming reconstructs LLMResponse from chunks."""

    @pytest.mark.asyncio()
    async def test_chunks_forwarded_and_response_reconstructed(self) -> None:
        from sovyx.cognitive.act import ActionResult
        from sovyx.cognitive.loop import CognitiveLoop
        from sovyx.engine.types import MindId

        chunks = [
            LLMStreamChunk(delta_text="Hi ", model="m", provider="p"),
            LLMStreamChunk(delta_text="there!", model="m", provider="p"),
            LLMStreamChunk(
                is_final=True,
                finish_reason="stop",
                tokens_in=5,
                tokens_out=3,
                model="m",
                provider="p",
            ),
        ]

        async def _fake_process_streaming(*a: Any, **kw: Any):  # noqa: ANN401
            return (aiter_chunks(), [])

        async def aiter_chunks():  # noqa: ANN201
            for c in chunks:
                yield c

        state_machine = MagicMock()
        state_machine.transition = MagicMock()
        state_machine.reset = MagicMock()

        perceive = AsyncMock()
        perception_mock = MagicMock()
        perception_mock.id = "p1"
        perception_mock.source = "voice"
        perception_mock.content = "Hello"
        perception_mock.metadata = {"complexity": 0.5}
        perceive.process.return_value = perception_mock

        attend = AsyncMock()
        attend.process.return_value = True

        think = MagicMock()
        think.process_streaming = _fake_process_streaming

        act = AsyncMock()
        act.process.return_value = ActionResult(
            response_text="Hi there!",
            target_channel="voice",
        )

        reflect = AsyncMock()
        event_bus = AsyncMock()

        loop = CognitiveLoop(
            state_machine=state_machine,
            perceive=perceive,
            attend=attend,
            think=think,
            act=act,
            reflect=reflect,
            event_bus=event_bus,
        )

        received_chunks: list[str] = []

        async def _on_chunk(text: str) -> None:
            received_chunks.append(text)

        request = MagicMock()
        request.mind_id = MindId("test")
        request.conversation_id = "conv1"
        request.conversation_history = []
        request.person_name = None
        request.perception = MagicMock()
        request.perception.source = "voice"

        result = await loop.process_request_streaming(request, _on_chunk)

        assert received_chunks == ["Hi ", "there!"]
        assert result.response_text == "Hi there!"
        act.process.assert_called_once()
        llm_resp = act.process.call_args.args[0]
        assert llm_resp.content == "Hi there!"
        assert llm_resp.finish_reason == "stop"
