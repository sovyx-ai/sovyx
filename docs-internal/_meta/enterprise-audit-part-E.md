# Enterprise Audit — Part E (dashboard, benchmarks)

Scope: `src/sovyx/dashboard/` (17 files, 5706 LOC) + `src/sovyx/benchmarks/` (3 files, 483 LOC).
Scoring: 10 criteria, 0 or 1 each. 8-10 = ENTERPRISE, 5-7 = DEVELOPED, 0-4 = NOT-ENT.

## Summary
| Module | Files | Avg | ENTERPRISE | DEVELOPED | NOT-ENT |
|---|---|---|---|---|---|
| dashboard | 17 | 8.6 | 14 | 3 | 0 |
| benchmarks | 3 | 9.3 | 3 | 0 | 0 |
| **TOTAL** | **20** | **8.7** | **17** | **3** | **0** |

Overall classification: **ENTERPRISE**.

## dashboard (17 files)

### File: `__init__.py` — 10/10 — ENTERPRISE
Trivial constant. All criteria vacuously satisfied.

### File: `_shared.py` — 9/10 — ENTERPRISE
Failed: 9 (no external to wrap — N/A-ish but scored 1).
Minor: uses broad `except Exception` — acceptable for lazy registry lookup with debug log.

### File: `server.py` (2134 LOC) — 7/10 — DEVELOPED
Failed: 1 (ERROR HANDLING — 25+ `except Exception: # noqa: BLE001` sites swallow specifics), 10 (CODE QUALITY — god file, SRP heavily violated), 9 (RESILIENCE — Telegram `getMe` has timeout=10s, good; but provider validation lacks retry).
Strengths: Bearer auth with `secrets.compare_digest` (timing-safe), `HTTPBearer(auto_error=False)`, CSP/X-Frame/Permissions-Policy headers, SPA fallback has path-traversal guard (`..` check + `startswith(static_root)`), import endpoint validates `multipart/form-data`, `/api/export` and `/api/chat` rate-limited via middleware, WebSocket auth via `compare_digest` on query token, graceful shutdown via `should_exit`.
Failures/concerns:
- **God file:** `create_app()` inlines ~40 endpoints (1600 LOC function). Each endpoint should live in its submodule's router. Current layout delegates data fns to submodules but routing stays here — no `APIRouter` factoring.
- **`_server_token` global:** mutated by `create_app()` via `global` — fragile for concurrent test/app creation. CLAUDE.md anti-pattern #10 acknowledges this and pushes `token=` param; production still mutates a module global.
- **Error handling shotgun:** nearly every registry lookup is wrapped in `except Exception: logger.debug(...)` — hides real bugs, no `context` dict per `engine/errors.py` convention.
- **Ollama `ping()` on every `/api/providers` GET:** no caching, blocks the request. Expensive path without ETag/cache.
- **`/api/safety/status` reaches into `bridge._adapters` (private):** encapsulation breach — same for `router._providers`, `get_injection_tracker()._conversations`, `scheduler._running`, `brain._embedding._loaded`, `guard._daily_budget`, `_sources.__len__()`. At least 8 private-attribute reads.
- **Import endpoint reads entire upload into memory** (`await upload.read()` → `tmp_path.write_bytes(data)`) — no streaming, no max-size guard. A 10GB upload will OOM the daemon. No content-length validation.
- **Path traversal in import:** `tmp_path` is `mkdtemp`'d (safe) but the archive's own contents are trusted to the `MindImporter` — auditing would need to check `upload/importer.py`, out of scope here but flagged.
- **Input validation:** settings/config/chat/providers PUT all use `await request.json()` + `isinstance(body, dict)` checks instead of pydantic models. Untyped dicts flow through. Enterprise API boundaries should be pydantic `BaseModel` request schemas with `response_model=` decl.

### File: `rate_limit.py` — 9/10 — ENTERPRISE
Failed: 10 (uses two module-level globals `_buckets`, `_last_cleanup` — acceptable for per-process limiter but untestable in isolation).
Strengths: sliding window under `Lock`, per-endpoint overrides for `/api/chat` (20/min) and `/api/export` (5/min), X-RateLimit-* headers, Retry-After on 429, stale bucket pruning every 5min, respects `X-Forwarded-For`.
Concerns:
- **Bucket key is `ip:path` with raw path** — `GET /api/conversations/abc123` and `GET /api/conversations/def456` bucket separately. Per-conversation ID creates unbounded bucket growth. Should normalize to route template or bucket per method+prefix.
- **No limiter on WebSocket `/ws`** (skipped explicitly) — a malicious client can spam reconnects; connect rate needs its own guard.
- Chat/export/import limits enumerated in a dict — fine; but no config override via `EngineConfig`.

### File: `events.py` — 10/10 — ENTERPRISE
Failed: none.
`ConnectionManager.broadcast()` copies snapshot under lock then releases before sending — slow consumers cannot block other sends or connect/disconnect. Stale sockets are pruned after the send loop. Early-out when `active_count == 0` avoids wasted serialization. Name-based event dispatch with per-branch `cast()` (xdist-safe per CLAUDE.md #8).

### File: `chat.py` — 8/10 — ENTERPRISE
Failed: 2 (validation done with `isinstance` in server.py, not pydantic), 9 (no retry on timeout — just propagates `CognitiveError`).
Strengths: `_DEFAULT_TIMEOUT=30.0s` on cognitive submission, raises `ValueError` on empty msg, `financial_callback` has explicit prefix allowlist (`fin_confirm:/fin_cancel:`), no arbitrary code eval.
Concerns:
- **No max message length:** `message_text` accepted up to whatever body-size Starlette tolerates. A 10MB chat message will flow into perception + LLM context. Rate limiter (20/min) mitigates but one message is enough to blow cost/context.
- `_chat_pending_confirmations` is a module-level dict — unbounded growth if confirmations never resolve. Needs TTL eviction.

### File: `export_import.py` — 8/10 — ENTERPRISE
Failed: 2 (no pydantic model for `overwrite` flag), 9 (no upload size cap — enforced in server.py, and absent there too).
Export uses `tempfile.NamedTemporaryFile(delete=False)` — caller cleans up. Import uses `mkdtemp` + `shutil.rmtree(..., ignore_errors=True)` in `finally`. Path traversal not possible at this layer (archive extracted by `MindImporter`).

### File: `logs.py` — 9/10 — ENTERPRISE
Failed: 10 (`time.sleep(0.1)` in async-reachable code — blocks event loop; should be `await asyncio.sleep`).
Strengths: seek-from-end for files >1MB, rotation-resilient (retries + `.1` backup), JSON parse with fallback, incremental `after` cursor via ISO-8601 lex compare. Normalizes legacy field names (`ts→timestamp`, `severity→level`) per anti-pattern #7.

### File: `activity.py` — 10/10 — ENTERPRISE
Safe SQL with parameter binding, graceful degradation when tables missing (`consolidation_log`), JSON metadata parsed with try/except narrow to `(ValueError, TypeError)`.

### File: `brain.py` — 10/10 — ENTERPRISE
Batched SQL with chunk_size=450 (avoids SQLite 999-param limit), bidirectional relation query, orphan rescue guarantees connectivity, hybrid FTS5+vector search with score normalization and dedup.

### File: `conversations.py` — 9/10 — ENTERPRISE
Failed: 10 (`# noqa: S608 # nosec B608` suppresses bandit — placeholders ARE safe here, but triple suppression is excessive).
SQL uses `?` placeholders; `list_conversations` resolves person IDs in one batch query.

### File: `daily_stats.py` — 10/10 — ENTERPRISE
`INSERT OR REPLACE` idempotent, LIKE-prefix for month queries, COALESCE for null safety, JSON-safe provider/model breakdowns.

### File: `settings.py` — 9/10 — ENTERPRISE
Failed: 2 (manual `isinstance`/`str().upper()` validation instead of pydantic).
Mutable field allowlist via dict dispatch — good. YAML persisted with `yaml.safe_load` (not `load`).

### File: `config.py` — 8/10 — ENTERPRISE
Failed: 2 (untyped `dict[str, Any]` updates, manual isinstance guards), 10 (ad-hoc `cast("Any", ...)` to bypass type system).
Strengths: field-level allowlist via `_MUTABLE_SECTIONS` frozenset, child-safe coherence enforcement (auto-strict + PII + financial), builtin guardrail ID protection, structured logs on every change.

### File: `status.py` — 10/10 — ENTERPRISE
Thread-safe counters with `threading.Lock`, day-boundary reset buffers previous day for async flush, persistence mirrors CostGuard pattern (dirty flag + engine_state table), graceful timezone fallback.

### File: `voice_status.py` — 9/10 — ENTERPRISE
Failed: 10 (repetitive try/except wrapper per service; could factor into a helper).
Per-service resolution with graceful fallback, `type(x).__name__` not `isinstance` (xdist-safe #8).

### File: `plugins.py` — 10/10 — ENTERPRISE
Pure data functions, no globals, complete manifest serialization, permission risk enrichment.

## benchmarks (3 files)

### File: `__init__.py` — 10/10 — ENTERPRISE
Clean re-export with `__all__`.

### File: `budgets.py` — 10/10 — ENTERPRISE
`StrEnum` for `HardwareTier` (anti-pattern #9 compliant), `frozen=True, slots=True` dataclasses, immutable tier limits, mapping-based check dispatch, no JSON handling so no traversal vectors.

### File: `baseline.py` — 8/10 — ENTERPRISE
Failed: 2 (no pydantic — raw `dict.get("name")` / `float(m["value"])` when loading), 9 (no file locking — concurrent benchmark runs racing on `latest.json` would corrupt).
Strengths: `RegressionDetected` typed exception, path-traversal NOT a concern (paths constructed from timestamp + fixed dir, no user input), JSON `json.loads` with narrow `(JSONDecodeError, OSError)` except, 10% tolerance configurable per instance, `higher_is_better` set for throughput metrics. `compare()` with missing baseline auto-saves current — convenient but could hide first-run regressions.

## Top issues across E

1. **`server.py` is a god file** (2134 LOC, ~40 endpoints inline in `create_app()`). Should be refactored to `APIRouter` per domain (status, brain, chat, plugins, safety, providers, channels, voice). No technical debt acknowledgement in code.
2. **No pydantic request/response models on mutating endpoints.** Settings, config, chat, providers, safety/rules, telegram/setup, custom-rules all accept `dict[str, Any]` with manual `isinstance` checks. Violates CLAUDE.md convention "All config via EngineConfig (pydantic-settings)" spirit at API boundary.
3. **Import endpoint loads entire upload into RAM** with no size cap. DoS vector — a 10GB `.sovyx-mind` POST will crash the daemon. Needs streaming upload + max-content-length middleware.
4. **Chat endpoint has no max message length.** Rate limiter slows abuse but one 10MB payload burns context/cost. Add `max_length` validator.
5. **Pervasive `except Exception: # noqa: BLE001` with debug-only logging** across server.py, status.py, voice_status.py. Hides real bugs in registry wiring. Should catch `ServiceNotRegisteredError` specifically per `engine/errors.py`.
6. **Private-attribute reads** (`router._providers`, `bridge._adapters`, `brain._embedding._loaded`, `guard._daily_budget`, `scheduler._running`, `get_injection_tracker()._conversations`, `counters._tz`, `_sources.__len__()`): 10+ sites. Couples dashboard tightly to internal impl; any refactor of those services breaks it.
7. **Rate limiter bucket key uses raw path** — path-parameter endpoints (`/api/conversations/{id}`, `/api/plugins/{name}`) create unbounded buckets. Normalize to route template.
8. **`/ws` WebSocket has no connect rate limit** — reconnect spam bypasses the middleware (explicitly skipped for upgrade headers).
9. **`time.sleep(0.1)` in `logs.py::_tail_lines`** inside an async-callable code path — blocks event loop during rotation race.
10. **Global mutable state:** `_server_token`, `_buckets`, `_last_cleanup`, `_chat_pending_confirmations`, `_counters` module singleton. Anti-pattern #10 documented escape hatch only for token; others unaddressed.
11. **Baseline.py: no file locking on `latest.json`** — parallel benchmark runs can corrupt.
12. **CSP permits `'unsafe-inline'` for styles** (Tailwind). Acceptable for internal dashboard; document threat model.

Verdict: the module is well-built (auth, rate limit, security headers, CORS scoped, CSP, WebSocket slow-consumer guard, graceful degradation everywhere) but **`server.py` needs decomposition** and **pydantic request models need to land on all PUT/POST endpoints** before this qualifies as fully enterprise-grade at the API layer. Benchmarks module is clean and mature.
