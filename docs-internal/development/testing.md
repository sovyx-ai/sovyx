# Testing

**Audience**: contributors writing or fixing tests.
**Scope**: test layout, coverage targets, patterns, xdist pitfalls, Hypothesis, dashboard tests, how to run each suite.
**Status**: canonical — mirrors `CLAUDE.md` testing conventions and the actual layout under `tests/`.

---

## Targets

| Metric                               | Target             | Where enforced                          |
| ------------------------------------ | ------------------ | --------------------------------------- |
| Total Python tests                   | 4,900+             | `tests/` tree                           |
| Dashboard tests                      | 400+               | `dashboard/src/**/*.test.{ts,tsx}`      |
| Per-file coverage (modified files)   | **≥95%**           | `pyproject.toml` → `fail_under = 95`    |
| Branch coverage                      | enabled            | `[tool.coverage.run] branch = true`     |
| Per-test wall clock                  | ≤20s timeout       | `uv run pytest --timeout=20`            |
| Async test mode                      | auto               | `[tool.pytest.ini_options]`             |

If coverage drops below 95% on any modified file, CI fails before merge. If a test exceeds the timeout, it is treated as a hang — fix the test, never bump the timeout silently.

---

## Test layout

```
tests/
├── conftest.py            # Hypothesis profile, data_dir/mind_dir fixtures,
│                          # rate-limiter reset (autouse)
├── unit/                  # 3,700+ — fast, isolated, per-module
│   ├── engine/            #   bootstrap, registry, lifecycle, events, config
│   ├── cognitive/         #   perceive/attend/think/act/reflect, gate, safety
│   ├── brain/             #   concepts, episodes, spreading activation, scoring
│   ├── context/           #   assembler, token budget, formatter
│   ├── mind/              #   mind config, personality
│   ├── llm/               #   provider router, complexity classifier, circuit
│   ├── voice/             #   Wyoming, VAD, wake-word, STT/TTS
│   ├── persistence/       #   pool, migrations, schemas
│   ├── observability/     #   logging, health, alerts, SLO
│   ├── bridge/            #   Telegram, Signal, manager
│   ├── cloud/             #   billing, license, backup, scheduler, dunning
│   ├── upgrade/           #   doctor, importer, migrations
│   ├── dashboard/         #   backend server unit tests
│   ├── cli/               #   Typer commands, daemon RPC client
│   ├── benchmarks/        #   budget baselines
│   ├── test_init.py              # package metadata, __version__, CLI entry
│   ├── test_packaging.py         # wheel/sdist shape
│   ├── test_edge_cases.py        # cross-cutting edge cases
│   ├── test_benchmarks.py        # perf regression baselines
│   ├── test_*_invariants.py      # Hypothesis invariants (brain, budget, cogloop)
│   └── test_*_properties.py      # property tests for API contracts / serialization
├── integration/           # 200+ — cross-module flows (engine↔brain↔llm)
├── dashboard/             # 35 — FastAPI server + adversarial + WS flows
│   ├── test_adversarial.py       # fuzz, malformed payloads, auth bypass attempts
│   ├── test_server.py            # create_app, auth, WS, SPA fallback
│   ├── test_brain_search.py      # retrieval endpoint contract
│   └── ...
├── plugins/               # 29 — sandbox, SDK, hot-reload, official plugins
├── property/              # 7 — Hypothesis (billing, brain, context, dunning,
│                          #                 serialization, spreading)
├── security/              # 6 — pii guard, financial gate, escalation, shadow
├── stress/                # 4 — load, soak, pool saturation
├── smoke/                 # 2 — fast end-to-end sanity (excluded from CI test job)
└── test_docs_smoke.py     # validates /docs site metadata
```

Subtotals (as of 2026-04-14): `unit ~27 files`, `integration ~25`, `dashboard 36`, `plugins 29`, `property 8`, `security 7`, `stress 5`, `smoke 2`.

---

## Running tests

### Full Python suite

```bash
uv run pytest tests/ --timeout=20
```

The CI job excludes `tests/smoke/` and runs on a 600-second wall-clock timeout with an extra 30-second SIGKILL grace, because async cleanup after the summary can hang for up to 20 minutes on some hosts (see `.github/workflows/ci.yml` — the deliberately convoluted `timeout | tee | grep` pipeline exists for this reason). You do not need the wrapper locally — a plain `pytest` works if your machine isn't under load.

### Subset by directory

```bash
uv run pytest tests/unit/brain/           # one module
uv run pytest tests/property/             # property tests only
uv run pytest tests/security/ -v
```

### Marker-based selection

```bash
uv run pytest -m no_islands               # brain graph connectivity regression
```

Registered markers live in `pyproject.toml` under `[tool.pytest.ini_options] markers`. Unknown markers are an error — register new ones there.

### Single test

```bash
uv run pytest tests/unit/engine/test_bootstrap.py::TestBootstrap::test_happy_path -v
```

### With coverage

```bash
uv run pytest tests/ --cov=sovyx --cov-report=term-missing --cov-report=xml
```

`fail_under = 95` will fail the run if coverage drops. The CI job uploads `coverage.xml` as an artifact from the 3.12 leg only.

### Dashboard (TypeScript)

```bash
cd dashboard
npx vitest run                 # CI mode — single pass
npx vitest                     # watch mode
npx vitest run --coverage      # coverage via @vitest/coverage-v8
```

Dashboard tests include **accessibility assertions** (keyboard nav, ARIA, focus order) via `@testing-library/react` + `@testing-library/jest-dom`. Every new interactive component must have at least one a11y test.

---

## Patterns — the non-negotiable rules

All patterns below come from `CLAUDE.md` and are enforced by reviewers. Violations are reverts, not nits.

### 1. `asyncio_mode=auto` — no `@pytest.mark.asyncio` needed

```python
# tests/unit/engine/test_events.py
class TestEventBus:
    async def test_subscribe_and_publish(self) -> None:
        bus = EventBus()
        received: list[Event] = []
        bus.subscribe(EventType.MIND_STARTED, received.append)
        await bus.publish(Event(type=EventType.MIND_STARTED, ...))
        assert len(received) == 1
```

The project-wide `asyncio_mode = "auto"` (in `[tool.pytest.ini_options]`) makes every `async def test_*` function awaitable automatically. Do not add `@pytest.mark.asyncio` unless you are customising the loop scope (rare). Occurrences of `@pytest.mark.asyncio()` in older files are being removed.

### 2. Dashboard / API auth — use `create_app(token=...)`, never monkeypatch globals

**Right (from `tests/dashboard/test_server.py`):**

```python
from sovyx.dashboard.server import create_app

_TOKEN = "test-token-fixo"

@pytest.fixture()
def client() -> TestClient:
    app = create_app(token=_TOKEN)
    return TestClient(app, headers={"Authorization": f"Bearer {_TOKEN}"})
```

**Wrong:**

```python
# ❌ Do NOT do this — it poisons global state.
def test_something(monkeypatch):
    monkeypatch.setattr("sovyx.dashboard.server._server_token", "x")
    monkeypatch.setattr("sovyx.dashboard.server._ensure_token", lambda: "x")
```

The `token=` parameter bypasses all filesystem lookups and global state. It exists specifically so tests never touch `_ensure_token` or `_server_token`. See anti-pattern #10.

### 3. `patch.object`, never string paths

**Right (from `tests/plugins/test_hot_reload.py`):**

```python
with patch.object(watcher, "_on_file_changed") as mock_on_changed:
    watcher.handle(event)
    mock_on_changed.assert_called_once()
```

**Wrong:**

```python
# ❌ String paths resolve differently under xdist reimport.
with patch("sovyx.plugins.hot_reload.watcher._on_file_changed") as m:
    ...
```

Under `pytest-xdist`, modules can be reimported in worker processes, giving you two `Watcher` classes with the same `__qualname__` but different `id()`. A string patch path re-resolves on each call and can target the wrong instance. `patch.object(imported_module, "name")` binds to the object you already imported. See anti-pattern #11.

### 4. Exception assertions — xdist-safe

**Right (from `tests/unit/engine/test_registry.py`):**

```python
with pytest.raises(Exception) as exc_info:
    registry.get("unknown-service")
assert type(exc_info.value).__name__ == "ServiceNotRegisteredError"
assert "unknown-service" in str(exc_info.value)
```

**Wrong:**

```python
# ❌ xdist reimport may produce a second class with the same name.
from sovyx.engine.registry import ServiceNotRegisteredError

with pytest.raises(ServiceNotRegisteredError):
    registry.get("unknown-service")
```

Same root cause as #3: `pytest-xdist` can instantiate modules in worker subprocesses, producing two distinct class objects. `pytest.raises(ServiceNotRegisteredError)` does an identity check; it fails if the raised exception is from the "other" module namespace. Assert on `type(exc).__name__` and the message. See anti-pattern #8.

Never use `isinstance()` for exception dispatch in production code either — use `type(exc).__name__` checks.

### 5. Every string-valued enum inherits from `StrEnum`

```python
# ✅
from enum import StrEnum

class ComplexityLevel(StrEnum):
    SIMPLE = "simple"
    MODERATE = "moderate"
    COMPLEX = "complex"

# ❌
from enum import Enum

class ComplexityLevel(Enum):   # identity comparison breaks under xdist
    SIMPLE = "simple"
    ...
```

`StrEnum` guarantees value-based equality (`ComplexityLevel.SIMPLE == "simple"` is `True`), which survives namespace duplication. See anti-pattern #9.

### 6. File-handler cleanup fixture (when your test touches logging)

```python
@pytest.fixture(autouse=True)
def _clean_handlers() -> Generator[None, None, None]:
    """Remove RotatingFileHandler instances leaked across tests."""
    yield
    root = logging.getLogger()
    for h in list(root.handlers):
        if isinstance(h, logging.handlers.RotatingFileHandler):
            h.close()
    root.handlers.clear()
```

`logging` keeps handlers on the root logger globally. Without cleanup, a later test that re-initialises logging ends up with duplicate handlers writing to stale `tmp_path` files that have already been removed, producing `FileNotFoundError` on close.

### 7. Class and test naming

```python
class TestFeatureName:
    """Short description of what's being tested."""

    def test_specific_behaviour(self, tmp_path: Path) -> None:
        """What should happen in this scenario."""
        ...
```

One class per feature. One `test_*` method per scenario. Docstrings on both. Keep each test focused on a single assertion target — "arrange, act, assert" with no shared hidden state.

### 8. Never inject fake modules into `sys.modules`

```python
# ❌ Poisons the rest of the suite.
sys.modules["sovyx.llm.openai"] = FakeOpenAIModule()
```

If you need to replace a module dependency, use `unittest.mock.patch.object` on the already-imported module, or accept an injection point in the production code. See anti-pattern #2.

### 9. Defence-in-depth is not a pattern, it's a smell

If a single test required three monkeypatches, two fixtures, and a `sys.modules` shim to pass, you have not understood which layer actually works. Find the minimal one, keep it, delete the rest. From `CLAUDE.md` anti-pattern #12:

> One layer, understood, is better than three layers, mysterious.

---

## Property-based tests (Hypothesis)

Hypothesis is used for invariant/contract testing. Configuration lives in `tests/conftest.py`.

### Global profile

```python
# tests/conftest.py
settings.register_profile(
    "sovyx",
    deadline=None,                         # disable per-example timeout
    suppress_health_check=[HealthCheck.too_slow],
)
settings.register_profile(
    "ci",
    deadline=None,
    max_examples=30,                       # smaller budget on CI
    suppress_health_check=[HealthCheck.too_slow],
)
settings.load_profile("sovyx")
```

`deadline=None` avoids flakes caused by first-run lazy loads (tiktoken, ONNX sessions) and by xdist workers under load. The `ci` profile is intentionally narrower — CI budgets beat exhaustive exploration.

### Writing a property test

```python
# tests/property/test_context_invariants.py
from hypothesis import given, settings
from hypothesis import strategies as st

class TestContextBudget:
    @given(
        prompt_tokens=st.integers(min_value=0, max_value=200_000),
        max_tokens=st.integers(min_value=1, max_value=200_000),
    )
    @settings(max_examples=20)
    def test_budget_never_negative(self, prompt_tokens: int, max_tokens: int) -> None:
        """Remaining budget is always non-negative, never exceeds max."""
        remaining = compute_remaining_budget(prompt_tokens, max_tokens)
        assert 0 <= remaining <= max_tokens
```

Rules:

- Always cap `max_examples` (20–50 for most properties; 100 for pure-math invariants).
- Use deterministic strategies (`st.from_regex`, `st.sampled_from`) for enum-like inputs.
- Avoid `st.text()` with unbounded size — bound it or use `st.characters(blacklist_characters=...)`.
- Property tests live in `tests/property/` **or** alongside unit tests as `test_*_properties.py`.

### Current property coverage

`tests/property/` — billing invariants, brain invariants, context invariants, dunning invariants, serialisation round-trip, spreading-activation invariants.

`tests/unit/test_*_properties.py` — API-contract fuzzing, brain invariants, budget invariants, cognitive-loop properties, serialisation.

---

## Dashboard tests

The `tests/dashboard/` directory tests the **backend** that serves the dashboard (FastAPI endpoints, WebSocket bridge, auth, SPA fallback). The **frontend** tests live in `dashboard/src/` colocated with the code they cover.

### Adversarial tests

`tests/dashboard/test_adversarial.py` fuzzes malformed payloads, tries auth bypass, and checks every endpoint for crash-free behaviour under hostile input. When you add a new endpoint, add its worst-case fuzz inputs here too (OWASP Top 10 style: path traversal, XSS, SQLi, oversize body, malformed JSON, wrong content-type).

### Endpoint contract tests

`tests/unit/test_api_contract_properties.py` (see the parametrised `TestConversationIdFuzz` class) enforces that **every** REST endpoint returns a predictable shape under random input and never 500s. New endpoints get a matching entry in this file.

### Frontend (vitest)

Each page/component under `dashboard/src/` has a colocated `.test.tsx` file. Conventions:

- Use `@testing-library/react` + `@testing-library/user-event`.
- Query by accessible role/name — never by CSS class or test-id unless unavoidable.
- Every interactive component has at least one keyboard-only flow test.
- Mock API calls at the `src/lib/api.ts` layer, never at `fetch()` — centralising the boundary means fewer per-test mocks.

Run them from `dashboard/`:

```bash
npx vitest run                      # one-shot
npx vitest --ui                     # interactive debugger
npx vitest run --coverage           # coverage with v8 provider
```

---

## Coverage

`pyproject.toml`:

```toml
[tool.coverage.run]
source = ["sovyx"]
branch = true

[tool.coverage.report]
fail_under = 95
show_missing = true
exclude_lines = [
    "if __name__ == .__main__.",
    "pragma: no cover",
    "if TYPE_CHECKING:",
    "\\.\\.\\.",            # bare `...` placeholders (protocols, stubs)
    "raise NotImplementedError",
]
```

Branch coverage means `if x: ...` without a covered `else` counts as uncovered. When a `pragma: no cover` is unavoidable, justify it in a comment (e.g., `pragma: no cover  # platform-specific: macOS only`).

Generate a local HTML report:

```bash
uv run pytest tests/ --cov=sovyx --cov-report=html
# open htmlcov/index.html
```

---

## xdist — what to know

Sovyx test runs can use `pytest-xdist` for parallelism. It ships with a nasty class of bugs caused by **module re-importation in worker subprocesses**.

### The failure mode

A worker subprocess imports `sovyx.engine.registry`. Another worker imports it again. Now there are two `ServiceNotRegisteredError` classes, both with `__qualname__ == "ServiceNotRegisteredError"`, but `id(cls_a) != id(cls_b)`. Any identity-based check breaks:

- `pytest.raises(ServiceNotRegisteredError)` — fails because the raised error is the "other" class.
- `isinstance(exc, ServiceNotRegisteredError)` — returns `False` for cross-worker exceptions.
- `cls is ServiceNotRegisteredError` — always wrong.

### The fix (already applied repo-wide)

1. **Assert on `type(exc).__name__`** and on substrings of `str(exc)`. Never on identity.
2. **Never use `isinstance()` for exception dispatch in production code.** Map on `type(exc).__name__`.
3. **All string-valued enums inherit from `StrEnum`.** Value equality survives duplication.
4. **Never use `patch("dotted.path")`.** Use `patch.object(module, "name")`.

If you reintroduce any of these in a PR, CI will catch it on the xdist run. If you see a test that passes locally (single-worker) but fails on CI, xdist is almost always the cause.

---

## Examples from the repo

Representative patterns you can copy verbatim.

### A — dashboard endpoint test with token auth

```python
# tests/unit/test_api_contract_properties.py
_TOKEN = "test-token-fixo"

@pytest.fixture()
async def client() -> AsyncClient:
    app = create_app(APIConfig(host="127.0.0.1", port=0), token=_TOKEN)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c

def _auth() -> dict[str, str]:
    return {"Authorization": f"Bearer {_TOKEN}"}

class TestStatusContract:
    async def test_always_returns_dict(self, client: AsyncClient) -> None:
        r = await client.get("/api/status", headers=_auth())
        assert r.status_code == 200
        assert isinstance(r.json(), dict)
```

### B — xdist-safe exception assertion

```python
# tests/unit/engine/test_registry.py (abridged)
class TestServiceRegistry:
    def test_missing_service_raises(self, registry) -> None:
        with pytest.raises(Exception) as exc_info:
            registry.get("unknown")
        assert type(exc_info.value).__name__ == "ServiceNotRegisteredError"
        assert "unknown" in str(exc_info.value)
```

### C — property test with bounded strategy

```python
# tests/unit/test_brain_properties.py (pattern)
class TestSpreadingActivation:
    @given(
        depth=st.integers(min_value=1, max_value=5),
        decay=st.floats(min_value=0.1, max_value=0.9, allow_nan=False),
    )
    @settings(max_examples=25)
    def test_activation_decays_monotonically(self, depth: int, decay: float) -> None:
        levels = spread(seed_concept, depth=depth, decay=decay)
        for i in range(len(levels) - 1):
            assert levels[i] >= levels[i + 1]
```

---

## References

- **`CLAUDE.md`** → Quality Gates, Testing Patterns, Anti-Patterns (especially #2, #8, #9, #10, #11, #12).
- **`docs/development/contributing.md`** — commit workflow, quality gates, PR checklist.
- **`docs/development/ci-pipeline.md`** — how these tests are invoked in CI and what the historical 2026-04-13 deadlock fix changed.
- **`docs/development/anti-patterns.md`** — the twelve tracked anti-patterns with code examples.
- **`pyproject.toml`** — pytest, coverage, hypothesis config.
- **`tests/conftest.py`** — Hypothesis profiles, fixtures.

### Upstream specs

- **SOVYX-BKD-SPE-001-ENGINE-CORE** (`vps-brain-dump/.../specs/`) — `create_app(token=...)` factory contract enforced by anti-pattern #10.
- **SOVYX-BKD-ADR-007-EVENT-ARCHITECTURE** — event bus semantics that drive the dispatch tests.
- **SOVYX-BKD-IMPL-015-OBSERVABILITY** — logging/health-check patterns tested under `tests/unit/observability/`.
- **`vps-brain-dump/memory/nodes/aiosqlite-deadlock-protocol.md`** — origin node of xdist-safe patterns (anti-pattern #8 + #9).
- **`.github/workflows/ci.yml`** → `test` job — the exact commands CI runs.
