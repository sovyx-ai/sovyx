"""Tests for sovyx.engine.rpc_server + sovyx.cli.rpc_client."""

from __future__ import annotations

import asyncio
import json
import sys
from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest

from sovyx.cli.rpc_client import DaemonClient, _port_file_for
from sovyx.engine.errors import ChannelConnectionError
from sovyx.engine.rpc_server import DaemonRPCServer

if TYPE_CHECKING:
    from pathlib import Path

_IS_WINDOWS = sys.platform == "win32"


class TestRPCEndToEnd:
    """Server + Client integration (works on both Unix and Windows)."""

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
        """A stale file (no listener) returns False."""
        socket_path = tmp_path / "test.sock"
        if _IS_WINDOWS:
            _port_file_for(socket_path).write_text("1", encoding="utf-8")
        else:
            socket_path.touch()
        client = DaemonClient(socket_path)
        assert client.is_daemon_running() is False


class TestDaemonRPCServer:
    """Server-side tests."""

    async def test_start_creates_transport_file(self, tmp_path: Path) -> None:
        socket_path = tmp_path / "test.sock"
        server = DaemonRPCServer(socket_path)
        await server.start()
        if _IS_WINDOWS:
            assert _port_file_for(socket_path).exists()
        else:
            assert socket_path.exists()
        await server.stop()

    async def test_stop_removes_transport_file(self, tmp_path: Path) -> None:
        socket_path = tmp_path / "test.sock"
        server = DaemonRPCServer(socket_path)
        await server.start()
        await server.stop()
        if _IS_WINDOWS:
            assert not _port_file_for(socket_path).exists()
        else:
            assert not socket_path.exists()

    @pytest.mark.skipif(_IS_WINDOWS, reason="Unix socket permissions not applicable on Windows")
    async def test_socket_permissions(self, tmp_path: Path) -> None:
        socket_path = tmp_path / "test.sock"
        server = DaemonRPCServer(socket_path)
        await server.start()
        mode = socket_path.stat().st_mode
        assert mode & 0o777 == 0o600  # noqa: PLR2004
        await server.stop()

    async def test_stale_file_replaced(self, tmp_path: Path) -> None:
        socket_path = tmp_path / "test.sock"
        if _IS_WINDOWS:
            _port_file_for(socket_path).write_text("0", encoding="utf-8")
        else:
            socket_path.touch()
        server = DaemonRPCServer(socket_path)
        await server.start()
        if _IS_WINDOWS:
            assert _port_file_for(socket_path).exists()
        else:
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

    async def test_port_file_contains_valid_port(self, tmp_path: Path) -> None:
        """On Windows, the .port file contains a valid TCP port number."""
        if not _IS_WINDOWS:
            pytest.skip("TCP port file only written on Windows")
        socket_path = tmp_path / "test.sock"
        server = DaemonRPCServer(socket_path)
        await server.start()
        try:
            port_file = _port_file_for(socket_path)
            port = int(port_file.read_text(encoding="utf-8").strip())
            assert 1 <= port <= 65535  # noqa: PLR2004
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
        if _IS_WINDOWS:
            assert not _port_file_for(socket_path).exists()
        else:
            assert not socket_path.exists()

    @pytest.mark.skip(
        reason="Flaky 10s asyncio race on CI runners; socket timeouts interact "
        "with the Ubuntu kernel's TCP backoff differently than expected. "
        "Covered by integration smoke tests."
    )
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
                if _IS_WINDOWS:
                    port = int(_port_file_for(socket_path).read_text(encoding="utf-8").strip())
                    reader, writer = await asyncio.open_connection("127.0.0.1", port)
                else:
                    reader, writer = await asyncio.open_unix_connection(str(socket_path))
                from sovyx.engine.rpc_protocol import _HEADER_SIZE

                header = await asyncio.wait_for(
                    reader.readexactly(_HEADER_SIZE),
                    timeout=2.0,
                )
                length = int.from_bytes(header, "big")
                raw = await asyncio.wait_for(
                    reader.readexactly(length),
                    timeout=2.0,
                )
                data = json.loads(raw.decode())
                assert data["error"]["code"] == -32000
                assert "timeout" in data["error"]["message"].lower()
                writer.close()
                await writer.wait_closed()
        finally:
            await server.stop()

    @pytest.mark.skip(reason="Same async-race cause as test_handle_timeout_error above.")
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
                if _IS_WINDOWS:
                    port = int(_port_file_for(socket_path).read_text(encoding="utf-8").strip())
                    reader, writer = await asyncio.open_connection("127.0.0.1", port)
                else:
                    reader, writer = await asyncio.open_unix_connection(str(socket_path))
                _ = await asyncio.wait_for(reader.read(4096), timeout=2.0)
                writer.close()
                await writer.wait_closed()
        finally:
            await server.stop()
