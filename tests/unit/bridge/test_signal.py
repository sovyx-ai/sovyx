"""Tests for SignalChannel adapter (V05-35)."""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from sovyx.bridge.channels import signal as _signal_mod  # anti-pattern #11
from sovyx.bridge.channels.signal import (
    _DEFAULT_API_URL,
    SignalChannel,
)
from sovyx.engine.types import ChannelType

# ── Helpers ───────────────────────────────────────────────────────────


def _make_channel(
    phone: str = "+15551234567",
    api_url: str = _DEFAULT_API_URL,
) -> SignalChannel:
    """Create a SignalChannel with a mocked BridgeManager."""
    bridge = AsyncMock()
    return SignalChannel(phone, bridge, api_url=api_url)


def _make_envelope(
    text: str = "hello",
    source: str = "+15559876543",
    source_name: str = "Alice",
    timestamp: int = 1712345678000,
    group_id: str | None = None,
) -> dict[str, Any]:
    """Build a signal-cli envelope dict."""
    data_message: dict[str, Any] = {
        "message": text,
        "timestamp": timestamp,
    }
    if group_id is not None:
        data_message["groupInfo"] = {"groupId": group_id}
    return {
        "envelope": {
            "source": source,
            "sourceName": source_name,
            "dataMessage": data_message,
        },
    }


class _FakeResponse:
    """Minimal aiohttp response mock."""

    def __init__(
        self,
        status: int = 200,
        body: object = None,
    ) -> None:
        self.status = status
        self._body = body or {}

    async def json(self) -> object:
        return self._body

    async def text(self) -> str:
        return json.dumps(self._body)

    async def __aenter__(self) -> _FakeResponse:
        return self

    async def __aexit__(self, *_: object) -> None:
        pass


class _FakeSession:
    """Minimal aiohttp.ClientSession mock."""

    def __init__(self, responses: dict[str, _FakeResponse] | None = None) -> None:
        self._responses = responses or {}
        self._default = _FakeResponse()

    def get(self, url: str, **_: object) -> _FakeResponse:
        return self._responses.get(url, self._default)

    def post(self, url: str, **_: object) -> _FakeResponse:
        return self._responses.get(url, self._default)

    def put(self, url: str, **_: object) -> _FakeResponse:
        return self._responses.get(url, self._default)

    async def __aenter__(self) -> _FakeSession:
        return self

    async def __aexit__(self, *_: object) -> None:
        pass


# ── Init Tests ────────────────────────────────────────────────────────


class TestInit:
    """Tests for SignalChannel initialization."""

    def test_valid_init(self) -> None:
        ch = _make_channel()
        assert ch.phone_number == "+15551234567"
        assert ch.api_url == _DEFAULT_API_URL

    def test_custom_api_url(self) -> None:
        ch = _make_channel(api_url="http://signal:9090/")
        assert ch.api_url == "http://signal:9090"  # trailing slash stripped

    def test_empty_phone_raises(self) -> None:
        with pytest.raises(Exception, match="phone number"):  # noqa: BLE001, PT011
            _make_channel(phone="")

    def test_whitespace_phone_raises(self) -> None:
        with pytest.raises(Exception, match="phone number"):  # noqa: BLE001, PT011
            _make_channel(phone="   ")

    def test_phone_stripped(self) -> None:
        ch = _make_channel(phone="  +15551234567  ")
        assert ch.phone_number == "+15551234567"


# ── Property Tests ────────────────────────────────────────────────────


class TestProperties:
    """Tests for channel properties."""

    def test_channel_type(self) -> None:
        ch = _make_channel()
        assert ch.channel_type == ChannelType.SIGNAL

    def test_capabilities(self) -> None:
        ch = _make_channel()
        assert "send" in ch.capabilities

    def test_format_capabilities(self) -> None:
        ch = _make_channel()
        caps = ch.format_capabilities
        assert caps["markdown"] is False
        assert caps["max_length"] == 6000  # noqa: PLR2004

    def test_not_running_initially(self) -> None:
        ch = _make_channel()
        assert ch.is_running is False


# ── Initialize Tests ──────────────────────────────────────────────────


class TestInitialize:
    """Tests for API connectivity check."""

    @pytest.mark.asyncio()
    async def test_success(self) -> None:
        ch = _make_channel()
        session = _FakeSession(
            {
                f"{_DEFAULT_API_URL}/v1/about": _FakeResponse(
                    200, {"versions": {"signal-cli": "0.13.0"}}
                ),
            }
        )
        with patch.object(_signal_mod, "aiohttp") as mock_aiohttp:
            mock_aiohttp.ClientSession.return_value = session
            mock_aiohttp.ClientTimeout = MagicMock()
            await ch.initialize({})

    @pytest.mark.asyncio()
    async def test_non_200_raises(self) -> None:
        ch = _make_channel()
        session = _FakeSession(
            {
                f"{_DEFAULT_API_URL}/v1/about": _FakeResponse(500),
            }
        )
        with (
            patch.object(_signal_mod, "aiohttp") as mock_aiohttp,
            pytest.raises(Exception, match="returned 500"),  # noqa: BLE001, PT011
        ):
            mock_aiohttp.ClientSession.return_value = session
            mock_aiohttp.ClientTimeout = MagicMock()
            await ch.initialize({})

    @pytest.mark.asyncio()
    async def test_connection_error(self) -> None:
        ch = _make_channel()
        with (
            patch.object(_signal_mod, "aiohttp") as mock_aiohttp,
            pytest.raises(Exception, match="Cannot connect"),  # noqa: BLE001, PT011
        ):
            mock_aiohttp.ClientSession.side_effect = ConnectionError("refused")
            mock_aiohttp.ClientTimeout = MagicMock()
            await ch.initialize({})


# ── Start/Stop Tests ──────────────────────────────────────────────────


class TestStartStop:
    """Tests for lifecycle management."""

    @pytest.mark.asyncio()
    async def test_start_sets_running(self) -> None:
        ch = _make_channel()
        with patch.object(ch, "_poll_loop", new_callable=AsyncMock):
            await ch.start()
            assert ch.is_running is True
            await ch.stop()
            assert ch.is_running is False

    @pytest.mark.asyncio()
    async def test_double_start_no_op(self) -> None:
        ch = _make_channel()
        with patch.object(ch, "_poll_loop", new_callable=AsyncMock):
            await ch.start()
            task1 = ch._poll_task
            await ch.start()  # should not create new task
            assert ch._poll_task is task1
            await ch.stop()

    @pytest.mark.asyncio()
    async def test_stop_without_start(self) -> None:
        ch = _make_channel()
        await ch.stop()  # should not raise


# ── Send Tests ────────────────────────────────────────────────────────


class TestSend:
    """Tests for sending messages."""

    @pytest.mark.asyncio()
    async def test_send_success(self) -> None:
        ch = _make_channel()
        session = _FakeSession(
            {
                f"{_DEFAULT_API_URL}/v2/send": _FakeResponse(201, {"timestamp": 1712345678001}),
            }
        )
        with patch.object(_signal_mod, "aiohttp") as mock_aiohttp:
            mock_aiohttp.ClientSession.return_value = session
            mock_aiohttp.ClientTimeout = MagicMock()
            msg_id = await ch.send("+15559876543", "hello")
        assert msg_id == "1712345678001"

    @pytest.mark.asyncio()
    async def test_send_failure_raises(self) -> None:
        ch = _make_channel()
        session = _FakeSession(
            {
                f"{_DEFAULT_API_URL}/v2/send": _FakeResponse(400, {"error": "bad"}),
            }
        )
        with (
            patch.object(_signal_mod, "aiohttp") as mock_aiohttp,
            pytest.raises(Exception, match="Signal send failed"),  # noqa: BLE001, PT011
        ):
            mock_aiohttp.ClientSession.return_value = session
            mock_aiohttp.ClientTimeout = MagicMock()
            await ch.send("+15559876543", "hello")

    @pytest.mark.asyncio()
    async def test_send_connection_error(self) -> None:
        ch = _make_channel()
        with (
            patch.object(_signal_mod, "aiohttp") as mock_aiohttp,
            pytest.raises(Exception, match="Failed to send"),  # noqa: BLE001, PT011
        ):
            mock_aiohttp.ClientSession.side_effect = ConnectionError("refused")
            mock_aiohttp.ClientTimeout = MagicMock()
            await ch.send("+15559876543", "hello")


# ── Unsupported Operations ────────────────────────────────────────────


class TestUnsupported:
    """Tests for operations not supported by Signal in v0.5."""

    @pytest.mark.asyncio()
    async def test_edit_raises(self) -> None:
        ch = _make_channel()
        with pytest.raises(NotImplementedError, match="edit"):
            await ch.edit("123", "new text")

    @pytest.mark.asyncio()
    async def test_delete_raises(self) -> None:
        ch = _make_channel()
        with pytest.raises(NotImplementedError, match="delete"):
            await ch.delete("123")

    @pytest.mark.asyncio()
    async def test_react_raises(self) -> None:
        ch = _make_channel()
        with pytest.raises(NotImplementedError, match="react"):
            await ch.react("123", "👍")


# ── Typing Indicator ──────────────────────────────────────────────────


class TestTyping:
    """Tests for typing indicator."""

    @pytest.mark.asyncio()
    async def test_send_typing_no_error(self) -> None:
        ch = _make_channel()
        session = _FakeSession()
        with patch.object(_signal_mod, "aiohttp") as mock_aiohttp:
            mock_aiohttp.ClientSession.return_value = session
            mock_aiohttp.ClientTimeout = MagicMock()
            await ch.send_typing("+15559876543")

    @pytest.mark.asyncio()
    async def test_send_typing_error_suppressed(self) -> None:
        ch = _make_channel()
        with patch(
            "sovyx.bridge.channels.signal.aiohttp",
            side_effect=RuntimeError("fail"),
        ):
            await ch.send_typing("+15559876543")  # should not raise


# ── Envelope Handling ─────────────────────────────────────────────────


class TestEnvelopeHandling:
    """Tests for _handle_envelope."""

    @pytest.mark.asyncio()
    async def test_dm_message(self) -> None:
        bridge = AsyncMock()
        ch = SignalChannel("+15551234567", bridge)
        await ch._handle_envelope(_make_envelope())
        bridge.handle_inbound.assert_awaited_once()
        inbound = bridge.handle_inbound.call_args[0][0]
        assert inbound.channel_type == ChannelType.SIGNAL
        assert inbound.text == "hello"
        assert inbound.channel_user_id == "+15559876543"
        assert inbound.chat_id == "+15559876543"  # DM: chat_id == source

    @pytest.mark.asyncio()
    async def test_group_message(self) -> None:
        bridge = AsyncMock()
        ch = SignalChannel("+15551234567", bridge)
        await ch._handle_envelope(_make_envelope(group_id="grp123"))
        inbound = bridge.handle_inbound.call_args[0][0]
        assert inbound.chat_id == "grp123"
        assert inbound.metadata["group_id"] == "grp123"

    @pytest.mark.asyncio()
    async def test_no_data_message_ignored(self) -> None:
        bridge = AsyncMock()
        ch = SignalChannel("+15551234567", bridge)
        await ch._handle_envelope({"envelope": {"source": "+1"}})
        bridge.handle_inbound.assert_not_awaited()

    @pytest.mark.asyncio()
    async def test_empty_text_ignored(self) -> None:
        bridge = AsyncMock()
        ch = SignalChannel("+15551234567", bridge)
        env = _make_envelope()
        env["envelope"]["dataMessage"]["message"] = ""
        await ch._handle_envelope(env)
        bridge.handle_inbound.assert_not_awaited()

    @pytest.mark.asyncio()
    async def test_none_text_ignored(self) -> None:
        bridge = AsyncMock()
        ch = SignalChannel("+15551234567", bridge)
        env = _make_envelope()
        env["envelope"]["dataMessage"]["message"] = None
        await ch._handle_envelope(env)
        bridge.handle_inbound.assert_not_awaited()

    @pytest.mark.asyncio()
    async def test_flat_envelope(self) -> None:
        """Handles envelope_data without nested 'envelope' key."""
        bridge = AsyncMock()
        ch = SignalChannel("+15551234567", bridge)
        flat = {
            "source": "+15559876543",
            "sourceName": "Bob",
            "dataMessage": {
                "message": "flat",
                "timestamp": 999,
            },
        }
        await ch._handle_envelope(flat)
        inbound = bridge.handle_inbound.call_args[0][0]
        assert inbound.text == "flat"

    @pytest.mark.asyncio()
    async def test_receipt_ignored(self) -> None:
        """Non-data envelopes (receipts, typing) are ignored."""
        bridge = AsyncMock()
        ch = SignalChannel("+15551234567", bridge)
        await ch._handle_envelope(
            {
                "envelope": {
                    "source": "+1",
                    "receiptMessage": {"type": "DELIVERED"},
                },
            }
        )
        bridge.handle_inbound.assert_not_awaited()


# ── Receive Messages ──────────────────────────────────────────────────


class TestReceiveMessages:
    """Tests for _receive_messages polling."""

    @pytest.mark.asyncio()
    async def test_receive_dispatches(self) -> None:
        bridge = AsyncMock()
        ch = SignalChannel("+15551234567", bridge)
        messages = [_make_envelope("msg1"), _make_envelope("msg2")]
        session = _FakeSession(
            {
                f"{_DEFAULT_API_URL}/v1/receive/%2B15551234567": _FakeResponse(200, messages),
            }
        )
        with patch.object(_signal_mod, "aiohttp") as mock_aiohttp:
            mock_aiohttp.ClientSession.return_value = session
            mock_aiohttp.ClientTimeout = MagicMock()
            await ch._receive_messages()
        assert bridge.handle_inbound.await_count == 2  # noqa: PLR2004

    @pytest.mark.asyncio()
    async def test_receive_non_200_ignored(self) -> None:
        bridge = AsyncMock()
        ch = SignalChannel("+15551234567", bridge)
        session = _FakeSession(
            {
                f"{_DEFAULT_API_URL}/v1/receive/%2B15551234567": _FakeResponse(500),
            }
        )
        with patch.object(_signal_mod, "aiohttp") as mock_aiohttp:
            mock_aiohttp.ClientSession.return_value = session
            mock_aiohttp.ClientTimeout = MagicMock()
            await ch._receive_messages()
        bridge.handle_inbound.assert_not_awaited()


# ── Poll Loop ─────────────────────────────────────────────────────────


class TestPollLoop:
    """Tests for the polling loop."""

    @pytest.mark.asyncio()
    async def test_poll_loop_stops_on_cancel(self) -> None:
        bridge = AsyncMock()
        ch = SignalChannel("+15551234567", bridge)
        ch._running = True

        call_count = 0

        async def fake_receive() -> None:
            nonlocal call_count
            call_count += 1
            if call_count >= 2:  # noqa: PLR2004
                ch._running = False

        with (
            patch.object(ch, "_receive_messages", side_effect=fake_receive),
            patch("sovyx.bridge.channels.signal.asyncio.sleep", new_callable=AsyncMock),
        ):
            await ch._poll_loop()
        assert call_count >= 2  # noqa: PLR2004

    @pytest.mark.asyncio()
    async def test_poll_loop_backoff_on_error(self) -> None:
        bridge = AsyncMock()
        ch = SignalChannel("+15551234567", bridge)
        ch._running = True

        call_count = 0

        async def failing_receive() -> None:
            nonlocal call_count
            call_count += 1
            if call_count >= 3:  # noqa: PLR2004
                ch._running = False
                return
            msg = "connection failed"
            raise ConnectionError(msg)

        sleep_args: list[float] = []

        async def capture_sleep(t: float) -> None:
            sleep_args.append(t)

        with (
            patch.object(ch, "_receive_messages", side_effect=failing_receive),
            patch("sovyx.bridge.channels.signal.asyncio.sleep", side_effect=capture_sleep),
        ):
            await ch._poll_loop()
        # Should have backed off: 1.0, 2.0
        assert len(sleep_args) >= 2  # noqa: PLR2004
        assert sleep_args[0] == 1.0
        assert sleep_args[1] == 2.0
