"""Sovyx Calculator Plugin — backward compatibility wrapper.

The calculator has been superseded by FinancialMathPlugin with Decimal
precision. This module provides a backward-compatible wrapper that
preserves the ``calculator`` name and plain-string output format.

For new code, use ``financial_math.FinancialMathPlugin`` directly.
"""

from __future__ import annotations

from decimal import Decimal

from sovyx.plugins.official.financial_math import (
    FinancialMathPlugin,
    _eval_node,
    _format_decimal,
    _safe_eval,
)
from sovyx.plugins.sdk import tool

_MAX_EXPRESSION_LEN = 500
_MAX_RESULT = Decimal("1E308")


class CalculatorPlugin(FinancialMathPlugin):
    """Backward-compatible calculator — wraps FinancialMathPlugin.

    Preserves ``name="calculator"`` and plain-string output
    so existing tool references (``calculator.calculate``) work.
    """

    @property
    def name(self) -> str:
        return "calculator"

    @property
    def version(self) -> str:
        return "1.0.0"

    @property
    def description(self) -> str:
        return "Safe math calculator using AST evaluation."

    @tool(
        description=(
            "Calculate a math expression safely. "
            "Supports: +, -, *, /, //, %, **, parentheses, pi, e, tau."
        ),
    )
    async def calculate(self, expression: str) -> str:
        """Evaluate expression — returns plain string for backward compat."""
        if len(expression) > _MAX_EXPRESSION_LEN:
            return f"Error: expression too long (max {_MAX_EXPRESSION_LEN} chars)"

        try:
            result = _safe_eval(expression)
        except (
            ValueError,
            TypeError,
            ZeroDivisionError,
            OverflowError,
        ) as e:
            return f"Error: {e}"
        except Exception:  # noqa: BLE001
            return "Error: invalid expression"

        if result.is_finite() and abs(result) > _MAX_RESULT:
            return "Error: result too large"

        return _format_decimal(result)


__all__ = ["CalculatorPlugin", "_eval_node", "_safe_eval"]
