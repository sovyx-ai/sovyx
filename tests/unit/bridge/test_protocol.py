"""Tests for sovyx.bridge.protocol — InboundMessage + OutboundMessage."""

from __future__ import annotations

from sovyx.bridge.protocol import InboundMessage, OutboundMessage
from sovyx.engine.types import ChannelType


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
