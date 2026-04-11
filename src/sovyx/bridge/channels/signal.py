"""Sovyx Signal channel adapter via signal-cli-rest-api.

Connects to a `signal-cli-rest-api <https://github.com/bbernhard/signal-cli-rest-api>`_
Docker container over HTTP.  Messages are received by polling the ``/v1/receive``
endpoint and sent via ``/v2/send``.

Limitations vs Telegram:
    - No markdown formatting (plain text only).
    - No inline buttons or keyboards.
    - Group support limited to basic send/receive.
    - Attachments not supported in v0.5.

Ref: SPE-014 §4.3 (Signal adapter), Pre-Compute V05-35.
"""

from __future__ import annotations

import asyncio
import contextlib
from typing import TYPE_CHECKING, Any
from urllib.parse import quote

import aiohttp

from sovyx.bridge.protocol import InboundMessage
from sovyx.engine.errors import ChannelConnectionError
from sovyx.engine.types import ChannelType
from sovyx.observability.logging import get_logger

if TYPE_CHECKING:
    from sovyx.bridge.manager import BridgeManager

logger = get_logger(__name__)

# ── Constants ───────────────────────────────────────────────────────

_DEFAULT_API_URL = "http://localhost:8080"
_POLL_INTERVAL = 1.0  # seconds between receive polls
_MAX_BACKOFF = 60  # max retry backoff in seconds
_SEND_TIMEOUT = 30  # HTTP timeout for send requests
_RECEIVE_TIMEOUT = 10  # HTTP timeout for receive polls


class SignalChannel:
    """Signal channel adapter using signal-cli-rest-api.

    The adapter communicates with a running ``signal-cli-rest-api`` instance
    (typically in Docker) via its REST endpoints.

    Args:
        phone_number: Registered Signal phone number (e.g. ``"+1234567890"``).
        bridge_manager: Bridge manager for inbound message routing.
        api_url: Base URL of the signal-cli-rest-api (default ``http://localhost:8080``).
    """

    def __init__(
        self,
        phone_number: str,
        bridge_manager: BridgeManager,
        *,
        api_url: str = _DEFAULT_API_URL,
    ) -> None:
        if not phone_number or not phone_number.strip():
            msg = "Signal phone number is required"
            raise ChannelConnectionError(msg)
        self._phone = phone_number.strip()
        self._bridge = bridge_manager
        self._api_url = api_url.rstrip("/")
        self._running = False
        self._poll_task: asyncio.Task[None] | None = None
        self._session: aiohttp.ClientSession | None = None

    @property
    def channel_type(self) -> ChannelType:
        """The type of channel."""
        return ChannelType.SIGNAL

    def _get_session(self) -> aiohttp.ClientSession:
        """Return the persistent session, creating one if needed."""
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
        return self._session

    @property
    def capabilities(self) -> set[str]:
        """Supported capabilities (plain text send only in v0.5)."""
        return {"send"}

    @property
    def format_capabilities(self) -> dict[str, object]:
        """Signal supports plain text only."""
        return {
            "markdown": False,
            "max_length": 6000,
            "parse_mode": None,
        }

    @property
    def is_running(self) -> bool:
        """Whether the channel is actively polling."""
        return self._running

    @property
    def phone_number(self) -> str:
        """Registered phone number."""
        return self._phone

    @property
    def api_url(self) -> str:
        """Base URL of signal-cli-rest-api."""
        return self._api_url

    async def initialize(self, config: dict[str, object]) -> None:
        """Verify connectivity to signal-cli-rest-api.

        Raises:
            ChannelConnectionError: If the API is unreachable or the number
                is not registered.
        """
        try:
            session = self._get_session()
            url = f"{self._api_url}/v1/about"
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=5)) as resp:
                if resp.status != 200:  # noqa: PLR2004
                    msg = f"signal-cli-rest-api returned {resp.status}"
                    raise ChannelConnectionError(msg)
                data = await resp.json()
                logger.info(
                    "signal_api_connected",
                    version=data.get("versions", {}).get("signal-cli", "unknown"),
                )
        except ChannelConnectionError:
            raise
        except Exception as exc:
            msg = f"Cannot connect to signal-cli-rest-api at {self._api_url}: {exc}"
            raise ChannelConnectionError(msg) from exc

    async def start(self) -> None:
        """Start polling for incoming messages."""
        if self._running:
            return
        self._session = aiohttp.ClientSession()
        self._running = True
        self._poll_task = asyncio.create_task(self._poll_loop())
        logger.info("signal_channel_started", phone=self._phone)

    async def stop(self) -> None:
        """Stop polling gracefully."""
        self._running = False
        if self._poll_task:
            self._poll_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._poll_task
        if self._session and not self._session.closed:
            await self._session.close()
            self._session = None
        logger.info("signal_channel_stopped")

    async def send(
        self,
        target: str,
        message: str,
        reply_to: str | None = None,
        buttons: list[list[object]] | None = None,
    ) -> str:
        """Send a text message to a Signal recipient.

        Args:
            target: Recipient phone number or group ID.
            message: Plain text message body.
            reply_to: Not supported by Signal API in v0.5 (ignored).
            buttons: Inline buttons — Signal doesn't support native buttons,
                so they are converted to numbered text options appended to
                the message body.

        Returns:
            A synthetic message ID (Signal REST API does not return one).

        Raises:
            ChannelConnectionError: If the send request fails.
        """
        # Convert buttons to numbered text (Signal has no inline buttons)
        if buttons:
            message = self._append_button_text(message, buttons)

        payload: dict[str, Any] = {
            "message": message,
            "number": self._phone,
            "recipients": [target],
        }

        try:
            session = self._get_session()
            url = f"{self._api_url}/v2/send"
            async with session.post(
                url,
                json=payload,
                timeout=aiohttp.ClientTimeout(total=_SEND_TIMEOUT),
            ) as resp:
                if resp.status not in (200, 201):
                    body = await resp.text()
                    msg = f"Signal send failed ({resp.status}): {body}"
                    raise ChannelConnectionError(msg)
                # Signal REST API returns timestamps, not message IDs
                result = await resp.json()
                timestamp = str(result.get("timestamp", "0"))
                logger.debug(
                    "signal_message_sent",
                    target=target,
                    timestamp=timestamp,
                )
                return timestamp
        except ChannelConnectionError:
            raise
        except Exception as exc:
            logger.error("signal_send_failed", target=target, error=str(exc))
            msg = f"Failed to send Signal message: {exc}"
            raise ChannelConnectionError(msg) from exc

    async def send_typing(self, target: str) -> None:
        """Send typing indicator (best-effort, may not be supported)."""
        with contextlib.suppress(Exception):
            payload = {
                "recipient": target,
            }
            session = self._get_session()
            url = f"{self._api_url}/v1/typing-indicator/{quote(self._phone)}"
            async with session.put(
                url,
                json=payload,
                timeout=aiohttp.ClientTimeout(total=5),
            ) as _:
                pass

    @staticmethod
    def _append_button_text(message: str, buttons: list[list[object]]) -> str:
        """Convert inline buttons to numbered text options.

        Example output:
            [original message]

            1️⃣ Approve
            2️⃣ Deny
        """
        number_emojis = ["1️⃣", "2️⃣", "3️⃣", "4️⃣", "5️⃣", "6️⃣", "7️⃣", "8️⃣", "9️⃣"]
        lines: list[str] = []
        idx = 0
        for row in buttons:
            for btn in row:
                emoji = number_emojis[idx] if idx < len(number_emojis) else f"{idx + 1}."
                text = getattr(btn, "text", str(btn))
                lines.append(f"{emoji} {text}")
                idx += 1
        if lines:
            return f"{message}\n\n" + "\n".join(lines)
        return message

    async def edit(
        self,
        message_id: str,
        new_text: str,
        buttons: list[list[object]] | None = None,
        target: str | None = None,
    ) -> None:
        """Not supported by Signal."""
        msg = "edit not supported for Signal"
        raise NotImplementedError(msg)

    async def delete(self, message_id: str) -> None:
        """Not supported by Signal."""
        msg = "delete not supported for Signal"
        raise NotImplementedError(msg)

    async def react(self, message_id: str, emoji: str) -> None:
        """Not supported in v0.5."""
        msg = "react not supported for Signal in v0.5"
        raise NotImplementedError(msg)

    # ── Polling ─────────────────────────────────────────────────

    async def _poll_loop(self) -> None:
        """Poll for incoming messages with exponential backoff on errors."""
        backoff = 1.0
        while self._running:
            try:
                await self._receive_messages()
                backoff = 1.0
                await asyncio.sleep(_POLL_INTERVAL)
            except asyncio.CancelledError:
                break
            except Exception:
                logger.warning(
                    "signal_poll_error",
                    backoff=backoff,
                    exc_info=True,
                )
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, _MAX_BACKOFF)

    async def _receive_messages(self) -> None:
        """Poll /v1/receive and dispatch inbound messages."""

        phone_encoded = quote(self._phone)
        url = f"{self._api_url}/v1/receive/{phone_encoded}"

        session = self._get_session()
        async with session.get(
            url,
            timeout=aiohttp.ClientTimeout(total=_RECEIVE_TIMEOUT),
        ) as resp:
            if resp.status != 200:  # noqa: PLR2004
                return

            messages: list[dict[str, Any]] = await resp.json()

        for msg_data in messages:
            await self._handle_envelope(msg_data)

    async def _handle_envelope(self, envelope_data: dict[str, Any]) -> None:
        """Parse a signal-cli envelope and dispatch as InboundMessage."""
        envelope = envelope_data.get("envelope", envelope_data)

        # Only handle data messages (not receipts, typing, etc.)
        data_message = envelope.get("dataMessage")
        if data_message is None:
            return

        text = data_message.get("message")
        if not text:
            return

        source = envelope.get("source", "")
        source_name = envelope.get("sourceName", source)
        timestamp = str(data_message.get("timestamp", "0"))

        # Determine chat_id (group or DM)
        group_info = data_message.get("groupInfo")
        chat_id = group_info.get("groupId", source) if group_info else source

        inbound = InboundMessage(
            channel_type=ChannelType.SIGNAL,
            channel_user_id=source,
            channel_message_id=timestamp,
            chat_id=chat_id,
            text=text,
            display_name=source_name,
            metadata={
                "timestamp": timestamp,
                **({"group_id": group_info["groupId"]} if group_info else {}),
            },
        )
        await self._bridge.handle_inbound(inbound)
