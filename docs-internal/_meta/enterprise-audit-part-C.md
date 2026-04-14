# Enterprise Audit — Part C (persistence, observability, plugins)

Scope: 36 files across three modules of `src/sovyx/` — `persistence/` (9), `observability/` (8), `plugins/` (19).

Criteria scored (0/1 each): Error Handling, Input Validation, Observability, Testing, Security, Concurrency, Configuration, Documentation, Resilience, Code Quality.
Classification: 8-10 ENTERPRISE, 5-7 DEVELOPED, 0-4 NOT-ENTERPRISE.

## Summary

| Module        | Files | Avg  | ENTERPRISE | DEVELOPED | NOT-ENT |
|---------------|------:|-----:|-----------:|----------:|--------:|
| persistence   | 9     | 9.2  | 8          | 1         | 0       |
| observability | 8     | 9.1  | 7          | 1         | 0       |
| plugins       | 19    | 8.4  | 15         | 4         | 0       |
| **Total**     | 36    | 8.7  | 30         | 6         | 0       |

Overall verdict: **ENTERPRISE-grade** backend. No NOT-ENTERPRISE files. Most gaps are minor (sandbox-bypass in official plugins, TOCTOU in DNS rebinding guard, god-class pressure on `plugins/manager.py`).

---

## persistence (9 files)

### File: src/sovyx/persistence/__init__.py — Score: 10/10 — ENTERPRISE
Thin package docstring, nothing to score negatively.

### File: src/sovyx/persistence/datetime_utils.py — Score: 10/10 — ENTERPRISE
Pure, typed, `@overload`, docstrings, UTC normalization. Tested in `test_datetime_utils.py`.

### File: src/sovyx/persistence/manager.py — Score: 10/10 — ENTERPRISE
DB-per-Mind isolation is enforced by storing pools keyed on `str(mind_id)` (`_brain_pools[str(mind_id)]`). Cross-mind leaks impossible absent a bug in the caller: every getter raises `DatabaseConnectionError` if pool missing. `stop()` closes all pools in correct order. Typed, documented.

### File: src/sovyx/persistence/migrations.py — Score: 10/10 — ENTERPRISE
Forward-only, checksummed, transactional, `_split_sql` handles BEGIN/END nesting. `MigrationError` context. 53-test test_migrations.py. Impressive.

### File: src/sovyx/persistence/pool.py — Score: 9/10 — ENTERPRISE
1W+N-readers WAL SQLite. Write serialization via `asyncio.Lock`. Clean shutdown order (readers → checkpoint → writer). Extension discovery with multi-path fallback.
Minor: `read()` is NOT lock-protected — round-robin over N connections with a shared `_read_index` counter can race under concurrent access (though SQLite WAL readers are fine concurrently). Acceptable for asyncio single-thread model.
- **#6 CONCURRENCY (partial nit)**: `self._read_index = (self._read_index + 1) % self._read_pool_size` is read-modify-write without a lock; in practice asyncio cooperative scheduling makes this safe between awaits, but a concurrent read-acquire without await could theoretically skew. Not a correctness hazard.

### File: src/sovyx/persistence/schemas/__init__.py — Score: 10/10 — ENTERPRISE
Package marker. N/A.

### File: src/sovyx/persistence/schemas/brain.py — Score: 10/10 — ENTERPRISE
4 migrations, checksums precomputed, conditional `has_sqlite_vec`, canonical relation ordering migration well-commented. Tested in `test_brain_schema.py`.

### File: src/sovyx/persistence/schemas/conversations.py — Score: 10/10 — ENTERPRISE
FTS5 + sync triggers. Clean.

### File: src/sovyx/persistence/schemas/system.py — Score: 10/10 — ENTERPRISE
engine_state, persons, channel_mappings, daily_stats. 2 migrations, checksums.

---

## observability (8 files)

### File: src/sovyx/observability/__init__.py — Score: 10/10 — ENTERPRISE
Lazy `__getattr__` import pattern correctly breaks the `engine.events → observability → alerts → engine.events` circular import (documented in CLAUDE.md anti-pattern #1). TYPE_CHECKING-only eager types, `_SUBMODULE_MAP` index for runtime resolution.

### File: src/sovyx/observability/logging.py — Score: 10/10 — ENTERPRISE
structlog + stdlib bridging, request contextvars (asyncio-safe), `SecretMasker` processor, `_setup_lock` threading.Lock for idempotency, rotating file handler, httpx/urllib3 noise suppression.

### File: src/sovyx/observability/metrics.py — Score: 10/10 — ENTERPRISE
OTel wrapper, explicit registry, `_NoOpRegistry` stub when disabled, `measure_latency` context manager, `collect_json` for API. Strong.

### File: src/sovyx/observability/prometheus.py — Score: 10/10 — ENTERPRISE
Pure converter, handles Sum/Histogram/Gauge, `+Inf` bucket handling, NaN/Inf formatting per spec.

### File: src/sovyx/observability/tracing.py — Score: 9/10 — ENTERPRISE
SovyxTracer wraps OTel, Sovyx-prefixed attributes, no-op-safe.
Minor: No retry on exporter failure (spans just lost on shutdown if exporter dies). But this is standard OTel pattern; `SimpleSpanProcessor` is acceptable for local.

### File: src/sovyx/observability/health.py — Score: 10/10 — ENTERPRISE
ABC `HealthCheck`, StrEnum status, `_safe_run` with timeout wrapping, 10 built-in checks, never-raises contract. Factory for default + offline registries.

### File: src/sovyx/observability/slo.py — Score: 10/10 — ENTERPRISE
Google SRE multi-window burn rate, deque ring buffers, 5 default SLOs, explicit alert-severity ranking helper.

### File: src/sovyx/observability/alerts.py — Score: 7/10 — DEVELOPED
AlertManager, state machine FIRING/RESOLVED, event-bus emission, burn-rate integration.
Failed criteria:
- **#6 CONCURRENCY**: `self._metrics: dict[str, deque[MetricSample]]` and `self._states: dict[str, AlertState]` are mutated from `record_metric()` (potentially from many tasks) and `evaluate()` (from scheduler task) with no `asyncio.Lock`. `AlertManager` relies on single-threaded async assumption but documents nothing. Under task interleaving, `evaluate()` builds `alert_lookup` then iterates `self._states.items()` in two passes — if `record_metric` runs between passes (not possible here since no `await` between them, but brittle). Acceptable by construction, but undocumented invariant.
- **#9 RESILIENCE (nit)**: `_emit_fired` / `_emit_resolved` do `await self._event_bus.emit(event)` with no try/except — if the event bus handler raises, `evaluate()` raises and the remaining transitions are lost (leaving `_states` partially updated).

### Summary — observability
7 ENTERPRISE / 1 DEVELOPED (alerts.py). No NOT-ENT.

---

## plugins (19 files)

### File: src/sovyx/plugins/__init__.py — Score: 10/10 — ENTERPRISE
Explicit re-export, `__all__`, no lazy tricks needed.

### File: src/sovyx/plugins/context.py — Score: 9/10 — ENTERPRISE
BrainAccess, EventBusAccess, PluginContext dataclass. Every method gated by `PermissionEnforcer.check()`. Source tagging `plugin:{name}`. Search result cap (50), content cap (10KB).
- **#10 CODE QUALITY (nit)**: Heavy direct access into brain internals (`self._brain._embedding`, `self._brain._concepts`, `self._brain._relations._pool`, `self._brain._episodes._pool`, `self._brain._memory._activations`, `self._brain._retrieval`, `self._brain._llm_router`). Quote: `embedding = await self._brain._embedding.encode(content, is_query=False)`. Violates SRP/encapsulation of `BrainService` — the plugin sandbox reaches into private attrs. Refactor opportunity: expose these on `BrainService` as public methods.

### File: src/sovyx/plugins/events.py — Score: 10/10 — ENTERPRISE
Frozen dataclasses, EventCategory.PLUGIN. Trivial and correct.

### File: src/sovyx/plugins/hot_reload.py — Score: 9/10 — ENTERPRISE
Watchdog optional-dep fallback, debounce, 3-retry reload, module-cache clear by prefix. Type-safe `object` annotations for watchdog types.
- **#6 CONCURRENCY (nit)**: `self._reload_count += 1` and `self._last_change[path] = now` mutated from thread-pool filesystem thread (`watchdog`) AND async reload task. Single writer per key mitigates, but not perfectly thread-safe. No lock.

### File: src/sovyx/plugins/lifecycle.py — Score: 10/10 — ENTERPRISE
Formal state machine (`_VALID_TRANSITIONS` table), history log, uptime calc, event emission with try/except.

### File: src/sovyx/plugins/manager.py — Score: 7/10 — DEVELOPED
God-class pressure (819 LOC, 25+ methods): registration, loading, execution, health, emitters, query, lifecycle. Mitigated by clean internal separation and `LoadedPlugin` dataclass.
Strong points: error boundary (`asyncio.wait_for`), consecutive-failure tracking, auto-disable, ImportGuard integration.
Failed criteria:
- **#10 CODE QUALITY**: 819 LOC in one file. Quote: class responsibilities span discovery, dependency resolution (`_topological_sort`), per-plugin health (`_PluginHealth`), tool execution, permission dispatch, and 4 separate `_emit_*` methods that are near-copies. God-class smell. Should split into `PluginLoader`, `PluginExecutor`, `PluginHealthTracker`, `PluginEventEmitter`.
- **#1 ERROR HANDLING (partial)**: `except Exception as e:  # noqa: BLE001` appears 7 times. Mostly justified (plugin boundary must swallow) but the bare `except Exception:  # noqa: BLE001  # nosec B110\n    pass` pattern in `_emit_*` methods is unauditable — if event emission keeps silently failing, the dashboard goes dark with no log. Quote: `except Exception:  # noqa: BLE001  # nosec B110\n    pass  # Event emission must never crash`. Should at minimum `logger.debug` on swallow.

### File: src/sovyx/plugins/manifest.py — Score: 10/10 — ENTERPRISE
Pure pydantic. `validate_permissions` cross-checks `Permission` enum. Name regex `^[a-z][a-z0-9\-]*$`. YAML loader with typed errors.

### File: src/sovyx/plugins/permissions.py — Score: 10/10 — ENTERPRISE
StrEnum, risk map, auto-disable on 10 denials, audit logging. Typed.

### File: src/sovyx/plugins/sandbox_fs.py — Score: 10/10 — ENTERPRISE
Path traversal defeated correctly: `(self._data_dir / relative).resolve()` then `target.relative_to(self._data_dir)`. This resolves symlinks BEFORE the check (the critical ordering). Rejects absolute paths up-front. 50MB/file, 500MB/plugin budget enforced before write. 35 tests.
Note: `_get_total_size()` called on EVERY write — O(files in tree) cost; at scale could be a latency issue, but within spec.

### File: src/sovyx/plugins/sandbox_http.py — Score: 8/10 — ENTERPRISE
Domain allowlist, `_is_local_ip` blocks loopback/private/link-local/multicast/reserved, rate limiter, timeout, response-size hint. 38 tests.
Failed criteria:
- **#5 SECURITY (TOCTOU)**: DNS rebinding "protection" resolves hostname separately via `socket.getaddrinfo()` (blocking! — called from async context, blocks event loop) then lets httpx do its own resolution on connect. Between the two, DNS can return a different IP. Quote: `resolved_ip = _resolve_hostname(hostname); if resolved_ip and _is_local_ip(resolved_ip) and not self._allow_local:` — then the actual request is `await self._client.request(method, url, ...)` which resolves again. A hostile DNS can serve `8.8.8.8` to the check and `127.0.0.1` to the real request. Real SSRF protection requires pinning the resolved IP onto the transport (custom httpx transport). Also, `_resolve_hostname` uses blocking `socket.getaddrinfo` which stalls the event loop.
- **#9 RESILIENCE (nit)**: `content_length` check only logs a warning when exceeded — the oversize response is still returned in full to the caller. The limit is advertised but not enforced. Quote: `if content_length and int(content_length) > self._max_bytes: logger.warning(...)` — no truncation, no raise. Caller receives the full bloat.

### File: src/sovyx/plugins/sdk.py — Score: 10/10 — ENTERPRISE
`@tool` decorator, auto JSON-Schema from type hints (Union/Optional/Literal/list/dict/Enum/primitives), OpenAI + Anthropic schema adapters, ABC with well-defined lifecycle.

### File: src/sovyx/plugins/security.py — Score: 9/10 — ENTERPRISE
AST scanner: blocks `os/subprocess/shutil/sys/importlib/ctypes/pickle/marshal/code/codeop/compileall/multiprocessing/threading/signal/resource/socket/http.server/xmlrpc/webbrowser/turtle/tkinter`; blocks calls to `eval/exec/compile/__import__`; blocks attributes `__import__/__subclasses__/__bases__/__globals__/__code__/__builtins__`. ImportGuard as runtime PEP 451 `MetaPathFinder`.
- **#5 SECURITY (pattern completeness)**: Gaps present — missing from BLOCKED_IMPORTS: `builtins` (direct `builtins.__import__`), `pty`, `pwd`, `grp`, `fcntl`, `mmap`, `nis`, `platform` (leaks hostname), `tempfile` (bypass of fs sandbox), `gc` (can walk frame objects), `inspect` (can walk frame objects → escape). Missing BLOCKED_ATTRIBUTES: `__closure__`, `__class__.__base__`, `co_consts`, `mro`, `gi_frame`, `cr_frame`, `f_back`, `f_locals`, `f_globals`. Defense-in-depth via ImportGuard partially covers the import side, but attribute-walk escapes (e.g., `().__class__.__base__.__subclasses__()`) are not blocked because `__subclasses__` IS blocked but `__base__` / `__class__` / `__mro__` are not. Sophisticated sandbox escape techniques remain possible.

### File: src/sovyx/plugins/testing.py — Score: 10/10 — ENTERPRISE
Complete mock harness: MockBrainAccess, MockEventBus, MockHttpClient, MockFsAccess, MockPluginContext. Assertion helpers. In-memory impls.

### File: src/sovyx/plugins/official/__init__.py — Score: 10/10 — ENTERPRISE
Empty package marker.

### File: src/sovyx/plugins/official/calculator.py — Score: 10/10 — ENTERPRISE
Thin wrapper over FinancialMathPlugin, backward-compat. Expression length cap, result cap, multi-exception catch.

### File: src/sovyx/plugins/official/financial_math.py — Score: 9/10 — ENTERPRISE
2019 LOC but justified by financial surface area (NPV, IRR, XIRR, depreciation, etc.). Pure AST-based expression eval, Decimal(28), ROUND_HALF_EVEN, max expression 500 chars, max exponent 1000, sanity caps. `_ValidationError`, `_require`, `_validate_value`, `_validate_list_len`, `_validate_periods`. 
- **#10 CODE QUALITY (nit)**: Single file is 2019 LOC; consider splitting into `expr.py`, `tvm.py`, `cashflow.py`, `stats.py`. Not a god class but a long file.

### File: src/sovyx/plugins/official/knowledge.py — Score: 10/10 — ENTERPRISE
922 LOC. Rate limiter (30 writes/min, 60 reads/min), centralized message catalog for i18n, retry with exponential (`_MAX_RETRIES`, `_RETRY_ERRORS`), JSON-structured returns.

### File: src/sovyx/plugins/official/weather.py — Score: 6/10 — DEVELOPED
Failed criteria:
- **#5 SECURITY**: Bypasses SandboxedHttpClient. Quote: `async with httpx.AsyncClient(timeout=10.0) as client: resp = await client.get(_GEOCODING_URL, ...)`. An official plugin that ships in-tree should exemplify the sandbox — instead it imports raw `httpx` twice (`_geocode` and `_fetch_weather`) and ignores the `SandboxedHttpClient` entirely. This also means domain allowlist is not enforced here: the plugin could be modified (or a supply-chain variant) to hit any URL, because the sandbox never sees these calls. Inconsistent with the architecture.
- **#1 ERROR HANDLING**: `except Exception:  # noqa: BLE001` in both helpers → just `return None`. No log, no distinguishing network error vs JSON error vs timeout.
- **#3 OBSERVABILITY**: No `logger` import, no metrics. On failure, caller only gets "Error fetching weather data" string.
- **#9 RESILIENCE**: No retry, no circuit breaker around Open-Meteo. One blip → empty response.

### File: src/sovyx/plugins/official/web_intelligence.py — Score: 7/10 — DEVELOPED
1962 LOC. Rate limits (30 search/min, 20 fetch/min, 5 research/min), tiered source credibility (three tiers of domain frozensets), sanitization (`_CONTROL_CHARS`), query length cap, result caps, timeouts, brain integration.
Failed criteria:
- **#5 SECURITY**: Same as weather — uses raw `httpx.AsyncClient` directly (3 sites) instead of `SandboxedHttpClient`. Quote: `import httpx  # noqa: PLC0415` followed by `async with httpx.AsyncClient(timeout=_SEARCH_TIMEOUT) as client:`. The plugin's `network:internet` permission is gated but the actual request path is NOT through the sandbox's domain-allowlist or local-IP check — SearXNG self-hosted URLs could easily hit internal services without the sandbox being consulted.
- **#10 CODE QUALITY (nit)**: 1962 LOC in one file — search backends, credibility, brain integration, caching. Could split.

---

## Top issues across C

1. **Sandbox bypass in official plugins (weather, web_intelligence)**: Raw `httpx.AsyncClient` imports directly, circumventing `SandboxedHttpClient`. This is the most dangerous inconsistency: the plugin platform ships its own SSRF/allowlist system, then the reference plugins don't use it. If a future plugin developer copies this pattern, the whole sandbox architecture is theater. **Fix: wire `ctx.http` (SandboxedHttpClient) into every official plugin; ban direct httpx at the ruff level (TID252 or custom check).**

2. **DNS rebinding is broken in `sandbox_http.py`**: Two-resolution TOCTOU — `socket.getaddrinfo` (blocking!) then httpx resolves again. Also blocks event loop. **Fix: use a custom httpx transport that pins the first resolved IP, or disable DNS lookup and pass the IP literally; move lookup to `asyncio.get_running_loop().getaddrinfo`.**

3. **AST scanner pattern completeness (`plugins/security.py`)**: Missing modules (`builtins`, `tempfile`, `gc`, `inspect`, `mmap`, `pty`) and attributes (`__class__`, `__mro__`, `__base__`, `gi_frame`, `cr_frame`, `f_*`). Sophisticated escapes (`().__class__.__base__.__subclasses__()`) still work. **Fix: expand blocklists; layer sys-audit hook; consider subprocess-based sandbox for untrusted code.**

4. **`plugins/manager.py` god-class (819 LOC)**: Discovery + loading + execution + health + emission + queries + lifecycle in one class. **Fix: split into 4 SRP classes; consolidate 4 `_emit_*` near-duplicates into one typed emitter.**

5. **Silent event-emission swallow in manager.py**: `except Exception: pass  # Event emission must never crash` with no logging. If the event bus goes dark, no one notices. **Fix: `logger.debug` the exception before swallowing.**

6. **`alerts.py` concurrency invariant undocumented**: Mutable dicts with no lock, relying on asyncio cooperative scheduling. Works today, fragile to future refactors (adding any `await` between reads could race). **Fix: document the invariant in module docstring, or guard with `asyncio.Lock()`.**

7. **oversize-response enforcement missing in `sandbox_http.py`**: `content_length` header is only logged when exceeded — response is still returned. **Fix: stream with `httpx.stream()` and abort once max_bytes read; or raise `PermissionDeniedError`.**

8. **Blocking IO in async paths**: `socket.getaddrinfo()` in `sandbox_http.py`, `os.walk()` in `sandbox_fs._get_total_size()` called on every write. **Fix: `asyncio.to_thread` or event-loop `getaddrinfo`.**

9. **Encapsulation leak in `plugins/context.py` BrainAccess**: Reaches into 7+ private attributes of BrainService. **Fix: expose a narrow public API on BrainService for plugin use.**

10. **File sizes**: financial_math.py (2019), web_intelligence.py (1962), knowledge.py (922), manager.py (819) — not bugs but test-surface and cognitive-load issues.

### Final verdict

- **persistence**: ENTERPRISE (9.2 avg). Best-engineered module of the three.
- **observability**: ENTERPRISE (9.1 avg). Only alerts.py drops a point on concurrency-invariant discipline.
- **plugins**: ENTERPRISE overall (8.4 avg), but with real security gaps in the two network-facing official plugins and scanner completeness. The sandbox INFRASTRUCTURE is enterprise; the USE of it is inconsistent.

No file falls into NOT-ENTERPRISE. The backend is production-shippable; the plugin-sandbox consistency issues are the single most important thing to fix before onboarding third-party plugin developers.
