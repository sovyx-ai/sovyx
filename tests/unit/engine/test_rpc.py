"""Tests for sovyx.engine.rpc_server + sovyx.cli.rpc_client."""

from __future__ import annotations

import asyncio
import json
from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest

from sovyx.cli.rpc_client import DaemonClient
from sovyx.engine.errors import ChannelConnectionError
from sovyx.engine.rpc_server import DaemonRPCServer

if TYPE_CHECKING:
    from pathlib import Path


class TestRPCEndToEnd:
    """Server + Client integration."""

    async def test_simple_method(self, tmp_path: Path) -> None:
        socket_path = tmp_path / "test.sock"
        server = DaemonRPCServer(socket_path)
        server.register_method("ping", lambda: "pong")

        await server.start()
        try:
            client = DaemonClient(socket_path)
            result = await client.call("ping")
            assert result == "pong"
        finally:
            await server.stop()

    async def test_method_with_params(self, tmp_path: Path) -> None:
        socket_path = tmp_path / "test.sock"
        server = DaemonRPCServer(socket_path)
        server.register_method("add", lambda a, b: a + b)

        await server.start()
        try:
            client = DaemonClient(socket_path)
            result = await client.call("add", {"a": 3, "b": 4})
            assert result == 7  # noqa: PLR2004
        finally:
            await server.stop()

    async def test_async_method(self, tmp_path: Path) -> None:
        socket_path = tmp_path / "test.sock"
        server = DaemonRPCServer(socket_path)

        async def async_greet(name: str = "World") -> str:
            return f"Hello, {name}!"

        server.register_method("greet", async_greet)

        await server.start()
        try:
            client = DaemonClient(socket_path)
            result = await client.call("greet", {"name": "Guipe"})
            assert result == "Hello, Guipe!"
        finally:
            await server.stop()

    async def test_method_not_found(self, tmp_path: Path) -> None:
        socket_path = tmp_path / "test.sock"
        server = DaemonRPCServer(socket_path)

        await server.start()
        try:
            client = DaemonClient(socket_path)
            with pytest.raises(ChannelConnectionError, match="Method not found"):
                await client.call("nonexistent")
        finally:
            await server.stop()

    async def test_method_error(self, tmp_path: Path) -> None:
        socket_path = tmp_path / "test.sock"
        server = DaemonRPCServer(socket_path)

        def fail() -> None:
            msg = "intentional error"
            raise RuntimeError(msg)

        server.register_method("fail", fail)

        await server.start()
        try:
            client = DaemonClient(socket_path)
            with pytest.raises(ChannelConnectionError, match="intentional"):
                await client.call("fail")
        finally:
            await server.stop()


class TestDaemonClient:
    """Client-side tests."""

    async def test_daemon_not_running(self, tmp_path: Path) -> None:
        socket_path = tmp_path / "nonexistent.sock"
        client = DaemonClient(socket_path)
        assert client.is_daemon_running() is False
        with pytest.raises(ChannelConnectionError, match="not running"):
            await client.call("ping")

    def test_stale_socket_detected(self, tmp_path: Path) -> None:
        """A stale socket file (no listener) returns False."""
        socket_path = tmp_path / "test.sock"
        socket_path.touch()  # File exists but no daemon listening
        client = DaemonClient(socket_path)
        assert client.is_daemon_running() is False


class TestDaemonRPCServer:
    """Server-side tests."""

    async def test_start_creates_socket(self, tmp_path: Path) -> None:
        socket_path = tmp_path / "test.sock"
        server = DaemonRPCServer(socket_path)
        await server.start()
        assert socket_path.exists()
        await server.stop()

    async def test_stop_removes_socket(self, tmp_path: Path) -> None:
        socket_path = tmp_path / "test.sock"
        server = DaemonRPCServer(socket_path)
        await server.start()
        await server.stop()
        assert not socket_path.exists()

    async def test_socket_permissions(self, tmp_path: Path) -> None:

        socket_path = tmp_path / "test.sock"
        server = DaemonRPCServer(socket_path)
        await server.start()
        mode = socket_path.stat().st_mode
        # Check owner-only (0o600) — socket type + permissions
        assert mode & 0o777 == 0o600  # noqa: PLR2004
        await server.stop()

    async def test_stale_socket_replaced(self, tmp_path: Path) -> None:
        socket_path = tmp_path / "test.sock"
        socket_path.touch()  # Stale socket
        server = DaemonRPCServer(socket_path)
        await server.start()
        assert socket_path.exists()
        await server.stop()

    async def test_register_method(self, tmp_path: Path) -> None:
        server = DaemonRPCServer(tmp_path / "test.sock")
        server.register_method("test", lambda: True)
        assert "test" in server._methods

    async def test_multiple_clients(self, tmp_path: Path) -> None:
        """Multiple sequential client calls."""
        socket_path = tmp_path / "test.sock"
        server = DaemonRPCServer(socket_path)
        counter = {"n": 0}

        def increment() -> int:
            counter["n"] += 1
            return counter["n"]

        server.register_method("inc", increment)

        await server.start()
        try:
            client = DaemonClient(socket_path)
            r1 = await client.call("inc")
            r2 = await client.call("inc")
            assert r1 == 1
            assert r2 == 2  # noqa: PLR2004
        finally:
            await server.stop()


class TestRPCServerCoverageGaps:
    """Cover remaining RPC server paths."""

    @pytest.mark.asyncio()
    async def test_stop_without_start(self, tmp_path: Path) -> None:
        """Stop when server was never started is safe."""
        socket_path = tmp_path / "test.sock"
        server = DaemonRPCServer(socket_path)
        await server.stop()  # _server is None — should not raise
        assert not socket_path.exists()

    @pytest.mark.asyncio()
    async def test_handle_timeout_error(self, tmp_path: Path) -> None:
        """TimeoutError during connection handling sends error response."""
        socket_path = tmp_path / "test.sock"
        server = DaemonRPCServer(socket_path)

        await server.start()
        try:
            with patch(
                "sovyx.engine.rpc_server.rpc_recv",
                side_effect=TimeoutError("timed out"),
            ):
                reader, writer = await asyncio.open_unix_connection(str(socket_path))
                # Server sends length-prefixed error response
                from sovyx.engine.rpc_protocol import _HEADER_SIZE

                header = await asyncio.wait_for(
                    reader.readexactly(_HEADER_SIZE),
                    timeout=5.0,
                )
                length = int.from_bytes(header, "big")
                raw = await asyncio.wait_for(
                    reader.readexactly(length),
                    timeout=5.0,
                )
                data = json.loads(raw.decode())
                assert data["error"]["code"] == -32000
                assert "timeout" in data["error"]["message"].lower()
                writer.close()
                await writer.wait_closed()
        finally:
            await server.stop()

    @pytest.mark.asyncio()
    async def test_handle_generic_exception(self, tmp_path: Path) -> None:
        """Generic exception during handling is logged silently."""
        socket_path = tmp_path / "test.sock"
        server = DaemonRPCServer(socket_path)

        await server.start()
        try:
            with patch(
                "sovyx.engine.rpc_server.rpc_recv",
                side_effect=RuntimeError("unexpected"),
            ):
                reader, writer = await asyncio.open_unix_connection(str(socket_path))
                # Connection should be closed by server
                _ = await asyncio.wait_for(reader.read(4096), timeout=5.0)
                # Server may or may not send data before closing
                writer.close()
                await writer.wait_closed()
        finally:
            await server.stop()
