# Anti-Patterns

**Audience**: contributors, maintainers, code reviewers.
**Scope**: twelve tracked anti-patterns from `CLAUDE.md` with full context, right/wrong examples, and the bug each one originated from. Plus four general hygiene anti-patterns.
**Status**: canonical — this is the expanded companion to `CLAUDE.md` → "Anti-Patterns (bugs that already happened)". If you see a PR reintroducing any of these, revert and link here.

> **Why this file exists.** Every item below is a bug we already paid for. The fix was applied repo-wide. The goal is never to pay for it again.

---

## Quick index

| #   | Anti-pattern                                                    | Module                   |
| --: | --------------------------------------------------------------- | ------------------------ |
| 1   | Circular imports in `observability/__init__.py`                 | `observability`          |
| 2   | `sys.modules` stubs in tests                                    | tests                    |
| 3   | `LoggingConfig.console_format` vs `format`                      | `observability`          |
| 4   | Hardcoded `log_file` paths                                      | `observability`, config  |
| 5   | Dashboard instantiating `EngineConfig()` itself                 | `dashboard`              |
| 6   | Raw `httpx` logs in the console                                 | `observability`          |
| 7   | Dashboard `LogEntry` field names                                | dashboard FE/BE          |
| 8   | `pytest.raises(InternalClass)` — xdist class identity           | tests                    |
| 9   | Plain `Enum` with string values                                 | all modules              |
| 10  | Monkeypatching `_ensure_token` / `_server_token`                | dashboard tests          |
| 11  | `patch("dotted.path")` string patching                          | tests                    |
| 12  | Defence-in-depth in tests                                       | tests                    |
| —   | `print()` in production code                                    | all modules              |
| —   | `logging.getLogger()` instead of `get_logger()`                 | all modules              |
| —   | Hardcoded filesystem paths                                      | config                   |
| —   | Env vars without the `SOVYX_*` prefix / wrong nesting           | config                   |

---

## #1 — Circular imports in `observability/__init__.py`

**The bug.** An innocent-looking `from .logging import get_logger` in `observability/__init__.py` produced a circular import the moment any `observability` submodule imported the package for logging. Stack traces blamed `structlog`; the real culprit was the eager import.

**The fix.** `observability/__init__.py` uses a **lazy `__getattr__`** to materialise its public names on demand:

```python
# src/sovyx/observability/__init__.py
__all__ = ["get_logger", "setup_logging", "AlertManager", ...]

def __getattr__(name: str) -> Any:
    if name == "get_logger":
        from .logging import get_logger as _get_logger
        return _get_logger
    if name == "setup_logging":
        from .logging import setup_logging as _setup
        return _setup
    # ...
    raise AttributeError(name)
```

**Rule.** **Never add eager `from .X import Y` lines to `observability/__init__.py`.** If you need a new public re-export, extend `__all__` and `__getattr__`. The same principle applies to any package that is imported by its own submodules.

**Verification.** `uv run python -c "import sovyx.observability.logging"` must succeed on a cold interpreter.

---

## #2 — `sys.modules` stubs in tests

**The bug.** A test wanted to stub out `sovyx.llm.openai`, so it did:

```python
# ❌ Do NOT do this.
import sys
sys.modules["sovyx.llm.openai"] = FakeOpenAI()
```

The stub leaked into every subsequent test in the same worker. Any later test that genuinely imported `sovyx.llm.openai` got the fake. With xdist, the damage was worker-shaped: some workers had the fake, others didn't, producing non-deterministic failures.

**The fix.** Use `unittest.mock.patch.object` on an already-imported module, or accept an injection point in the production API.

```python
# ✅
from sovyx.llm import openai as openai_module

def test_fake_provider(monkeypatch):
    monkeypatch.setattr(openai_module, "complete", lambda *a, **k: "stubbed")
    ...
```

**Rule.** **Never inject fake modules into `sys.modules`.** If you need a module-wide substitute, the production code needs a factory or dependency-injection seam.

---

## #3 — `LoggingConfig.console_format` vs `format`

**The bug.** Old YAML configs used `format:` under `logging:`. In v0.5.24 the field was renamed to `console_format:` to distinguish it from the **file handler**, which always writes JSON regardless of configuration. Legacy configs broke on load with a pydantic `ExtraForbidden` error.

**The fix.** `EngineConfig` auto-migrates legacy YAML: if the loader sees `logging.format`, it remaps to `logging.console_format` with a `DeprecationWarning`. The new canonical name is **`console_format`**. File output is always JSON — there is no config for it.

**Right:**

```yaml
logging:
  level: INFO
  console_format: human     # or "json"
```

**Wrong:**

```yaml
logging:
  level: INFO
  format: human             # legacy, auto-migrated but deprecated
```

**Rule.** **Use `console_format` everywhere new.** Never add a config knob to change the file handler format — it is JSON by contract.

---

## #4 — Hardcoded `log_file` paths

**The bug.** Multiple modules resolved log paths locally — `Path.home() / ".sovyx" / "logs" / "sovyx.log"`, `/tmp/sovyx.log`, etc. When `SOVYX_DATA_DIR` was overridden, logs kept writing to the hardcoded path.

**The fix.** `LoggingConfig.log_file` defaults to `None`. The `EngineConfig` model validator resolves it to `data_dir / "logs" / "sovyx.log"`:

```python
# src/sovyx/engine/config.py (pattern)
@model_validator(mode="after")
def _resolve_log_file(self) -> EngineConfig:
    if self.log.log_file is None:
        self.log.log_file = self.data_dir / "logs" / "sovyx.log"
    return self
```

**Right:**

```python
from sovyx.engine.config import EngineConfig

config = EngineConfig()
log_path = config.log.log_file   # always populated after validator
```

**Wrong:**

```python
# ❌
log_path = Path.home() / ".sovyx" / "logs" / "sovyx.log"

# ❌
log_path = Path("/var/log/sovyx/sovyx.log")
```

**Rule.** **Never hardcode paths.** Always derive from `EngineConfig.data_dir`. If your module doesn't have access to `EngineConfig`, accept the path as a constructor argument — don't reach for `Path.home()` or environment variables directly.

---

## #5 — Dashboard instantiating `EngineConfig()` itself

**The bug.** The dashboard server did `config = EngineConfig()` on startup, producing a **second** config instance disconnected from the running engine. Env-var overrides applied to the daemon (e.g., `SOVYX_DATA_DIR`) were re-read at dashboard startup, but any programmatic config changes made after engine boot were invisible to the dashboard.

**The fix.** The dashboard resolves `EngineConfig` from the `ServiceRegistry`:

```python
# src/sovyx/dashboard/server.py (pattern)
from sovyx.engine.registry import ServiceRegistry

def create_app(api_config: APIConfig | None = None, *, token: str | None = None) -> FastAPI:
    registry = ServiceRegistry.current()
    engine_config = registry.get("engine_config")   # single source of truth
    ...
```

**Right:**

```python
registry = ServiceRegistry.current()
cfg = registry.get("engine_config")
```

**Wrong:**

```python
# ❌ creates an independent copy, drifts from the engine
cfg = EngineConfig()
```

**Rule.** **Never instantiate `EngineConfig()` outside the engine bootstrap.** Read it from the registry. Consumers get the same singleton the rest of the process is using.

---

## #6 — Raw `httpx` logs flooding the console

**The bug.** `httpx` logs every request at `INFO` by default (`HTTP Request: POST https://api.anthropic.com/... "200 OK"`). In production logs this drowned everything else, and in tests it leaked sensitive URLs into CI output.

**The fix.** `setup_logging()` explicitly suppresses `httpx` (and `httpcore`) to `WARNING`:

```python
# src/sovyx/observability/logging.py (pattern)
def setup_logging(config: LoggingConfig) -> None:
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    ...
```

**Diagnostic.** If you ever see raw `HTTP Request: ...` lines in your terminal or CI logs, `setup_logging()` was not called. The bootstrap order is wrong — fix it there, don't paper over it with a per-call filter.

**Rule.** **Never turn `httpx` back on** (level DEBUG/INFO) except in a dedicated diagnostic context manager that scopes the change.

---

## #7 — Dashboard `LogEntry` field names

**The bug.** The frontend expected one set of field names (`timestamp`, `level`, `logger`, `event`). Different backend sources produced different names (`ts`, `severity`, `module`, `message`). Log entries arrived half-rendered, with `[object Object]` in cells.

**The fix.** The backend **normalises at the boundary**. The `LogEntry` frontend type has exactly **four required fields**:

```typescript
// dashboard/src/types/api.ts
export interface LogEntry {
  timestamp: string;
  level: string;
  logger: string;
  event: string;
  // optional context fields follow...
}
```

Backend normalisation map:

| Source field | Normalised field |
| ------------ | ---------------- |
| `ts`         | `timestamp`      |
| `severity`   | `level`          |
| `message`    | `event`          |
| `module`     | `logger`         |

**Rule.** **Never change these four field names.** If you need another field on the frontend, add it as optional. If a new log source emits different names, add a normalisation entry on the backend — never fix it in the UI.

---

## #8 — `pytest.raises(InternalClass)` — xdist class identity

**The bug.** Under `pytest-xdist`, worker subprocesses can reimport a module, producing two distinct class objects named `ServiceNotRegisteredError` with different `id()`. A test that did:

```python
# ❌
with pytest.raises(ServiceNotRegisteredError):
    registry.get("missing")
```

could fail because the exception raised by the production code came from "the other" `ServiceNotRegisteredError` class — `except ServiceNotRegisteredError` does an identity check that returns `False` across namespace duplicates.

**The fix.** Assert on the **exception class name** and the **message**:

```python
# ✅
with pytest.raises(Exception) as exc_info:
    registry.get("missing")
assert type(exc_info.value).__name__ == "ServiceNotRegisteredError"
assert "missing" in str(exc_info.value)
```

Same principle in production dispatch code: **never use `isinstance()` on custom exceptions** for routing logic. Use `type(exc).__name__`.

**Rule.** `pytest.raises(Exception) + type-name assertion` is the only xdist-safe pattern. Any PR introducing `pytest.raises(SovyxError)` or `isinstance(exc, SovyxError)` for dispatch is reverted on sight. See `testing.md` → xdist.

---

## #9 — Plain `Enum` with string values

**The bug.** Enums defined as:

```python
# ❌
from enum import Enum

class ComplexityLevel(Enum):
    SIMPLE = "simple"
    MODERATE = "moderate"
    COMPLEX = "complex"
```

do **identity-based equality**. `ComplexityLevel.SIMPLE == "simple"` returns `False`. Under xdist reimport, `ComplexityLevel.SIMPLE == ComplexityLevel.SIMPLE` could also fail if the two sides came from different worker namespaces.

**The fix.** **All string-valued enums inherit from `StrEnum`:**

```python
# ✅
from enum import StrEnum

class ComplexityLevel(StrEnum):
    SIMPLE = "simple"
    MODERATE = "moderate"
    COMPLEX = "complex"

assert ComplexityLevel.SIMPLE == "simple"          # True
assert str(ComplexityLevel.SIMPLE) == "simple"     # True
```

`StrEnum` gives value-based equality, `__str__` returns the value, and JSON serialisation works without extra hooks. Immune to xdist namespace duplication.

**Rule.** **Never `class Foo(Enum)` with string members.** Always `StrEnum`. If you must hold non-string values (rare: integer flags, bitfields), `IntEnum`/`IntFlag` with a clear comment.

---

## #10 — Monkeypatching `_ensure_token` / `_server_token`

**The bug.** Older dashboard tests did:

```python
# ❌
monkeypatch.setattr("sovyx.dashboard.server._server_token", "test")
monkeypatch.setattr("sovyx.dashboard.server._ensure_token", lambda: "test")
```

Two problems:

1. `_server_token` is a module-level global. Under xdist, workers each had their own copy — some saw "test", others saw a real token read from disk.
2. The string-path `setattr` resolved to a different module object depending on import order (see #11).

**The fix.** `create_app` accepts an explicit `token=` parameter that **bypasses all filesystem and global-state lookups**:

```python
# ✅
from sovyx.dashboard.server import create_app

_TOKEN = "test-token-fixo"

@pytest.fixture()
def client() -> TestClient:
    app = create_app(token=_TOKEN)
    return TestClient(app, headers={"Authorization": f"Bearer {_TOKEN}"})
```

**Rule.** **Never monkeypatch auth internals.** Always use `create_app(token=...)`. If you need a new test seam, add a parameter to `create_app` — don't patch a private.

---

## #11 — `patch("dotted.path")` string patching

**The bug.** String-based patch paths like:

```python
# ❌
with patch("sovyx.plugins.hot_reload.Watcher._on_file_changed") as m:
    ...
```

resolve the target **lazily**, at patch-enter time. Under xdist, multiple import paths can yield different module objects with the same dotted name — the patch applies to one, the production code calls through the other, the mock never records the call.

**The fix.** Import the module once, then `patch.object` on the object you already have:

```python
# ✅ (from tests/plugins/test_hot_reload.py)
from sovyx.plugins.hot_reload import watcher as watcher_module

with patch.object(watcher_module, "_on_file_changed") as m:
    ...
```

Or, when patching an instance method:

```python
# ✅
with patch.object(watcher_instance, "_resolve_plugin_name", return_value="calc"):
    ...
```

**Rule.** **Never `patch("dotted.path")`.** Always `patch.object(obj, "name")`. String paths resolve to different module objects under xdist — `patch.object` binds at the call site.

---

## #12 — Defence-in-depth in tests

**The bug.** Someone added a mock. It didn't fix the test. They added a monkeypatch. Still failing. They added a `sys.modules` stub. Test passed. All three layers stayed — nobody knew which actually worked.

**The fix — and the principle:**

> If a fix works, **remove the workaround**. If you need three layers to make a test pass, you don't understand which one is doing the work.
>
> **One layer, understood, is better than three layers, mysterious.**

Process:

1. When a test passes after a change, revert the other changes one by one.
2. Confirm the test still passes with only the minimal change.
3. Delete every layer that wasn't load-bearing.
4. If the test regresses when you remove a layer, figure out why **before** restoring it.

**Rule.** **Defence-in-depth in tests is a smell.** Exactly one seam per test. If that seam requires production-code change to be clean, make the production change.

---

## General hygiene anti-patterns

Beyond the twelve enumerated bugs, these four are rejected on sight in code review.

### `print()` in production code

**Right:**

```python
from sovyx.observability.logging import get_logger

logger = get_logger(__name__)
logger.info("user_message_received", user_id=uid, length=len(text))
```

**Wrong:**

```python
# ❌
print(f"got message from {uid}: {text[:40]}")
```

**Why.** `print()` bypasses log levels, structured fields, the JSON file handler, and every observability integration (SLO burn rate, alert routing). It also leaks into stdout where nobody will look when the daemon is running under systemd. The one exception is `src/sovyx/__main__.py` printing the version string — that's a CLI UX affordance, not logging.

### Plain `logging.getLogger()` instead of `get_logger()`

**Right:**

```python
from sovyx.observability.logging import get_logger
logger = get_logger(__name__)
```

**Wrong:**

```python
# ❌
import logging
logger = logging.getLogger(__name__)
```

**Why.** `get_logger` returns a `structlog` bound logger configured with the project's processor chain (context vars, OTel trace IDs, JSON output on the file handler, human output on the console). Plain `logging.getLogger()` returns a bare stdlib logger that emits through the root handler only — it drops all structured context.

### Hardcoded filesystem paths

**Right:**

```python
# Accept config or registry lookup.
def __init__(self, config: EngineConfig) -> None:
    self._data_dir = config.data_dir
    self._db_path = config.data_dir / "brain" / "graph.sqlite"
```

**Wrong:**

```python
# ❌
from pathlib import Path
self._data_dir = Path.home() / ".sovyx"
self._db_path = Path("/var/sovyx/graph.sqlite")
```

**Why.** `EngineConfig.data_dir` respects `SOVYX_DATA_DIR`, test fixtures' `tmp_path`, and platform differences (Windows `%APPDATA%`, macOS `~/Library/Application Support`, Linux `~/.local/share`). Hardcoded paths break tests, containers, and every user whose home isn't `/root`.

### Env vars without the `SOVYX_*` prefix or wrong nesting

**Right:**

```bash
SOVYX_LOG__LEVEL=DEBUG                                # LoggingConfig.level
SOVYX_LLM__ANTHROPIC__API_KEY=sk-ant-...              # nested
SOVYX_DATA_DIR=/srv/sovyx/data                        # top-level
```

**Wrong:**

```bash
# ❌ no prefix — won't be read by pydantic-settings
LOG_LEVEL=DEBUG

# ❌ single underscore for nesting — reads as a top-level field
SOVYX_LOG_LEVEL=DEBUG

# ❌ lowercase — env vars are case-insensitive on Windows but
#    we standardise on UPPER_SNAKE for portability
sovyx_log__level=DEBUG
```

**Why.** `EngineConfig` is a `pydantic_settings.BaseSettings` with `env_prefix="SOVYX_"` and `env_nested_delimiter="__"`. The prefix keeps the Sovyx namespace clean; the `__` delimiter means `SOVYX_LOG__LEVEL` lands in `config.log.level`. Single-underscore nesting silently fails — pydantic treats `SOVYX_LOG_LEVEL` as a top-level field that doesn't exist and warns but doesn't error. Always use double underscores for nesting.

---

## How to use this document

- **Reviewer.** When you spot any of these in a PR, link the section here in your review comment. Don't re-explain.
- **Author.** Run `uv run pytest tests/ --timeout=20` locally. If it passes, re-check the `testing.md` patterns and this file before opening the PR. Most rejections are one of the 16 items above.
- **New contributor.** Read this whole file once. Every entry represents time already lost by someone else. You are not expected to rediscover these tradeoffs.

---

## References

- **`CLAUDE.md`** → "Anti-Patterns (bugs that already happened)" — the canonical 12-item list.
- **`docs/development/contributing.md`** — setup, workflow, commit conventions, PR checklist.
- **`docs/development/testing.md`** — test layout, the patterns referenced by #2, #8, #9, #10, #11, #12.
- **`docs/development/ci-pipeline.md`** — how the quality gates surface these issues in CI.
- **`vps-brain-dump/memory/nodes/int-001.md`** through **`int-005.md`** — original investigation notes for the observability and logging fixes (#1, #3, #4, #6, #7).
- **`vps-brain-dump/memory/obsidian-stack-decisions.md`** — stack-level decisions that drove #9 (StrEnum) and #10 (token injection).
- **`pyproject.toml`** — ruff/mypy/bandit/pytest configuration that enforces several of these rules.
- **`src/sovyx/observability/__init__.py`**, **`src/sovyx/engine/config.py`**, **`src/sovyx/dashboard/server.py`** — reference implementations for #1, #4, #10.
