"""Tests for sovyx.cognitive.act — ActPhase + ToolExecutor."""

from __future__ import annotations

from dataclasses import dataclass
from unittest.mock import AsyncMock, MagicMock

from sovyx.bridge.protocol import InlineButton
from sovyx.cognitive.act import ActionResult, ActPhase, ToolExecutor
from sovyx.cognitive.financial_gate import FinancialGate
from sovyx.cognitive.perceive import Perception
from sovyx.engine.types import PerceptionType
from sovyx.llm.models import LLMResponse, ToolCall
from sovyx.mind.config import SafetyConfig


def _perception(content: str = "Hello") -> Perception:
    return Perception(
        id="p1",
        type=PerceptionType.USER_MESSAGE,
        source="telegram",
        content=content,
        metadata={"reply_to": "msg123"},
    )


def _response(
    content: str = "Hi!",
    finish_reason: str = "stop",
    tool_calls: list[ToolCall] | None = None,
) -> LLMResponse:
    return LLMResponse(
        content=content,
        model="test",
        tokens_in=10,
        tokens_out=5,
        latency_ms=100,
        cost_usd=0.001,
        finish_reason=finish_reason,
        provider="test",
        tool_calls=tool_calls,
    )


class TestToolExecutor:
    """ToolExecutor v0.1."""

    async def test_no_tools_returns_errors(self) -> None:
        executor = ToolExecutor()
        calls = [ToolCall(id="tc1", function_name="search", arguments={"q": "test"})]
        results = await executor.execute(calls)
        assert len(results) == 1
        assert results[0].success is False
        assert "no tools" in results[0].output

    async def test_empty_calls(self) -> None:
        executor = ToolExecutor()
        results = await executor.execute([])
        assert results == []

    def test_register_tool(self) -> None:
        executor = ToolExecutor()
        executor.register_tool("test", lambda: None)
        assert "test" in executor._tools


class TestActPhase:
    """ActPhase processing."""

    async def test_text_response(self) -> None:
        phase = ActPhase(ToolExecutor(), AsyncMock())
        result = await phase.process(_response(), [], _perception())
        assert isinstance(result, ActionResult)
        assert result.response_text == "Hi!"
        assert result.target_channel == "telegram"

    async def test_reply_to_from_metadata(self) -> None:
        phase = ActPhase(ToolExecutor(), AsyncMock())
        result = await phase.process(_response(), [], _perception())
        assert result.reply_to == "msg123"

    async def test_degraded_response(self) -> None:
        phase = ActPhase(ToolExecutor(), AsyncMock())
        result = await phase.process(
            _response(content="Degraded", finish_reason="error"), [], _perception()
        )
        assert result.degraded is True

    async def test_tool_calls_handled(self) -> None:
        phase = ActPhase(ToolExecutor(), AsyncMock())
        calls = [ToolCall(id="tc1", function_name="search", arguments={})]
        result = await phase.process(_response(tool_calls=calls), [], _perception())
        assert len(result.tool_calls_made) == 1

    async def test_no_reply_to_if_missing(self) -> None:
        p = Perception(
            id="p1",
            type=PerceptionType.USER_MESSAGE,
            source="cli",
            content="Hi",
        )
        phase = ActPhase(ToolExecutor(), AsyncMock())
        result = await phase.process(_response(), [], p)
        assert result.reply_to is None

    async def test_pii_guard_applied(self) -> None:
        """PII guard redacts text when configured."""

        @dataclass
        class _PIIResult:
            text: str

        pii_mock = MagicMock()
        pii_mock.check.return_value = _PIIResult(text="Hi [REDACTED]!")
        phase = ActPhase(ToolExecutor(), AsyncMock(), pii_guard=pii_mock)
        result = await phase.process(_response(content="Hi John!"), [], _perception())
        assert result.response_text == "Hi [REDACTED]!"
        pii_mock.check.assert_called_once()

    async def test_output_guard_filtered_with_category(self) -> None:
        """Output guard filters content and reports category."""

        @dataclass
        class _Match:
            category: MagicMock

        @dataclass
        class _FilterResult:
            text: str
            filtered: bool
            action: str
            match: _Match | None

        cat_mock = MagicMock()
        cat_mock.value = "harmful"
        guard_mock = MagicMock()
        guard_mock.check.return_value = _FilterResult(
            text="[filtered]",
            filtered=True,
            action="redact",
            match=_Match(category=cat_mock),
        )
        phase = ActPhase(ToolExecutor(), AsyncMock(), output_guard=guard_mock)
        result = await phase.process(_response(content="bad"), [], _perception())
        assert result.output_filtered is True
        assert result.filter_reason == "redact:harmful"
        assert result.response_text == "[filtered]"

    async def test_output_guard_not_filtered(self) -> None:
        """Output guard passes clean content through."""

        @dataclass
        class _FilterResult:
            text: str
            filtered: bool
            action: str
            match: None = None

        guard_mock = MagicMock()
        guard_mock.check.return_value = _FilterResult(
            text="clean",
            filtered=False,
            action="pass",
        )
        phase = ActPhase(ToolExecutor(), AsyncMock(), output_guard=guard_mock)
        result = await phase.process(_response(content="clean"), [], _perception())
        assert result.output_filtered is False
        assert result.filter_reason is None
        assert result.response_text == "clean"

    async def test_output_guard_filtered_without_category(self) -> None:
        """Output guard reports action when no category."""

        @dataclass
        class _FilterResult:
            text: str
            filtered: bool
            action: str
            match: None = None

        guard_mock = MagicMock()
        guard_mock.check.return_value = _FilterResult(
            text="[filtered]",
            filtered=True,
            action="replace",
        )
        phase = ActPhase(ToolExecutor(), AsyncMock(), output_guard=guard_mock)
        result = await phase.process(_response(content="bad"), [], _perception())
        assert result.output_filtered is True
        assert result.filter_reason == "replace"


def _financial_gate(enabled: bool = True) -> FinancialGate:
    """Create a FinancialGate with configurable financial_confirmation."""
    return FinancialGate(SafetyConfig(financial_confirmation=enabled))


def _financial_tool_call(
    tool_id: str = "tc_fin1",
    name: str = "buy_crypto",
    args: dict[str, object] | None = None,
) -> ToolCall:
    """Create a financial tool call."""
    return ToolCall(
        id=tool_id,
        function_name=name,
        arguments=args or {"amount": "100", "currency": "BTC"},
    )


def _readonly_tool_call(tool_id: str = "tc_ro1") -> ToolCall:
    """Create a read-only (non-financial) tool call."""
    return ToolCall(
        id=tool_id,
        function_name="get_balance",
        arguments={"account": "main"},
    )


class TestActionResultButtons:
    """ActionResult.buttons field — type safety and defaults."""

    def test_buttons_default_none(self) -> None:
        result = ActionResult(response_text="Hi", target_channel="telegram")
        assert result.buttons is None

    def test_buttons_with_inline_buttons(self) -> None:
        btns = [
            [
                InlineButton(text="✅ Approve", callback_data="fin_confirm:tc1"),
                InlineButton(text="❌ Deny", callback_data="fin_cancel:tc1"),
            ]
        ]
        result = ActionResult(
            response_text="Confirm?",
            target_channel="telegram",
            buttons=btns,
        )
        assert result.buttons is not None
        assert len(result.buttons) == 1
        assert len(result.buttons[0]) == 2
        assert isinstance(result.buttons[0][0], InlineButton)
        assert result.buttons[0][0].text == "✅ Approve"
        assert result.buttons[0][1].callback_data == "fin_cancel:tc1"

    def test_buttons_empty_list(self) -> None:
        result = ActionResult(
            response_text="Hi",
            target_channel="telegram",
            buttons=[],
        )
        assert result.buttons == []

    def test_buttons_multiple_rows(self) -> None:
        row1 = [InlineButton(text="A", callback_data="a")]
        row2 = [
            InlineButton(text="B", callback_data="b"),
            InlineButton(text="C", callback_data="c"),
        ]
        result = ActionResult(
            response_text="Pick",
            target_channel="telegram",
            buttons=[row1, row2],
        )
        assert len(result.buttons) == 2  # type: ignore[arg-type]
        assert len(result.buttons[1]) == 2  # type: ignore[index]


class TestActPhaseFinancialButtons:
    """ActPhase generates inline buttons for financial confirmations."""

    async def test_single_financial_tool_generates_buttons(self) -> None:
        gate = _financial_gate()
        phase = ActPhase(ToolExecutor(), AsyncMock(), financial_gate=gate)
        tc = _financial_tool_call()
        result = await phase.process(_response(tool_calls=[tc]), [], _perception())

        assert result.pending_confirmation is True
        assert result.buttons is not None
        assert len(result.buttons) == 1  # one row
        assert len(result.buttons[0]) == 2  # Approve + Deny

        approve_btn = result.buttons[0][0]
        deny_btn = result.buttons[0][1]
        assert isinstance(approve_btn, InlineButton)
        assert isinstance(deny_btn, InlineButton)
        assert approve_btn.text == "✅ Approve"
        assert deny_btn.text == "❌ Deny"

    async def test_single_financial_tool_callback_data_format(self) -> None:
        gate = _financial_gate()
        phase = ActPhase(ToolExecutor(), AsyncMock(), financial_gate=gate)
        tc = _financial_tool_call(tool_id="tc_abc123")
        result = await phase.process(_response(tool_calls=[tc]), [], _perception())

        assert result.buttons is not None
        assert result.buttons[0][0].callback_data == "fin_confirm:tc_abc123"
        assert result.buttons[0][1].callback_data == "fin_cancel:tc_abc123"

    async def test_confirmation_details_single_tool(self) -> None:
        gate = _financial_gate()
        phase = ActPhase(ToolExecutor(), AsyncMock(), financial_gate=gate)
        tc = _financial_tool_call(tool_id="tc_x", name="buy_crypto")
        result = await phase.process(_response(tool_calls=[tc]), [], _perception())

        assert result.confirmation_details is not None
        assert result.confirmation_details["tool_call_id"] == "tc_x"
        assert result.confirmation_details["tool_name"] == "buy_crypto"
        assert "summary" in result.confirmation_details

    async def test_confirmation_message_contains_summary(self) -> None:
        gate = _financial_gate()
        phase = ActPhase(ToolExecutor(), AsyncMock(), financial_gate=gate)
        tc = _financial_tool_call()
        result = await phase.process(_response(tool_calls=[tc]), [], _perception())

        assert "⚠️" in result.response_text
        assert "confirmation" in result.response_text.lower()

    async def test_tool_calls_preserved_in_result(self) -> None:
        gate = _financial_gate()
        phase = ActPhase(ToolExecutor(), AsyncMock(), financial_gate=gate)
        tc = _financial_tool_call()
        result = await phase.process(_response(tool_calls=[tc]), [], _perception())

        assert len(result.tool_calls_made) == 1
        assert result.tool_calls_made[0].id == tc.id

    async def test_readonly_tool_no_buttons(self) -> None:
        """Read-only tools (get_balance) should NOT trigger financial gate."""
        gate = _financial_gate()
        phase = ActPhase(ToolExecutor(), AsyncMock(), financial_gate=gate)
        tc = _readonly_tool_call()
        result = await phase.process(_response(tool_calls=[tc]), [], _perception())

        assert result.pending_confirmation is False
        assert result.buttons is None

    async def test_gate_disabled_no_buttons(self) -> None:
        """When financial_confirmation=False, no buttons generated."""
        gate = _financial_gate(enabled=False)
        phase = ActPhase(ToolExecutor(), AsyncMock(), financial_gate=gate)
        tc = _financial_tool_call()
        result = await phase.process(_response(tool_calls=[tc]), [], _perception())

        assert result.pending_confirmation is False
        assert result.buttons is None

    async def test_no_gate_no_buttons(self) -> None:
        """When no financial_gate configured, no buttons generated."""
        phase = ActPhase(ToolExecutor(), AsyncMock())
        tc = _financial_tool_call()
        result = await phase.process(_response(tool_calls=[tc]), [], _perception())

        assert result.pending_confirmation is False
        assert result.buttons is None

    async def test_reply_to_preserved_with_buttons(self) -> None:
        gate = _financial_gate()
        phase = ActPhase(ToolExecutor(), AsyncMock(), financial_gate=gate)
        tc = _financial_tool_call()
        result = await phase.process(_response(tool_calls=[tc]), [], _perception())

        assert result.reply_to == "msg123"

    async def test_target_channel_preserved_with_buttons(self) -> None:
        gate = _financial_gate()
        phase = ActPhase(ToolExecutor(), AsyncMock(), financial_gate=gate)
        tc = _financial_tool_call()
        result = await phase.process(_response(tool_calls=[tc]), [], _perception())

        assert result.target_channel == "telegram"


class TestActPhaseBatchConfirmation:
    """ActPhase batch confirmation — multiple financial tool calls."""

    async def test_multiple_financial_tools_batch_buttons(self) -> None:
        gate = _financial_gate()
        phase = ActPhase(ToolExecutor(), AsyncMock(), financial_gate=gate)
        tc1 = _financial_tool_call(tool_id="tc1", name="buy_crypto")
        tc2 = _financial_tool_call(
            tool_id="tc2",
            name="transfer_funds",
            args={"amount": "500", "recipient": "alice"},
        )
        result = await phase.process(_response(tool_calls=[tc1, tc2]), [], _perception())

        assert result.pending_confirmation is True
        assert result.buttons is not None
        assert len(result.buttons) == 1  # one row
        assert len(result.buttons[0]) == 2  # Approve All + Deny All

        assert result.buttons[0][0].text == "✅ Approve All"
        assert result.buttons[0][1].text == "❌ Deny All"

    async def test_batch_callback_data_uses_group_id(self) -> None:
        gate = _financial_gate()
        phase = ActPhase(ToolExecutor(), AsyncMock(), financial_gate=gate)
        tc1 = _financial_tool_call(tool_id="tc_first")
        tc2 = _financial_tool_call(tool_id="tc_second", name="sell_crypto")
        result = await phase.process(_response(tool_calls=[tc1, tc2]), [], _perception())

        assert result.buttons is not None
        # Group anchor is the first tool call ID
        assert result.buttons[0][0].callback_data == "fin_confirm_all:tc_first"
        assert result.buttons[0][1].callback_data == "fin_cancel_all:tc_first"

    async def test_batch_confirmation_details(self) -> None:
        gate = _financial_gate()
        phase = ActPhase(ToolExecutor(), AsyncMock(), financial_gate=gate)
        tc1 = _financial_tool_call(tool_id="tc1", name="buy_crypto")
        tc2 = _financial_tool_call(tool_id="tc2", name="sell_crypto")
        result = await phase.process(_response(tool_calls=[tc1, tc2]), [], _perception())

        assert result.confirmation_details is not None
        assert result.confirmation_details["tool_call_ids"] == ["tc1", "tc2"]
        assert result.confirmation_details["tool_names"] == [
            "buy_crypto",
            "sell_crypto",
        ]
        assert result.confirmation_details["count"] == 2
        assert "summaries" in result.confirmation_details

    async def test_batch_message_lists_all_actions(self) -> None:
        gate = _financial_gate()
        phase = ActPhase(ToolExecutor(), AsyncMock(), financial_gate=gate)
        tc1 = _financial_tool_call(tool_id="tc1", name="buy_crypto")
        tc2 = _financial_tool_call(tool_id="tc2", name="sell_crypto")
        result = await phase.process(_response(tool_calls=[tc1, tc2]), [], _perception())

        assert "Multiple" in result.response_text or "multiple" in result.response_text
        assert "1." in result.response_text
        assert "2." in result.response_text

    async def test_mixed_financial_and_readonly(self) -> None:
        """Only financial tool calls get gated; readonly passes through."""
        gate = _financial_gate()
        phase = ActPhase(ToolExecutor(), AsyncMock(), financial_gate=gate)
        tc_fin = _financial_tool_call(tool_id="tc_fin")
        tc_ro = _readonly_tool_call(tool_id="tc_ro")
        result = await phase.process(_response(tool_calls=[tc_fin, tc_ro]), [], _perception())

        # The financial one triggers the gate
        assert result.pending_confirmation is True
        assert result.buttons is not None
        # Single financial = single approve/deny (not batch)
        assert result.buttons[0][0].text == "✅ Approve"

    async def test_no_buttons_for_text_only_response(self) -> None:
        """Text responses (no tool calls) never have buttons."""
        gate = _financial_gate()
        phase = ActPhase(ToolExecutor(), AsyncMock(), financial_gate=gate)
        result = await phase.process(_response(), [], _perception())
        assert result.buttons is None
        assert result.pending_confirmation is False

    async def test_three_financial_tools_still_batch(self) -> None:
        gate = _financial_gate()
        phase = ActPhase(ToolExecutor(), AsyncMock(), financial_gate=gate)
        calls = [
            _financial_tool_call(tool_id="tc1", name="buy_crypto"),
            _financial_tool_call(tool_id="tc2", name="sell_crypto"),
            _financial_tool_call(
                tool_id="tc3",
                name="transfer_funds",
                args={"amount": "200", "recipient": "bob"},
            ),
        ]
        result = await phase.process(_response(tool_calls=calls), [], _perception())

        assert result.pending_confirmation is True
        assert result.buttons is not None
        assert result.buttons[0][0].text == "✅ Approve All"
        assert result.confirmation_details is not None
        assert result.confirmation_details["count"] == 3
