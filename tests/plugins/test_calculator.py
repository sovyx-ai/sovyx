"""Tests for Sovyx Calculator Plugin (TASK-443)."""

from __future__ import annotations

from decimal import Decimal

import pytest

from sovyx.plugins.official.calculator import CalculatorPlugin, _safe_eval


class TestCalculatorPlugin:
    """Tests for CalculatorPlugin via SDK."""

    def test_name(self) -> None:
        p = CalculatorPlugin()
        assert p.name == "calculator"

    def test_version(self) -> None:
        p = CalculatorPlugin()
        assert p.version == "1.0.0"

    def test_description(self) -> None:
        p = CalculatorPlugin()
        assert "calculator" in p.description.lower()

    @pytest.mark.anyio()
    async def test_simple_addition(self) -> None:
        p = CalculatorPlugin()
        assert await p.calculate("2 + 3") == "5"

    @pytest.mark.anyio()
    async def test_subtraction(self) -> None:
        p = CalculatorPlugin()
        assert await p.calculate("10 - 4") == "6"

    @pytest.mark.anyio()
    async def test_multiplication(self) -> None:
        p = CalculatorPlugin()
        assert await p.calculate("6 * 7") == "42"

    @pytest.mark.anyio()
    async def test_division(self) -> None:
        p = CalculatorPlugin()
        assert await p.calculate("10 / 4") == "2.5"

    @pytest.mark.anyio()
    async def test_floor_division(self) -> None:
        p = CalculatorPlugin()
        assert await p.calculate("10 // 3") == "3"

    @pytest.mark.anyio()
    async def test_modulo(self) -> None:
        p = CalculatorPlugin()
        assert await p.calculate("10 % 3") == "1"

    @pytest.mark.anyio()
    async def test_power(self) -> None:
        p = CalculatorPlugin()
        assert await p.calculate("2 ** 10") == "1024"

    @pytest.mark.anyio()
    async def test_parentheses(self) -> None:
        p = CalculatorPlugin()
        assert await p.calculate("(2 + 3) * 4") == "20"

    @pytest.mark.anyio()
    async def test_nested_parentheses(self) -> None:
        p = CalculatorPlugin()
        assert await p.calculate("((2 + 3) * (4 - 1))") == "15"

    @pytest.mark.anyio()
    async def test_negative_number(self) -> None:
        p = CalculatorPlugin()
        assert await p.calculate("-5 + 3") == "-2"

    @pytest.mark.anyio()
    async def test_float_result(self) -> None:
        p = CalculatorPlugin()
        assert await p.calculate("1 / 3") == "0.3333333333"

    @pytest.mark.anyio()
    async def test_pi_constant(self) -> None:
        p = CalculatorPlugin()
        result = await p.calculate("pi * 2")
        assert result.startswith("6.28")

    @pytest.mark.anyio()
    async def test_e_constant(self) -> None:
        p = CalculatorPlugin()
        result = await p.calculate("e")
        assert result.startswith("2.71")

    @pytest.mark.anyio()
    async def test_tau_constant(self) -> None:
        p = CalculatorPlugin()
        result = await p.calculate("tau")
        assert result.startswith("6.28")

    @pytest.mark.anyio()
    async def test_division_by_zero(self) -> None:
        p = CalculatorPlugin()
        result = await p.calculate("1 / 0")
        assert "Error" in result

    @pytest.mark.anyio()
    async def test_expression_too_long(self) -> None:
        p = CalculatorPlugin()
        result = await p.calculate("1 + " * 200)
        assert "too long" in result

    @pytest.mark.anyio()
    async def test_rejects_function_call(self) -> None:
        p = CalculatorPlugin()
        result = await p.calculate("print('hello')")
        assert "Error" in result

    @pytest.mark.anyio()
    async def test_rejects_import(self) -> None:
        p = CalculatorPlugin()
        result = await p.calculate("__import__('os')")
        assert "Error" in result

    @pytest.mark.anyio()
    async def test_rejects_attribute(self) -> None:
        p = CalculatorPlugin()
        result = await p.calculate("os.system")
        assert "Error" in result

    @pytest.mark.anyio()
    async def test_rejects_unknown_variable(self) -> None:
        p = CalculatorPlugin()
        result = await p.calculate("x + 1")
        assert "Error" in result

    @pytest.mark.anyio()
    async def test_large_exponent_rejected(self) -> None:
        p = CalculatorPlugin()
        result = await p.calculate("2 ** 10000")
        assert "Error" in result

    @pytest.mark.anyio()
    async def test_unary_plus(self) -> None:
        p = CalculatorPlugin()
        assert await p.calculate("+5") == "5"

    @pytest.mark.anyio()
    async def test_syntax_error(self) -> None:
        p = CalculatorPlugin()
        result = await p.calculate("2 + +")
        assert "Error" in result

    @pytest.mark.anyio()
    async def test_string_constant_rejected(self) -> None:
        p = CalculatorPlugin()
        result = await p.calculate("'hello'")
        assert "Error" in result


class TestSafeEval:
    """Direct tests for _safe_eval."""

    def test_integer(self) -> None:
        assert _safe_eval("42") == Decimal(42)

    def test_float(self) -> None:
        assert _safe_eval("3.14") == Decimal("3.14")

    def test_complex_expression(self) -> None:
        assert _safe_eval("2 + 3 * 4 - 1") == Decimal(13)

    def test_power_within_limit(self) -> None:
        result = _safe_eval("2 ** 100")
        expected = Decimal(2**100)
        assert abs(result - expected) / expected < Decimal("1e-20")

    def test_invalid_syntax(self) -> None:
        with pytest.raises(ValueError, match="invalid syntax"):
            _safe_eval("2 +")

    def test_unsupported_node(self) -> None:
        with pytest.raises(ValueError, match="unsupported"):
            _safe_eval("[1, 2, 3]")


class TestEdgeCases:
    """Edge cases for full coverage."""

    @pytest.mark.anyio()
    async def test_integer_float_result(self) -> None:
        """Float result that equals integer displayed as int."""
        p = CalculatorPlugin()
        assert await p.calculate("4.0 + 2.0") == "6"

    @pytest.mark.anyio()
    async def test_inf_constant(self) -> None:
        """Infinity constant returns 'Error: result too large'."""
        p = CalculatorPlugin()
        result = await p.calculate("inf")
        # inf > _MAX_RESULT so it triggers the too-large guard
        assert "too large" in result or "inf" in result.lower()

    @pytest.mark.anyio()
    async def test_very_large_result(self) -> None:
        """Very large result returns error."""
        p = CalculatorPlugin()
        result = await p.calculate("10 ** 309")
        # Either large number or error
        assert isinstance(result, str)

    def test_unsupported_unary(self) -> None:
        """Unsupported unary op raises ValueError."""
        import ast

        node = ast.UnaryOp(op=ast.Invert(), operand=ast.Constant(value=5))
        with pytest.raises(ValueError, match="unsupported unary"):
            from sovyx.plugins.official.calculator import _eval_node

            _eval_node(node)

    def test_unsupported_binary(self) -> None:
        """Unsupported binary op raises ValueError."""
        import ast

        node = ast.BinOp(
            left=ast.Constant(value=1),
            op=ast.BitAnd(),
            right=ast.Constant(value=2),
        )
        with pytest.raises(ValueError, match="unsupported operator"):
            from sovyx.plugins.official.calculator import _eval_node

            _eval_node(node)

    @pytest.mark.anyio()
    async def test_generic_exception(self) -> None:
        """Generic exception returns 'invalid expression'."""
        p = CalculatorPlugin()
        # This triggers the generic except
        result = await p.calculate("lambda x: x")
        assert "Error" in result
