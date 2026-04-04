"""Sovyx DaemonClient — JSON-RPC 2.0 client for CLI ↔ daemon communication."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from sovyx.engine.errors import ChannelConnectionError
from sovyx.engine.rpc_protocol import rpc_recv, rpc_send
from sovyx.observability.logging import get_logger

logger = get_logger(__name__)

DEFAULT_SOCKET_PATH = Path.home() / ".sovyx" / "sovyx.sock"


class DaemonClient:
    """JSON-RPC 2.0 client for daemon communication.

    Used by all CLI commands that need the daemon running.
    """

    def __init__(self, socket_path: Path | None = None) -> None:
        self._socket_path = socket_path or DEFAULT_SOCKET_PATH
        self._request_id = 0

    def is_daemon_running(self) -> bool:
        """Check if daemon is running by probing the socket.

        A stale socket file (from a crash) will fail the connect probe
        instead of falsely reporting the daemon as running.
        """
        if not self._socket_path.exists():
            return False
        try:
            import socket

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
