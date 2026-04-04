"""Tests for sovyx.llm.providers._shared — JSON parsing and retry delay."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from sovyx.engine.errors import LLMError
from sovyx.llm.providers._shared import retry_delay, safe_parse_json


class TestSafeParseJson:
    """safe_parse_json guards against bad API responses."""

    def test_valid_json(self) -> None:
        resp = _mock_response(
            status_code=200,
            text='{"content": [{"type": "text", "text": "hello"}]}',
            content_type="application/json",
        )
        result = safe_parse_json(resp, "TestProvider")
        assert result["content"][0]["text"] == "hello"

    def test_html_response_raises(self) -> None:
        resp = _mock_response(
            status_code=502,
            text="<html><body>Bad Gateway</body></html>",
            content_type="text/html",
        )
        with pytest.raises(LLMError, match="HTML instead of JSON"):
            safe_parse_json(resp, "TestProvider")

    def test_empty_body_raises(self) -> None:
        resp = _mock_response(
            status_code=200,
            text="",
            content_type="application/json",
        )
        with pytest.raises(LLMError, match="empty response body"):
            safe_parse_json(resp, "TestProvider")

    def test_whitespace_body_raises(self) -> None:
        resp = _mock_response(
            status_code=200,
            text="   \n  ",
            content_type="application/json",
        )
        with pytest.raises(LLMError, match="empty response body"):
            safe_parse_json(resp, "TestProvider")

    def test_malformed_json_raises(self) -> None:
        resp = _mock_response(
            status_code=200,
            text="{invalid json: true,}",
            content_type="application/json",
        )
        with pytest.raises(LLMError, match="invalid JSON"):
            safe_parse_json(resp, "TestProvider")

    def test_truncated_json_raises(self) -> None:
        resp = _mock_response(
            status_code=200,
            text='{"content": [{"type": "text"',
            content_type="application/json",
        )
        with pytest.raises(LLMError, match="invalid JSON"):
            safe_parse_json(resp, "TestProvider")


class TestRetryDelay:
    """retry_delay implements Full Jitter with Retry-After support."""

    def test_first_attempt_bounded(self) -> None:
        """Attempt 0: random(0, 1.0)."""
        delays = [retry_delay(0) for _ in range(100)]
        assert all(0 <= d <= 1.0 for d in delays)

    def test_second_attempt_bounded(self) -> None:
        """Attempt 1: random(0, 2.0)."""
        delays = [retry_delay(1) for _ in range(100)]
        assert all(0 <= d <= 2.0 for d in delays)

    def test_capped_at_max(self) -> None:
        """High attempt numbers don't exceed 30s cap."""
        delays = [retry_delay(10) for _ in range(100)]
        assert all(0 <= d <= 30.0 for d in delays)

    def test_has_jitter(self) -> None:
        """Delays should vary (not deterministic)."""
        delays = {retry_delay(2) for _ in range(50)}
        assert len(delays) > 1  # At least 2 distinct values

    def test_respects_retry_after_header(self) -> None:
        resp = _mock_response(
            status_code=429,
            text="rate limited",
            content_type="application/json",
            retry_after="5",
        )
        delay = retry_delay(0, resp)
        assert delay == 5.0

    def test_retry_after_minimum(self) -> None:
        """Retry-After < 0.5 is clamped to 0.5."""
        resp = _mock_response(status_code=429, text="", content_type="", retry_after="0.1")
        delay = retry_delay(0, resp)
        assert delay == 0.5

    def test_non_numeric_retry_after_falls_through(self) -> None:
        """Non-numeric Retry-After → use jitter instead."""
        resp = _mock_response(
            status_code=429,
            text="",
            content_type="",
            retry_after="Thu, 01 Dec 2025 16:00:00 GMT",
        )
        delay = retry_delay(0, resp)
        assert 0 <= delay <= 1.0  # Falls through to jitter


# ── Helpers ──────────────────────────────────────────────────────


def _mock_response(
    *,
    status_code: int,
    text: str,
    content_type: str,
    retry_after: str | None = None,
) -> MagicMock:
    """Create a mock httpx.Response."""
    resp = MagicMock()
    resp.status_code = status_code
    resp.text = text
    headers = {"content-type": content_type}
    if retry_after is not None:
        headers["retry-after"] = retry_after
    resp.headers = headers
    return resp
