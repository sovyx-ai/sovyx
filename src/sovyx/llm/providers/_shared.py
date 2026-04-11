"""Shared utilities for LLM provider implementations.

Centralises JSON parsing, retry delay calculation, and response
validation to eliminate duplication across Anthropic/OpenAI/Ollama.

See ``sovyx-imm-d2-llm-providers`` §2 and §7 for design rationale.
"""

from __future__ import annotations

import json
import math
import random
from typing import TYPE_CHECKING, Any

from sovyx.engine.errors import LLMError

if TYPE_CHECKING:
    import httpx
from sovyx.observability.logging import get_logger

logger = get_logger(__name__)

# Retry constants (Full Jitter — AWS recommended)
_BASE_DELAY = 1.0
_MAX_DELAY = 30.0


def safe_parse_json(resp: httpx.Response, provider: str) -> dict[str, Any]:
    """Parse JSON from an HTTP response with content-type validation.

    Guards against HTML error pages, empty bodies, and malformed JSON
    that ``resp.json()`` would either silently misparse or crash on.

    Args:
        resp: httpx response object.
        provider: Provider name for error messages.

    Returns:
        Parsed JSON dict.

    Raises:
        LLMError: If response is not valid JSON.
    """
    content_type = resp.headers.get("content-type", "")
    if "text/html" in content_type:
        msg = (
            f"{provider} returned HTML instead of JSON "
            f"(status {resp.status_code}). Possible proxy/CDN error."
        )
        raise LLMError(msg)

    body = resp.text
    if not body.strip():
        msg = f"{provider} returned empty response body (status {resp.status_code})"
        raise LLMError(msg)

    try:
        result: dict[str, Any] = json.loads(body)
    except json.JSONDecodeError as e:
        msg = f"{provider} returned invalid JSON (status {resp.status_code}): {body[:200]}"
        raise LLMError(msg) from e

    return result


def _sanitize_tool_name(name: str) -> str:
    """Sanitize tool name for LLM providers.

    OpenAI requires ``^[a-zA-Z0-9_-]+$`` (no dots).
    Sovyx uses exactly ONE dot for namespacing: ``plugin.tool``.
    We replace that single dot with ``--`` (double hyphen).

    Hyphens ARE allowed in OpenAI tool names. Plugin names
    validated by manifest use ``[a-z][a-z0-9-]*`` so single
    hyphens are common, but ``--`` never appears in valid names
    (manifest blocks consecutive hyphens).

    ``calculator.calculate`` → ``calculator--calculate``
    ``my-plugin.do_thing``   → ``my-plugin--do_thing``
    """
    return name.replace(".", "--", 1)


def _unsanitize_tool_name(name: str) -> str:
    """Reverse tool name sanitization.

    Replaces the first ``--`` back to ``.`` for internal dispatch.

    ``calculator--calculate`` → ``calculator.calculate``
    ``my-plugin--do_thing``   → ``my-plugin.do_thing``
    """
    return name.replace("--", ".", 1)


def format_tools_openai(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Convert Sovyx tool definitions to OpenAI function-calling format.

    Input: list of ``{"name": str, "description": str, "parameters": dict}``
    Output: list of ``{"type": "function", "function": {...}}``

    Also used by Ollama (same format).
    """
    formatted: list[dict[str, Any]] = []
    for tool in tools:
        formatted.append(
            {
                "type": "function",
                "function": {
                    "name": _sanitize_tool_name(tool.get("name", "")),
                    "description": tool.get("description", ""),
                    "parameters": tool.get("parameters", {}),
                },
            }
        )
    return formatted


def format_tools_anthropic(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Convert Sovyx tool definitions to Anthropic tool format.

    Input: list of ``{"name": str, "description": str, "parameters": dict}``
    Output: list of ``{"name": str, "description": str, "input_schema": dict}``
    """
    formatted: list[dict[str, Any]] = []
    for tool in tools:
        formatted.append(
            {
                "name": _sanitize_tool_name(tool.get("name", "")),
                "description": tool.get("description", ""),
                "input_schema": tool.get("parameters", {}),
            }
        )
    return formatted


def format_tools_google(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Convert Sovyx tool definitions to Google Gemini format.

    Input: list of ``{"name": str, "description": str, "parameters": dict}``
    Output: ``[{"functionDeclarations": [...]}]``
    """
    declarations: list[dict[str, Any]] = []
    for tool in tools:
        declarations.append(
            {
                "name": _sanitize_tool_name(tool.get("name", "")),
                "description": tool.get("description", ""),
                "parameters": tool.get("parameters", {}),
            }
        )
    return [{"functionDeclarations": declarations}]


def parse_tool_calls_openai(tool_calls_data: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Parse OpenAI tool_calls into Sovyx format.

    Input: ``[{"id": str, "function": {"name": str, "arguments": str}}]``
    Output: ``[{"id": str, "function_name": str, "arguments": dict}]``

    Also used by Ollama.
    """
    results: list[dict[str, Any]] = []
    for tc in tool_calls_data:
        func = tc.get("function", {})
        args_str = func.get("arguments", "{}")
        try:
            arguments = json.loads(args_str) if isinstance(args_str, str) else args_str
        except json.JSONDecodeError:
            arguments = {}
        results.append(
            {
                "id": tc.get("id", ""),
                "function_name": _unsanitize_tool_name(func.get("name", "")),
                "arguments": arguments if isinstance(arguments, dict) else {},
            }
        )
    return results


def parse_tool_calls_anthropic(content_blocks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Parse Anthropic tool_use blocks into Sovyx format.

    Input: content blocks with ``{"type": "tool_use", "id": str, "name": str, "input": dict}``
    Output: ``[{"id": str, "function_name": str, "arguments": dict}]``
    """
    results: list[dict[str, Any]] = []
    for block in content_blocks:
        if block.get("type") == "tool_use":
            results.append(
                {
                    "id": block.get("id", ""),
                    "function_name": _unsanitize_tool_name(block.get("name", "")),
                    "arguments": block.get("input", {}),
                }
            )
    return results


def parse_tool_calls_google(parts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Parse Google Gemini functionCall parts into Sovyx format.

    Input: parts with ``{"functionCall": {"name": str, "args": dict}}``
    Output: ``[{"id": str, "function_name": str, "arguments": dict}]``
    """
    results: list[dict[str, Any]] = []
    for i, part in enumerate(parts):
        fc = part.get("functionCall")
        if fc:
            results.append(
                {
                    "id": f"gemini-{i}",
                    "function_name": _unsanitize_tool_name(fc.get("name", "")),
                    "arguments": fc.get("args", {}),
                }
            )
    return results


def retry_delay(attempt: int, resp: httpx.Response | None = None) -> float:
    """Calculate retry delay using Full Jitter (AWS best practice).

    Formula: ``sleep = random_between(0, min(cap, base × 2^attempt))``

    Respects ``Retry-After`` header when present (takes precedence).

    Args:
        attempt: Zero-based retry attempt number.
        resp: Optional response to check for Retry-After header.

    Returns:
        Delay in seconds.
    """
    # Respect Retry-After header if present
    if resp is not None:
        retry_after = resp.headers.get("retry-after")
        if retry_after is not None:
            try:
                return max(0.5, float(retry_after))
            except ValueError:
                pass  # Non-numeric Retry-After — fall through to jitter

    # Full Jitter: random(0, min(cap, base × 2^attempt))
    exp_delay = min(_MAX_DELAY, _BASE_DELAY * math.pow(2, attempt))
    return random.uniform(0, exp_delay)  # nosec B311 — non-crypto jitter for backoff
