"""Tests for POST /api/chat endpoint and dashboard chat module (DASH-01).

Coverage targets:
- sovyx.dashboard.chat: ≥95%
- /api/chat endpoint in server.py: ≥95%

Tests cover:
- Authentication (missing/invalid token)
- Input validation (empty, whitespace, wrong types, missing fields)
- Happy path (message → response)
- Conversation continuity (conversation_id reuse)
- Error handling (engine not running, cognitive timeout, cognitive error)
- WebSocket broadcast on chat message
- Edge cases (very long message, special characters, concurrent requests)
"""

from __future__ import annotations

import secrets
from datetime import datetime
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from starlette.testclient import TestClient

from sovyx.dashboard.server import create_app

if TYPE_CHECKING:
    from pathlib import Path


# ── Fixtures ──


@pytest.fixture(autouse=True)
def _clean_token(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Redirect token file to tmp_path for test isolation."""
    token_file = tmp_path / "token"
    monkeypatch.setattr("sovyx.dashboard.server.TOKEN_FILE", token_file)


@pytest.fixture()
def token(tmp_path: Path) -> str:
    """Generate a token and write it to the test token file."""
    t = secrets.token_urlsafe(32)
    token_file = tmp_path / "token"
    token_file.write_text(t)
    return t


@pytest.fixture()
def client(token: str) -> TestClient:
    """TestClient with auth token available."""
    app = create_app()
    return TestClient(app)


@pytest.fixture()
def auth_headers(token: str) -> dict[str, str]:
    """Authorization header for authenticated requests."""
    return {"Authorization": f"Bearer {token}"}


def _make_action_result(
    response_text: str = "Hello from Aria!",
    filtered: bool = False,
    error: bool = False,
    reply_to: str | None = None,
    tool_names: list[str] | None = None,
) -> object:
    """Create a mock ActionResult with the given properties.

    ``tool_names`` is a convenience list of fully-qualified tool names
    (``"plugin.tool"``) that get converted into synthetic ``ToolCall``
    entries on ``tool_calls_made``. Used by tag-derivation tests.
    """
    from sovyx.cognitive.act import ActionResult
    from sovyx.llm.models import ToolCall

    tool_calls_made: list[ToolCall] = []
    if tool_names:
        tool_calls_made = [
            ToolCall(id=f"call-{i}", function_name=name, arguments={})
            for i, name in enumerate(tool_names)
        ]

    return ActionResult(
        response_text=response_text,
        target_channel="dashboard",
        reply_to=reply_to,
        filtered=filtered,
        error=error,
        tool_calls_made=tool_calls_made,
    )


def _make_mock_registry(
    action_result: object | None = None,
    gate_side_effect: Exception | None = None,
) -> MagicMock:
    """Create a mock ServiceRegistry with all dependencies wired.

    Args:
        action_result: ActionResult to return from gate.submit().
            If None, uses a default successful result.
        gate_side_effect: Exception to raise from gate.submit().
    """
    if action_result is None:
        action_result = _make_action_result()

    mock_registry = MagicMock()

    # PersonResolver mock
    mock_person_resolver = AsyncMock()
    mock_person_resolver.resolve = AsyncMock(return_value="person-123")

    # ConversationTracker mock
    mock_conv_tracker = AsyncMock()
    mock_conv_tracker.get_or_create = AsyncMock(
        return_value=("conv-456", [{"role": "user", "content": "Hi"}]),
    )
    mock_conv_tracker.add_turn = AsyncMock()

    # CogLoopGate mock
    mock_gate = AsyncMock()
    if gate_side_effect is not None:
        mock_gate.submit = AsyncMock(side_effect=gate_side_effect)
    else:
        mock_gate.submit = AsyncMock(return_value=action_result)

    # BridgeManager mock (for mind_id access)
    mock_bridge = MagicMock()
    mock_bridge.mind_id = "aria"

    # Wire resolve() to return the right mock per type
    async def _resolve(interface: type) -> object:
        from sovyx.bridge.identity import PersonResolver
        from sovyx.bridge.manager import BridgeManager
        from sovyx.bridge.sessions import ConversationTracker
        from sovyx.cognitive.gate import CogLoopGate

        mapping: dict[type, object] = {
            PersonResolver: mock_person_resolver,
            ConversationTracker: mock_conv_tracker,
            CogLoopGate: mock_gate,
            BridgeManager: mock_bridge,
        }
        result = mapping.get(interface)
        if result is None:
            msg = f"Service not registered: {interface.__name__}"
            raise Exception(msg)  # noqa: TRY002
        return result

    mock_registry.resolve = AsyncMock(side_effect=_resolve)

    # Attach mocks for direct assertion access
    mock_registry._person_resolver = mock_person_resolver
    mock_registry._conv_tracker = mock_conv_tracker
    mock_registry._gate = mock_gate
    mock_registry._bridge = mock_bridge

    return mock_registry


# ── Auth Tests ──


class TestChatAuth:
    """Authentication requirements for POST /api/chat."""

    def test_requires_auth(self, client: TestClient) -> None:
        """Request without Authorization header returns 401."""
        resp = client.post("/api/chat", json={"message": "Hello"})
        assert resp.status_code == 401

    def test_rejects_invalid_token(self, client: TestClient) -> None:
        """Request with wrong token returns 401."""
        resp = client.post(
            "/api/chat",
            json={"message": "Hello"},
            headers={"Authorization": "Bearer wrong-token-value"},
        )
        assert resp.status_code == 401

    def test_rejects_malformed_auth(self, client: TestClient) -> None:
        """Request with malformed auth header returns 401 or 403."""
        resp = client.post(
            "/api/chat",
            json={"message": "Hello"},
            headers={"Authorization": "Basic dXNlcjpwYXNz"},
        )
        # FastAPI HTTPBearer returns 403 for wrong scheme
        assert resp.status_code in {401, 403}


# ── Input Validation Tests ──


class TestChatValidation:
    """Input validation for POST /api/chat."""

    def test_rejects_empty_body(
        self,
        client: TestClient,
        auth_headers: dict[str, str],
    ) -> None:
        """Empty request body returns 422."""
        resp = client.post(
            "/api/chat",
            content=b"",
            headers={**auth_headers, "Content-Type": "application/json"},
        )
        assert resp.status_code == 422

    def test_rejects_non_json(
        self,
        client: TestClient,
        auth_headers: dict[str, str],
    ) -> None:
        """Non-JSON body returns 422."""
        resp = client.post(
            "/api/chat",
            content=b"not json",
            headers={**auth_headers, "Content-Type": "application/json"},
        )
        assert resp.status_code == 422

    def test_rejects_array_body(
        self,
        client: TestClient,
        auth_headers: dict[str, str],
    ) -> None:
        """Array body instead of object returns 422."""
        resp = client.post("/api/chat", json=["hello"], headers=auth_headers)
        assert resp.status_code == 422
        assert "Expected JSON object" in resp.json()["error"]

    def test_rejects_missing_message(
        self,
        client: TestClient,
        auth_headers: dict[str, str],
    ) -> None:
        """Missing 'message' field returns 422."""
        resp = client.post(
            "/api/chat",
            json={"user_name": "Test"},
            headers=auth_headers,
        )
        assert resp.status_code == 422
        assert "message" in resp.json()["error"].lower()

    def test_rejects_empty_message(
        self,
        client: TestClient,
        auth_headers: dict[str, str],
    ) -> None:
        """Empty string message returns 422."""
        resp = client.post(
            "/api/chat",
            json={"message": ""},
            headers=auth_headers,
        )
        assert resp.status_code == 422

    def test_rejects_whitespace_message(
        self,
        client: TestClient,
        auth_headers: dict[str, str],
    ) -> None:
        """Whitespace-only message returns 422."""
        resp = client.post(
            "/api/chat",
            json={"message": "   \n\t  "},
            headers=auth_headers,
        )
        assert resp.status_code == 422

    def test_rejects_non_string_message(
        self,
        client: TestClient,
        auth_headers: dict[str, str],
    ) -> None:
        """Non-string message (number) returns 422."""
        resp = client.post(
            "/api/chat",
            json={"message": 12345},
            headers=auth_headers,
        )
        assert resp.status_code == 422

    def test_rejects_null_message(
        self,
        client: TestClient,
        auth_headers: dict[str, str],
    ) -> None:
        """Null message returns 422."""
        resp = client.post(
            "/api/chat",
            json={"message": None},
            headers=auth_headers,
        )
        assert resp.status_code == 422

    def test_rejects_non_string_conversation_id(
        self,
        client: TestClient,
        auth_headers: dict[str, str],
    ) -> None:
        """Non-string conversation_id returns 422."""
        resp = client.post(
            "/api/chat",
            json={"message": "Hello", "conversation_id": 123},
            headers=auth_headers,
        )
        assert resp.status_code == 422
        assert "conversation_id" in resp.json()["error"]


# ── Service Unavailable Tests ──


class TestChatServiceUnavailable:
    """Behavior when engine is not running."""

    def test_no_registry_returns_503(
        self,
        client: TestClient,
        auth_headers: dict[str, str],
    ) -> None:
        """Without registry (engine not running), returns 503."""
        resp = client.post(
            "/api/chat",
            json={"message": "Hello"},
            headers=auth_headers,
        )
        assert resp.status_code == 503
        assert "Engine not running" in resp.json()["error"]


# ── Happy Path Tests ──


class TestChatHappyPath:
    """Successful chat message processing."""

    def test_basic_message(
        self,
        client: TestClient,
        auth_headers: dict[str, str],
    ) -> None:
        """Send a message and get a response."""
        mock_registry = _make_mock_registry()
        client.app.state.registry = mock_registry  # type: ignore[union-attr]

        resp = client.post(
            "/api/chat",
            json={"message": "Hello!"},
            headers=auth_headers,
        )

        assert resp.status_code == 200
        data = resp.json()
        assert data["response"] == "Hello from Aria!"
        assert data["conversation_id"] == "conv-456"
        assert data["mind_id"] == "aria"
        assert "timestamp" in data

    def test_response_has_valid_timestamp(
        self,
        client: TestClient,
        auth_headers: dict[str, str],
    ) -> None:
        """Response timestamp is valid ISO 8601."""
        mock_registry = _make_mock_registry()
        client.app.state.registry = mock_registry  # type: ignore[union-attr]

        resp = client.post(
            "/api/chat",
            json={"message": "Hi"},
            headers=auth_headers,
        )
        data = resp.json()
        # Should parse without error
        ts = datetime.fromisoformat(data["timestamp"])
        assert ts.tzinfo is not None  # Must be timezone-aware

    def test_custom_user_name(
        self,
        client: TestClient,
        auth_headers: dict[str, str],
    ) -> None:
        """Custom user_name is passed to PersonResolver."""
        mock_registry = _make_mock_registry()
        client.app.state.registry = mock_registry  # type: ignore[union-attr]

        resp = client.post(
            "/api/chat",
            json={"message": "Hello", "user_name": "Guipe"},
            headers=auth_headers,
        )

        assert resp.status_code == 200
        # Verify PersonResolver was called with the custom name
        call_args = mock_registry._person_resolver.resolve.call_args
        assert call_args[0][2] == "Guipe"

    def test_default_user_name(
        self,
        client: TestClient,
        auth_headers: dict[str, str],
    ) -> None:
        """Default user_name is 'Dashboard' when not specified."""
        mock_registry = _make_mock_registry()
        client.app.state.registry = mock_registry  # type: ignore[union-attr]

        resp = client.post(
            "/api/chat",
            json={"message": "Hello"},
            headers=auth_headers,
        )
        assert resp.status_code == 200

        call_args = mock_registry._person_resolver.resolve.call_args
        assert call_args[0][2] == "Dashboard"

    def test_with_conversation_id(
        self,
        client: TestClient,
        auth_headers: dict[str, str],
    ) -> None:
        """Providing conversation_id continues existing conversation."""
        mock_registry = _make_mock_registry()
        client.app.state.registry = mock_registry  # type: ignore[union-attr]

        resp = client.post(
            "/api/chat",
            json={
                "message": "Follow up",
                "conversation_id": "existing-conv-id",
            },
            headers=auth_headers,
        )

        assert resp.status_code == 200

    def test_null_conversation_id(
        self,
        client: TestClient,
        auth_headers: dict[str, str],
    ) -> None:
        """Null conversation_id auto-creates a new conversation."""
        mock_registry = _make_mock_registry()
        client.app.state.registry = mock_registry  # type: ignore[union-attr]

        resp = client.post(
            "/api/chat",
            json={"message": "First message", "conversation_id": None},
            headers=auth_headers,
        )

        assert resp.status_code == 200
        assert resp.json()["conversation_id"] == "conv-456"

    def test_non_string_user_name_defaults(
        self,
        client: TestClient,
        auth_headers: dict[str, str],
    ) -> None:
        """Non-string user_name falls back to 'Dashboard'."""
        mock_registry = _make_mock_registry()
        client.app.state.registry = mock_registry  # type: ignore[union-attr]

        resp = client.post(
            "/api/chat",
            json={"message": "Hello", "user_name": 42},
            headers=auth_headers,
        )

        assert resp.status_code == 200
        call_args = mock_registry._person_resolver.resolve.call_args
        assert call_args[0][2] == "Dashboard"


# ── Tag Derivation Tests ──


class TestChatTags:
    """``tags`` field on the /api/chat response.

    Every assistant response must carry at least one tag so the chat UI
    can always show the user which modules produced the reply. Plugin
    tags are derived from ReAct tool names (``"plugin.tool"`` →
    ``"plugin"``), deduplicated, sorted, and followed by ``"brain"``
    to signal that the cognitive loop always participated.
    """

    def test_no_tools_tags_brain_only(
        self,
        client: TestClient,
        auth_headers: dict[str, str],
    ) -> None:
        """Pure cognitive reply (no tool calls) surfaces ["brain"]."""
        mock_registry = _make_mock_registry(_make_action_result())
        client.app.state.registry = mock_registry  # type: ignore[union-attr]

        resp = client.post(
            "/api/chat",
            json={"message": "Hi"},
            headers=auth_headers,
        )
        assert resp.status_code == 200
        assert resp.json()["tags"] == ["brain"]

    def test_tool_call_adds_plugin_tag(
        self,
        client: TestClient,
        auth_headers: dict[str, str],
    ) -> None:
        """A single tool call adds its plugin tag before ``brain``."""
        mock_registry = _make_mock_registry(
            _make_action_result(tool_names=["financial_math.compute_interest"]),
        )
        client.app.state.registry = mock_registry  # type: ignore[union-attr]

        resp = client.post(
            "/api/chat",
            json={"message": "What's 5% on $1000 for 3 years?"},
            headers=auth_headers,
        )
        assert resp.status_code == 200
        assert resp.json()["tags"] == ["financial_math", "brain"]

    def test_multiple_plugins_dedup_and_sort(
        self,
        client: TestClient,
        auth_headers: dict[str, str],
    ) -> None:
        """Plugin names are deduplicated and sorted alphabetically."""
        mock_registry = _make_mock_registry(
            _make_action_result(
                tool_names=[
                    "weather.current",
                    "financial_math.compute_interest",
                    "financial_math.format_currency",  # same plugin twice
                ],
            ),
        )
        client.app.state.registry = mock_registry  # type: ignore[union-attr]

        resp = client.post(
            "/api/chat",
            json={"message": "Compare cost and weather"},
            headers=auth_headers,
        )
        assert resp.status_code == 200
        # financial_math < weather (alphabetical), brain last, no dupes.
        assert resp.json()["tags"] == ["financial_math", "weather", "brain"]

    def test_malformed_tool_name_no_crash(
        self,
        client: TestClient,
        auth_headers: dict[str, str],
    ) -> None:
        """A tool name without a dot is treated as the whole plugin name."""
        mock_registry = _make_mock_registry(
            _make_action_result(tool_names=["orphan_tool"]),
        )
        client.app.state.registry = mock_registry  # type: ignore[union-attr]

        resp = client.post(
            "/api/chat",
            json={"message": "Legacy tool"},
            headers=auth_headers,
        )
        assert resp.status_code == 200
        # split(".", 1)[0] on "orphan_tool" returns "orphan_tool" itself.
        assert resp.json()["tags"] == ["orphan_tool", "brain"]


# ── Turn Recording Tests ──


class TestChatTurnRecording:
    """Verify conversation turns are recorded correctly."""

    def test_records_user_and_assistant_turns(
        self,
        client: TestClient,
        auth_headers: dict[str, str],
    ) -> None:
        """Both user message and AI response are recorded as turns."""
        mock_registry = _make_mock_registry()
        client.app.state.registry = mock_registry  # type: ignore[union-attr]

        client.post(
            "/api/chat",
            json={"message": "Hello!"},
            headers=auth_headers,
        )

        tracker = mock_registry._conv_tracker
        assert tracker.add_turn.call_count == 2

        # First call: user turn
        first_call = tracker.add_turn.call_args_list[0]
        assert first_call[0][1] == "user"
        assert first_call[0][2] == "Hello!"

        # Second call: assistant turn
        second_call = tracker.add_turn.call_args_list[1]
        assert second_call[0][1] == "assistant"
        assert second_call[0][2] == "Hello from Aria!"

    def test_filtered_result_no_assistant_turn(
        self,
        client: TestClient,
        auth_headers: dict[str, str],
    ) -> None:
        """Filtered response records only user turn, not assistant."""
        action_result = _make_action_result(
            response_text="",
            filtered=True,
        )
        mock_registry = _make_mock_registry(action_result=action_result)
        client.app.state.registry = mock_registry  # type: ignore[union-attr]

        client.post(
            "/api/chat",
            json={"message": "Bad stuff"},
            headers=auth_headers,
        )

        tracker = mock_registry._conv_tracker
        # Only user turn recorded (filtered = no assistant turn)
        assert tracker.add_turn.call_count == 1
        assert tracker.add_turn.call_args_list[0][0][1] == "user"


# ── Error Handling Tests ──


class TestChatErrorHandling:
    """Error scenarios and graceful degradation."""

    def test_cognitive_timeout(
        self,
        client: TestClient,
        auth_headers: dict[str, str],
    ) -> None:
        """Cognitive loop timeout returns 500 with friendly message."""
        from sovyx.engine.errors import CognitiveError

        mock_registry = _make_mock_registry(
            gate_side_effect=CognitiveError("Timed out after 30s"),
        )
        client.app.state.registry = mock_registry  # type: ignore[union-attr]

        resp = client.post(
            "/api/chat",
            json={"message": "Hello"},
            headers=auth_headers,
        )

        assert resp.status_code == 500
        assert "Failed to process" in resp.json()["error"]

    def test_error_result_still_returns_response(
        self,
        client: TestClient,
        auth_headers: dict[str, str],
    ) -> None:
        """ActionResult with error=True still returns response text."""
        action_result = _make_action_result(
            response_text="I had trouble processing that.",
            error=True,
        )
        mock_registry = _make_mock_registry(action_result=action_result)
        client.app.state.registry = mock_registry  # type: ignore[union-attr]

        resp = client.post(
            "/api/chat",
            json={"message": "Complex query"},
            headers=auth_headers,
        )

        assert resp.status_code == 200
        data = resp.json()
        assert data["response"] == "I had trouble processing that."

    def test_unexpected_exception_returns_500(
        self,
        client: TestClient,
        auth_headers: dict[str, str],
    ) -> None:
        """Unexpected exception in the pipeline returns 500."""
        mock_registry = _make_mock_registry(
            gate_side_effect=RuntimeError("Unexpected failure"),
        )
        client.app.state.registry = mock_registry  # type: ignore[union-attr]

        resp = client.post(
            "/api/chat",
            json={"message": "Hello"},
            headers=auth_headers,
        )

        assert resp.status_code == 500
        assert "Failed to process" in resp.json()["error"]


# ── Edge Cases ──


class TestChatEdgeCases:
    """Edge cases and boundary conditions."""

    def test_long_message(
        self,
        client: TestClient,
        auth_headers: dict[str, str],
    ) -> None:
        """Very long message is accepted and processed."""
        mock_registry = _make_mock_registry()
        client.app.state.registry = mock_registry  # type: ignore[union-attr]

        long_msg = "A" * 5000
        resp = client.post(
            "/api/chat",
            json={"message": long_msg},
            headers=auth_headers,
        )
        assert resp.status_code == 200

    def test_unicode_message(
        self,
        client: TestClient,
        auth_headers: dict[str, str],
    ) -> None:
        """Unicode characters (emoji, CJK, arabic) are handled."""
        mock_registry = _make_mock_registry()
        client.app.state.registry = mock_registry  # type: ignore[union-attr]

        resp = client.post(
            "/api/chat",
            json={"message": "Olá 🧠 你好 مرحبا"},
            headers=auth_headers,
        )
        assert resp.status_code == 200

    def test_message_with_only_whitespace_chars_rejected(
        self,
        client: TestClient,
        auth_headers: dict[str, str],
    ) -> None:
        """Message with tabs and newlines only is rejected."""
        resp = client.post(
            "/api/chat",
            json={"message": "\t\n\r  "},
            headers=auth_headers,
        )
        assert resp.status_code == 422

    def test_extra_fields_ignored(
        self,
        client: TestClient,
        auth_headers: dict[str, str],
    ) -> None:
        """Extra fields in request body are silently ignored."""
        mock_registry = _make_mock_registry()
        client.app.state.registry = mock_registry  # type: ignore[union-attr]

        resp = client.post(
            "/api/chat",
            json={
                "message": "Hello",
                "extra_field": "ignored",
                "another": 123,
            },
            headers=auth_headers,
        )
        assert resp.status_code == 200


# ── Channel Type Tests ──


class TestDashboardChannelType:
    """Verify ChannelType.DASHBOARD is properly used."""

    def test_channel_type_exists(self) -> None:
        """ChannelType.DASHBOARD is defined in the enum."""
        from sovyx.engine.types import ChannelType

        assert hasattr(ChannelType, "DASHBOARD")
        assert ChannelType.DASHBOARD.value == "dashboard"

    def test_channel_type_used_in_resolve(
        self,
        client: TestClient,
        auth_headers: dict[str, str],
    ) -> None:
        """PersonResolver is called with ChannelType.DASHBOARD."""
        from sovyx.engine.types import ChannelType

        mock_registry = _make_mock_registry()
        client.app.state.registry = mock_registry  # type: ignore[union-attr]

        client.post(
            "/api/chat",
            json={"message": "Hello"},
            headers=auth_headers,
        )

        call_args = mock_registry._person_resolver.resolve.call_args
        assert call_args[0][0] == ChannelType.DASHBOARD

    def test_perception_source_is_dashboard(
        self,
        client: TestClient,
        auth_headers: dict[str, str],
    ) -> None:
        """Perception source field is 'dashboard'."""
        mock_registry = _make_mock_registry()
        client.app.state.registry = mock_registry  # type: ignore[union-attr]

        client.post(
            "/api/chat",
            json={"message": "Hello"},
            headers=auth_headers,
        )

        # Check the CognitiveRequest submitted to the gate
        gate_call = mock_registry._gate.submit.call_args
        request = gate_call[0][0]
        assert request.perception.source == "dashboard"

    def test_all_channel_types_unique(self) -> None:
        """All ChannelType values are unique (no duplication)."""
        from sovyx.engine.types import ChannelType

        values = [ct.value for ct in ChannelType]
        assert len(values) == len(set(values))


# ── WebSocket Broadcast Tests ──


class TestChatWebSocketBroadcast:
    """Verify WebSocket events are broadcast on chat messages."""

    def test_broadcast_called_on_success(
        self,
        client: TestClient,
        auth_headers: dict[str, str],
    ) -> None:
        """Successful chat triggers ChatMessage WebSocket broadcast."""
        mock_registry = _make_mock_registry()
        client.app.state.registry = mock_registry  # type: ignore[union-attr]

        # Mock the ws_manager.broadcast
        broadcast_calls: list[dict[str, Any]] = []

        async def _capture_broadcast(
            message: dict[str, Any],
        ) -> None:
            broadcast_calls.append(message)

        original = client.app.state.ws_manager.broadcast  # type: ignore[union-attr]
        client.app.state.ws_manager.broadcast = _capture_broadcast  # type: ignore[union-attr]

        try:
            resp = client.post(
                "/api/chat",
                json={"message": "Hello"},
                headers=auth_headers,
            )
            assert resp.status_code == 200

            assert len(broadcast_calls) == 1
            event = broadcast_calls[0]
            assert event["type"] == "ChatMessage"
            assert event["data"]["conversation_id"] == "conv-456"
            assert "response_preview" in event["data"]
        finally:
            client.app.state.ws_manager.broadcast = original  # type: ignore[union-attr]


# ── Unit Tests for chat module ──


class TestHandleChatMessageUnit:
    """Unit tests for handle_chat_message function directly."""

    @pytest.mark.asyncio()
    async def test_empty_message_raises_value_error(self) -> None:
        """Empty message raises ValueError."""
        from sovyx.dashboard.chat import handle_chat_message

        mock_registry = _make_mock_registry()
        with pytest.raises(ValueError, match="empty"):
            await handle_chat_message(mock_registry, message="")

    @pytest.mark.asyncio()
    async def test_whitespace_message_raises_value_error(self) -> None:
        """Whitespace-only message raises ValueError."""
        from sovyx.dashboard.chat import handle_chat_message

        mock_registry = _make_mock_registry()
        with pytest.raises(ValueError, match="empty"):
            await handle_chat_message(mock_registry, message="   ")

    @pytest.mark.asyncio()
    async def test_returns_correct_structure(self) -> None:
        """Return dict has all required keys."""
        from sovyx.dashboard.chat import handle_chat_message

        mock_registry = _make_mock_registry()
        result = await handle_chat_message(
            mock_registry,
            message="Hello",
        )

        assert "response" in result
        assert "conversation_id" in result
        assert "mind_id" in result
        assert "timestamp" in result

    @pytest.mark.asyncio()
    async def test_message_stripped(self) -> None:
        """Leading/trailing whitespace is stripped from message."""
        from sovyx.dashboard.chat import handle_chat_message

        mock_registry = _make_mock_registry()
        await handle_chat_message(
            mock_registry,
            message="  Hello  ",
        )

        # The perception content should be stripped
        gate_call = mock_registry._gate.submit.call_args
        request = gate_call[0][0]
        assert request.perception.content == "Hello"

    @pytest.mark.asyncio()
    async def test_cognitive_error_propagates(self) -> None:
        """CognitiveError from gate.submit propagates to caller."""
        from sovyx.dashboard.chat import handle_chat_message
        from sovyx.engine.errors import CognitiveError

        mock_registry = _make_mock_registry(
            gate_side_effect=CognitiveError("Queue full"),
        )
        with pytest.raises(CognitiveError):
            await handle_chat_message(
                mock_registry,
                message="Hello",
            )

    @pytest.mark.asyncio()
    async def test_custom_timeout(self) -> None:
        """Custom timeout is passed to gate.submit."""
        from sovyx.dashboard.chat import handle_chat_message

        mock_registry = _make_mock_registry()
        await handle_chat_message(
            mock_registry,
            message="Hello",
            timeout=60.0,
        )

        gate_call = mock_registry._gate.submit.call_args
        assert gate_call[1]["timeout"] == 60.0
