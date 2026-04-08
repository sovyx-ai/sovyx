# Sovyx v1.0 — Security & Multi-Tenant Architecture Guide

> Technical debt analysis, remediation strategies, and architecture decisions
> for enterprise-ready multi-tenant production.
>
> Generated from deep-dive audit on v0.5.1 codebase (2026-04-08).
> **Updated:** 2026-04-08 — Added §0 (completed fixes), updated §2 and §5 with current state.

---

## Table of Contents

0. [Completed Fixes (v0.5.1)](#0-completed-fixes-v051)
1. [WebSocket Authentication](#1-websocket-authentication)
2. [Type Safety Audit](#2-type-safety-audit)
3. [Exception Handling Architecture](#3-exception-handling-architecture)
4. [Multi-Tenant Architecture](#4-multi-tenant-architecture)
5. [Additional Gaps Discovered](#5-additional-gaps-discovered)
6. [Implementation Priority](#6-implementation-priority)

---

## 0. Completed Fixes (v0.5.1)

> These items were identified during the audit and fixed immediately because
> they were low-risk, high-value, and architecturally independent of v1.0
> multi-tenant decisions. Each one improves the codebase NOW without creating
> throwaway work that would need to be redone later.

### 0.1 FastAPI Version Hardcode → Dynamic

**Commit:** `7d16538`

**Problem:** `FastAPI(version="0.1.0")` was hardcoded in `server.py:177`.
OpenAPI docs and `/api/docs` showed wrong version. Any automation or client
checking the API version would get stale data.

**Fix:** `FastAPI(version=__version__)` — imports from `sovyx.__init__`.

**Why now:** One-line change. Zero risk. No architectural dependency.
The version was already maintained in 3 places (`pyproject.toml`,
`__init__.py`, `package.json`) — this just wired the server to read it.

**Why not v1.0:** No reason to defer. It's a bug, not a design decision.

---

### 0.2 Error Detail Leak in Chat Endpoint

**Commit:** `7d16538`

**Problem:** `except ValueError as exc: return JSONResponse({"error": str(exc)})`.
The `str(exc)` could expose internal details (file paths, config values,
validation messages with field names) to the client. In security testing
(FE-27), we confirmed that the `/api/brain/search` endpoint returns
user-controlled content in JSON responses, and while `Content-Type:
application/json` + CSP mitigate XSS, error message content should still
be opaque to clients.

**Fix:**
```python
# Before:
return JSONResponse({"error": str(exc)}, status_code=422)

# After:
logger.warning("dashboard_chat_validation_error", error=str(exc))
return JSONResponse({"error": "Invalid request"}, status_code=422)
```

**Why now:** Security fix. No architectural dependency. The detail is
still available in server logs for debugging.

**Why not v1.0:** Error message opacity is a security principle, not a
multi-tenant feature. Deferring would leave a known information disclosure
vector in production.

---

### 0.3 Private Attribute Access → Public Properties (SLF001: 5 → 0)

**Commit:** `7d16538` (BridgeManager), `96830b2` (remaining 4)

**Problem:** 5 modules accessed private attributes (`_attr`) of other
classes, violating encapsulation. Each was suppressed with `# noqa: SLF001`.

| Class | Private Access | New Property | Consumers |
|-------|---------------|--------------|-----------|
| `BridgeManager` | `_mind_id` | `mind_id: MindId` | `chat.py` |
| `PersonalityEngine` | `_config` | `config: MindConfig` | `server.py` |
| `CloudBackupService` | `_r2`, `_config` | `r2: R2Client`, `backup_config: BackupConfig` | `scheduler.py` |
| `MigrationRunner` | `_version` | `schema_version: SchemaVersion` | `blue_green.py` |
| `DatabasePool` | `_db_path` | `db_path: Path` | `schema.py` |

**Why now:** Each property is 3 lines (decorator + docstring + return).
Zero behavioral change. Improves API surface for any future consumer.
Tests that mocked `_attr` directly were updated to use the public property.

**Why not v1.0:** Encapsulation violations compound. Every new consumer
would copy the `_attr` pattern and add another `SLF001`. Fixing early
prevents debt accumulation.

**Design decision:** Properties (read-only) rather than methods, because
these are identity/configuration attributes — not computed or expensive.
No setter needed; mutation goes through dedicated methods.

---

### 0.4 Type Safety Cleanup (type: ignore: 37 → 28)

**Commit:** `96830b2`

9 suppressions eliminated across 4 categories:

#### Fixed: Container Covariance (2 of 5)

| File | Before | After | Why |
|------|--------|-------|-----|
| `brain/retrieval.py` | `sorted(scores, key=scores.get)  # type: ignore[arg-type]` | `sorted(scores, key=lambda k: scores.get(k, 0.0))` | `dict.get` returns `Optional[V]` which doesn't match `Callable[[K], SupportsLessThan]`. Lambda with default is type-safe. |
| `brain/working_memory.py` | `min(self._activations, key=self._activations.get)  # type: ignore[arg-type]` | `min(self._activations, key=lambda k: self._activations.get(k, 0.0))` | Same pattern. |

**3 kept (aiosqlite.Row):** `concept_repo.py:274`, `episode_repo.py:192`,
`relation_repo.py:260`. The `aiosqlite.Row` type is declared as `object`
in the official stubs. `tuple(row)` is correct at runtime but mypy can't
verify it. No fix possible without upstream stub changes or a wrapper
function that adds overhead per-row.

#### Fixed: Generic Registry Returns (2)

| File | Before | After |
|------|--------|-------|
| `engine/registry.py:94` | `return self._instances[interface]  # type: ignore[return-value]` | `return cast(T, self._instances[interface])` |
| `engine/registry.py:103` | `return instance  # type: ignore[return-value]` | `return cast(T, instance)` |

**Why `cast` and not `@overload`:** The registry stores `dict[type, object]`
internally. `T` comes from the caller's `resolve(SomeType)`. `cast` is the
standard pattern for DI containers — it tells mypy "trust me, this is `T`"
without runtime overhead. `@overload` wouldn't help because the key is
dynamic.

#### Fixed: Literal Narrowing (3)

| File | Before | After |
|------|--------|-------|
| `dashboard/settings.py:83` | `config.log.level = level  # type: ignore[assignment]` | `config.log.level = cast("Any", level)` |
| `dashboard/config.py:174` | `p.tone = tone  # type: ignore[assignment]` | `p.tone = cast("Any", tone)` |
| `dashboard/config.py:243` | `s.content_filter = cf  # type: ignore[assignment]` | `s.content_filter = cast("Any", cf)` |

**Why `cast(Any)` instead of named type aliases:**

These fields are `Literal["warm", "neutral", "direct", "playful"]` etc.
in Pydantic models. The value comes from validated user input (`str` that
was already checked against `valid_tones`). Options for fixing:

1. **`cast(Literal["warm", "neutral", ...], tone)`** — Correct but verbose,
   duplicates the Literal definition (DRY violation). If someone adds a tone
   to the model but forgets the cast, silent type mismatch.

2. **Named type alias** (`ToneType = Literal[...]`, used in both model and
   cast) — Ideal but requires creating aliases, exporting them, updating
   imports. ~30 lines of ceremony for 3 assignments in stable code.

3. **`cast(Any, tone)`** — Pragmatic. The `if tone in valid_tones` check
   above already validates correctness at runtime. The cast just silences
   mypy. Type safety is maintained by the runtime check, not the annotation.

**Chose option 3.** For v1.0, option 2 is recommended when the config module
gets refactored for multi-tenant (new fields, new validation patterns).

#### Fixed: Timezone Type (1)

| File | Before | After |
|------|--------|-------|
| `context/formatter.py:138` | `tz = UTC  # type: ignore[assignment]` | `resolved_tz: ZoneInfo \| datetime.timezone = UTC` |

**Why:** `tz` was declared as `ZoneInfo` by mypy inference from the `try`
block. The `except` fallback to `UTC` (`datetime.timezone`) was a type
mismatch. Fix: union type annotation. Renamed to `resolved_tz` to avoid
`no-redef` error from mypy's flow analysis.

#### Fixed: Misc (1)

| File | Before | After |
|------|--------|-------|
| `cloud/backup.py:187` | `return response["Body"].read()  # type: ignore[no-any-return]` | `data: bytes = response["Body"].read(); return data` |

**Why:** boto3's `.read()` returns `Any` in the stubs. Intermediate
variable with `: bytes` annotation gives mypy the concrete type.

#### Kept: asyncio.run (1)

`cli/main.py:39` — `asyncio.run(coro)  # type: ignore[arg-type]`

**Why kept:** The function signature is `def _run(coro: object) -> object`
to match the CLI dispatcher pattern. `asyncio.run()` expects
`Coroutine[Any, Any, T]` but we receive `object` from typer's callback
chain. The only alternative is `Any` annotations, which ruff's `ANN401`
rule forbids. This is a genuine type system limitation at the CLI boundary.

#### Remaining 28 Suppressions — Full Audit

| Category | Count | Files | Fixable in v1.0? |
|----------|-------|-------|------------------|
| Optional dep imports (`import-not-found`) | 10 | voice/*, cloud/backup, cloud/llm_proxy | No — runtime-optional packages |
| Untyped imports (`import-untyped`) | 4 | brain/embedding, voice/vad, voice/tts_piper, voice/wake_word | Yes — write `.pyi` stubs for `onnxruntime` |
| Moonshine SDK (`misc`, `attr-defined`) | 8 | voice/stt.py | Yes — write `.pyi` stub for `moonshine_voice` |
| aiosqlite.Row → tuple (`arg-type`) | 3 | brain/*_repo.py | No — upstream stub limitation |
| asyncio.run boundary (`arg-type`) | 1 | cli/main.py | No — type system limitation |
| Telegram kwargs (`arg-type`) | 1 | bridge/channels/telegram.py | Yes — TypedDict for kwargs |
| Event bus emit (`arg-type`) | 1 | voice/pipeline.py | Yes — widen EventBus.emit signature |

**v1.0 target:** Write `.pyi` stubs for `onnxruntime` + `moonshine_voice`
→ eliminates 12. Fix telegram kwargs + event bus emit → eliminates 2.
**Result: 28 → 14** (all legitimate optional-dep or upstream limitations).

---

### 0.5 Request ID Middleware

**Commit:** `96830b2`

**What:** New `RequestIdMiddleware` added to the ASGI middleware stack.

```python
class RequestIdMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        request_id = request.headers.get("x-request-id") or str(uuid.uuid4())
        request.state.request_id = request_id
        response = await call_next(request)
        response.headers["X-Request-Id"] = request_id
        return response
```

**Why now:**
- 20 lines, zero dependencies, zero risk
- Enables request tracing TODAY — any log that reads `request.state.request_id`
  gets correlation for free
- Reverse proxies (nginx, Caddy) can set `X-Request-Id` upstream and it
  flows through unchanged
- Client-side: response header allows correlating UI errors with server logs

**Why not v1.0:** Request ID is infrastructure, not business logic. It's
useful immediately for debugging. Deferring means every debug session
between now and v1.0 lacks correlation.

**Middleware ordering:** Added BEFORE `SecurityHeadersMiddleware` so that
security headers are applied to all responses including error responses
from request ID generation.

**v1.0 improvement:** Integrate with structlog's `contextvars` so every
log line in the request lifecycle automatically includes the request ID:

```python
# v1.0:
import structlog
structlog.contextvars.bind_contextvars(request_id=request_id)
```

---

### 0.6 Summary of v0.5.1 Fixes

| Metric | Before | After | Delta |
|--------|--------|-------|-------|
| `type: ignore` | 37 | 28 | -9 |
| `noqa: SLF001` | 5 | 0 | -5 |
| Request ID tracing | ❌ | ✅ | New |
| Version hardcode | `"0.1.0"` | `__version__` | Fixed |
| Error detail leak | `str(exc)` → client | Generic message | Fixed |
| CI runs (last 3) | — | ✅ ✅ ✅ | All green |

**What was NOT fixed (and why):**

| Item | Why Deferred |
|------|-------------|
| WebSocket ticket auth | Depends on JWT design (§1). Building a ticket system against the current single-token auth would create a temporary system that gets replaced in v1.0. |
| Exception narrowing (136 blocks) | Requires `SovyxError` hierarchy (§3) which must be designed alongside multi-tenant error semantics. Narrowing `except Exception` to `except (OSError, SovyxError)` today would need re-narrowing when tenant-specific errors exist. |
| Multi-tenant (JWT, RBAC, isolation) | Over-engineering without users. The architecture is documented (§4) but implementing without real multi-tenant requirements risks building the wrong abstractions. |
| CSRF protection | Not applicable until JWT moves to httpOnly cookies (§4). Current Bearer token is CSRF-immune. |
| Audit logging | Multi-tenant feature. Single-user audit log has minimal value and adds write overhead. |
| Rate limiting | Single-user. Rate limiting yourself is pointless. |
| `.pyi` stubs (onnxruntime, moonshine) | Low priority. Voice module is stable, stubs would prevent regressions but don't fix bugs. v1.0 when voice module gets refactored. |

**Principle applied:** Fix what improves the code NOW without creating
throwaway work. Document what needs architectural decisions. Defer what
depends on features that don't exist yet.

---

## 1. WebSocket Authentication

### Current State (v0.5)

```
Client → /ws?token=<raw-token> → Server validates → Connection accepted
```

**Problem:** The raw auth token is sent as a URL query parameter.

**Attack vectors:**
- **Browser history:** URL with token stored in browsing history
- **Proxy/CDN logs:** Intermediate proxies log full URLs including query params
- **Referrer header:** If WS page links elsewhere, token leaks via `Referer`
- **Server access logs:** Default web server configs log query strings
- **Shoulder surfing:** Token visible in DevTools Network tab URL

**Why it exists:** The WebSocket API (`new WebSocket(url)`) does not support
custom HTTP headers. This is a browser limitation, not a code shortcut.

### v1.0 Solution: Ticket-Based Authentication

```
┌──────────┐    POST /api/ws-ticket     ┌──────────┐
│  Browser  │ ─────────────────────────→ │  Server   │
│           │   Authorization: Bearer T  │           │
│           │ ←───────────────────────── │           │
│           │   { "ticket": "abc123" }   │           │
│           │                            │           │
│           │    /ws?ticket=abc123        │           │
│           │ ─────────────────────────→ │           │
│           │   (WebSocket upgrade)      │           │
│           │ ←═════════════════════════ │           │
│           │   Connection established   │           │
└──────────┘                            └──────────┘
```

**Ticket properties:**
- Single-use (consumed on WS connect, cannot be replayed)
- Short-lived (30s TTL — enough for immediate WS connection)
- Cryptographically random (32 bytes, `secrets.token_urlsafe(32)`)
- Bound to origin IP (optional, prevents ticket theft)
- Not the auth token (if logged, doesn't compromise the session)

### Implementation Plan

**Backend (`server.py`):**

```python
import time
from dataclasses import dataclass

@dataclass(frozen=True, slots=True)
class _WsTicket:
    """Single-use WebSocket authentication ticket."""
    token_hash: str      # SHA-256 of the auth token that created it
    created_at: float    # time.monotonic()
    client_ip: str       # Optional IP binding

_TICKET_TTL = 30.0  # seconds
_ticket_store: dict[str, _WsTicket] = {}  # ticket_id → metadata

@app.post("/api/ws-ticket", dependencies=[Depends(verify_token)])
async def create_ws_ticket(request: Request) -> JSONResponse:
    """Issue a single-use ticket for WebSocket connection."""
    # Prune expired tickets
    now = time.monotonic()
    expired = [k for k, v in _ticket_store.items() if now - v.created_at > _TICKET_TTL]
    for k in expired:
        del _ticket_store[k]

    ticket_id = secrets.token_urlsafe(32)
    _ticket_store[ticket_id] = _WsTicket(
        token_hash=hashlib.sha256(_server_token.encode()).hexdigest(),
        created_at=now,
        client_ip=request.client.host if request.client else "",
    )
    return JSONResponse({"ticket": ticket_id, "expires_in": int(_TICKET_TTL)})

@app.websocket("/ws")
async def websocket_endpoint(
    websocket: WebSocket,
    ticket: str | None = Query(default=None),
    # Keep legacy token param for backward compat during migration
    token: str | None = Query(default=None),
) -> None:
    authenticated = False

    if ticket and ticket in _ticket_store:
        meta = _ticket_store.pop(ticket)  # Single-use: consume immediately
        if time.monotonic() - meta.created_at <= _TICKET_TTL:
            authenticated = True

    # Legacy fallback (remove in v1.1)
    if not authenticated and token:
        authenticated = secrets.compare_digest(token, _server_token)

    if not authenticated:
        await websocket.close(code=4001, reason="Unauthorized")
        return

    await ws_manager.connect(websocket)
    # ... rest unchanged
```

**Frontend (`use-websocket.ts`):**

```typescript
async function getWsTicket(): Promise<string> {
  const resp = await api.post<{ ticket: string }>("/api/ws-ticket");
  return resp.ticket;
}

// In connect():
const ticket = await getWsTicket();
const ws = new WebSocket(`${WS_BASE}/ws?ticket=${encodeURIComponent(ticket)}`);
```

**Migration strategy:**
1. v1.0-alpha: Add ticket endpoint + support both `ticket` and `token` params
2. v1.0-beta: Frontend uses ticket by default, `token` param emits deprecation warning
3. v1.1: Remove `token` query param support entirely

**Tests required:**
- Ticket is single-use (second connection with same ticket fails)
- Ticket expires after TTL
- Expired ticket rejected
- Legacy `token` param still works (migration period)
- Concurrent ticket creation under load
- Ticket store doesn't leak memory (pruning works)

---

## 2. Type Safety Audit

### Current State

37 `# type: ignore` annotations across the codebase. Audit of each:

### Category A: Legitimate — Optional Dependencies (13 instances)

These suppress `import-not-found` or `import-untyped` for optional packages
that may not be installed. **Correct usage — keep as-is.**

| File | Line | Suppression | Package |
|------|------|-------------|---------|
| `cloud/backup.py` | 169 | `import-not-found` | `boto3` |
| `cloud/llm_proxy.py` | 471 | `import-not-found` | `litellm` |
| `voice/vad.py` | 159 | `import-untyped` | `onnxruntime` |
| `voice/audio.py` | 264 | `import-not-found` | `sounddevice` |
| `voice/tts_piper.py` | 225 | `import-untyped` | `onnxruntime` |
| `voice/tts_piper.py` | 289 | `import-not-found` | `piper_phonemize` |
| `voice/wyoming.py` | 770-771 | `import-not-found` | `zeroconf` |
| `voice/pipeline.py` | 283 | `import-not-found` | `sounddevice` |
| `voice/tts_kokoro.py` | 181 | `import-not-found` | `kokoro_onnx` |
| `voice/wake_word.py` | 245 | `import-untyped` | `onnxruntime` |
| `voice/stt.py` | 225 | `import-not-found` | `moonshine_voice` |
| `brain/embedding.py` | 225-226 | `import-untyped` | `onnxruntime`, `tokenizers` |

**v1.0 action:** Create stub files (`py.typed` stubs) for critical optional deps
(`onnxruntime`, `tokenizers`). For truly optional deps, keep `type: ignore`.

### Category B: Generic Container Covariance (6 instances)

These handle SQLite row tuples and dict `.get()` return types where mypy
can't infer the concrete type.

| File | Line | Issue |
|------|------|-------|
| `brain/retrieval.py` | 180 | `dict.get` as sort key |
| `brain/working_memory.py` | 45 | `dict.get` as min key |
| `brain/relation_repo.py` | 260 | `sqlite3.Row` → `tuple` |
| `brain/episode_repo.py` | 192 | `sqlite3.Row` → `tuple` |
| `brain/concept_repo.py` | 274 | `sqlite3.Row` → `tuple` |
| `engine/registry.py` | 94, 103 | Generic `T` return from dict |

**v1.0 action:** Fix with explicit casts:

```python
# Before:
sorted_ids = sorted(scores, key=scores.get, reverse=True)  # type: ignore[arg-type]

# After:
sorted_ids = sorted(scores, key=lambda x: scores.get(x, 0.0), reverse=True)
```

```python
# Before:
r: tuple[Any, ...] = tuple(row)  # type: ignore[arg-type]

# After — typed row parser:
def _parse_row(row: aiosqlite.Row) -> tuple[Any, ...]:
    return tuple(row)
```

```python
# Registry — use TypeVar bound correctly:
# Before:
return self._instances[interface]  # type: ignore[return-value]

# After — properly typed with overload:
@overload
def resolve(self, interface: type[T]) -> T: ...
```

**Estimated effort:** 2-3 hours. All mechanical.

### Category C: Literal Narrowing (3 instances)

Config assignment where mypy can't narrow `str` to `Literal["debug", "info", ...]`.

| File | Line | Issue |
|------|------|-------|
| `dashboard/settings.py` | 83 | `config.log.level = level` |
| `dashboard/config.py` | 174 | `p.tone = tone` |
| `dashboard/config.py` | 243 | `s.content_filter = cf` |

**v1.0 action:** Add runtime validation + explicit cast:

```python
# Before:
config.log.level = level  # type: ignore[assignment]

# After:
_VALID_LEVELS: Final = frozenset({"debug", "info", "warning", "error", "critical"})
if level not in _VALID_LEVELS:
    raise ValueError(f"Invalid log level: {level}")
config.log.level = cast(LogLevel, level)
```

### Category D: Third-Party Library Gaps (8 instances)

Moonshine voice SDK has no type stubs. Multiple `attr-defined` and `misc`
suppressions for its `TranscriptEventListener` and event objects.

| File | Lines | Issue |
|------|-------|-------|
| `voice/stt.py` | 319-420 | 7 suppressions for moonshine API |
| `voice/pipeline.py` | 820 | Event bus emit with union type |

**v1.0 action:** Write a `.pyi` stub file for `moonshine_voice`:

```python
# stubs/moonshine_voice/__init__.pyi
class TranscriptEventListener:
    def on_partial(self, event: TranscriptEvent) -> None: ...
    def on_final(self, event: TranscriptEvent) -> None: ...
    def on_error(self, event: TranscriptEvent) -> None: ...

class TranscriptEvent:
    class Line:
        text: str
    line: Line
```

### Category E: Miscellaneous (7 instances)

| File | Line | Issue | Fix |
|------|------|-------|-----|
| `cli/main.py` | 39 | `asyncio.run` arg-type | Wrapper function with proper typing |
| `cloud/backup.py` | 187 | `no-any-return` from boto3 | Cast `bytes` |
| `context/formatter.py` | 138 | `UTC` assignment | Use `datetime.timezone.utc` |
| `bridge/channels/telegram.py` | 115 | `**kwargs` arg-type | TypedDict for kwargs |

**v1.0 target:** Zero `type: ignore` in core modules (`engine/`, `bridge/`,
`cognitive/`, `dashboard/`). Optional deps in `voice/` and `cloud/` can keep
justified suppressions.

### Summary (Updated 2026-04-08)

> **9 of 37 fixed in v0.5.1** (commit `96830b2`). See §0.4 for details.

| Category | Original | Fixed in v0.5.1 | Remaining | v1.0 Action |
|----------|----------|-----------------|-----------|-------------|
| A. Optional deps | 13 | 0 | 13 | Keep (runtime-optional, unfixable) |
| B. Container covariance | 6 | 2 | 4 (3 aiosqlite + 1 telegram) | 3 unfixable (upstream), 1 fixable |
| C. Literal narrowing | 3 | 3 | 0 | ✅ Done |
| D. Third-party stubs | 8 | 0 | 8 | Write `.pyi` stubs |
| E. Miscellaneous | 7 | 4 | 3 | 1 unfixable (asyncio), 2 fixable |
| **Total** | **37** | **9** | **28** | **14 fixable → target: 14** |

---

## 3. Exception Handling Architecture

### Current State

136 `except Exception` blocks across the codebase. This is the most serious
architectural debt.

### Taxonomy

After analyzing all 136 occurrences, they fall into 5 patterns:

#### Pattern 1: "Best-Effort" Operations (47 instances)

**Where:** Dashboard queries, health checks, status collection, brain search.

**Current:**
```python
except Exception:  # noqa: BLE001
    logger.exception("brain_graph_failed")
    return {"nodes": [], "edges": []}
```

**Problem:** Catches everything including `KeyboardInterrupt`, `SystemExit`,
`MemoryError`. A `MemoryError` silently returns empty data instead of
propagating. On multi-tenant, this masks cascading failures.

**v1.0 fix — SovyxServiceError hierarchy:**

```python
# src/sovyx/engine/errors.py

class SovyxError(Exception):
    """Base for all Sovyx-specific errors."""

class ServiceUnavailableError(SovyxError):
    """A required service (DB, LLM, brain) is not available."""

class ConfigurationError(SovyxError):
    """Invalid or missing configuration."""

class AuthenticationError(SovyxError):
    """Authentication failed."""

class AuthorizationError(SovyxError):
    """User lacks permission for this operation."""

class ResourceNotFoundError(SovyxError):
    """Requested resource does not exist."""

class ValidationError(SovyxError):
    """Input validation failed."""

class RateLimitError(SovyxError):
    """Rate limit exceeded."""

class TenantIsolationError(SovyxError):
    """Cross-tenant access attempt detected."""
```

**Refactored dashboard pattern:**

```python
# Before:
except Exception:  # noqa: BLE001
    return {"nodes": [], "edges": []}

# After:
except (aiosqlite.Error, ServiceUnavailableError) as exc:
    logger.warning("brain_graph_unavailable", error=str(exc))
    return {"nodes": [], "edges": []}
except SovyxError as exc:
    logger.warning("brain_graph_error", error=str(exc))
    raise
# Let non-Sovyx exceptions (MemoryError, SystemExit) propagate
```

#### Pattern 2: CLI Error Presentation (7 instances, all `pragma: no cover`)

**Where:** `cli/main.py` — wrapping daemon operations.

**Current:**
```python
except Exception as e:  # pragma: no cover
    console.print(f"[red]Error: {e}[/red]")
    raise typer.Exit(1) from e
```

**Assessment:** This is actually correct for CLI UX — catch everything,
present a human message, exit cleanly. The `pragma: no cover` is acceptable
because CLI error paths are hard to unit test (they involve daemon IPC).

**v1.0 fix:** Add integration tests for CLI error paths using subprocess.
Replace `Exception` with `(OSError, SovyxError)` + separate `except Exception`
for truly unexpected errors with "please report a bug" messaging.

#### Pattern 3: Graceful Degradation (28 instances)

**Where:** Health checks, upgrade/migration, bootstrap.

**Current:**
```python
except Exception as e:  # pragma: no cover
    return HealthResult(status="red", message=str(e))
```

**Assessment:** Health checks MUST catch broadly — a health check that
crashes is worse than one that reports "red". Same for migrations.

**v1.0 fix:** Narrow to `(OSError, aiosqlite.Error, SovyxError)` plus
a final `except Exception` that:
1. Logs at ERROR level with full traceback
2. Increments an `unhandled_exception_total` Prometheus counter
3. Returns degraded response
4. Triggers alert if counter exceeds threshold

#### Pattern 4: Resource Cleanup (18 instances)

**Where:** DB pool, connection close, file cleanup.

**Current:**
```python
finally:
    try:
        await conn.close()
    except Exception:
        pass
```

**Assessment:** Correct — cleanup must not throw. But should log.

**v1.0 fix:**
```python
finally:
    try:
        await conn.close()
    except OSError:
        logger.debug("connection_close_failed", exc_info=True)
```

#### Pattern 5: Cognitive Pipeline (8 instances)

**Where:** `cognitive/gate.py`, `cognitive/loop.py`, `cognitive/think.py`.

**Most critical.** These handle LLM errors during thinking.

**Current:**
```python
except Exception as e:
    logger.exception("cognitive_loop_failed")
    return ActionResult(error=True, response_text="I encountered an error.")
```

**Problem in multi-tenant:** One tenant's malformed input crashes the
cognitive pipeline, affecting other tenants sharing the same process.

**v1.0 fix:**

```python
except (LLMProviderError, TimeoutError) as exc:
    # Expected LLM failures — recoverable
    logger.warning("cognitive_llm_error", tenant_id=ctx.tenant_id, error=str(exc))
    return ActionResult(error=True, response_text="Service temporarily unavailable.")
except SovyxError as exc:
    # Known application errors
    logger.error("cognitive_app_error", tenant_id=ctx.tenant_id, error=str(exc))
    return ActionResult(error=True, response_text="An error occurred.")
except Exception as exc:
    # Unknown errors — log, alert, but don't crash the process
    logger.critical("cognitive_unhandled", tenant_id=ctx.tenant_id, exc_info=True)
    metrics.counter("cognitive_unhandled_total").inc()
    return ActionResult(error=True, response_text="An unexpected error occurred.")
```

### Exception Handling Summary

| Pattern | Count | v1.0 Strategy |
|---------|-------|---------------|
| Best-effort queries | 47 | Narrow to `(OSError, aiosqlite.Error, SovyxError)` |
| CLI presentation | 7 | Keep broad, add integration tests |
| Graceful degradation | 28 | Narrow + unhandled counter + alerting |
| Resource cleanup | 18 | Narrow to `OSError`, add debug logging |
| Cognitive pipeline | 8 | Layered: LLM → SovyxError → Exception with metrics |
| **Remaining misc** | **28** | **Audit individually during v1.0 development** |

### Global Exception Middleware (new)

```python
class UnhandledExceptionMiddleware(BaseHTTPMiddleware):
    """Catch-all for unhandled exceptions in API endpoints.

    - Logs full traceback at CRITICAL level
    - Increments Prometheus counter
    - Returns generic 500 (no detail leak)
    - Alerts if rate exceeds threshold
    """
    async def dispatch(self, request, call_next):
        try:
            return await call_next(request)
        except Exception:
            logger.critical("unhandled_api_exception", exc_info=True, path=request.url.path)
            metrics.counter("api_unhandled_exception_total", labels={"path": request.url.path}).inc()
            return JSONResponse(
                {"error": "Internal server error"},
                status_code=500,
                headers={"X-Request-Id": request.state.request_id},
            )
```

---

## 4. Multi-Tenant Architecture

### Current Single-Tenant Assumptions

| Component | v0.5 Assumption | Multi-Tenant Requirement |
|-----------|-----------------|--------------------------|
| **Auth** | Single token in `~/.sovyx/token` | Per-user JWT/OAuth2 + RBAC |
| **User ID** | `_DASHBOARD_CHANNEL_USER_ID = "dashboard-user"` | Real user identity from auth |
| **Mind** | Single mind per instance | Mind-per-tenant or shared minds |
| **DB** | Single SQLite file | Per-tenant DB or row-level isolation |
| **Config** | Single `system.yaml` | Per-tenant config with inheritance |
| **WS** | Broadcast to all connections | Per-tenant event streams |
| **Token** | File-based, single value | JWT with claims (tenant_id, roles, exp) |
| **Rate limiting** | None on dashboard | Per-tenant rate limits |

### Authentication Architecture (v1.0)

```
┌──────────────┐     ┌────────────────┐     ┌───────────────┐
│   Browser     │────→│  Auth Gateway   │────→│  JWT Issuer    │
│               │     │  (middleware)   │     │  (internal)    │
│  JWT in       │     │                │     │                │
│  httpOnly     │     │  Validates JWT │     │  Signs tokens  │
│  cookie       │     │  Extracts:     │     │  Refresh flow  │
│               │     │  - tenant_id   │     │                │
│               │     │  - user_id     │     │                │
│               │     │  - roles[]     │     │                │
└──────────────┘     └────────────────┘     └───────────────┘
```

**JWT claims:**
```json
{
  "sub": "user-uuid-123",
  "tid": "tenant-uuid-456",
  "roles": ["admin", "chat"],
  "minds": ["aria", "custom-mind"],
  "iat": 1712560000,
  "exp": 1712563600,
  "iss": "sovyx"
}
```

**Token storage (browser):**
- Access token: `httpOnly` cookie (not localStorage — prevents XSS theft)
- Refresh token: `httpOnly` + `secure` + `SameSite=Strict` cookie
- Current `localStorage` token: deprecated, migration path provided

**Migration from v0.5:**
1. v1.0-alpha: Support both file token and JWT. File token gets `admin` role.
2. v1.0-beta: JWT required for new installations. File token emits warning.
3. v1.1: File token removed.

### Tenant Isolation

```python
@dataclass(frozen=True, slots=True)
class TenantContext:
    """Injected into every request via middleware."""
    tenant_id: TenantId
    user_id: UserId
    roles: frozenset[str]
    allowed_minds: frozenset[MindId]

    def assert_mind_access(self, mind_id: MindId) -> None:
        if mind_id not in self.allowed_minds:
            raise TenantIsolationError(
                f"User {self.user_id} cannot access mind {mind_id}"
            )

    def assert_role(self, role: str) -> None:
        if role not in self.roles:
            raise AuthorizationError(f"Required role: {role}")
```

**Database isolation strategy:**

Option A: **Schema-per-tenant** (recommended for ≤100 tenants)
```
~/.sovyx/data/
├── tenant-abc/
│   ├── sovyx.db
│   ├── brain.db
│   └── embeddings/
├── tenant-def/
│   ├── sovyx.db
│   └── ...
```

Option B: **Row-level isolation** (for >100 tenants)
```sql
-- Every table gets tenant_id column
CREATE TABLE conversations (
    id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    mind_id TEXT NOT NULL,
    ...
    CONSTRAINT fk_tenant FOREIGN KEY (tenant_id) REFERENCES tenants(id)
);

-- Every query filtered by tenant
SELECT * FROM conversations WHERE tenant_id = ? AND id = ?;
```

**Recommendation:** Start with Option A (simpler, stronger isolation,
no risk of cross-tenant leaks via query bugs). Migrate to Option B
only if tenant count exceeds 100 or cloud deployment requires it.

### WebSocket Multi-Tenant

```python
class TenantWebSocketManager:
    """Per-tenant WebSocket connection management."""

    def __init__(self) -> None:
        # tenant_id → set of connections
        self._connections: dict[TenantId, set[WebSocket]] = defaultdict(set)

    async def connect(self, ws: WebSocket, tenant: TenantContext) -> None:
        self._connections[tenant.tenant_id].add(ws)

    async def broadcast(self, tenant_id: TenantId, event: dict) -> None:
        """Broadcast only to connections belonging to this tenant."""
        dead: list[WebSocket] = []
        for ws in self._connections.get(tenant_id, set()):
            try:
                await ws.send_json(event)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self._connections[tenant_id].discard(ws)
```

### Rate Limiting

```python
class TenantRateLimiter:
    """Token bucket rate limiter per tenant."""

    def __init__(self, requests_per_minute: int = 60) -> None:
        self._buckets: dict[TenantId, _TokenBucket] = {}
        self._rpm = requests_per_minute

    async def check(self, tenant_id: TenantId) -> None:
        bucket = self._buckets.setdefault(
            tenant_id,
            _TokenBucket(capacity=self._rpm, refill_rate=self._rpm / 60.0),
        )
        if not bucket.consume():
            raise RateLimitError(f"Rate limit exceeded for tenant {tenant_id}")
```

**Rate limit tiers:**
| Tier | Chat RPM | API RPM | WS Connections | Minds |
|------|----------|---------|----------------|-------|
| Free | 10 | 30 | 1 | 1 |
| Pro | 60 | 300 | 5 | 5 |
| Enterprise | 300 | 1000 | 50 | Unlimited |

### Dashboard User Identity (replacing `_DASHBOARD_CHANNEL_USER_ID`)

```python
# v0.5 (current):
_DASHBOARD_CHANNEL_USER_ID = "dashboard-user"  # All sessions = same user

# v1.0:
async def handle_chat_message(
    registry: ServiceRegistry,
    message: str,
    tenant: TenantContext,  # From JWT middleware
    user_name: str | None = None,
) -> ChatResponse:
    person_id = await person_resolver.resolve(
        channel=ChannelType.DASHBOARD,
        channel_user_id=str(tenant.user_id),  # Real user identity
        display_name=user_name or tenant.user_id,
    )
    # ... rest of pipeline with tenant.assert_mind_access(mind_id)
```

---

## 5. Additional Gaps Discovered During Audit

### 5.1 Private Attribute Access — ✅ RESOLVED

> **All 5 SLF001 violations fixed in v0.5.1** (commits `7d16538`, `96830b2`).
> See §0.3 for full details, rationale, and property signatures.

| File | Access | Property Added | Status |
|------|--------|---------------|--------|
| `server.py` | `personality._config` | `PersonalityEngine.config` | ✅ Fixed |
| `scheduler.py` | `self._service._r2` | `CloudBackupService.r2` | ✅ Fixed |
| `scheduler.py` | `self._service._config` | `CloudBackupService.backup_config` | ✅ Fixed |
| `blue_green.py` | `self._migration_runner._version` | `MigrationRunner.schema_version` | ✅ Fixed |
| `schema.py` | `self._pool._db_path` | `DatabasePool.db_path` | ✅ Fixed |

### 5.2 Request ID Tracing — ✅ PARTIALLY RESOLVED

> **Middleware implemented in v0.5.1** (commit `96830b2`). See §0.5.
> Remaining: structlog integration for automatic log correlation.

**v0.5.1 (done):** `RequestIdMiddleware` injects `X-Request-Id` into every
request/response. Available via `request.state.request_id`.

**v1.0 remaining:** Integrate with structlog contextvars so every log line
in the request lifecycle automatically includes the request ID:

```python
# Add to RequestIdMiddleware.dispatch():
import structlog
structlog.contextvars.bind_contextvars(request_id=request_id)
```

This requires ensuring all log calls use structlog (already the case in
`src/sovyx/`) and that contextvars are cleared after each request (structlog
handles this automatically with async middleware).

### 5.3 No CSRF Protection

**Current:** API uses Bearer token (stateless, no cookies) — CSRF not applicable.

**v1.0 with cookies:** When JWT moves to httpOnly cookies, CSRF becomes critical.

**Solution:** Double-submit cookie pattern:
```python
# Server sets: Set-Cookie: csrf_token=<random>; SameSite=Strict
# Client sends: X-CSRF-Token: <same random> header
# Server validates header matches cookie
```

### 5.4 No Audit Log

**Multi-tenant requirement:** Every data-modifying operation must be logged.

```python
@dataclass
class AuditEntry:
    timestamp: datetime
    tenant_id: TenantId
    user_id: UserId
    action: str  # "chat.send", "config.update", "mind.create"
    resource_id: str
    ip_address: str
    user_agent: str
    result: Literal["success", "denied", "error"]
    metadata: dict[str, Any]
```

### 5.5 Secret Management

**Current:** Token in plaintext file (`~/.sovyx/token`, mode 0o600).

**v1.0:** For multi-tenant, need proper secret management:
- LLM API keys: encrypted at rest (AES-256-GCM, key from master password)
- JWT signing key: rotatable, stored in config with key ID
- Tenant API keys: hashed (bcrypt/argon2), never stored in plaintext
- Backup encryption keys: per-tenant, derived from tenant secret

---

## 6. Implementation Priority

### Phase 1: Security Hardening (v1.0-alpha)
**Estimated effort: 1 week** (reduced from 2 — 3 tasks completed in v0.5.1)

| Task | Priority | Effort | Status |
|------|----------|--------|--------|
| Error hierarchy (`SovyxError` tree) | P0 | 1 day | TODO |
| Narrow top-30 `except Exception` blocks | P0 | 2 days | TODO |
| WebSocket ticket-based auth | P0 | 1 day | TODO |
| Request ID middleware | P1 | 0.5 day | ✅ v0.5.1 |
| Fix `type: ignore` (categories B, C, E) | P1 | 1 day | ✅ v0.5.1 (9 of 9 fixable) |
| Unhandled exception middleware + metrics | P1 | 0.5 day | TODO |
| 5 remaining SLF001 → public properties | P2 | 0.5 day | ✅ v0.5.1 |
| structlog request ID integration | P2 | 0.5 day | TODO |
| `.pyi` stubs (onnxruntime, moonshine) | P2 | 1 day | TODO |

### Phase 2: Multi-Tenant Foundation (v1.0-beta)
**Estimated effort: 3 weeks**

| Task | Priority | Effort |
|------|----------|--------|
| JWT auth (issue, validate, refresh) | P0 | 3 days |
| TenantContext middleware | P0 | 1 day |
| Schema-per-tenant database | P0 | 3 days |
| Replace `_DASHBOARD_CHANNEL_USER_ID` | P0 | 1 day |
| Per-tenant WebSocket manager | P1 | 1 day |
| Rate limiter (token bucket) | P1 | 1 day |
| CSRF protection (double-submit cookie) | P1 | 0.5 day |
| Audit log | P1 | 2 days |
| httpOnly cookie token storage | P2 | 1 day |

### Phase 3: Production Readiness (v1.0-rc)
**Estimated effort: 2 weeks**

| Task | Priority | Effort |
|------|----------|--------|
| Moonshine voice stubs (`.pyi`) | P2 | 1 day |
| Secret encryption at rest | P1 | 2 days |
| CLI error path integration tests | P2 | 1 day |
| Penetration test (full OWASP Top 10) | P0 | 3 days |
| Load test (multi-tenant, 100 concurrent) | P0 | 2 days |
| Security documentation for operators | P1 | 1 day |

---

## Appendix A: Files Modified Per Phase

### Phase 1 (Security Hardening)
```
DONE: src/sovyx/dashboard/server.py       — Request ID middleware, version fix, error leak fix ✅ v0.5.1
DONE: src/sovyx/brain/retrieval.py        — Fix type: ignore (lambda key) ✅ v0.5.1
DONE: src/sovyx/brain/working_memory.py   — Fix type: ignore (lambda key) ✅ v0.5.1
DONE: src/sovyx/engine/registry.py        — Fix type: ignore (cast generic) ✅ v0.5.1
DONE: src/sovyx/dashboard/settings.py     — Fix type: ignore (cast Any) ✅ v0.5.1
DONE: src/sovyx/dashboard/config.py       — Fix type: ignore (cast Any) ✅ v0.5.1
DONE: src/sovyx/context/formatter.py      — Fix type: ignore (timezone union) ✅ v0.5.1
DONE: src/sovyx/cloud/backup.py           — Fix type: ignore + add properties ✅ v0.5.1
DONE: src/sovyx/mind/personality.py       — Add .config property ✅ v0.5.1
DONE: src/sovyx/persistence/pool.py       — Add .db_path property ✅ v0.5.1
DONE: src/sovyx/upgrade/schema.py         — Add .schema_version property ✅ v0.5.1
DONE: src/sovyx/bridge/manager.py         — Add .mind_id property ✅ v0.5.1

TODO: src/sovyx/engine/errors.py          — Error hierarchy (NEW)
TODO: src/sovyx/dashboard/server.py       — WS ticket, exception middleware
TODO: src/sovyx/dashboard/chat.py         — Narrow exceptions
TODO: src/sovyx/dashboard/brain.py        — Narrow exceptions (6 blocks)
TODO: src/sovyx/dashboard/conversations.py — Narrow exceptions (6 blocks)
TODO: src/sovyx/dashboard/status.py       — Narrow exceptions (3 blocks)
TODO: src/sovyx/cognitive/gate.py         — Layered exception handling
TODO: src/sovyx/cognitive/loop.py         — Layered exception handling
TODO: dashboard/src/hooks/use-websocket.ts — Ticket-based WS auth
TODO: stubs/onnxruntime/__init__.pyi      — Type stubs (NEW)
TODO: stubs/moonshine_voice/__init__.pyi  — Type stubs (NEW)
```

### Phase 2 (Multi-Tenant)
```
NEW:  src/sovyx/auth/jwt.py               — JWT issue/validate/refresh
NEW:  src/sovyx/auth/middleware.py         — TenantContext extraction
NEW:  src/sovyx/auth/csrf.py              — CSRF protection
NEW:  src/sovyx/auth/rate_limit.py        — Token bucket per tenant
NEW:  src/sovyx/auth/audit.py             — Audit logging
EDIT: src/sovyx/dashboard/server.py       — JWT middleware, per-tenant WS
EDIT: src/sovyx/dashboard/chat.py         — TenantContext integration
EDIT: src/sovyx/persistence/pool.py       — Schema-per-tenant routing
EDIT: src/sovyx/engine/config.py          — Per-tenant config
```

---

*Document version: 1.1 — 2026-04-08*
*Author: Nyx (deep-dive audit of Sovyx v0.5.1)*
*Codebase: 25,192 LOC Python, 4,396 tests, 98% dashboard coverage*
*v1.1 update: §0 added (completed fixes), §2/§5/§6 updated with post-fix state*
