"""Sovyx FinancialGate — intercept financial tool calls for confirmation.

When ``financial_confirmation`` is enabled in SafetyConfig, tool calls
classified as financial are NOT executed immediately. Instead, the gate
returns a pending confirmation that the channel bridge presents to the
user for approval.

Classification uses two signals:
1. **Tool name**: matches against known financial action patterns
   (payment, transfer, buy, sell, trade, withdraw, invest, etc.)
2. **Argument keys**: presence of financial argument names
   (amount, price, cost, balance, etc.)

A tool is financial if its name matches OR it has ≥2 financial argument keys.
Read-only tools (calculate, estimate, check, get, list, show, view) are
explicitly excluded to avoid false positives.

When ``financial_confirmation=False``, the gate is a complete no-op
with zero overhead.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from sovyx.observability.logging import get_logger

if TYPE_CHECKING:
    from sovyx.llm.models import ToolCall
    from sovyx.mind.config import SafetyConfig

logger = get_logger(__name__)

# ── Financial tool name patterns ───────────────────────────────────────
# Match tool names indicating financial write operations.

_FINANCIAL_NAME_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\b(?:send|make|create|execute|submit|process)_?payment\b", re.I),
    re.compile(r"\b(?:transfer|wire|send)_?(?:funds?|money|crypto|tokens?)\b", re.I),
    re.compile(r"\b(?:buy|purchase|order|acquire)\b", re.I),
    re.compile(r"\b(?:sell|liquidate|dispose)\b", re.I),
    re.compile(r"\b(?:trade|swap|exchange|convert)_?(?:crypto|tokens?|currency|stocks?)?\b", re.I),
    re.compile(r"\b(?:withdraw|cash_?out|redeem)\b", re.I),
    re.compile(r"\b(?:invest|stake|deposit|fund)\b", re.I),
    re.compile(r"\b(?:place_?(?:order|bet|bid)|submit_?order)\b", re.I),
    re.compile(r"\b(?:approve|authorize|confirm)_?(?:transaction|payment|transfer)\b", re.I),
    re.compile(r"\b(?:cancel|refund|chargeback)\b", re.I),
    re.compile(
        r"\b(?:subscribe|unsubscribe|upgrade|downgrade)_?(?:plan|tier|membership)?\b",
        re.I,
    ),
)

# ── Read-only exclusion patterns ──────────────────────────────────────
# These prefixes indicate read operations that should NOT be gated,
# even if the name contains financial-sounding words.

_READONLY_PREFIXES: tuple[re.Pattern[str], ...] = (
    re.compile(r"^(?:get|fetch|list|show|view|check|read|query|search)_", re.I),
    re.compile(r"^(?:calculate|compute|estimate|simulate|preview|forecast)_", re.I),
    re.compile(r"^(?:validate|verify|lookup|describe|inspect|monitor)_", re.I),
)

# ── Financial argument keys ───────────────────────────────────────────
# Presence of ≥2 of these keys in arguments signals financial intent.

_FINANCIAL_ARG_KEYS: frozenset[str] = frozenset(
    {
        "amount",
        "price",
        "cost",
        "total",
        "balance",
        "quantity",
        "units",
        "shares",
        "value",
        "fee",
        "tip",
        "currency",
        "token",
        "wallet",
        "account",
        "recipient",
        "destination",
        "payment_method",
    }
)

# Minimum number of financial arg keys to trigger (avoids false positives)
_MIN_FINANCIAL_ARGS = 2

# Confirmation timeout (seconds)
CONFIRMATION_TIMEOUT_SECONDS = 300  # 5 minutes


@dataclass(frozen=True, slots=True)
class PendingConfirmation:
    """A financial action awaiting user confirmation.

    Attributes:
        tool_call: The original tool call that triggered the gate.
        summary: Human-readable summary of the action.
        created_at: Unix timestamp when the confirmation was created.
        timeout_seconds: Seconds before the confirmation expires.
    """

    tool_call: ToolCall
    summary: str
    created_at: float
    timeout_seconds: int = CONFIRMATION_TIMEOUT_SECONDS

    @property
    def expired(self) -> bool:
        """Check if confirmation has timed out."""
        return (time.monotonic() - self.created_at) > self.timeout_seconds


@dataclass
class FinancialGateState:
    """Mutable state for the financial gate.

    Tracks pending confirmations per conversation.
    """

    pending: dict[str, PendingConfirmation] = field(default_factory=dict)

    def add(self, confirmation: PendingConfirmation) -> None:
        """Add a pending confirmation."""
        self.pending[confirmation.tool_call.id] = confirmation

    def get_pending(self) -> PendingConfirmation | None:
        """Get the most recent non-expired pending confirmation."""
        # Clean expired
        expired_keys = [k for k, v in self.pending.items() if v.expired]
        for k in expired_keys:
            logger.info(
                "financial_confirmation_expired",
                tool_call_id=k,
            )
            del self.pending[k]

        # Return most recent
        if self.pending:
            return next(reversed(self.pending.values()))
        return None

    def confirm(self, tool_call_id: str) -> PendingConfirmation | None:
        """Confirm and remove a pending confirmation."""
        return self.pending.pop(tool_call_id, None)

    def cancel_all(self) -> int:
        """Cancel all pending confirmations. Returns count cancelled."""
        count = len(self.pending)
        self.pending.clear()
        return count


# ── Confirmation response detection ───────────────────────────────────

_CONFIRM_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(
        r"^\s*(?:yes|yep|yeah|yup|sure|ok|okay|confirm|approved?|go\s+ahead|do\s+it)\s*[.!]?\s*$",
        re.I,
    ),
    re.compile(r"^\s*(?:sim|confirma|confirmado|pode|manda|vai|beleza|bora)\s*[.!]?\s*$", re.I),
)

_CANCEL_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"^\s*(?:no|nope|nah|cancel|deny|stop|abort|don'?t|negative)\s*[.!]?\s*$", re.I),
    re.compile(r"^\s*(?:não|nao|cancela|para|aborta|nega|deixa)\s*[.!]?\s*$", re.I),
)


def is_confirmation(text: str) -> bool:
    """Check if user message is a confirmation response."""
    return any(p.match(text) for p in _CONFIRM_PATTERNS)


def is_cancellation(text: str) -> bool:
    """Check if user message is a cancellation response."""
    return any(p.match(text) for p in _CANCEL_PATTERNS)


class FinancialGate:
    """Intercept financial tool calls for user confirmation.

    Reads SafetyConfig dynamically — when ``financial_confirmation``
    is False, all methods are no-ops with zero overhead.
    """

    def __init__(self, safety_config: SafetyConfig) -> None:
        self._safety = safety_config
        self._state = FinancialGateState()

    @property
    def state(self) -> FinancialGateState:
        """Access the gate state (for testing/inspection)."""
        return self._state

    def check_tool_call(self, tool_call: ToolCall) -> PendingConfirmation | None:
        """Check if a tool call requires financial confirmation.

        Args:
            tool_call: The tool call to check.

        Returns:
            PendingConfirmation if the call is gated, None if it can proceed.
        """
        if not self._safety.financial_confirmation:
            return None

        if not self._is_financial(tool_call):
            return None

        summary = self._build_summary(tool_call)
        confirmation = PendingConfirmation(
            tool_call=tool_call,
            summary=summary,
            created_at=time.monotonic(),
        )
        self._state.add(confirmation)

        logger.info(
            "financial_tool_call_gated",
            tool=tool_call.function_name,
            call_id=tool_call.id,
            summary=summary,
        )

        return confirmation

    def handle_user_response(
        self,
        text: str,
    ) -> tuple[str, PendingConfirmation | None]:
        """Handle user response to a pending confirmation.

        Args:
            text: User message text.

        Returns:
            ("confirmed", confirmation) if user approved.
            ("cancelled", confirmation) if user denied.
            ("expired", None) if no pending or expired.
            ("none", None) if no pending confirmation.
        """
        pending = self._state.get_pending()
        if pending is None:
            return "none", None

        if is_confirmation(text):
            confirmed = self._state.confirm(pending.tool_call.id)
            logger.info(
                "financial_confirmation_approved",
                tool_call_id=pending.tool_call.id,
                tool=pending.tool_call.function_name,
            )
            return "confirmed", confirmed

        if is_cancellation(text):
            self._state.confirm(pending.tool_call.id)  # remove it
            logger.info(
                "financial_confirmation_cancelled",
                tool_call_id=pending.tool_call.id,
                tool=pending.tool_call.function_name,
            )
            return "cancelled", pending

        # Not a clear confirm/cancel — don't consume the pending
        return "none", None

    def has_pending(self) -> bool:
        """Check if there are any pending confirmations."""
        return self._state.get_pending() is not None

    def _is_financial(self, tool_call: ToolCall) -> bool:
        """Classify a tool call as financial or not."""
        name = tool_call.function_name

        # Exclude read-only operations first
        for pattern in _READONLY_PREFIXES:
            if pattern.match(name):
                return False

        # Check name patterns
        for pattern in _FINANCIAL_NAME_PATTERNS:
            if pattern.search(name):
                return True

        # Check argument keys (≥2 financial keys = financial)
        arg_keys = {k.lower() for k in tool_call.arguments}
        financial_args = arg_keys & _FINANCIAL_ARG_KEYS
        return len(financial_args) >= _MIN_FINANCIAL_ARGS

    @staticmethod
    def _build_summary(tool_call: ToolCall) -> str:
        """Build human-readable summary of the financial action."""
        name = tool_call.function_name.replace("_", " ").title()
        args = tool_call.arguments

        parts = [f"Action: {name}"]

        # Extract key financial details
        for key in ("amount", "price", "cost", "total", "value"):
            if key in args:
                parts.append(f"Amount: {args[key]}")
                break

        for key in ("currency", "token"):
            if key in args:
                parts.append(f"Currency: {args[key]}")
                break

        for key in ("recipient", "destination", "account", "wallet"):
            if key in args:
                parts.append(f"To: {args[key]}")
                break

        return " | ".join(parts)
