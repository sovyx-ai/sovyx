#!/usr/bin/env bash
# pre-release.sh — Espelha EXATAMENTE o CI antes de tagar.
# Nasceu da vergonha do v0.9.0 (12-abr-2026, 8 re-tags, 1h de loop).
#
# Uso: ./scripts/pre-release.sh [versão]
# Exemplo: ./scripts/pre-release.sh 0.10.0
#
# Se a versão for passada, valida que pyproject.toml bate.
# Se TUDO passar, mostra o comando de tag. Nunca taga automaticamente.

set -euo pipefail

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

FAILED=0
STEP=0

step() {
    STEP=$((STEP + 1))
    echo ""
    echo -e "${YELLOW}━━━ Step $STEP: $1 ━━━${NC}"
}

pass() {
    echo -e "${GREEN}✓ $1${NC}"
}

fail() {
    echo -e "${RED}✗ $1${NC}"
    FAILED=$((FAILED + 1))
}

cd "$(dirname "$0")/.."
echo "📁 Working directory: $(pwd)"
echo "🕐 $(date -u '+%Y-%m-%d %H:%M UTC')"

# ── Version check (optional) ──
if [ -n "${1:-}" ]; then
    step "Version check"
    PKG_VERSION=$(grep '^version' pyproject.toml | head -1 | sed 's/.*"\(.*\)"/\1/')
    if [ "$1" = "$PKG_VERSION" ]; then
        pass "pyproject.toml version matches: $PKG_VERSION"
    else
        fail "Version mismatch: argument=$1, pyproject.toml=$PKG_VERSION"
    fi
fi

# ── Step 1: Ruff lint (src + tests — EXATAMENTE como CI) ──
step "Ruff check (src/ tests/)"
if uv run ruff check src/ tests/; then
    pass "ruff check"
else
    fail "ruff check"
fi

step "Ruff format check (src/ tests/)"
if uv run ruff format --check src/ tests/; then
    pass "ruff format"
else
    fail "ruff format"
fi

# ── Step 2: mypy (src/ inteiro — EXATAMENTE como CI) ──
step "Type check — mypy src/"
if uv run mypy src/; then
    pass "mypy"
else
    fail "mypy"
fi

# ── Step 3: Security scan ──
step "Security — bandit"
if uv run bandit -r src/sovyx/ --configfile pyproject.toml; then
    pass "bandit"
else
    fail "bandit"
fi

# ── Step 4: Install extra test deps (EXATAMENTE como CI) ──
step "Install extra test deps (ddgs, trafilatura)"
if uv pip install "ddgs>=9.0" "trafilatura>=2.0" --quiet 2>/dev/null; then
    pass "ddgs + trafilatura installed"
else
    fail "ddgs + trafilatura install"
fi

# ── Step 5: Tests with coverage on BOTH Pythons (EXATAMENTE como CI) ──
for PYVER in 3.11 3.12; do
    step "Tests — Python $PYVER"
    
    # Ensure Python version is available
    if ! uv python install "$PYVER" --quiet 2>/dev/null; then
        fail "Python $PYVER not available"
        continue
    fi
    
    # Sync deps for this Python version
    if ! uv sync --dev --python "$PYVER" --quiet 2>/dev/null; then
        fail "uv sync for Python $PYVER"
        continue
    fi
    
    # Install extras for this Python version
    uv pip install --python "$PYVER" "ddgs>=9.0" "trafilatura>=2.0" --quiet 2>/dev/null
    
    if uv run --python "$PYVER" python -m pytest tests/ \
        --ignore=tests/smoke \
        --cov=sovyx \
        --cov-report=term-missing \
        --cov-fail-under=95 \
        -q; then
        pass "pytest Python $PYVER + coverage ≥95%"
    else
        fail "pytest Python $PYVER"
    fi
done

# ── Step 6: Dashboard ──
if [ -d "dashboard" ]; then
    step "Dashboard — TypeScript + tests + build"
    cd dashboard

    if [ -f "package-lock.json" ]; then
        npm ci --silent 2>/dev/null
    elif [ -f "pnpm-lock.yaml" ]; then
        pnpm install --frozen-lockfile --silent 2>/dev/null
    fi

    DASH_FAIL=0
    if npx tsc -b tsconfig.app.json; then
        pass "tsc"
    else
        fail "tsc"; DASH_FAIL=1
    fi

    if npx vitest run 2>/dev/null; then
        pass "vitest"
    else
        fail "vitest"; DASH_FAIL=1
    fi

    if npm run build --silent 2>/dev/null; then
        pass "build"
    else
        fail "build"; DASH_FAIL=1
    fi

    cd ..

    if [ -f "src/sovyx/dashboard/static/index.html" ]; then
        pass "static assets exist"
    else
        fail "static assets missing — dashboard build didn't copy"
    fi
fi

# ── Result ──
echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
if [ $FAILED -eq 0 ]; then
    echo -e "${GREEN}🎯 ALL CHECKS PASSED${NC}"
    if [ -n "${1:-}" ]; then
        echo ""
        echo "  git tag v$1 && git push origin v$1"
        echo ""
    else
        echo ""
        echo "  Ready to tag. Run:"
        echo "  git tag vX.Y.Z && git push origin vX.Y.Z"
        echo ""
    fi
else
    echo -e "${RED}💀 $FAILED CHECK(S) FAILED — DO NOT TAG${NC}"
    echo ""
    echo "  Fix the failures above, then run this script again."
    echo ""
    exit 1
fi
