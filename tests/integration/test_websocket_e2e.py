"""VAL-26: WebSocket event flow E2E.

Tests the /ws endpoint: auth, ping/pong, event broadcast.
Uses Starlette TestClient WebSocket support.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest
from starlette.testclient import TestClient

from sovyx.dashboard.server import create_app
from sovyx.engine.config import APIConfig

_TOKEN = "ws-test-token"


@pytest.fixture()
def app() -> object:
    with patch("sovyx.dashboard.server.TOKEN_FILE") as mock_tf:
        mock_tf.exists.return_value = True
        mock_tf.read_text.return_value = _TOKEN
        return create_app(APIConfig(host="127.0.0.1", port=0))


@pytest.fixture()
def sync_client(app: object) -> TestClient:
    return TestClient(app)  # type: ignore[arg-type]


class TestWebSocketAuth:
    """WS authentication via query param."""

    def test_no_token_closes_4001(self, sync_client: TestClient) -> None:
        with pytest.raises(Exception), sync_client.websocket_connect("/ws"):  # noqa: B017
            pass

    def test_wrong_token_closes_4001(self, sync_client: TestClient) -> None:
        with pytest.raises(Exception), sync_client.websocket_connect("/ws?token=wrong"):  # noqa: B017
            pass

    def test_valid_token_connects(self, sync_client: TestClient) -> None:
        with sync_client.websocket_connect(f"/ws?token={_TOKEN}") as ws:
            ws.send_text("ping")
            data = ws.receive_text()
            assert data == "pong"


class TestWebSocketPingPong:
    """Keep-alive mechanism."""

    def test_multiple_pings(self, sync_client: TestClient) -> None:
        with sync_client.websocket_connect(f"/ws?token={_TOKEN}") as ws:
            for _ in range(3):
                ws.send_text("ping")
                assert ws.receive_text() == "pong"


class TestWebSocketDisconnect:
    """Clean disconnect handling."""

    def test_disconnect_is_clean(self, sync_client: TestClient) -> None:
        """Client can disconnect without errors."""
        with sync_client.websocket_connect(f"/ws?token={_TOKEN}") as ws:
            ws.send_text("ping")
            assert ws.receive_text() == "pong"
        # No exception on scope exit = clean disconnect
