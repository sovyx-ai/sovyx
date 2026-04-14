# CLAUDE.md — Sovyx Development Guide

## What is Sovyx
Sovereign Minds Engine — persistent AI companion with real memory, cognitive loop, and brain graph. Python library + CLI daemon + React dashboard.

## Stack
- **Backend:** Python 3.12, structlog, pydantic v2, pydantic-settings, FastAPI, aiosqlite, ONNX Runtime
- **Frontend:** React 19, TypeScript, Vite, Tailwind CSS, Zustand, TanStack Virtual, i18next
- **Build:** uv (Python), npm (dashboard), Hatch (packaging)
- **CI:** GitHub Actions → ruff + mypy + bandit + pytest + vitest + tsc + Docker + PyPI

## Quality Gates (MANDATORY before any commit)

```bash
# Python (from repo root)
uv run ruff check src/ tests/
uv run ruff format --check src/ tests/
uv run mypy src/                          # strict mode
uv run bandit -r src/sovyx/ --configfile pyproject.toml
uv run pytest tests/ --timeout=20         # 4900+ tests, coverage ≥95%

# Dashboard (from dashboard/)
npx tsc -b tsconfig.app.json             # zero errors
npx vitest run                            # 400+ tests
```

If ANY gate fails, fix before committing. Never skip.

## Repo Layout

```
src/sovyx/
├── engine/          # Config, bootstrap, lifecycle, events, registry, RPC
├── cognitive/       # Perceive → Attend → Think → Act → Reflect loop
├── brain/           # Concepts, episodes, relations, embedding, scoring, retrieval
├── bridge/          # Inbound/outbound messaging, channels (Telegram, Signal)
├── persistence/     # SQLite pool manager, migrations, schemas
├── observability/   # Logging (structlog), health checks, alerts, SLOs
├── llm/             # Multi-provider router (Anthropic, OpenAI, Google, Ollama)
├── mind/            # Mind config, personality
├── context/         # Context assembly for LLM calls
├── cli/             # Typer CLI (sovyx start/stop/init/logs)
├── dashboard/       # FastAPI server, API endpoints, WebSocket bridge
├── cloud/           # Billing, licensing, backup, scheduler
├── voice/           # TTS/STT, VAD, wake word, Wyoming protocol
├── upgrade/         # Doctor, importer, migrations
└── benchmarks/      # Budget baselines

dashboard/           # React SPA (git submodule, separate commits)
├── src/pages/       # Route pages (logs, brain, conversations, etc.)
├── src/stores/      # Zustand store + slices
├── src/components/  # UI components
├── src/hooks/       # useWebSocket, custom hooks
├── src/types/api.ts # TypeScript types mirroring backend schemas
└── src/lib/         # API client, formatters, utils

tests/
├── unit/            # Fast, isolated (3700+ tests)
├── integration/     # Cross-component (200+ tests)
├── dashboard/       # Backend API + adversarial tests
├── property/        # Hypothesis property-based tests
├── security/        # Security-specific tests
└── stress/          # Load/performance tests
```

## Conventions

### Python
- **Logging:** Always `from sovyx.observability.logging import get_logger` then `logger = get_logger(__name__)`. Never `print()` or `logging.getLogger()` directly.
- **Config:** All config via `EngineConfig` (pydantic-settings). Env vars: `SOVYX_*` prefix, `__` for nesting (e.g., `SOVYX_LOG__LEVEL=DEBUG`).
- **Errors:** Custom exceptions in `engine/errors.py`. Always include `context` dict.
- **Type hints:** All functions fully typed. `from __future__ import annotations` in every file.
- **Imports:** `TYPE_CHECKING` block for type-only imports. Ruff enforces `TCH` rules.
- **Async:** All database/IO operations are async. Tests use `pytest-asyncio` with `mode=auto`.
- **Docstrings:** Every public class/function. First line = imperative summary.

### Dashboard (TypeScript)
- **Types:** All API responses typed in `src/types/api.ts`. Must mirror backend exactly.
- **State:** Zustand store in `src/stores/dashboard.ts` with slices pattern.
- **API calls:** Via `src/lib/api.ts` (centralized, handles auth + errors).
- **i18n:** All user-visible strings via `useTranslation()`.
- **Tests:** Colocated `*.test.tsx` next to each page/component.

### Git
- **Commits:** Conventional commits (`feat:`, `fix:`, `refactor:`, `test:`, `chore:`).
- **Tags:** `vX.Y.Z` triggers PyPI + Docker publish.
- **Dashboard:** Commit dashboard changes first (it's a submodule), then `git add dashboard` in main repo.
- **Branch:** Always `main`. No feature branches (fast iteration, CI validates).

## Anti-Patterns (bugs that already happened)

1. **Circular imports in `observability/__init__.py`:** Uses `__getattr__` lazy loading. Never add eager imports there.
2. **`sys.modules` stubs in tests:** Never inject fake modules into `sys.modules` — poisons the full test suite.
3. **`LoggingConfig.console_format` (not `format`):** The field was renamed in v0.5.24. Legacy YAML with `format:` is auto-migrated. File handler ALWAYS writes JSON.
4. **`log_file` is resolved by `EngineConfig` model_validator:** `LoggingConfig.log_file` defaults to `None`. `EngineConfig` resolves it to `data_dir/logs/sovyx.log`. Never hardcode log paths.
5. **Dashboard `EngineConfig` from registry:** Dashboard resolves config from `ServiceRegistry`, not by instantiating a new `EngineConfig()`.
6. **httpx logs:** Suppressed to WARNING in `setup_logging()`. If you see raw HTTP lines in console, `setup_logging()` wasn't called.
7. **Dashboard frontend:** `LogEntry` has 4 required fields: `timestamp`, `level`, `logger`, `event`. Backend normalizes (`ts→timestamp`, `severity→level`, `message→event`, `module→logger`).
8. **xdist class identity:** pytest-xdist can reimport modules, creating duplicate classes. Never use `pytest.raises(InternalClass)` directly — use `pytest.raises(Exception)` + assert on class name. Never use `isinstance()` for exception dispatch in production code — use `type(exc).__name__`.
9. **Enums are StrEnum:** All enums with string values MUST inherit from `StrEnum`, never plain `Enum`. Guarantees value-based comparison, immune to xdist namespace duplication.
10. **Auth in tests:** Use `create_app(token="...")` for tests. Never monkeypatch `_ensure_token` or set `_server_token` global. The `token` parameter bypasses all filesystem and global state.
11. **patch string path:** Never use `patch("sovyx.module.function")` — use `patch.object(imported_module, "function")`. String paths resolve to different module objects under xdist.
12. **Defense-in-depth in tests:** If a fix works, remove the workaround. If you need 3 layers to make a test pass, you don't understand which one works. One layer, understood, is better than three layers, mysterious.

## Testing Patterns

```python
# Test class naming
class TestFeatureName:
    """Short description of what's being tested."""

    def test_specific_behavior(self, tmp_path: Path) -> None:
        """What should happen in this scenario."""
        ...

# Async tests (no decorator needed — asyncio_mode=auto)
class TestAsyncFeature:
    @pytest.mark.asyncio()
    async def test_async_behavior(self) -> None: ...

# File handler cleanup fixture
@pytest.fixture(autouse=True)
def _clean_handlers() -> Generator[None, None, None]:
    yield
    root = logging.getLogger()
    for h in list(root.handlers):
        if isinstance(h, logging.handlers.RotatingFileHandler):
            h.close()
    root.handlers.clear()

# Property-based tests with Hypothesis
from hypothesis import given, settings
from hypothesis import strategies as st

@given(level=st.sampled_from(["DEBUG", "INFO", "WARNING", "ERROR"]))
@settings(max_examples=20)
def test_any_valid_level(self, level: str) -> None: ...

# Auth in dashboard/API tests — use token parameter, never monkeypatch
_TOKEN = "test-token-fixo"

@pytest.fixture()
def app() -> FastAPI:
    return create_app(token=_TOKEN)

@pytest.fixture()
def client(app: FastAPI) -> TestClient:
    return TestClient(app, headers={"Authorization": f"Bearer {_TOKEN}"})

# Exception assertions — xdist-safe, never pytest.raises(InternalException)
with pytest.raises(Exception) as exc_info:
    do_something_that_raises()
assert type(exc_info.value).__name__ == "LLMError"
assert "expected message" in str(exc_info.value)
```

## Debugging Rules

When investigating bugs:
1. **Audit first** — before fixing anything, grep the full codebase for ALL instances of the same pattern. Map the size of the problem before solving any single instance.
2. **Group by root cause** — if 28 tests fail, find out how many distinct root causes exist. Fix causes, not symptoms.
3. **Don't band-aid** — understand the root cause. If you can't explain WHY a fix works, it's not ready.
4. **One commit per root cause** — all fixes for the same root cause go in one commit. No partial pushes to CI for incremental testing.
5. **No shotgun debugging** — if you're setting the same value in 3 places hoping one sticks, stop and trace the actual read path.
6. **Local suite before push** — run the full affected test suite locally before pushing to CI. Each CI round-trip wastes minutes and fragments reasoning.
7. **Check the full chain** — a config bug might affect CLI, dashboard, and API.
8. **Write regression tests** — the bug must never recur.
9. **If you're in the third fix→push→CI-fail cycle for the same problem, STOP** — the approach is wrong. Step back, reassess the strategy.

## Deploy Flow

1. Bump version in `pyproject.toml` + `src/sovyx/__init__.py`
2. `git commit` + `git tag vX.Y.Z` + `git push origin main --tags`
3. CI runs: Lint → Type Check → Security → Tests → Dashboard Build
4. On tag: Docker Build + PyPI Publish (automatic)

## Working Style

When given a task:
1. **Understand the scope** — read relevant source files, understand dependencies
2. **Check for existing patterns** — look at similar code in the repo for conventions
3. **Implement** — write code following conventions above
4. **Write tests** — ≥95% coverage on modified files, include edge cases
5. **Run ALL quality gates** — ruff, mypy, bandit, pytest, vitest, tsc
6. **Commit with conventional message** — descriptive body explaining WHY

When modifying tests:
1. **Never introduce workarounds** — if a test needs patching to pass, the production code might need a better interface (e.g., `create_app(token=...)` instead of monkeypatching globals)
2. **Prefer explicit parameters over mocking** — dependency injection beats monkeypatch
3. **One assertion pattern** — use the xdist-safe patterns documented above consistently
4. **Remove dead code** — if a fix makes a workaround unnecessary, delete the workaround in the same commit

## Deep Reference
- Architecture + decisions: `docs/SOVYX-BIBLE.md`
- Backend specs: search for SPE/IMPL files in the repo
- Code patterns: look at existing tests for real examples
- Frontend types: `dashboard/src/types/api.ts`