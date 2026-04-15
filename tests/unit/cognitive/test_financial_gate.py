"""Tests for sovyx.cognitive.financial_gate — financial tool call confirmation.

Covers:
- Financial tool call detection (name patterns + argument keys)
- Read-only exclusion (calculate, estimate, get, check, etc.)
- Confirmation flow: pending → confirm/cancel/expire
- User response detection (yes/no in EN and PT-BR)
- Financial confirmation disabled → zero overhead
- False positive checks: non-financial tools pass
- Dynamic config update
"""

from __future__ import annotations

import time

from sovyx.cognitive.financial_gate import (
    FinancialGate,
    FinancialGateState,
    PendingConfirmation,
    is_cancellation,
    is_confirmation,
)
from sovyx.llm.models import ToolCall
from sovyx.mind.config import SafetyConfig


def _tool(
    name: str = "send_payment",
    args: dict[str, object] | None = None,
    call_id: str = "tc-1",
) -> ToolCall:
    return ToolCall(
        id=call_id,
        function_name=name,
        arguments=args or {},
    )


# ── Financial classification ──────────────────────────────────────────


class TestFinancialClassification:
    """Tool calls are correctly classified as financial or not."""

    def test_send_payment_is_financial(self) -> None:
        gate = FinancialGate(SafetyConfig(financial_confirmation=True))
        result = gate.check_tool_call(_tool("send_payment"))
        assert result is not None

    def test_transfer_funds_is_financial(self) -> None:
        gate = FinancialGate(SafetyConfig(financial_confirmation=True))
        result = gate.check_tool_call(_tool("transfer_funds"))
        assert result is not None

    def test_buy_is_financial(self) -> None:
        gate = FinancialGate(SafetyConfig(financial_confirmation=True))
        result = gate.check_tool_call(_tool("buy"))
        assert result is not None

    def test_sell_is_financial(self) -> None:
        gate = FinancialGate(SafetyConfig(financial_confirmation=True))
        result = gate.check_tool_call(_tool("sell"))
        assert result is not None

    def test_trade_crypto_is_financial(self) -> None:
        gate = FinancialGate(SafetyConfig(financial_confirmation=True))
        result = gate.check_tool_call(_tool("trade_crypto"))
        assert result is not None

    def test_withdraw_is_financial(self) -> None:
        gate = FinancialGate(SafetyConfig(financial_confirmation=True))
        result = gate.check_tool_call(_tool("withdraw"))
        assert result is not None

    def test_invest_is_financial(self) -> None:
        gate = FinancialGate(SafetyConfig(financial_confirmation=True))
        result = gate.check_tool_call(_tool("invest"))
        assert result is not None

    def test_place_order_is_financial(self) -> None:
        gate = FinancialGate(SafetyConfig(financial_confirmation=True))
        result = gate.check_tool_call(_tool("place_order"))
        assert result is not None

    def test_cancel_is_financial(self) -> None:
        gate = FinancialGate(SafetyConfig(financial_confirmation=True))
        result = gate.check_tool_call(_tool("cancel"))
        assert result is not None

    def test_subscribe_plan_is_financial(self) -> None:
        gate = FinancialGate(SafetyConfig(financial_confirmation=True))
        result = gate.check_tool_call(_tool("subscribe_plan"))
        assert result is not None

    def test_approve_transaction_is_financial(self) -> None:
        gate = FinancialGate(SafetyConfig(financial_confirmation=True))
        result = gate.check_tool_call(_tool("approve_transaction"))
        assert result is not None


class TestReadOnlyExclusion:
    """Read-only tools are NOT classified as financial."""

    def test_calculate_cost_not_financial(self) -> None:
        gate = FinancialGate(SafetyConfig(financial_confirmation=True))
        result = gate.check_tool_call(_tool("calculate_cost"))
        assert result is None

    def test_get_balance_not_financial(self) -> None:
        gate = FinancialGate(SafetyConfig(financial_confirmation=True))
        result = gate.check_tool_call(_tool("get_balance"))
        assert result is None

    def test_check_price_not_financial(self) -> None:
        gate = FinancialGate(SafetyConfig(financial_confirmation=True))
        result = gate.check_tool_call(_tool("check_price"))
        assert result is None

    def test_list_transactions_not_financial(self) -> None:
        gate = FinancialGate(SafetyConfig(financial_confirmation=True))
        result = gate.check_tool_call(_tool("list_transactions"))
        assert result is None

    def test_estimate_fee_not_financial(self) -> None:
        gate = FinancialGate(SafetyConfig(financial_confirmation=True))
        result = gate.check_tool_call(_tool("estimate_fee"))
        assert result is None

    def test_view_account_not_financial(self) -> None:
        gate = FinancialGate(SafetyConfig(financial_confirmation=True))
        result = gate.check_tool_call(_tool("view_account"))
        assert result is None

    def test_simulate_trade_not_financial(self) -> None:
        gate = FinancialGate(SafetyConfig(financial_confirmation=True))
        result = gate.check_tool_call(_tool("simulate_trade"))
        assert result is None


class TestNonFinancialTools:
    """Non-financial tools pass through."""

    def test_search_web_not_financial(self) -> None:
        gate = FinancialGate(SafetyConfig(financial_confirmation=True))
        result = gate.check_tool_call(_tool("search_web"))
        assert result is None

    def test_send_message_not_financial(self) -> None:
        gate = FinancialGate(SafetyConfig(financial_confirmation=True))
        result = gate.check_tool_call(_tool("send_message"))
        assert result is None

    def test_set_reminder_not_financial(self) -> None:
        gate = FinancialGate(SafetyConfig(financial_confirmation=True))
        result = gate.check_tool_call(_tool("set_reminder"))
        assert result is None


class TestArgumentBasedClassification:
    """Tools classified by argument keys when name doesn't match."""

    def test_generic_tool_with_financial_args(self) -> None:
        gate = FinancialGate(SafetyConfig(financial_confirmation=True))
        tc = _tool(
            "process_action",
            {"amount": 100, "currency": "USD", "recipient": "alice"},
        )
        result = gate.check_tool_call(tc)
        assert result is not None

    def test_generic_tool_with_one_arg_not_financial(self) -> None:
        """One financial arg key is not enough (need ≥2)."""
        gate = FinancialGate(SafetyConfig(financial_confirmation=True))
        tc = _tool("process_action", {"amount": 100})
        result = gate.check_tool_call(tc)
        assert result is None

    def test_generic_tool_with_two_args_is_financial(self) -> None:
        gate = FinancialGate(SafetyConfig(financial_confirmation=True))
        tc = _tool("process_action", {"amount": 100, "recipient": "bob"})
        result = gate.check_tool_call(tc)
        assert result is not None


# ── Confirmation disabled ─────────────────────────────────────────────


class TestConfirmationDisabled:
    """financial_confirmation=False → zero overhead."""

    def test_financial_tool_passes_when_disabled(self) -> None:
        gate = FinancialGate(SafetyConfig(financial_confirmation=False))
        result = gate.check_tool_call(_tool("send_payment", {"amount": 1000}))
        assert result is None

    def test_has_pending_false_when_disabled(self) -> None:
        gate = FinancialGate(SafetyConfig(financial_confirmation=False))
        assert not gate.has_pending()


# ── Confirmation flow ─────────────────────────────────────────────────


class TestConfirmationFlow:
    """Full confirmation lifecycle: gate → confirm/cancel/expire."""

    def test_gate_creates_pending(self) -> None:
        gate = FinancialGate(SafetyConfig(financial_confirmation=True))
        result = gate.check_tool_call(_tool("send_payment", {"amount": 500}))
        assert result is not None
        assert gate.has_pending()
        assert result.summary
        assert "Send Payment" in result.summary

    def test_confirm_resolves_pending(self) -> None:
        gate = FinancialGate(SafetyConfig(financial_confirmation=True))
        gate.check_tool_call(_tool("send_payment"))

        action, confirmed = gate.handle_user_response("yes")
        assert action == "confirmed"
        assert confirmed is not None
        assert not gate.has_pending()

    def test_cancel_resolves_pending(self) -> None:
        gate = FinancialGate(SafetyConfig(financial_confirmation=True))
        gate.check_tool_call(_tool("send_payment"))

        action, pending = gate.handle_user_response("no")
        assert action == "cancelled"
        assert pending is not None
        assert not gate.has_pending()

    def test_unrelated_message_doesnt_consume(self) -> None:
        gate = FinancialGate(SafetyConfig(financial_confirmation=True))
        gate.check_tool_call(_tool("send_payment"))

        action, _ = gate.handle_user_response("what's the weather?")
        assert action == "none"
        assert gate.has_pending()  # Still pending

    def test_no_pending_returns_none(self) -> None:
        gate = FinancialGate(SafetyConfig(financial_confirmation=True))
        action, result = gate.handle_user_response("yes")
        assert action == "none"
        assert result is None

    def test_summary_includes_amount(self) -> None:
        gate = FinancialGate(SafetyConfig(financial_confirmation=True))
        result = gate.check_tool_call(
            _tool("send_payment", {"amount": 1000, "currency": "USD"}),
        )
        assert result is not None
        assert "1000" in result.summary
        assert "USD" in result.summary

    def test_summary_includes_recipient(self) -> None:
        gate = FinancialGate(SafetyConfig(financial_confirmation=True))
        result = gate.check_tool_call(
            _tool("transfer_funds", {"amount": 50, "recipient": "alice@example.com"}),
        )
        assert result is not None
        assert "alice@example.com" in result.summary


# ── Expiration ────────────────────────────────────────────────────────


class TestExpiration:
    """Pending confirmations expire after timeout."""

    def test_expired_confirmation_cleared(self) -> None:
        gate = FinancialGate(SafetyConfig(financial_confirmation=True))
        tc = _tool("send_payment")
        # Create with a past timestamp
        confirmation = PendingConfirmation(
            tool_call=tc,
            summary="test",
            created_at=time.monotonic() - 400,  # 400s ago > 300s timeout
            timeout_seconds=300,
        )
        gate.state.add(confirmation)

        # Expired confirmation should be cleaned up
        assert gate.state.get_pending() is None
        assert not gate.has_pending()

    def test_not_yet_expired(self) -> None:
        gate = FinancialGate(SafetyConfig(financial_confirmation=True))
        gate.check_tool_call(_tool("send_payment"))
        assert gate.has_pending()  # Just created, not expired


# ── User response detection ───────────────────────────────────────────


class TestUserResponseDetection:
    """Confirmation/cancellation pattern matching."""

    def test_english_confirmations(self) -> None:
        for text in ["yes", "Yes", "YES", "yep", "sure", "ok", "confirm", "go ahead", "do it"]:
            assert is_confirmation(text), f"Should confirm: {text}"

    def test_portuguese_confirmations(self) -> None:
        for text in ["sim", "confirma", "pode", "manda", "vai", "beleza", "bora"]:
            assert is_confirmation(text), f"Should confirm: {text}"

    def test_english_cancellations(self) -> None:
        for text in ["no", "No", "nope", "cancel", "stop", "abort", "don't"]:
            assert is_cancellation(text), f"Should cancel: {text}"

    def test_portuguese_cancellations(self) -> None:
        for text in ["não", "nao", "cancela", "para", "aborta"]:
            assert is_cancellation(text), f"Should cancel: {text}"

    def test_regular_text_not_confirmation(self) -> None:
        assert not is_confirmation("what's the weather?")
        assert not is_confirmation("tell me about python")

    def test_regular_text_not_cancellation(self) -> None:
        assert not is_cancellation("what's the weather?")
        assert not is_cancellation("tell me about python")


# ── Dynamic config ────────────────────────────────────────────────────


class TestDynamicConfig:
    """Config changes take effect dynamically."""

    def test_enable_at_runtime(self) -> None:
        cfg = SafetyConfig(financial_confirmation=False)
        gate = FinancialGate(cfg)

        assert gate.check_tool_call(_tool("send_payment")) is None

        cfg.financial_confirmation = True
        result = gate.check_tool_call(_tool("send_payment"))
        assert result is not None

    def test_disable_at_runtime(self) -> None:
        cfg = SafetyConfig(financial_confirmation=True)
        gate = FinancialGate(cfg)

        result = gate.check_tool_call(_tool("send_payment"))
        assert result is not None

        cfg.financial_confirmation = False
        result = gate.check_tool_call(
            _tool("buy", call_id="tc-2"),
        )
        assert result is None


# ── Gate state ────────────────────────────────────────────────────────


class TestGateState:
    """FinancialGateState operations."""

    def test_cancel_all(self) -> None:
        state = FinancialGateState()
        tc1 = _tool("buy", call_id="tc-1")
        tc2 = _tool("sell", call_id="tc-2")
        state.add(PendingConfirmation(tc1, "buy", time.monotonic()))
        state.add(PendingConfirmation(tc2, "sell", time.monotonic()))

        count = state.cancel_all()
        assert count == 2
        assert state.get_pending() is None

    def test_confirm_returns_confirmation(self) -> None:
        state = FinancialGateState()
        tc = _tool("buy", call_id="tc-1")
        pending = PendingConfirmation(tc, "buy", time.monotonic())
        state.add(pending)

        result = state.confirm("tc-1")
        assert result is pending
        assert state.get_pending() is None

    def test_confirm_nonexistent_returns_none(self) -> None:
        state = FinancialGateState()
        assert state.confirm("nonexistent") is None


# ── TASK-345: LLM Intent Classification ──


class TestClassifyIntent:
    """classify_intent (regex synchronous fallback)."""

    def test_confirm_english(self) -> None:
        from sovyx.cognitive.financial_gate import classify_intent

        assert classify_intent("yes") == "confirmed"
        assert classify_intent("Sure!") == "confirmed"
        assert classify_intent("go ahead") == "confirmed"

    def test_confirm_portuguese(self) -> None:
        from sovyx.cognitive.financial_gate import classify_intent

        assert classify_intent("sim") == "confirmed"
        assert classify_intent("confirma") == "confirmed"
        assert classify_intent("bora") == "confirmed"

    def test_cancel_english(self) -> None:
        from sovyx.cognitive.financial_gate import classify_intent

        assert classify_intent("no") == "cancelled"
        assert classify_intent("cancel") == "cancelled"
        assert classify_intent("abort") == "cancelled"

    def test_cancel_portuguese(self) -> None:
        from sovyx.cognitive.financial_gate import classify_intent

        assert classify_intent("não") == "cancelled"
        assert classify_intent("cancela") == "cancelled"

    def test_unclear(self) -> None:
        from sovyx.cognitive.financial_gate import classify_intent

        assert classify_intent("maybe") == "unclear"
        assert classify_intent("oui") == "unclear"  # French not supported
        assert classify_intent("ja") == "unclear"  # German not supported
        assert classify_intent("はい") == "unclear"  # Japanese not supported


class TestClassifyIntentLLM:
    """classify_intent_llm (async, language-agnostic)."""

    async def test_confirm_via_llm(self) -> None:
        from unittest.mock import AsyncMock, MagicMock

        from sovyx.cognitive.financial_gate import classify_intent_llm

        router = MagicMock()
        response = MagicMock()
        response.content = "CONFIRM"
        router.generate = AsyncMock(return_value=response)

        result = await classify_intent_llm("oui", router)
        assert result == "confirmed"

    async def test_cancel_via_llm(self) -> None:
        from unittest.mock import AsyncMock, MagicMock

        from sovyx.cognitive.financial_gate import classify_intent_llm

        router = MagicMock()
        response = MagicMock()
        response.content = "CANCEL"
        router.generate = AsyncMock(return_value=response)

        result = await classify_intent_llm("nein", router)
        assert result == "cancelled"

    async def test_unclear_via_llm(self) -> None:
        from unittest.mock import AsyncMock, MagicMock

        from sovyx.cognitive.financial_gate import classify_intent_llm

        router = MagicMock()
        response = MagicMock()
        response.content = "UNCLEAR"
        router.generate = AsyncMock(return_value=response)

        result = await classify_intent_llm("asdfgh", router)
        assert result == "unclear"

    async def test_llm_failure_returns_unclear(self) -> None:
        """Typed LLM failure → fall back to 'unclear'.

        Uses ``LLMError`` because that's what the production router
        raises for provider/transport problems; the narrow catch in
        ``classify_intent_llm`` covers it. A bare ``RuntimeError``
        would represent a programmer error that SHOULD bubble up,
        not a recognised LLM failure.
        """
        from unittest.mock import AsyncMock, MagicMock

        from sovyx.cognitive.financial_gate import classify_intent_llm
        from sovyx.engine.errors import LLMError

        router = MagicMock()
        router.generate = AsyncMock(side_effect=LLMError("LLM down"))

        result = await classify_intent_llm("yes", router)
        assert result == "unclear"

    async def test_temperature_zero(self) -> None:
        from unittest.mock import AsyncMock, MagicMock

        from sovyx.cognitive.financial_gate import classify_intent_llm

        router = MagicMock()
        response = MagicMock()
        response.content = "CONFIRM"
        router.generate = AsyncMock(return_value=response)

        await classify_intent_llm("si", router)
        _, kwargs = router.generate.call_args
        assert kwargs["temperature"] == 0.0
        assert kwargs["max_tokens"] == 10


class TestHandleUserResponseAsync:
    """handle_user_response_async — cascading classification."""

    def _make_gate(self) -> FinancialGate:
        from sovyx.mind.config import SafetyConfig

        safety = SafetyConfig(financial_confirmation=True)
        gate = FinancialGate(safety_config=safety)
        return gate

    def _add_pending(self, gate: FinancialGate) -> None:
        from sovyx.llm.models import ToolCall

        tc = ToolCall(
            id="tc_1",
            function_name="send_payment",
            arguments={"amount": "100", "currency": "USD"},
        )
        gate.check_tool_call(tc)

    async def test_regex_match_skips_llm(self) -> None:
        gate = self._make_gate()
        self._add_pending(gate)

        result, pending = await gate.handle_user_response_async("yes")
        assert result == "confirmed"
        assert pending is not None

    async def test_llm_used_when_regex_unclear(self) -> None:
        from unittest.mock import AsyncMock, MagicMock

        gate = self._make_gate()
        self._add_pending(gate)

        router = MagicMock()
        response = MagicMock()
        response.content = "CONFIRM"
        router.generate = AsyncMock(return_value=response)

        result, pending = await gate.handle_user_response_async("oui", llm_router=router)
        assert result == "confirmed"
        router.generate.assert_called_once()

    async def test_no_llm_and_regex_unclear(self) -> None:
        gate = self._make_gate()
        self._add_pending(gate)

        result, pending = await gate.handle_user_response_async("oui")
        assert result == "unclear"
        assert pending is not None  # NOT consumed

    async def test_no_pending_returns_none(self) -> None:
        gate = self._make_gate()

        result, pending = await gate.handle_user_response_async("yes")
        assert result == "none"
        assert pending is None

    async def test_cancel_via_llm(self) -> None:
        from unittest.mock import AsyncMock, MagicMock

        gate = self._make_gate()
        self._add_pending(gate)

        router = MagicMock()
        response = MagicMock()
        response.content = "CANCEL"
        router.generate = AsyncMock(return_value=response)

        result, pending = await gate.handle_user_response_async("nein", llm_router=router)
        assert result == "cancelled"
        assert pending is not None
