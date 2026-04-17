"""Sovyx DaemonRPCServer — JSON-RPC 2.0 over Unix socket (or TCP on Windows)."""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable

from sovyx.engine.rpc_protocol import rpc_recv, rpc_send
from sovyx.observability.logging import get_logger

logger = get_logger(__name__)

# Default socket path (on Windows, the .port sibling stores the TCP port)
DEFAULT_SOCKET_PATH = Path.home() / ".sovyx" / "sovyx.sock"


def _port_file_for(socket_path: Path) -> Path:
    """Derive TCP port-file path from the nominal socket path."""
    return socket_path.with_suffix(".port")


class DaemonRPCServer:
    """JSON-RPC 2.0 server via Unix domain socket (or TCP localhost on Windows).

    On Unix/macOS: binds a domain socket with 0o600 permissions.
    On Windows: binds TCP 127.0.0.1 on an ephemeral port, writes port to .port file.
    """

    def __init__(
        self,
        socket_path: Path | None = None,
    ) -> None:
        self._socket_path = socket_path or DEFAULT_SOCKET_PATH
        self._methods: dict[str, Callable[..., Any]] = {}
        self._server: asyncio.AbstractServer | None = None

    def register_method(self, name: str, handler: Callable[..., Any]) -> None:
        """Register an RPC method."""
        self._methods[name] = handler
        logger.debug("rpc_method_registered", method=name)

    async def start(self) -> None:
        """Start accepting connections (Unix socket or TCP on Windows)."""
        self._socket_path.parent.mkdir(parents=True, exist_ok=True)

        if sys.platform == "win32":
            self._server = await asyncio.start_server(
                self._handle_connection,
                host="127.0.0.1",
                port=0,
            )
            port = self._server.sockets[0].getsockname()[1]
            port_file = _port_file_for(self._socket_path)
            port_file.write_text(str(port), encoding="utf-8")
            logger.info("rpc_server_started", transport="tcp", port=port)
        else:
            if self._socket_path.exists():
                self._socket_path.unlink()
            self._server = await asyncio.start_unix_server(
                self._handle_connection,
                path=str(self._socket_path),
            )
            os.chmod(self._socket_path, 0o600)
            logger.info("rpc_server_started", transport="unix", path=str(self._socket_path))

    async def stop(self) -> None:
        """Close server and cleanup files."""
        if self._server:
            self._server.close()
            await self._server.wait_closed()
        if sys.platform == "win32":
            _port_file_for(self._socket_path).unlink(missing_ok=True)
        else:
            self._socket_path.unlink(missing_ok=True)
        logger.info("rpc_server_stopped")

    async def _handle_connection(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        """Handle a single client connection."""
        try:
            request = await rpc_recv(reader)
            response = await self._process_request(request)
            await rpc_send(writer, response)
        except TimeoutError:
            error_resp = self._error_response(None, -32000, "Request timeout")
            await rpc_send(writer, error_resp)
        except (json.JSONDecodeError, asyncio.IncompleteReadError):
            error_resp = self._error_response(None, -32700, "Parse error")
            await rpc_send(writer, error_resp)
        except Exception:  # noqa: BLE001
            logger.exception("rpc_connection_error")
        finally:
            writer.close()
            await writer.wait_closed()

    async def _process_request(self, request: dict[str, Any]) -> dict[str, Any]:
        """Process a JSON-RPC 2.0 request."""
        req_id = request.get("id")
        method = request.get("method", "")
        params = request.get("params", {})

        if method not in self._methods:
            return self._error_response(req_id, -32601, f"Method not found: {method}")

        handler = self._methods[method]
        try:
            result = handler(**params) if isinstance(params, dict) else handler()
            # Handle async handlers
            if asyncio.iscoroutine(result):
                result = await result
            return {
                "jsonrpc": "2.0",
                "result": result,
                "id": req_id,
            }
        except Exception as e:  # noqa: BLE001 — RPC dispatch boundary — translates to error response
            return self._error_response(req_id, -32000, str(e))

    @staticmethod
    def _error_response(req_id: int | str | None, code: int, message: str) -> dict[str, Any]:
        """Build JSON-RPC error response."""
        return {
            "jsonrpc": "2.0",
            "error": {"code": code, "message": message},
            "id": req_id,
        }
