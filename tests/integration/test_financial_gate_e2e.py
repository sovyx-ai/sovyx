"""E2E: Financial gate confirmation flow (TASK-347).

Tests the full pipeline for financial confirmation:
  Tool call → FinancialGate → ActionResult with buttons →
  BridgeManager sends with buttons → callback_data →
  BridgeManager intercepts → resolves confirmation → edits message

10 scenarios covering buttons, LLM fallback, regex, edge cases.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from sovyx.bridge.manager import BridgeManager
from sovyx.bridge.protocol import InboundMessage, InlineButton, OutboundMessage
from sovyx.cognitive.act import ActionResult
from sovyx.cognitive.financial_gate import (
    FinancialGate,
    classify_intent,
    classify_intent_llm,
)
from sovyx.engine.events import EventBus
from sovyx.engine.types import ChannelType, MindId
from sovyx.llm.models import LLMResponse, ToolCall
from sovyx.mind.config import SafetyConfig

# ── Helpers ──


def _make_financial_gate(*, enabled: bool = True) -> FinancialGate:
    safety = SafetyConfig(financial_confirmation=enabled)
    return FinancialGate(safety_config=safety)


def _make_tool_call(
    name: str = "send_payment",
    args: dict[str, str] | None = None,
) -> ToolCall:
    return ToolCall(
        id="tc_001",
        function_name=name,
        arguments=args or {"amount": "500", "currency": "USDT"},
    )


def _make_llm_response(*, tool_calls: list[ToolCall] | None = None) -> LLMResponse:
    return LLMResponse(
        content="",
        model="test",
        tokens_in=10,
        tokens_out=5,
        latency_ms=100,
        cost_usd=0.001,
        finish_reason="tool_use" if tool_calls else "stop",
        provider="test",
        tool_calls=tool_calls or [],
    )


def _make_bridge_manager(
    *,
    financial_gate: FinancialGate | None = None,
    adapter: object | None = None,
) -> BridgeManager:
    """Create minimal BridgeManager for testing."""
    manager = BridgeManager.__new__(BridgeManager)
    manager._events = EventBus()
    manager._gate = AsyncMock()
    manager._resolver = AsyncMock()
    manager._tracker = AsyncMock()
    manager._mind_id = MindId("test-mind")
    manager._financial_gate = financial_gate
    manager._adapters = {}
    from sovyx.bridge.manager import _LRULockDict

    manager._conv_locks = _LRULockDict()
    manager._pending_confirmations = {}
    if adapter:
        manager._adapters[ChannelType.TELEGRAM] = adapter  # type: ignore[assignment]
    return manager


def _make_inbound(
    *,
    text: str = "hello",
    callback_data: str | None = None,
    callback_message_id: str | None = None,
    chat_id: str = "chat_123",
) -> InboundMessage:
    metadata: dict[str, object] = {}
    if callback_data:
        metadata["callback_data"] = callback_data
    if callback_message_id:
        metadata["callback_message_id"] = callback_message_id
    return InboundMessage(
        channel_type=ChannelType.TELEGRAM,
        channel_user_id="user_1",
        channel_message_id="msg_1",
        chat_id=chat_id,
        text=text,
        metadata=metadata,
    )


# ── Scenario 1: Tool call → button → approve ──


class TestScenario1ButtonApprove:
    """Financial tool call → buttons sent → user clicks ✅ → approved."""

    async def test_full_flow(self) -> None:
        gate = _make_financial_gate()
        adapter = AsyncMock()
        adapter.send = AsyncMock(return_value="sent_msg_1")
        adapter.edit = AsyncMock()
        adapter.capabilities = {"send", "inline_buttons", "edit"}
        manager = _make_bridge_manager(financial_gate=gate, adapter=adapter)

        # Step 1: ActPhase generates buttons for financial tool call
        tc = _make_tool_call()
        pending = gate.check_tool_call(tc)
        assert pending is not None

        # Step 2: ActionResult has buttons
        result = ActionResult(
            response_text=f"⚠️ Financial action requires confirmation:\n{pending.summary}",
            target_channel="telegram",
            pending_confirmation=True,
            confirmation_details={"tool_call_id": tc.id},
            buttons=[
                [
                    InlineButton("✅ Approve", f"fin_confirm:{tc.id}"),
                    InlineButton("❌ Deny", f"fin_cancel:{tc.id}"),
                ]
            ],
        )
        assert result.buttons is not None
        assert len(result.buttons[0]) == 2

        # Step 3: Simulate BridgeManager sending and tracking
        outbound = OutboundMessage(
            channel_type=ChannelType.TELEGRAM,
            target="chat_123",
            text=result.response_text,
            buttons=result.buttons,
        )
        sent_id = await manager._send_response(outbound)
        assert sent_id == "sent_msg_1"

        # Track pending
        from sovyx.bridge.manager import _PendingConfirmationCtx

        manager._pending_confirmations["chat_123"] = _PendingConfirmationCtx(
            message_id=sent_id,
            chat_id="chat_123",
            channel_type=ChannelType.TELEGRAM,
            tool_call_ids=[tc.id],
        )

        # Step 4: User clicks ✅ → callback
        callback_msg = _make_inbound(
            callback_data=f"fin_confirm:{tc.id}",
            callback_message_id=sent_id,
        )
        await manager._handle_financial_callback(callback_msg)

        # Step 5: Original message edited, buttons removed
        adapter.edit.assert_called_once()
        edit_args = adapter.edit.call_args
        assert "Approved" in edit_args[0][1] or "approved" in edit_args[0][1].lower()


# ── Scenario 2: Tool call → button → deny ──


class TestScenario2ButtonDeny:
    """Financial tool call → buttons → user clicks ❌ → cancelled."""

    async def test_deny_edits_message(self) -> None:
        gate = _make_financial_gate()
        adapter = AsyncMock()
        adapter.send = AsyncMock(return_value="sent_msg_2")
        adapter.edit = AsyncMock()
        manager = _make_bridge_manager(financial_gate=gate, adapter=adapter)

        tc = _make_tool_call()
        gate.check_tool_call(tc)

        from sovyx.bridge.manager import _PendingConfirmationCtx

        manager._pending_confirmations["chat_123"] = _PendingConfirmationCtx(
            message_id="sent_msg_2",
            chat_id="chat_123",
            channel_type=ChannelType.TELEGRAM,
            tool_call_ids=[tc.id],
        )

        callback_msg = _make_inbound(callback_data=f"fin_cancel:{tc.id}")
        await manager._handle_financial_callback(callback_msg)

        adapter.edit.assert_called_once()
        edit_text = adapter.edit.call_args[0][1]
        assert "cancel" in edit_text.lower() or "❌" in edit_text


# ── Scenario 3: Timeout → expires ──


class TestScenario3Timeout:
    """Pending confirmation expires after timeout."""

    def test_expired_confirmation(self) -> None:
        import time

        gate = _make_financial_gate()
        tc = _make_tool_call()
        pending = gate.check_tool_call(tc)
        assert pending is not None

        # Monkey-patch created_at to simulate timeout
        object.__setattr__(pending, "created_at", time.monotonic() - 400)
        assert pending.expired is True

        # get_pending returns None for expired
        result = gate.state.get_pending()
        assert result is None


# ── Scenario 4: LLM classifies "sí" (Spanish) → approve ──


class TestScenario4LLMSpanish:
    """Canal sem botão → LLM classifies 'sí' in Spanish → confirm."""

    async def test_spanish_confirm(self) -> None:
        router = MagicMock()
        response = MagicMock()
        response.content = "CONFIRM"
        router.generate = AsyncMock(return_value=response)

        result = await classify_intent_llm("sí, confirmo", router)
        assert result == "confirmed"


# ── Scenario 5: LLM classifies "nein" (German) → cancel ──


class TestScenario5LLMGerman:
    """Canal sem botão → LLM classifies 'nein' in German → cancel."""

    async def test_german_cancel(self) -> None:
        router = MagicMock()
        response = MagicMock()
        response.content = "CANCEL"
        router.generate = AsyncMock(return_value=response)

        result = await classify_intent_llm("nein, danke", router)
        assert result == "cancelled"


# ── Scenario 6: LLM offline → regex fallback PT → approve ──


class TestScenario6RegexFallback:
    """LLM offline → regex fallback classifies 'sim' → confirm."""

    async def test_regex_fallback_portuguese(self) -> None:
        gate = _make_financial_gate()
        tc = _make_tool_call()
        gate.check_tool_call(tc)

        # No LLM router → falls back to regex
        result, pending = await gate.handle_user_response_async("sim")
        assert result == "confirmed"
        assert pending is not None


# ── Scenario 7: Read-only tool (get_balance) → NOT intercepted ──


class TestScenario7ReadOnlyNotGated:
    """Read-only tools bypass the financial gate entirely."""

    def test_get_balance_not_financial(self) -> None:
        gate = _make_financial_gate()
        tc = ToolCall(
            id="tc_ro",
            function_name="get_balance",
            arguments={"account": "main"},
        )
        result = gate.check_tool_call(tc)
        assert result is None
        assert not gate.has_pending()

    def test_check_price_not_financial(self) -> None:
        gate = _make_financial_gate()
        tc = ToolCall(
            id="tc_ro2",
            function_name="check_price",
            arguments={"token": "BTC"},
        )
        result = gate.check_tool_call(tc)
        assert result is None

    def test_list_transactions_not_financial(self) -> None:
        gate = _make_financial_gate()
        tc = ToolCall(
            id="tc_ro3",
            function_name="list_transactions",
            arguments={"account": "main", "limit": "10"},
        )
        result = gate.check_tool_call(tc)
        assert result is None


# ── Scenario 8: financial_confirmation=False → gate disabled ──


class TestScenario8GateDisabled:
    """When financial_confirmation=False, gate is complete no-op."""

    def test_no_gating(self) -> None:
        gate = _make_financial_gate(enabled=False)
        tc = _make_tool_call()
        result = gate.check_tool_call(tc)
        assert result is None
        assert not gate.has_pending()

    def test_handle_response_noop(self) -> None:
        gate = _make_financial_gate(enabled=False)
        result, pending = gate.handle_user_response("yes")
        assert result == "none"
        assert pending is None


# ── Scenario 9: child_safe_mode → financial_confirmation forced ──


class TestScenario9ChildSafeForces:
    """child_safe_mode forces financial_confirmation=True via coherence."""

    def test_coherence_enforcement(self) -> None:
        from sovyx.dashboard.config import _apply_safety
        from sovyx.mind.config import MindConfig

        cfg = MindConfig(
            name="Aria",
            safety=SafetyConfig(
                child_safe_mode=True,
                financial_confirmation=False,
            ),
        )
        changes: dict[str, str] = {}
        _apply_safety(cfg, {"child_safe_mode": True}, changes)
        assert cfg.safety.financial_confirmation is True


# ── Scenario 10: Callback with no pending → graceful handling ──


class TestScenario10NoPending:
    """Callback arrives but no pending confirmation exists."""

    async def test_stale_callback(self) -> None:
        adapter = AsyncMock()
        adapter.send = AsyncMock(return_value="msg_x")
        adapter.edit = AsyncMock()
        manager = _make_bridge_manager(adapter=adapter)

        # No pending registered
        callback_msg = _make_inbound(callback_data="fin_confirm:old_tc")
        await manager._handle_financial_callback(callback_msg)

        # Should send new message (no context to edit)
        adapter.send.assert_called_once()
        adapter.edit.assert_not_called()


# ── Scenario 11: Batch — multiple financial tool calls ──


class TestScenario11BatchConfirmation:
    """Multiple financial tool calls → batch buttons."""

    def test_multiple_pending(self) -> None:
        gate = _make_financial_gate()
        tc1 = ToolCall(
            id="tc_a",
            function_name="send_payment",
            arguments={"amount": "100", "currency": "USD"},
        )
        tc2 = ToolCall(
            id="tc_b",
            function_name="execute_payment",
            arguments={"amount": "200", "currency": "EUR"},
        )
        p1 = gate.check_tool_call(tc1)
        p2 = gate.check_tool_call(tc2)
        assert p1 is not None
        assert p2 is not None
        assert len(gate.state.pending) == 2


# ── Scenario 12: classify_intent regex covers PT and EN ──


class TestScenario12RegexCoverage:
    """Regex covers common PT and EN confirmation/cancellation phrases."""

    @pytest.mark.parametrize(
        "text",
        ["yes", "yep", "sure", "ok", "confirm", "go ahead", "do it"],
    )
    def test_english_confirms(self, text: str) -> None:
        assert classify_intent(text) == "confirmed"

    @pytest.mark.parametrize(
        "text",
        ["sim", "confirma", "confirmado", "pode", "manda", "vai", "beleza", "bora"],
    )
    def test_portuguese_confirms(self, text: str) -> None:
        assert classify_intent(text) == "confirmed"

    @pytest.mark.parametrize(
        "text",
        ["no", "nope", "cancel", "deny", "stop", "abort"],
    )
    def test_english_cancels(self, text: str) -> None:
        assert classify_intent(text) == "cancelled"

    @pytest.mark.parametrize(
        "text",
        ["não", "nao", "cancela", "para", "aborta"],
    )
    def test_portuguese_cancels(self, text: str) -> None:
        assert classify_intent(text) == "cancelled"

    @pytest.mark.parametrize(
        "text",
        ["oui", "ja", "sí", "da", "はい", "maybe", "I think so", "perhaps"],
    )
    def test_other_languages_unclear(self, text: str) -> None:
        assert classify_intent(text) == "unclear"
