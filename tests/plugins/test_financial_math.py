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


# ── Percentage Operations ──


class TestPercentageOf:
    """Test percentage 'of' mode."""

    @pytest.mark.anyio()
    async def test_basic(self) -> None:
        p = FinancialMathPlugin()
        data = _parse(await p.percentage(mode="of", rate=17.3, value=4827.50))
        assert data["ok"] is True
        assert data["mode"] == "of"
        assert data["result"] == "835.1575"

    @pytest.mark.anyio()
    async def test_whole_percent(self) -> None:
        p = FinancialMathPlugin()
        data = _parse(await p.percentage(mode="of", rate=50, value=200))
        assert data["result"] == "100"

    @pytest.mark.anyio()
    async def test_small_percent(self) -> None:
        p = FinancialMathPlugin()
        data = _parse(await p.percentage(mode="of", rate=0.5, value=10000))
        assert data["result"] == "50"

    @pytest.mark.anyio()
    async def test_missing_rate(self) -> None:
        p = FinancialMathPlugin()
        data = _parse(await p.percentage(mode="of", value=100))
        assert data["ok"] is False
        assert "rate" in str(data["message"])

    @pytest.mark.anyio()
    async def test_missing_value(self) -> None:
        p = FinancialMathPlugin()
        data = _parse(await p.percentage(mode="of", rate=10))
        assert data["ok"] is False
        assert "value" in str(data["message"])


class TestPercentageChange:
    """Test percentage 'change' mode."""

    @pytest.mark.anyio()
    async def test_decrease(self) -> None:
        p = FinancialMathPlugin()
        data = _parse(await p.percentage(mode="change", from_value=67500, to_value=58200))
        assert data["ok"] is True
        result = Decimal(str(data["result"]))
        # (58200 - 67500) / 67500 * 100 = -13.777...%
        assert result < Decimal("-13.7")
        assert result > Decimal("-13.8")

    @pytest.mark.anyio()
    async def test_increase(self) -> None:
        p = FinancialMathPlugin()
        data = _parse(await p.percentage(mode="change", from_value=100, to_value=150))
        assert data["result"] == "50"

    @pytest.mark.anyio()
    async def test_no_change(self) -> None:
        p = FinancialMathPlugin()
        data = _parse(await p.percentage(mode="change", from_value=100, to_value=100))
        assert data["result"] == "0"

    @pytest.mark.anyio()
    async def test_zero_from_value(self) -> None:
        p = FinancialMathPlugin()
        data = _parse(await p.percentage(mode="change", from_value=0, to_value=100))
        assert data["ok"] is False
        assert "zero" in str(data["message"]).lower()

    @pytest.mark.anyio()
    async def test_missing_params(self) -> None:
        p = FinancialMathPlugin()
        data = _parse(await p.percentage(mode="change", from_value=100))
        assert data["ok"] is False


class TestPercentageMarkup:
    """Test percentage 'markup' mode."""

    @pytest.mark.anyio()
    async def test_basic_markup(self) -> None:
        p = FinancialMathPlugin()
        data = _parse(await p.percentage(mode="markup", rate=40, cost=100))
        assert data["ok"] is True
        assert data["result"] == "140"
        assert data["markup_amount"] == "40"

    @pytest.mark.anyio()
    async def test_decimal_markup(self) -> None:
        p = FinancialMathPlugin()
        data = _parse(await p.percentage(mode="markup", rate=33.33, cost=150))
        assert data["ok"] is True
        result = Decimal(str(data["result"]))
        assert result == Decimal("199.995")

    @pytest.mark.anyio()
    async def test_zero_markup(self) -> None:
        p = FinancialMathPlugin()
        data = _parse(await p.percentage(mode="markup", rate=0, cost=100))
        assert data["result"] == "100"


class TestGrossMargin:
    """Test percentage 'gross_margin' mode."""

    @pytest.mark.anyio()
    async def test_basic_margin(self) -> None:
        p = FinancialMathPlugin()
        data = _parse(await p.percentage(mode="gross_margin", revenue=140, cost=100))
        assert data["ok"] is True
        result = Decimal(str(data["result"]))
        # (140 - 100) / 140 * 100 = 28.571...%
        assert result > Decimal("28.57")
        assert result < Decimal("28.58")

    @pytest.mark.anyio()
    async def test_zero_margin(self) -> None:
        p = FinancialMathPlugin()
        data = _parse(await p.percentage(mode="gross_margin", revenue=100, cost=100))
        assert data["result"] == "0"

    @pytest.mark.anyio()
    async def test_negative_margin(self) -> None:
        """Loss scenario: cost > revenue."""
        p = FinancialMathPlugin()
        data = _parse(await p.percentage(mode="gross_margin", revenue=80, cost=100))
        result = Decimal(str(data["result"]))
        assert result < Decimal(0)

    @pytest.mark.anyio()
    async def test_zero_revenue(self) -> None:
        p = FinancialMathPlugin()
        data = _parse(await p.percentage(mode="gross_margin", revenue=0, cost=100))
        assert data["ok"] is False


class TestNetMargin:
    """Test percentage 'net_margin' mode."""

    @pytest.mark.anyio()
    async def test_basic(self) -> None:
        p = FinancialMathPlugin()
        data = _parse(await p.percentage(mode="net_margin", revenue=1000, net_income=150))
        assert data["ok"] is True
        assert data["result"] == "15"

    @pytest.mark.anyio()
    async def test_loss(self) -> None:
        p = FinancialMathPlugin()
        data = _parse(await p.percentage(mode="net_margin", revenue=1000, net_income=-50))
        result = Decimal(str(data["result"]))
        assert result == Decimal("-5")

    @pytest.mark.anyio()
    async def test_zero_revenue(self) -> None:
        p = FinancialMathPlugin()
        data = _parse(await p.percentage(mode="net_margin", revenue=0, net_income=100))
        assert data["ok"] is False


class TestPercentageEdgeCases:
    """Edge cases and error handling for percentage tool."""

    @pytest.mark.anyio()
    async def test_unknown_mode(self) -> None:
        p = FinancialMathPlugin()
        data = _parse(await p.percentage(mode="invalid"))
        assert data["ok"] is False
        assert "unknown mode" in str(data["message"])
        assert "of" in str(data["message"])  # lists valid modes

    @pytest.mark.anyio()
    async def test_message_present(self) -> None:
        p = FinancialMathPlugin()
        data = _parse(await p.percentage(mode="of", rate=10, value=200))
        assert "message" in data
        assert "20" in str(data["message"])

    @pytest.mark.anyio()
    async def test_large_percentage(self) -> None:
        """500% markup."""
        p = FinancialMathPlugin()
        data = _parse(await p.percentage(mode="markup", rate=500, cost=100))
        assert data["result"] == "600"

    @pytest.mark.anyio()
    async def test_fractional_cent_precision(self) -> None:
        """0.01% of $1,000,000 = $100 exactly."""
        p = FinancialMathPlugin()
        data = _parse(await p.percentage(mode="of", rate=0.01, value=1000000))
        assert data["result"] == "100"


# ── Interest & Growth ──


class TestSimpleInterest:
    """Test interest 'simple' mode."""

    @pytest.mark.anyio()
    async def test_basic(self) -> None:
        p = FinancialMathPlugin()
        data = _parse(await p.interest(mode="simple", principal=10000, rate=5, periods=3))
        assert data["ok"] is True
        assert data["mode"] == "simple"
        # 10000 * 0.05 * 3 = 1500 interest, 11500 total
        assert data["interest"] == "1500"
        assert data["total"] == "11500"
        assert data["result"] == "11500"

    @pytest.mark.anyio()
    async def test_decimal_rate(self) -> None:
        """Rate ≤1 treated as decimal: 0.05 = 5%."""
        p = FinancialMathPlugin()
        data = _parse(await p.interest(mode="simple", principal=10000, rate=0.05, periods=3))
        assert data["total"] == "11500"

    @pytest.mark.anyio()
    async def test_fractional_periods(self) -> None:
        p = FinancialMathPlugin()
        data = _parse(await p.interest(mode="simple", principal=1000, rate=12, periods=0.5))
        # 1000 * 0.12 * 0.5 = 60
        assert data["interest"] == "60"

    @pytest.mark.anyio()
    async def test_missing_principal(self) -> None:
        p = FinancialMathPlugin()
        data = _parse(await p.interest(mode="simple", rate=5, periods=3))
        assert data["ok"] is False
        assert "principal" in str(data["message"])


class TestCompoundInterest:
    """Test interest 'compound' mode."""

    @pytest.mark.anyio()
    async def test_annual_compound(self) -> None:
        p = FinancialMathPlugin()
        data = _parse(
            await p.interest(
                mode="compound",
                principal=10000,
                rate=13.75,
                periods=1,
            )
        )
        assert data["ok"] is True
        # 10000 * (1 + 0.1375)^1 = 11375
        assert data["total"] == "11375"

    @pytest.mark.anyio()
    async def test_monthly_compound(self) -> None:
        """R$10k at 1.15%/month for 12 months."""
        p = FinancialMathPlugin()
        data = _parse(
            await p.interest(
                mode="compound",
                principal=10000,
                rate=0.0115,
                periods=12,
            )
        )
        assert data["ok"] is True
        result = Decimal(str(data["total"]))
        # 10000 * 1.0115^12 ≈ 11470.72
        assert result > Decimal("11470")
        assert result < Decimal("11471")

    @pytest.mark.anyio()
    async def test_quarterly_compounding(self) -> None:
        """10% annual, compounded quarterly, 2 years."""
        p = FinancialMathPlugin()
        data = _parse(
            await p.interest(
                mode="compound",
                principal=1000,
                rate=10,
                periods=2,
                compounds_per_period=4,
            )
        )
        assert data["ok"] is True
        result = Decimal(str(data["total"]))
        # 1000 * (1 + 0.10/4)^(4*2) = 1000 * 1.025^8 ≈ 1218.40
        assert result > Decimal("1218")
        assert result < Decimal("1219")

    @pytest.mark.anyio()
    async def test_zero_compounds_rejected(self) -> None:
        p = FinancialMathPlugin()
        data = _parse(
            await p.interest(
                mode="compound",
                principal=1000,
                rate=5,
                periods=1,
                compounds_per_period=0,
            )
        )
        assert data["ok"] is False

    @pytest.mark.anyio()
    async def test_message_has_breakdown(self) -> None:
        p = FinancialMathPlugin()
        data = _parse(
            await p.interest(
                mode="compound",
                principal=5000,
                rate=8,
                periods=3,
            )
        )
        assert "interest" in str(data["message"]).lower()


class TestCAGR:
    """Test interest 'cagr' mode."""

    @pytest.mark.anyio()
    async def test_basic(self) -> None:
        """$5k → $42k over 6 years."""
        p = FinancialMathPlugin()
        data = _parse(
            await p.interest(
                mode="cagr",
                initial_value=5000,
                final_value=42000,
                years=6,
            )
        )
        assert data["ok"] is True
        result = Decimal(str(data["cagr_percent"]))
        # (42000/5000)^(1/6) - 1 ≈ 0.4247 → 42.47%
        assert result > Decimal("42")
        assert result < Decimal("43")

    @pytest.mark.anyio()
    async def test_double(self) -> None:
        """$100 → $200 over 1 year = 100% CAGR."""
        p = FinancialMathPlugin()
        data = _parse(
            await p.interest(
                mode="cagr",
                initial_value=100,
                final_value=200,
                years=1,
            )
        )
        assert data["result"] == "100"

    @pytest.mark.anyio()
    async def test_loss(self) -> None:
        """$100 → $50 = negative CAGR."""
        p = FinancialMathPlugin()
        data = _parse(
            await p.interest(
                mode="cagr",
                initial_value=100,
                final_value=50,
                years=2,
            )
        )
        result = Decimal(str(data["result"]))
        assert result < Decimal(0)

    @pytest.mark.anyio()
    async def test_zero_initial_rejected(self) -> None:
        p = FinancialMathPlugin()
        data = _parse(
            await p.interest(
                mode="cagr",
                initial_value=0,
                final_value=100,
                years=1,
            )
        )
        assert data["ok"] is False

    @pytest.mark.anyio()
    async def test_zero_years_rejected(self) -> None:
        p = FinancialMathPlugin()
        data = _parse(
            await p.interest(
                mode="cagr",
                initial_value=100,
                final_value=200,
                years=0,
            )
        )
        assert data["ok"] is False

    @pytest.mark.anyio()
    async def test_total_return_included(self) -> None:
        p = FinancialMathPlugin()
        data = _parse(
            await p.interest(
                mode="cagr",
                initial_value=1000,
                final_value=1500,
                years=3,
            )
        )
        assert "total_return_percent" in data
        assert data["total_return_percent"] == "50"


class TestRuleOf72:
    """Test interest 'rule_of_72' mode."""

    @pytest.mark.anyio()
    async def test_basic(self) -> None:
        """At 8%/year, doubles in ~9 years."""
        p = FinancialMathPlugin()
        data = _parse(await p.interest(mode="rule_of_72", rate=8))
        assert data["ok"] is True
        assert data["result"] == "9"

    @pytest.mark.anyio()
    async def test_decimal_rate(self) -> None:
        """0.08 auto-converts to 8%."""
        p = FinancialMathPlugin()
        data = _parse(await p.interest(mode="rule_of_72", rate=0.08))
        assert data["result"] == "9"

    @pytest.mark.anyio()
    async def test_high_rate(self) -> None:
        """At 24%, doubles in 3 years."""
        p = FinancialMathPlugin()
        data = _parse(await p.interest(mode="rule_of_72", rate=24))
        assert data["result"] == "3"

    @pytest.mark.anyio()
    async def test_zero_rate_rejected(self) -> None:
        p = FinancialMathPlugin()
        data = _parse(await p.interest(mode="rule_of_72", rate=0))
        assert data["ok"] is False

    @pytest.mark.anyio()
    async def test_negative_rate_rejected(self) -> None:
        p = FinancialMathPlugin()
        data = _parse(await p.interest(mode="rule_of_72", rate=-5))
        assert data["ok"] is False


class TestInterestEdgeCases:
    """Edge cases for interest tool."""

    @pytest.mark.anyio()
    async def test_unknown_mode(self) -> None:
        p = FinancialMathPlugin()
        data = _parse(await p.interest(mode="invalid"))
        assert data["ok"] is False
        assert "unknown mode" in str(data["message"])

    @pytest.mark.anyio()
    async def test_compound_vs_simple(self) -> None:
        """Compound must always be >= simple for same params."""
        p = FinancialMathPlugin()
        simple = _parse(
            await p.interest(
                mode="simple",
                principal=10000,
                rate=10,
                periods=5,
            )
        )
        compound = _parse(
            await p.interest(
                mode="compound",
                principal=10000,
                rate=10,
                periods=5,
            )
        )
        assert Decimal(str(compound["total"])) >= Decimal(str(simple["total"]))

    @pytest.mark.anyio()
    async def test_negative_rate_rejected(self) -> None:
        p = FinancialMathPlugin()
        data = _parse(await p.interest(mode="simple", principal=1000, rate=-5, periods=1))
        assert data["ok"] is False


# ── Time Value of Money ──


class TestNPV:
    """Test tvm 'npv' mode."""

    @pytest.mark.anyio()
    async def test_basic(self) -> None:
        """Investment of $100k, returns: 25k, 35k, 40k, 30k at 12%."""
        p = FinancialMathPlugin()
        data = _parse(
            await p.tvm(
                mode="npv",
                rate=12,
                cashflows=[-100000, 25000, 35000, 40000, 30000],
            )
        )
        assert data["ok"] is True
        result = Decimal(str(data["result"]))
        # NPV at 12% ≈ -2240 (slightly negative)
        assert result > Decimal("-2500")
        assert result < Decimal("-2000")
        assert data["profitable"] is False

    @pytest.mark.anyio()
    async def test_unprofitable(self) -> None:
        p = FinancialMathPlugin()
        data = _parse(
            await p.tvm(
                mode="npv",
                rate=20,
                cashflows=[-100000, 25000, 25000, 25000],
            )
        )
        assert data["profitable"] is False

    @pytest.mark.anyio()
    async def test_zero_rate(self) -> None:
        """At 0%, NPV = sum of cashflows."""
        p = FinancialMathPlugin()
        data = _parse(
            await p.tvm(
                mode="npv",
                rate=0,
                cashflows=[-100, 60, 60],
            )
        )
        result = Decimal(str(data["result"]))
        assert result == Decimal(20)

    @pytest.mark.anyio()
    async def test_empty_cashflows(self) -> None:
        p = FinancialMathPlugin()
        data = _parse(await p.tvm(mode="npv", rate=10, cashflows=[]))
        assert data["ok"] is False


class TestIRR:
    """Test tvm 'irr' mode."""

    @pytest.mark.anyio()
    async def test_basic(self) -> None:
        """Same cashflows as NPV test — IRR should be ~13.2%."""
        p = FinancialMathPlugin()
        data = _parse(
            await p.tvm(
                mode="irr",
                cashflows=[-100000, 25000, 35000, 40000, 30000],
            )
        )
        assert data["ok"] is True
        result = Decimal(str(data["irr_percent"]))
        # IRR ≈ 10.7%
        assert result > Decimal("10")
        assert result < Decimal("12")

    @pytest.mark.anyio()
    async def test_npv_at_irr_is_zero(self) -> None:
        """NPV evaluated at IRR should be approximately zero."""
        p = FinancialMathPlugin()
        cfs = [-100000, 25000, 35000, 40000, 30000]
        irr_data = _parse(await p.tvm(mode="irr", cashflows=cfs))
        irr_rate = float(str(irr_data["irr_percent"]))

        npv_data = _parse(await p.tvm(mode="npv", rate=irr_rate, cashflows=cfs))
        npv = Decimal(str(npv_data["result"]))
        assert abs(npv) < Decimal("1")  # ~zero

    @pytest.mark.anyio()
    async def test_simple_double(self) -> None:
        """-100, +200 → IRR = 100%."""
        p = FinancialMathPlugin()
        data = _parse(await p.tvm(mode="irr", cashflows=[-100, 200]))
        result = Decimal(str(data["irr_percent"]))
        assert abs(result - Decimal(100)) < Decimal("0.01")

    @pytest.mark.anyio()
    async def test_too_few_cashflows(self) -> None:
        p = FinancialMathPlugin()
        data = _parse(await p.tvm(mode="irr", cashflows=[-100]))
        assert data["ok"] is False


class TestPV:
    """Test tvm 'pv' mode."""

    @pytest.mark.anyio()
    async def test_basic(self) -> None:
        """FV=10000, rate=10%, n=5 → PV≈6209.21."""
        p = FinancialMathPlugin()
        data = _parse(
            await p.tvm(
                mode="pv",
                future_value=10000,
                rate=10,
                periods=5,
            )
        )
        assert data["ok"] is True
        result = Decimal(str(data["result"]))
        assert result > Decimal("6209")
        assert result < Decimal("6210")

    @pytest.mark.anyio()
    async def test_pv_fv_roundtrip(self) -> None:
        """PV→FV→PV should return original value."""
        p = FinancialMathPlugin()
        pv_data = _parse(
            await p.tvm(
                mode="pv",
                future_value=10000,
                rate=8,
                periods=3,
            )
        )
        pv = float(str(pv_data["result"]))
        fv_data = _parse(
            await p.tvm(
                mode="fv",
                present_value=pv,
                rate=8,
                periods=3,
            )
        )
        fv = Decimal(str(fv_data["result"]))
        assert abs(fv - Decimal(10000)) < Decimal("1")


class TestFV:
    """Test tvm 'fv' mode."""

    @pytest.mark.anyio()
    async def test_basic(self) -> None:
        """PV=1000, 10%, 10 periods."""
        p = FinancialMathPlugin()
        data = _parse(
            await p.tvm(
                mode="fv",
                present_value=1000,
                rate=10,
                periods=10,
            )
        )
        assert data["ok"] is True
        result = Decimal(str(data["result"]))
        # 1000 * 1.10^10 ≈ 2593.74
        assert result > Decimal("2593")
        assert result < Decimal("2594")


class TestAnnuityPV:
    """Test tvm 'annuity_pv' mode."""

    @pytest.mark.anyio()
    async def test_basic(self) -> None:
        """PMT=1000, 1%/period, 12 periods → PV≈11255.08."""
        p = FinancialMathPlugin()
        data = _parse(
            await p.tvm(
                mode="annuity_pv",
                payment=1000,
                rate=0.01,
                periods=12,
            )
        )
        assert data["ok"] is True
        result = Decimal(str(data["result"]))
        assert result > Decimal("11255")
        assert result < Decimal("11256")

    @pytest.mark.anyio()
    async def test_zero_rate(self) -> None:
        """At 0%, annuity PV = PMT * n."""
        p = FinancialMathPlugin()
        data = _parse(
            await p.tvm(
                mode="annuity_pv",
                payment=500,
                rate=0,
                periods=10,
            )
        )
        assert data["result"] == "5000"


class TestAnnuityFV:
    """Test tvm 'annuity_fv' mode."""

    @pytest.mark.anyio()
    async def test_basic(self) -> None:
        """PMT=1000, 1%/period, 12 periods."""
        p = FinancialMathPlugin()
        data = _parse(
            await p.tvm(
                mode="annuity_fv",
                payment=1000,
                rate=0.01,
                periods=12,
            )
        )
        assert data["ok"] is True
        result = Decimal(str(data["result"]))
        # ≈ 12682.50
        assert result > Decimal("12682")
        assert result < Decimal("12683")

    @pytest.mark.anyio()
    async def test_zero_rate(self) -> None:
        p = FinancialMathPlugin()
        data = _parse(
            await p.tvm(
                mode="annuity_fv",
                payment=500,
                rate=0,
                periods=10,
            )
        )
        assert data["result"] == "5000"


class TestTVMEdgeCases:
    """Edge cases for TVM tool."""

    @pytest.mark.anyio()
    async def test_unknown_mode(self) -> None:
        p = FinancialMathPlugin()
        data = _parse(await p.tvm(mode="invalid"))
        assert data["ok"] is False

    @pytest.mark.anyio()
    async def test_missing_params(self) -> None:
        p = FinancialMathPlugin()
        data = _parse(await p.tvm(mode="pv"))
        assert data["ok"] is False
        assert "missing" in str(data["message"])
