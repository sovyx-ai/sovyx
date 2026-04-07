"""VAL-30: Telegram bridge roundtrip — full pipeline E2E.

Tests the complete message flow:
  InboundMessage → PersonResolver → ConversationTracker →
  CogLoopGate → ActionResult → OutboundMessage → ChannelAdapter

Uses real SQLite DBs (in-memory) for persistence. Mocks:
  - CogLoopGate (LLM call) → returns canned ActionResult
  - ChannelAdapter (Telegram API) → captures outbound messages
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import AsyncMock

import pytest

from sovyx.bridge.identity import PersonResolver
from sovyx.bridge.manager import BridgeManager
from sovyx.bridge.protocol import InboundMessage
from sovyx.bridge.sessions import ConversationTracker
from sovyx.cognitive.act import ActionResult
from sovyx.cognitive.gate import CogLoopGate
from sovyx.engine.events import EventBus
from sovyx.engine.types import ChannelType, MindId
from sovyx.persistence.migrations import MigrationRunner
from sovyx.persistence.pool import DatabasePool
from sovyx.persistence.schemas.conversations import get_conversation_migrations
from sovyx.persistence.schemas.system import get_system_migrations

if TYPE_CHECKING:
    from pathlib import Path


# ── Fixtures ────────────────────────────────────────────────────────────────


@pytest.fixture()
async def system_pool(tmp_path: Path) -> DatabasePool:
    """System DB with persons + channel_mappings tables."""
    pool = DatabasePool(db_path=tmp_path / "system.db", read_pool_size=1)
    await pool.initialize()
    runner = MigrationRunner(pool)
    await runner.initialize()
    await runner.run_migrations(get_system_migrations())
    yield pool  # type: ignore[misc]
    await pool.close()


@pytest.fixture()
async def conversation_pool(tmp_path: Path) -> DatabasePool:
    """Conversation DB with conversations + turns tables."""
    pool = DatabasePool(db_path=tmp_path / "conversations.db", read_pool_size=1)
    await pool.initialize()
    runner = MigrationRunner(pool)
    await runner.initialize()
    await runner.run_migrations(get_conversation_migrations())
    yield pool  # type: ignore[misc]
    await pool.close()


@pytest.fixture()
def mind_id() -> MindId:
    return MindId("test-mind")


@pytest.fixture()
def event_bus() -> EventBus:
    return EventBus()


@pytest.fixture()
def person_resolver(system_pool: DatabasePool) -> PersonResolver:
    return PersonResolver(system_pool)


@pytest.fixture()
def conversation_tracker(conversation_pool: DatabasePool) -> ConversationTracker:
    return ConversationTracker(conversation_pool, timeout_minutes=30)


@pytest.fixture()
def mock_gate() -> AsyncMock:
    """Mock CogLoopGate that returns a canned response."""
    gate = AsyncMock(spec=CogLoopGate)
    gate.submit = AsyncMock(
        return_value=ActionResult(
            response_text="Hello from Sovyx!",
            target_channel="telegram",
            reply_to="msg-001",
        ),
    )
    return gate


@pytest.fixture()
def mock_adapter() -> AsyncMock:
    """Mock ChannelAdapter that captures outbound messages."""
    adapter = AsyncMock()
    adapter.channel_type = ChannelType.TELEGRAM
    adapter.capabilities = {"send", "edit", "delete"}
    adapter.format_capabilities = {"markdown": True, "max_length": 4096}
    adapter.send = AsyncMock(return_value="sent-msg-001")
    adapter.start = AsyncMock()
    adapter.stop = AsyncMock()
    return adapter


@pytest.fixture()
def bridge_manager(
    event_bus: EventBus,
    mock_gate: AsyncMock,
    person_resolver: PersonResolver,
    conversation_tracker: ConversationTracker,
    mock_adapter: AsyncMock,
    mind_id: MindId,
) -> BridgeManager:
    """BridgeManager wired with real resolver/tracker, mock gate/adapter."""
    mgr = BridgeManager(
        event_bus=event_bus,
        cog_loop_gate=mock_gate,
        person_resolver=person_resolver,
        conversation_tracker=conversation_tracker,
        mind_id=mind_id,
    )
    mgr.register_channel(mock_adapter)
    return mgr


def _make_inbound(
    text: str = "Hello!",
    user_id: str = "tg-user-42",
    msg_id: str = "msg-001",
    chat_id: str = "chat-42",
    display_name: str = "Alice",
) -> InboundMessage:
    """Helper to create an InboundMessage."""
    return InboundMessage(
        channel_type=ChannelType.TELEGRAM,
        channel_user_id=user_id,
        channel_message_id=msg_id,
        chat_id=chat_id,
        text=text,
        display_name=display_name,
    )


# ── Full Pipeline Tests ────────────────────────────────────────────────────


class TestBridgeRoundtrip:
    """Complete message roundtrip: inbound → process → outbound."""

    @pytest.mark.asyncio()
    async def test_full_pipeline(
        self,
        bridge_manager: BridgeManager,
        mock_gate: AsyncMock,
        mock_adapter: AsyncMock,
    ) -> None:
        """Message flows through entire pipeline and response is sent."""
        inbound = _make_inbound(text="What is the meaning of life?")
        await bridge_manager.handle_inbound(inbound)

        # Gate was called with a CognitiveRequest
        mock_gate.submit.assert_called_once()
        request = mock_gate.submit.call_args[0][0]
        assert request.perception.content == "What is the meaning of life?"
        assert request.mind_id == MindId("test-mind")
        assert request.person_name == "Alice"

        # Adapter sent the response
        mock_adapter.send.assert_called_once()
        call_args = mock_adapter.send.call_args
        assert call_args[0][0] == "chat-42"  # target
        assert call_args[0][1] == "Hello from Sovyx!"  # message text

    @pytest.mark.asyncio()
    async def test_response_sent_to_correct_chat(
        self,
        bridge_manager: BridgeManager,
        mock_adapter: AsyncMock,
    ) -> None:
        """Response goes to the chat the message came from."""
        inbound = _make_inbound(chat_id="group-chat-99")
        await bridge_manager.handle_inbound(inbound)

        mock_adapter.send.assert_called_once()
        target = mock_adapter.send.call_args[0][0]
        assert target == "group-chat-99"


class TestPersonResolution:
    """Person resolver creates new person on first contact."""

    @pytest.mark.asyncio()
    async def test_new_person_created(
        self,
        bridge_manager: BridgeManager,
        system_pool: DatabasePool,
    ) -> None:
        """First message from unknown user creates a person record."""
        inbound = _make_inbound(user_id="tg-new-user", display_name="Bob")
        await bridge_manager.handle_inbound(inbound)

        # Verify person exists in DB
        async with system_pool.read() as conn:
            cursor = await conn.execute(
                "SELECT name, display_name FROM persons",
            )
            rows = await cursor.fetchall()

        assert len(rows) == 1
        assert rows[0][0] == "Bob"  # name
        assert rows[0][1] == "Bob"  # display_name

    @pytest.mark.asyncio()
    async def test_same_user_resolves_to_same_person(
        self,
        bridge_manager: BridgeManager,
        mock_gate: AsyncMock,
    ) -> None:
        """Two messages from same user → same person_id in gate requests."""
        await bridge_manager.handle_inbound(
            _make_inbound(user_id="tg-repeat", display_name="Charlie", msg_id="m1"),
        )
        await bridge_manager.handle_inbound(
            _make_inbound(user_id="tg-repeat", display_name="Charlie", msg_id="m2"),
        )

        assert mock_gate.submit.call_count == 2
        r1 = mock_gate.submit.call_args_list[0][0][0]
        r2 = mock_gate.submit.call_args_list[1][0][0]
        assert r1.perception.person_id == r2.perception.person_id

    @pytest.mark.asyncio()
    async def test_no_display_name_uses_user_id(
        self,
        bridge_manager: BridgeManager,
        system_pool: DatabasePool,
    ) -> None:
        """User without display_name → name falls back to channel_user_id."""
        inbound = _make_inbound(user_id="tg-anon-42", display_name="")
        await bridge_manager.handle_inbound(inbound)

        async with system_pool.read() as conn:
            cursor = await conn.execute("SELECT name FROM persons")
            row = await cursor.fetchone()

        assert row is not None
        assert row[0] == "tg-anon-42"

    @pytest.mark.asyncio()
    async def test_channel_mapping_created(
        self,
        bridge_manager: BridgeManager,
        system_pool: DatabasePool,
    ) -> None:
        """Channel mapping is created linking platform user to person."""
        inbound = _make_inbound(user_id="tg-mapped-user")
        await bridge_manager.handle_inbound(inbound)

        async with system_pool.read() as conn:
            cursor = await conn.execute(
                "SELECT channel_type, channel_user_id FROM channel_mappings",
            )
            row = await cursor.fetchone()

        assert row is not None
        assert row[0] == "telegram"
        assert row[1] == "tg-mapped-user"


class TestConversationTracking:
    """ConversationTracker manages conversations and history."""

    @pytest.mark.asyncio()
    async def test_conversation_created(
        self,
        bridge_manager: BridgeManager,
        conversation_pool: DatabasePool,
    ) -> None:
        """First message creates a conversation record."""
        await bridge_manager.handle_inbound(_make_inbound())

        async with conversation_pool.read() as conn:
            cursor = await conn.execute(
                "SELECT mind_id, channel, status FROM conversations",
            )
            row = await cursor.fetchone()

        assert row is not None
        assert row[0] == "test-mind"
        assert row[1] == "telegram"
        assert row[2] == "active"

    @pytest.mark.asyncio()
    async def test_turns_recorded(
        self,
        bridge_manager: BridgeManager,
        conversation_pool: DatabasePool,
    ) -> None:
        """User turn and assistant turn are both recorded."""
        await bridge_manager.handle_inbound(_make_inbound(text="Hello!"))

        async with conversation_pool.read() as conn:
            cursor = await conn.execute(
                "SELECT role, content FROM conversation_turns ORDER BY rowid",
            )
            turns = await cursor.fetchall()

        assert len(turns) == 2
        assert turns[0][0] == "user"
        assert turns[0][1] == "Hello!"
        assert turns[1][0] == "assistant"
        assert turns[1][1] == "Hello from Sovyx!"

    @pytest.mark.asyncio()
    async def test_message_count_incremented(
        self,
        bridge_manager: BridgeManager,
        conversation_pool: DatabasePool,
    ) -> None:
        """message_count in conversations table is updated."""
        await bridge_manager.handle_inbound(_make_inbound(msg_id="m1"))
        await bridge_manager.handle_inbound(_make_inbound(msg_id="m2"))

        async with conversation_pool.read() as conn:
            cursor = await conn.execute(
                "SELECT message_count FROM conversations",
            )
            row = await cursor.fetchone()

        assert row is not None
        # 2 messages × 2 turns each (user + assistant) = 4
        assert row[0] == 4

    @pytest.mark.asyncio()
    async def test_history_passed_to_gate(
        self,
        bridge_manager: BridgeManager,
        mock_gate: AsyncMock,
    ) -> None:
        """Second message includes conversation history in the request."""
        await bridge_manager.handle_inbound(_make_inbound(text="First", msg_id="m1"))
        await bridge_manager.handle_inbound(_make_inbound(text="Second", msg_id="m2"))

        assert mock_gate.submit.call_count == 2
        second_request = mock_gate.submit.call_args_list[1][0][0]

        # History from the first exchange should be present
        history = second_request.conversation_history
        assert len(history) >= 2  # at least user+assistant from first msg
        assert any(h["content"] == "First" for h in history)
        assert any(h["content"] == "Hello from Sovyx!" for h in history)


class TestGateFailure:
    """Handle CogLoopGate failures gracefully."""

    @pytest.mark.asyncio()
    async def test_gate_returns_none(
        self,
        bridge_manager: BridgeManager,
        mock_gate: AsyncMock,
        mock_adapter: AsyncMock,
    ) -> None:
        """Gate returns None → fallback error response sent."""
        mock_gate.submit.return_value = None
        await bridge_manager.handle_inbound(_make_inbound())

        # Should still send a response (fallback)
        mock_adapter.send.assert_called_once()
        sent_text = mock_adapter.send.call_args[0][1]
        assert "wrong" in sent_text.lower() or len(sent_text) > 0

    @pytest.mark.asyncio()
    async def test_gate_returns_error(
        self,
        bridge_manager: BridgeManager,
        mock_gate: AsyncMock,
        mock_adapter: AsyncMock,
    ) -> None:
        """Gate returns ActionResult with error=True → sends response text."""
        mock_gate.submit.return_value = ActionResult(
            response_text="I'm having trouble.",
            target_channel="telegram",
            error=True,
        )
        await bridge_manager.handle_inbound(_make_inbound())

        mock_adapter.send.assert_called_once()
        sent_text = mock_adapter.send.call_args[0][1]
        assert sent_text == "I'm having trouble."

    @pytest.mark.asyncio()
    async def test_gate_returns_filtered(
        self,
        bridge_manager: BridgeManager,
        mock_gate: AsyncMock,
        mock_adapter: AsyncMock,
    ) -> None:
        """Gate returns filtered=True → no response sent."""
        mock_gate.submit.return_value = ActionResult(
            response_text="",
            target_channel="telegram",
            filtered=True,
        )
        await bridge_manager.handle_inbound(_make_inbound())

        # Filtered → no response
        mock_adapter.send.assert_not_called()

    @pytest.mark.asyncio()
    async def test_gate_raises_exception(
        self,
        bridge_manager: BridgeManager,
        mock_gate: AsyncMock,
        mock_adapter: AsyncMock,
    ) -> None:
        """Gate raises CognitiveError → fallback error response."""
        from sovyx.engine.errors import CognitiveError

        mock_gate.submit.side_effect = CognitiveError("timeout")
        await bridge_manager.handle_inbound(_make_inbound())

        # Should send fallback error response
        mock_adapter.send.assert_called_once()


class TestMultipleUsers:
    """Multiple users interacting concurrently."""

    @pytest.mark.asyncio()
    async def test_different_users_different_conversations(
        self,
        bridge_manager: BridgeManager,
        conversation_pool: DatabasePool,
    ) -> None:
        """Two different users → two separate conversations."""
        await bridge_manager.handle_inbound(
            _make_inbound(user_id="user-A", display_name="Alice"),
        )
        await bridge_manager.handle_inbound(
            _make_inbound(user_id="user-B", display_name="Bob"),
        )

        async with conversation_pool.read() as conn:
            cursor = await conn.execute("SELECT COUNT(*) FROM conversations")
            count = (await cursor.fetchone())[0]

        assert count == 2

    @pytest.mark.asyncio()
    async def test_same_user_same_conversation(
        self,
        bridge_manager: BridgeManager,
        conversation_pool: DatabasePool,
    ) -> None:
        """Two messages from same user → same conversation (within timeout)."""
        await bridge_manager.handle_inbound(
            _make_inbound(user_id="user-C", msg_id="m1"),
        )
        await bridge_manager.handle_inbound(
            _make_inbound(user_id="user-C", msg_id="m2"),
        )

        async with conversation_pool.read() as conn:
            cursor = await conn.execute("SELECT COUNT(*) FROM conversations")
            count = (await cursor.fetchone())[0]

        assert count == 1


class TestEdgeCases:
    """Edge cases and error recovery."""

    @pytest.mark.asyncio()
    async def test_empty_message_text(
        self,
        bridge_manager: BridgeManager,
        mock_gate: AsyncMock,
    ) -> None:
        """Empty message text still flows through pipeline."""
        await bridge_manager.handle_inbound(_make_inbound(text=""))
        mock_gate.submit.assert_called_once()

    @pytest.mark.asyncio()
    async def test_long_message_text(
        self,
        bridge_manager: BridgeManager,
        mock_gate: AsyncMock,
    ) -> None:
        """Long message text flows through without truncation at bridge level."""
        long_text = "x" * 5000
        await bridge_manager.handle_inbound(_make_inbound(text=long_text))
        mock_gate.submit.assert_called_once()
        request = mock_gate.submit.call_args[0][0]
        assert len(request.perception.content) == 5000

    @pytest.mark.asyncio()
    async def test_adapter_send_failure_doesnt_crash(
        self,
        bridge_manager: BridgeManager,
        mock_adapter: AsyncMock,
    ) -> None:
        """If adapter.send fails, handle_inbound doesn't raise."""
        mock_adapter.send.side_effect = ConnectionError("network down")
        # Should not raise
        await bridge_manager.handle_inbound(_make_inbound())

    @pytest.mark.asyncio()
    async def test_no_adapter_registered(
        self,
        event_bus: EventBus,
        mock_gate: AsyncMock,
        person_resolver: PersonResolver,
        conversation_tracker: ConversationTracker,
        mind_id: MindId,
    ) -> None:
        """Message for unregistered channel type → no crash, no response."""
        mgr = BridgeManager(
            event_bus=event_bus,
            cog_loop_gate=mock_gate,
            person_resolver=person_resolver,
            conversation_tracker=conversation_tracker,
            mind_id=mind_id,
        )
        # No adapter registered → should handle gracefully
        await mgr.handle_inbound(_make_inbound())
