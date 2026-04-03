"""Sovyx DaemonRPCServer — JSON-RPC 2.0 over Unix domain socket."""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable

from sovyx.observability.logging import get_logger

logger = get_logger(__name__)

# Default socket path
DEFAULT_SOCKET_PATH = Path.home() / ".sovyx" / "sovyx.sock"


class DaemonRPCServer:
    """JSON-RPC 2.0 server via Unix domain socket.

    Registers methods that CLI can invoke.
    Socket permissions: 0o600 (owner-only).
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
        """Create Unix socket and start accepting connections."""
        self._socket_path.parent.mkdir(parents=True, exist_ok=True)

        # Remove stale socket
        if self._socket_path.exists():
            self._socket_path.unlink()

        self._server = await asyncio.start_unix_server(
            self._handle_connection,
            path=str(self._socket_path),
        )
        # Set permissions to owner-only
        os.chmod(self._socket_path, 0o600)
        logger.info("rpc_server_started", path=str(self._socket_path))

    async def stop(self) -> None:
        """Close socket and cleanup file."""
        if self._server:
            self._server.close()
            await self._server.wait_closed()
        self._socket_path.unlink(missing_ok=True)
        logger.info("rpc_server_stopped")

    async def _handle_connection(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        """Handle a single client connection."""
        try:
            data = await asyncio.wait_for(reader.read(65536), timeout=10.0)
            if not data:
                return

            request = json.loads(data.decode())
            response = await self._process_request(request)
            writer.write(json.dumps(response).encode())
            await writer.drain()
        except TimeoutError:
            error_resp = self._error_response(None, -32000, "Request timeout")
            writer.write(json.dumps(error_resp).encode())
            await writer.drain()
        except json.JSONDecodeError:
            error_resp = self._error_response(None, -32700, "Parse error")
            writer.write(json.dumps(error_resp).encode())
            await writer.drain()
        except Exception:
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
        except Exception as e:
            return self._error_response(req_id, -32000, str(e))

    @staticmethod
    def _error_response(req_id: int | str | None, code: int, message: str) -> dict[str, Any]:
        """Build JSON-RPC error response."""
        return {
            "jsonrpc": "2.0",
            "error": {"code": code, "message": message},
            "id": req_id,
        }
