"""Tests for Sovyx Financial Math Plugin — TASK-485.

Covers: Decimal engine, AST expression parser, structured JSON output,
input validation, edge cases, backward compatibility.
"""

from __future__ import annotations

import json
from decimal import Decimal

import pytest

from sovyx.plugins.official.financial_math import (
    FinancialMathPlugin,
    _eval_node,
    _format_decimal,
    _safe_eval,
    _to_decimal,
)

# ── Helpers ──


def _parse(raw: str) -> dict[str, object]:
    """Parse JSON response from plugin tool."""
    data = json.loads(raw)
    assert isinstance(data, dict)
    return data


# ── Decimal Conversion ──


class TestToDecimal:
    """Test _to_decimal helper — the foundation of precision."""

    def test_from_int(self) -> None:
        assert _to_decimal(42) == Decimal(42)

    def test_from_float_via_string(self) -> None:
        """Float 0.1 must convert via string to preserve precision."""
        result = _to_decimal(0.1)
        assert result == Decimal("0.1")
        # NOT Decimal(0.1) which would be 0.1000000000000000055...

    def test_from_string(self) -> None:
        assert _to_decimal("3.14") == Decimal("3.14")

    def test_from_decimal(self) -> None:
        d = Decimal("99.99")
        assert _to_decimal(d) is d  # identity

    def test_nan_rejected(self) -> None:
        with pytest.raises(Exception):  # noqa: B017
            _to_decimal(float("nan"))

    def test_inf_rejected(self) -> None:
        with pytest.raises(Exception):  # noqa: B017
            _to_decimal(float("inf"))

    def test_unsupported_type(self) -> None:
        with pytest.raises(Exception):  # noqa: B017
            _to_decimal([1, 2, 3])  # type: ignore[arg-type]


# ── Decimal Formatting ──


class TestFormatDecimal:
    """Test _format_decimal — display-quality output."""

    def test_integer_result(self) -> None:
        assert _format_decimal(Decimal("6.00")) == "6"

    def test_fractional_result(self) -> None:
        result = _format_decimal(Decimal("3.14159265358979323846"))
        assert result.startswith("3.14159265")

    def test_trailing_zeros_stripped(self) -> None:
        result = _format_decimal(Decimal("1.50000"))
        assert result == "1.5"

    def test_small_fraction(self) -> None:
        result = _format_decimal(Decimal("0.001"))
        assert result == "0.001"

    def test_negative(self) -> None:
        result = _format_decimal(Decimal("-42.5"))
        assert result == "-42.5"

    def test_infinity(self) -> None:
        result = _format_decimal(Decimal("Infinity"))
        assert result == "Infinity"

    def test_negative_infinity(self) -> None:
        result = _format_decimal(Decimal("-Infinity"))
        assert result == "-Infinity"

    def test_large_integer(self) -> None:
        result = _format_decimal(Decimal("1000000"))
        assert result == "1000000"


# ── Safe Eval (Decimal-native) ──


class TestSafeEval:
    """Test _safe_eval — AST parser returning Decimal."""

    def test_integer(self) -> None:
        assert _safe_eval("42") == Decimal(42)

    def test_float_literal(self) -> None:
        result = _safe_eval("3.14")
        assert isinstance(result, Decimal)

    def test_addition(self) -> None:
        assert _safe_eval("2 + 3") == Decimal(5)

    def test_decimal_precision(self) -> None:
        """0.1 + 0.2 must equal 0.3 exactly."""
        result = _safe_eval("0.1 + 0.2")
        assert result == Decimal("0.3")

    def test_complex_expression(self) -> None:
        assert _safe_eval("2 + 3 * 4 - 1") == Decimal(13)

    def test_parentheses(self) -> None:
        assert _safe_eval("(2 + 3) * 4") == Decimal(20)

    def test_nested_parentheses(self) -> None:
        assert _safe_eval("((2 + 3) * (4 - 1))") == Decimal(15)

    def test_power(self) -> None:
        assert _safe_eval("2 ** 10") == Decimal(1024)

    def test_floor_division(self) -> None:
        assert _safe_eval("10 // 3") == Decimal(3)

    def test_modulo(self) -> None:
        assert _safe_eval("10 % 3") == Decimal(1)

    def test_unary_minus(self) -> None:
        assert _safe_eval("-5") == Decimal(-5)

    def test_unary_plus(self) -> None:
        assert _safe_eval("+5") == Decimal(5)

    def test_pi_constant(self) -> None:
        result = _safe_eval("pi")
        assert result > Decimal(3) and result < Decimal(4)

    def test_e_constant(self) -> None:
        result = _safe_eval("e")
        assert result > Decimal(2) and result < Decimal(3)

    def test_tau_constant(self) -> None:
        result = _safe_eval("tau")
        assert result > Decimal(6) and result < Decimal(7)

    def test_division_by_zero(self) -> None:
        with pytest.raises((ZeroDivisionError, Exception)):
            _safe_eval("1 / 0")

    def test_syntax_error(self) -> None:
        with pytest.raises(ValueError, match="invalid syntax"):
            _safe_eval("2 +")

    def test_function_call_rejected(self) -> None:
        with pytest.raises(ValueError, match="unsupported"):
            _safe_eval("print('hello')")

    def test_list_rejected(self) -> None:
        with pytest.raises(ValueError, match="unsupported"):
            _safe_eval("[1, 2, 3]")

    def test_string_constant_rejected(self) -> None:
        with pytest.raises(ValueError, match="unsupported constant"):
            _safe_eval("'hello'")

    def test_unknown_variable(self) -> None:
        with pytest.raises(ValueError, match="unknown variable"):
            _safe_eval("x + 1")

    def test_large_exponent_rejected(self) -> None:
        with pytest.raises(ValueError, match="exponent too large"):
            _safe_eval("2 ** 10000")


class TestEvalNodeDirect:
    """Direct tests for _eval_node edge cases."""

    def test_unsupported_unary(self) -> None:
        import ast as _ast

        node = _ast.UnaryOp(op=_ast.Invert(), operand=_ast.Constant(value=5))
        with pytest.raises(ValueError, match="unsupported unary"):
            _eval_node(node)

    def test_unsupported_binary(self) -> None:
        import ast as _ast

        node = _ast.BinOp(
            left=_ast.Constant(value=1),
            op=_ast.BitAnd(),
            right=_ast.Constant(value=2),
        )
        with pytest.raises(ValueError, match="unsupported operator"):
            _eval_node(node)


# ── Plugin Interface ──


class TestPluginInterface:
    """Test plugin metadata and SDK compliance."""

    def test_name(self) -> None:
        p = FinancialMathPlugin()
        assert p.name == "financial-math"

    def test_version(self) -> None:
        p = FinancialMathPlugin()
        assert p.version == "2.0.0"

    def test_description(self) -> None:
        p = FinancialMathPlugin()
        assert "precision" in p.description.lower()
        assert "decimal" in p.description.lower()


# ── Calculate Tool — Structured Output ──


class TestCalculateTool:
    """Test calculate() tool with structured JSON responses."""

    @pytest.mark.anyio()
    async def test_basic_addition(self) -> None:
        p = FinancialMathPlugin()
        data = _parse(await p.calculate("2 + 3"))
        assert data["ok"] is True
        assert data["action"] == "calculate"
        assert data["result"] == "5"
        assert data["precision"] == "decimal"
        assert "message" in data

    @pytest.mark.anyio()
    async def test_decimal_precision(self) -> None:
        """The showcase moment: 0.1 + 0.2 == 0.3 exactly."""
        p = FinancialMathPlugin()
        data = _parse(await p.calculate("0.1 + 0.2"))
        assert data["ok"] is True
        assert data["result"] == "0.3"

    @pytest.mark.anyio()
    async def test_financial_calculation(self) -> None:
        """Real financial calc: monthly compound interest."""
        p = FinancialMathPlugin()
        data = _parse(await p.calculate("10000 * 1.0115 ** 12"))
        assert data["ok"] is True
        result = Decimal(str(data["result"]))
        # 10000 * 1.0115^12 ≈ 11470.72
        assert result > Decimal("11400")
        assert result < Decimal("11500")

    @pytest.mark.anyio()
    async def test_division(self) -> None:
        p = FinancialMathPlugin()
        data = _parse(await p.calculate("10 / 4"))
        assert data["result"] == "2.5"

    @pytest.mark.anyio()
    async def test_float_integer_display(self) -> None:
        """6.0 should display as 6, not 6.0."""
        p = FinancialMathPlugin()
        data = _parse(await p.calculate("3.0 + 3.0"))
        assert data["result"] == "6"

    @pytest.mark.anyio()
    async def test_negative_result(self) -> None:
        p = FinancialMathPlugin()
        data = _parse(await p.calculate("-5 + 3"))
        assert data["result"] == "-2"

    @pytest.mark.anyio()
    async def test_pi_calculation(self) -> None:
        p = FinancialMathPlugin()
        data = _parse(await p.calculate("pi * 2"))
        assert data["ok"] is True
        result = str(data["result"])
        assert result.startswith("6.28")

    @pytest.mark.anyio()
    async def test_one_third(self) -> None:
        p = FinancialMathPlugin()
        data = _parse(await p.calculate("1 / 3"))
        assert data["ok"] is True
        result = str(data["result"])
        assert result.startswith("0.3333333333")

    # ── Error Responses ──

    @pytest.mark.anyio()
    async def test_empty_expression(self) -> None:
        p = FinancialMathPlugin()
        data = _parse(await p.calculate(""))
        assert data["ok"] is False
        assert data["action"] == "error"
        assert "empty" in str(data["message"]).lower()

    @pytest.mark.anyio()
    async def test_whitespace_only(self) -> None:
        p = FinancialMathPlugin()
        data = _parse(await p.calculate("   "))
        assert data["ok"] is False

    @pytest.mark.anyio()
    async def test_too_long(self) -> None:
        p = FinancialMathPlugin()
        data = _parse(await p.calculate("1 + " * 200))
        assert data["ok"] is False
        assert "too long" in str(data["message"])

    @pytest.mark.anyio()
    async def test_division_by_zero(self) -> None:
        p = FinancialMathPlugin()
        data = _parse(await p.calculate("1 / 0"))
        assert data["ok"] is False

    @pytest.mark.anyio()
    async def test_syntax_error(self) -> None:
        p = FinancialMathPlugin()
        data = _parse(await p.calculate("2 + +"))
        assert data["ok"] is False

    @pytest.mark.anyio()
    async def test_function_call_rejected(self) -> None:
        p = FinancialMathPlugin()
        data = _parse(await p.calculate("print('hello')"))
        assert data["ok"] is False

    @pytest.mark.anyio()
    async def test_import_rejected(self) -> None:
        p = FinancialMathPlugin()
        data = _parse(await p.calculate("__import__('os')"))
        assert data["ok"] is False

    @pytest.mark.anyio()
    async def test_attribute_rejected(self) -> None:
        p = FinancialMathPlugin()
        data = _parse(await p.calculate("os.system"))
        assert data["ok"] is False

    @pytest.mark.anyio()
    async def test_unknown_variable(self) -> None:
        p = FinancialMathPlugin()
        data = _parse(await p.calculate("x + 1"))
        assert data["ok"] is False

    @pytest.mark.anyio()
    async def test_large_exponent(self) -> None:
        p = FinancialMathPlugin()
        data = _parse(await p.calculate("2 ** 10000"))
        assert data["ok"] is False

    @pytest.mark.anyio()
    async def test_string_rejected(self) -> None:
        p = FinancialMathPlugin()
        data = _parse(await p.calculate("'hello'"))
        assert data["ok"] is False

    @pytest.mark.anyio()
    async def test_lambda_rejected(self) -> None:
        p = FinancialMathPlugin()
        data = _parse(await p.calculate("lambda x: x"))
        assert data["ok"] is False

    @pytest.mark.anyio()
    async def test_result_too_large(self) -> None:
        """Result exceeding 1E308 should error."""
        p = FinancialMathPlugin()
        data = _parse(await p.calculate("10 ** 309"))
        assert data["ok"] is False
        assert "too large" in str(data["message"])


# ── Contract Tests (JSON Schema) ──


class TestStructuredOutputContract:
    """Every response must follow the structured JSON contract."""

    @pytest.mark.anyio()
    async def test_success_has_required_fields(self) -> None:
        p = FinancialMathPlugin()
        data = _parse(await p.calculate("1 + 1"))
        assert "ok" in data
        assert "action" in data
        assert "result" in data
        assert "precision" in data
        assert "message" in data
        assert data["ok"] is True
        assert isinstance(data["result"], str)

    @pytest.mark.anyio()
    async def test_error_has_required_fields(self) -> None:
        p = FinancialMathPlugin()
        data = _parse(await p.calculate("invalid"))
        assert "ok" in data
        assert "action" in data
        assert "message" in data
        assert data["ok"] is False
        assert data["action"] == "error"

    @pytest.mark.anyio()
    async def test_response_is_valid_json(self) -> None:
        p = FinancialMathPlugin()
        raw = await p.calculate("42 * 7")
        data = json.loads(raw)  # must not raise
        assert isinstance(data, dict)


# ── Backward Compatibility ──


class TestBackwardCompatibility:
    """Ensure old CalculatorPlugin import still works."""

    def test_import_works(self) -> None:
        from sovyx.plugins.official.calculator import CalculatorPlugin

        p = CalculatorPlugin()
        assert p.name == "calculator"  # compat wrapper keeps old name

    def test_safe_eval_import(self) -> None:
        from sovyx.plugins.official.calculator import _safe_eval as old_eval

        result = old_eval("2 + 3")
        assert result == Decimal(5)

    def test_eval_node_import(self) -> None:
        from sovyx.plugins.official.calculator import _eval_node as old_node

        assert old_node is not None


# ── Precision Showcase ──


class TestPrecisionShowcase:
    """Demonstrate Decimal precision vs float — the whole point."""

    @pytest.mark.anyio()
    async def test_01_plus_02_equals_03(self) -> None:
        """The classic float failure: 0.1 + 0.2 != 0.3 in float."""
        # Float fails:
        assert 0.1 + 0.2 != 0.3  # noqa: PLR2004

        # Decimal nails it:
        p = FinancialMathPlugin()
        data = _parse(await p.calculate("0.1 + 0.2"))
        assert data["result"] == "0.3"

    @pytest.mark.anyio()
    async def test_financial_subtraction(self) -> None:
        """386.66 - 1000 = -613.34 exactly."""
        p = FinancialMathPlugin()
        data = _parse(await p.calculate("386.66 - 1000"))
        assert data["result"] == "-613.34"

    @pytest.mark.anyio()
    async def test_percentage_precision(self) -> None:
        """17.3% of 4827.50 — float would introduce error."""
        p = FinancialMathPlugin()
        data = _parse(await p.calculate("4827.50 * 0.173"))
        result = Decimal(str(data["result"]))
        assert result == Decimal("835.1575")

    @pytest.mark.anyio()
    async def test_compound_interest_precision(self) -> None:
        """Compound interest: must be exact to the cent."""
        p = FinancialMathPlugin()
        data = _parse(await p.calculate("10000 * 1.0115 ** 12"))
        result = Decimal(str(data["result"]))
        # Verify it's close to the known correct value
        expected = Decimal("10000") * Decimal("1.0115") ** 12
        diff = abs(result - expected)
        assert diff < Decimal("0.01")
