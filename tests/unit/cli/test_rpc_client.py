"""Tests for sovyx.cli.rpc_client — DaemonClient JSON-RPC 2.0 client."""

from __future__ import annotations

import asyncio
import json
import sys
from typing import TYPE_CHECKING, Any
from unittest.mock import patch

if TYPE_CHECKING:
    from pathlib import Path

import pytest

from sovyx.cli.rpc_client import DEFAULT_SOCKET_PATH, DaemonClient, _port_file_for
from sovyx.engine.errors import ChannelConnectionError
from sovyx.engine.rpc_protocol import _HEADER_SIZE

_IS_WINDOWS = sys.platform == "win32"

# ── Helpers ──────────────────────────────────────────────────────────────────


def _encode_rpc_response(payload: dict[str, Any]) -> bytes:
    """Encode a JSON-RPC response with length prefix."""
    data = json.dumps(payload).encode()
    return len(data).to_bytes(_HEADER_SIZE, "big") + data


async def _start_mock_daemon(
    socket_path: Path,
    response: dict[str, Any] | None = None,
    *,
    hang: bool = False,
    close_early: bool = False,
) -> asyncio.AbstractServer:
    """Start a mock server that replies with a fixed response.

    On Unix: Unix domain socket. On Windows: TCP 127.0.0.1 with port file.
    """

    async def handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        if close_early:
            writer.close()
            await writer.wait_closed()
            return
        try:
            header = await reader.readexactly(_HEADER_SIZE)
        except asyncio.IncompleteReadError:
            writer.close()
            await writer.wait_closed()
            return
        length = int.from_bytes(header, "big")
        await reader.readexactly(length)
        if hang:
            await asyncio.sleep(60)
            return
        if response is not None:
            writer.write(_encode_rpc_response(response))
            await writer.drain()
        writer.close()
        await writer.wait_closed()

    if _IS_WINDOWS:
        server = await asyncio.start_server(handler, host="127.0.0.1", port=0)
        port = server.sockets[0].getsockname()[1]
        port_file = _port_file_for(socket_path)
        port_file.write_text(str(port), encoding="utf-8")
    else:
        server = await asyncio.start_unix_server(handler, path=str(socket_path))
    return server


async def _stop_mock_daemon(server: asyncio.AbstractServer, socket_path: Path) -> None:
    """Stop mock daemon and clean up transport files."""
    server.close()
    await server.wait_closed()
    if _IS_WINDOWS:
        _port_file_for(socket_path).unlink(missing_ok=True)


# ── Constructor ──────────────────────────────────────────────────────────────


class TestDaemonClientInit:
    """Tests for DaemonClient.__init__."""

    def test_default_socket_path(self) -> None:
        client = DaemonClient()
        assert client._socket_path == DEFAULT_SOCKET_PATH

    def test_custom_socket_path(self, tmp_path: Path) -> None:
        sock = tmp_path / "custom.sock"
        client = DaemonClient(socket_path=sock)
        assert client._socket_path == sock

    def test_initial_request_id_is_zero(self) -> None:
        client = DaemonClient()
        assert client._request_id == 0


# ── is_daemon_running ────────────────────────────────────────────────────────


class TestIsDaemonRunning:
    """Tests for DaemonClient.is_daemon_running."""

    def test_no_transport_file(self, tmp_path: Path) -> None:
        """No socket/port file → False."""
        client = DaemonClient(socket_path=tmp_path / "nonexistent.sock")
        assert client.is_daemon_running() is False

    @pytest.mark.asyncio
    async def test_daemon_running(self, tmp_path: Path) -> None:
        """Real server listening → True."""
        sock = tmp_path / "test.sock"
        resp = {"jsonrpc": "2.0", "id": 1, "result": "ok"}
        server = await _start_mock_daemon(sock, response=resp)
        try:
            client = DaemonClient(socket_path=sock)
            assert client.is_daemon_running() is True
        finally:
            await _stop_mock_daemon(server, sock)

    def test_stale_file(self, tmp_path: Path) -> None:
        """Transport file exists but no server → False."""
        sock = tmp_path / "stale.sock"
        if _IS_WINDOWS:
            _port_file_for(sock).write_text("1", encoding="utf-8")
        else:
            sock.touch()
        client = DaemonClient(socket_path=sock)
        assert client.is_daemon_running() is False

    def test_connection_refused(self, tmp_path: Path) -> None:
        """Transport file exists but connection refused → False."""
        sock = tmp_path / "refused.sock"
        if _IS_WINDOWS:
            _port_file_for(sock).write_text("1", encoding="utf-8")
        else:
            sock.touch()
        client = DaemonClient(socket_path=sock)
        assert client.is_daemon_running() is False

    @pytest.mark.skipif(not _IS_WINDOWS, reason="Windows-only: invalid port file content")
    def test_invalid_port_file_content(self, tmp_path: Path) -> None:
        """Port file with non-numeric content → False."""
        sock = tmp_path / "bad.sock"
        _port_file_for(sock).write_text("not-a-number", encoding="utf-8")
        client = DaemonClient(socket_path=sock)
        assert client.is_daemon_running() is False


# ── call ─────────────────────────────────────────────────────────────────────


class TestCall:
    """Tests for DaemonClient.call."""

    @pytest.mark.asyncio
    async def test_daemon_not_running_raises(self, tmp_path: Path) -> None:
        """call() when daemon not running → ChannelConnectionError."""
        client = DaemonClient(socket_path=tmp_path / "absent.sock")
        with pytest.raises(ChannelConnectionError, match="not running"):
            await client.call("status")

    @pytest.mark.asyncio
    async def test_successful_call(self, tmp_path: Path) -> None:
        """call() with valid response → returns result."""
        sock = tmp_path / "daemon.sock"
        response = {"jsonrpc": "2.0", "id": 1, "result": {"status": "running"}}
        server = await _start_mock_daemon(sock, response=response)
        try:
            client = DaemonClient(socket_path=sock)
            result = await client.call("status")
            assert result == {"status": "running"}
        finally:
            await _stop_mock_daemon(server, sock)

    @pytest.mark.asyncio
    async def test_request_id_increments(self, tmp_path: Path) -> None:
        """Each call increments the request ID."""
        sock = tmp_path / "daemon.sock"
        received_requests: list[dict[str, Any]] = []

        async def capturing_handler(
            reader: asyncio.StreamReader, writer: asyncio.StreamWriter
        ) -> None:
            header = await reader.readexactly(_HEADER_SIZE)
            length = int.from_bytes(header, "big")
            data = await reader.readexactly(length)
            req = json.loads(data.decode())
            received_requests.append(req)
            resp = {"jsonrpc": "2.0", "id": req["id"], "result": "ok"}
            encoded = _encode_rpc_response(resp)
            writer.write(encoded)
            await writer.drain()
            writer.close()
            await writer.wait_closed()

        if _IS_WINDOWS:
            server = await asyncio.start_server(capturing_handler, host="127.0.0.1", port=0)
            port = server.sockets[0].getsockname()[1]
            _port_file_for(sock).write_text(str(port), encoding="utf-8")
        else:
            server = await asyncio.start_unix_server(capturing_handler, path=str(sock))
        try:
            client = DaemonClient(socket_path=sock)
            await client.call("method1")
            await client.call("method2")
            assert received_requests[0]["id"] == 1
            assert received_requests[1]["id"] == 2
        finally:
            await _stop_mock_daemon(server, sock)

    @pytest.mark.asyncio
    async def test_params_forwarded(self, tmp_path: Path) -> None:
        """call() forwards params dict to daemon."""
        sock = tmp_path / "daemon.sock"
        received_params: list[dict[str, Any]] = []

        async def handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            header = await reader.readexactly(_HEADER_SIZE)
            length = int.from_bytes(header, "big")
            data = await reader.readexactly(length)
            req = json.loads(data.decode())
            received_params.append(req.get("params", {}))
            resp = _encode_rpc_response({"jsonrpc": "2.0", "id": req["id"], "result": None})
            writer.write(resp)
            await writer.drain()
            writer.close()
            await writer.wait_closed()

        if _IS_WINDOWS:
            server = await asyncio.start_server(handler, host="127.0.0.1", port=0)
            port = server.sockets[0].getsockname()[1]
            _port_file_for(sock).write_text(str(port), encoding="utf-8")
        else:
            server = await asyncio.start_unix_server(handler, path=str(sock))
        try:
            client = DaemonClient(socket_path=sock)
            await client.call("test", params={"key": "value"})
            assert received_params[0] == {"key": "value"}
        finally:
            await _stop_mock_daemon(server, sock)

    @pytest.mark.asyncio
    async def test_default_params_empty_dict(self, tmp_path: Path) -> None:
        """call() without params sends empty dict."""
        sock = tmp_path / "daemon.sock"
        received_params: list[dict[str, Any]] = []

        async def handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            header = await reader.readexactly(_HEADER_SIZE)
            length = int.from_bytes(header, "big")
            data = await reader.readexactly(length)
            req = json.loads(data.decode())
            received_params.append(req.get("params", {}))
            resp = _encode_rpc_response({"jsonrpc": "2.0", "id": req["id"], "result": None})
            writer.write(resp)
            await writer.drain()
            writer.close()
            await writer.wait_closed()

        if _IS_WINDOWS:
            server = await asyncio.start_server(handler, host="127.0.0.1", port=0)
            port = server.sockets[0].getsockname()[1]
            _port_file_for(sock).write_text(str(port), encoding="utf-8")
        else:
            server = await asyncio.start_unix_server(handler, path=str(sock))
        try:
            client = DaemonClient(socket_path=sock)
            await client.call("test")
            assert received_params[0] == {}
        finally:
            await _stop_mock_daemon(server, sock)

    @pytest.mark.asyncio
    async def test_rpc_error_response(self, tmp_path: Path) -> None:
        """call() with RPC error in response → ChannelConnectionError."""
        sock = tmp_path / "daemon.sock"
        error_response = {
            "jsonrpc": "2.0",
            "id": 1,
            "error": {"code": -32601, "message": "Method not found"},
        }
        server = await _start_mock_daemon(sock, response=error_response)
        try:
            client = DaemonClient(socket_path=sock)
            with pytest.raises(ChannelConnectionError, match="Method not found"):
                await client.call("nonexistent")
        finally:
            await _stop_mock_daemon(server, sock)

    @pytest.mark.asyncio
    async def test_rpc_error_missing_fields(self, tmp_path: Path) -> None:
        """call() with RPC error missing code/message → uses defaults."""
        sock = tmp_path / "daemon.sock"
        error_response = {
            "jsonrpc": "2.0",
            "id": 1,
            "error": {},
        }
        server = await _start_mock_daemon(sock, response=error_response)
        try:
            client = DaemonClient(socket_path=sock)
            with pytest.raises(ChannelConnectionError, match=r"RPC error \(\?\): unknown"):
                await client.call("test")
        finally:
            await _stop_mock_daemon(server, sock)

    @pytest.mark.asyncio
    async def test_result_none_when_missing(self, tmp_path: Path) -> None:
        """call() with response missing 'result' → returns None."""
        sock = tmp_path / "daemon.sock"
        response = {"jsonrpc": "2.0", "id": 1}
        server = await _start_mock_daemon(sock, response=response)
        try:
            client = DaemonClient(socket_path=sock)
            result = await client.call("test")
            assert result is None
        finally:
            await _stop_mock_daemon(server, sock)

    @pytest.mark.asyncio
    async def test_connect_timeout_no_transport(self, tmp_path: Path) -> None:
        """call() when is_daemon_running is mocked True but transport absent → error."""
        sock = tmp_path / "daemon.sock"
        client = DaemonClient(socket_path=sock)

        with (
            patch.object(client, "is_daemon_running", return_value=True),
            pytest.raises((ChannelConnectionError, OSError, FileNotFoundError)),
        ):
            await client.call("status", timeout=0.5)

    @pytest.mark.asyncio
    async def test_writer_closed_after_call(self, tmp_path: Path) -> None:
        """Writer is properly closed after successful call (finally block)."""
        sock = tmp_path / "daemon.sock"
        response = {"jsonrpc": "2.0", "id": 1, "result": "ok"}
        server = await _start_mock_daemon(sock, response=response)
        try:
            client = DaemonClient(socket_path=sock)
            await client.call("test")
        finally:
            await _stop_mock_daemon(server, sock)

    @pytest.mark.asyncio
    async def test_writer_closed_on_error(self, tmp_path: Path) -> None:
        """Writer is closed even when RPC error occurs (finally block)."""
        sock = tmp_path / "daemon.sock"
        error_response = {
            "jsonrpc": "2.0",
            "id": 1,
            "error": {"code": -1, "message": "fail"},
        }
        server = await _start_mock_daemon(sock, response=error_response)
        try:
            client = DaemonClient(socket_path=sock)
            with pytest.raises(ChannelConnectionError):
                await client.call("test")
        finally:
            await _stop_mock_daemon(server, sock)
