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
    # W1.2 — the bridge calls this when the cogloop reports error=True so the
    # dashboard error banner sees the cognitive-layer failure.
    pipeline.report_cognitive_error = AsyncMock()
    # current_utterance_id is a property in production; expose as a
    # plain attribute on the mock so accessing it never raises.
    pipeline.current_utterance_id = "trace-fixed-uuid"
    # v0.31.7 CR1 — the bridge's finally now reads
    # ``self._pipeline._llm_cancel_hook`` to decide whether to null
    # the hook (identity check). Mirror that real-pipeline contract on
    # the mock: ``register_llm_cancel_hook(hook)`` writes the hook
    # to ``_llm_cancel_hook``, and reading the attribute returns the
    # last-written value.
    pipeline._llm_cancel_hook = None
    # Track every register call so tests can assert on the lifecycle.
    pipeline._registered_hooks: list[Callable[[], Awaitable[None]] | None] = []

    def _register(hook: Callable[[], Awaitable[None]] | None) -> None:
        pipeline._registered_hooks.append(hook)
        pipeline._llm_cancel_hook = hook

    pipeline.register_llm_cancel_hook = MagicMock(side_effect=_register)
    return pipeline


class TestVoiceCognitiveBridgeCognitiveErrorSignal:
    """W1.2 / G-P1-1 — a real cognitive failure (error=True) surfaces on the
    voice bus; a healthy turn and a barge-in cancellation do not."""

    @pytest.mark.asyncio
    async def test_error_result_reports_cognitive_error(self) -> None:
        cogloop = MagicMock()
        degraded = ActionResult(
            response_text="I'm having trouble thinking clearly right now.",
            target_channel="voice",
            degraded=True,
            error=True,
            metadata={"reason": "llm_think_degraded"},
        )
        cogloop.process_request_streaming = AsyncMock(return_value=degraded)
        pipeline = _make_pipeline()
        bridge = VoiceCognitiveBridge(cogloop, pipeline, streaming=True)

        result = await bridge.process(_make_cog_request())

        assert result.error is True
        pipeline.report_cognitive_error.assert_awaited_once()
        # The reason token is carried into the bus signal.
        _, kwargs = pipeline.report_cognitive_error.call_args
        assert "llm_think_degraded" in kwargs["error"]

    @pytest.mark.asyncio
    async def test_healthy_result_does_not_report(self) -> None:
        cogloop = MagicMock()
        cogloop.process_request_streaming = AsyncMock(return_value=_make_action_result("hi"))
        pipeline = _make_pipeline()
        bridge = VoiceCognitiveBridge(cogloop, pipeline, streaming=True)

        await bridge.process(_make_cog_request())

        pipeline.report_cognitive_error.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_barge_in_cancellation_does_not_report(self) -> None:
        """A barge-in returns the degraded sentinel with error=False, so the
        cognitive-error signal must NOT fire (it is a user interruption, not
        an LLM failure)."""
        cogloop = MagicMock()

        async def _never_returns(_req: object) -> ActionResult:
            await asyncio.Event().wait()  # block until cancelled
            raise AssertionError("unreachable")

        cogloop.process_request_streaming = AsyncMock(side_effect=_never_returns)
        pipeline = _make_pipeline()
        bridge = VoiceCognitiveBridge(cogloop, pipeline, streaming=True)

        # Drive the cancel hook from a concurrent task once it's registered.
        async def _barge_in() -> None:
            for _ in range(100):
                hook = pipeline._llm_cancel_hook
                if hook is not None:
                    await hook()
                    return
                await asyncio.sleep(0)

        proc_task = asyncio.create_task(bridge.process(_make_cog_request()))
        await _barge_in()
        result = await proc_task

        assert result.degraded is True
        assert result.error is False
        assert result.metadata.get("cancelled_reason") == "barge_in"
        pipeline.report_cognitive_error.assert_not_awaited()


class TestVoiceCognitiveBridgeCancelHookHappyPath:
    """Happy-path lifecycle: hook registered before await, unregistered after."""

    @pytest.mark.asyncio
    async def test_streaming_returns_action_result_on_success(self) -> None:
        cogloop = MagicMock()

        async def _streams_chunks(
            _req: object,
            on_text_chunk: Callable[[str], Awaitable[None]],
        ) -> ActionResult:
            await on_text_chunk("hi")
            return _make_action_result("hi")

        cogloop.process_request_streaming = AsyncMock(side_effect=_streams_chunks)
        pipeline = _make_pipeline()
        bridge = VoiceCognitiveBridge(cogloop, pipeline, streaming=True)

        result = await bridge.process(_make_cog_request())

        assert result.response_text == "hi"
        assert result.degraded is False
        cogloop.process_request_streaming.assert_awaited_once()
        pipeline.start_thinking.assert_awaited_once()
        pipeline.stream_text.assert_awaited_once_with("hi")
        pipeline.flush_stream.assert_awaited_once()
        # Chunks were streamed — the batch fallback must NOT double-speak.
        pipeline.speak.assert_not_awaited()

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


class TestVoiceCognitiveBridgeStreamingFallbacks:
    """Zero-chunk short-circuits are spoken; batch turns always release state.

    P1 fix — pre-fix a loop result that never streamed a chunk (the C6
    dependency-gate synthetic "no LLM provider" message is the
    canonical case) was flushed silently: the user heard the thinking
    filler and then nothing, in exactly the misconfiguration the gate
    exists to explain. And the batch path left the pipeline latched in
    THINKING on empty/filtered responses and cogloop exceptions.
    """

    @pytest.mark.asyncio
    async def test_streaming_zero_chunks_speaks_response_text(self) -> None:
        cogloop = MagicMock()
        degraded = ActionResult(
            response_text="I can't respond right now — no LLM provider available.",
            target_channel="voice",
            degraded=True,
            error=True,
            metadata={"reason": "cognitive_dependency_missing"},
        )
        cogloop.process_request_streaming = AsyncMock(return_value=degraded)
        pipeline = _make_pipeline()
        bridge = VoiceCognitiveBridge(cogloop, pipeline, streaming=True)

        result = await bridge.process(_make_cog_request())

        assert result.degraded is True
        pipeline.speak.assert_awaited_once_with(degraded.response_text)
        pipeline.flush_stream.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_streaming_zero_chunks_empty_text_flushes(self) -> None:
        cogloop = MagicMock()
        cogloop.process_request_streaming = AsyncMock(return_value=_make_action_result(""))
        pipeline = _make_pipeline()
        bridge = VoiceCognitiveBridge(cogloop, pipeline, streaming=True)

        await bridge.process(_make_cog_request())

        pipeline.speak.assert_not_awaited()
        pipeline.flush_stream.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_barge_in_cancel_flushes_with_discard(self) -> None:
        """The cancelled streaming turn must DISCARD its residual buffer —
        pre-fix the normal flush synthesized the interrupted response's
        tail, so the user heard a fragment after barging in."""
        cogloop = MagicMock()
        loop_started = asyncio.Event()

        async def _never_returns(_req: object, **_kw: object) -> ActionResult:
            loop_started.set()
            await asyncio.Event().wait()
            raise AssertionError("unreachable")

        cogloop.process_request_streaming = AsyncMock(side_effect=_never_returns)
        pipeline = _make_pipeline()
        bridge = VoiceCognitiveBridge(cogloop, pipeline, streaming=True)

        async def _barge_in() -> None:
            # Wait until the cogloop body is genuinely parked — a cancel
            # delivered before the task's first step never enters
            # _process_streaming, so its CancelledError cleanup (the
            # subject under test) would be skipped.
            await asyncio.wait_for(loop_started.wait(), timeout=5.0)
            for _ in range(100):
                hook = pipeline._llm_cancel_hook
                if hook is not None:
                    await hook()
                    return
                await asyncio.sleep(0)

        proc_task = asyncio.create_task(bridge.process(_make_cog_request()))
        await _barge_in()
        result = await proc_task

        assert result.metadata.get("cancelled_reason") == "barge_in"
        pipeline.flush_stream.assert_awaited_once_with(discard_buffer=True)

    @pytest.mark.asyncio
    async def test_batch_empty_response_releases_state(self) -> None:
        cogloop = MagicMock()
        cogloop.process_request = AsyncMock(return_value=_make_action_result(""))
        pipeline = _make_pipeline()
        bridge = VoiceCognitiveBridge(cogloop, pipeline, streaming=False)

        await bridge.process(_make_cog_request())

        pipeline.speak.assert_not_awaited()
        pipeline.flush_stream.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_batch_exception_releases_state(self) -> None:
        cogloop = MagicMock()
        cogloop.process_request = AsyncMock(side_effect=RuntimeError("provider 500"))
        pipeline = _make_pipeline()
        bridge = VoiceCognitiveBridge(cogloop, pipeline, streaming=False)

        with pytest.raises(Exception) as exc_info:
            await bridge._process_batch(_make_cog_request())
        assert type(exc_info.value).__name__ == "RuntimeError"
        pipeline.flush_stream.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_bridge_registers_segment_guard(self) -> None:
        """P0 safety wire-up — construction registers the loop's
        regex-tier guard on the pipeline's per-segment hook."""
        cogloop = MagicMock()
        pipeline = _make_pipeline()

        VoiceCognitiveBridge(cogloop, pipeline, streaming=True)

        pipeline.set_stream_segment_guard.assert_called_once_with(
            cogloop.guard_streaming_segment,
        )


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


class TestVoiceCognitiveBridgeFinallyIdentityCheck:
    """v0.31.7 CR1 — the bridge's ``finally`` block must only null
    ``_llm_cancel_hook`` when the live hook is STILL the one this turn
    registered. If a later turn already replaced the hook (the race
    window between turn-N cancellation and turn-N+1 registration), the
    identity check guarantees we don't null the new turn's live hook.
    """

    @pytest.mark.asyncio
    async def test_finally_only_nulls_hook_if_owned(self) -> None:
        """When an external party (turn N+1) replaces the cancel hook
        before turn N's ``finally`` runs, ``finally`` MUST leave the
        replacement intact.

        Reproduces the cancel-hook null race documented in
        cognitive_bridge.py finally block. Without the identity check,
        turn N's finally would null the replacement and turn N+1's
        barge-in would see ``llm_cancel="no_hook_registered"``.
        """
        cogloop = MagicMock()
        cogloop.process_request_streaming = AsyncMock(return_value=_make_action_result())
        pipeline = _make_pipeline()
        bridge = VoiceCognitiveBridge(cogloop, pipeline, streaming=True)

        # Sentinel hook representing "turn N+1 already took over".
        async def _hook_b() -> None:  # pragma: no cover — marker only
            return None

        # Patch process_request_streaming so RIGHT BEFORE returning, we
        # simulate turn N+1 swapping in its own hook on the pipeline.
        # This is exactly the race the v0.31.7 fix defends against.
        original_streaming = cogloop.process_request_streaming

        async def _streaming_with_external_swap(*args: object, **kwargs: object) -> ActionResult:
            result = await original_streaming(*args, **kwargs)
            # Simulate turn N+1's bridge.process registering its hook
            # before turn N's finally runs.
            pipeline.register_llm_cancel_hook(_hook_b)
            return result

        cogloop.process_request_streaming = _streaming_with_external_swap  # type: ignore[assignment]

        await bridge.process(_make_cog_request())

        # The replacement hook must STILL be the live hook. Turn N's
        # finally must NOT have nulled it.
        assert pipeline._llm_cancel_hook is _hook_b
        # The last register call from turn N's finally MUST NOT have
        # been ``None`` (it short-circuited the identity check). The
        # only register calls should be:
        #   1. turn N's own cancel hook (callable)
        #   2. _hook_b (turn N+1 takeover, simulated)
        # No third call to ``None`` from turn N's finally.
        assert len(pipeline._registered_hooks) == 2  # noqa: PLR2004
        assert callable(pipeline._registered_hooks[0])
        assert pipeline._registered_hooks[1] is _hook_b

    @pytest.mark.asyncio
    async def test_finally_nulls_hook_when_still_owned(self) -> None:
        """Happy path: when no external party replaced the hook, the
        bridge's ``finally`` MUST null it (the pre-v0.31.7 behaviour
        that Part A's identity check preserves)."""
        cogloop = MagicMock()
        cogloop.process_request_streaming = AsyncMock(return_value=_make_action_result())
        pipeline = _make_pipeline()
        bridge = VoiceCognitiveBridge(cogloop, pipeline, streaming=True)

        await bridge.process(_make_cog_request())

        # Final state: hook nulled (no race interference).
        assert pipeline._llm_cancel_hook is None
        # Lifecycle: register(callable) → register(None).
        assert len(pipeline._registered_hooks) == 2  # noqa: PLR2004
        assert callable(pipeline._registered_hooks[0])
        assert pipeline._registered_hooks[1] is None

    @pytest.mark.asyncio
    async def test_finally_identity_check_uses_is_not_equality(self) -> None:
        """The identity check must use ``is`` (object identity), not
        ``==``. Two distinct closures are never equal at the identity
        level even if their behaviour matches — this is the contract
        we depend on for the race-condition fix."""
        cogloop = MagicMock()
        cogloop.process_request_streaming = AsyncMock(return_value=_make_action_result())
        pipeline = _make_pipeline()
        bridge = VoiceCognitiveBridge(cogloop, pipeline, streaming=True)

        # A hook with an ``__eq__`` that returns True for any other
        # callable would FOOL an ``==`` check but not an ``is`` check.
        class _AlwaysEqualHook:
            async def __call__(self) -> None:  # pragma: no cover
                return None

            def __eq__(self, _other: object) -> bool:
                return True

            def __hash__(self) -> int:
                return 0

        intruder = _AlwaysEqualHook()

        original_streaming = cogloop.process_request_streaming

        async def _streaming_with_intruder(*args: object, **kwargs: object) -> ActionResult:
            result = await original_streaming(*args, **kwargs)
            pipeline.register_llm_cancel_hook(intruder)
            return result

        cogloop.process_request_streaming = _streaming_with_intruder  # type: ignore[assignment]

        await bridge.process(_make_cog_request())

        # If the bridge used ``==`` instead of ``is``, the intruder's
        # ``__eq__`` would have made the comparison true and the
        # finally would have nulled it. ``is`` survives the intrusion.
        assert pipeline._llm_cancel_hook is intruder
