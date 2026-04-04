"""Tests for sovyx.bridge.channels.telegram — TelegramChannel."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock

import pytest

from sovyx.bridge.channels.telegram import TelegramChannel
from sovyx.engine.errors import ChannelConnectionError
from sovyx.engine.types import ChannelType

if TYPE_CHECKING:
    from sovyx.bridge.protocol import InboundMessage


VALID_TOKEN = "123456789:ABCdefGHIjklmnOPQrsTUVwxyz012345678"


def _mock_bridge() -> AsyncMock:
    return AsyncMock()


class TestInit:
    """Initialization."""

    def test_valid_token(self) -> None:
        ch = TelegramChannel(VALID_TOKEN, _mock_bridge())
        assert ch.channel_type == ChannelType.TELEGRAM

    def test_empty_token_raises(self) -> None:
        with pytest.raises(ChannelConnectionError, match="token"):
            TelegramChannel("", _mock_bridge())

    def test_whitespace_token_raises(self) -> None:
        with pytest.raises(ChannelConnectionError, match="token"):
            TelegramChannel("   ", _mock_bridge())


class TestProperties:
    """Protocol properties."""

    def test_channel_type(self) -> None:
        ch = TelegramChannel(VALID_TOKEN, _mock_bridge())
        assert ch.channel_type == ChannelType.TELEGRAM

    def test_capabilities(self) -> None:
        ch = TelegramChannel(VALID_TOKEN, _mock_bridge())
        assert "send" in ch.capabilities

    def test_format_capabilities(self) -> None:
        ch = TelegramChannel(VALID_TOKEN, _mock_bridge())
        caps = ch.format_capabilities
        assert caps["markdown"] is True
        assert caps["max_length"] == 4096  # noqa: PLR2004

    def test_is_running_initial(self) -> None:
        ch = TelegramChannel(VALID_TOKEN, _mock_bridge())
        assert ch.is_running is False


class TestSend:
    """Send messages."""

    async def test_send_text(self) -> None:
        ch = TelegramChannel(VALID_TOKEN, _mock_bridge())
        mock_result = MagicMock()
        mock_result.message_id = 42
        ch._bot.send_message = AsyncMock(return_value=mock_result)

        msg_id = await ch.send("123", "Hello!")
        assert msg_id == "42"
        ch._bot.send_message.assert_called_once()

    async def test_send_with_reply(self) -> None:
        ch = TelegramChannel(VALID_TOKEN, _mock_bridge())
        mock_result = MagicMock()
        mock_result.message_id = 43
        ch._bot.send_message = AsyncMock(return_value=mock_result)

        await ch.send("123", "Reply!", reply_to="10")
        call_kwargs = ch._bot.send_message.call_args
        assert call_kwargs.kwargs.get("reply_to_message_id") == 10  # noqa: PLR2004

    async def test_send_failure_raises(self) -> None:
        ch = TelegramChannel(VALID_TOKEN, _mock_bridge())
        ch._bot.send_message = AsyncMock(side_effect=RuntimeError("API error"))
        with pytest.raises(RuntimeError, match="API error"):
            await ch.send("123", "fail")


class TestOnMessage:
    """Incoming message handling."""

    async def test_text_message(self) -> None:
        bridge = _mock_bridge()
        ch = TelegramChannel(VALID_TOKEN, bridge)

        msg = MagicMock()
        msg.text = "Hello Sovyx"
        msg.message_id = 100
        msg.chat.id = 456
        msg.from_user.id = 789
        msg.from_user.full_name = "Guipe"
        msg.from_user.username = "byguipe"

        await ch._on_message(msg)

        bridge.handle_inbound.assert_called_once()
        inbound: InboundMessage = bridge.handle_inbound.call_args[0][0]
        assert inbound.text == "Hello Sovyx"
        assert inbound.channel_user_id == "789"
        assert inbound.chat_id == "456"
        assert inbound.display_name == "Guipe"

    async def test_no_text_ignored(self) -> None:
        bridge = _mock_bridge()
        ch = TelegramChannel(VALID_TOKEN, bridge)

        msg = MagicMock()
        msg.text = None
        msg.from_user = MagicMock()

        await ch._on_message(msg)
        bridge.handle_inbound.assert_not_called()

    async def test_no_from_user_ignored(self) -> None:
        bridge = _mock_bridge()
        ch = TelegramChannel(VALID_TOKEN, bridge)

        msg = MagicMock()
        msg.text = "Hello"
        msg.from_user = None

        await ch._on_message(msg)
        bridge.handle_inbound.assert_not_called()

    async def test_group_message_chat_id(self) -> None:
        """v9 fix: chat_id is group ID (negative), not user_id."""
        bridge = _mock_bridge()
        ch = TelegramChannel(VALID_TOKEN, bridge)

        msg = MagicMock()
        msg.text = "Group msg"
        msg.message_id = 200
        msg.chat.id = -100123456
        msg.from_user.id = 789
        msg.from_user.full_name = "Guipe"
        msg.from_user.username = "byguipe"

        await ch._on_message(msg)
        inbound: InboundMessage = bridge.handle_inbound.call_args[0][0]
        assert inbound.chat_id == "-100123456"
        assert inbound.channel_user_id == "789"


class TestStubs:
    """v0.5+ stubs raise NotImplementedError."""

    async def test_edit_raises(self) -> None:
        ch = TelegramChannel(VALID_TOKEN, _mock_bridge())
        with pytest.raises(NotImplementedError, match="v0.1"):
            await ch.edit("1", "new")

    async def test_delete_raises(self) -> None:
        ch = TelegramChannel(VALID_TOKEN, _mock_bridge())
        with pytest.raises(NotImplementedError, match="v0.1"):
            await ch.delete("1")

    async def test_react_raises(self) -> None:
        ch = TelegramChannel(VALID_TOKEN, _mock_bridge())
        with pytest.raises(NotImplementedError, match="v0.1"):
            await ch.react("1", "👍")

    async def test_send_typing_no_crash(self) -> None:
        """send_typing suppresses errors (best-effort)."""
        ch = TelegramChannel(VALID_TOKEN, _mock_bridge())
        # Should not raise even though bot isn't connected
        await ch.send_typing("123")


class TestInitialize:
    """Protocol compliance."""

    async def test_initialize_no_op(self) -> None:
        ch = TelegramChannel(VALID_TOKEN, _mock_bridge())
        await ch.initialize({})  # Should not crash


class TestLifecycle:
    """Start/stop."""

    async def test_start_sets_running(self) -> None:
        ch = TelegramChannel(VALID_TOKEN, _mock_bridge())
        # Patch _poll_loop to avoid actual polling
        ch._poll_loop = AsyncMock()  # type: ignore[method-assign]
        await ch.start()
        assert ch.is_running is True
        await ch.stop()

    async def test_stop_closes_session(self) -> None:
        ch = TelegramChannel(VALID_TOKEN, _mock_bridge())
        ch._bot.session.close = AsyncMock()
        ch._poll_loop = AsyncMock()  # type: ignore[method-assign]
        await ch.start()
        await ch.stop()
        ch._bot.session.close.assert_called_once()
        assert ch.is_running is False

    async def test_double_start_idempotent(self) -> None:
        ch = TelegramChannel(VALID_TOKEN, _mock_bridge())
        ch._poll_loop = AsyncMock()  # type: ignore[method-assign]
        await ch.start()
        await ch.start()  # Second start should be no-op
        assert ch.is_running is True
        await ch.stop()
