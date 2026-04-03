"""Sovyx Bridge protocol — InboundMessage + OutboundMessage."""

from __future__ import annotations

import dataclasses
from datetime import UTC, datetime
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from sovyx.engine.types import ChannelType, PersonId


@dataclasses.dataclass
class InboundMessage:
    """Message received from a communication channel.

    Attributes:
        channel_type: Source channel (telegram, cli, etc.).
        channel_user_id: Platform user ID (who sent it).
        channel_message_id: Platform message ID.
        chat_id: Where to reply. DMs: same as channel_user_id.
            Groups: negative group ID (Telegram).
        text: Message content.
        person_id: Resolved PersonId (set by PersonResolver).
        display_name: Human-readable sender name.
        metadata: Channel-specific metadata.
        timestamp: When the message was received.
    """

    channel_type: ChannelType
    channel_user_id: str
    channel_message_id: str
    chat_id: str
    text: str
    person_id: PersonId | None = None
    display_name: str = ""
    metadata: dict[str, object] = dataclasses.field(default_factory=dict)
    timestamp: datetime = dataclasses.field(default_factory=lambda: datetime.now(UTC))


@dataclasses.dataclass
class OutboundMessage:
    """Message to send via a communication channel.

    Attributes:
        channel_type: Target channel.
        target: Chat ID (where to send).
        text: Message content.
        reply_to: Platform message ID to reply to.
    """

    channel_type: ChannelType
    target: str
    text: str
    reply_to: str | None = None
