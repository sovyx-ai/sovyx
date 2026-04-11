"""Sovyx Calculator Plugin — safe math eval via AST.

Built-in plugin with zero external dependencies. Proves the
Plugin SDK → LLM → tool_call → execute → response pipeline works.

Supports: +, -, *, /, //, %, **, parentheses, int, float.
Rejects: function calls, imports, attribute access, assignments.

Ref: SPE-008 §7.3
"""

from __future__ import annotations

import ast
import math
import operator
from typing import ClassVar

from sovyx.plugins.sdk import ISovyxPlugin, tool

# Allowed binary operators
_BINARY_OPS: dict[type, object] = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod,
    ast.Pow: operator.pow,
}

# Allowed unary operators
_UNARY_OPS: dict[type, object] = {
    ast.UAdd: operator.pos,
    ast.USub: operator.neg,
}

# Allowed constants
_CONSTANTS: dict[str, float] = {
    "pi": math.pi,
    "e": math.e,
    "tau": math.tau,
    "inf": math.inf,
}

_MAX_EXPRESSION_LEN = 500
_MAX_RESULT = 1e308


class CalculatorPlugin(ISovyxPlugin):
    """Safe math calculator — no eval(), AST-only evaluation."""

    config_schema: ClassVar[dict[str, object]] = {}

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
        """Evaluate a math expression safely via AST.

        Args:
            expression: Math expression string (e.g. "2 + 3 * 4").

        Returns:
            Result as string, or error message.
        """
        if len(expression) > _MAX_EXPRESSION_LEN:
            return f"Error: expression too long (max {_MAX_EXPRESSION_LEN} chars)"

        try:
            result = _safe_eval(expression)
        except (ValueError, TypeError, ZeroDivisionError, OverflowError) as e:
            return f"Error: {e}"
        except Exception:  # noqa: BLE001
            return "Error: invalid expression"

        # Format result
        if isinstance(result, float):
            if abs(result) > _MAX_RESULT:
                return "Error: result too large"
            if result == int(result) and not math.isinf(result):
                return str(int(result))
            return f"{result:.10g}"
        return str(result)


def _safe_eval(expression: str) -> int | float:
    """Evaluate a math expression using AST.

    Only allows:
    - Numbers (int, float)
    - Binary operators (+, -, *, /, //, %, **)
    - Unary operators (+, -)
    - Named constants (pi, e, tau, inf)
    - Parentheses

    Raises:
        ValueError: If expression contains disallowed constructs.
    """
    try:
        tree = ast.parse(expression.strip(), mode="eval")
    except SyntaxError as e:
        msg = f"invalid syntax: {e.msg}"
        raise ValueError(msg) from e

    return _eval_node(tree.body)


def _eval_node(node: ast.expr) -> int | float:
    """Recursively evaluate an AST node."""
    # Number literal
    if isinstance(node, ast.Constant):
        if isinstance(node.value, (int, float)):
            return node.value
        msg = f"unsupported constant type: {type(node.value).__name__}"
        raise ValueError(msg)

    # Named constant (pi, e, etc.)
    if isinstance(node, ast.Name):
        name = node.id.lower()
        if name in _CONSTANTS:
            return _CONSTANTS[name]
        msg = f"unknown variable: {node.id}"
        raise ValueError(msg)

    # Unary operator (-x, +x)
    if isinstance(node, ast.UnaryOp):
        op_func = _UNARY_OPS.get(type(node.op))
        if op_func is None:
            msg = f"unsupported unary operator: {type(node.op).__name__}"
            raise ValueError(msg)
        operand = _eval_node(node.operand)
        result: int | float = op_func(operand)  # type: ignore[operator]
        return result

    # Binary operator (x + y, x * y, etc.)
    if isinstance(node, ast.BinOp):
        op_func = _BINARY_OPS.get(type(node.op))
        if op_func is None:
            msg = f"unsupported operator: {type(node.op).__name__}"
            raise ValueError(msg)
        left = _eval_node(node.left)
        right = _eval_node(node.right)

        # Safety: limit power to prevent huge numbers
        if isinstance(node.op, ast.Pow) and isinstance(right, (int, float)) and abs(right) > 1000:
            msg = "exponent too large (max 1000)"
            raise ValueError(msg)

        bin_result: int | float = op_func(left, right)  # type: ignore[operator]
        return bin_result

    # Anything else is not allowed
    msg = f"unsupported expression: {type(node).__name__}"
    raise ValueError(msg)
