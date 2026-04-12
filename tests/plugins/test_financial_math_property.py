"""Property-based tests for Financial Math Plugin (TASK-494).

Uses Hypothesis to verify mathematical invariants that must hold
for ALL valid inputs, not just handpicked examples.
"""

from __future__ import annotations

import json
from decimal import Decimal

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from sovyx.plugins.official.financial_math import FinancialMathPlugin

# ── Helpers ──

_plugin = FinancialMathPlugin()


def _parse(raw: str) -> dict[str, object]:
    return json.loads(raw)  # type: ignore[no-any-return]


# ── Strategies ──

# Sane financial values (avoid overflow/timeout)
sane_amount = st.floats(min_value=0.01, max_value=1e12, allow_nan=False, allow_infinity=False)
sane_rate = st.floats(min_value=0.1, max_value=50, allow_nan=False, allow_infinity=False)
sane_periods = st.integers(min_value=1, max_value=360)
sane_months = st.integers(min_value=1, max_value=600)
sane_pct = st.floats(min_value=-99, max_value=999, allow_nan=False, allow_infinity=False)
positive_float = st.floats(min_value=0.01, max_value=1e10, allow_nan=False, allow_infinity=False)


# ── Calculator Invariants ──


class TestCalculateProperties:
    """Property: calculator always returns valid JSON."""

    @given(
        a=st.integers(min_value=-1000, max_value=1000), b=st.integers(min_value=1, max_value=1000)
    )
    @settings(max_examples=50)
    @pytest.mark.anyio()
    async def test_addition_commutative(self, a: int, b: int) -> None:
        """a + b == b + a."""
        r1 = _parse(await _plugin.calculate(f"{a} + {b}"))
        r2 = _parse(await _plugin.calculate(f"{b} + {a}"))
        assert r1["result"] == r2["result"]

    @given(a=st.integers(min_value=1, max_value=100), b=st.integers(min_value=1, max_value=100))
    @settings(max_examples=50)
    @pytest.mark.anyio()
    async def test_multiplication_commutative(self, a: int, b: int) -> None:
        """a * b == b * a."""
        r1 = _parse(await _plugin.calculate(f"{a} * {b}"))
        r2 = _parse(await _plugin.calculate(f"{b} * {a}"))
        assert r1["result"] == r2["result"]


# ── Percentage Invariants ──


class TestPercentageProperties:
    """Property: percentage calculations are mathematically consistent."""

    @given(rate=sane_pct, value=positive_float)
    @settings(max_examples=50)
    @pytest.mark.anyio()
    async def test_pct_of_positive(self, rate: float, value: float) -> None:
        """X% of positive Y has same sign as X."""
        data = _parse(await _plugin.percentage(mode="of", rate=rate, value=value))
        if data["ok"]:
            result = Decimal(str(data["result"]))
            if result == 0:
                return  # rounding to zero is valid
            if rate > 0:
                assert result > 0
            elif rate < 0:
                assert result < 0

    @given(
        from_val=st.floats(min_value=1, max_value=1e8, allow_nan=False, allow_infinity=False),
        to_val=st.floats(min_value=1, max_value=1e8, allow_nan=False, allow_infinity=False),
    )
    @settings(max_examples=50)
    @pytest.mark.anyio()
    async def test_change_sign(self, from_val: float, to_val: float) -> None:
        """% change is positive when to > from, negative when to < from."""
        data = _parse(
            await _plugin.percentage(mode="change", from_value=from_val, to_value=to_val)
        )
        if data["ok"]:
            result = Decimal(str(data["result"]))
            if to_val > from_val:
                assert result > 0
            elif to_val < from_val:
                assert result < 0
            else:
                assert result == 0


# ── Interest Invariants ──


class TestInterestProperties:
    """Property: compound always >= simple for positive rates."""

    @given(principal=sane_amount, rate=sane_rate, periods=sane_periods)
    @settings(max_examples=50)
    @pytest.mark.anyio()
    async def test_compound_ge_simple(
        self,
        principal: float,
        rate: float,
        periods: int,
    ) -> None:
        """Compound >= Simple for positive rate and periods >= 1."""
        simple = _parse(
            await _plugin.interest(
                mode="simple",
                principal=principal,
                rate=rate,
                periods=periods,
            )
        )
        compound = _parse(
            await _plugin.interest(
                mode="compound",
                principal=principal,
                rate=rate,
                periods=periods,
            )
        )
        if simple["ok"] and compound["ok"]:
            s = Decimal(str(simple["result"]))
            c = Decimal(str(compound["result"]))
            assert c >= s - Decimal("0.01")  # small tolerance for rounding


# ── TVM Invariants ──


class TestTVMProperties:
    """Property: PV↔FV roundtrip, NPV(IRR)≈0."""

    @given(pv=sane_amount, rate=sane_rate, periods=sane_periods)
    @settings(max_examples=30)
    @pytest.mark.anyio()
    async def test_pv_fv_roundtrip(
        self,
        pv: float,
        rate: float,
        periods: int,
    ) -> None:
        """PV→FV→PV should return ~original."""
        fv_data = _parse(
            await _plugin.tvm(mode="fv", present_value=pv, rate=rate, periods=periods)
        )
        if not fv_data["ok"]:
            return
        fv = float(str(fv_data["result"]))
        pv_data = _parse(await _plugin.tvm(mode="pv", future_value=fv, rate=rate, periods=periods))
        if not pv_data["ok"]:
            return
        recovered = Decimal(str(pv_data["result"]))
        original = Decimal(str(pv))
        # Allow 0.1% tolerance for Decimal precision
        if original > 0:
            error = abs(recovered - original) / original
            assert error < Decimal("0.001"), f"PV roundtrip error: {error}"

    @given(
        cfs=st.lists(
            st.floats(min_value=-10000, max_value=50000, allow_nan=False, allow_infinity=False),
            min_size=3,
            max_size=10,
        ),
    )
    @settings(max_examples=20)
    @pytest.mark.anyio()
    async def test_npv_at_zero_rate_is_sum(self, cfs: list[float]) -> None:
        """NPV at 0% = sum of cashflows."""
        data = _parse(await _plugin.tvm(mode="npv", rate=0, cashflows=cfs))
        if data["ok"]:
            npv = Decimal(str(data["result"]))
            expected = sum(Decimal(str(cf)) for cf in cfs)
            assert abs(npv - expected) < Decimal("0.01")


# ── Amortization Invariants ──


class TestAmortizationProperties:
    """Property: SAC always cheaper than Price in total interest."""

    @given(
        principal=st.floats(min_value=1000, max_value=1e12, allow_nan=False, allow_infinity=False),
        annual_rate=st.floats(min_value=1, max_value=30, allow_nan=False, allow_infinity=False),
        months=st.integers(min_value=2, max_value=600),
    )
    @settings(max_examples=30)
    @pytest.mark.anyio()
    async def test_sac_cheaper_than_price(
        self,
        principal: float,
        annual_rate: float,
        months: int,
    ) -> None:
        """SAC total interest < Price total interest."""
        compare = _parse(
            await _plugin.amortization(
                mode="compare",
                principal=principal,
                annual_rate=annual_rate,
                months=months,
            )
        )
        if not compare["ok"]:
            return
        price_interest = Decimal(str(compare["price"]["total_interest"]))
        sac_interest = Decimal(str(compare["sac"]["total_interest"]))
        assert sac_interest <= price_interest + Decimal("0.01")

    @given(
        principal=st.floats(min_value=1000, max_value=1e10, allow_nan=False, allow_infinity=False),
        annual_rate=st.floats(min_value=1, max_value=20, allow_nan=False, allow_infinity=False),
        months=st.integers(min_value=1, max_value=120),
    )
    @settings(max_examples=30)
    @pytest.mark.anyio()
    async def test_total_principal_equals_loan(
        self,
        principal: float,
        annual_rate: float,
        months: int,
    ) -> None:
        """Sum of principal payments ≈ original loan amount."""
        data = _parse(
            await _plugin.amortization(
                mode="price",
                principal=principal,
                annual_rate=annual_rate,
                months=months,
            )
        )
        if not data["ok"]:
            return
        # total_paid - total_interest ≈ principal
        total_paid = Decimal(str(data["total_paid"]))
        total_interest = Decimal(str(data["total_interest"]))
        principal_paid = total_paid - total_interest
        d_principal = Decimal(str(principal))
        if d_principal > 0:
            error = abs(principal_paid - d_principal) / d_principal
            assert error < Decimal("0.01"), f"Principal mismatch: {error}"


# ── Portfolio Invariants ──


class TestPortfolioProperties:
    """Property: Sortino >= Sharpe, drawdown non-negative."""

    @given(
        returns=st.lists(
            st.floats(min_value=-20, max_value=20, allow_nan=False, allow_infinity=False),
            min_size=3,
            max_size=100,
        ),
    )
    @settings(max_examples=30)
    @pytest.mark.anyio()
    async def test_sharpe_and_sortino_same_sign(self, returns: list[float]) -> None:
        """Sharpe and Sortino always have the same sign."""
        sharpe = _parse(await _plugin.portfolio(mode="sharpe", returns=returns))
        sortino = _parse(await _plugin.portfolio(mode="sortino", returns=returns))
        if not sharpe["ok"] or not sortino["ok"]:
            return
        s_val = str(sharpe["sharpe"])
        so_val = str(sortino["sortino"])
        if "infinity" in s_val or "infinity" in so_val:
            return
        s = Decimal(s_val)
        so = Decimal(so_val)
        # Same sign (or one is zero)
        assert s * so >= 0 or abs(s) < Decimal("0.01") or abs(so) < Decimal("0.01")

    @given(
        returns=st.lists(
            st.floats(min_value=-50, max_value=50, allow_nan=False, allow_infinity=False),
            min_size=2,
            max_size=100,
        ),
    )
    @settings(max_examples=30)
    @pytest.mark.anyio()
    async def test_drawdown_non_negative(self, returns: list[float]) -> None:
        """Max drawdown is always >= 0."""
        data = _parse(await _plugin.portfolio(mode="max_drawdown", returns=returns))
        if data["ok"]:
            dd = Decimal(str(data["result"]))
            assert dd >= Decimal(0)


# ── Position Sizing Invariants ──


class TestPositionSizingProperties:
    """Property: half-Kelly = Kelly / 2."""

    @given(
        wr=st.floats(min_value=10, max_value=90, allow_nan=False, allow_infinity=False),
        rr=st.floats(min_value=0.1, max_value=10, allow_nan=False, allow_infinity=False),
        bankroll=sane_amount,
    )
    @settings(max_examples=30)
    @pytest.mark.anyio()
    async def test_half_kelly_is_half(
        self,
        wr: float,
        rr: float,
        bankroll: float,
    ) -> None:
        """Half-Kelly position = Full Kelly / 2."""
        full = _parse(
            await _plugin.position_size(
                mode="kelly",
                win_rate=wr,
                reward_risk_ratio=rr,
                bankroll=bankroll,
            )
        )
        half = _parse(
            await _plugin.position_size(
                mode="half_kelly",
                win_rate=wr,
                reward_risk_ratio=rr,
                bankroll=bankroll,
            )
        )
        if not full["ok"] or not half["ok"]:
            return
        full_frac = Decimal(str(full["recommended_fraction"].rstrip("%")))
        half_frac = Decimal(str(half["recommended_fraction"].rstrip("%")))
        assert abs(half_frac * 2 - full_frac) < Decimal("0.01")


# ── Currency Invariants ──


class TestCurrencyProperties:
    """Property: format→parse roundtrip."""

    @given(value=st.floats(min_value=0.01, max_value=1e9, allow_nan=False, allow_infinity=False))
    @settings(max_examples=30)
    @pytest.mark.anyio()
    async def test_format_parse_roundtrip(self, value: float) -> None:
        """Format then parse should recover ~original value."""
        fmt = _parse(await _plugin.currency(mode="format", value=value, code="USD"))
        if not fmt["ok"]:
            return
        parsed = _parse(await _plugin.currency(mode="parse", text=str(fmt["formatted"])))
        if not parsed["ok"]:
            return
        original = Decimal(str(value)).quantize(Decimal("0.01"))
        recovered = Decimal(str(parsed["value"]))
        assert abs(recovered - original) < Decimal("0.01")
