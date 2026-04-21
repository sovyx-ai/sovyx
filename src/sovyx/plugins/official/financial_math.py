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
from typing import TYPE_CHECKING, ClassVar

if TYPE_CHECKING:
    from collections.abc import Sequence

from sovyx.plugins.sdk import ISovyxPlugin, tool

# ── Constants ──

_PRECISION = 28
_MAX_EXPRESSION_LEN = 500
_MAX_EXPONENT = 1000
_MAX_RESULT = Decimal("1E308")
_MAX_PERIODS = 1200  # 100 years monthly
_MAX_CASHFLOWS = 1000
_MAX_RETURNS = 10000
_MAX_VALUE = Decimal("1E15")  # quadrillion — sanity cap
_MIN_RATE = Decimal("-0.99")  # -99% floor (avoid div-by-zero)

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


def _validate_value(val: Decimal, name: str = "value") -> None:
    """Check value is within sane bounds."""
    if not val.is_finite():
        msg = f"{name}: must be a finite number"
        raise _ValidationError(msg)
    if abs(val) > _MAX_VALUE:
        msg = f"{name}: value too large (max ±{_MAX_VALUE})"
        raise _ValidationError(msg)


def _validate_list_len(
    lst: Sequence[object],
    name: str,
    max_len: int,
) -> None:
    """Check list is not too long."""
    if len(lst) > max_len:
        msg = f"{name}: too many items ({len(lst)} > {max_len})"
        raise _ValidationError(msg)


def _validate_periods(n: int, name: str = "months") -> None:
    """Check periods within bounds."""
    if n > _MAX_PERIODS:
        msg = f"{name}: too many periods ({n} > {_MAX_PERIODS})"
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

    # ── Interest & Growth ──

    @tool(
        description=(
            "Interest and growth calculations. Modes: "
            "'simple' (P*r*t), 'compound' (P*(1+r/n)^(n*t)), "
            "'cagr' (annualized return from initial→final over years), "
            "'rule_of_72' (years to double at given rate). "
            "Rates auto-detected: >1 treated as percentage, ≤1 as decimal. "
            "Example: interest(mode='compound', principal=10000, rate=13.75, periods=12)"
        ),
    )
    async def interest(
        self,
        mode: str,
        *,
        principal: float | None = None,
        rate: float | None = None,
        periods: float | None = None,
        compounds_per_period: int = 1,
        initial_value: float | None = None,
        final_value: float | None = None,
        years: float | None = None,
    ) -> str:
        """Interest and growth calculations with Decimal precision.

        Args:
            mode: 'simple', 'compound', 'cagr', 'rule_of_72'.
            principal: Starting amount (simple/compound).
            rate: Interest rate — auto-detect: >1 = percentage, ≤1 = decimal.
            periods: Number of periods (simple/compound).
            compounds_per_period: Compounding frequency per period (default 1).
            initial_value: Starting value (cagr).
            final_value: Ending value (cagr).
            years: Number of years (cagr).

        Returns:
            JSON with result, breakdown, and step-by-step explanation.
        """
        mode = mode.strip().lower()

        try:
            if mode == "simple":
                return self._interest_simple(principal, rate, periods)
            if mode == "compound":
                return self._interest_compound(
                    principal,
                    rate,
                    periods,
                    compounds_per_period,
                )
            if mode == "cagr":
                return self._interest_cagr(initial_value, final_value, years)
            if mode == "rule_of_72":
                return self._interest_rule72(rate)
        except _ValidationError as e:
            return _err(str(e))
        except (ZeroDivisionError, DecimalException, OverflowError) as e:
            return _err(f"calculation error: {e}")

        valid = "simple, compound, cagr, rule_of_72"
        return _err(f"unknown mode: '{mode}'. Valid: {valid}")

    # ── Interest Internals ──

    @staticmethod
    def _normalize_rate(rate: float | None) -> Decimal:
        """Convert rate to decimal form. >1 treated as percentage."""
        _require(rate=rate)
        d = _to_decimal(rate)
        if d < _ZERO:
            msg = "rate cannot be negative"
            raise _ValidationError(msg)
        # Auto-detect: 13.75 → 0.1375, 0.05 → 0.05
        if d > Decimal(1):
            return d / _HUNDRED
        return d

    @staticmethod
    def _interest_simple(
        principal: float | None,
        rate: float | None,
        periods: float | None,
    ) -> str:
        """Simple interest: A = P * (1 + r * t)."""
        _require(principal=principal, rate=rate, periods=periods)
        p = _to_decimal(principal)
        r = FinancialMathPlugin._normalize_rate(rate)
        t = _to_decimal(periods)
        interest_amount = p * r * t
        total = p + interest_amount
        return _ok(
            "interest",
            mode="simple",
            principal=_format_decimal(p),
            rate=_format_decimal(r * _HUNDRED) + "%",
            rate_decimal=_format_decimal(r),
            periods=_format_decimal(t),
            interest=_format_decimal(interest_amount),
            total=_format_decimal(total),
            result=_format_decimal(total),
            message=(
                f"P={_format_decimal(p)}, r={_format_decimal(r * _HUNDRED)}%, "
                f"t={_format_decimal(t)} → "
                f"Interest={_format_decimal(interest_amount)}, "
                f"Total={_format_decimal(total)}"
            ),
        )

    @staticmethod
    def _interest_compound(
        principal: float | None,
        rate: float | None,
        periods: float | None,
        n: int = 1,
    ) -> str:
        """Compound interest: A = P * (1 + r/n)^(n*t)."""
        _require(principal=principal, rate=rate, periods=periods)
        p = _to_decimal(principal)
        r = FinancialMathPlugin._normalize_rate(rate)
        t = _to_decimal(periods)
        d_n = _to_decimal(n)
        if d_n <= _ZERO:
            msg = "compounds_per_period must be positive"
            raise _ValidationError(msg)

        # A = P * (1 + r/n)^(n*t)
        rate_per_compound = r / d_n
        growth_factor = (Decimal(1) + rate_per_compound) ** (d_n * t)
        total = p * growth_factor
        interest_amount = total - p

        return _ok(
            "interest",
            mode="compound",
            principal=_format_decimal(p),
            rate=_format_decimal(r * _HUNDRED) + "%",
            rate_decimal=_format_decimal(r),
            periods=_format_decimal(t),
            compounds_per_period=str(n),
            growth_factor=_format_decimal(growth_factor),
            interest=_format_decimal(interest_amount),
            total=_format_decimal(total),
            result=_format_decimal(total),
            message=(
                f"P={_format_decimal(p)}, "
                f"r={_format_decimal(r * _HUNDRED)}%/period, "
                f"n={n}, t={_format_decimal(t)} → "
                f"Total={_format_decimal(total)} "
                f"(+{_format_decimal(interest_amount)} interest)"
            ),
        )

    @staticmethod
    def _interest_cagr(
        initial_value: float | None,
        final_value: float | None,
        years: float | None,
    ) -> str:
        """CAGR = (final/initial)^(1/years) - 1."""
        _require(
            initial_value=initial_value,
            final_value=final_value,
            years=years,
        )
        v0 = _to_decimal(initial_value)
        vf = _to_decimal(final_value)
        y = _to_decimal(years)
        if v0 <= _ZERO:
            msg = "initial_value must be positive"
            raise _ValidationError(msg)
        if y <= _ZERO:
            msg = "years must be positive"
            raise _ValidationError(msg)

        # CAGR = (Vf/V0)^(1/y) - 1
        ratio = vf / v0
        exponent = Decimal(1) / y
        cagr = ratio**exponent - Decimal(1)
        cagr_pct = cagr * _HUNDRED
        total_return = (vf - v0) / v0 * _HUNDRED

        return _ok(
            "interest",
            mode="cagr",
            initial_value=_format_decimal(v0),
            final_value=_format_decimal(vf),
            years=_format_decimal(y),
            cagr_decimal=_format_decimal(cagr),
            cagr_percent=_format_decimal(cagr_pct),
            total_return_percent=_format_decimal(total_return),
            result=_format_decimal(cagr_pct),
            message=(
                f"{_format_decimal(v0)} → {_format_decimal(vf)} "
                f"over {_format_decimal(y)} years: "
                f"CAGR = {_format_decimal(cagr_pct)}%/year "
                f"(total return: {_format_decimal(total_return)}%)"
            ),
        )

    @staticmethod
    def _interest_rule72(rate: float | None) -> str:
        """Rule of 72: years to double ≈ 72 / rate%."""
        _require(rate=rate)
        d = _to_decimal(rate)
        # Accept both percentage (8) and decimal (0.08)
        if d > _ZERO and d <= Decimal(1):
            d = d * _HUNDRED  # convert 0.08 → 8

        if d <= _ZERO:
            msg = "rate must be positive"
            raise _ValidationError(msg)

        years_to_double = Decimal(72) / d

        return _ok(
            "interest",
            mode="rule_of_72",
            rate=_format_decimal(d) + "%",
            years_to_double=_format_decimal(years_to_double),
            result=_format_decimal(years_to_double),
            message=(
                f"At {_format_decimal(d)}% per year, "
                f"money doubles in ~{_format_decimal(years_to_double)} years"
            ),
        )

    # ── Time Value of Money ──

    @tool(
        description=(
            "Time value of money calculations. Modes: "
            "'npv' (net present value from cashflows), "
            "'irr' (internal rate of return via Newton-Raphson), "
            "'pv' (present value of a future amount), "
            "'fv' (future value of a present amount), "
            "'annuity_pv' (present value of periodic payments), "
            "'annuity_fv' (future value of periodic payments). "
            "Example: tvm(mode='npv', rate=12, cashflows=[-100000, 25000, 35000, 40000, 30000])"
        ),
    )
    async def tvm(
        self,
        mode: str,
        *,
        rate: float | None = None,
        cashflows: list[float] | None = None,
        present_value: float | None = None,
        future_value: float | None = None,
        payment: float | None = None,
        periods: float | None = None,
    ) -> str:
        """Time value of money with Decimal precision.

        Args:
            mode: 'npv', 'irr', 'pv', 'fv', 'annuity_pv', 'annuity_fv'.
            rate: Discount/interest rate (auto-detect: >1 = %, ≤1 = decimal).
            cashflows: List of cashflows for NPV/IRR (first is usually negative).
            present_value: PV amount (for fv mode).
            future_value: FV amount (for pv mode).
            payment: Periodic payment (for annuity modes).
            periods: Number of periods.

        Returns:
            JSON with result and breakdown.
        """
        mode = mode.strip().lower()

        try:
            if mode == "npv":
                return self._tvm_npv(rate, cashflows)
            if mode == "irr":
                return self._tvm_irr(cashflows)
            if mode == "pv":
                return self._tvm_pv(future_value, rate, periods)
            if mode == "fv":
                return self._tvm_fv(present_value, rate, periods)
            if mode == "annuity_pv":
                return self._tvm_annuity_pv(payment, rate, periods)
            if mode == "annuity_fv":
                return self._tvm_annuity_fv(payment, rate, periods)
        except _ValidationError as e:
            return _err(str(e))
        except (ZeroDivisionError, DecimalException, OverflowError) as e:
            return _err(f"calculation error: {e}")

        valid = "npv, irr, pv, fv, annuity_pv, annuity_fv"
        return _err(f"unknown mode: '{mode}'. Valid: {valid}")

    # ── TVM Internals ──

    @staticmethod
    def _tvm_npv(
        rate: float | None,
        cashflows: list[float] | None,
    ) -> str:
        """NPV = Σ CF_t / (1+r)^t."""
        _require(rate=rate, cashflows=cashflows)
        assert cashflows is not None  # for mypy
        if not cashflows:
            msg = "cashflows cannot be empty"
            raise _ValidationError(msg)
        _validate_list_len(cashflows, "cashflows", _MAX_CASHFLOWS)
        r = FinancialMathPlugin._normalize_rate(rate)
        npv = _ZERO
        for t, cf in enumerate(cashflows):
            d_cf = _to_decimal(cf)
            npv += d_cf / (Decimal(1) + r) ** t
        return _ok(
            "tvm",
            mode="npv",
            rate=_format_decimal(r * _HUNDRED) + "%",
            periods=str(len(cashflows)),
            npv=_format_decimal(npv),
            result=_format_decimal(npv),
            profitable=npv > _ZERO,
            message=(
                f"NPV at {_format_decimal(r * _HUNDRED)}% = "
                f"{_format_decimal(npv)} "
                f"({'profitable' if npv > _ZERO else 'not profitable'})"
            ),
        )

    @staticmethod
    def _tvm_irr(cashflows: list[float] | None) -> str:
        """IRR via Newton-Raphson iteration."""
        _require(cashflows=cashflows)
        assert cashflows is not None
        if len(cashflows) < 2:  # noqa: PLR2004
            msg = "need at least 2 cashflows"
            raise _ValidationError(msg)
        _validate_list_len(cashflows, "cashflows", _MAX_CASHFLOWS)

        d_cfs = [_to_decimal(cf) for cf in cashflows]

        # Newton-Raphson: find r where NPV(r) = 0
        r = Decimal("0.1")  # initial guess 10%
        max_iter = 100
        tolerance = Decimal("1E-10")

        for _ in range(max_iter):
            npv = _ZERO
            dnpv = _ZERO  # derivative
            for t, cf in enumerate(d_cfs):
                denom = (Decimal(1) + r) ** t
                if denom == _ZERO:
                    break
                npv += cf / denom
                if t > 0:
                    dnpv -= _to_decimal(t) * cf / (Decimal(1) + r) ** (t + 1)

            if dnpv == _ZERO:
                msg = "IRR calculation did not converge (zero derivative)"
                raise _ValidationError(msg)

            r_new = r - npv / dnpv
            if abs(r_new - r) < tolerance:
                irr_pct = r_new * _HUNDRED
                return _ok(
                    "tvm",
                    mode="irr",
                    irr_decimal=_format_decimal(r_new),
                    irr_percent=_format_decimal(irr_pct),
                    iterations=str(_ + 1),
                    result=_format_decimal(irr_pct),
                    message=f"IRR = {_format_decimal(irr_pct)}%",
                )
            r = r_new

        msg = f"IRR did not converge after {max_iter} iterations"
        raise _ValidationError(msg)

    @staticmethod
    def _tvm_pv(
        future_value: float | None,
        rate: float | None,
        periods: float | None,
    ) -> str:
        """PV = FV / (1+r)^n."""
        _require(future_value=future_value, rate=rate, periods=periods)
        fv = _to_decimal(future_value)
        r = FinancialMathPlugin._normalize_rate(rate)
        n = _to_decimal(periods)
        pv = fv / (Decimal(1) + r) ** n
        return _ok(
            "tvm",
            mode="pv",
            future_value=_format_decimal(fv),
            rate=_format_decimal(r * _HUNDRED) + "%",
            periods=_format_decimal(n),
            present_value=_format_decimal(pv),
            result=_format_decimal(pv),
            message=(
                f"FV={_format_decimal(fv)} at "
                f"{_format_decimal(r * _HUNDRED)}% for "
                f"{_format_decimal(n)} periods → PV={_format_decimal(pv)}"
            ),
        )

    @staticmethod
    def _tvm_fv(
        present_value: float | None,
        rate: float | None,
        periods: float | None,
    ) -> str:
        """FV = PV * (1+r)^n."""
        _require(present_value=present_value, rate=rate, periods=periods)
        pv = _to_decimal(present_value)
        r = FinancialMathPlugin._normalize_rate(rate)
        n = _to_decimal(periods)
        fv = pv * (Decimal(1) + r) ** n
        return _ok(
            "tvm",
            mode="fv",
            present_value=_format_decimal(pv),
            rate=_format_decimal(r * _HUNDRED) + "%",
            periods=_format_decimal(n),
            future_value=_format_decimal(fv),
            result=_format_decimal(fv),
            message=(
                f"PV={_format_decimal(pv)} at "
                f"{_format_decimal(r * _HUNDRED)}% for "
                f"{_format_decimal(n)} periods → FV={_format_decimal(fv)}"
            ),
        )

    @staticmethod
    def _tvm_annuity_pv(
        payment: float | None,
        rate: float | None,
        periods: float | None,
    ) -> str:
        """Annuity PV = PMT * [1 - (1+r)^(-n)] / r."""
        _require(payment=payment, rate=rate, periods=periods)
        pmt = _to_decimal(payment)
        r = FinancialMathPlugin._normalize_rate(rate)
        n = _to_decimal(periods)
        if r == _ZERO:  # noqa: SIM108
            pv = pmt * n
        else:
            pv = pmt * (Decimal(1) - (Decimal(1) + r) ** (-n)) / r
        return _ok(
            "tvm",
            mode="annuity_pv",
            payment=_format_decimal(pmt),
            rate=_format_decimal(r * _HUNDRED) + "%",
            periods=_format_decimal(n),
            present_value=_format_decimal(pv),
            result=_format_decimal(pv),
            message=(
                f"PMT={_format_decimal(pmt)} at "
                f"{_format_decimal(r * _HUNDRED)}% for "
                f"{_format_decimal(n)} periods → PV={_format_decimal(pv)}"
            ),
        )

    @staticmethod
    def _tvm_annuity_fv(
        payment: float | None,
        rate: float | None,
        periods: float | None,
    ) -> str:
        """Annuity FV = PMT * [(1+r)^n - 1] / r."""
        _require(payment=payment, rate=rate, periods=periods)
        pmt = _to_decimal(payment)
        r = FinancialMathPlugin._normalize_rate(rate)
        n = _to_decimal(periods)
        if r == _ZERO:  # noqa: SIM108
            fv = pmt * n
        else:
            fv = pmt * ((Decimal(1) + r) ** n - Decimal(1)) / r
        return _ok(
            "tvm",
            mode="annuity_fv",
            payment=_format_decimal(pmt),
            rate=_format_decimal(r * _HUNDRED) + "%",
            periods=_format_decimal(n),
            future_value=_format_decimal(fv),
            result=_format_decimal(fv),
            message=(
                f"PMT={_format_decimal(pmt)} at "
                f"{_format_decimal(r * _HUNDRED)}% for "
                f"{_format_decimal(n)} periods → FV={_format_decimal(fv)}"
            ),
        )

    # ── Amortization ──

    @tool(
        description=(
            "Loan amortization calculations. Modes: "
            "'price' (fixed payment — French system), "
            "'sac' (fixed amortization — Brazilian system), "
            "'compare' (side-by-side Price vs SAC). "
            "Returns schedule summary (first 3 + last 3 payments for >6 months). "
            "Example: amortization(mode='compare', principal=400000, annual_rate=9.5, months=360)"
        ),
    )
    async def amortization(
        self,
        mode: str,
        *,
        principal: float | None = None,
        annual_rate: float | None = None,
        months: int | None = None,
    ) -> str:
        """Loan amortization with Decimal precision.

        Args:
            mode: 'price', 'sac', 'compare'.
            principal: Loan amount.
            annual_rate: Annual interest rate (auto-detect: >1 = %, ≤1 = decimal).
            months: Total number of monthly payments.

        Returns:
            JSON with schedule, totals, and breakdown.
        """
        mode = mode.strip().lower()

        try:
            _require(principal=principal, annual_rate=annual_rate, months=months)
            assert months is not None
            p = _to_decimal(principal)
            annual_r = self._normalize_rate(annual_rate)
            n = int(months)
            if p <= _ZERO:
                msg = "principal must be positive"
                raise _ValidationError(msg)
            if n <= 0:
                msg = "months must be positive"
                raise _ValidationError(msg)
            _validate_periods(n)
            _validate_value(p, "principal")
            # Monthly rate from annual
            monthly_r = (Decimal(1) + annual_r) ** (Decimal(1) / Decimal(12)) - Decimal(1)

            if mode == "price":
                return self._amort_price(p, monthly_r, n, annual_r)
            if mode == "sac":
                return self._amort_sac(p, monthly_r, n, annual_r)
            if mode == "compare":
                return self._amort_compare(p, monthly_r, n, annual_r)
        except _ValidationError as e:
            return _err(str(e))
        except (ZeroDivisionError, DecimalException, OverflowError) as e:
            return _err(f"calculation error: {e}")

        return _err(f"unknown mode: '{mode}'. Valid: price, sac, compare")

    # ── Amortization Internals ──

    @staticmethod
    def _price_payment(p: Decimal, r: Decimal, n: int) -> Decimal:
        """Calculate fixed Price payment: PMT = P * r / (1 - (1+r)^-n)."""
        if r == _ZERO:
            return p / Decimal(n)
        return p * r / (Decimal(1) - (Decimal(1) + r) ** (-n))

    @staticmethod
    def _build_price_schedule(
        p: Decimal,
        r: Decimal,
        n: int,
    ) -> list[dict[str, str]]:
        """Build full Price amortization schedule."""
        pmt = FinancialMathPlugin._price_payment(p, r, n)
        balance = p
        schedule: list[dict[str, str]] = []
        for month in range(1, n + 1):
            interest = balance * r
            principal_paid = pmt - interest
            balance -= principal_paid
            if balance < _ZERO:
                balance = _ZERO
            schedule.append(
                {
                    "month": str(month),
                    "payment": _format_decimal(pmt),
                    "principal": _format_decimal(principal_paid),
                    "interest": _format_decimal(interest),
                    "balance": _format_decimal(balance),
                }
            )
        return schedule

    @staticmethod
    def _build_sac_schedule(
        p: Decimal,
        r: Decimal,
        n: int,
    ) -> list[dict[str, str]]:
        """Build full SAC amortization schedule."""
        fixed_amort = p / Decimal(n)
        balance = p
        schedule: list[dict[str, str]] = []
        for month in range(1, n + 1):
            interest = balance * r
            payment = fixed_amort + interest
            balance -= fixed_amort
            if balance < _ZERO:
                balance = _ZERO
            schedule.append(
                {
                    "month": str(month),
                    "payment": _format_decimal(payment),
                    "principal": _format_decimal(fixed_amort),
                    "interest": _format_decimal(interest),
                    "balance": _format_decimal(balance),
                }
            )
        return schedule

    @staticmethod
    def _schedule_summary(
        schedule: list[dict[str, str]],
    ) -> list[dict[str, str]]:
        """Return first 3 + last 3 for long schedules, full for short."""
        if len(schedule) <= 6:  # noqa: PLR2004
            return schedule
        return schedule[:3] + schedule[-3:]

    @staticmethod
    def _schedule_totals(
        schedule: list[dict[str, str]],
    ) -> tuple[Decimal, Decimal, Decimal]:
        """Sum total paid, total interest, total principal from schedule."""
        total_paid = _ZERO
        total_interest = _ZERO
        total_principal = _ZERO
        for row in schedule:
            total_paid += _to_decimal(row["payment"])
            total_interest += _to_decimal(row["interest"])
            total_principal += _to_decimal(row["principal"])
        return total_paid, total_interest, total_principal

    @staticmethod
    def _amort_price(
        p: Decimal,
        r: Decimal,
        n: int,
        annual_r: Decimal,
    ) -> str:
        """Price system (French) — fixed payment."""
        schedule = FinancialMathPlugin._build_price_schedule(p, r, n)
        total_paid, total_interest, _ = FinancialMathPlugin._schedule_totals(
            schedule,
        )
        pmt = FinancialMathPlugin._price_payment(p, r, n)

        return _ok(
            "amortization",
            mode="price",
            principal=_format_decimal(p),
            annual_rate=_format_decimal(annual_r * _HUNDRED) + "%",
            monthly_rate=_format_decimal(r * _HUNDRED) + "%",
            months=str(n),
            fixed_payment=_format_decimal(pmt),
            total_paid=_format_decimal(total_paid),
            total_interest=_format_decimal(total_interest),
            schedule=FinancialMathPlugin._schedule_summary(schedule),
            result=_format_decimal(pmt),
            message=(
                f"Price: {n} payments of {_format_decimal(pmt)} | "
                f"Total: {_format_decimal(total_paid)} | "
                f"Interest: {_format_decimal(total_interest)}"
            ),
        )

    @staticmethod
    def _amort_sac(
        p: Decimal,
        r: Decimal,
        n: int,
        annual_r: Decimal,
    ) -> str:
        """SAC system — fixed amortization."""
        schedule = FinancialMathPlugin._build_sac_schedule(p, r, n)
        total_paid, total_interest, _ = FinancialMathPlugin._schedule_totals(
            schedule,
        )
        first_pmt = _to_decimal(schedule[0]["payment"])
        last_pmt = _to_decimal(schedule[-1]["payment"])

        return _ok(
            "amortization",
            mode="sac",
            principal=_format_decimal(p),
            annual_rate=_format_decimal(annual_r * _HUNDRED) + "%",
            monthly_rate=_format_decimal(r * _HUNDRED) + "%",
            months=str(n),
            first_payment=_format_decimal(first_pmt),
            last_payment=_format_decimal(last_pmt),
            total_paid=_format_decimal(total_paid),
            total_interest=_format_decimal(total_interest),
            schedule=FinancialMathPlugin._schedule_summary(schedule),
            result=_format_decimal(first_pmt),
            message=(
                f"SAC: first {_format_decimal(first_pmt)}, "
                f"last {_format_decimal(last_pmt)} | "
                f"Total: {_format_decimal(total_paid)} | "
                f"Interest: {_format_decimal(total_interest)}"
            ),
        )

    @staticmethod
    def _amort_compare(
        p: Decimal,
        r: Decimal,
        n: int,
        annual_r: Decimal,
    ) -> str:
        """Side-by-side Price vs SAC comparison."""
        price_sched = FinancialMathPlugin._build_price_schedule(p, r, n)
        sac_sched = FinancialMathPlugin._build_sac_schedule(p, r, n)
        price_total, price_interest, _ = FinancialMathPlugin._schedule_totals(price_sched)
        sac_total, sac_interest, _ = FinancialMathPlugin._schedule_totals(sac_sched)
        price_pmt = FinancialMathPlugin._price_payment(p, r, n)
        sac_first = _to_decimal(sac_sched[0]["payment"])
        sac_last = _to_decimal(sac_sched[-1]["payment"])
        savings = price_total - sac_total

        return _ok(
            "amortization",
            mode="compare",
            principal=_format_decimal(p),
            annual_rate=_format_decimal(annual_r * _HUNDRED) + "%",
            months=str(n),
            price={
                "fixed_payment": _format_decimal(price_pmt),
                "total_paid": _format_decimal(price_total),
                "total_interest": _format_decimal(price_interest),
            },
            sac={
                "first_payment": _format_decimal(sac_first),
                "last_payment": _format_decimal(sac_last),
                "total_paid": _format_decimal(sac_total),
                "total_interest": _format_decimal(sac_interest),
            },
            savings_with_sac=_format_decimal(savings),
            result=_format_decimal(savings),
            message=(
                f"Price: {_format_decimal(price_pmt)}/mo, "
                f"total {_format_decimal(price_total)} | "
                f"SAC: {_format_decimal(sac_first)}→"
                f"{_format_decimal(sac_last)}/mo, "
                f"total {_format_decimal(sac_total)} | "
                f"SAC saves {_format_decimal(savings)}"
            ),
        )

    # ── Portfolio Analytics ──

    @tool(
        description=(
            "Portfolio performance analytics. Modes: "
            "'returns' (calculate returns from prices), "
            "'sharpe' (Sharpe ratio — risk-adjusted return), "
            "'sortino' (Sortino ratio — downside-only risk), "
            "'max_drawdown' (worst peak-to-trough decline), "
            "'volatility' (annualized standard deviation), "
            "'summary' (all metrics at once). "
            "Accepts returns as percentages or prices. "
            "Example: portfolio(mode='summary', "
            "returns=[3.2, 1.5, -0.8, 4.1, 2.7], risk_free_rate=1)"
        ),
    )
    async def portfolio(
        self,
        mode: str,
        *,
        returns: list[float] | None = None,
        prices: list[float] | None = None,
        risk_free_rate: float = 0,
        periods_per_year: int = 12,
    ) -> str:
        """Portfolio analytics with Decimal precision.

        Args:
            mode: 'returns', 'sharpe', 'sortino', 'max_drawdown',
                  'volatility', 'summary'.
            returns: List of period returns as percentages (e.g. [3.2, -0.8]).
            prices: List of asset prices (alternative to returns).
            risk_free_rate: Risk-free rate per period (default 0).
            periods_per_year: Annualization factor (12=monthly, 252=daily).

        Returns:
            JSON with metrics and breakdown.
        """
        mode = mode.strip().lower()

        try:
            # Get returns — either directly or from prices
            rets = self._get_returns(returns, prices)
            # risk_free_rate is in same units as returns (percentage)
            d_rf = _to_decimal(risk_free_rate) / _HUNDRED

            if mode == "returns":
                return self._portfolio_returns(returns, prices)
            if mode == "sharpe":
                return self._portfolio_sharpe(rets, d_rf, periods_per_year)
            if mode == "sortino":
                return self._portfolio_sortino(rets, d_rf, periods_per_year)
            if mode == "max_drawdown":
                return self._portfolio_drawdown(rets)
            if mode == "volatility":
                return self._portfolio_volatility(rets, periods_per_year)
            if mode == "summary":
                return self._portfolio_summary(
                    rets,
                    d_rf,
                    periods_per_year,
                )
        except _ValidationError as e:
            return _err(str(e))
        except (ZeroDivisionError, DecimalException, OverflowError) as e:
            return _err(f"calculation error: {e}")

        valid = "returns, sharpe, sortino, max_drawdown, volatility, summary"
        return _err(f"unknown mode: '{mode}'. Valid: {valid}")

    # ── Portfolio Internals ──

    @staticmethod
    def _get_returns(
        returns: list[float] | None,
        prices: list[float] | None,
    ) -> list[Decimal]:
        """Get decimal returns from either returns or prices."""
        if returns is not None and len(returns) > 0:
            _validate_list_len(returns, "returns", _MAX_RETURNS)
            return [_to_decimal(r) / _HUNDRED for r in returns]
        if prices is not None and len(prices) >= 2:  # noqa: PLR2004
            _validate_list_len(prices, "prices", _MAX_RETURNS)
            d_prices = [_to_decimal(p) for p in prices]
            return [
                (d_prices[i] - d_prices[i - 1]) / d_prices[i - 1] for i in range(1, len(d_prices))
            ]
        msg = "provide 'returns' (list of %) or 'prices' (list of prices)"
        raise _ValidationError(msg)

    @staticmethod
    def _mean(values: list[Decimal]) -> Decimal:
        n = len(values)
        if n == 0:
            return _ZERO
        return sum(values) / Decimal(n)

    @staticmethod
    def _std_dev(values: list[Decimal], mean: Decimal) -> Decimal:
        n = len(values)
        if n < 2:  # noqa: PLR2004
            return _ZERO
        variance = sum((v - mean) ** 2 for v in values) / Decimal(n - 1)
        return variance.sqrt()

    @staticmethod
    def _downside_dev(
        values: list[Decimal],
        target: Decimal,
    ) -> Decimal:
        """Downside deviation — only negative deviations count."""
        downs = [(v - target) ** 2 for v in values if v < target]
        if not downs:
            return _ZERO
        return (sum(downs) / Decimal(len(downs))).sqrt()

    @staticmethod
    def _portfolio_returns(
        returns: list[float] | None,
        prices: list[float] | None,
    ) -> str:
        if prices is not None and len(prices) >= 2:  # noqa: PLR2004
            d_prices = [_to_decimal(p) for p in prices]
            rets = [
                (d_prices[i] - d_prices[i - 1]) / d_prices[i - 1] * _HUNDRED
                for i in range(1, len(d_prices))
            ]
            return _ok(
                "portfolio",
                mode="returns",
                count=str(len(rets)),
                returns=[_format_decimal(r) + "%" for r in rets],
                result=[_format_decimal(r) for r in rets],
                message=f"Calculated {len(rets)} returns from {len(d_prices)} prices",
            )
        if returns is not None:
            return _ok(
                "portfolio",
                mode="returns",
                count=str(len(returns)),
                returns=[str(r) + "%" for r in returns],
                result=[str(r) for r in returns],
                message=f"Using {len(returns)} provided returns",
            )
        return _err("provide 'returns' or 'prices'")

    @staticmethod
    def _portfolio_sharpe(
        rets: list[Decimal],
        rf: Decimal,
        ppy: int,
    ) -> str:
        """Sharpe = (mean_return - rf) / std_dev * sqrt(ppy)."""
        mean_r = FinancialMathPlugin._mean(rets)
        std = FinancialMathPlugin._std_dev(rets, mean_r)
        if std == _ZERO:
            return _ok(
                "portfolio",
                mode="sharpe",
                sharpe="infinity",
                result="infinity",
                message="Sharpe: ∞ (zero volatility)",
            )
        sharpe = (mean_r - rf) / std * _to_decimal(ppy).sqrt()
        return _ok(
            "portfolio",
            mode="sharpe",
            mean_return=_format_decimal(mean_r * _HUNDRED) + "%",
            std_dev=_format_decimal(std * _HUNDRED) + "%",
            risk_free=_format_decimal(rf * _HUNDRED) + "%",
            annualization_factor=str(ppy),
            sharpe=_format_decimal(sharpe),
            result=_format_decimal(sharpe),
            message=f"Sharpe Ratio: {_format_decimal(sharpe)}",
        )

    @staticmethod
    def _portfolio_sortino(
        rets: list[Decimal],
        rf: Decimal,
        ppy: int,
    ) -> str:
        """Sortino = (mean_return - rf) / downside_dev * sqrt(ppy)."""
        mean_r = FinancialMathPlugin._mean(rets)
        dd = FinancialMathPlugin._downside_dev(rets, rf)
        if dd == _ZERO:
            return _ok(
                "portfolio",
                mode="sortino",
                sortino="infinity",
                result="infinity",
                message="Sortino: ∞ (no downside)",
            )
        sortino = (mean_r - rf) / dd * _to_decimal(ppy).sqrt()
        return _ok(
            "portfolio",
            mode="sortino",
            mean_return=_format_decimal(mean_r * _HUNDRED) + "%",
            downside_dev=_format_decimal(dd * _HUNDRED) + "%",
            risk_free=_format_decimal(rf * _HUNDRED) + "%",
            sortino=_format_decimal(sortino),
            result=_format_decimal(sortino),
            message=f"Sortino Ratio: {_format_decimal(sortino)}",
        )

    @staticmethod
    def _portfolio_drawdown(rets: list[Decimal]) -> str:
        """Max drawdown from returns."""
        if not rets:
            return _err("no returns provided")
        cumulative = Decimal(1)
        peak = Decimal(1)
        max_dd = _ZERO
        for r in rets:
            cumulative *= Decimal(1) + r
            if cumulative > peak:
                peak = cumulative
            dd = (peak - cumulative) / peak
            if dd > max_dd:
                max_dd = dd
        return _ok(
            "portfolio",
            mode="max_drawdown",
            max_drawdown_percent=_format_decimal(max_dd * _HUNDRED) + "%",
            result=_format_decimal(max_dd * _HUNDRED),
            message=f"Max Drawdown: {_format_decimal(max_dd * _HUNDRED)}%",
        )

    @staticmethod
    def _portfolio_volatility(
        rets: list[Decimal],
        ppy: int,
    ) -> str:
        """Annualized volatility = std_dev * sqrt(periods_per_year)."""
        mean_r = FinancialMathPlugin._mean(rets)
        std = FinancialMathPlugin._std_dev(rets, mean_r)
        ann_vol = std * _to_decimal(ppy).sqrt()
        return _ok(
            "portfolio",
            mode="volatility",
            period_volatility=_format_decimal(std * _HUNDRED) + "%",
            annualized_volatility=_format_decimal(ann_vol * _HUNDRED) + "%",
            periods_per_year=str(ppy),
            result=_format_decimal(ann_vol * _HUNDRED),
            message=(
                f"Volatility: {_format_decimal(std * _HUNDRED)}%/period, "
                f"{_format_decimal(ann_vol * _HUNDRED)}% annualized"
            ),
        )

    @staticmethod
    def _portfolio_summary(
        rets: list[Decimal],
        rf: Decimal,
        ppy: int,
    ) -> str:
        """All portfolio metrics in one call."""
        mean_r = FinancialMathPlugin._mean(rets)
        std = FinancialMathPlugin._std_dev(rets, mean_r)
        dd = FinancialMathPlugin._downside_dev(rets, rf)
        ann_vol = std * _to_decimal(ppy).sqrt()

        # Sharpe
        sharpe = (
            "infinity"
            if std == _ZERO
            else _format_decimal((mean_r - rf) / std * _to_decimal(ppy).sqrt())
        )

        # Sortino
        sortino = (
            "infinity"
            if dd == _ZERO
            else _format_decimal((mean_r - rf) / dd * _to_decimal(ppy).sqrt())
        )

        # Max drawdown
        cumulative = Decimal(1)
        peak = Decimal(1)
        max_dd = _ZERO
        for r in rets:
            cumulative *= Decimal(1) + r
            if cumulative > peak:
                peak = cumulative
            d = (peak - cumulative) / peak
            if d > max_dd:
                max_dd = d

        # Total return
        total_ret = cumulative - Decimal(1)

        return _ok(
            "portfolio",
            mode="summary",
            periods=str(len(rets)),
            mean_return=_format_decimal(mean_r * _HUNDRED) + "%",
            total_return=_format_decimal(total_ret * _HUNDRED) + "%",
            volatility=_format_decimal(ann_vol * _HUNDRED) + "%",
            sharpe=sharpe,
            sortino=sortino,
            max_drawdown=_format_decimal(max_dd * _HUNDRED) + "%",
            result=sharpe,
            message=(
                f"Summary ({len(rets)} periods): "
                f"Return {_format_decimal(mean_r * _HUNDRED)}%/period, "
                f"Sharpe {sharpe}, Sortino {sortino}, "
                f"Max DD {_format_decimal(max_dd * _HUNDRED)}%, "
                f"Vol {_format_decimal(ann_vol * _HUNDRED)}%"
            ),
        )

    # ── Position Sizing ──

    @tool(
        description=(
            "Position sizing for risk management. Modes: "
            "'kelly' (optimal fraction of bankroll to bet), "
            "'half_kelly' (conservative — half the Kelly fraction), "
            "'fixed_fractional' (risk X% of bankroll per trade), "
            "'max_risk' (max units given entry, stop, and risk amount). "
            "Example: position_size(mode='kelly', win_rate=60, "
            "reward_risk_ratio=2.0, bankroll=50000)"
        ),
    )
    async def position_size(
        self,
        mode: str,
        *,
        win_rate: float | None = None,
        reward_risk_ratio: float | None = None,
        bankroll: float | None = None,
        risk_percent: float | None = None,
        entry_price: float | None = None,
        stop_price: float | None = None,
        risk_amount: float | None = None,
    ) -> str:
        """Position sizing with Decimal precision.

        Args:
            mode: 'kelly', 'half_kelly', 'fixed_fractional', 'max_risk'.
            win_rate: Win probability as % (e.g. 60 for 60%).
            reward_risk_ratio: Average win / average loss (e.g. 2.0).
            bankroll: Total capital available.
            risk_percent: % of bankroll to risk per trade (fixed_fractional).
            entry_price: Trade entry price (max_risk).
            stop_price: Stop loss price (max_risk).
            risk_amount: Max dollar amount to risk (max_risk).

        Returns:
            JSON with position size, rationale, and warnings.
        """
        mode = mode.strip().lower()

        try:
            if mode == "kelly":
                return self._pos_kelly(
                    win_rate,
                    reward_risk_ratio,
                    bankroll,
                    full=True,
                )
            if mode == "half_kelly":
                return self._pos_kelly(
                    win_rate,
                    reward_risk_ratio,
                    bankroll,
                    full=False,
                )
            if mode == "fixed_fractional":
                return self._pos_fixed_fractional(
                    bankroll,
                    risk_percent,
                )
            if mode == "max_risk":
                return self._pos_max_risk(
                    entry_price,
                    stop_price,
                    risk_amount,
                )
        except _ValidationError as e:
            return _err(str(e))
        except (ZeroDivisionError, DecimalException, OverflowError) as e:
            return _err(f"calculation error: {e}")

        valid = "kelly, half_kelly, fixed_fractional, max_risk"
        return _err(f"unknown mode: '{mode}'. Valid: {valid}")

    # ── Position Sizing Internals ──

    @staticmethod
    def _pos_kelly(
        win_rate: float | None,
        rr_ratio: float | None,
        bankroll: float | None,
        *,
        full: bool,
    ) -> str:
        """Kelly criterion: f* = (p*b - q) / b."""
        _require(win_rate=win_rate, reward_risk_ratio=rr_ratio)
        d_wr = _to_decimal(win_rate)
        # Auto-detect: >1 treated as percentage
        p = d_wr / _HUNDRED if d_wr > Decimal(1) else d_wr
        if p <= _ZERO or p >= Decimal(1):
            msg = "win_rate must be between 0% and 100% (exclusive)"
            raise _ValidationError(msg)
        b = _to_decimal(rr_ratio)
        if b <= _ZERO:
            msg = "reward_risk_ratio must be positive"
            raise _ValidationError(msg)
        q = Decimal(1) - p

        # Kelly fraction: f* = (p*b - q) / b
        kelly_f = (p * b - q) / b
        label = "half_kelly" if not full else "kelly"
        fraction = kelly_f / Decimal(2) if not full else kelly_f

        warnings: list[str] = []
        if kelly_f <= _ZERO:
            warnings.append("No edge detected — Kelly says don't bet.")
            fraction = _ZERO
        elif kelly_f > Decimal("0.25"):
            warnings.append(
                f"Aggressive: Kelly fraction is {_format_decimal(kelly_f * _HUNDRED)}%. "
                "Consider half-Kelly for safety."
            )

        result: dict[str, object] = {
            "kelly_fraction": _format_decimal(kelly_f * _HUNDRED) + "%",
            "recommended_fraction": _format_decimal(fraction * _HUNDRED) + "%",
            "win_rate": _format_decimal(p * _HUNDRED) + "%",
            "reward_risk_ratio": _format_decimal(b),
        }

        if bankroll is not None:
            d_bank = _to_decimal(bankroll)
            position = d_bank * fraction
            result["bankroll"] = _format_decimal(d_bank)
            result["position_size"] = _format_decimal(position)
            result["result"] = _format_decimal(position)
            msg_str = (
                f"{label}: {_format_decimal(fraction * _HUNDRED)}% of "
                f"{_format_decimal(d_bank)} = {_format_decimal(position)}"
            )
        else:
            result["result"] = _format_decimal(fraction * _HUNDRED)
            msg_str = f"{label}: bet {_format_decimal(fraction * _HUNDRED)}% of bankroll"

        if warnings:
            result["warnings"] = warnings

        return _ok("position_size", mode=label, message=msg_str, **result)

    @staticmethod
    def _pos_fixed_fractional(
        bankroll: float | None,
        risk_percent: float | None,
    ) -> str:
        """Fixed fractional: risk X% of bankroll per trade."""
        _require(bankroll=bankroll, risk_percent=risk_percent)
        d_bank = _to_decimal(bankroll)
        d_pct = _to_decimal(risk_percent)
        if d_pct > Decimal(1):
            d_pct = d_pct / _HUNDRED
        risk_amount = d_bank * d_pct

        return _ok(
            "position_size",
            mode="fixed_fractional",
            bankroll=_format_decimal(d_bank),
            risk_percent=_format_decimal(d_pct * _HUNDRED) + "%",
            risk_amount=_format_decimal(risk_amount),
            result=_format_decimal(risk_amount),
            message=(
                f"Risk {_format_decimal(d_pct * _HUNDRED)}% of "
                f"{_format_decimal(d_bank)} = "
                f"{_format_decimal(risk_amount)} per trade"
            ),
        )

    @staticmethod
    def _pos_max_risk(
        entry_price: float | None,
        stop_price: float | None,
        risk_amount: float | None,
    ) -> str:
        """Max units: risk_amount / |entry - stop|."""
        _require(
            entry_price=entry_price,
            stop_price=stop_price,
            risk_amount=risk_amount,
        )
        d_entry = _to_decimal(entry_price)
        d_stop = _to_decimal(stop_price)
        d_risk = _to_decimal(risk_amount)
        risk_per_unit = abs(d_entry - d_stop)
        if risk_per_unit == _ZERO:
            msg = "entry and stop prices cannot be equal"
            raise _ValidationError(msg)
        max_units = d_risk / risk_per_unit
        total_position = max_units * d_entry

        return _ok(
            "position_size",
            mode="max_risk",
            entry_price=_format_decimal(d_entry),
            stop_price=_format_decimal(d_stop),
            risk_per_unit=_format_decimal(risk_per_unit),
            risk_amount=_format_decimal(d_risk),
            max_units=_format_decimal(max_units),
            total_position=_format_decimal(total_position),
            result=_format_decimal(max_units),
            message=(
                f"Entry {_format_decimal(d_entry)}, "
                f"Stop {_format_decimal(d_stop)} "
                f"(risk {_format_decimal(risk_per_unit)}/unit) → "
                f"Max {_format_decimal(max_units)} units "
                f"(position: {_format_decimal(total_position)})"
            ),
        )

    # ── Currency Formatting ──

    @tool(
        description=(
            "Format numbers as currency. Modes: "
            "'format' (display a value as currency), "
            "'convert' (apply exchange rate and format), "
            "'parse' (extract numeric value from currency string). "
            "Supports BRL, USD, EUR, GBP, JPY, BTC, ETH and custom. "
            "Example: currency(mode='format', value=1234567.89, code='BRL')"
        ),
    )
    async def currency(
        self,
        mode: str,
        *,
        value: float | None = None,
        code: str = "USD",
        decimals: int | None = None,
        from_code: str | None = None,
        to_code: str | None = None,
        rate: float | None = None,
        text: str | None = None,
    ) -> str:
        """Currency formatting with Decimal precision.

        Args:
            mode: 'format', 'convert', 'parse'.
            value: Numeric value to format or convert.
            code: Currency code (default: USD).
            decimals: Override decimal places (auto if None).
            from_code: Source currency (convert mode).
            to_code: Target currency (convert mode).
            rate: Exchange rate (convert mode).
            text: Currency string to parse (parse mode).

        Returns:
            JSON with formatted value and metadata.
        """
        mode = mode.strip().lower()

        try:
            if mode == "format":
                return self._curr_format(value, code, decimals)
            if mode == "convert":
                return self._curr_convert(
                    value,
                    from_code,
                    to_code,
                    rate,
                    decimals,
                )
            if mode == "parse":
                return self._curr_parse(text)
        except _ValidationError as e:
            return _err(str(e))
        except (DecimalException, OverflowError) as e:
            return _err(f"formatting error: {e}")

        return _err(f"unknown mode: '{mode}'. Valid: format, convert, parse")

    # ── Currency Config ──

    _CURRENCY_INFO: dict[str, tuple[str, int, str]] = {
        # code → (symbol, default_decimals, position)
        "USD": ("$", 2, "prefix"),
        "BRL": ("R$", 2, "prefix"),
        "EUR": ("€", 2, "prefix"),
        "GBP": ("£", 2, "prefix"),
        "JPY": ("¥", 0, "prefix"),
        "CNY": ("¥", 2, "prefix"),
        "BTC": ("₿", 8, "prefix"),
        "ETH": ("Ξ", 8, "prefix"),
        "SAT": ("sat", 0, "suffix"),
    }

    @staticmethod
    def _format_currency(
        val: Decimal,
        code: str,
        override_decimals: int | None = None,
    ) -> str:
        """Format a Decimal as currency string."""
        code_upper = code.upper()
        symbol, default_dec, position = FinancialMathPlugin._CURRENCY_INFO.get(
            code_upper,
            ("", 2, "prefix"),
        )
        dec = override_decimals if override_decimals is not None else default_dec

        # Quantize to desired precision
        quantizer = Decimal(10) ** -dec
        quantized = val.quantize(quantizer, rounding=ROUND_HALF_EVEN)

        # Format with thousands separator
        sign = "-" if quantized < _ZERO else ""
        abs_val = abs(quantized)
        int_part = int(abs_val)
        frac_part = abs_val - Decimal(int_part)

        # Thousands separator
        int_str = f"{int_part:,}"

        if dec > 0:
            frac_str = str(frac_part.quantize(quantizer))[2:]  # skip "0."
            unsigned_num = f"{int_str}.{frac_str}"
        else:
            unsigned_num = int_str

        # Symbol placement (sign always before symbol for prefix)
        if not symbol:
            return f"{sign}{unsigned_num} {code_upper}"
        if position == "suffix":
            return f"{sign}{unsigned_num} {symbol}"
        return f"{sign}{symbol}{unsigned_num}"

    @staticmethod
    def _curr_format(
        value: float | None,
        code: str,
        decimals: int | None,
    ) -> str:
        _require(value=value)
        d_val = _to_decimal(value)
        formatted = FinancialMathPlugin._format_currency(d_val, code, decimals)
        return _ok(
            "currency",
            mode="format",
            value=_format_decimal(d_val),
            code=code.upper(),
            formatted=formatted,
            result=formatted,
            message=formatted,
        )

    @staticmethod
    def _curr_convert(
        value: float | None,
        from_code: str | None,
        to_code: str | None,
        rate: float | None,
        decimals: int | None,
    ) -> str:
        _require(value=value, rate=rate, from_code=from_code, to_code=to_code)
        assert from_code is not None and to_code is not None
        d_val = _to_decimal(value)
        d_rate = _to_decimal(rate)
        converted = d_val * d_rate
        from_fmt = FinancialMathPlugin._format_currency(
            d_val,
            from_code,
            decimals,
        )
        to_fmt = FinancialMathPlugin._format_currency(
            converted,
            to_code,
            decimals,
        )
        return _ok(
            "currency",
            mode="convert",
            from_value=_format_decimal(d_val),
            from_code=from_code.upper(),
            to_value=_format_decimal(converted),
            to_code=to_code.upper(),
            rate=_format_decimal(d_rate),
            from_formatted=from_fmt,
            to_formatted=to_fmt,
            result=_format_decimal(converted),
            message=f"{from_fmt} × {_format_decimal(d_rate)} = {to_fmt}",
        )

    @staticmethod
    def _curr_parse(text: str | None) -> str:
        """Extract numeric value from currency string."""
        _require(text=text)
        assert text is not None
        raw = text.strip()

        # Detect currency code/symbol
        detected_code = ""
        for code, (symbol, _, _) in FinancialMathPlugin._CURRENCY_INFO.items():
            if symbol in raw or code in raw.upper():
                detected_code = code
                break

        # Remove currency symbols (longest first to avoid partial matches)
        cleaned = raw
        symbols = sorted(
            (s for _, (s, _, _) in FinancialMathPlugin._CURRENCY_INFO.items()),
            key=len,
            reverse=True,
        )
        for symbol in symbols:
            cleaned = cleaned.replace(symbol, "")
        cleaned = cleaned.strip()

        # Handle comma as thousands separator (1,234,567.89)
        # vs comma as decimal (1.234.567,89 — BR style)
        if "," in cleaned and "." in cleaned:
            if cleaned.rindex(",") > cleaned.rindex("."):
                # BR style: dots are thousands, comma is decimal
                cleaned = cleaned.replace(".", "").replace(",", ".")
            else:
                # US style: commas are thousands
                cleaned = cleaned.replace(",", "")
        elif "," in cleaned and "." not in cleaned:
            # Could be thousands or decimal — check position
            parts = cleaned.split(",")
            if len(parts) == 2 and len(parts[1]) <= 2:  # noqa: PLR2004
                # Likely decimal: "1234,56"
                cleaned = cleaned.replace(",", ".")
            else:
                # Likely thousands: "1,234,567"
                cleaned = cleaned.replace(",", "")

        try:
            d_val = Decimal(cleaned)
        except DecimalException as exc:
            msg = f"could not parse '{raw}' as a number"
            raise _ValidationError(msg) from exc

        result: dict[str, str] = {
            "value": _format_decimal(d_val),
            "result": _format_decimal(d_val),
            "message": f"Parsed: {_format_decimal(d_val)}",
        }
        if detected_code:
            result["detected_currency"] = detected_code
            result["message"] += f" ({detected_code})"

        return _ok("currency", mode="parse", **result)
