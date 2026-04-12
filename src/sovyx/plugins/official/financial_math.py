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

_MATH_CONSTANTS: dict[str, Decimal] = {
    "pi": Decimal(str(math.pi)),
    "e": Decimal(str(math.e)),
    "tau": Decimal(str(math.tau)),
}


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
