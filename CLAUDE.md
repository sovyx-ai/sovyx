# CLAUDE.md — Sovyx Development Guide

## What is Sovyx
Sovereign Minds Engine — persistent AI companion with real memory, cognitive loop, and brain graph. Python library + CLI daemon + React dashboard.

## Stack
- **Backend:** Python 3.11 / 3.12 (CI matrix), structlog, pydantic v2, pydantic-settings, FastAPI, aiosqlite, ONNX Runtime, httpx, argon2-cffi, PyJWT
- **Frontend:** React 19, TypeScript, Vite, Tailwind CSS, Zustand, TanStack Virtual, zod (runtime response validation), i18next
- **Build:** uv (Python, `uv.lock` committed), npm (dashboard), Hatch (packaging) with `hatchling` backend
- **CI:** GitHub Actions on self-hosted `sovyx-4core` → ruff + mypy + bandit + pytest (3.11 & 3.12) + vitest + tsc + Docker + PyPI
- **CLI:** `sovyx` entry point (`sovyx.cli.main:app`), plugin entry points under `sovyx.plugins` group

## Quality Gates (MANDATORY before any commit)

```bash
# Python (from repo root)
uv lock --check                               # lockfile must match pyproject.toml
uv run ruff check src/ tests/
uv run ruff format --check src/ tests/
uv run mypy src/                              # strict mode, 222 files
uv run bandit -r src/sovyx/ --configfile pyproject.toml
uv run python -m pytest tests/ --ignore=tests/smoke --timeout=30   # ~7 700 tests

# Dashboard (from dashboard/)
npx tsc -b tsconfig.app.json                  # zero new errors
npx vitest run                                # ~767 tests
```

If ANY gate fails, fix before committing. Never skip.

**Version bump gotcha:** any change to `pyproject.toml` `version` requires `uv lock` to regenerate `uv.lock` — CI enforces `uv lock --check`.

## Repo Layout

```
src/sovyx/
├── engine/              # Config, bootstrap, lifecycle, events, registry, RPC
│   └── _lock_dict.py    # LRULockDict — bounded asyncio.Lock dict (shared)
├── cognitive/           # Perceive → Attend → Think → Act → Reflect loop
│   ├── safety/          # Split from safety_patterns.py: pattern catalogs per language
│   │   ├── patterns_en.py, patterns_pt.py, patterns_es.py
│   │   ├── patterns_child_safe.py
│   │   └── _classifier_* (budget, cache, types)
│   └── reflect/         # Split from reflect.py: concept extraction + episode encoding
│       ├── phase.py     # Reflect phase orchestration
│       ├── _categories.py, _scoring.py, _prompts.py, _fallback.py, _models.py
├── brain/               # Concepts, episodes, relations, embedding, scoring, retrieval
│   ├── _model_downloader.py   # Extracted from embedding.py (ONNX model fetch + SHA256)
│   ├── _novelty.py      # Extracted from service.py (novelty scoring)
│   └── _centroid.py     # Extracted from service.py (category centroids)
├── bridge/              # Inbound/outbound messaging
│   └── channels/        # telegram.py, signal.py
├── persistence/         # SQLite pool manager (WAL, round-robin readers), migrations
├── observability/       # Logging (structlog), health checks, alerts, SLOs, tracing
├── llm/                 # Multi-provider router (Anthropic, OpenAI, Google, Ollama)
├── mind/                # Mind config, personality
├── context/             # Context assembly for LLM calls
├── cli/                 # Typer CLI: sovyx start/stop/init/logs/doctor
├── dashboard/           # FastAPI server
│   ├── server.py        # ~700 LOC — wires routers only; endpoints live in routes/
│   └── routes/          # 21 APIRouter modules (split from the old 2 134 LOC server.py)
│       ├── activity, brain, channels, chat, config, conversation_import,
│       ├── conversations, data, emotions, logs, onboarding, plugins,
│       ├── providers, safety, settings, setup, status, telemetry,
│       ├── voice, voice_test, websocket
│       └── _deps.py     # Shared verify_token dependency
├── tiers.py             # ServiceTier enum, feature/mind-limit maps (informational)
├── license.py           # LicenseValidator (Ed25519 public key JWT, offline)
├── voice/               # STT, TTS, VAD, wake word, Wyoming
│   └── pipeline/        # Split from pipeline.py: state machine + output queue + barge-in
│       ├── _orchestrator.py, _output_queue.py, _barge_in.py
│       ├── _state.py, _events.py, _config.py, _constants.py
├── plugins/             # Plugin loader, sandbox, SDK
│   ├── _event_emitter.py      # Extracted from manager.py (4 lifecycle event emitters)
│   ├── _manager_types.py      # Shared types for manager split
│   ├── _dependency.py         # Dependency resolution
│   ├── sandbox_http.py        # SandboxedHttpClient (all plugins MUST use this)
│   ├── sandbox_fs.py          # Filesystem sandbox
│   └── official/              # First-party plugins (financial_math, weather, web_intelligence, knowledge)
├── upgrade/             # Doctor, importer, blue-green, backup manager
└── benchmarks/          # Budget baselines

dashboard/               # React SPA — part of the main repo (NOT a submodule)
├── src/App.tsx, main.tsx, router.tsx
├── src/pages/           # Route pages (logs, brain, conversations, plugins, settings, …)
├── src/stores/          # Zustand store (dashboard.ts + slices/: activity, auth, brain,
│                        # chat, connection, conversations, logs, onboarding, plugins,
│                        # settings, stats, status)
├── src/components/      # dashboard/, ui/, auth/, chat/, settings/, layout/, common
├── src/hooks/           # use-auth, use-websocket, use-mobile, use-onboarding
├── src/types/           # api.ts (compile-time types) + schemas.ts (zod runtime)
└── src/lib/             # api.ts (apiFetch + api.{get,post,put,patch,delete} + buildQuery),
                         # safe-json.ts (clamp + secret redaction), format.ts, i18n.ts, utils.ts

tests/
├── unit/                # Fast, isolated (split by module: brain/, cognitive/, engine/, …)
├── integration/         # Cross-component
├── dashboard/           # Backend API + adversarial tests (use create_app)
├── plugins/             # Plugin + sandbox tests
├── property/            # Hypothesis property-based tests
├── security/            # Security-specific tests
├── stress/              # Load/performance tests
└── smoke/               # Excluded from CI via --ignore=tests/smoke

docs/                    # Public docs — MkDocs source
├── getting-started.md, architecture.md, api-reference.md,
├── configuration.md, contributing.md, faq.md, security.md,
├── llm-router.md, roadmap.md
├── modules/             # Per-module public docs
└── _meta/               # Tooling output (gitignored)

docs-internal/           # Internal planning, audits, specs — gitignored
```

## Conventions

### Python
- **Logging:** Always `from sovyx.observability.logging import get_logger` then `logger = get_logger(__name__)`. Never `print()` or `logging.getLogger()` directly.
- **Config:** All config via `EngineConfig` (pydantic-settings). Env vars: `SOVYX_*` prefix, `__` for nesting (e.g., `SOVYX_LOG__LEVEL=DEBUG`). Tuning knobs live under `EngineConfig.tuning.{safety,brain,voice}` — overridable via `SOVYX_TUNING__VOICE__AUTO_SELECT_MIN_GPU_VRAM_MB=...`.
- **Errors:** Custom exceptions in `engine/errors.py`. Always include `context` dict.
- **Type hints:** All functions fully typed. `from __future__ import annotations` in every file.
- **Imports:** `TYPE_CHECKING` block for type-only imports. Ruff enforces `TCH` rules.
- **Async:** All database/IO operations are async. Sync CPU-bound work (ONNX, boto3) MUST be wrapped in `asyncio.to_thread()`. Tests use `pytest-asyncio` with `mode=auto`.
- **Docstrings:** Every public class/function. First line = imperative summary.

### Dashboard (TypeScript)
- **Types:** Compile-time in `src/types/api.ts`; runtime zod schemas in `src/types/schemas.ts`. Pass `{ schema }` to `api.get/post/put/patch/delete` to validate the response (safeParse — logs mismatch, returns payload).
- **State:** Zustand store in `src/stores/dashboard.ts` with slices pattern.
- **API calls:** ALWAYS via `src/lib/api.ts` — `api.*` for JSON, `apiFetch(path, init, overrideToken?)` for raw `Response` (binary/FormData). Defaults: 30 s timeout, retry w/ exp backoff on 429/503/5xx for idempotent verbs.
- **Auth token:** `sessionStorage` + in-memory fallback. NEVER `localStorage`.
- **Hot-path memoization:** `React.memo` on rows in virtualized lists (log-row, chat-bubble, plugin-card, timeline-row, tool-item). `useMemo`/`useCallback` for derived values + stable props.
- **i18n:** All user-visible strings via `useTranslation()`.
- **Tests:** Colocated `*.test.tsx` next to each page/component.

### Git
- **Commits:** Conventional commits (`feat:`, `fix:`, `refactor:`, `test:`, `chore:`, `perf:`, `docs:`).
- **Tags:** `vX.Y.Z` triggers `publish.yml` — runs full CI gate, then PyPI (OIDC trusted publishing) + Docker + GitHub Release. Tag version must match `pyproject.toml` version or publish fails.
- **Dashboard:** part of the main repo; stage dashboard changes alongside backend changes in the same commit when they're related.
- **Branch:** Always `main`. No feature branches (fast iteration, CI validates).

## Anti-Patterns (bugs that already happened)

1. **Circular imports in `observability/__init__.py`:** Uses `__getattr__` lazy loading. Never add eager imports there.
2. **`sys.modules` stubs in tests:** Never inject fake modules into `sys.modules` for a module that's already imported — the `import X as Y` alias captures the real module at import time. Patch the aliased attribute directly (`patch.object(real_module, "attr", mock)`) or use `sys.modules` only for truly first-time imports inside the function under test.
3. **`LoggingConfig.console_format` (not `format`):** The field was renamed in v0.5.24. Legacy YAML with `format:` is auto-migrated. File handler ALWAYS writes JSON.
4. **`log_file` is resolved by `EngineConfig` model_validator:** `LoggingConfig.log_file` defaults to `None`. `EngineConfig` resolves it to `data_dir/logs/sovyx.log`. Never hardcode log paths.
5. **Dashboard `EngineConfig` from registry:** Dashboard resolves config from `ServiceRegistry`, not by instantiating a new `EngineConfig()`.
6. **httpx logs:** Suppressed to WARNING in `setup_logging()`. If you see raw HTTP lines in console, `setup_logging()` wasn't called.
7. **Dashboard frontend:** `LogEntry` has 4 required fields: `timestamp`, `level`, `logger`, `event`. Backend normalizes (`ts→timestamp`, `severity→level`, `message→event`, `module→logger`).
8. **xdist class identity:** pytest-xdist can reimport modules, creating duplicate classes. Never use `pytest.raises(InternalClass)` directly — use `pytest.raises(Exception)` + assert on class name. Never use `isinstance()` for exception dispatch in production code — use `type(exc).__name__`.
9. **Enums are StrEnum:** All enums with string values MUST inherit from `StrEnum`, never plain `Enum`. Guarantees value-based comparison, immune to xdist namespace duplication.
10. **Auth in tests:** Use `create_app(token="...")` for tests. Never monkeypatch `_ensure_token` or set `_server_token` global. The `token` parameter bypasses all filesystem and global state.
11. **patch string path:** Never use `patch("sovyx.module.function")` when you can `patch.object(imported_module, "function")`. String paths can resolve to different module objects under xdist or after refactors; attribute patches are stable.
12. **Defense-in-depth in tests:** If a fix works, remove the workaround. If you need 3 layers to make a test pass, you don't understand which one works. One layer, understood, is better than three layers, mysterious.
13. **Plugin imports via SandboxedHttpClient, not raw httpx:** Every official plugin in `plugins/official/` MUST instantiate `SandboxedHttpClient` and call `.get()`/`.post()` on it. Raw `httpx.AsyncClient(...)` from plugin code bypasses allowed-domains + rate-limit + size-cap enforcement and turns the sandbox into theater.
14. **Sync CPU-bound in `async def` blocks the event loop:** ONNX inference (Piper, Kokoro, Silero, Moonshine, OpenWakeWord), `boto3` calls, and any other blocking CPU/IO MUST be wrapped in `asyncio.to_thread(fn, *args)`. A naked `self._sess.run(...)` inside an async handler stalls every other coroutine (voice pipeline, bridge, dashboard WS) for the inference duration.
15. **Unbounded `defaultdict(asyncio.Lock)` leaks memory:** One-lock-per-key patterns must use `sovyx.engine._lock_dict.LRULockDict(maxsize=N)` so keys that stop appearing get evicted. Raw `defaultdict(asyncio.Lock)` grows forever over a long-lived daemon.
16. **God files (>500 LOC with mixed responsibilities):** Don't let a single module accumulate orchestration + helpers + types + models. Once it's hard to navigate, split into a subpackage (see `cognitive/safety/`, `cognitive/reflect/`, `voice/pipeline/`, `dashboard/routes/` as references — each sub-file owns one responsibility, `__init__.py` re-exports the public surface for back-compat).
17. **Hardcoded tuning constants:** Thresholds, timeouts, URLs, SHAs, etc. go in `EngineConfig.tuning.{safety,brain,voice}` (pydantic-settings). Module-level `_CONST = _TuningCls().field` pattern keeps import-time access while allowing `SOVYX_TUNING__*` env overrides. Never hardcode in a `.py` constant.
18. **Raw `fetch()` in the frontend:** Every network call MUST go through `src/lib/api.ts`. `api.*` wraps JSON + auth + retry + timeout + schema validation; `apiFetch` wraps raw-Response cases. A loose `fetch("/api/…")` drifts from the auth header injection and 401 handler.
19. **`localStorage` for auth tokens:** XSS-exposed. Use `sessionStorage` (tab-scoped) + in-memory fallback, which is what `src/lib/api.ts` already does. A token migrator reads any legacy `localStorage` entries into `sessionStorage` on boot.
20. **Test patches must follow module splits:** When you extract a helper (`_model_downloader`, `_event_emitter`, `_output_queue`, etc.), every `patch("old.module.X")` in the test suite becomes a silent no-op. The test still appears to mock X, but the real implementation runs. Grep for the old path and migrate patches to the new one in the same commit as the split.

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

# Mocking SandboxedHttpClient-based plugins
# SandboxedHttpClient internally calls ._client.request(METHOD, url, ...) — NOT .get().
# Tests that patch httpx.AsyncClient MUST mock .request, not .get, and wire
# MockClient.return_value to the configured mock (NOT the async-with __aenter__ path).
with patch("httpx.AsyncClient") as MockClient:
    mock_client = AsyncMock()
    mock_client.request = AsyncMock(return_value=mock_resp)
    mock_client.aclose = AsyncMock()
    MockClient.return_value = mock_client
    result = await my_plugin_func()

# Patching a module-level aliased import (e.g. `import onnxruntime as ort`)
# sys.modules patches DON'T work — the alias captures the real module at
# import time. Patch the real module's attribute directly:
import onnxruntime
with patch.object(onnxruntime, "InferenceSession", return_value=mock_sess):
    ...

# Patch targets after a module split: `from sovyx.brain.embedding import ModelDownloader`
# was moved to sovyx.brain._model_downloader. Tests must patch the NEW path:
with patch("sovyx.brain._model_downloader.httpx.AsyncClient", ...):
    ...
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
10. **Windows mypy noise:** local `uv run mypy src/` on Windows reports platform-specific `AF_UNIX` / `os.sysconf` / `getrusage` / `open_unix_server` errors. Those 9 are false positives on Windows; only count errors OUTSIDE that list as real regressions. CI runs Linux — the true baseline.

## Deploy Flow

1. Bump `version` in `pyproject.toml` (single source of truth — `src/sovyx/__init__.py` reads it via `importlib.metadata.version`).
2. `uv lock` to refresh `uv.lock` (CI enforces `uv lock --check`).
3. `git commit` + `git tag vX.Y.Z` + `git push origin main` + `git push origin vX.Y.Z`.
4. Tag push triggers `publish.yml`:
   - **CI gate** — full ci.yml (lint + typecheck + security + dashboard + Python 3.11 & 3.12 tests) must pass.
   - **Build** — dashboard `npm run build` bakes static assets into `src/sovyx/dashboard/static/`; `uv build` produces sdist + wheel. Publish fails if tag version ≠ pyproject.toml version.
   - **Publish to PyPI** — OIDC trusted publishing, no API token.
   - **GitHub Release** — auto-generated release notes + artifacts.
   - **Docker** — `docker.yml` builds + pushes image in parallel.
5. If CI fails on a tagged commit, fix + commit + re-tag with `git tag -d vX.Y.Z && git tag vX.Y.Z && git push origin vX.Y.Z --force`.

## Working Style

When given a task:
1. **Understand the scope** — read relevant source files, understand dependencies.
2. **Check for existing patterns** — look at similar code in the repo for conventions.
3. **Implement** — write code following conventions above.
4. **Write tests** — ≥95 % coverage on modified files, include edge cases.
5. **Run ALL quality gates** — ruff (+ format), mypy (strict), bandit, pytest, vitest, tsc.
6. **Commit with conventional message** — descriptive body explaining WHY.

When modifying tests:
1. **Never introduce workarounds** — if a test needs patching to pass, the production code might need a better interface (e.g., `create_app(token=...)` instead of monkeypatching globals).
2. **Prefer explicit parameters over mocking** — dependency injection beats monkeypatch.
3. **One assertion pattern** — use the xdist-safe patterns documented above consistently.
4. **Remove dead code** — if a fix makes a workaround unnecessary, delete the workaround in the same commit.

When splitting a god file:
1. **Public surface stays stable** — `__init__.py` re-exports everything so callers don't break.
2. **One responsibility per sub-file** — underscore-prefixed modules (`_event_emitter.py`, `_model_downloader.py`) signal "internal, accessed via parent package".
3. **Migrate tests in the same commit** — any `patch("old.module.X")` target becomes a silent no-op once the split lands.
4. **Preserve the public docstring** — move it to the parent module's `__init__.py` if the original class was the face of the module.

## Deep Reference
- Public docs (MkDocs): `docs/` — architecture, getting-started, configuration, api-reference, security, per-module specs under `docs/modules/`.
- Internal planning + audits: `docs-internal/` (gitignored, local only).
- Backend specs (IMPL/SPE/ADR): live under `docs-internal/`, searchable by number.
- Code patterns: look at existing tests for real examples — `tests/unit/` mirrors `src/sovyx/`.
- Frontend types: `dashboard/src/types/api.ts` (compile-time) + `dashboard/src/types/schemas.ts` (runtime).
