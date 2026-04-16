"""Shared streaming helpers for LLM provider implementations.

SSE (Server-Sent Events) for Anthropic / OpenAI / Google, and NDJSON
for Ollama. All four cloud + local providers converge on yielding
:class:`sovyx.llm.models.LLMStreamChunk` objects via their own
``stream()`` method — this module supplies the line-level parsers.
"""

from __future__ import annotations

import contextlib
import json
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    import httpx


async def iter_sse_events(
    response: httpx.Response,
) -> AsyncIterator[tuple[str, dict[str, Any]]]:
    """Yield ``(event_type, data_dict)`` pairs from an SSE stream.

    Parses the standard ``event: <name>\\ndata: <json>\\n\\n`` framing
    used by Anthropic and (without the ``event:`` line) OpenAI. Lines
    starting with ``:`` are comments — discarded. The OpenAI sentinel
    ``data: [DONE]`` is yielded as ``("done", {})`` so callers can
    short-circuit cleanly.

    Args:
        response: An open httpx streaming response.

    Yields:
        ``(event_type, data)`` for each complete SSE event. ``event_type``
        defaults to ``"message"`` when no explicit ``event:`` line is
        present (OpenAI/Google convention).
    """
    event_type = "message"
    buffer: list[str] = []

    async for raw_line in response.aiter_lines():
        line = raw_line.rstrip("\r")
        if not line:
            # Blank line = event boundary — flush.
            if buffer:
                data_str = "\n".join(buffer)
                buffer.clear()
                if data_str.strip() == "[DONE]":
                    yield ("done", {})
                else:
                    with contextlib.suppress(json.JSONDecodeError):
                        yield (event_type, json.loads(data_str))
                event_type = "message"
            continue

        if line.startswith(":"):
            # SSE comment line — keep-alive, discard.
            continue

        if line.startswith("event:"):
            event_type = line[len("event:") :].strip()
            continue

        if line.startswith("data:"):
            buffer.append(line[len("data:") :].lstrip())
            continue

        # Unknown line type — ignore (forward compat).

    # Flush trailing event without blank-line terminator (rare).
    if buffer:
        data_str = "\n".join(buffer)
        if data_str.strip() == "[DONE]":
            yield ("done", {})
        else:
            with contextlib.suppress(json.JSONDecodeError):
                yield (event_type, json.loads(data_str))


async def iter_ndjson_lines(
    response: httpx.Response,
) -> AsyncIterator[dict[str, Any]]:
    """Yield parsed JSON objects from a newline-delimited JSON stream.

    Used by Ollama, which sends one JSON object per line on its
    ``/api/chat`` streaming endpoint. The terminal ``done: true`` line
    is yielded like any other — the caller decides when to stop.

    Args:
        response: An open httpx streaming response.

    Yields:
        Parsed JSON dict per line. Malformed lines are skipped silently.
    """
    async for raw_line in response.aiter_lines():
        line = raw_line.strip()
        if not line:
            continue
        try:
            yield json.loads(line)
        except json.JSONDecodeError:
            # Skip malformed line, continue with the rest of the stream.
            continue
