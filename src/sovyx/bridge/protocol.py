"""Sovyx Bridge protocol — InboundMessage + OutboundMessage + InlineButton.

Defines the core data contracts for communication between the cognitive
pipeline and channel adapters (Telegram, Signal, Dashboard, etc.).
"""

from __future__ import annotations

import dataclasses
from datetime import UTC, datetime
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from sovyx.engine.types import ChannelType, PersonId


@dataclasses.dataclass(frozen=True, slots=True)
class InlineButton:
    """A single inline button for interactive messages.

    Attributes:
        text: Button label displayed to the user.
        callback_data: Opaque string returned when the user presses the button.
            Max 64 bytes (Telegram limit). Convention for financial gate:
            ``"fin_confirm:{tool_call_id}"`` / ``"fin_cancel:{tool_call_id}"``.
    """

    text: str
    callback_data: str

    def __post_init__(self) -> None:
        if not self.text:
            msg = "InlineButton.text must be non-empty"
            raise ValueError(msg)
        if not self.callback_data:
            msg = "InlineButton.callback_data must be non-empty"
            raise ValueError(msg)
        if len(self.callback_data.encode()) > 64:
            msg = f"callback_data exceeds 64 bytes: {len(self.callback_data.encode())}B"
            raise ValueError(msg)


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
            Callback queries populate ``callback_data`` (str) and
            ``callback_message_id`` (str, the message the button was on).
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

    @property
    def is_callback(self) -> bool:
        """True if this message originated from an inline button press."""
        return "callback_data" in self.metadata

    @property
    def callback_data(self) -> str | None:
        """Shortcut for callback_data from metadata (set by callback queries)."""
        val = self.metadata.get("callback_data")
        return str(val) if val is not None else None

    @property
    def callback_message_id(self) -> str | None:
        """Message ID the callback button was attached to."""
        val = self.metadata.get("callback_message_id")
        return str(val) if val is not None else None


@dataclasses.dataclass
class OutboundMessage:
    """Message to send via a communication channel.

    Attributes:
        channel_type: Target channel.
        target: Chat ID (where to send).
        text: Message content.
        reply_to: Platform message ID to reply to.
        buttons: Optional inline buttons as rows of buttons.
            Each inner list is a row. Channels that don't support inline
            buttons convert them to numbered text options automatically.
        edit_message_id: If set, edit this message instead of sending new.
            Used to update button messages after user interaction.
    """

    channel_type: ChannelType
    target: str
    text: str
    reply_to: str | None = None
    buttons: list[list[object]] | None = None
    edit_message_id: str | None = None
