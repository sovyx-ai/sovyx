"""Tests for sovyx.cognitive.loop — CognitiveLoop."""

from __future__ import annotations

from unittest.mock import AsyncMock

from sovyx.cognitive.act import ActionResult
from sovyx.cognitive.gate import CognitiveRequest
from sovyx.cognitive.loop import CognitiveLoop
from sovyx.cognitive.perceive import Perception
from sovyx.cognitive.state import CognitiveStateMachine
from sovyx.engine.types import (
    CognitivePhase,
    ConversationId,
    MindId,
    PerceptionType,
)
from sovyx.llm.models import LLMResponse

MIND = MindId("aria")
CONV = ConversationId("conv1")


def _request(content: str = "Hello") -> CognitiveRequest:
    return CognitiveRequest(
        perception=Perception(
            id="p1",
            type=PerceptionType.USER_MESSAGE,
            source="telegram",
            content=content,
        ),
        mind_id=MIND,
        conversation_id=CONV,
        conversation_history=[],
        person_name="Guipe",
    )


def _mock_perceive() -> AsyncMock:
    phase = AsyncMock()
    phase.process = AsyncMock(side_effect=lambda p: p)  # passthrough
    return phase


def _mock_attend(passes: bool = True) -> AsyncMock:
    phase = AsyncMock()
    phase.process = AsyncMock(return_value=passes)
    return phase


def _mock_think() -> AsyncMock:
    phase = AsyncMock()
    phase.process = AsyncMock(
        return_value=(
            LLMResponse(
                content="Hello!",
                model="test",
                tokens_in=10,
                tokens_out=5,
                latency_ms=100,
                cost_usd=0.0,
                finish_reason="stop",
                provider="test",
            ),
            [{"role": "user", "content": "Hello"}],
        )
    )
    return phase


def _mock_act() -> AsyncMock:
    phase = AsyncMock()
    phase.process = AsyncMock(
        return_value=ActionResult(
            response_text="Hello!", target_channel="telegram"
        )
    )
    return phase


def _mock_reflect() -> AsyncMock:
    phase = AsyncMock()
    phase.process = AsyncMock()
    return phase


def _loop(
    attend_passes: bool = True,
    think: AsyncMock | None = None,
    act: AsyncMock | None = None,
    reflect: AsyncMock | None = None,
) -> CognitiveLoop:
    return CognitiveLoop(
        state_machine=CognitiveStateMachine(),
        perceive=_mock_perceive(),
        attend=_mock_attend(attend_passes),
        think=think or _mock_think(),
        act=act or _mock_act(),
        reflect=reflect or _mock_reflect(),
        event_bus=AsyncMock(),
    )


class TestFullLoop:
    """Complete cognitive loop."""

    async def test_full_cycle(self) -> None:
        loop = _loop()
        result = await loop.process_request(_request())
        assert isinstance(result, ActionResult)
        assert result.response_text == "Hello!"
        assert result.target_channel == "telegram"

    async def test_state_machine_resets_to_idle(self) -> None:
        loop = _loop()
        await loop.process_request(_request())
        assert loop._state.current == CognitivePhase.IDLE

    async def test_all_phases_called(self) -> None:
        perceive = _mock_perceive()
        attend = _mock_attend()
        think = _mock_think()
        act = _mock_act()
        reflect = _mock_reflect()
        loop = CognitiveLoop(
            CognitiveStateMachine(), perceive, attend, think, act, reflect, AsyncMock()
        )
        await loop.process_request(_request())
        perceive.process.assert_called_once()
        attend.process.assert_called_once()
        think.process.assert_called_once()
        act.process.assert_called_once()
        reflect.process.assert_called_once()


class TestFiltering:
    """AttendPhase filtering."""

    async def test_filtered_returns_empty(self) -> None:
        loop = _loop(attend_passes=False)
        result = await loop.process_request(_request())
        assert result.filtered is True
        assert result.response_text == ""

    async def test_filtered_skips_think_act_reflect(self) -> None:
        think = _mock_think()
        loop = _loop(attend_passes=False, think=think)
        await loop.process_request(_request())
        think.process.assert_not_called()


class TestErrorHandling:
    """Error handling — never raises, always returns ActionResult."""

    async def test_perceive_error_returns_action_result(self) -> None:
        loop = _loop()
        loop._perceive.process = AsyncMock(side_effect=ValueError("bad input"))
        result = await loop.process_request(_request())
        assert result.error is True
        assert "Something went wrong" in result.response_text

    async def test_think_error_returns_action_result(self) -> None:
        think = _mock_think()
        think.process = AsyncMock(side_effect=RuntimeError("LLM down"))
        loop = _loop(think=think)
        result = await loop.process_request(_request())
        assert result.error is True

    async def test_reflect_failure_still_returns_response(self) -> None:
        reflect = _mock_reflect()
        reflect.process = AsyncMock(side_effect=RuntimeError("DB error"))
        loop = _loop(reflect=reflect)
        result = await loop.process_request(_request())
        # Should return normal response (reflect is best-effort)
        assert result.response_text == "Hello!"
        assert result.error is False

    async def test_state_machine_reset_after_error(self) -> None:
        loop = _loop()
        loop._perceive.process = AsyncMock(side_effect=RuntimeError("fail"))
        await loop.process_request(_request())
        # State machine should be reset to IDLE
        assert loop._state.current == CognitivePhase.IDLE

    async def test_never_raises(self) -> None:
        """process_request NEVER propagates exceptions."""
        loop = _loop()
        loop._perceive.process = AsyncMock(side_effect=Exception("catastrophic"))
        result = await loop.process_request(_request())
        assert isinstance(result, ActionResult)


class TestLifecycle:
    """Start/stop."""

    async def test_start_stop(self) -> None:
        loop = _loop()
        await loop.start()
        await loop.stop()
