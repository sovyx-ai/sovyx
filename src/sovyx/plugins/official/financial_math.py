"""Sovyx Financial Math Plugin — enterprise-grade financial calculations.

Precision-first financial mathematics using Python's Decimal module.
All calculations use banker's rounding (ROUND_HALF_EVEN) and 28-digit
precision. No float arithmetic — every operation is Decimal-native.

This plugin serves as the **SDK showcase**: it demonstrates tool design,
structured output, input validation, error handling, and Decimal precision
that developers can study and replicate.

Built-in plugin with zero external dependencies.

Tools:
    calculate — Safe expression parser (AST-only, Decimal-native)

Ref: SPE-008 §7.3
"""

from __future__ import annotations

import ast
import json
import math
import operator
from decimal import ROUND_HALF_EVEN, Decimal, DecimalException, InvalidOperation
from typing import ClassVar

from sovyx.plugins.sdk import ISovyxPlugin, tool

# ── Constants ──

_PRECISION = 28
_MAX_EXPRESSION_LEN = 500
_MAX_EXPONENT = 1000
_MAX_RESULT = Decimal("1E308")

_ZERO = Decimal(0)
_HUNDRED = Decimal(100)

_MATH_CONSTANTS: dict[str, Decimal] = {
    "pi": Decimal(str(math.pi)),
    "e": Decimal(str(math.e)),
    "tau": Decimal(str(math.tau)),
}


class _ValidationError(Exception):
    """Raised when required parameters are missing."""


def _require(**kwargs: object) -> None:
    """Validate that all required parameters are provided and not None."""
    missing = [k for k, v in kwargs.items() if v is None]
    if missing:
        names = ", ".join(missing)
        msg = f"missing required parameter(s): {names}"
        raise _ValidationError(msg)


def _fmt(value: object) -> str:
    """Format a value for JSON output."""
    if value is None:
        return "null"
    if isinstance(value, Decimal):
        return _format_decimal(value)
    if isinstance(value, float):
        return _format_decimal(_to_decimal(value))
    return str(value)


# ── Decimal helpers ──


def _to_decimal(value: object) -> Decimal:
    """Convert a value to Decimal safely.

    Always converts through string to avoid float precision loss.
    ``Decimal(0.1)`` → ``0.1000000000000000055511151231257827021181583404541015625``
    ``Decimal("0.1")`` → ``0.1`` ← what we want.
    """
    if isinstance(value, Decimal):
        return value
    if isinstance(value, int):
        return Decimal(value)
    if isinstance(value, float):
        if math.isnan(value) or math.isinf(value):
            msg = f"cannot convert {value} to Decimal"
            raise InvalidOperation(msg)
        return Decimal(str(value))
    if isinstance(value, str):
        return Decimal(value)
    msg = f"cannot convert {type(value).__name__} to Decimal"
    raise InvalidOperation(msg)


def _format_decimal(d: Decimal, max_places: int = 10) -> str:
    """Format a Decimal for display.

    - Integer results: no decimal point (``Decimal("6.00")`` → ``"6"``)
    - Fractional results: up to ``max_places`` significant decimal digits
    - Uses banker's rounding (ROUND_HALF_EVEN)
    """
    if not d.is_finite():
        return str(d)

    # Check if it's effectively an integer
    if d == d.to_integral_value():
        return str(d.to_integral_value())

    # Quantize to max_places
    quantizer = Decimal(10) ** -max_places
    rounded = d.quantize(quantizer, rounding=ROUND_HALF_EVEN)

    # Strip trailing zeros but keep at least 1 decimal place
    normalized = rounded.normalize()
    if "." not in str(normalized):
        return str(normalized)
    return str(normalized)


# ── Response helpers ──


def _ok(action: str, **kwargs: object) -> str:
    """Build a success JSON response."""
    return json.dumps({"ok": True, "action": action, **kwargs})


def _err(message: str) -> str:
    """Build an error JSON response."""
    return json.dumps({"ok": False, "action": "error", "message": message})


# ── AST Expression Engine (Decimal-native) ──

_BINARY_OPS: dict[type, object] = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod,
    ast.Pow: operator.pow,
}

_UNARY_OPS: dict[type, object] = {
    ast.UAdd: operator.pos,
    ast.USub: operator.neg,
}


def _safe_eval(expression: str) -> Decimal:
    """Evaluate a math expression via AST, returning Decimal.

    Only allows: numbers, binary ops, unary ops, named constants, parens.

    Raises:
        ValueError: If expression contains disallowed constructs.
    """
    try:
        tree = ast.parse(expression.strip(), mode="eval")
    except SyntaxError as e:
        msg = f"invalid syntax: {e.msg}"
        raise ValueError(msg) from e

    return _eval_node(tree.body)


def _eval_node(node: ast.expr) -> Decimal:
    """Recursively evaluate an AST node, all arithmetic in Decimal."""
    # Number literal → convert to Decimal
    if isinstance(node, ast.Constant):
        if isinstance(node.value, (int, float)):
            return _to_decimal(node.value)
        msg = f"unsupported constant type: {type(node.value).__name__}"
        raise ValueError(msg)

    # Named constant (pi, e, tau)
    if isinstance(node, ast.Name):
        name = node.id.lower()
        if name in _MATH_CONSTANTS:
            return _MATH_CONSTANTS[name]
        msg = f"unknown variable: {node.id}"
        raise ValueError(msg)

    # Unary operator (-x, +x)
    if isinstance(node, ast.UnaryOp):
        op_func = _UNARY_OPS.get(type(node.op))
        if op_func is None:
            msg = f"unsupported unary operator: {type(node.op).__name__}"
            raise ValueError(msg)
        operand = _eval_node(node.operand)
        result: Decimal = op_func(operand)  # type: ignore[operator]
        return result

    # Binary operator
    if isinstance(node, ast.BinOp):
        op_func = _BINARY_OPS.get(type(node.op))
        if op_func is None:
            msg = f"unsupported operator: {type(node.op).__name__}"
            raise ValueError(msg)
        left = _eval_node(node.left)
        right = _eval_node(node.right)

        # Safety: limit power exponent
        if isinstance(node.op, ast.Pow) and abs(right) > _MAX_EXPONENT:
            msg = f"exponent too large (max {_MAX_EXPONENT})"
            raise ValueError(msg)

        bin_result: Decimal = op_func(left, right)  # type: ignore[operator]
        return bin_result

    msg = f"unsupported expression: {type(node).__name__}"
    raise ValueError(msg)


# ── Plugin ──


class FinancialMathPlugin(ISovyxPlugin):
    """Enterprise-grade financial math — Decimal precision, AST-only eval.

    All calculations use Python's ``decimal.Decimal`` with 28-digit precision
    and banker's rounding (ROUND_HALF_EVEN). No float arithmetic anywhere.

    This plugin is designed as a **showcase** for the Sovyx Plugin SDK.
    """

    config_schema: ClassVar[dict[str, object]] = {}

    @property
    def name(self) -> str:
        return "financial-math"

    @property
    def version(self) -> str:
        return "2.0.0"

    @property
    def description(self) -> str:
        return (
            "Precision financial mathematics — Decimal-native calculations "
            "with banker's rounding. Expression parser, percentage operations, "
            "interest, amortization, portfolio analytics, and more."
        )

    @tool(
        description=(
            "Evaluate a math expression with Decimal precision. "
            "Supports: +, -, *, /, //, %, **, parentheses, pi, e, tau. "
            "All arithmetic uses Decimal (no floating-point errors). "
            "Example: '1500 * 1.0115 ** 12' returns exact result."
        ),
    )
    async def calculate(self, expression: str) -> str:
        """Evaluate a math expression via safe AST parser.

        All arithmetic is Decimal-native — ``0.1 + 0.2 == 0.3`` is exact.

        Args:
            expression: Math expression (e.g. ``"2 + 3 * 4"``).

        Returns:
            JSON with result and precision info.
        """
        if not expression or not expression.strip():
            return _err("empty expression")

        if len(expression) > _MAX_EXPRESSION_LEN:
            return _err(f"expression too long (max {_MAX_EXPRESSION_LEN} chars)")

        try:
            result = _safe_eval(expression)
        except (ValueError, TypeError, ZeroDivisionError, OverflowError) as e:
            return _err(str(e))
        except DecimalException as e:
            return _err(f"decimal error: {e}")
        except Exception:  # noqa: BLE001
            return _err("invalid expression")

        # Check result bounds
        if result.is_finite() and abs(result) > _MAX_RESULT:
            return _err("result too large")

        formatted = _format_decimal(result)

        return _ok(
            "calculate",
            expression=expression.strip(),
            result=formatted,
            precision="decimal",
            message=f"{expression.strip()} = {formatted}",
        )

    # ── Percentage Operations ──

    @tool(
        description=(
            "Percentage calculations. Modes: "
            "'of' (X% of Y), 'change' (from→to, returns %), "
            "'markup' (cost + X% markup), 'gross_margin' (revenue & cost → margin%), "
            "'net_margin' (revenue & net_income → margin%). "
            "Example: percentage(mode='change', from_value=67500, to_value=58200)"
        ),
    )
    async def percentage(
        self,
        mode: str,
        *,
        rate: float | None = None,
        value: float | None = None,
        from_value: float | None = None,
        to_value: float | None = None,
        cost: float | None = None,
        revenue: float | None = None,
        net_income: float | None = None,
    ) -> str:
        """Percentage operations with Decimal precision.

        Args:
            mode: Operation type — 'of', 'change', 'markup',
                  'gross_margin', 'net_margin'.
            rate: Percentage rate (e.g. 17.3 for 17.3%). Used by 'of', 'markup'.
            value: Base value. Used by 'of'.
            from_value: Starting value. Used by 'change'.
            to_value: Ending value. Used by 'change'.
            cost: Cost amount. Used by 'markup', 'gross_margin'.
            revenue: Revenue amount. Used by 'gross_margin', 'net_margin'.
            net_income: Net income. Used by 'net_margin'.

        Returns:
            JSON with result and breakdown.
        """
        mode = mode.strip().lower()
        try:
            if mode == "of":
                return self._pct_of(rate, value)
            if mode == "change":
                return self._pct_change(from_value, to_value)
            if mode == "markup":
                return self._pct_markup(rate, cost)
            if mode == "gross_margin":
                return self._pct_gross_margin(revenue, cost)
            if mode == "net_margin":
                return self._pct_net_margin(revenue, net_income)
        except _ValidationError as e:
            return _err(str(e))
        except (ZeroDivisionError, DecimalException) as e:
            return _err(f"calculation error: {e}")

        valid = "of, change, markup, gross_margin, net_margin"
        return _err(f"unknown mode: '{mode}'. Valid: {valid}")

    # ── Percentage Internals ──

    @staticmethod
    def _pct_of(
        rate: float | None,
        value: float | None,
    ) -> str:
        """X% of Y."""
        _require(rate=rate, value=value)
        d_rate = _to_decimal(rate) / _HUNDRED
        d_value = _to_decimal(value)
        result = d_rate * d_value
        return _ok(
            "percentage",
            mode="of",
            rate=_fmt(rate),
            value=_fmt(value),
            result=_format_decimal(result),
            message=(
                f"{_format_decimal(_to_decimal(rate))}% of "
                f"{_format_decimal(d_value)} = "
                f"{_format_decimal(result)}"
            ),
        )

    @staticmethod
    def _pct_change(
        from_value: float | None,
        to_value: float | None,
    ) -> str:
        """Percentage change from → to."""
        _require(from_value=from_value, to_value=to_value)
        d_from = _to_decimal(from_value)
        d_to = _to_decimal(to_value)
        if d_from == _ZERO:
            raise ZeroDivisionError("from_value cannot be zero")
        change = ((d_to - d_from) / d_from) * _HUNDRED
        return _ok(
            "percentage",
            mode="change",
            from_value=_fmt(from_value),
            to_value=_fmt(to_value),
            change_percent=_format_decimal(change),
            result=_format_decimal(change),
            message=(
                f"{_format_decimal(d_from)} → {_format_decimal(d_to)}: {_format_decimal(change)}%"
            ),
        )

    @staticmethod
    def _pct_markup(
        rate: float | None,
        cost: float | None,
    ) -> str:
        """Apply markup to cost."""
        _require(rate=rate, cost=cost)
        d_rate = _to_decimal(rate)
        d_cost = _to_decimal(cost)
        markup_amount = d_cost * d_rate / _HUNDRED
        price = d_cost + markup_amount
        return _ok(
            "percentage",
            mode="markup",
            cost=_format_decimal(d_cost),
            rate=_fmt(rate),
            markup_amount=_format_decimal(markup_amount),
            price=_format_decimal(price),
            result=_format_decimal(price),
            message=(
                f"Cost {_format_decimal(d_cost)} + "
                f"{_format_decimal(d_rate)}% markup = "
                f"{_format_decimal(price)}"
            ),
        )

    @staticmethod
    def _pct_gross_margin(
        revenue: float | None,
        cost: float | None,
    ) -> str:
        """Gross margin = (revenue - cost) / revenue * 100."""
        _require(revenue=revenue, cost=cost)
        d_rev = _to_decimal(revenue)
        d_cost = _to_decimal(cost)
        if d_rev == _ZERO:
            raise ZeroDivisionError("revenue cannot be zero")
        margin = ((d_rev - d_cost) / d_rev) * _HUNDRED
        return _ok(
            "percentage",
            mode="gross_margin",
            revenue=_format_decimal(d_rev),
            cost=_format_decimal(d_cost),
            gross_profit=_format_decimal(d_rev - d_cost),
            margin_percent=_format_decimal(margin),
            result=_format_decimal(margin),
            message=(
                f"Revenue {_format_decimal(d_rev)}, "
                f"Cost {_format_decimal(d_cost)} → "
                f"Gross Margin {_format_decimal(margin)}%"
            ),
        )

    @staticmethod
    def _pct_net_margin(
        revenue: float | None,
        net_income: float | None,
    ) -> str:
        """Net margin = net_income / revenue * 100."""
        _require(revenue=revenue, net_income=net_income)
        d_rev = _to_decimal(revenue)
        d_net = _to_decimal(net_income)
        if d_rev == _ZERO:
            raise ZeroDivisionError("revenue cannot be zero")
        margin = (d_net / d_rev) * _HUNDRED
        return _ok(
            "percentage",
            mode="net_margin",
            revenue=_format_decimal(d_rev),
            net_income=_format_decimal(d_net),
            margin_percent=_format_decimal(margin),
            result=_format_decimal(margin),
            message=(
                f"Revenue {_format_decimal(d_rev)}, "
                f"Net Income {_format_decimal(d_net)} → "
                f"Net Margin {_format_decimal(margin)}%"
            ),
        )
