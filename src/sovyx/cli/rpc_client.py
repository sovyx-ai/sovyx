"""Sovyx DaemonClient — JSON-RPC 2.0 client for CLI ↔ daemon communication."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import Any

from sovyx.engine.errors import ChannelConnectionError
from sovyx.engine.rpc_protocol import rpc_recv, rpc_send
from sovyx.observability.logging import get_logger

logger = get_logger(__name__)

DEFAULT_SOCKET_PATH = Path.home() / ".sovyx" / "sovyx.sock"


def _port_file_for(socket_path: Path) -> Path:
    """Derive TCP port-file path from the nominal socket path."""
    return socket_path.with_suffix(".port")


class DaemonClient:
    """JSON-RPC 2.0 client for daemon communication.

    On Unix/macOS: connects via Unix domain socket.
    On Windows: reads TCP port from .port file, connects to 127.0.0.1.
    """

    def __init__(self, socket_path: Path | None = None) -> None:
        self._socket_path = socket_path or DEFAULT_SOCKET_PATH
        self._request_id = 0

    def _read_port(self) -> int | None:
        """Read the TCP port from the .port file (Windows transport)."""
        port_file = _port_file_for(self._socket_path)
        if not port_file.exists():
            return None
        try:
            port = int(port_file.read_text(encoding="utf-8").strip())
        except (ValueError, OSError):
            return None
        if not (1 <= port <= 65535):  # noqa: PLR2004
            return None
        return port

    def is_daemon_running(self) -> bool:
        """Check if daemon is running by probing the connection.

        On Unix: probes the domain socket.
        On Windows: reads port from .port file and probes TCP 127.0.0.1.
        A stale file (from a crash) will fail the connect probe.
        """
        import socket  # noqa: PLC0415

        if sys.platform == "win32":
            port = self._read_port()
            if port is None:
                return False
            try:
                sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                sock.settimeout(1.0)
                sock.connect(("127.0.0.1", port))
                sock.close()
            except (ConnectionRefusedError, OSError, TimeoutError):
                return False
            else:
                return True

        if not self._socket_path.exists():
            return False
        try:
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            sock.settimeout(1.0)
            sock.connect(str(self._socket_path))
            sock.close()
        except (ConnectionRefusedError, OSError, TimeoutError):
            return False
        else:
            return True

    async def call(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        timeout: float = 5.0,
    ) -> object:
        """Send JSON-RPC request and await response.

        Args:
            method: RPC method name.
            params: Method parameters.
            timeout: Max wait time in seconds.

        Returns:
            Result from the daemon.

        Raises:
            ChannelConnectionError: If daemon not running or timeout.
        """
        if not self.is_daemon_running():
            if sys.platform == "win32":
                port_file = _port_file_for(self._socket_path)
                msg = f"Sovyx daemon not running (no port file at {port_file})"
            else:
                msg = f"Sovyx daemon not running (no socket at {self._socket_path})"
            raise ChannelConnectionError(msg)

        self._request_id += 1
        request = {
            "jsonrpc": "2.0",
            "method": method,
            "params": params or {},
            "id": self._request_id,
        }

        try:
            if sys.platform == "win32":
                port = self._read_port()
                reader, writer = await asyncio.wait_for(
                    asyncio.open_connection("127.0.0.1", port),
                    timeout=timeout,
                )
            else:
                reader, writer = await asyncio.wait_for(
                    asyncio.open_unix_connection(str(self._socket_path)),
                    timeout=timeout,
                )
        except (TimeoutError, OSError) as e:  # pragma: no cover
            msg = f"Cannot connect to daemon: {e}"
            raise ChannelConnectionError(msg) from e

        try:
            await rpc_send(writer, request)
            response = await rpc_recv(reader, timeout=timeout)

            if "error" in response:
                error = response["error"]
                msg = f"RPC error ({error.get('code', '?')}): {error.get('message', 'unknown')}"
                raise ChannelConnectionError(msg)

            return response.get("result")
        except TimeoutError as e:  # pragma: no cover
            msg = f"Daemon response timeout ({timeout}s)"
            raise ChannelConnectionError(msg) from e
        finally:
            writer.close()
            await writer.wait_closed()
