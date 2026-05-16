# CLAUDE.md — Sovyx Development Guide

## North Star

These principles override defaults when in conflict. They are enforced via `feedback_*` memories that carry the same authority as this file.

1. **Enterprise-grade, no band-aids AND no over-engineering.** Fix root causes; stop where marginal value < marginal risk. (`feedback_enterprise_only`)
2. **Zero speculation.** State only what is verified at HEAD; mark unverified claims explicitly. (`feedback_no_speculation`)
3. **Staged adoption.** Foundation → wire-up → default-flip across separate commits. Validators ship LENIENT; flip STRICT after one minor cycle of telemetry. (`feedback_staged_adoption`)
4. **Full autonomous authority on technical scope.** Operator delegates architecture, migration, testing strategy. `AskUserQuestion` is reserved for product scope, priority, UX phrasing — never technical. (`feedback_full_autonomous_authority`)
5. **Validation batched at tag milestones.** Ship between checkpoints; operator validates against `OPERATOR-VALIDATION-BACKLOG-2026.md`. (`feedback_validation_batching`)
6. **Don't watch CI after tag push.** Skip `gh run watch` on `publish.yml`. (`feedback_ci_watching`)
7. **No palliative shell scripts in chat.** Diagnostic scripts ship as committed `.sh` files with download URL — never inline heredocs. (`feedback_no_inline_scripts_in_chat`)

## Rule Precedence

When two rules conflict, apply in this order:

1. **`feedback_*` memories** — operator's explicit guidance, same authority as this file.
2. **Anti-patterns below** — incidents already paid for in production.
3. **Conventions** — style and idiom.
4. **Stack defaults** — what the framework gives you.

Lower-priority rules cannot override higher-priority ones. If tempted, stop and surface the conflict.

## What is Sovyx

Sovereign Minds Engine — persistent AI companion with real memory, cognitive loop, and brain graph. Python library + CLI daemon + React dashboard.

## Stack

- **Backend:** Python 3.11 / 3.12 (CI matrix), structlog, pydantic v2, pydantic-settings, FastAPI, aiosqlite, ONNX Runtime, httpx, argon2-cffi, PyJWT.
- **Frontend:** React 19, TypeScript, Vite, Tailwind, Zustand, TanStack Virtual, zod (runtime validation), i18next.
- **Build:** uv (Python, `uv.lock` committed), npm (dashboard), Hatch packaging with `hatchling` backend.
- **CI:** GitHub Actions on self-hosted `sovyx-4core` → ruff + mypy + bandit + pytest (3.11 & 3.12) + vitest + tsc + Docker + PyPI.
- **CLI:** `sovyx` entry point (`sovyx.cli.main:app`), plugin entry points under `sovyx.plugins`.

## Quality Gates (MANDATORY before any commit)

**Mechanical forcing function — `git push` is REJECTED without proof:**

```bash
./scripts/install_hooks.sh    # one-time per clone — installs pre-push hook
./scripts/verify_gates.sh     # runs all 8 gates + writes .git/.last-gates-pass marker
git push                      # hook validates marker fresh + HEAD-matched, else REJECTS
```

The hook at `.githooks/pre-push` (activated by `install_hooks.sh` via `git config core.hooksPath .githooks`) checks `.git/.last-gates-pass` for a HEAD-matching marker within 30 min (override: `SOVYX_GATES_MAX_AGE_SEC`). Escape hatch `git push --no-verify` requires explicit operator approval + commit-body rationale.

The 8 gates (in order):

```bash
uv run ruff check src/ tests/                                          # 1. lint
uv run ruff format --check src/ tests/                                 # 2. format
uv run mypy src/                                                       # 3. type (strict)
uv run bandit -r src/sovyx/ --configfile pyproject.toml                # 4. security
uv run python -m pytest tests/ --ignore=tests/smoke --timeout=30 -q    # 5. tests
npx tsc -b tsconfig.app.json                                           # 6. dashboard type (from dashboard/)
npx vitest run --reporter=dot                                          # 7. dashboard tests (from dashboard/)
uv run python scripts/dev/check_boundary_round_trip_coverage.py        # 8. boundary round-trip (Mission C2 §T4.1)
```

Plus `uv lock --check` when bumping versions. If running gates ad-hoc, grep the summary line — never trust the harness exit code alone. Pre-v0.42.2 the pattern `pytest ... 2>&1 | tail -N` masked 6 real failures across 4 cycles (`feedback_ci_preflight.md` + `feedback_no_speculation.md` Addendum 2026-05-14).

**Version bump:** any change to `pyproject.toml` `version` requires `uv lock` to regenerate `uv.lock` — CI enforces `uv lock --check`.

**Post-tag CI verification:** after `git push origin <tag>`, run `gh run list --workflow=publish.yml --limit 3` to confirm the previous tag passed BEFORE bumping the next. Skipping this shipped 6 tags atop a broken pipeline in v0.41.x.

## Repo Layout

```
src/sovyx/
├── engine/              # Config, bootstrap, lifecycle, events, registry, RPC (LRULockDict in _lock_dict.py)
├── cognitive/           # Perceive → Attend → Think → Act → Reflect loop (safety/, reflect/)
├── brain/               # Concepts, episodes, relations, embedding, scoring, retrieval
├── bridge/channels/     # telegram.py, signal.py
├── persistence/         # SQLite pool manager (WAL, round-robin readers), migrations
├── observability/       # Logging (structlog), health, alerts, SLOs, tracing
├── llm/                 # Multi-provider router (Anthropic, OpenAI, Google, Ollama)
├── mind/                # Mind config, personality
├── context/             # Context assembly for LLM calls
├── cli/                 # Typer CLI: sovyx start/stop/init/logs/doctor
├── dashboard/           # FastAPI; server.py wires routers, routes/ holds APIRouter per domain
├── tiers.py             # ServiceTier enum, feature/mind-limit maps
├── license.py           # LicenseValidator (Ed25519 public-key JWT, offline)
├── voice/               # STT, TTS, VAD, wake word, Wyoming. Per-mind identity via MindConfig.
│   ├── _capture_task.py # AudioCaptureTask composes mixins from capture/
│   ├── capture/         # Ring buffer + lifecycle + loop + restart strategy mixins
│   └── pipeline/        # State machine + output queue + barge-in
├── plugins/             # Loader + sandbox + SDK. Official plugins under official/ MUST use SandboxedHttpClient.
├── upgrade/             # Doctor, importer, blue-green, backup manager
└── benchmarks/          # Budget baselines

dashboard/               # React SPA — part of main repo (NOT a submodule)
├── src/pages/           # Route pages
├── src/stores/          # Zustand store (dashboard.ts + slices/)
├── src/components/      # dashboard/, ui/, auth/, chat/, settings/, layout/, common
├── src/hooks/           # use-auth, use-websocket, use-mobile, use-onboarding, use-resolved-mind-id
├── src/types/           # api.ts (compile-time) + schemas.ts (zod runtime)
└── src/lib/             # api.ts (apiFetch + api.{get,post,…}), safe-json.ts, format.ts, i18n.ts

tests/                   # unit/ integration/ dashboard/ plugins/ property/ security/ stress/ smoke/(excluded)
docs/                    # Public MkDocs source
docs-internal/           # Internal planning, missions, ADRs (gitignored)
```

## Conventions

### Python

- **Logging:** `from sovyx.observability.logging import get_logger` → `logger = get_logger(__name__)`. Never `print()` or `logging.getLogger()` directly.
- **Config:** All config via `EngineConfig` (pydantic-settings). Env: `SOVYX_*` prefix, `__` for nesting. Tuning knobs under `EngineConfig.tuning.{safety,brain,voice}` — overridable via `SOVYX_TUNING__*`.
- **Errors:** Custom exceptions in `engine/errors.py`; always include `context` dict.
- **Type hints:** Fully typed. `from __future__ import annotations` in every file. `TYPE_CHECKING` block for type-only imports (ruff `TCH`).
- **Async:** All DB/IO is async. Sync CPU-bound work (ONNX, boto3) MUST be wrapped in `asyncio.to_thread()`. Tests use `pytest-asyncio` with `mode=auto`.
- **Docstrings:** Every public class/function. Imperative first line. No other comments unless WHY is non-obvious.

### Dashboard (TypeScript)

- **Types:** Compile-time in `src/types/api.ts`; runtime zod in `src/types/schemas.ts`. Pass `{ schema }` to `api.{get,post,put,patch,delete}` for safeParse validation.
- **State:** Zustand store at `src/stores/dashboard.ts` with slices pattern.
- **API calls:** ALWAYS via `src/lib/api.ts` — `api.*` for JSON, `apiFetch(path, init, overrideToken?)` for raw `Response`. Defaults: 30s timeout, exp-backoff retry on 429/503/5xx for idempotent verbs.
- **Auth token:** `sessionStorage` + in-memory fallback. NEVER `localStorage`.
- **Hot-path memoization:** `React.memo` on rows in virtualized lists (log-row, chat-bubble, plugin-card, timeline-row, tool-item); `useMemo`/`useCallback` for derived values + stable props.
- **i18n:** All user-visible strings via `useTranslation()`.
- **Mind id:** Use `useResolvedMindId` hook — never hardcode `"default"` (anti-pattern #35). ESLint rule guards this.
- **Tests:** Colocated `*.test.tsx` next to each page/component.

### Git

- **Commits:** Conventional (`feat:`, `fix:`, `refactor:`, `test:`, `chore:`, `perf:`, `docs:`).
- **Tags:** `vX.Y.Z` triggers `publish.yml` — full CI gate → PyPI (OIDC) + Docker + GitHub Release. Tag version must match `pyproject.toml` version.
- **Dashboard:** part of main repo; stage dashboard changes alongside backend in the same commit when related.
- **Branch:** Always `main`. No feature branches.

## Anti-Patterns (bugs that already happened)

Each entry is **rule + why + pointer**. Forensic detail lives in the referenced commit/mission/file. Cross-references in memories and commits use the entry number — preserve numbering when adding (append, never renumber).

**Index by category:**

- **Logging & Config:** 1, 3, 4, 5, 6, 7, 17, 23, 35
- **Imports & Test Patches:** 2, 11, 20, 36, 38
- **Concurrency & Async:** 14, 15, 30
- **Cross-Platform:** 21, 22, 24
- **Voice Subsystem:** 25, 26, 27, 28, 29, 39
- **Tests:** 8, 9, 10, 12, 31
- **Architecture & Design:** 13, 16, 18, 19, 32, 33, 34, 37, 39, 40

---

1. **Circular imports in `observability/__init__.py`:** lazy `__getattr__`. Never add eager imports.
2. **`sys.modules` stubs miss aliased imports:** `import X as Y` captures the real module at import time. Use `patch.object(real_module, "attr", mock)`. Reserve `sys.modules` for genuinely first-time imports.
3. **`LoggingConfig.console_format` (not `format`):** renamed v0.5.24; legacy YAML auto-migrates. File handler ALWAYS writes JSON.
4. **`log_file` resolved by `EngineConfig` validator:** `LoggingConfig.log_file` defaults to `None`; resolved to `data_dir/logs/sovyx.log`. Never hardcode log paths.
5. **Dashboard `EngineConfig` from registry:** resolved via `ServiceRegistry`, never `EngineConfig()` instantiation.
6. **httpx logs at WARNING in `setup_logging()`:** raw HTTP lines in console = `setup_logging()` wasn't called.
7. **`LogEntry` has 4 required fields:** `timestamp`, `level`, `logger`, `event`. Backend normalizes `ts→timestamp`, `severity→level`, `message→event`, `module→logger`.
8. **xdist class identity:** pytest-xdist can reimport modules → duplicate classes. Never `pytest.raises(InternalClass)`; use `pytest.raises(Exception)` + `assert type(exc).__name__ == "X"`. In prod, dispatch on `type(exc).__name__`, never `isinstance`.
9. **Enums are `StrEnum`:** every string-valued enum inherits from `StrEnum`, never plain `Enum`. Guarantees value-based comparison + xdist namespace safety.
10. **Auth in tests via `create_app(token="...")`:** never monkeypatch `_ensure_token` or `_server_token`. The `token` parameter bypasses filesystem + global state.
11. **Prefer `patch.object` over string-path patches:** `patch("module.attr")` can resolve to different module objects under xdist or after refactors. `patch.object(imported_module, "attr")` is stable.
12. **Defense-in-depth in tests is a smell:** if 3 layers make a test pass, you don't know which one works. One layer understood > three mysterious. When a fix makes a workaround unnecessary, delete it in the same commit.
13. **Plugins use `SandboxedHttpClient`, never raw `httpx`:** raw `httpx.AsyncClient(...)` from plugin code bypasses allowed-domains + rate-limit + size-cap and turns the sandbox into theater.
14. **Sync CPU-bound in `async def` blocks the event loop:** ONNX inference (Piper, Kokoro, Silero, Moonshine, OpenWakeWord), `boto3`, any blocking CPU/IO MUST be wrapped in `asyncio.to_thread(fn, *args)`.
15. **Unbounded `defaultdict(asyncio.Lock)` leaks memory:** one-lock-per-key patterns use `sovyx.engine._lock_dict.LRULockDict(maxsize=N)` so unused keys evict.
16. **God files (>500 LOC, mixed responsibilities) split into subpackage:** `__init__.py` re-exports public surface; underscore-prefixed sub-files are internal. Migrate test patches in the same commit (#20). Worked examples: `cognitive/safety/`, `cognitive/reflect/`, `voice/pipeline/`, `voice/capture/`, `dashboard/routes/`.
17. **Hardcoded tuning constants:** thresholds, timeouts, URLs, SHAs live in `EngineConfig.tuning.{safety,brain,voice}`. Module-level `_CONST = _TuningCls().field` keeps import-time access + `SOVYX_TUNING__*` env override.
18. **Raw `fetch()` in frontend:** every network call via `src/lib/api.ts` — `api.*` for JSON (auth + retry + timeout + schema), `apiFetch` for raw `Response`. A loose `fetch("/api/…")` drifts from auth injection + 401 handler.
19. **`localStorage` for auth tokens is XSS-exposed:** use `sessionStorage` (tab-scoped) + in-memory fallback (in `src/lib/api.ts`). Boot-time migrator reads legacy `localStorage`.
20. **Test patches must follow module splits:** extracting a helper turns every `patch("old.module.X")` into a silent no-op. Migrate paths in the same commit as the split. Extends to: lazy `from X import Y` (#38); `caplog.set_level(logger=...)` widening; `patch.object(mod, "sys", ...)` across submodule boundaries.
21. **Windows capture APOs corrupt mic before PortAudio sees it:** Voice Clarity (`VocaEffectPack`/`voiceclarityep`) destroys Silero VAD input — max speech prob < 0.01 despite healthy RMS. Fix: WASAPI exclusive (`capture_wasapi_exclusive`) bypasses APO chain. Auto-detected at startup (`voice._apo_detector`); auto-bypasses on repeated deaf heartbeats (`voice_clarity_autofix=True`). Never tune VAD or add AGC — signal is destroyed upstream. Surfaces: `sovyx doctor voice_capture_apo`, `GET /api/voice/capture-diagnostics`.
22. **Windows `time.monotonic()` ticks at ~15.6 ms without `timeBeginPeriod`:** `time.sleep(0.01)` can yield zero-tick delta. Timer-sensitive tests: sleeps ≥ 50 ms or fake clock; perf measurement uses `time.perf_counter`. Linux sub-µs masks this on CI.
23. **`EngineConfig.data_dir` defaults to `~/.sovyx`; bootstrap re-seeds env from it:** `bootstrap()` reads `<data_dir>/{channel,secrets}.env` into the process env. Tests MUST pass both `data_dir=tmp_path` AND `database=DatabaseConfig(data_dir=tmp_path)`. Use `monkeypatch.delenv` (auto-restored), not `os.environ.pop`. Bootstrap auto-detect checks 9 cloud-LLM keys (`ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `GOOGLE_API_KEY`, `XGROK_API_KEY`, `DEEPSEEK_API_KEY`, `MISTRAL_API_KEY`, `GROQ_API_KEY`, `TOGETHER_API_KEY`, `FIREWORKS_API_KEY`).
24. **Strict `>` on `time.monotonic()` deadlines is silently wrong on coarse clocks:** when `now` and deadline share a tick, `>` never fires (`ttl_sec=0` never expires). Prefer `>=` — inclusive + coarse-safe.
25. **Frame-typed pipeline is observability, NOT state-machine rewrite (Hybrid Option C):** `PipelineFrame` + 8 subclasses in `voice/pipeline/_frame_types.py` instrument transitions/cancellations with structured metadata, but authoritative state stays in `VoicePipelineState` + boolean flags. Frames go into a bounded 256-entry ring buffer via `PipelineStateMachine.record_frame`; surfaced at `GET /api/voice/frame-history`. Never couple prod logic to frame presence. Full Pipecat rewrite deferred to v0.24.0+.
26. **KB profile signing — dev key in repo, prod rotation via HSM:** `voice/health/_mixer_kb/_trusted_keys/v1.pub` is dev. Private key at `.signing-keys/sovyx_kb_v1.priv` is gitignored + STAYS LOCAL. Loader stays `Mode.LENIENT` for v0.23.x; flips `STRICT` after one minor cycle (`feedback_staged_adoption`). Prod: HSM-backed (YubiKey/AWS KMS/GCP Cloud KMS), multi-key trust store with overlapping windows. Compromise: 24h advisory + emergency v2 roll + `STRICT` flip + community PR purge. Procedure: `docs/contributing/voice-kb-rotation.md`.
27. **`contextlib.suppress` + `logger.debug(..._skipped, reason=…)` is the canonical "intentional ignore":** replaces raw `try/except: pass` for genuinely benign failures. Explicit intent + observability, debug-stripped in prod. Reject: silent suppression with no log; WARN floods; raising errors callers can't handle.
28. **Cold probe MUST validate signal energy, not callback count (Furo W-1):** APOs leave PortAudio callbacks firing while delivering exact-zero PCM. v0.24.0: `_diagnose_cold` reads `rms_db`; strict mode returns `Diagnosis.NO_SIGNAL` when `rms_db < probe_rms_db_no_signal`; lenient emits `voice.probe.cold_silence_rejected{mode=lenient_passthrough}`. **Generalizes:** any acceptance gate downstream of a real-world signal source MUST verify the signal itself, not just the wrapping mechanics.
29. **`CaptureRestartFrame` is observability, NOT state-machine rewrite (sibling of #25):** every restart method (`request_exclusive_restart`, `request_alsa_hw_direct_restart`, …) emits a frame BEFORE the ring-buffer epoch increments; orchestrator records via `PipelineStateMachine.record_frame`. Surfaced at `GET /api/voice/restart-history`. Schema fields stay `.optional()` for one minor cycle before promotion.
30. **`psutil.open_files()`/`net_connections()` hang during async teardown on Windows:** psutil iterates kernel handles + `os.stat()` per handle; closing handles cause indefinite blocks — `try/except` catches exceptions, NOT blocked syscalls. CI symptom: 6+ min timeout in `_capture_psutil_metrics`. Linux unaffected. Fix: `skip_expensive: bool` kwarg on metrics-emit path; cheap fields still flow on shutdown. Site: `observability/resources.py::_capture_psutil_metrics` + `_emit_snapshot(final=True)` (commit `003a63f`). **Generalizes:** shutdown/cancellation hooks MUST avoid handle-iterating syscalls or wrap in `asyncio.wait_for` with a strict deadline.
31. **Perf gate p99 ratio is tail-sensitive even with median-of-3:** `scripts/check_perf_regression.py` runs `bench_observability.py` 3× and takes median p99. Sustained GitHub Linux contention can blow all 3 → median = noise → gate fails on unrelated commits. **Triage:** if `git diff` doesn't touch `observability/logging.py`, `_async_handler.py`, or the structlog chain, prior is contention. If it does, suspect lost `put_nowait` fast path on `AsyncQueueHandler.enqueue` or `BackgroundLogWriter` doing work on the producer thread. Hardening: bump `_DEFAULT_REPEATS` 3→5 or trimmed-mean (drop high+low).
32. **Mixin stubs silently shadow real methods later in MRO:** `def foo(self) -> None: ...` on `MixinA` is a real method (the `...` body returns `None`) and wins MRO over the real `foo` on a later `MixinB`. Shadowed call returns `None` silently — invisible to mypy/ruff/bandit. Safe patterns: (a) target BEFORE caller in MRO → naked stub is fine (real method found first); (b) target AFTER caller in MRO → declare cross-mixin reference inside `if TYPE_CHECKING:` (erased at runtime → MRO falls through). Documented in `voice/capture/_loop_mixin.py`.
33. **Per-mind config from RPC handlers: best-effort YAML, never assume registry methods exist:** `MagicMock`-typed `registry.resolve(...).method(...)` returns `Any` and masks `AttributeError` at test time → prod blows up at first invocation. Before `await registry.resolve(X).method(y)`, grep `class X:` for `def method`. Privacy-sensitive paths (retention) MUST fall through to global defaults on malformed config — compliance > perfect resolution. Reference: `_load_mind_config_best_effort` in `engine/_rpc_handlers.py`.
34. **Schedulers with kill-switch flags default OFF + skip instantiation when disabled:** default-OFF means default-ABSENT, not default-PRESENT-but-no-op. Bootstrap: `if config.X.enabled: register_instance(...)`. Lifecycle: `if registry.is_registered(X): start ...`. Always-instantiate-+-start-time-check leaks no-op tasks into the loop + no-op registry entries, confusing for triage. Applied: ConsolidationScheduler/DreamScheduler/RetentionScheduler.
35. **Cross-layer config defaults are sentinels, not values:** `VoicePipelineConfig.mind_id: str = "default"` is a sentinel callers MUST overwrite; every caller path that omits it is a silent bug. Prior: voice pipeline launched under phantom `"default"` because `dashboard/routes/voice.py` read `getattr(request.app.state, "mind_id", "default")` while no production code ever assigned `app.state.mind_id`. Safe patterns: (a) **make field required** (preferred for NEW fields) — type-check enforces; (b) **detect sentinel at top wire-up + structured WARN** — safe migration when sentinel already shipped. Pattern (b): `voice/factory/__init__.py` (`voice.factory.mind_id_default_sentinel`) + `dashboard/_shared.resolve_active_mind_id_for_request`. **Recurring offender — surfaced 5+ times in voice flow.** Frontend: `useResolvedMindId` hook + ESLint rule.
36. **`patch.object` on async functions auto-detects `AsyncMock`** (Python 3.8+ inspects with `iscoroutinefunction` and substitutes `AsyncMock`); string-path `patch` follows the same autodetect when the import resolves at patch time. Prefer `patch.object(module, "name", return_value=X)` over `patch("path", new_callable=AsyncMock, return_value=X)` — autodetect is documented and load-bearing for clean async test code.
37. **Cryptographic verifier verdict ordering — cheapest + most-common-failure first, dependency invariants before dependent ops:** in a 5-way verdict (`ACCEPTED/REJECTED_NO_SIGNATURE/REJECTED_BAD_SIGNATURE/REJECTED_MALFORMED_SIGNATURE/REJECTED_NO_TRUSTED_KEY`), order: (1) `pubkey is None` (else `pubkey.verify(...)` crashes with `AttributeError`); (2) `signature is None` (cheap, avoids canonicalization); (3) signature shape malformed (b64 invalid OR length != 64; avoids less-informative `InvalidSignature`); (4) actual `pubkey.verify` (expensive). Site: `_persistence.py::_verify_calibration_signature`.
38. **Lazy `from X import Y` inside a function body invalidates module-level patches:** the lazy import resolves on the SOURCE module at call-time, not on the caller's top-level binding. Patch `X.Y` (source attr), NOT `caller.Y`. Mixed cases: a single test may patch BOTH `caller.eager_attr` AND `source.lazy_attr`. Extends #20. **Cross-platform corollary:** when production references a POSIX-only attribute (`signal.SIGKILL`, `os.killpg`), Windows tests patching `sys.platform="linux"` MUST also `patch.object(target, "ATTR", value, create=True)`.
39. **Probe-verdict misrouting + cross-platform event-name drift.** Two paired subrules.

    **(a) Verdict-disjoint remediation.** Acceptance gates + remediation routers MUST consume the probe **verdict** (a categorical classification), not the wrapping symptom. `vad_mute` (user not speaking) and `no_signal` (driver silent) are orthogonal failure classes; routing both to the same ladder loses the operator's working hardware. Sibling of #28. v0.44.0 verdict-router (Mission C1 T1.3) restored disjoint dispatch with `assert_never` exhaustiveness. LENIENT consultation-of-derived-reason corollary (`is_recheck_eligible`/`is_apo_class_reason`, commit `c5791e40`): when a verdict-disjoint field is added during staged adoption, every classifier consumer MUST consult the new field first with fallback to legacy — bare reads of the legacy field silently disable the dispatch. Mission anchor: `docs-internal/missions/MISSION-c1-vad-mute-reclassification-2026-05-14.md`.

    **(b) Cross-platform event-name drift.** Cross-platform event names MUST be neutral; platform-specific terminology (`apo.*`, `wasapi.*`, `dsound.*`) MUST be `sys.platform`-gated or live behind a neutral wrapper. Strategies can be platform-specific without the wrapping event needing to be. Sibling of #21. Pre-H2 mission: `audio.apo.bypassed` + `voice_apo_bypass_ineffective` fired on Linux hosts where `voice_clarity_active=False`. Generalizes: an event's name is part of its public API for operators, dashboards, and downstream triage tooling. Mission anchor: separate v0.43.3 mission (sibling of C1).
40. **Typed response boundary drifts from producer dict shape when both evolve independently:** a `Model.model_validate(helper_dict)` call at a route boundary is only as strict as the LAST round-trip test that exercised the producer's real prod shape. Helper functions returning `dict[str, Any]` provide no static cross-boundary type check, so the producer can grow new field shapes (int alongside str, additional enum values, optional → required) without the boundary noticing. The forward-additive policy on response models (`model_config = {"extra": "allow"}`) is load-bearing — closing it off would break Phase 5.D's freedom to ship new SLI fields without route migrations — but that flexibility MUST be paired with a producer→boundary round-trip test, else drift escapes CI. Reference: Mission C2 — `VoiceStatusResponse.capture.input_device: str | None` narrowed at commit `aee85844` (Phase 5.D v0.32.7); producer always emitted `int | str | None` via `AudioCaptureTask._input_device` rebound to `info.device_index` at `_capture_task.py:694`. Every `/api/voice/status` request 500'd in production until C2 widened the union at commit `00cb6e72`. Quality Gate 8 (`scripts/dev/check_boundary_round_trip_coverage.py`) enforces the round-trip pairing AST-mechanically: every `.model_validate(...)` call in `routes/voice.py` MUST have a paired test under `tests/dashboard/` calling either `Model.model_validate(...)` directly OR `assert_boundary_accepts(Model, ...)`. Mission anchor: `docs-internal/missions/MISSION-c2-voice-status-response-contract-2026-05-16.md`.

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

# Exception assertions — xdist-safe (see anti-pattern #8)
with pytest.raises(Exception) as exc_info:
    do_something_that_raises()
assert type(exc_info.value).__name__ == "LLMError"
assert "expected message" in str(exc_info.value)

# Mocking SandboxedHttpClient plugins — internal call is ._client.request(METHOD, url, ...), NOT .get().
# Wire MockClient.return_value to the mock (NOT the async-with __aenter__ path).
with patch("httpx.AsyncClient") as MockClient:
    mock_client = AsyncMock()
    mock_client.request = AsyncMock(return_value=mock_resp)
    mock_client.aclose = AsyncMock()
    MockClient.return_value = mock_client
    result = await my_plugin_func()

# Aliased imports (anti-pattern #2): patch real module, not sys.modules
import onnxruntime
with patch.object(onnxruntime, "InferenceSession", return_value=mock_sess):
    ...

# After a module split (anti-pattern #20): patch the NEW path
with patch("sovyx.brain._model_downloader.httpx.AsyncClient", ...):
    ...
```

## Debugging Rules

1. **Audit first.** Grep the full codebase for ALL instances of the same pattern. Map the size before solving any single instance.
2. **Group by root cause.** If 28 tests fail, find how many distinct root causes exist. Fix causes, not symptoms.
3. **Don't band-aid.** If you can't explain WHY a fix works, it's not ready.
4. **One commit per root cause.** No partial pushes to CI for incremental testing.
5. **No shotgun debugging.** If you're setting the same value in 3 places hoping one sticks, stop and trace the actual read path.
6. **Local suite before push.** Each CI round-trip wastes minutes and fragments reasoning.
7. **Check the full chain.** A config bug might affect CLI, dashboard, and API.
8. **Write regression tests.** The bug must never recur.
9. **Third fix→push→CI-fail cycle = STOP.** The approach is wrong. Step back, reassess.
10. **Windows mypy noise:** local `uv run mypy src/` reports 9 platform-specific false positives (`AF_UNIX`, `os.sysconf`, `getrusage`, `open_unix_server`). Only errors OUTSIDE that list are real regressions. CI runs Linux — the true baseline.
11. **Closure protocol on a bug class.** When fixing one site (e.g. #35 on `VoiceStep.tsx`), grep ALL consumers of the same flag/sentinel before declaring the fix complete. State the closure assertion in the commit body. Bug classes surface in waves; each unaudited consumer is the next RC.

## Working Style

**On any task:**

1. Understand scope — read relevant files + dependencies.
2. Check for existing patterns — look at similar code for conventions.
3. Implement — follow conventions above.
4. Tests — ≥95% coverage on modified files, include edge cases.
5. Run ALL quality gates — `./scripts/verify_gates.sh`.
6. Commit with conventional message — body explains WHY.

**When modifying tests:**

1. Never introduce workarounds — if a test needs patching to pass, production may need a better interface (e.g. `create_app(token=...)` over monkeypatch).
2. Prefer explicit parameters over mocking — dependency injection beats monkeypatch.
3. One assertion pattern — use the xdist-safe form (#8) consistently.
4. Remove dead code — if a fix makes a workaround unnecessary, delete it in the same commit.

**When splitting a god file:**

1. Public surface stays stable — `__init__.py` re-exports everything.
2. One responsibility per sub-file — underscore-prefixed modules signal "internal, accessed via parent".
3. Migrate tests in the same commit — old `patch("old.module.X")` becomes a silent no-op (#20).
4. Preserve the public docstring — move it to the parent `__init__.py` if the original class was the face of the module.

## Deploy Flow

1. Bump `version` in `pyproject.toml` (single source — `src/sovyx/__init__.py` reads via `importlib.metadata.version`).
2. `uv lock` (CI enforces `uv lock --check`).
3. `git commit` + `git tag vX.Y.Z` + `git push origin main` + `git push origin vX.Y.Z`.
4. Tag triggers `publish.yml`: CI gate → dashboard build → `uv build` → PyPI (OIDC) → GitHub Release → Docker (parallel).
5. If CI fails on a tagged commit: fix + commit + re-tag with `git tag -d vX.Y.Z && git tag vX.Y.Z && git push origin vX.Y.Z --force`.

Per `feedback_ci_watching`: don't `gh run watch` after tag push — the operator surfaces failures via the validation backlog.

### Two-Tier GA Strategy (voice subsystem)

Per master mission `MISSION-voice-final-skype-grade-2026.md`:

- **v0.30.0 — single-mind production GA.** Phases 1-7 complete (cold-probe, bypass tiers, telemetry/IMM listener, multi-platform). Operators MAY ship without Phase 8.
- **v0.31.0 — FINAL multi-mind GA.** Phase 8 complete (per-mind wake word, voice ID, language, accent, cadence).

Phase 8 work goes into v0.30.x patches or directly v0.31.0 — never blocks v0.30.0 release.

## Mission Lifecycle

Multi-version work is coordinated via long-running structured missions.

- **Active** missions: `docs-internal/missions/MISSION-*.md` with task IDs (T1.1, T1.2, …) + Phase boundaries mapped to versions.
- **ADRs** at `docs-internal/ADR-*.md` are CANONICAL — referenced from code docstrings. Never delete; supersede via a new ADR referencing the old.
- **Completed/superseded** missions are archived to `docs-internal/archive/missions-completed/` with an `## Archive Footer` block (status, code refs, predecessor/successor). Update `docs-internal/archive/INDEX.md`.
- **Forensic resolution docs** go to `docs-internal/archive/forensics-resolved/` with the same footer convention.
- **Never delete** a mission or ADR that produced shipped code — reference value > workspace cleanliness. Pure orphans (planning docs that produced no code, byte-identical duplicates) are the only valid DELETE targets.

When closing a mission task in a commit, reference the mission file + task ID in the body (e.g. `Mission: docs-internal/missions/MISSION-voice-final-skype-grade-2026.md §Phase 1.T2`) and update the mission spec to mark the task ✅ shipped in a follow-up `docs(mission):` commit.

## Deep Reference

- Public docs (MkDocs): `docs/` — architecture, getting-started, configuration, api-reference, security, per-module under `docs/modules/`.
- Internal planning + audits: `docs-internal/` (gitignored).
- Backend specs (IMPL/SPE/ADR): `docs-internal/`, searchable by number.
- Code patterns: existing tests are canonical — `tests/unit/` mirrors `src/sovyx/`.
- Frontend types: `dashboard/src/types/api.ts` (compile-time) + `schemas.ts` (runtime).

## Persistent Memory

Auto-memory persists across sessions.

- **Location:** `C:\Users\guipe\.claude\projects\E--sovyx\memory\`
- **Index file:** `MEMORY.md` — load every linked entry at session start. Keep index lines ≤ 150 chars; detail lives in the linked file.
- **Authority:** `feedback_*` carry the SAME authority as CLAUDE.md and OVERRIDE default behavior (see `## Rule Precedence`). The North Star is the canonical summary of the current `feedback_*` set.
- **Project memories** (`project_*`) carry historical context: missions, incidents, paranoid investigations.
- **User memories** (`user_*`) carry preferences and role context.
- **Reference memories** (`reference_*`) point to external systems.

Before recommending from memory, verify the referenced file/function still exists. **Memory state at write time ≠ current state.** When a memory recommends a flag/file/path, grep before relying on it.
