"""Length-prefixed RPC protocol for Unix domain socket communication.

Wire format: ``[4-byte big-endian length][JSON payload]``

This replaces the previous raw ``read(65536)`` approach which silently
truncated payloads >64KB.  The 4-byte header supports payloads up to
~4GB; a 10MB safety limit prevents abuse.

See ``sovyx-imm-d8d10-infra-polish`` §1 for design rationale.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

# 4-byte big-endian length prefix
_HEADER_SIZE = 4

# Safety limit: reject payloads larger than 10MB
_MAX_PAYLOAD = 10 * 1024 * 1024


async def rpc_send(writer: asyncio.StreamWriter, payload: dict[str, Any]) -> None:
    """Send a length-prefixed JSON message.

    Args:
        writer: asyncio stream writer.
        payload: JSON-serialisable dict.

    Raises:
        ValueError: If serialised payload exceeds ``_MAX_PAYLOAD``.
    """
    data = json.dumps(payload).encode()
    if len(data) > _MAX_PAYLOAD:
        msg = f"RPC payload too large: {len(data):,} bytes (limit {_MAX_PAYLOAD:,})"
        raise ValueError(msg)
    writer.write(len(data).to_bytes(_HEADER_SIZE, "big") + data)
    await writer.drain()


async def rpc_recv(
    reader: asyncio.StreamReader,
    timeout: float = 10.0,
) -> dict[str, Any]:
    """Receive a length-prefixed JSON message.

    Args:
        reader: asyncio stream reader.
        timeout: Max wait time in seconds for each read operation.

    Returns:
        Parsed JSON dict.

    Raises:
        ValueError: If declared length exceeds ``_MAX_PAYLOAD``.
        asyncio.IncompleteReadError: If connection closed mid-message.
        TimeoutError: If read exceeds *timeout*.
        json.JSONDecodeError: If payload is not valid JSON.
    """
    header = await asyncio.wait_for(
        reader.readexactly(_HEADER_SIZE),
        timeout=timeout,
    )
    length = int.from_bytes(header, "big")
    if length > _MAX_PAYLOAD:
        msg = f"RPC payload too large: {length:,} bytes (limit {_MAX_PAYLOAD:,})"
        raise ValueError(msg)
    data = await asyncio.wait_for(
        reader.readexactly(length),
        timeout=timeout,
    )
    result: dict[str, Any] = json.loads(data.decode())
    return result
