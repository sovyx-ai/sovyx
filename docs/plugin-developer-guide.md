# Plugin Developer Guide

This is the practical recipe for writing a Sovyx plugin. For the
architectural overview (sandbox layers, permission model, security
scanner internals, official plugin catalogue) see
[modules/plugins.md](modules/plugins.md) — this guide focuses on
**how to write one**.

> **Upgrading from v0.40.x?** Jump to
> [Upgrade — v0.41.0 BrainAccess `mind_id` keyword-only](#upgrade--v0410-brainaccess-mind_id-keyword-only)
> first — that release tightened the BrainAccess constructor to
> require an explicit `mind_id` keyword argument. Any custom
> `PluginContext` factory or test fixture that builds a
> `BrainAccess` directly needs a one-line update.

---

## TL;DR — a 12-line plugin

```python
from sovyx.plugins.sdk import ISovyxPlugin, tool


class HelloPlugin(ISovyxPlugin):
    @property
    def name(self) -> str:        return "hello"
    @property
    def version(self) -> str:     return "1.0.0"
    @property
    def description(self) -> str: return "Say hi."

    @tool(description="Greet a person by name.")
    async def greet(self, name: str) -> str:
        return f"Hello, {name}!"
```

Pair it with a `plugin.yaml` (manifest, see
[modules/plugins.md §Manifest](modules/plugins.md#manifest)) and run
`sovyx plugin install ./hello` to install. The Mind then sees
`hello.greet` as a callable tool whenever its LLM provider supports
function calling.

---

## Project layout

```
my-plugin/
├── plugin.yaml           # manifest (see plugins.md §Manifest)
├── __init__.py           # exports a class implementing ISovyxPlugin
├── plugin.py             # optional — main implementation if you prefer not to inline in __init__.py
└── tests/
    └── test_my_plugin.py
```

`plugin.yaml` is **required**; the rest is convention. The manager
imports the package by name and looks for a single
`ISovyxPlugin` subclass at the module top level.

---

## The `@tool` decorator

`@tool` reads your method's type hints and builds a JSON Schema for
the `parameters` field that the LLM sees. The tool name is the method
name; the qualified name registered with the manager is
`<plugin.name>.<method_name>`.

```python
from typing import Literal

from sovyx.plugins.sdk import tool


@tool(
    description="Convert a price between two currencies at today's rate.",
    timeout_seconds=10,            # default 30, hard kill if exceeded
    requires_confirmation=False,    # set True for state-mutating tools
)
async def convert(
    self,
    amount: float,
    from_currency: Literal["USD", "EUR", "BRL"],
    to_currency: Literal["USD", "EUR", "BRL"],
) -> dict[str, float]:
    """LLM-visible docstring is appended to `description`."""
    ...
```

Type hint conventions the schema generator recognises:

| Python type                  | JSON Schema                          |
|------------------------------|--------------------------------------|
| `int`, `float`, `bool`, `str`| `integer` / `number` / `boolean` / `string` |
| `Literal["a", "b"]`          | `enum`                                |
| `list[T]`                    | `array` with `items: T`               |
| `dict[str, T]`               | `object`                              |
| `T | None` or `Optional[T]`  | makes the parameter optional (default required) |

Return type is informational only — the LLM works with whatever you
return as long as it's JSON-serialisable.

---

## The `PluginContext` (what your plugin can touch)

During `setup(context)`, the manager hands you a `PluginContext`
populated **only** with the access objects matching your declared
permissions. Anything else is `None`.

```python
from sovyx.plugins.context import PluginContext
from sovyx.plugins.sdk import ISovyxPlugin


class MyPlugin(ISovyxPlugin):
    async def setup(self, context: PluginContext) -> None:
        self._brain = context.brain         # None unless brain:read/write declared
        self._events = context.event_bus    # None unless event:* declared
        self._http = context.http           # None unless network:* declared
        self._fs = context.filesystem      # None unless fs:* declared

        # Always available regardless of permissions:
        self._plugin_name = context.plugin_name
        self._version = context.plugin_version
        self._data_dir = context.data_dir
        self._config = context.config       # dict from plugin.yaml `config:` section
        self._log = context.logger          # standard logger; auto-tagged plugin=<name>
```

If your plugin is permission-gated for `brain:read` only, `context.brain`
is a `BrainAccess` that raises `PermissionDeniedError` on every
write method. Don't introspect `is None` — call the methods you
declared and let the enforcer fail loudly if the manifest is
out-of-sync with the code.

---

## BrainAccess — memory access for plugins

`BrainAccess` is the brain-scoped facade. It always operates **within
the Mind that loaded the plugin** — there is no cross-mind access.

```python
# Read operations (require brain:read)
results = await self._brain.search("query", limit=5)
similar = await self._brain.find_similar("content", threshold=0.9)
related = await self._brain.get_related(concept_id, limit=10)
episodes = await self._brain.search_episodes("query")
top = await self._brain.get_top_concepts(limit=10, category="fact")
stats = await self._brain.get_stats()

# Write operations (require brain:write)
cid = await self._brain.learn("name", "content", category="fact")
ok = await self._brain.forget(cid)
ok = await self._brain.update(cid, content="...", importance=0.8)
rid = await self._brain.create_relation(src, tgt, "related_to")
result = await self._brain.reinforce(cid, importance_delta=0.05)
ok = await self._brain.boost_importance(cid, delta=0.05)

# Analysis (brain:read only)
verdict = await self._brain.classify_content(old, new)  # SAME / EXTENDS / CONTRADICTS / UNRELATED
```

Hard limits enforced by `BrainAccess`:

| Limit                 | Value          | Where                       |
|-----------------------|----------------|-----------------------------|
| Max search results    | 50 per call    | every `*search*` / `*top*` method |
| Max concept content   | 10 KB           | `learn`, `update`            |
| Cosine similarity threshold for `find_similar` | 0.9 default | adjustable per call |
| `forget_all` safety cap | 20 concepts | per call                      |

All plugin-written concepts are tagged `source="plugin:<name>"` —
the dashboard surfaces this on the concept-detail view so users can
see where a memory came from.

---

## Upgrade — v0.41.0 BrainAccess `mind_id` keyword-only

**Breaking change in v0.41.0.** `BrainAccess.__init__` now requires
`mind_id` as a keyword-only argument with no default.

```python
# Before v0.41.0 — implicitly bound to "default" mind via PluginContext factory:
access = BrainAccess(
    brain=brain_service,
    enforcer=enforcer,
    write_allowed=True,
    plugin_name="my-plugin",
)

# v0.41.0+ — explicit mind_id required:
access = BrainAccess(
    brain=brain_service,
    enforcer=enforcer,
    write_allowed=True,
    plugin_name="my-plugin",
    mind_id=MindId("jonny"),     # <-- required, keyword-only, no default
)
```

**Why:** prior versions implicitly bound BrainAccess to the
`"default"` mind sentinel at construction time. When operators
configured a non-default mind, plugin writes still landed in
`default` — a privacy and correctness violation. v0.41.0 closes this
by making the binding explicit. See
[CLAUDE.md anti-pattern #35](../CLAUDE.md#anti-patterns) for the
broader sentinel-default class this addresses.

**Who is affected:**
- Plugins that **don't construct `BrainAccess` directly** (the
  overwhelming majority — they receive it pre-built via
  `PluginContext.brain`): no change required.
- Custom `PluginContext` factories or test fixtures that instantiate
  `BrainAccess` directly: pass `mind_id=...` keyword-only as shown
  above.
- Third-party plugin authors who built test harnesses against pre-
  v0.41.0 internals: same one-line addition.

**Future direction:** multi-mind plugin invocation context (per-call
mind, instead of per-load mind) is tracked in
`docs-internal/missions/MISSION-plugin-context-multi-mind-FUTURE.md`
and activates when multi-mind daemon support lands. Until then,
load-time `mind_id` binding is the correct contract.

---

## Other access objects

### `EventBusAccess` (`event:subscribe` / `event:emit`)

```python
from sovyx.engine.events import ConceptLearned

async def setup(self, context: PluginContext) -> None:
    self._events = context.event_bus
    if self._events is not None:
        self._events.subscribe(ConceptLearned, self._on_concept_learned)

async def _on_concept_learned(self, event: ConceptLearned) -> None:
    self._log.info("seen concept", concept_id=event.concept_id)

async def teardown(self) -> None:
    if self._events is not None:
        self._events.cleanup()    # unsubscribe everything we added
```

Subscriptions are tracked and automatically removed by `cleanup()`.
Cross-plugin pub-sub uses `plugin.<other_plugin>.*` event namespaces.

### `SandboxedHttpClient` (`network:internet` / `network:local`)

```python
# In setup():
self._http = context.http

# Anywhere in a tool:
response = await self._http.get("https://api.open-meteo.com/v1/forecast", params={...})
response = await self._http.post("https://api.example.com/foo", json={"bar": 1})
response = await self._http.request("PROPFIND", url, headers={"Depth": "1"}, content=body)
```

Hard rules — see [modules/plugins.md §Sandbox](modules/plugins.md#sandbox)
for the architectural detail:

- Outbound host MUST match `network.allowed_domains` from
  `plugin.yaml`. Loopback / RFC1918 require `network:local` declared
  separately.
- DNS rebinding is checked — the resolved IP is validated against
  the same allow / block list as the hostname.
- Redirects are followed manually and re-validated on every hop.
- Default response size cap: 5 MB (configurable per call).
- Per-plugin rate limit applies across all hosts.

Always use `self._http`. Importing `httpx` directly inside the
plugin bypasses the sandbox and trips the static AST scanner at
install time (anti-pattern #13 in CLAUDE.md).

### `SandboxedFsAccess` (`fs:read` / `fs:write`)

```python
self._fs = context.filesystem

await self._fs.write("cache.json", json.dumps({...}))
data = await self._fs.read("cache.json")    # bytes
```

Paths are resolved relative to the plugin's `data_dir`. Symlinks
are resolved BEFORE the path check — you cannot escape via
`../../../etc/passwd`. Per-file cap: 50 MB. Per-plugin total: 500 MB.

---

## Testing your plugin

Build a stub `PluginContext` directly — no need to spin up the
full engine.

```python
import logging
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from sovyx.engine.types import MindId
from sovyx.plugins.context import BrainAccess, PluginContext
from sovyx.plugins.permissions import PermissionEnforcer

from my_plugin import MyPlugin


@pytest.fixture()
def context(tmp_path: Path) -> PluginContext:
    brain = AsyncMock()
    enforcer = PermissionEnforcer(declared={"brain:read", "brain:write"})

    return PluginContext(
        plugin_name="my-plugin",
        plugin_version="1.0.0",
        data_dir=tmp_path,
        config={},
        logger=logging.getLogger("my-plugin"),
        brain=BrainAccess(
            brain=brain,
            enforcer=enforcer,
            write_allowed=True,
            plugin_name="my-plugin",
            mind_id=MindId("test-mind"),       # v0.41.0+ requirement
        ),
    )


@pytest.mark.asyncio()
async def test_setup_subscribes(context: PluginContext) -> None:
    plugin = MyPlugin()
    await plugin.setup(context)
    # assertions
```

Patterns to follow (also documented in
[CLAUDE.md §Testing Patterns](../CLAUDE.md#testing-patterns)):

- Patch `SandboxedHttpClient` at the **`.request`** call site,
  not `.get` — the client funnels every verb through `.request`
  internally.
- Patch aliased imports via `patch.object(real_module, "attr", mock)`
  rather than the alias path (anti-pattern #2).
- Use `pytest.raises(Exception)` + `assert type(exc).__name__ == "X"`
  rather than `pytest.raises(SpecificException)` — xdist re-imports
  modules and breaks identity comparisons (anti-pattern #8).

---

## Manifest + permissions cheat-sheet

Full manifest schema is in [modules/plugins.md §Manifest](modules/plugins.md#manifest).
The minimal lifecycle hooks you'd implement on `ISovyxPlugin`:

```python
class MyPlugin(ISovyxPlugin):
    @property
    def name(self) -> str:        return "my-plugin"
    @property
    def version(self) -> str:     return "1.0.0"
    @property
    def description(self) -> str: return "What I do."

    async def setup(self, context: PluginContext) -> None:
        """Called once on load. Capture context, subscribe to events."""

    async def teardown(self) -> None:
        """Called on disable/reload. Free resources, unsubscribe."""

    async def health_check(self) -> dict[str, object]:
        """Optional. Return a dict; dashboard surfaces it on the plugin page."""
        return {"status": "ok"}
```

Five consecutive tool failures auto-disable the plugin. Once you
fix the cause, `sovyx plugin enable <name>` re-activates it.

---

## Reference

- Architecture, sandbox layers, security model, official plugin catalogue: [modules/plugins.md](modules/plugins.md)
- ISovyxPlugin ABC + `@tool` source: `src/sovyx/plugins/sdk.py`
- BrainAccess / EventBusAccess / PluginContext source: `src/sovyx/plugins/context.py`
- Permission enum + enforcer: `src/sovyx/plugins/permissions.py`
- Sandboxed HTTP client: `src/sovyx/plugins/sandbox_http.py`
- Sandboxed filesystem: `src/sovyx/plugins/sandbox_fs.py`
- Reference plugins (study targets): `src/sovyx/plugins/official/`
  — `financial_math` (pure, decimal precision), `home_assistant`
  (LAN sandbox with `requires_confirmation`), `caldav` (HTTP-extension
  + XML parsing), `knowledge` (brain:read + brain:write).
