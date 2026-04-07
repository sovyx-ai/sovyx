"""VAL-34: WebSocket adversarial testing.

Tests WebSocket endpoint against malformed messages, oversized payloads,
rapid reconnects, and protocol violations.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest
from starlette.testclient import TestClient

from sovyx.dashboard.server import create_app
from sovyx.engine.config import APIConfig

_TOKEN = "ws-adv-token"


@pytest.fixture()
def app() -> object:
    with patch("sovyx.dashboard.server.TOKEN_FILE") as mock_tf:
        mock_tf.exists.return_value = True
        mock_tf.read_text.return_value = _TOKEN
        return create_app(APIConfig(host="127.0.0.1", port=0))


@pytest.fixture()
def sync_client(app: object) -> TestClient:
    return TestClient(app)  # type: ignore[arg-type]


class TestMalformedMessages:
    """Send invalid data through WebSocket."""

    def test_empty_message(self, sync_client: TestClient) -> None:
        with sync_client.websocket_connect(f"/ws?token={_TOKEN}") as ws:
            ws.send_text("")
            # Should not crash — might ignore or echo
            ws.send_text("ping")
            assert ws.receive_text() == "pong"

    def test_very_long_message(self, sync_client: TestClient) -> None:
        with sync_client.websocket_connect(f"/ws?token={_TOKEN}") as ws:
            ws.send_text("x" * 100_000)
            # Should not crash
            ws.send_text("ping")
            assert ws.receive_text() == "pong"

    def test_json_message(self, sync_client: TestClient) -> None:
        with sync_client.websocket_connect(f"/ws?token={_TOKEN}") as ws:
            ws.send_text('{"type": "malicious", "payload": "test"}')
            ws.send_text("ping")
            assert ws.receive_text() == "pong"

    def test_binary_message_disconnects(self, sync_client: TestClient) -> None:
        """Binary data on text-only WS causes disconnect (expected)."""
        with pytest.raises(Exception), sync_client.websocket_connect(f"/ws?token={_TOKEN}") as ws:  # noqa: B017
            ws.send_bytes(b"\x00\x01\x02\x03")
            ws.receive_text()  # Should fail/disconnect

    def test_null_bytes_in_message(self, sync_client: TestClient) -> None:
        with sync_client.websocket_connect(f"/ws?token={_TOKEN}") as ws:
            ws.send_text("ping\x00pong")
            ws.send_text("ping")
            assert ws.receive_text() == "pong"


class TestRapidReconnects:
    """Multiple rapid connect/disconnect cycles."""

    def test_rapid_reconnects(self, sync_client: TestClient) -> None:
        for _ in range(10):
            with sync_client.websocket_connect(f"/ws?token={_TOKEN}") as ws:
                ws.send_text("ping")
                assert ws.receive_text() == "pong"


class TestSQLiViaWebSocket:
    """SQL injection attempts via WebSocket messages."""

    @pytest.mark.parametrize(
        "payload",
        [
            "'; DROP TABLE concepts; --",
            "ping' OR '1'='1",
            "ping; SELECT * FROM sqlite_master",
        ],
    )
    def test_sqli_in_ws_message(
        self, sync_client: TestClient, payload: str,
    ) -> None:
        with sync_client.websocket_connect(f"/ws?token={_TOKEN}") as ws:
            ws.send_text(payload)
            # Should not crash — server ignores non-ping messages
            ws.send_text("ping")
            assert ws.receive_text() == "pong"
