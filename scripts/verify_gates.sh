#!/usr/bin/env bash
# verify_gates.sh — pre-bump verification (forcing function)
#
# Runs every quality gate that the publish.yml CI workflow runs, AND
# verifies each one by reading the actual summary line (not the exit
# code). Exits non-zero if ANY gate's summary contains "failed".
#
# Why this script exists:
#   - The harness's `(exit code N)` summary in completion notifications
#     is unreliable when the test command is piped to `tail -N`. Default
#     bash without `pipefail` reports the LAST pipe stage's exit code
#     (tail is always 0). 4 consecutive bump cycles (v0.41.3 → v0.42.1)
#     shipped with CI-red regressions because pre-commit pytest reported
#     "exit code 0" while pytest was actually returning 1.
#   - This script uses `set -euo pipefail` + explicit grep on the
#     summary line of each gate's output. The grep's exit code is the
#     gate verdict — independent of how the test runner reports.
#
# Codified by:
#   feedback_ci_preflight.md Addendum 2026-05-14
#   feedback_no_speculation.md Addendum 2026-05-14 (source-of-truth principle)
#   MISSION-post-v0_42_2-quality-discipline-2026-05-14.md Phase 2
#
# Usage:
#   ./scripts/verify_gates.sh
#
# Exit codes:
#   0 — all gates verified clean via summary line
#   1 — at least one gate has "failed" in summary
#   2 — a gate didn't produce expected summary (hang / timeout / OOM)

set -euo pipefail

LOG_DIR="${TMPDIR:-/tmp}/sovyx-verify-gates"
mkdir -p "$LOG_DIR"

# Color (only when TTY)
if [[ -t 1 ]]; then
    GREEN=$(printf '\033[32m')
    RED=$(printf '\033[31m')
    YELLOW=$(printf '\033[33m')
    RESET=$(printf '\033[0m')
else
    GREEN=""; RED=""; YELLOW=""; RESET=""
fi

GATE_NUM=0
GATE_TOTAL=7
FAILURES=()

ok() {
    printf '%s✓%s gate %d/%d — %s\n' "$GREEN" "$RESET" "$GATE_NUM" "$GATE_TOTAL" "$1"
}

bad() {
    printf '%s✗%s gate %d/%d — %s\n' "$RED" "$RESET" "$GATE_NUM" "$GATE_TOTAL" "$1"
    FAILURES+=("$1")
}

# ── Gate 1: ruff lint ────────────────────────────────────────────────
GATE_NUM=1
LOG="$LOG_DIR/01-ruff-lint.log"
if uv run ruff check src/ tests/ >"$LOG" 2>&1; then
    # Verify by output line too — ruff prints "All checks passed!"
    if grep -q "All checks passed" "$LOG"; then
        ok "ruff check (lint) — All checks passed"
    else
        bad "ruff check — exit 0 but no 'All checks passed' line; log: $LOG"
    fi
else
    bad "ruff check — non-zero exit; log: $LOG"
fi

# ── Gate 2: ruff format --check ──────────────────────────────────────
GATE_NUM=2
LOG="$LOG_DIR/02-ruff-format.log"
if uv run ruff format --check src/ tests/ >"$LOG" 2>&1; then
    if grep -qE "[0-9]+ files? already formatted" "$LOG"; then
        SUMMARY=$(grep -oE "[0-9]+ files? already formatted" "$LOG" | head -1)
        ok "ruff format --check — $SUMMARY"
    else
        bad "ruff format --check — exit 0 but no 'already formatted' line; log: $LOG"
    fi
else
    bad "ruff format --check — non-zero exit; log: $LOG"
fi

# ── Gate 3: mypy strict ──────────────────────────────────────────────
GATE_NUM=3
LOG="$LOG_DIR/03-mypy.log"
if uv run mypy src/ >"$LOG" 2>&1; then
    if grep -qE "Success: no issues found in [0-9]+ source files" "$LOG"; then
        SUMMARY=$(grep -oE "Success: no issues found in [0-9]+ source files" "$LOG" | head -1)
        ok "mypy strict — $SUMMARY"
    else
        bad "mypy — exit 0 but no Success line; log: $LOG"
    fi
else
    bad "mypy — non-zero exit; log: $LOG"
fi

# ── Gate 4: bandit ───────────────────────────────────────────────────
GATE_NUM=4
LOG="$LOG_DIR/04-bandit.log"
if uv run bandit -r src/sovyx/ --configfile pyproject.toml >"$LOG" 2>&1; then
    # Bandit prints "Medium: N" + "High: N" — both must be 0
    HIGH=$(grep -oE "High:\s*[0-9]+" "$LOG" | head -1 | grep -oE "[0-9]+" || echo "?")
    MEDIUM=$(grep -oE "Medium:\s*[0-9]+" "$LOG" | head -1 | grep -oE "[0-9]+" || echo "?")
    if [[ "$HIGH" == "0" && "$MEDIUM" == "0" ]]; then
        ok "bandit — Medium: 0, High: 0"
    else
        bad "bandit — Medium: $MEDIUM, High: $HIGH (non-zero); log: $LOG"
    fi
else
    bad "bandit — non-zero exit; log: $LOG"
fi

# ── Gate 5: pytest (full suite, --ignore=tests/smoke per CLAUDE.md) ──
GATE_NUM=5
LOG="$LOG_DIR/05-pytest.log"
printf '%s…%s gate 5/7 — pytest (full suite, may take 5-10 min)…\n' "$YELLOW" "$RESET"
# Run without -v to avoid Windows CI hang (per Mission Phase 3 hypothesis).
# `pipefail` ensures pytest's exit code propagates through tee.
# pytest summary line formats:
#   -q mode:        `N failed, M passed, K skipped, ... in T.Ts (H:MM:SS)`  (no `=` decoration)
#   -v / default:   `========= N passed, ... in T.Ts =========`            (decorated)
# Match both: look for "N passed" or "N failed" followed by "in T" duration token,
# OR the decorated `^=+ ... [0-9]+ (passed|failed)` shape.
SUMMARY_RE='([0-9]+ (passed|failed).*in [0-9]+\.[0-9]+s|^=+ .*[0-9]+ (passed|failed))'
if uv run python -m pytest tests/ --ignore=tests/smoke --timeout=30 -q >"$LOG" 2>&1; then
    # Even if exit is 0, GREP the summary line to be sure
    if grep -qE "[0-9]+ failed.*in [0-9]+\.[0-9]+s" "$LOG"; then
        FAILED=$(grep -oE "[0-9]+ failed" "$LOG" | head -1)
        bad "pytest — exit 0 but summary line says $FAILED; log: $LOG"
    elif grep -qE "$SUMMARY_RE" "$LOG"; then
        PASSED=$(grep -oE "[0-9]+ passed" "$LOG" | head -1)
        ok "pytest — $PASSED (verified via summary line, not exit code)"
    else
        bad "pytest — exit 0 but NO summary line; run may have hung; log: $LOG"
        exit 2
    fi
else
    if grep -qE "[0-9]+ failed.*in [0-9]+\.[0-9]+s" "$LOG"; then
        FAILED=$(grep -oE "[0-9]+ failed" "$LOG" | head -1)
        bad "pytest — $FAILED; log: $LOG"
    else
        bad "pytest — non-zero exit, NO summary line; likely hung; log: $LOG"
        exit 2
    fi
fi

# ── Gate 6: dashboard tsc ────────────────────────────────────────────
GATE_NUM=6
LOG="$LOG_DIR/06-tsc.log"
if (cd dashboard && npx tsc -b tsconfig.app.json) >"$LOG" 2>&1; then
    # tsc with no errors produces no output; exit 0 = clean
    if [[ ! -s "$LOG" ]] || ! grep -qE "error TS[0-9]+" "$LOG"; then
        ok "tsc — no type errors"
    else
        ERRS=$(grep -cE "error TS[0-9]+" "$LOG")
        bad "tsc — $ERRS type errors despite exit 0?; log: $LOG"
    fi
else
    ERRS=$(grep -cE "error TS[0-9]+" "$LOG" || echo "?")
    bad "tsc — $ERRS type errors; log: $LOG"
fi

# ── Gate 7: dashboard vitest ─────────────────────────────────────────
GATE_NUM=7
LOG="$LOG_DIR/07-vitest.log"
printf '%s…%s gate 7/7 — vitest (full suite, may take 1-2 min)…\n' "$YELLOW" "$RESET"
if (cd dashboard && npx vitest run --reporter=dot) >"$LOG" 2>&1; then
    if grep -qE "Tests +[0-9]+ failed" "$LOG"; then
        FAILED=$(grep -oE "[0-9]+ failed" "$LOG" | head -1)
        bad "vitest — exit 0 but $FAILED in summary; log: $LOG"
    elif grep -qE "Tests +[0-9]+ passed \([0-9]+\)" "$LOG"; then
        SUMMARY=$(grep -E "Tests +[0-9]+ passed" "$LOG" | head -1 | sed 's/^[[:space:]]*//')
        ok "vitest — $SUMMARY"
    else
        bad "vitest — exit 0 but NO summary line; log: $LOG"
        exit 2
    fi
else
    if grep -qE "Tests +[0-9]+ failed" "$LOG"; then
        FAILED=$(grep -oE "[0-9]+ failed" "$LOG" | head -1)
        bad "vitest — $FAILED; log: $LOG"
    else
        bad "vitest — non-zero exit, no summary; log: $LOG"
        exit 2
    fi
fi

# ── Final verdict ────────────────────────────────────────────────────
echo ""
if [[ ${#FAILURES[@]} -eq 0 ]]; then
    printf '%s🟢 all %d gates verified GREEN via summary lines (not exit codes).%s\n' "$GREEN" "$GATE_TOTAL" "$RESET"
    printf '%slogs:%s %s\n' "$YELLOW" "$RESET" "$LOG_DIR"
    exit 0
else
    printf '%s🔴 %d/%d gates FAILED:%s\n' "$RED" "${#FAILURES[@]}" "$GATE_TOTAL" "$RESET"
    for f in "${FAILURES[@]}"; do
        printf '   - %s\n' "$f"
    done
    printf '%slogs:%s %s\n' "$YELLOW" "$RESET" "$LOG_DIR"
    exit 1
fi
