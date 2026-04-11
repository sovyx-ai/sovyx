"""Tests for sovyx.bridge.protocol — InboundMessage + OutboundMessage + InlineButton."""

from __future__ import annotations

import pytest

from sovyx.bridge.protocol import InboundMessage, InlineButton, OutboundMessage
from sovyx.engine.types import ChannelType


class TestInlineButton:
    """InlineButton dataclass — frozen, slotted, validated."""

    def test_create_valid(self) -> None:
        btn = InlineButton(text="✅ Approve", callback_data="fin_confirm:tc1")
        assert btn.text == "✅ Approve"
        assert btn.callback_data == "fin_confirm:tc1"

    def test_frozen(self) -> None:
        btn = InlineButton(text="OK", callback_data="ok")
        with pytest.raises(AttributeError):
            btn.text = "changed"  # type: ignore[misc]

    def test_empty_text_raises(self) -> None:
        with pytest.raises(ValueError, match="text must be non-empty"):
            InlineButton(text="", callback_data="ok")

    def test_empty_callback_data_raises(self) -> None:
        with pytest.raises(ValueError, match="callback_data must be non-empty"):
            InlineButton(text="OK", callback_data="")

    def test_callback_data_64_byte_limit(self) -> None:
        data_64 = "a" * 64
        btn = InlineButton(text="OK", callback_data=data_64)
        assert btn.callback_data == data_64

    def test_callback_data_exceeds_64_bytes(self) -> None:
        data_65 = "a" * 65
        with pytest.raises(ValueError, match="exceeds 64 bytes"):
            InlineButton(text="OK", callback_data=data_65)

    def test_callback_data_multibyte_check(self) -> None:
        """Multi-byte chars should count by encoded bytes, not chars."""
        # "é" is 2 bytes UTF-8 → 33 × 2 = 66 bytes > 64
        with pytest.raises(ValueError, match="exceeds 64 bytes"):
            InlineButton(text="OK", callback_data="é" * 33)

    def test_equality(self) -> None:
        a = InlineButton(text="OK", callback_data="ok")
        b = InlineButton(text="OK", callback_data="ok")
        assert a == b

    def test_inequality(self) -> None:
        a = InlineButton(text="OK", callback_data="ok")
        b = InlineButton(text="Cancel", callback_data="cancel")
        assert a != b

    def test_hashable(self) -> None:
        """Frozen dataclass should be hashable (usable in sets/dicts)."""
        btn = InlineButton(text="OK", callback_data="ok")
        assert hash(btn) == hash(InlineButton(text="OK", callback_data="ok"))
        s = {btn}
        assert len(s) == 1

    def test_financial_gate_convention(self) -> None:
        """Convention: fin_confirm:{tool_call_id} / fin_cancel:{tool_call_id}."""
        confirm = InlineButton(text="✅ Approve", callback_data="fin_confirm:tc_abc123")
        cancel = InlineButton(text="❌ Deny", callback_data="fin_cancel:tc_abc123")
        assert confirm.callback_data.startswith("fin_confirm:")
        assert cancel.callback_data.startswith("fin_cancel:")

    def test_slots(self) -> None:
        """Slots enabled — no __dict__."""
        btn = InlineButton(text="OK", callback_data="ok")
        assert not hasattr(btn, "__dict__")


class TestInboundMessage:
    """InboundMessage dataclass."""

    def test_fields(self) -> None:
        msg = InboundMessage(
            channel_type=ChannelType.TELEGRAM,
            channel_user_id="123",
            channel_message_id="msg1",
            chat_id="123",
            text="Hello",
        )
        assert msg.channel_type == ChannelType.TELEGRAM
        assert msg.text == "Hello"
        assert msg.person_id is None
        assert msg.display_name == ""

    def test_default_timestamp(self) -> None:
        msg = InboundMessage(
            channel_type=ChannelType.CLI,
            channel_user_id="u",
            channel_message_id="m",
            chat_id="u",
            text="Hi",
        )
        assert msg.timestamp.tzinfo is not None

    def test_with_metadata(self) -> None:
        msg = InboundMessage(
            channel_type=ChannelType.TELEGRAM,
            channel_user_id="123",
            channel_message_id="msg1",
            chat_id="-999",
            text="Group msg",
            metadata={"group": True},
        )
        assert msg.chat_id == "-999"
        assert msg.metadata["group"] is True

    def test_is_callback_false_by_default(self) -> None:
        msg = InboundMessage(
            channel_type=ChannelType.TELEGRAM,
            channel_user_id="123",
            channel_message_id="msg1",
            chat_id="123",
            text="Hello",
        )
        assert msg.is_callback is False
        assert msg.callback_data is None

    def test_is_callback_true_with_callback_data(self) -> None:
        msg = InboundMessage(
            channel_type=ChannelType.TELEGRAM,
            channel_user_id="123",
            channel_message_id="cb1",
            chat_id="123",
            text="",
            metadata={
                "callback_data": "fin_confirm:tc1",
                "callback_message_id": "msg99",
            },
        )
        assert msg.is_callback is True
        assert msg.callback_data == "fin_confirm:tc1"
        assert msg.callback_message_id == "msg99"

    def test_callback_data_coerced_to_str(self) -> None:
        """Even non-string callback_data is coerced to str."""
        msg = InboundMessage(
            channel_type=ChannelType.TELEGRAM,
            channel_user_id="u",
            channel_message_id="m",
            chat_id="c",
            text="",
            metadata={"callback_data": 42},
        )
        assert msg.is_callback is True
        assert msg.callback_data == "42"

    def test_callback_message_id_none_when_absent(self) -> None:
        msg = InboundMessage(
            channel_type=ChannelType.TELEGRAM,
            channel_user_id="u",
            channel_message_id="m",
            chat_id="c",
            text="hi",
        )
        assert msg.callback_message_id is None


class TestOutboundMessage:
    """OutboundMessage dataclass."""

    def test_fields(self) -> None:
        msg = OutboundMessage(
            channel_type=ChannelType.TELEGRAM,
            target="123",
            text="Hi!",
            reply_to="msg1",
        )
        assert msg.target == "123"
        assert msg.reply_to == "msg1"

    def test_no_reply(self) -> None:
        msg = OutboundMessage(
            channel_type=ChannelType.CLI,
            target="local",
            text="OK",
        )
        assert msg.reply_to is None

    def test_buttons_default_none(self) -> None:
        msg = OutboundMessage(
            channel_type=ChannelType.TELEGRAM,
            target="123",
            text="Confirm?",
        )
        assert msg.buttons is None

    def test_with_buttons(self) -> None:
        confirm = InlineButton(text="✅ Approve", callback_data="fin_confirm:tc1")
        cancel = InlineButton(text="❌ Deny", callback_data="fin_cancel:tc1")
        msg = OutboundMessage(
            channel_type=ChannelType.TELEGRAM,
            target="123",
            text="Transfer $500. Approve?",
            buttons=[[confirm, cancel]],
        )
        assert msg.buttons is not None
        assert len(msg.buttons) == 1
        assert len(msg.buttons[0]) == 2
        assert msg.buttons[0][0].text == "✅ Approve"
        assert msg.buttons[0][1].callback_data == "fin_cancel:tc1"

    def test_multi_row_buttons(self) -> None:
        """Button grid can have multiple rows."""
        row1 = [InlineButton(text="A", callback_data="a")]
        row2 = [
            InlineButton(text="B", callback_data="b"),
            InlineButton(text="C", callback_data="c"),
        ]
        msg = OutboundMessage(
            channel_type=ChannelType.TELEGRAM,
            target="123",
            text="Choose",
            buttons=[row1, row2],
        )
        assert msg.buttons is not None
        assert len(msg.buttons) == 2
        assert len(msg.buttons[1]) == 2

    def test_empty_buttons_list(self) -> None:
        """Empty list is valid (different from None — no buttons vs 0 rows)."""
        msg = OutboundMessage(
            channel_type=ChannelType.TELEGRAM,
            target="123",
            text="No buttons here",
            buttons=[],
        )
        assert msg.buttons == []

    def test_edit_message_id_default_none(self) -> None:
        msg = OutboundMessage(
            channel_type=ChannelType.TELEGRAM,
            target="123",
            text="updated",
        )
        assert msg.edit_message_id is None

    def test_edit_message_id_set(self) -> None:
        msg = OutboundMessage(
            channel_type=ChannelType.TELEGRAM,
            target="123",
            text="✅ Approved",
            edit_message_id="msg99",
        )
        assert msg.edit_message_id == "msg99"

    def test_backward_compat_no_buttons(self) -> None:
        """Existing code creating OutboundMessage without buttons still works."""
        msg = OutboundMessage(
            channel_type=ChannelType.CLI,
            target="local",
            text="Plain message",
            reply_to="msg1",
        )
        assert msg.buttons is None
        assert msg.edit_message_id is None
        assert msg.text == "Plain message"
