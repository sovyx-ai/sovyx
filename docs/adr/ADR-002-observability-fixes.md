# ADR-002: Observability Lacunas Post-Review

**Status:** Accepted  
**Date:** 2026-04-04  
**Context:** Post-OBS-07 code review identified 4 issues requiring correction before proceeding to Dashboard phase.

---

## Issue 1: `health.py` not exported in `__init__.py`

### Problem
`src/sovyx/observability/__init__.py` exports logging, metrics, and tracing modules but **completely omits** health.py. The health module (616 lines, 10 check classes, `HealthRegistry`, `CheckStatus`, `CheckResult`, `create_default_registry`) is not part of the package's public API.

This violates Python packaging best practices: the `__init__.py` file defines the public surface of a package. An omitted module is effectively invisible to consumers who rely on `from sovyx.observability import ...`.

### Decision
Export only the **stable public API** from health.py — the types consumers need, the registry, and the factory:

```python
# In __init__.py — add:
from sovyx.observability.health import (
    CheckResult,
    CheckStatus,
    HealthCheck,
    HealthRegistry,
    create_default_registry,
)
```

Individual check classes (`DiskSpaceCheck`, `RAMCheck`, etc.) are **implementation details**. Users create them via `create_default_registry()` or register custom ones implementing `HealthCheck`. Following the principle of minimal public surface.

Add all new exports to `__all__`.

---

## Issue 2: `sovyx doctor` CLI not wired to health.py

### Problem
The `doctor` command in `cli/main.py` (line 186) has two modes:
1. **Daemon running**: calls `client.call("doctor")` via RPC — returns whatever the daemon implements (not our health.py)
2. **Daemon not running**: checks only `data_dir.exists()` and `system.yaml.exists()` — 2 trivial checks vs. our 10 comprehensive checks

The 10 health checks we built in OBS-04 are **never called by the CLI**. They exist in isolation.

### Analysis: Offline vs Online checks
Our 10 checks fall into two categories:

| Check | Needs daemon? | Offline-capable? |
|-------|:---:|:---:|
| DiskSpaceCheck | No | ✅ |
| RAMCheck | No | ✅ |
| CPUCheck | No | ✅ |
| DatabaseCheck | No* | ✅ (probes SQLite file directly) |
| BrainIndexedCheck | Yes | ❌ (needs live BrainService) |
| LLMReachableCheck | Yes | ❌ (needs provider instances) |
| ModelLoadedCheck | No | ✅ (checks if model files exist) |
| ChannelConnectedCheck | Yes | ❌ (needs live channels) |
| ConsolidationCheck | Yes | ❌ (needs running cycle) |
| CostBudgetCheck | Yes | ❌ (needs CostGuard state) |

*DatabaseCheck takes a `write_fn` callback. Without daemon, we can do a read-only probe (file exists + readable) but not write test.

### Decision
Rewrite `sovyx doctor` with two tiers:

**Tier 1 — Always available (offline):**
- DiskSpaceCheck, RAMCheck, CPUCheck, ModelLoadedCheck
- DatabaseCheck in read-only mode (file exists + size > 0)
- Config validation (system.yaml parseable)

**Tier 2 — Daemon required (online):**
- All 10 checks via RPC call to daemon (daemon registers full HealthRegistry at startup)
- Rich table output with status colors (green/yellow/red)

Output format: Rich Table with columns [Status, Check, Message, Details].

### Implementation
1. Create `run_offline_checks()` in health.py — runs only the offline-capable checks
2. Rewrite `doctor` command to:
   - Always run offline checks
   - If daemon running: also run online checks via RPC
   - Display results in a Rich table with colored status icons
3. Add `--json` flag for machine-readable output

---

## Issue 3: Duplicate `messages_processed` metric

### Problem
`sovyx.messages.processed` is incremented in TWO places:

1. **`bridge/manager.py:147`** — `handle_inbound()` entry point, with `{"channel": message.channel_type.value}`
2. **`cognitive/loop.py:169`** — after successful cognitive loop completion, with `{"mind_id": str(request.mind_id)}`

These measure **different things** at **different points** in the pipeline:
- Bridge: "a message arrived from a channel" (could fail before reaching cognitive loop)
- Loop: "a message was fully processed through perceive→attend→think→act→reflect"

Using the same metric name with different attributes and different semantics is a violation of OTel semantic conventions: *"As a rule of thumb, aggregations over all the attributes of a given metric SHOULD be meaningful"* (Prometheus/OTel naming guidelines).

### Research: OTel Messaging Semantic Conventions
OTel defines:
- `messaging.client.operation.duration` — for receive operations
- `messaging.process.duration` — for processing operations
- `messaging.client.consumed.messages` — count per delivery, reported ONCE

Key quote: *"This metric SHOULD be reported once per message delivery. For example, if receiving and processing operations are both instrumented for a single message delivery, this counter is incremented when the message is received and not reported when it is processed."*

### Decision
Rename to two distinct metrics following OTel-inspired naming:

| Old | New | Location | Meaning |
|-----|-----|----------|---------|
| `sovyx.messages.processed` (bridge) | `sovyx.messages.received` | bridge/manager.py | Message arrived from channel |
| `sovyx.messages.processed` (loop) | `sovyx.messages.processed` | cognitive/loop.py | Message fully processed through loop |

This gives us a clear **funnel metric**: received → processed. The difference = messages that failed/dropped in pipeline.

In `metrics.py`, add `messages_received` counter alongside the existing `messages_processed`.

Update attributes:
- `messages_received`: `{channel: str}` — which channel it came from
- `messages_processed`: `{mind_id: str, status: "ok"|"degraded"|"error"}` — outcome

---

## Issue 4: `_follow_log` untested behind `pragma: no cover`

### Problem
30 lines of real logic (file watching, JSON parsing, filtering, output) excluded from testing via `pragma: no cover`. This is technical debt hidden by a pragma.

### Analysis
The function is inherently blocking (infinite `while True` with `readline()` + `sleep(0.1)`). Traditional unit testing approaches:

1. **Thread + timeout** — fragile, timing-dependent, flaky in CI
2. **Mock file object** — possible but doesn't test real file I/O
3. **Refactor to generator** — extract the core logic into a testable generator, keep the blocking loop as a thin wrapper

### Decision
**Refactor approach:**

```python
# Testable generator (pure logic, no blocking)
def _iter_new_lines(file_handle: TextIO) -> Generator[dict, None, None]:
    """Yield parsed JSON entries as they appear."""
    while True:
        line = file_handle.readline()
        if not line:
            return  # caller decides whether to retry
        line = line.strip()
        if not line:
            continue
        try:
            yield json.loads(line)
        except json.JSONDecodeError:
            continue

# Thin blocking wrapper (stays pragma: no cover)
def _follow_log(...):
    with open(log_file) as f:
        f.seek(0, 2)
        while True:
            for entry in _iter_new_lines(f):
                if _matches(entry, ...):
                    display(entry)
            time.sleep(0.1)
```

The generator `_iter_new_lines` is fully testable with `io.StringIO`. The blocking loop wrapper is ~10 lines and stays `pragma: no cover`.

This reduces untested logic from 30 lines to ~10 lines.

---

## Execution Plan

### PR: `obs/review-fixes`

**Step 1 — health.py exports** (~5 min)
- Update `observability/__init__.py` with health exports
- Update `__all__`

**Step 2 — Wire doctor command** (~20 min)
- Add `run_offline_checks()` to health.py
- Rewrite `doctor` in cli/main.py using Rich Table + health checks
- Support `--json` flag
- Tests for offline doctor flow

**Step 3 — Fix duplicate metric** (~10 min)
- Add `messages_received` counter to MetricsRegistry
- Rename bridge usage from `messages_processed` to `messages_received`
- Update metrics test

**Step 4 — Refactor _follow_log** (~15 min)
- Extract `_iter_new_lines` generator
- Test generator with StringIO
- Keep blocking wrapper as pragma: no cover

**Step 5 — Full quality gates**
- ruff check + format
- mypy --strict
- bandit
- pytest (all)
- coverage ≥95% on modified files

**Estimated total:** ~50 min
