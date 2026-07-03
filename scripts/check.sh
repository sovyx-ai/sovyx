#!/usr/bin/env bash
# Sovyx — Quick backend-only quality pass (lint + mypy + bandit + pytest).
# Does NOT run the dashboard gates nor gates 8-19; the pre-push source
# of truth is scripts/verify_gates.sh.
# Usage: ./scripts/check.sh
set -euo pipefail

echo "=== 1/4 Lint (ruff) ==="
uv run ruff check src/ tests/
uv run ruff format --check src/ tests/
echo "✅ Lint passed"
echo ""

echo "=== 2/4 Type Check (mypy) ==="
uv run mypy src/
echo "✅ Type check passed"
echo ""

echo "=== 3/4 Security (bandit) ==="
uv run bandit -r src/ -c pyproject.toml
echo "✅ Security passed"
echo ""

echo "=== 4/4 Tests + Coverage ==="
uv run python -m pytest tests/ --cov=sovyx --cov-report=term-missing --cov-fail-under=95 -v
echo "✅ Tests passed"
echo ""

echo "🔮 All checks passed. Code is clean."
