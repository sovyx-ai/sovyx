"""Sovyx Telegram channel adapter via aiogram 3.x.

Receives messages → InboundMessage → BridgeManager.
Sends OutboundMessage → Telegram Bot API.
"""

from __future__ import annotations

import asyncio
import contextlib
from typing import TYPE_CHECKING

from aiogram import Bot, Dispatcher, Router
from aiogram.exceptions import AiogramError
from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    MessageEntity,
)

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
        # Formatted output via telegramify-markdown (MessageEntity, not parse_mode).
        # See _format_message() for markdown → entity conversion.
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
        return {"send", "inline_buttons", "edit"}

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
        buttons: list[list[object]] | None = None,
    ) -> str:
        """Send message via Bot API with markdown formatting.

        Uses telegramify-markdown to convert standard markdown to
        Telegram MessageEntity objects.  Falls back to plain text
        if conversion fails.

        Args:
            target: Telegram chat ID.
            message: Message text (markdown supported).
            reply_to: Message ID to reply to.
            buttons: Optional inline buttons (list of rows of InlineButton).
        """
        text, entities = self._format_message(message)
        kwargs: dict[str, object] = {"text": text}
        if entities:
            kwargs["entities"] = entities
        if reply_to:
            kwargs["reply_to_message_id"] = int(reply_to)
        if buttons:
            kwargs["reply_markup"] = self._build_inline_keyboard(buttons)
        try:
            result = await self._bot.send_message(
                chat_id=int(target),
                **kwargs,  # type: ignore[arg-type]
            )
            return str(result.message_id)
        except Exception as e:  # noqa: BLE001
            logger.error(
                "telegram_send_failed",
                target=target,
                error=str(e),
            )
            raise

    @staticmethod
    def _format_message(text: str) -> tuple[str, list[MessageEntity]]:
        """Convert markdown to Telegram entities. Falls back to plain text."""
        try:
            from telegramify_markdown import convert

            raw_text, raw_entities = convert(text)
            # Convert telegramify entities to aiogram MessageEntity
            entities: list[MessageEntity] = []
            for ent in raw_entities:
                kwargs: dict[str, object] = {
                    "type": ent.type,
                    "offset": ent.offset,
                    "length": ent.length,
                }
                if ent.url:
                    kwargs["url"] = ent.url
                if ent.language:
                    kwargs["language"] = ent.language
                entities.append(MessageEntity(**kwargs))  # type: ignore[arg-type]
            return raw_text, entities
        except Exception:  # noqa: BLE001
            logger.debug("telegram_markdown_fallback", exc_info=True)
            return text, []

    async def edit(
        self,
        message_id: str,
        new_text: str,
        buttons: list[list[object]] | None = None,
        target: str | None = None,
    ) -> None:
        """Edit a previously sent message.

        Args:
            message_id: Telegram message ID to edit.
            new_text: New text content.
            buttons: New inline keyboard (None = remove keyboard).
            target: Telegram chat ID (required for editMessageText).
        """
        if not target:
            logger.warning("telegram_edit_no_target", message_id=message_id)
            return
        text, entities = self._format_message(new_text)
        kwargs: dict[str, object] = {
            "chat_id": int(target),
            "message_id": int(message_id),
            "text": text,
        }
        if entities:
            kwargs["entities"] = entities
        if buttons is not None:
            kwargs["reply_markup"] = self._build_inline_keyboard(buttons)
        try:
            await self._bot.edit_message_text(**kwargs)  # type: ignore[arg-type]
        except (TimeoutError, AiogramError, OSError) as e:
            # AiogramError: base for every aiogram-typed failure
            # (rate-limit, bad request, forbidden, network). Timeout
            # and OSError cover raw transport issues that don't get
            # wrapped. Edit failures are non-fatal — the original
            # message stays visible to the user — but we log with
            # traceback so rate-limit storms are diagnosable.
            logger.error(
                "telegram_edit_failed",
                message_id=message_id,
                error=str(e),
                exc_info=True,
            )

    @staticmethod
    def _build_inline_keyboard(
        buttons: list[list[object]],
    ) -> InlineKeyboardMarkup:
        """Convert generic InlineButton rows to Telegram InlineKeyboardMarkup.

        Args:
            buttons: Rows of InlineButton (or any object with text + callback_data).

        Returns:
            aiogram InlineKeyboardMarkup.
        """
        rows: list[list[InlineKeyboardButton]] = []
        for row in buttons:
            tg_row: list[InlineKeyboardButton] = []
            for btn in row:
                tg_row.append(
                    InlineKeyboardButton(
                        text=getattr(btn, "text", str(btn)),
                        callback_data=getattr(btn, "callback_data", ""),
                    )
                )
            rows.append(tg_row)
        return InlineKeyboardMarkup(inline_keyboard=rows)

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
            except Exception:  # noqa: BLE001 — poll loop must survive single failures
                logger.warning(
                    "telegram_poll_error",
                    backoff=backoff,
                    exc_info=True,
                )
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, _MAX_BACKOFF)
            else:
                backoff = 1
