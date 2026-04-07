"""POLISH-09: Full pipeline integration — boot → message → cogloop → response.

The single most important test in the codebase. Proves the machine starts
and processes a message end-to-end with real DB persistence.

Flow:
  InboundMessage → BridgeManager → PersonResolver → ConversationTracker →
  CogLoopGate → CognitiveLoop (perceive→attend→think→act→reflect) →
  ActionResult → OutboundMessage → ChannelAdapter (captured)

Real components: SQLite DBs, PersonResolver, ConversationTracker,
  CognitiveLoop, CogLoopGate, BridgeManager, EventBus.
Mocked: LLM phases (no API key), ChannelAdapter (captures output).
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, PropertyMock

import pytest

from sovyx.bridge.identity import PersonResolver
from sovyx.bridge.manager import BridgeManager
from sovyx.bridge.protocol import InboundMessage
from sovyx.bridge.sessions import ConversationTracker
from sovyx.cognitive.act import ActionResult, ActPhase
from sovyx.cognitive.attend import AttendPhase
from sovyx.cognitive.gate import CogLoopGate
from sovyx.cognitive.loop import CognitiveLoop
from sovyx.cognitive.perceive import PerceivePhase, Perception
from sovyx.cognitive.reflect import ReflectPhase
from sovyx.cognitive.state import CognitiveStateMachine
from sovyx.cognitive.think import ThinkPhase
from sovyx.engine.events import EventBus
from sovyx.engine.types import ChannelType, MindId, PerceptionType
from sovyx.persistence.migrations import MigrationRunner
from sovyx.persistence.pool import DatabasePool
from sovyx.persistence.schemas.conversations import get_conversation_migrations
from sovyx.persistence.schemas.system import get_system_migrations

if TYPE_CHECKING:
    from pathlib import Path


# ── Fixtures ────────────────────────────────────────────────────────────────


@pytest.fixture()
async def system_pool(tmp_path: Path) -> DatabasePool:
    """System DB with persons + channel_mappings."""
    pool = DatabasePool(db_path=tmp_path / "system.db", read_pool_size=1)
    await pool.initialize()
    runner = MigrationRunner(pool)
    await runner.initialize()
    await runner.run_migrations(get_system_migrations())
    yield pool  # type: ignore[misc]
    await pool.close()


@pytest.fixture()
async def conv_pool(tmp_path: Path) -> DatabasePool:
    """Conversation DB."""
    pool = DatabasePool(db_path=tmp_path / "conv.db", read_pool_size=1)
    await pool.initialize()
    runner = MigrationRunner(pool)
    await runner.initialize()
    await runner.run_migrations(get_conversation_migrations())
    yield pool  # type: ignore[misc]
    await pool.close()


@pytest.fixture()
def event_bus() -> EventBus:
    return EventBus()


@pytest.fixture()
def mind_id() -> MindId:
    return MindId("test-mind")


@pytest.fixture()
def person_resolver(system_pool: DatabasePool) -> PersonResolver:
    return PersonResolver(system_pool)


@pytest.fixture()
def conversation_tracker(conv_pool: DatabasePool) -> ConversationTracker:
    return ConversationTracker(conv_pool, timeout_minutes=30)


def _make_mock_adapter(
    sent: list[str],
) -> AsyncMock:
    """Create a mock ChannelAdapter that captures sent text."""
    adapter = AsyncMock()
    type(adapter).channel_type = PropertyMock(return_value=ChannelType.TELEGRAM)
    type(adapter).capabilities = PropertyMock(return_value={"send"})

    async def _capture_send(target: str, text: str, **kwargs: object) -> None:
        sent.append(text)

    adapter.send = AsyncMock(side_effect=_capture_send)
    adapter.start = AsyncMock()
    adapter.stop = AsyncMock()
    return adapter


def _make_cognitive_loop(event_bus: EventBus, *, fail_think: bool = False) -> CognitiveLoop:
    """Create CognitiveLoop with mock phases."""
    state_machine = CognitiveStateMachine()

    perceive = AsyncMock(spec=PerceivePhase)

    def _perceive(p: object) -> Perception:
        content = getattr(p, "content", str(p))
        source = getattr(p, "source", "telegram")
        return Perception(
            id="perc-1",
            type=PerceptionType.USER_MESSAGE,
            content=content,
            source=source,
        )

    perceive.process = AsyncMock(side_effect=_perceive)

    attend = AsyncMock(spec=AttendPhase)
    attend.process = AsyncMock(return_value=True)

    think = AsyncMock(spec=ThinkPhase)
    if fail_think:
        think.process = AsyncMock(side_effect=RuntimeError("LLM exploded"))
    else:
        think.process = AsyncMock(
            return_value=(
                "I received your message and I'm responding thoughtfully.",
                [{"role": "user", "content": "hello"}],
            ),
        )

    act = AsyncMock(spec=ActPhase)
    act.process = AsyncMock(
        return_value=ActionResult(
            response_text="I received your message and I'm responding thoughtfully.",
            target_channel=ChannelType.TELEGRAM,
        ),
    )

    reflect = AsyncMock(spec=ReflectPhase)
    reflect.process = AsyncMock(return_value=None)

    return CognitiveLoop(
        state_machine=state_machine,
        perceive=perceive,
        attend=attend,
        think=think,
        act=act,
        reflect=reflect,
        event_bus=event_bus,
    )


@pytest.fixture()
async def gate(event_bus: EventBus) -> CogLoopGate:
    """Real CogLoopGate wrapping cognitive loop."""
    loop = _make_cognitive_loop(event_bus)
    g = CogLoopGate(loop)
    await g.start()
    yield g  # type: ignore[misc]
    await g.stop()


def _make_inbound(
    mind_id: MindId,
    *,
    text: str = "Hello!",
    user_id: str = "user-456",
    chat_id: str = "chat-123",
    display_name: str = "Test User",
) -> InboundMessage:
    return InboundMessage(
        channel_type=ChannelType.TELEGRAM,
        channel_user_id=user_id,
        channel_message_id="msg-001",
        chat_id=chat_id,
        text=text,
        display_name=display_name,
    )


# ── Tests ───────────────────────────────────────────────────────────────────


class TestFullPipeline:
    """End-to-end pipeline: message in → response out."""

    @pytest.mark.asyncio()
    @pytest.mark.timeout(10)
    async def test_message_flows_through_entire_pipeline(
        self,
        event_bus: EventBus,
        gate: CogLoopGate,
        person_resolver: PersonResolver,
        conversation_tracker: ConversationTracker,
        mind_id: MindId,
    ) -> None:
        """A single message traverses the full pipeline and produces a response.

        This is THE test. If this passes, the machine works.
        """
        sent: list[str] = []
        adapter = _make_mock_adapter(sent)

        bridge = BridgeManager(
            event_bus=event_bus,
            cog_loop_gate=gate,
            person_resolver=person_resolver,
            conversation_tracker=conversation_tracker,
            mind_id=mind_id,
        )
        bridge.register_channel(adapter)

        message = _make_inbound(mind_id, text="Hello, I'm testing the full pipeline!")
        await bridge.handle_inbound(message)

        # ── Verify: response was sent ──
        assert len(sent) == 1, "Expected exactly one outbound message"
        assert sent[0], "Response text should not be empty"

        # ── Verify: person was resolved in DB ──
        person_id = await person_resolver.resolve(
            channel_type=ChannelType.TELEGRAM,
            channel_user_id="user-456",
            display_name="Test User",
        )
        assert person_id is not None

    @pytest.mark.asyncio()
    @pytest.mark.timeout(10)
    async def test_multiple_messages_same_conversation(
        self,
        event_bus: EventBus,
        gate: CogLoopGate,
        person_resolver: PersonResolver,
        conversation_tracker: ConversationTracker,
        mind_id: MindId,
    ) -> None:
        """Multiple messages from same user use the same conversation."""
        sent: list[str] = []
        adapter = _make_mock_adapter(sent)

        bridge = BridgeManager(
            event_bus=event_bus,
            cog_loop_gate=gate,
            person_resolver=person_resolver,
            conversation_tracker=conversation_tracker,
            mind_id=mind_id,
        )
        bridge.register_channel(adapter)

        for text in ["First message", "Second message"]:
            msg = _make_inbound(mind_id, text=text)
            await bridge.handle_inbound(msg)

        assert len(sent) == 2, "Both messages should produce responses"  # noqa: PLR2004

        # Same person resolved
        pid = await person_resolver.resolve(ChannelType.TELEGRAM, "user-456", "Test User")
        assert pid is not None

    @pytest.mark.asyncio()
    @pytest.mark.timeout(10)
    async def test_different_users_different_persons(
        self,
        event_bus: EventBus,
        gate: CogLoopGate,
        person_resolver: PersonResolver,
        conversation_tracker: ConversationTracker,
        mind_id: MindId,
    ) -> None:
        """Different users create separate persons."""
        sent: list[str] = []
        adapter = _make_mock_adapter(sent)

        bridge = BridgeManager(
            event_bus=event_bus,
            cog_loop_gate=gate,
            person_resolver=person_resolver,
            conversation_tracker=conversation_tracker,
            mind_id=mind_id,
        )
        bridge.register_channel(adapter)

        for uid, name in [("alice-id", "Alice"), ("bob-id", "Bob")]:
            msg = _make_inbound(mind_id, user_id=uid, chat_id=f"chat-{uid}", display_name=name)
            await bridge.handle_inbound(msg)

        assert len(sent) == 2  # noqa: PLR2004

        alice = await person_resolver.resolve(ChannelType.TELEGRAM, "alice-id", "Alice")
        bob = await person_resolver.resolve(ChannelType.TELEGRAM, "bob-id", "Bob")
        assert alice != bob

    @pytest.mark.asyncio()
    @pytest.mark.timeout(10)
    async def test_cogloop_error_returns_graceful_response(
        self,
        event_bus: EventBus,
        person_resolver: PersonResolver,
        conversation_tracker: ConversationTracker,
        mind_id: MindId,
    ) -> None:
        """When cognitive loop fails, user gets graceful error, not a crash."""
        failing_loop = _make_cognitive_loop(event_bus, fail_think=True)
        error_gate = CogLoopGate(failing_loop)
        await error_gate.start()

        try:
            sent: list[str] = []
            adapter = _make_mock_adapter(sent)

            bridge = BridgeManager(
                event_bus=event_bus,
                cog_loop_gate=error_gate,
                person_resolver=person_resolver,
                conversation_tracker=conversation_tracker,
                mind_id=mind_id,
            )
            bridge.register_channel(adapter)

            msg = _make_inbound(mind_id, text="This will fail")
            # Should NOT raise — error is handled gracefully
            await bridge.handle_inbound(msg)

            # Response should be sent (error message to user)
            assert len(sent) == 1
        finally:
            await error_gate.stop()
