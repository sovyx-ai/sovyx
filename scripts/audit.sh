#!/usr/bin/env bash
# Sovyx — Enterprise Integrity Audit
# Verifies ALL quality dimensions in one pass.
# Exit 0 = market-ready. Any failure = exit 1.
# Usage: ./scripts/audit.sh
set -uo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO_ROOT"

FAIL=0
PASS=0
TOTAL=0

pass() {
  PASS=$((PASS + 1))
  TOTAL=$((TOTAL + 1))
  echo "  ✅ $1"
}

fail() {
  FAIL=$((FAIL + 1))
  TOTAL=$((TOTAL + 1))
  echo "  ❌ $1"
}

section() {
  echo ""
  echo "═══ $1 ═══"
}

# ─── 1. i18n — No hardcoded English in TSX components ───
section "1/10 i18n — Hardcoded strings"

# Check for defaultValue="SomeEnglish" without i18n-ok comment
HARDCODED=$(grep -rn --include="*.tsx" \
  -E 'defaultValue="[A-Z][a-z]{2,}"' \
  dashboard/src/pages/ dashboard/src/components/ 2>/dev/null \
  | grep -v '\.test\.' \
  | grep -v '// i18n-ok' \
  | wc -l)

if [ "$HARDCODED" -eq 0 ]; then
  pass "No hardcoded defaultValues in components"
else
  fail "Found $HARDCODED hardcoded defaultValues"
fi

# ─── 2. TypeScript — Zero prod errors ───
section "2/10 TypeScript — Prod code"

cd "$REPO_ROOT/dashboard"
if npx tsc --noEmit -p tsconfig.app.json > /dev/null 2>&1; then
  pass "Zero TypeScript errors in production code"
else
  fail "TypeScript errors in production code"
fi
cd "$REPO_ROOT"

# ─── 3. Security — npm audit ───
section "3/10 Security — npm vulnerabilities"

cd "$REPO_ROOT/dashboard"
AUDIT_OUTPUT=$(npm audit --omit=dev 2>&1 || true)
if echo "$AUDIT_OUTPUT" | grep -qE "found 0 vulnerabilities|no vulnerabilities"; then
  pass "Zero npm vulnerabilities (production)"
else
  HIGH=$(echo "$AUDIT_OUTPUT" | grep -ciE "high|critical" 2>/dev/null || echo "0")
  if [ "$HIGH" -eq 0 ]; then
    pass "No high/critical npm vulnerabilities"
  else
    fail "npm audit: high/critical vulnerabilities found"
  fi
fi
cd "$REPO_ROOT"

# ─── 4. Accessibility ───
section "4/10 Accessibility"

# Multi-line check: find <input tags that don't have aria-label within 5 lines
# Uses perl for multi-line matching
INPUT_VIOLATIONS=$(perl -0777 -ne '
  while (/<input\b((?:(?!>|aria-label|aria-labelledby|type="hidden").)*?)>/gs) {
    # Only count if no aria-label found before closing >
    my $match = $&;
    if ($match !~ /aria-label/ && $match !~ /type="hidden"/) {
      $count++;
    }
  }
  END { print $count // 0 }
' dashboard/src/pages/*.tsx dashboard/src/components/**/*.tsx 2>/dev/null || echo "0")

if [ "$INPUT_VIOLATIONS" -eq 0 ]; then
  pass "All inputs have aria-label"
else
  fail "Found $INPUT_VIOLATIONS inputs without aria-label"
fi

# Multi-line img check
IMG_VIOLATIONS=$(perl -0777 -ne '
  while (/<img\b((?:(?!>).)*?)>/gs) {
    my $match = $&;
    if ($match !~ /alt=/) {
      $count++;
    }
  }
  END { print $count // 0 }
' dashboard/src/pages/*.tsx dashboard/src/components/**/*.tsx 2>/dev/null || echo "0")

if [ "$IMG_VIOLATIONS" -eq 0 ]; then
  pass "All images have alt attribute"
else
  fail "Found $IMG_VIOLATIONS images without alt"
fi

# ─── 5. Dashboard build ───
section "5/10 Dashboard — Build"

cd "$REPO_ROOT/dashboard"
if npm run build > /dev/null 2>&1; then
  pass "Dashboard builds cleanly"
else
  fail "Dashboard build failed"
fi
cd "$REPO_ROOT"

# ─── 6. Dashboard tests ───
section "6/10 Dashboard — Tests"

cd "$REPO_ROOT/dashboard"
TEST_OUTPUT=$(npx vitest run 2>&1 || true)

if echo "$TEST_OUTPUT" | grep -qE "[0-9]+ failed"; then
  NFAIL=$(echo "$TEST_OUTPUT" | grep -oP '\d+(?=\s+failed)' | head -1)
  NPASS=$(echo "$TEST_OUTPUT" | grep -oP '\d+(?=\s+passed)' | head -1)
  fail "Dashboard tests: ${NFAIL:-?} failed (${NPASS:-?} passed)"
elif echo "$TEST_OUTPUT" | grep -qP '\d+\s+passed'; then
  NPASS=$(echo "$TEST_OUTPUT" | grep -oP '\d+(?=\s+passed)' | head -1)
  pass "Dashboard tests: ${NPASS} passed"
else
  fail "Dashboard tests did not complete"
fi
cd "$REPO_ROOT"

# ─── 7. Backend lint (ruff) ───
section "7/10 Backend — Lint"

if uv run ruff check src/ tests/ > /dev/null 2>&1; then
  pass "Ruff lint: zero issues"
else
  fail "Ruff lint found issues"
fi

# ─── 8. Backend type check (mypy) ───
section "8/10 Backend — Type check"

if uv run mypy src/ > /dev/null 2>&1; then
  pass "mypy: zero errors"
else
  fail "mypy found errors"
fi

# ─── 9. Backend security (bandit) ───
section "9/10 Backend — Security scan"

if uv run bandit -r src/ -c pyproject.toml -q > /dev/null 2>&1; then
  pass "Bandit: zero issues"
else
  fail "Bandit found security issues"
fi

# ─── 10. Backend tests ───
section "10/10 Backend — Tests"

BACKEND_OUTPUT=$(uv run python -m pytest tests/ --tb=no -q --timeout=30 2>&1 || true)
if echo "$BACKEND_OUTPUT" | grep -qP '\d+\s+passed'; then
  BK_PASS=$(echo "$BACKEND_OUTPUT" | grep -oP '\d+(?=\s+passed)' | tail -1)
  if echo "$BACKEND_OUTPUT" | grep -qP '\d+\s+failed'; then
    BK_FAIL=$(echo "$BACKEND_OUTPUT" | grep -oP '\d+(?=\s+failed)' | tail -1)
    fail "Backend tests: ${BK_FAIL} failed (${BK_PASS} passed)"
  else
    pass "Backend tests: ${BK_PASS} passed"
  fi
else
  fail "Backend tests did not complete"
fi

# ─── Summary ───
echo ""
echo "╔══════════════════════════════════════╗"
echo "║     SOVYX ENTERPRISE AUDIT v0.5      ║"
echo "╠══════════════════════════════════════╣"
printf "║  Checks: %2d passed / %2d total        ║\n" "$PASS" "$TOTAL"
if [ "$FAIL" -eq 0 ]; then
  echo "║  Status: ✅ MARKET-READY             ║"
else
  printf "║  Status: ❌ %2d FAILURE(S)            ║\n" "$FAIL"
fi
echo "╚══════════════════════════════════════╝"
echo ""

if [ "$FAIL" -gt 0 ]; then
  exit 1
fi

echo "🔮 Enterprise audit passed. Code is market-ready."
exit 0
