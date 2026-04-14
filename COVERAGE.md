# Coverage Policy

## Target

- **Backend**: ≥ 95 % line coverage per modified file. CI runs `pytest --cov` on Python 3.11 and 3.12.
- **Frontend**: every page and every component with meaningful interaction logic must have a colocated `*.test.tsx`. Vitest runs on every CI pass.

## Current state

| Suite | Tests | Notes |
|---|---:|---|
| Backend (`tests/`, minus `tests/smoke`) | ~7 820 | Runs on Python 3.11 **and** 3.12; identical pass rate required. |
| Frontend (`dashboard/src/**/*.test.ts(x)`) | 767 | jsdom, vitest. |
| **Total** | **~8 587** | — |

CI gate: the `Test (Python X)` jobs fail on any `^=+ .* [0-9]+ failed` in the pytest summary; `Dashboard Build` fails on any vitest failure or `tsc` diagnostic.

## Running coverage locally

```bash
make test-cov                          # backend, enforces --cov-fail-under=95
uv run python -m pytest tests/ \
    --ignore=tests/smoke \
    --timeout=30 \
    --cov=sovyx \
    --cov-report=term-missing \
    --cov-report=xml
```

The `coverage.xml` is uploaded as a CI artifact (`coverage-report`) on the 3.12 job. Pull it from the workflow run page to inspect uncovered lines.

## `pragma: no cover` policy

Coverage exclusions are limited to three categories, all audited:

| Category | Typical modules | Why excluded |
|---|---|---|
| OS-interaction paths | `engine/health.py`, `engine/lifecycle.py`, `cli/rpc_client.py` | `/proc`, psutil, signals, PID files, Unix sockets — not reproducible in-process. |
| Hardware-dependent | `voice/audio.py`, `voice/wake_word.py` | Audio device initialization, ONNX device selection. Covered in integration stress suites, not unit tests. |
| CLI presentation | `cli/main.py`, `cli/commands/*` | Typer + Rich console flows; covered via subprocess integration tests (`tests/integration/cli/`). |

No business logic is excluded. Every new `# pragma: no cover` requires a one-line justification in the same commit message.

## Related audits

- `type: ignore` annotations are audited and kept minimal; each one ties to a documented upstream stub limitation or an optional-dependency boundary.
- `noqa` annotations are dominated by `BLE001` (broad exception, tracked — see the BLE001 sweep in `CHANGELOG.md` entry for v0.11.0) and `PLC0415` (lazy imports for optional deps — intentional).
- Full anti-pattern catalog lives in [`CLAUDE.md`](CLAUDE.md).
