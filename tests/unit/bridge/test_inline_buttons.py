"""Tests for TASK-340: InlineButton protocol + OutboundMessage buttons.

Covers:
- InlineButton creation and immutability (frozen dataclass)
- OutboundMessage with buttons field
- InboundMessage callback_data / callback_message_id properties
- OutboundMessage edit_message_id field
- TelegramChannel._build_inline_keyboard conversion
- SignalChannel._append_button_text conversion
- BridgeManager._send_response passes buttons to adapter
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from sovyx.bridge.protocol import InboundMessage, InlineButton, OutboundMessage
from sovyx.engine.types import ChannelType

# ── InlineButton ──


class TestInlineButton:
    """InlineButton dataclass tests."""

    def test_creation(self) -> None:
        btn = InlineButton(text="Approve", callback_data="fin_confirm:abc123")
        assert btn.text == "Approve"
        assert btn.callback_data == "fin_confirm:abc123"

    def test_frozen(self) -> None:
        btn = InlineButton(text="X", callback_data="y")
        with pytest.raises(AttributeError):
            btn.text = "Z"  # type: ignore[misc]

    def test_slots(self) -> None:
        btn = InlineButton(text="X", callback_data="y")
        assert hasattr(btn, "__slots__")

    def test_equality(self) -> None:
        a = InlineButton(text="OK", callback_data="ok")
        b = InlineButton(text="OK", callback_data="ok")
        assert a == b

    def test_inequality(self) -> None:
        a = InlineButton(text="OK", callback_data="ok")
        b = InlineButton(text="OK", callback_data="cancel")
        assert a != b


# ── OutboundMessage ──


class TestOutboundMessage:
    """OutboundMessage with buttons and edit_message_id."""

    def test_default_no_buttons(self) -> None:
        msg = OutboundMessage(
            channel_type=ChannelType.TELEGRAM,
            target="123",
            text="Hello",
        )
        assert msg.buttons is None
        assert msg.edit_message_id is None

    def test_with_buttons(self) -> None:
        btn1 = InlineButton(text="✅ Approve", callback_data="fin_confirm:x")
        btn2 = InlineButton(text="❌ Deny", callback_data="fin_cancel:x")
        msg = OutboundMessage(
            channel_type=ChannelType.TELEGRAM,
            target="123",
            text="Confirm?",
            buttons=[[btn1, btn2]],
        )
        assert msg.buttons is not None
        assert len(msg.buttons) == 1
        assert len(msg.buttons[0]) == 2
        assert msg.buttons[0][0].text == "✅ Approve"  # type: ignore[union-attr]

    def test_with_edit_message_id(self) -> None:
        msg = OutboundMessage(
            channel_type=ChannelType.TELEGRAM,
            target="123",
            text="Updated",
            edit_message_id="456",
        )
        assert msg.edit_message_id == "456"

    def test_multiple_rows(self) -> None:
        row1 = [InlineButton(text="A", callback_data="a")]
        row2 = [
            InlineButton(text="B", callback_data="b"),
            InlineButton(text="C", callback_data="c"),
        ]
        msg = OutboundMessage(
            channel_type=ChannelType.TELEGRAM,
            target="123",
            text="Pick",
            buttons=[row1, row2],
        )
        assert len(msg.buttons) == 2  # type: ignore[arg-type]
        assert len(msg.buttons[1]) == 2  # type: ignore[index]


# ── InboundMessage ──


class TestInboundMessageCallback:
    """InboundMessage callback_data and callback_message_id properties."""

    def test_no_callback(self) -> None:
        msg = InboundMessage(
            channel_type=ChannelType.TELEGRAM,
            channel_user_id="u1",
            channel_message_id="m1",
            chat_id="c1",
            text="hello",
        )
        assert msg.callback_data is None
        assert msg.callback_message_id is None

    def test_with_callback(self) -> None:
        msg = InboundMessage(
            channel_type=ChannelType.TELEGRAM,
            channel_user_id="u1",
            channel_message_id="m1",
            chat_id="c1",
            text="",
            metadata={
                "callback_data": "fin_confirm:abc",
                "callback_message_id": "789",
            },
        )
        assert msg.callback_data == "fin_confirm:abc"
        assert msg.callback_message_id == "789"

    def test_callback_data_coerced_to_string(self) -> None:
        msg = InboundMessage(
            channel_type=ChannelType.TELEGRAM,
            channel_user_id="u1",
            channel_message_id="m1",
            chat_id="c1",
            text="",
            metadata={"callback_data": 42},
        )
        assert msg.callback_data == "42"


# ── TelegramChannel._build_inline_keyboard ──


class TestTelegramInlineKeyboard:
    """TelegramChannel._build_inline_keyboard conversion."""

    def test_single_row(self) -> None:
        from sovyx.bridge.channels.telegram import TelegramChannel

        btn1 = InlineButton(text="✅ Yes", callback_data="yes")
        btn2 = InlineButton(text="❌ No", callback_data="no")
        markup = TelegramChannel._build_inline_keyboard([[btn1, btn2]])

        assert len(markup.inline_keyboard) == 1
        assert len(markup.inline_keyboard[0]) == 2
        assert markup.inline_keyboard[0][0].text == "✅ Yes"
        assert markup.inline_keyboard[0][0].callback_data == "yes"
        assert markup.inline_keyboard[0][1].text == "❌ No"
        assert markup.inline_keyboard[0][1].callback_data == "no"

    def test_multiple_rows(self) -> None:
        from sovyx.bridge.channels.telegram import TelegramChannel

        row1 = [InlineButton(text="A", callback_data="a")]
        row2 = [
            InlineButton(text="B", callback_data="b"),
            InlineButton(text="C", callback_data="c"),
        ]
        markup = TelegramChannel._build_inline_keyboard([row1, row2])

        assert len(markup.inline_keyboard) == 2
        assert len(markup.inline_keyboard[0]) == 1
        assert len(markup.inline_keyboard[1]) == 2

    def test_empty_buttons(self) -> None:
        from sovyx.bridge.channels.telegram import TelegramChannel

        markup = TelegramChannel._build_inline_keyboard([])
        assert len(markup.inline_keyboard) == 0


# ── SignalChannel._append_button_text ──


class TestSignalButtonText:
    """SignalChannel._append_button_text conversion."""

    def test_two_buttons(self) -> None:
        from sovyx.bridge.channels.signal import SignalChannel

        btn1 = InlineButton(text="Approve", callback_data="ok")
        btn2 = InlineButton(text="Deny", callback_data="no")
        result = SignalChannel._append_button_text("Confirm?", [[btn1, btn2]])
        assert "1️⃣ Approve" in result
        assert "2️⃣ Deny" in result
        assert result.startswith("Confirm?")

    def test_no_buttons(self) -> None:
        from sovyx.bridge.channels.signal import SignalChannel

        result = SignalChannel._append_button_text("Hello", [])
        assert result == "Hello"

    def test_multiple_rows(self) -> None:
        from sovyx.bridge.channels.signal import SignalChannel

        row1 = [InlineButton(text="A", callback_data="a")]
        row2 = [InlineButton(text="B", callback_data="b")]
        result = SignalChannel._append_button_text("Pick:", [row1, row2])
        assert "1️⃣ A" in result
        assert "2️⃣ B" in result

    def test_more_than_nine_buttons(self) -> None:
        from sovyx.bridge.channels.signal import SignalChannel

        btns = [[InlineButton(text=f"Opt{i}", callback_data=f"o{i}")] for i in range(11)]
        result = SignalChannel._append_button_text("Choose:", btns)
        assert "10. Opt9" in result
        assert "11. Opt10" in result


# ── BridgeManager._send_response ──


class TestBridgeManagerSendButtons:
    """BridgeManager._send_response passes buttons to adapter."""

    def _make_manager(self, adapter: object) -> object:
        from sovyx.bridge.manager import BridgeManager

        manager = BridgeManager.__new__(BridgeManager)
        manager._adapters = {ChannelType.TELEGRAM: adapter}  # type: ignore[attr-defined]
        return manager

    @pytest.mark.asyncio
    async def test_send_with_buttons(self) -> None:
        adapter = AsyncMock()
        adapter.send = AsyncMock(return_value="msg_123")
        manager = self._make_manager(adapter)

        btn = InlineButton(text="OK", callback_data="ok")
        outbound = OutboundMessage(
            channel_type=ChannelType.TELEGRAM,
            target="chat_1",
            text="Confirm?",
            buttons=[[btn]],
        )
        result = await manager._send_response(outbound)
        assert result == "msg_123"
        adapter.send.assert_called_once_with(
            "chat_1",
            "Confirm?",
            reply_to=None,
            buttons=[[btn]],
        )

    @pytest.mark.asyncio
    async def test_send_without_buttons(self) -> None:
        adapter = AsyncMock()
        adapter.send = AsyncMock(return_value="msg_456")
        manager = self._make_manager(adapter)

        outbound = OutboundMessage(
            channel_type=ChannelType.TELEGRAM,
            target="chat_1",
            text="Hello",
        )
        result = await manager._send_response(outbound)
        assert result == "msg_456"
        adapter.send.assert_called_once_with(
            "chat_1",
            "Hello",
            reply_to=None,
            buttons=None,
        )

    @pytest.mark.asyncio
    async def test_edit_message(self) -> None:
        adapter = AsyncMock()
        adapter.edit = AsyncMock()
        manager = self._make_manager(adapter)

        outbound = OutboundMessage(
            channel_type=ChannelType.TELEGRAM,
            target="chat_1",
            text="✅ Approved",
            edit_message_id="msg_789",
            buttons=None,
        )
        result = await manager._send_response(outbound)
        assert result == "msg_789"
        adapter.edit.assert_called_once_with(
            "msg_789",
            "✅ Approved",
            buttons=None,
            target="chat_1",
        )

    @pytest.mark.asyncio
    async def test_no_adapter_returns_none(self) -> None:
        from sovyx.bridge.manager import BridgeManager

        manager = BridgeManager.__new__(BridgeManager)
        manager._adapters = {}

        outbound = OutboundMessage(
            channel_type=ChannelType.SIGNAL,
            target="123",
            text="Test",
        )
        result = await manager._send_response(outbound)
        assert result is None

    @pytest.mark.asyncio
    async def test_send_exception_returns_none(self) -> None:
        adapter = AsyncMock()
        adapter.send = AsyncMock(side_effect=RuntimeError("network"))
        manager = self._make_manager(adapter)

        outbound = OutboundMessage(
            channel_type=ChannelType.TELEGRAM,
            target="chat_1",
            text="Test",
        )
        result = await manager._send_response(outbound)
        assert result is None
