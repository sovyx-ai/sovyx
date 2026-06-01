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
GATE_TOTAL=19
FAILURES=()

ok() {
    printf '%s✓%s gate %d/%d — %s\n' "$GREEN" "$RESET" "$GATE_NUM" "$GATE_TOTAL" "$1"
}

bad() {
    printf '%s✗%s gate %d/%d — %s\n' "$RED" "$RESET" "$GATE_NUM" "$GATE_TOTAL" "$1"
    FAILURES+=("$1")
}

# Neutral outcome — a gate that is correctly not-applicable on THIS box
# (e.g. a dashboard-bundle check on a checkout with no local build). Does
# NOT count as a failure; the gate is enforced elsewhere (publish.yml).
skip() {
    printf '%s○%s gate %d/%d — SKIP: %s\n' "$YELLOW" "$RESET" "$GATE_NUM" "$GATE_TOTAL" "$1"
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
printf '%s…%s gate 5/%d — pytest (full suite, may take 5-10 min)…\n' "$YELLOW" "$RESET" "$GATE_TOTAL"
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
    elif grep -qE "[0-9]+ passed.*in [0-9]+\.[0-9]+s" "$LOG"; then
        # Windows post-pytest shutdown noise (comtypes CoUninitialize log error
        # writing to a closed stream after pytest collected its summary). The
        # framework completed cleanly — summary line confirms ``N passed, 0
        # failed`` — but the interpreter shutdown surfaces a non-zero exit.
        # Sibling of CLAUDE.md anti-pattern #30 (psutil shutdown hang) +
        # anti-pattern #22 (Windows timing noise). Pre-v0.49.10 this hit
        # verify_gates.sh as a false-positive "hang" verdict and blocked
        # local pre-push proof. Fix: when exit is nonzero AND the summary
        # line is present AND no failure count is reported, treat as success.
        PASSED=$(grep -oE "[0-9]+ passed" "$LOG" | head -1)
        ok "pytest — $PASSED (exit nonzero post-test; framework completed clean)"
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
printf '%s…%s gate 7/%d — vitest (full suite, may take 1-2 min)…\n' "$YELLOW" "$RESET" "$GATE_TOTAL"
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

# ── Gate 8: boundary round-trip coverage (Mission C2 §T4.1 + C C.6 §1) ──
# STRICT-flip Variant A (Mission C C.6 §1): harness invokes the gate
# with --scope all --strict so the all-routes coverage is enforced
# across every src/sovyx/dashboard/routes/*.py module. The script's
# own default scope stays "voice" so individual dev invocations
# (`uv run python scripts/dev/check_boundary_round_trip_coverage.py`)
# continue to produce the original voice-scope summary; only the
# harness gate is STRICT-flipped. Layer-1 rollback: remove --scope all
# --strict (script reverts to voice-scope STRICT default).
GATE_NUM=8
LOG="$LOG_DIR/08-boundary-round-trip.log"
if uv run python scripts/dev/check_boundary_round_trip_coverage.py --scope all --strict >"$LOG" 2>&1; then
    # All-scope STRICT success line format:
    # "Quality Gate 8 — boundary round-trip coverage (all-routes scope, STRICT): N file(s), M unique model(s) across K call site(s)."
    # "all-routes coverage: every model has a paired test"
    if grep -qE "all-routes coverage: every model has a paired test" "$LOG"; then
        SUMMARY=$(grep -oE "[0-9]+ unique model\(s\) across [0-9]+ call site\(s\)" "$LOG" | head -1)
        ok "boundary round-trip coverage — $SUMMARY (all-routes scope, STRICT)"
    else
        bad "boundary round-trip coverage — exit 0 but no all-routes summary line; log: $LOG"
    fi
else
    bad "boundary round-trip coverage — uncovered model(s) detected (STRICT all-scope); log: $LOG"
fi

# ── Gate 9: ladder iteration discipline (Mission C3 §T4.1) ───────────
GATE_NUM=9
LOG="$LOG_DIR/09-ladder-iteration.log"
if uv run python scripts/dev/check_ladder_iteration_discipline.py >"$LOG" 2>&1; then
    # Success line format:
    # "Quality Gate 9 — ladder iteration discipline: no anti-shape detected."
    if grep -q "no anti-shape detected" "$LOG"; then
        ok "ladder iteration discipline — no anti-pattern #41 sites"
    else
        bad "ladder iteration discipline — exit 0 but no summary line; log: $LOG"
    fi
else
    bad "ladder iteration discipline — anti-pattern #41 detected; log: $LOG"
fi

# ── Gate 10: degraded signal surface (Mission C4 §T5.1) ──────────────
GATE_NUM=10
LOG="$LOG_DIR/10-degraded-signal-surface.log"
if uv run python scripts/dev/check_degraded_signal_surface.py >"$LOG" 2>&1; then
    # Success line format:
    # "Quality Gate 10 — degraded signal surface: every degraded-signal WARN paired ..."
    if grep -q "every degraded-signal WARN paired" "$LOG"; then
        ok "degraded signal surface — no anti-pattern #42 sites"
    else
        bad "degraded signal surface — exit 0 but no summary line; log: $LOG"
    fi
else
    bad "degraded signal surface — anti-pattern #42 detected; log: $LOG"
fi

# ── Gate 11: dashboard bundle integrity (Mission C5 §T1.3) ───────────
# STRICT-when-applicable (W0.1 — MISSION-VOICE-DEEP-INVESTIGATION-2026-06-01).
# The hashed-chunk bundle only exists on disk after `npm run build`, so a
# checkout with NO local build legitimately has nothing to verify — that
# `static_dir_missing` case SKIPS (the gate is enforced STRICT in
# publish.yml against the built wheel, where the bundle always exists).
# But a bundle that IS present and PARTIAL / index-missing / no-assets now
# FAILS locally too (anti-pattern #43). This closes the prior "LENIENT
# warn forever" gap — Gate 11's STRICT target was v0.48.0 but the flip was
# never executed (V-C5-7 stamp lag) — without breaking devs who haven't
# built the dashboard. `--json` makes the verdict machine-parseable so the
# skip vs fail branch is deterministic, not a string heuristic.
GATE_NUM=11
LOG="$LOG_DIR/11-dashboard-bundle-integrity.log"
if uv run python scripts/dev/check_dashboard_bundle_integrity.py --json >"$LOG" 2>&1; then
    ok "dashboard bundle integrity — FULLY_PRESENT"
else
    if grep -q '"verdict": "static_dir_missing"' "$LOG"; then
        skip "dashboard bundle integrity — no local dashboard build (enforced STRICT in publish.yml)"
    else
        bad "dashboard bundle integrity — anti-pattern #43: bundle present but incomplete; log: $LOG"
    fi
fi

# ── Gate 12: LLM provider wire-discipline (Mission C6 §T1.4) ─────────
# Mission C6 Phase 1.A LENIENT — warn-only locally; STRICT in publish.yml's
# post-build verify (Mission C6 §T1.4). Phase 3 v0.50.0 promotes this to
# STRICT in verify_gates.sh as well, per ADR-D12.
GATE_NUM=12
LOG="$LOG_DIR/12-llm-provider-discipline.log"
if uv run python scripts/dev/check_llm_provider_discipline.py >"$LOG" 2>&1; then
    if grep -q "discipline: PASS" "$LOG"; then
        ok "llm provider discipline — PASS"
    else
        # exit 0 but no summary line — gate ran in an unexpected shape, warn
        printf '%s⚠%s gate %d/%d — llm provider discipline LENIENT warn: exit 0 without PASS line; log: %s\n' \
            "$YELLOW" "$RESET" "$GATE_NUM" "$GATE_TOTAL" "$LOG"
    fi
else
    # Non-zero exit — LENIENT phase, warn only; do NOT fail verify_gates.sh.
    # Phase 3 v0.50.0 STRICT promotion will replace this branch with `bad ...`.
    printf '%s⚠%s gate %d/%d — llm provider discipline LENIENT warn (Phase 1.A v0.49.0; STRICT at v0.50.0); log: %s\n' \
        "$YELLOW" "$RESET" "$GATE_NUM" "$GATE_TOTAL" "$LOG"
fi

# ── Gate 13: platform-neutral event names (Mission H2 §T1.5) ─────────
# Mission H2 Phase 1.A LENIENT — warn-only locally; STRICT in publish.yml's
# post-build verify (Mission H2 §T1.5). Phase 3 v0.51.0 promotes this to
# STRICT in verify_gates.sh as well, per ADR-D13. Pre-mission baseline:
# 31 violations across `_bypass_coordinator_mixin.py` (Phase 1.B target),
# `factory/_diagnostics.py` + `_apo_detector_linux.py` (Phase 1.D target),
# `_apo_detector.py` + `health/watchdog.py` (deferred to future cohort).
GATE_NUM=13
LOG="$LOG_DIR/13-platform-neutral-event-names.log"
if uv run python scripts/dev/check_platform_neutral_event_names.py >"$LOG" 2>&1; then
    if grep -q "discipline: PASS" "$LOG"; then
        ok "platform-neutral event names — PASS"
    else
        # exit 0 but violations present (LENIENT report-only) — surface as warn
        VIOLATIONS=$(grep -oE "[0-9]+ violation\(s\)" "$LOG" | head -1 || echo "0 violations")
        printf '%s⚠%s gate %d/%d — platform-neutral event names LENIENT warn: %s (Mission H2 v0.49.6; STRICT at v0.51.0); log: %s\n' \
            "$YELLOW" "$RESET" "$GATE_NUM" "$GATE_TOTAL" "$VIOLATIONS" "$LOG"
    fi
else
    # Non-zero exit — LENIENT phase, warn only; do NOT fail verify_gates.sh.
    # Phase 3 v0.51.0 STRICT promotion will replace this branch with `bad ...`.
    printf '%s⚠%s gate %d/%d — platform-neutral event names LENIENT warn (Mission H2 v0.49.6; STRICT at v0.51.0); log: %s\n' \
        "$YELLOW" "$RESET" "$GATE_NUM" "$GATE_TOTAL" "$LOG"
fi

# ── Gate 14: quarantine reason discipline (Mission H3 §T1.4) ─────────
# Mission H3 Phase 1.A LENIENT — warn-only locally; STRICT in publish.yml's
# post-build verify (Mission H3 §T1.4). Phase 3 v0.53.0 promotes this to
# STRICT in verify_gates.sh as well, per ADR-D13. Pre-mission baseline:
# 0 violations across `_quarantine.py` + `capture_integrity.py` (the only
# call sites pass `reason=_DEFAULT_QUARANTINE_REASON` which expands to a
# string literal; Gate 14 LENIENT reports this as a literal_terminal
# violation, but Phase 1.B refactors the call site to use the SSoT
# resolver before STRICT enforcement).
GATE_NUM=14
LOG="$LOG_DIR/14-quarantine-reason-discipline.log"
if uv run python scripts/dev/check_quarantine_reason_discipline.py >"$LOG" 2>&1; then
    if grep -q "discipline: PASS" "$LOG"; then
        ok "quarantine reason discipline — PASS"
    else
        # exit 0 but violations present (LENIENT report-only) — surface as warn
        VIOLATIONS=$(grep -oE "[0-9]+ violation\(s\)" "$LOG" | head -1 || echo "0 violations")
        printf '%s⚠%s gate %d/%d — quarantine reason discipline LENIENT warn: %s (Mission H3 v0.49.10; STRICT at v0.53.0); log: %s\n' \
            "$YELLOW" "$RESET" "$GATE_NUM" "$GATE_TOTAL" "$VIOLATIONS" "$LOG"
    fi
else
    # Non-zero exit — LENIENT phase, warn only; do NOT fail verify_gates.sh.
    # Phase 3 v0.53.0 STRICT promotion will replace this branch with `bad ...`.
    printf '%s⚠%s gate %d/%d — quarantine reason discipline LENIENT warn (Mission H3 v0.49.10; STRICT at v0.53.0); log: %s\n' \
        "$YELLOW" "$RESET" "$GATE_NUM" "$GATE_TOTAL" "$LOG"
fi

# ── Gate 15: resource hygiene discipline (Mission H4 §T1.4) ──────────
# Mission H4 Phase 1.A LENIENT — warn-only locally; STRICT in publish.yml's
# post-build verify (Mission H4 §T1.4). Phase 3 v0.54.0 promotes this to
# STRICT in verify_gates.sh as well, per ADR-D12. Pre-mission baseline:
# 1 known consumer-name-drift violation at `observability/anomaly.py:224`
# (reads `system.rss_bytes` which is a legacy alias of `process.rss_bytes`).
# Phase 1.B v0.49.15 renames the consumer and the violation count drops
# to 0; until then Gate 15 reports the drift in LENIENT mode.
GATE_NUM=15
LOG="$LOG_DIR/15-resource-hygiene-discipline.log"
if uv run python scripts/dev/check_resource_hygiene_discipline.py >"$LOG" 2>&1; then
    if grep -q "discipline: PASS" "$LOG"; then
        ok "resource hygiene discipline — PASS"
    else
        # exit 0 but violations present (LENIENT report-only) — surface as warn
        VIOLATIONS=$(grep -oE "[0-9]+ violation\(s\)" "$LOG" | head -1 || echo "0 violations")
        printf '%s⚠%s gate %d/%d — resource hygiene discipline LENIENT warn: %s (Mission H4 v0.49.14; STRICT at v0.54.0); log: %s\n' \
            "$YELLOW" "$RESET" "$GATE_NUM" "$GATE_TOTAL" "$VIOLATIONS" "$LOG"
    fi
else
    # Non-zero exit — LENIENT phase, warn only; do NOT fail verify_gates.sh.
    # Phase 3 v0.54.0 STRICT promotion will replace this branch with `bad ...`.
    printf '%s⚠%s gate %d/%d — resource hygiene discipline LENIENT warn (Mission H4 v0.49.14; STRICT at v0.54.0); log: %s\n' \
        "$YELLOW" "$RESET" "$GATE_NUM" "$GATE_TOTAL" "$LOG"
fi

# ── Gate 16: zod twin completeness (Mission C §C.0 + C.2) ───────────
# Mission C Phase C.0-a LENIENT — warn-only locally; STRICT in publish.yml
# post-build verify is NOT yet wired (Phase C.0-a foundation ship). Phase
# 3 v0.53.x promotes this to STRICT in verify_gates.sh as well, per the
# C.0 staged-adoption window. Pre-mission baseline: 12 violations across
# the `ResourceCohortMetricsSchema` twin (the C-P0-1 NOMINATED #1
# typed-view staleness). Phase C.2 closes those 12 in the same minor
# cycle; once C.2 ships the baseline drops to 0 and the STRICT-flip is
# safe.
GATE_NUM=16
LOG="$LOG_DIR/16-zod-twin-completeness.log"
if uv run python scripts/dev/check_zod_twin_completeness.py >"$LOG" 2>&1; then
    if grep -q "zod twin discipline: PASS" "$LOG"; then
        ok "zod twin completeness — PASS"
    else
        # exit 0 but violations present (LENIENT report-only) — surface as warn
        VIOLATIONS=$(grep -oE "[0-9]+ violation\(s\)" "$LOG" | head -1 || echo "0 violations")
        printf '%s⚠%s gate %d/%d — zod twin completeness LENIENT warn: %s (Mission C v0.49.38; STRICT at v0.53.x); log: %s\n' \
            "$YELLOW" "$RESET" "$GATE_NUM" "$GATE_TOTAL" "$VIOLATIONS" "$LOG"
    fi
else
    # Non-zero exit — LENIENT phase, warn only; do NOT fail verify_gates.sh.
    # Phase 3 v0.53.x STRICT promotion will replace this branch with `bad ...`.
    printf '%s⚠%s gate %d/%d — zod twin completeness LENIENT warn (Mission C v0.49.38; STRICT at v0.53.x); log: %s\n' \
        "$YELLOW" "$RESET" "$GATE_NUM" "$GATE_TOTAL" "$LOG"
fi

# ── Gate 17: response_model presence (Mission C §C.0) ───────────────
# Mission C Phase C.0-b LENIENT — warn-only locally; STRICT in
# publish.yml post-build verify is NOT yet wired. Phase C.4 progressively
# adds response_model= to the missing routes by subsystem; once that
# body work completes, the v0.53.x cut promotes this to STRICT.
# Pre-mission baseline: ~69 violations across 26 route files (Mission C
# audit Gate 18 §17). LENIENT prevents NEW additions to the backlog
# while the body work is sequenced.
GATE_NUM=17
LOG="$LOG_DIR/17-response-model-presence.log"
if uv run python scripts/dev/check_response_model_presence.py >"$LOG" 2>&1; then
    if grep -q "response_model discipline: PASS" "$LOG"; then
        ok "response_model presence — PASS"
    else
        VIOLATIONS=$(grep -oE "[0-9]+ violation\(s\)" "$LOG" | head -1 || echo "0 violations")
        printf '%s⚠%s gate %d/%d — response_model presence LENIENT warn: %s (Mission C v0.49.38; STRICT at v0.53.x); log: %s\n' \
            "$YELLOW" "$RESET" "$GATE_NUM" "$GATE_TOTAL" "$VIOLATIONS" "$LOG"
    fi
else
    printf '%s⚠%s gate %d/%d — response_model presence LENIENT warn (Mission C v0.49.38; STRICT at v0.53.x); log: %s\n' \
        "$YELLOW" "$RESET" "$GATE_NUM" "$GATE_TOTAL" "$LOG"
fi

# ── Gate 18: boundary helper realism (Mission C §C.0) ───────────────
# Mission C Phase C.0-c LENIENT — warn-only locally; STRICT in
# publish.yml post-build verify is NOT yet wired. Phase C.6 progressively
# migrates inline `helper_factory=lambda:` callsites to named callables
# that mirror the real producer's runtime-bound dict shape (anti-pattern
# #40 sibling; closes proposed anti-pattern #60). Pre-mission baseline:
# 51 violations across 5 boundary test files.
GATE_NUM=18
LOG="$LOG_DIR/18-boundary-helper-real.log"
if uv run python scripts/dev/check_boundary_helper_real.py >"$LOG" 2>&1; then
    if grep -q "boundary helper discipline: PASS" "$LOG"; then
        ok "boundary helper realism — PASS"
    else
        VIOLATIONS=$(grep -oE "[0-9]+ violation\(s\)" "$LOG" | head -1 || echo "0 violations")
        printf '%s⚠%s gate %d/%d — boundary helper realism LENIENT warn: %s (Mission C v0.49.38; STRICT at v0.53.x); log: %s\n' \
            "$YELLOW" "$RESET" "$GATE_NUM" "$GATE_TOTAL" "$VIOLATIONS" "$LOG"
    fi
else
    printf '%s⚠%s gate %d/%d — boundary helper realism LENIENT warn (Mission C v0.49.38; STRICT at v0.53.x); log: %s\n' \
        "$YELLOW" "$RESET" "$GATE_NUM" "$GATE_TOTAL" "$LOG"
fi

# ── Gate 19: name-lock integrity (Mission Ω-3, anti-pattern #68 DRAFT) ─────
# Mission Ω-3 Phase T0 LENIENT — warn-only locally; STRICT at v0.52.0.
# Asserts every docs-internal/* path reference in src/sovyx docstrings
# resolves to an existing file. A dead link ships to PyPI as a public
# dead doc link (CLAUDE.md Git section). Past archive moves silently
# rotted 34 such links (Mission Ω-3 T0 repair); baseline after repair: 0.
# Bare spec-ID citations (no docs-internal/ prefix) are provenance, not
# path links, and are deliberately not flagged.
GATE_NUM=19
LOG="$LOG_DIR/19-name-lock-integrity.log"
if uv run python scripts/dev/check_name_lock_integrity.py >"$LOG" 2>&1; then
    if grep -q "name-lock integrity: PASS" "$LOG"; then
        ok "name-lock integrity — PASS"
    else
        # exit 0 but no PASS line — unexpected shape, warn
        printf '%s⚠%s gate %d/%d — name-lock integrity LENIENT warn: exit 0 without PASS line; log: %s\n' \
            "$YELLOW" "$RESET" "$GATE_NUM" "$GATE_TOTAL" "$LOG"
    fi
else
    # Non-zero exit — LENIENT phase, warn only; do NOT fail verify_gates.sh.
    # v0.52.0 STRICT promotion will replace this branch with `bad ...`.
    VIOLATIONS=$(grep -oE "[0-9]+ violation\(s\)" "$LOG" | head -1 || echo "0 violations")
    printf '%s⚠%s gate %d/%d — name-lock integrity LENIENT warn: %s (Mission Ω-3 v0.49.x; STRICT at v0.52.0); log: %s\n' \
        "$YELLOW" "$RESET" "$GATE_NUM" "$GATE_TOTAL" "$VIOLATIONS" "$LOG"
fi

# ── Final verdict ────────────────────────────────────────────────────
echo ""
if [[ ${#FAILURES[@]} -eq 0 ]]; then
    printf '%s🟢 all %d gates verified GREEN via summary lines (not exit codes).%s\n' "$GREEN" "$GATE_TOTAL" "$RESET"
    printf '%slogs:%s %s\n' "$YELLOW" "$RESET" "$LOG_DIR"
    # Write marker for pre-push hook: HEAD SHA + epoch.
    # The hook reads this to verify gates ran against the current
    # HEAD recently. Marker is in .git/ (not tracked) so each clone
    # builds its own fresh proof history.
    GIT_DIR=$(git rev-parse --git-dir 2>/dev/null || echo ".git")
    HEAD_SHA=$(git rev-parse HEAD 2>/dev/null || echo "no-head")
    EPOCH=$(date +%s)
    printf '%s\n%s\n' "$HEAD_SHA" "$EPOCH" > "$GIT_DIR/.last-gates-pass"
    printf '%smarker:%s %s/.last-gates-pass (HEAD=%s epoch=%s)\n' "$YELLOW" "$RESET" "$GIT_DIR" "${HEAD_SHA:0:8}" "$EPOCH"
    exit 0
else
    printf '%s🔴 %d/%d gates FAILED:%s\n' "$RED" "${#FAILURES[@]}" "$GATE_TOTAL" "$RESET"
    for f in "${FAILURES[@]}"; do
        printf '   - %s\n' "$f"
    done
    printf '%slogs:%s %s\n' "$YELLOW" "$RESET" "$LOG_DIR"
    # Invalidate any stale marker — gate failure means no proof.
    GIT_DIR=$(git rev-parse --git-dir 2>/dev/null || echo ".git")
    rm -f "$GIT_DIR/.last-gates-pass"
    exit 1
fi
