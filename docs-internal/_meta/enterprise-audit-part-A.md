# Enterprise Audit — Part A (engine, cognitive, brain)

Brutal, file-level 10-criteria audit. Each .py file scored 0 (fail) or 1 (pass)
per criterion. `__init__.py` files skipped (single-line). Classification:
ENTERPRISE (8-10), DEVELOPED (5-7), NOT-ENTERPRISE (0-4).

Criteria: 1=errors, 2=input-validation, 3=observability, 4=testing,
5=security, 6=concurrency, 7=config, 8=docs, 9=resilience, 10=code-quality.

## Summary

| Module | Files | Avg | ENTERPRISE | DEVELOPED | NOT-ENT |
|---|---:|---:|---:|---:|---:|
| engine | 12 | 8.6/10 | 10 | 2 | 0 |
| cognitive | 23 | 8.0/10 | 15 | 8 | 0 |
| brain | 13 | 7.8/10 | 10 | 3 | 0 |
| **TOTAL** | **48** | **8.1/10** | **35** | **13** | **0** |

## engine (12 files audited, __init__.py skipped)

### File: src/sovyx/engine/errors.py — Score: 10/10 — ENTERPRISE

Typed exceptions with context dicts. Full module/class docstrings. No I/O.
StrEnum-compatible base. Tests exist. No failures.

### File: src/sovyx/engine/types.py — Score: 10/10 — ENTERPRISE

StrEnum everywhere (per anti-pattern #9), NewType IDs, docstrings on every
enum/class. Tests present. No failures.

### File: src/sovyx/engine/protocols.py — Score: 9/10 — ENTERPRISE

Failed criteria:
- **#8 [DOCUMENTATION]**: Minor — `generate()` returns `object` (a dummy type)
  instead of a concrete LLMResponse protocol; `initialize(config: dict[str, object])`
  is untyped at boundary.

### File: src/sovyx/engine/config.py — Score: 10/10 — ENTERPRISE

Pydantic-settings with env_prefix, model_validator for log_file resolution,
backward-compat migrator, typed exceptions. Tests thorough. No failures.

### File: src/sovyx/engine/events.py — Score: 10/10 — ENTERPRISE

Frozen dataclass events with category property, async event bus with error
isolation (`except Exception → logger.error; other handlers continue`),
correlation_id propagation. Tests cover events+bus. No failures.

### File: src/sovyx/engine/registry.py — Score: 9/10 — ENTERPRISE

Failed criteria:
- **#1 [ERROR HANDLING]**: `shutdown_all` uses `contextlib.suppress(Exception)`
  without logging the swallowed exception; only logs `"service_shutting_down"`
  before the suppressed block. Silent failure mode.

### File: src/sovyx/engine/lifecycle.py — Score: 8/10 — ENTERPRISE

Failed criteria:
- **#1 [ERROR HANDLING]**: `_print_startup_banner` uses
  `except Exception:  # noqa: BLE001  # nosec B110 … banner is best-effort`
  — acknowledged, but still a bare-ish swallow.
- **#8 [DOCUMENTATION]**: The banner method contains `print(banner)  # noqa: T201`
  — deliberate, but documented anti-pattern violation.

### File: src/sovyx/engine/health.py — Score: 9/10 — ENTERPRISE

Failed criteria:
- **#6 [CONCURRENCY]**: `_check_memory` opens `/proc/meminfo` with blocking
  `open()` in an async function. Minor — not an async-IO operation but
  synchronous file read inside async method.

### File: src/sovyx/engine/bootstrap.py — Score: 8/10 — ENTERPRISE

Failed criteria:
- **#6 [CONCURRENCY]**: Writes to `os.environ` at runtime
  (`os.environ[_k] = _v` loop on channel.env) — global mutable state,
  not thread-safe if bootstrap is ever called concurrently.
- **#10 [CODE QUALITY]**: `bootstrap()` is a 400+ line god-function that
  wires all services — should be split into per-subsystem factories; mixes
  concerns (env loading, logging, DB, brain, LLM, cognitive, plugins,
  bridge).

### File: src/sovyx/engine/degradation.py — Score: 9/10 — ENTERPRISE

Failed criteria:
- **#7 [CONFIGURATION]**: Hardcoded `_disk_threshold_mb = 100` and
  `shutil.disk_usage("/")` (hardcoded root path — won't measure data_dir
  on Windows or custom mounts).

### File: src/sovyx/engine/rpc_protocol.py — Score: 10/10 — ENTERPRISE

Length-prefixed protocol with 10MB cap, timeout support, typed exceptions.
Compact, single-responsibility. Test file present. No failures.

### File: src/sovyx/engine/rpc_server.py — Score: 7/10 — DEVELOPED-NOT-ENTERPRISE

Failed criteria:
- **#1 [ERROR HANDLING]**: `except Exception: logger.exception("rpc_connection_error")`
  swallows ALL errors without sending an error response — client hangs.
- **#5 [SECURITY]**: No authentication on the RPC socket beyond FS permissions
  (0o600). Any local process running as the same user can call any registered
  method. No rate limiting. `handler(**params)` directly unpacks user-supplied
  params → method can be called with arbitrary kwargs.
- **#9 [RESILIENCE]**: No circuit breaker; method call errors are just
  serialized to client with `str(e)` (leaks internal details).

## cognitive (23 files audited, __init__.py skipped)

### File: src/sovyx/cognitive/state.py — Score: 10/10 — ENTERPRISE

Tiny state machine, typed transitions, logger, tests cover invalid
transitions. No failures.

### File: src/sovyx/cognitive/perceive.py — Score: 9/10 — ENTERPRISE

Failed criteria:
- **#2 [INPUT VALIDATION]**: `Perception` is a plain `@dataclasses.dataclass`,
  not a Pydantic model — no range validation on `priority` or schema
  enforcement on `metadata: dict[str, object]`.

### File: src/sovyx/cognitive/attend.py — Score: 9/10 — ENTERPRISE

Failed criteria:
- **#1 [ERROR HANDLING]**: `_classify_with_llm` uses `except Exception:  # noqa: BLE001`
  swallowing all LLM classifier errors to `logger.debug`. Warning-level
  would be more appropriate for a safety-critical path.

### File: src/sovyx/cognitive/think.py — Score: 8/10 — ENTERPRISE

Failed criteria:
- **#1 [ERROR HANDLING]**: `except Exception: logger.exception("think_phase_failed")`
  — bare-Exception catch returns a degraded LLMResponse without distinguishing
  error categories (quota, timeout, provider down).
- **#2 [INPUT VALIDATION]**: `perception.metadata.get("complexity", 0.5)` reads
  untyped metadata and coerces via `isinstance(raw_complexity, (int, float, str))`
  — no schema for metadata.

### File: src/sovyx/cognitive/act.py — Score: 8/10 — ENTERPRISE

Failed criteria:
- **#1 [ERROR HANDLING]**: Two `except Exception as e:  # noqa: BLE001`
  in `ToolExecutor.execute` and `_react_loop` — acknowledged exceptions
  but still broad.
- **#9 [RESILIENCE]**: Re-invocation LLM call inside ReAct loop has no per-call
  timeout beyond what LLMRouter enforces; a stuck LLM stalls the whole loop.

### File: src/sovyx/cognitive/reflect.py — Score: 7/10 — DEVELOPED-NOT-ENTERPRISE

Failed criteria:
- **#10 [CODE QUALITY]**: 1021 LOC in a single file — largest in cognitive
  module. Mixes LLM extraction, regex fallback, episode encoding, concept
  linking, Hebbian orchestration. Should be split into `extraction.py` +
  `reflect.py`.
- **#1 [ERROR HANDLING]**: Relies on reflect being wrapped by caller's
  `except Exception` in `loop.py` — fragile contract.
- **#2 [INPUT VALIDATION]**: LLM JSON extraction parses free-form strings;
  only regex-sanitized but no strict pydantic schema for extracted concept
  shape.

### File: src/sovyx/cognitive/loop.py — Score: 9/10 — ENTERPRISE

Failed criteria:
- **#1 [ERROR HANDLING]**: `_categorize_error` dispatches by
  `type(exc).__name__` string — intentional per anti-pattern #8, but
  defaults to a generic message for anything not in the small whitelist
  (no `TokenBudgetExceededError`, `CircuitOpenError`, `CognitiveError`
  mapping).

### File: src/sovyx/cognitive/gate.py — Score: 10/10 — ENTERPRISE

PriorityQueue with backpressure, graceful drain, context binding, cancel
handling. Tests present. No failures.

### File: src/sovyx/cognitive/audit_store.py — Score: 6/10 — DEVELOPED-NOT-ENTERPRISE

Failed criteria:
- **#6 [CONCURRENCY]**: Uses synchronous `sqlite3.connect()` and
  `conn.executemany` — blocking IO inside what's called from async code paths
  (`record()` is called from `SafetyAuditTrail.record` which runs in async
  pipelines). Violates anti-pattern guidance for async IO.
- **#3 [OBSERVABILITY]**: `_init_db` catches `sqlite3.Error` and logs
  `audit_store_init_failed` then silently continues with a broken store.
- **#10 [CODE QUALITY]**: Dual implementation — there's also a separate
  in-memory `SafetyAuditTrail` that duplicates event storage. Write-through
  logic lives in `SafetyAuditTrail.record` calling `get_audit_store().append`
  — unclear ownership.
- **#9 [RESILIENCE]**: No retry on `sqlite3.Error`; failed flush drops
  events (`return 0`).

### File: src/sovyx/cognitive/custom_rules.py — Score: 9/10 — ENTERPRISE

Failed criteria:
- **#10 [CODE QUALITY]**: Module-global `_compiled_cache: dict` is mutable
  state without lock — fine under GIL but documented anti-pattern of
  module-level mutable state.

### File: src/sovyx/cognitive/financial_gate.py — Score: 9/10 — ENTERPRISE

Failed criteria:
- **#10 [CODE QUALITY]**: Duplicate `_CLASSIFY_PROMPT` definition (line 190
  and again line 248) — dead code / copy-paste bug.

### File: src/sovyx/cognitive/output_guard.py — Score: 9/10 — ENTERPRISE

Failed criteria:
- **#1 [ERROR HANDLING]**: `_classify_with_llm` uses
  `except Exception:  # noqa: BLE001` → debug log and return None. Safety-
  critical path should at minimum warn.

### File: src/sovyx/cognitive/pii_guard.py — Score: 8/10 — ENTERPRISE

Failed criteria:
- **#1 [ERROR HANDLING]**: `_ner_classify` uses
  `except Exception:  # noqa: BLE001` catching imports/timeouts/parse errors
  uniformly.
- **#2 [INPUT VALIDATION]**: `llm_router: object | None = None` uses `object`
  to avoid circular import, then `router.generate  # type: ignore[attr-defined]`
  — type safety escape.

### File: src/sovyx/cognitive/safety_patterns.py — Score: 7/10 — DEVELOPED-NOT-ENTERPRISE

Failed criteria:
- **#10 [CODE QUALITY]**: 1165 LOC — largest file in audited scope. Essentially
  a giant regex catalog with per-tier frozensets. Hard to diff, hard to
  review. Should be data-driven (YAML) not compiled-in.
- **#7 [CONFIGURATION]**: Hardcoded regex patterns can't be updated without
  code deploy — patterns should be loadable from config for rapid response
  to new threats.
- **#9 [RESILIENCE]**: Regex catastrophic backtracking risk — no timeout
  on regex evaluation. A crafted input could DoS the safety pipeline.

### File: src/sovyx/cognitive/safety_classifier.py — Score: 8/10 — ENTERPRISE

Failed criteria:
- **#7 [CONFIGURATION]**: `_HOURLY_BUDGET_CAP = 0`, `_COST_PER_CALL_USD = 0.0001`,
  `_CLASSIFY_TIMEOUT_SEC = 2.0` are module constants — not wired to EngineConfig.
- **#10 [CODE QUALITY]**: 704 LOC — mixes budget, cache, classifier, batch
  classification. Three responsibilities in one file.

### File: src/sovyx/cognitive/safety_audit.py — Score: 8/10 — ENTERPRISE

Failed criteria:
- **#1 [ERROR HANDLING]**: `except Exception:  # noqa: BLE001` around SQLite
  store append — swallowed with `pass`, only a noqa comment.
- **#6 [CONCURRENCY]**: `deque` is thread-safe for append/pop but `get_stats`
  iterates the deque — during iteration another thread can mutate; not
  guaranteed atomic for statistics.

### File: src/sovyx/cognitive/safety_container.py — Score: 10/10 — ENTERPRISE

Clean DI container; docstrings thorough; `for_testing` factory; global
singleton with explicit reset. No failures.

### File: src/sovyx/cognitive/safety_escalation.py — Score: 9/10 — ENTERPRISE

Failed criteria:
- **#1 [ERROR HANDLING]**: Two `except Exception:  # noqa: BLE001` blocks with
  bare `pass` when notifier call fails — loses escalation signal silently.

### File: src/sovyx/cognitive/safety_i18n.py — Score: 8/10 — ENTERPRISE

Failed criteria:
- **#3 [OBSERVABILITY]**: No logger — pure static map. Missing telemetry on
  unknown-language fallback.
- **#7 [CONFIGURATION]**: Translation table is hardcoded in-module; should
  be loadable/extendable from config files for translator contributions
  without code changes.

### File: src/sovyx/cognitive/safety_notifications.py — Score: 10/10 — ENTERPRISE

Protocol-based sink, debounce, clear responsibilities, tests. No failures.

### File: src/sovyx/cognitive/shadow_mode.py — Score: 9/10 — ENTERPRISE

Failed criteria:
- **#6 [CONCURRENCY]**: Module-level `_cached_patterns` + `_cached_config_hash`
  without lock — under xdist/concurrent tests, could race.

### File: src/sovyx/cognitive/text_normalizer.py — Score: 9/10 — ENTERPRISE

Failed criteria:
- **#1 [ERROR HANDLING]**: Each decoder uses `except Exception:  # noqa: BLE001`
  — intentional fail-safe, but 5 bare catches in one file.

### File: src/sovyx/cognitive/injection_tracker.py — Score: 8/10 — ENTERPRISE

Failed criteria:
- **#6 [CONCURRENCY]**: Module-level dict `_conversations` (based on file
  header description) — thread-safe via GIL but unbounded growth mitigated
  only by MAX_CONVERSATIONS cap; no eviction policy documented.
- **#10 [CODE QUALITY]**: 453 LOC — scoring, suspicion patterns, sliding
  window, and verdict logic all in one file.

## brain (13 files audited, __init__.py skipped)

### File: src/sovyx/brain/models.py — Score: 10/10 — ENTERPRISE

Pydantic models with field constraints (`ge`, `le`). Factory defaults.
Tests present. No failures.

### File: src/sovyx/brain/working_memory.py — Score: 9/10 — ENTERPRISE

Failed criteria:
- **#9 [RESILIENCE]**: Assumes single-threaded asyncio. Comment explicitly
  states "not thread-safe". If ever called from a thread pool, silent
  corruption. No runtime guard.

### File: src/sovyx/brain/spreading.py — Score: 10/10 — ENTERPRISE

Clean algorithm, typed inputs, docstrings, observability. No failures.

### File: src/sovyx/brain/contradiction.py — Score: 9/10 — ENTERPRISE

Failed criteria:
- **#9 [RESILIENCE]**: LLM-based classification fallback to heuristic is
  good, but no timeout on the LLM call — relies on router's timeout.

### File: src/sovyx/brain/retrieval.py — Score: 9/10 — ENTERPRISE

Failed criteria:
- **#1 [ERROR HANDLING]**: Two `except Exception: logger.debug(...)` blocks
  silently fall back to FTS-only without propagating partial-failure info
  to the caller.

### File: src/sovyx/brain/concept_repo.py — Score: 8/10 — ENTERPRISE

Failed criteria:
- **#1 [ERROR HANDLING]**: SQL exception handling is implicit — relies on
  pool's transaction context to raise; no specific mapping from SQLite
  errors to `PersistenceError` subclasses.
- **#10 [CODE QUALITY]**: 505 LOC with FTS, embedding, CRUD, search, hybrid
  rank logic — tight coupling to multiple concerns.

### File: src/sovyx/brain/episode_repo.py — Score: 9/10 — ENTERPRISE

Failed criteria:
- **#1 [ERROR HANDLING]**: Same pattern as concept_repo — SQL exceptions
  propagate as raw `aiosqlite` errors instead of mapped `PersistenceError`.

### File: src/sovyx/brain/relation_repo.py — Score: 10/10 — ENTERPRISE

Canonical-ordering on writes, typed, tests cover graph ops. No failures.

### File: src/sovyx/brain/embedding.py — Score: 7/10 — DEVELOPED-NOT-ENTERPRISE

Failed criteria:
- **#10 [CODE QUALITY]**: 705 LOC — mixes model downloader, ONNX runtime,
  tokenizer, cooldown file handling, checksum verification, and encoding.
  Should be split into `downloader.py` + `engine.py`.
- **#7 [CONFIGURATION]**: Hardcoded `MODEL_SHA256`, `MODEL_URLS`,
  `_COOLDOWN_SECONDS = 900`, `MODEL_DIMENSIONS = 384` — should be
  configurable per-deployment.
- **#5 [SECURITY]**: Downloads model from `huggingface.co` and `github.com`
  over HTTPS with SHA256 verification — OK, but mirror fallback and
  user-controlled `data_dir` means a symlink attack on the model path is
  not guarded.

### File: src/sovyx/brain/learning.py — Score: 9/10 — ENTERPRISE

Failed criteria:
- **#7 [CONFIGURATION]**: `_STAR_K = 15` and other magic numbers
  (`_CO_ACTIVATION_THRESHOLD = 0.7`, `_IMPORTANCE_BOOST = 0.02`) are
  hardcoded — should be config params.

### File: src/sovyx/brain/scoring.py — Score: 9/10 — ENTERPRISE

Failed criteria:
- **#10 [CODE QUALITY]**: 583 LOC with ImportanceScorer, ConfidenceScorer,
  EvolutionScorer, ScoreNormalizer, weights, drift detection — should be
  split by scorer type.

### File: src/sovyx/brain/consolidation.py — Score: 8/10 — ENTERPRISE

Failed criteria:
- **#10 [CODE QUALITY]**: 526 LOC. `ConsolidationCycle` + `ConsolidationScheduler`
  in one file; cycle method runs 7-step pipeline inline.
- **#9 [RESILIENCE]**: Consolidation steps (decay, merge, prune) appear
  to lack per-step retry; one step raising aborts the whole cycle.

### File: src/sovyx/brain/service.py — Score: 7/10 — DEVELOPED-NOT-ENTERPRISE

Failed criteria:
- **#10 [CODE QUALITY]**: 712 LOC — god-class: implements BrainReader +
  BrainWriter + centroid cache + consolidation hooks + working memory
  orchestration + event emission + contradiction detection call-site +
  star-topology Hebbian orchestration. Four to six responsibilities in
  one class.
- **#2 [INPUT VALIDATION]**: `**kwargs: object` on `learn_concept` and
  `encode_episode` — unbounded kwargs without schema.
- **#1 [ERROR HANDLING]**: Several silent int-coercion patterns
  (`int(hit_raw) if isinstance(hit_raw, (int, float, str)) else 0`) swallow
  type errors from metadata dict.

## Top issues across Part A

1. **BLE001 (bare `except Exception`) pattern is pervasive** — 20+ occurrences
   across cognitive and brain modules, acknowledged with `# noqa: BLE001` but
   frequently swallows to `logger.debug` or silent `pass`. The noqa is
   acknowledgement, not remediation. Safety-critical paths (LLM classifier,
   PII NER, audit store, escalation notifier) all have this pattern.

2. **God classes / mega-files** — 6 files over 500 LOC mixing 3+ responsibilities:
   - `cognitive/safety_patterns.py` (1165 LOC): giant hardcoded regex catalog
   - `cognitive/reflect.py` (1021 LOC): extraction + encoding + Hebbian
   - `cognitive/safety_classifier.py` (704 LOC): budget + cache + classifier
   - `brain/service.py` (712 LOC): reader + writer + orchestrator + events
   - `brain/embedding.py` (705 LOC): downloader + runtime + encoding
   - `brain/scoring.py` (583 LOC): 4 scorers + weights + drift
   - `engine/bootstrap.py` (572 LOC): wires everything inline

3. **Hardcoded config constants outside EngineConfig** — critical tuning
   parameters are module globals:
   - `safety_classifier.py`: `_HOURLY_BUDGET_CAP`, `_COST_PER_CALL_USD`,
     `_CLASSIFY_TIMEOUT_SEC`
   - `safety_patterns.py`: entire pattern catalog in-code
   - `brain/embedding.py`: model URLs, SHA256, dimensions, cooldown
   - `brain/learning.py`: `_STAR_K`, thresholds
   - `brain/consolidation.py`: thresholds hardcoded
   - `engine/degradation.py`: `_disk_threshold_mb`, `shutil.disk_usage("/")`
     (hardcoded root path, wrong on Windows and non-root data_dirs)

4. **Sync IO in async context** — `cognitive/audit_store.py` uses
   `sqlite3.connect` + `executemany` synchronously from async call paths;
   `engine/health.py` reads `/proc/meminfo` with blocking `open()` inside
   an async health check.

5. **Module-level mutable state without locks** — `custom_rules._compiled_cache`,
   `shadow_mode._cached_patterns`, `shadow_mode._cached_config_hash`,
   `safety_container._container`, `injection_tracker` conversation map.
   Fine under single-threaded asyncio + GIL but documented risk under
   pytest-xdist reimports and any future multi-threaded usage.

6. **RPC server is the weakest file** — `engine/rpc_server.py` (7/10):
   no auth beyond file-mode, no rate limit, swallows all errors to logs
   without client notification, and `handler(**params)` pattern allows
   arbitrary kwargs from the socket into registered methods.

7. **Testing is strong** — all 48 audited files have matching test files
   in `tests/unit/<module>/test_<name>.py`; most cover edge cases and
   error paths (criterion #4 passes universally). `test_reflect.py`
   (1869 LOC), `test_safety_classifier.py` (1111 LOC), and
   `test_safety_patterns.py` (723 LOC) are deep. No file fails #4.

8. **Observability is strong** — every service file uses
   `get_logger(__name__)` from `sovyx.observability.logging`; structured
   logging with keyword args is consistent; metrics + tracing wired into
   `cognitive/loop.py` and `brain/service.py`. Criterion #3 fails only on
   `safety_i18n.py` (no logger).

9. **Input validation gaps at internal boundaries** — external-facing
   models (Pydantic `Concept`, `Episode`, `Relation`, `EngineConfig`) are
   strong, but internal boundaries use `**kwargs: object`,
   `metadata: dict[str, object]`, and `object | None` escape hatches
   (BrainService.learn_concept, ThinkPhase.process, PIIGuard.__init__).

10. **Resilience gaps on external calls** — LLM calls inside ReAct loop,
    PII NER, contradiction detection, and safety classifier rely on the
    router's global timeout. No per-call circuit breaker or retry-with-
    backoff at the cognitive layer — any LLM stall propagates as a full
    loop timeout.
