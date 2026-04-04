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
        msg = (
            f"{provider} returned invalid JSON (status {resp.status_code}): "
            f"{body[:200]}"
        )
        raise LLMError(msg) from e

    return result


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
    return random.uniform(0, exp_delay)
