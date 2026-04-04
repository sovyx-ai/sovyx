"""Sovyx Telegram channel adapter via aiogram 3.x.

Receives messages → InboundMessage → BridgeManager.
Sends OutboundMessage → Telegram Bot API.
"""

from __future__ import annotations

import asyncio
import contextlib
from typing import TYPE_CHECKING

from aiogram import Bot, Dispatcher, Router

from sovyx.bridge.protocol import InboundMessage
from sovyx.engine.errors import ChannelConnectionError
from sovyx.engine.types import ChannelType
from sovyx.observability.logging import get_logger

if TYPE_CHECKING:
    from aiogram.types import Message

    from sovyx.bridge.manager import BridgeManager

logger = get_logger(__name__)

# Max retry backoff in seconds
_MAX_BACKOFF = 60


class TelegramChannel:
    """Telegram channel adapter.

    v0.1: text + reply only. No media, inline keyboards, or complex groups.
    """

    def __init__(self, token: str, bridge_manager: BridgeManager) -> None:
        if not token or not token.strip():
            msg = "Telegram bot token is required"
            raise ChannelConnectionError(msg)
        self._token = token.strip()
        self._bridge = bridge_manager
        # Plain text: no parse_mode. LLM output contains arbitrary
        # markdown characters that MarkdownV2 rejects.  Plain text is
        # functional; formatted output deferred to v0.2 via
        # telegramify-markdown.  See sovyx-imm-d1-telegram §3.
        self._bot = Bot(token=self._token)
        self._router = Router()
        self._dp = Dispatcher()
        self._dp.include_router(self._router)
        self._running = False
        self._poll_task: asyncio.Task[None] | None = None

        # Register message handler
        self._router.message.register(self._on_message)

    @property
    def channel_type(self) -> ChannelType:
        """The type of channel."""
        return ChannelType.TELEGRAM

    @property
    def capabilities(self) -> set[str]:
        """Supported capabilities."""
        return {"send"}

    @property
    def format_capabilities(self) -> dict[str, object]:
        """Format support."""
        return {
            "markdown": True,
            "max_length": 4096,
            "parse_mode": "MarkdownV2",
        }

    @property
    def is_running(self) -> bool:
        """Whether the channel is actively polling."""
        return self._running

    async def initialize(self, config: dict[str, object]) -> None:
        """No-op (config via __init__). Protocol compliance."""

    async def start(self) -> None:
        """Start Telegram polling in background."""
        if self._running:
            return
        self._running = True
        self._poll_task = asyncio.create_task(self._poll_loop())
        logger.info("telegram_channel_started")

    async def stop(self) -> None:
        """Stop polling gracefully."""
        self._running = False
        if self._poll_task:
            self._poll_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._poll_task
        await self._bot.session.close()
        logger.info("telegram_channel_stopped")

    async def send(
        self,
        target: str,
        message: str,
        reply_to: str | None = None,
    ) -> str:
        """Send message via Bot API. Returns platform message ID."""
        kwargs: dict[str, object] = {"text": message}
        if reply_to:
            kwargs["reply_to_message_id"] = int(reply_to)
        try:
            result = await self._bot.send_message(
                chat_id=int(target),
                **kwargs,  # type: ignore[arg-type]
            )
            return str(result.message_id)
        except Exception as e:
            logger.error(
                "telegram_send_failed",
                target=target,
                error=str(e),
            )
            raise

    async def edit(self, message_id: str, new_text: str) -> None:
        """Stub — not supported in v0.1."""
        msg = "edit not supported in v0.1"
        raise NotImplementedError(msg)

    async def delete(self, message_id: str) -> None:
        """Stub — not supported in v0.1."""
        msg = "delete not supported in v0.1"
        raise NotImplementedError(msg)

    async def react(self, message_id: str, emoji: str) -> None:
        """Stub — not supported in v0.1."""
        msg = "react not supported in v0.1"
        raise NotImplementedError(msg)

    async def send_typing(self, target: str) -> None:
        """Send 'typing...' indicator to the chat."""
        with contextlib.suppress(Exception):
            await self._bot.send_chat_action(chat_id=int(target), action="typing")

    async def _on_message(self, message: Message) -> None:
        """Handle incoming Telegram message."""
        if not message.text or not message.from_user:
            return

        inbound = InboundMessage(
            channel_type=ChannelType.TELEGRAM,
            channel_user_id=str(message.from_user.id),
            channel_message_id=str(message.message_id),
            chat_id=str(message.chat.id),
            text=message.text,
            display_name=(message.from_user.full_name or message.from_user.username or ""),
        )
        await self._bridge.handle_inbound(inbound)

    async def _poll_loop(self) -> None:
        """Polling loop with exponential backoff on errors."""
        backoff = 1
        while self._running:
            try:
                await self._dp.start_polling(self._bot, handle_signals=False)
            except asyncio.CancelledError:
                break
            except Exception:
                logger.warning(
                    "telegram_poll_error",
                    backoff=backoff,
                    exc_info=True,
                )
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, _MAX_BACKOFF)
            else:
                backoff = 1
