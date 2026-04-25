"""Tests for ``sovyx.voice.cognitive_bridge`` — Gap 2 LLM cancel hook.

Covers the bridge's barge-in cancellation contract introduced for
Mission Gap 2:

* ``process()`` registers an LLM cancel hook with the pipeline before
  awaiting the cogloop task and unregisters it in ``finally``.
* The hook actually cancels the in-flight cogloop task.
* On the cancellation path, the bridge returns a sentinel
  :class:`ActionResult` with ``degraded=True`` and
  ``metadata['cancelled_reason'] == 'barge_in'`` instead of
  re-raising :class:`asyncio.CancelledError` (which would crash the
  capture consumer).
* Hook lifecycle is correct on the happy path AND on the regular
  exception path.

Reference: MISSION-voice-mixer-enterprise-refactor-2026-04-25 §3.4
(Ring 5 cancellation chain), §3.6 (orchestrator cancel hook contract).
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock

import pytest

from sovyx.cognitive.act import ActionResult
from sovyx.cognitive.gate import CognitiveRequest
from sovyx.cognitive.perceive import Perception
from sovyx.engine.types import ConversationId, MindId, PerceptionType
from sovyx.voice.cognitive_bridge import VoiceCognitiveBridge

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable


def _make_action_result(text: str = "ok") -> ActionResult:
    """Build a real :class:`ActionResult` for the cogloop mock."""
    return ActionResult(response_text=text, target_channel="voice")


def _make_cog_request(mind_id: str = "demo-mind", text: str = "hello") -> CognitiveRequest:
    """Build a real :class:`CognitiveRequest` so type hints resolve."""
    from uuid import uuid4

    return CognitiveRequest(
        perception=Perception(
            id=str(uuid4()),
            type=PerceptionType.USER_MESSAGE,
            source="voice",
            content=text,
        ),
        mind_id=MindId(mind_id),
        conversation_id=ConversationId(f"voice-{mind_id}"),
        conversation_history=[],
        person_name=None,
    )


def _make_pipeline() -> MagicMock:
    """Mock pipeline exposing the surface the bridge actually touches."""
    pipeline = MagicMock()
    pipeline.start_thinking = AsyncMock()
    pipeline.stream_text = AsyncMock()
    pipeline.flush_stream = AsyncMock()
    pipeline.speak = AsyncMock()
    # current_utterance_id is a property in production; expose as a
    # plain attribute on the mock so accessing it never raises.
    pipeline.current_utterance_id = "trace-fixed-uuid"
    # Track every register call so tests can assert on the lifecycle.
    pipeline._registered_hooks: list[Callable[[], Awaitable[None]] | None] = []

    def _register(hook: Callable[[], Awaitable[None]] | None) -> None:
        pipeline._registered_hooks.append(hook)

    pipeline.register_llm_cancel_hook = MagicMock(side_effect=_register)
    return pipeline


class TestVoiceCognitiveBridgeCancelHookHappyPath:
    """Happy-path lifecycle: hook registered before await, unregistered after."""

    @pytest.mark.asyncio
    async def test_streaming_returns_action_result_on_success(self) -> None:
        cogloop = MagicMock()
        cogloop.process_request_streaming = AsyncMock(return_value=_make_action_result("hi"))
        pipeline = _make_pipeline()
        bridge = VoiceCognitiveBridge(cogloop, pipeline, streaming=True)

        result = await bridge.process(_make_cog_request())

        assert result.response_text == "hi"
        assert result.degraded is False
        cogloop.process_request_streaming.assert_awaited_once()
        pipeline.start_thinking.assert_awaited_once()
        pipeline.flush_stream.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_non_streaming_returns_action_result_on_success(self) -> None:
        cogloop = MagicMock()
        cogloop.process_request = AsyncMock(return_value=_make_action_result("done"))
        pipeline = _make_pipeline()
        bridge = VoiceCognitiveBridge(cogloop, pipeline, streaming=False)

        result = await bridge.process(_make_cog_request())

        assert result.response_text == "done"
        cogloop.process_request.assert_awaited_once()
        pipeline.speak.assert_awaited_once_with("done")

    @pytest.mark.asyncio
    async def test_hook_registered_then_unregistered_on_success(self) -> None:
        """The hook lifecycle: register(callable) then register(None)."""
        cogloop = MagicMock()
        cogloop.process_request_streaming = AsyncMock(return_value=_make_action_result())
        pipeline = _make_pipeline()
        bridge = VoiceCognitiveBridge(cogloop, pipeline, streaming=True)

        await bridge.process(_make_cog_request())

        # Two calls: a callable in (register), then None (unregister).
        assert len(pipeline._registered_hooks) == 2  # noqa: PLR2004
        assert callable(pipeline._registered_hooks[0])
        assert pipeline._registered_hooks[1] is None


class TestVoiceCognitiveBridgeCancelHookFiring:
    """Cancellation path: hook fires → task cancels → sentinel returned."""

    @pytest.mark.asyncio
    async def test_hook_cancels_in_flight_cogloop_task(self) -> None:
        """Invoking the registered hook cancels the spawned cogloop task,
        the bridge's ``await`` raises CancelledError, and process()
        returns the sentinel ActionResult — never re-raises."""
        # cogloop call hangs indefinitely so the test owns the
        # cancellation timing instead of racing the scheduler.
        cogloop_started = asyncio.Event()

        async def _hung_cogloop(*_args: object, **_kwargs: object) -> object:
            cogloop_started.set()
            await asyncio.sleep(60)  # noqa: ASYNC110 — test holds open until cancel
            return _make_action_result("never-reached")

        cogloop = MagicMock()
        cogloop.process_request_streaming = AsyncMock(side_effect=_hung_cogloop)
        pipeline = _make_pipeline()
        bridge = VoiceCognitiveBridge(cogloop, pipeline, streaming=True)

        # Spawn process() so we can fire the hook concurrently.
        process_task: asyncio.Task[object] = asyncio.create_task(
            bridge.process(_make_cog_request()),
        )
        # Wait until the hook has been registered AND cogloop task is
        # actually running. The first registered entry is the live hook.
        await cogloop_started.wait()
        await asyncio.sleep(0)  # let process() reach the await on cogloop_task
        assert pipeline._registered_hooks
        live_hook = pipeline._registered_hooks[0]
        assert live_hook is not None

        # Fire the hook — simulates the orchestrator's
        # cancel_speech_chain step 3 invoking the registered hook.
        await live_hook()

        result = await process_task
        # Sentinel ActionResult — degraded + metadata reason populated.
        assert result.degraded is True
        assert result.response_text == ""
        assert result.metadata.get("cancelled_reason") == "barge_in"

        # Hook lifecycle: registered + unregistered (None at end).
        assert pipeline._registered_hooks[-1] is None

    @pytest.mark.asyncio
    async def test_hook_idempotent_on_done_task(self) -> None:
        """Calling the hook after the cogloop task has already completed
        is a no-op (no exception). Required because the orchestrator
        contract permits multiple invocations across barge-in events."""
        cogloop = MagicMock()
        cogloop.process_request_streaming = AsyncMock(return_value=_make_action_result())
        pipeline = _make_pipeline()
        bridge = VoiceCognitiveBridge(cogloop, pipeline, streaming=True)

        # Capture the hook registered before completion.
        captured: dict[str, Callable[[], Awaitable[None]] | None] = {"hook": None}

        def _capture(hook: Callable[[], Awaitable[None]] | None) -> None:
            if hook is not None and captured["hook"] is None:
                captured["hook"] = hook
            pipeline._registered_hooks.append(hook)

        pipeline.register_llm_cancel_hook = MagicMock(side_effect=_capture)

        await bridge.process(_make_cog_request())
        # cogloop task is done by now — hook should still be safe to invoke.
        assert captured["hook"] is not None
        # Should not raise.
        await captured["hook"]()


class TestVoiceCognitiveBridgeExceptionUnwiring:
    """Hook is unregistered even when the cogloop raises a regular exception."""

    @pytest.mark.asyncio
    async def test_hook_unregistered_on_streaming_exception(self) -> None:
        cogloop = MagicMock()
        cogloop.process_request_streaming = AsyncMock(side_effect=RuntimeError("boom"))
        pipeline = _make_pipeline()
        bridge = VoiceCognitiveBridge(cogloop, pipeline, streaming=True)

        with pytest.raises(RuntimeError, match="boom"):
            await bridge.process(_make_cog_request())

        # Last hook entry must be None (unwired in finally).
        assert pipeline._registered_hooks[-1] is None
        # flush_stream still ran — bridge contract preserved.
        pipeline.flush_stream.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_hook_unregistered_on_batch_exception(self) -> None:
        cogloop = MagicMock()
        cogloop.process_request = AsyncMock(side_effect=RuntimeError("bad"))
        pipeline = _make_pipeline()
        bridge = VoiceCognitiveBridge(cogloop, pipeline, streaming=False)

        with pytest.raises(RuntimeError, match="bad"):
            await bridge.process(_make_cog_request())

        assert pipeline._registered_hooks[-1] is None


class TestVoiceCognitiveBridgeNoHookCallsOutsideLifecycle:
    """Regression: a stale hook from a prior turn must not survive into
    a new turn. After every ``process`` call the registered hook is
    None, not a leftover callable."""

    @pytest.mark.asyncio
    async def test_two_sequential_turns_each_cleanly_unregister(self) -> None:
        cogloop = MagicMock()
        cogloop.process_request_streaming = AsyncMock(return_value=_make_action_result())
        pipeline = _make_pipeline()
        bridge = VoiceCognitiveBridge(cogloop, pipeline, streaming=True)

        await bridge.process(_make_cog_request())
        await bridge.process(_make_cog_request("demo-mind", "again"))

        # Two turns × (register + unregister) = 4 entries.
        assert len(pipeline._registered_hooks) == 4  # noqa: PLR2004
        # Every odd index is None (the unregister of each turn).
        assert pipeline._registered_hooks[1] is None
        assert pipeline._registered_hooks[3] is None
        # Every even index is callable (the register of each turn).
        assert callable(pipeline._registered_hooks[0])
        assert callable(pipeline._registered_hooks[2])
        # Each turn registered a NEW callable, not the same object —
        # the closure binds per-call.
        assert pipeline._registered_hooks[0] is not pipeline._registered_hooks[2]
