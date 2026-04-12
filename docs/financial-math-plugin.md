# Financial Math Plugin

Enterprise-grade financial calculations with **Decimal precision** — no floating-point errors, no external dependencies.

## Overview

The Financial Math plugin replaces the basic calculator with 9 specialized financial tools. Every calculation uses Python's `Decimal` type with banker's rounding (`ROUND_HALF_EVEN`), ensuring `0.1 + 0.2 == 0.3` — always.

| Tool | Modes | Use Case |
|------|-------|----------|
| `calculate` | — | Safe math expressions via AST |
| `percentage` | of, change, markup, gross_margin, net_margin | Business math |
| `interest` | simple, compound, cagr, rule_of_72 | Growth & returns |
| `tvm` | npv, irr, pv, fv, annuity_pv, annuity_fv | Investment analysis |
| `amortization` | price, sac, compare | Loan schedules |
| `portfolio` | returns, sharpe, sortino, max_drawdown, volatility, summary | Risk analytics |
| `position_size` | kelly, half_kelly, fixed_fractional, max_risk | Trade sizing |
| `currency` | format, convert, parse | Money display |

## Quick Examples

### Percentage Change
```
"What's the % change from $150 to $200?"
→ percentage(mode="change", from_value=150, to_value=200)
→ {"ok": true, "result": "33.3333333333", "message": "150 → 200 = +33.3333333333%"}
```

### Compound Interest
```
"$10,000 at 8% for 10 years compounded"
→ interest(mode="compound", principal=10000, rate=8, periods=10)
→ {"ok": true, "result": "21589.2499776...", "message": "10000 at 8% for 10 periods → 21589.25"}
```

### Net Present Value
```
"Investment: -$100k, then $25k, $35k, $40k, $30k at 10%"
→ tvm(mode="npv", rate=10, cashflows=[-100000, 25000, 35000, 40000, 30000])
→ {"ok": true, "npv": "2195.89", "profitable": true}
```

### Internal Rate of Return
```
"What's the IRR for these cashflows?"
→ tvm(mode="irr", cashflows=[-100000, 25000, 35000, 40000, 30000])
→ {"ok": true, "irr_percent": "10.71%", "message": "IRR = 10.71%"}
```

### Loan Comparison (Price vs SAC)
```
"Compare R$400k loan at 9.5% for 30 years"
→ amortization(mode="compare", principal=400000, annual_rate=9.5, months=360)
→ Price: R$3,250/mo, total R$1.17M | SAC: R$4,148→R$1,120/mo, total R$963k | SAC saves R$207k
```

### Portfolio Summary
```
"Analyze my monthly returns: 3.2%, 1.5%, -0.8%, 4.1%, 2.7%"
→ portfolio(mode="summary", returns=[3.2, 1.5, -0.8, 4.1, 2.7], risk_free_rate=1)
→ Sharpe: 2.34, Sortino: 5.81, Max DD: 0.8%, Vol: 6.46%
```

### Kelly Criterion
```
"Position size: 60% win rate, 2:1 reward/risk, $50k bankroll"
→ position_size(mode="kelly", win_rate=60, reward_risk_ratio=2.0, bankroll=50000)
→ Kelly: 40% → $20,000 (warning: aggressive, consider half-Kelly)
```

### Currency Formatting
```
"Format 1234567.89 as BRL"
→ currency(mode="format", value=1234567.89, code="BRL")
→ "R$1,234,567.89"
```

## Architecture

### Decimal-First Design

Every number enters the system through `_to_decimal()` which converts via string representation:

```python
# ✅ Correct — no floating-point contamination
Decimal(str(0.1))  # → Decimal('0.1')

# ❌ Wrong — inherits float imprecision
Decimal(0.1)       # → Decimal('0.1000000000000000055511151231257827021181583404541015625')
```

### Structured JSON Output

All tools return JSON with consistent structure:

```json
{
  "ok": true,
  "action": "tvm",
  "mode": "npv",
  "result": "2195.89",
  "message": "Human-readable summary",
  "...": "mode-specific fields"
}
```

Errors follow the same pattern:
```json
{
  "ok": false,
  "action": "error",
  "message": "missing required parameter(s): rate, cashflows"
}
```

### Rate Auto-Detection

Rates are auto-normalized: values > 1 are treated as percentages, ≤ 1 as decimals.

```
rate=10   → 0.10 (10%)
rate=0.10 → 0.10 (10%)
```

### Safety Limits

| Limit | Value | Purpose |
|-------|-------|---------|
| `_MAX_VALUE` | 1E15 | Prevent overflow in Decimal operations |
| `_MAX_PERIODS` | 1,200 | 100 years monthly — beyond this is unrealistic |
| `_MAX_CASHFLOWS` | 1,000 | Prevent CPU-intensive NPV/IRR loops |
| `_MAX_RETURNS` | 10,000 | Cap portfolio analytics input size |
| `_MAX_EXPRESSION_LEN` | 500 | Prevent AST parser abuse |
| `_MAX_EXPONENT` | 1,000 | Prevent `2**10000` style attacks |

### Zero External Dependencies

- **IRR**: Newton-Raphson iterative solver (≤100 iterations, 1E-10 tolerance)
- **Amortization**: Full schedule generation with summary mode for long loans
- **Portfolio**: Pure Decimal math — no numpy, no pandas, no scipy

## Supported Currencies

| Code | Symbol | Decimals | Position |
|------|--------|----------|----------|
| USD | $ | 2 | prefix |
| BRL | R$ | 2 | prefix |
| EUR | € | 2 | prefix |
| GBP | £ | 2 | prefix |
| JPY | ¥ | 0 | prefix |
| CNY | ¥ | 2 | prefix |
| BTC | ₿ | 8 | prefix |
| ETH | Ξ | 8 | prefix |
| SAT | sat | 0 | suffix |

Unknown currencies use the code as suffix: `"100.00 CHF"`.

## Testing

- **228 tests** total (215 unit + 13 property-based)
- **Hypothesis** property tests verify mathematical invariants:
  - PV↔FV roundtrip ≈ identity
  - NPV(0%) = sum(cashflows)
  - SAC total interest < Price total interest
  - Compound ≥ Simple for positive rates
  - Half-Kelly = Kelly / 2
  - Format→Parse roundtrip ≈ identity
- **mypy strict**, **ruff**, **bandit** clean

## Installation

The Financial Math plugin is built-in. No installation needed.

```python
# pyproject.toml entry point
[project.entry-points."sovyx.plugins"]
calculator = "sovyx.plugins.official.calculator:CalculatorPlugin"
financial-math = "sovyx.plugins.official.financial_math:FinancialMathPlugin"
```

Both `calculator` (backward-compatible wrapper) and `financial-math` (full version) are registered. The calculator wrapper preserves the old `name="calculator"` and plain-text output format.
