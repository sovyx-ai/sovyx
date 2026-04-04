"""Tests for sovyx.engine.rpc_protocol — length-prefixed wire format."""

from __future__ import annotations

import asyncio
import json

import pytest

from sovyx.engine.rpc_protocol import _HEADER_SIZE, _MAX_PAYLOAD, rpc_recv, rpc_send


class TestRPCProtocol:
    """Length-prefixed protocol roundtrip and safety tests."""

    @pytest.mark.asyncio
    async def test_roundtrip(self) -> None:
        """Send → recv returns identical payload."""
        payload = {"jsonrpc": "2.0", "result": "hello", "id": 1}

        # Encode manually to feed into StreamReader
        raw = json.dumps(payload).encode()
        frame = len(raw).to_bytes(_HEADER_SIZE, "big") + raw

        reader = asyncio.StreamReader()
        reader.feed_data(frame)
        reader.feed_eof()

        result = await rpc_recv(reader)
        assert result == payload

    @pytest.mark.asyncio
    async def test_large_payload_roundtrip(self) -> None:
        """100KB payload roundtrips correctly via framing."""
        payload = {"data": "x" * 100_000}

        raw = json.dumps(payload).encode()
        frame = len(raw).to_bytes(_HEADER_SIZE, "big") + raw

        reader = asyncio.StreamReader()
        reader.feed_data(frame)
        reader.feed_eof()

        result = await rpc_recv(reader)
        assert result == payload
        assert len(result["data"]) == 100_000

    @pytest.mark.asyncio
    async def test_too_large_recv_rejects(self) -> None:
        """Declared length exceeding limit raises ValueError on recv."""
        reader = asyncio.StreamReader()
        fake_length = _MAX_PAYLOAD + 1
        reader.feed_data(fake_length.to_bytes(_HEADER_SIZE, "big"))
        reader.feed_eof()

        with pytest.raises(ValueError, match="too large"):
            await rpc_recv(reader)

    @pytest.mark.asyncio
    async def test_too_large_send_rejects(self) -> None:
        """Payload exceeding limit raises ValueError on send."""
        payload = {"data": "x" * (_MAX_PAYLOAD + 1)}

        # Create a dummy writer (we expect it to fail before writing)
        reader = asyncio.StreamReader()
        transport = _NoopTransport()
        protocol = asyncio.StreamReaderProtocol(asyncio.StreamReader())
        writer = asyncio.StreamWriter(
            transport, protocol, reader, asyncio.get_event_loop()
        )

        with pytest.raises(ValueError, match="too large"):
            await rpc_send(writer, payload)

    @pytest.mark.asyncio
    async def test_incomplete_header_raises(self) -> None:
        """Connection closed mid-header raises IncompleteReadError."""
        reader = asyncio.StreamReader()
        reader.feed_data(b"\x00\x00")  # Only 2 of 4 header bytes
        reader.feed_eof()

        with pytest.raises(asyncio.IncompleteReadError):
            await rpc_recv(reader)


class _NoopTransport(asyncio.Transport):
    """Minimal transport that discards writes (for send-rejection tests)."""

    def write(self, data: bytes) -> None:
        pass

    def is_closing(self) -> bool:
        return False

    def close(self) -> None:
        pass
