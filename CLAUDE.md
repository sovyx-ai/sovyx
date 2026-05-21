# CLAUDE.md — Sovyx Development Guide

## North Star

These principles override defaults when in conflict. Enforced via `feedback_*` memories that carry the same authority as this file.

1. **Enterprise-grade, no band-aids AND no over-engineering.** Fix root causes; stop where marginal value < marginal risk. (`feedback_enterprise_only`)
2. **Zero speculation.** State only what is verified at HEAD; mark unverified claims explicitly. (`feedback_no_speculation`)
3. **Staged adoption.** Foundation → wire-up → default-flip across separate commits. Validators ship LENIENT; flip STRICT after one minor cycle. (`feedback_staged_adoption`)
4. **Full autonomous authority on technical scope.** Operator delegates architecture, migration, testing. `AskUserQuestion` reserved for product scope, priority, UX phrasing — never technical. (`feedback_full_autonomous_authority`)
5. **Validation batched at tag milestones.** Ship between checkpoints; operator validates against `OPERATOR-VALIDATION-BACKLOG-2026.md`. (`feedback_validation_batching`)
6. **Don't watch CI after tag push.** Skip `gh run watch` on `publish.yml`. (`feedback_ci_watching`)
7. **No palliative shell scripts in chat.** Diagnostic scripts ship as committed `.sh` w/ download URL — never inline heredocs. (`feedback_no_inline_scripts_in_chat`)

## Rule Precedence

When two rules conflict, apply in order: (1) **`feedback_*` memories** — operator's explicit guidance, same authority as this file; (2) **Anti-patterns below** — incidents already paid for in production; (3) **Conventions** — style and idiom; (4) **Stack defaults** — what the framework gives you. Lower-priority rules cannot override higher-priority ones. If tempted, stop and surface the conflict.

## What is Sovyx

Sovereign Minds Engine — persistent AI companion with real memory, cognitive loop, and brain graph. Python library + CLI daemon + React dashboard.

## Stack

- **Backend:** Python 3.11/3.12 (CI matrix), structlog, pydantic v2, pydantic-settings, FastAPI, aiosqlite, ONNX Runtime, httpx, argon2-cffi, PyJWT.
- **Frontend:** React 19, TypeScript, Vite, Tailwind, Zustand, TanStack Virtual, zod, i18next.
- **Build:** uv (`uv.lock` committed), npm (dashboard), Hatch + `hatchling` backend.
- **CI:** GitHub Actions on self-hosted `sovyx-4core` → ruff + mypy + bandit + pytest (3.11 & 3.12) + vitest + tsc + Docker + PyPI.
- **CLI:** `sovyx` entry point (`sovyx.cli.main:app`), plugin entry points under `sovyx.plugins`.

## Quality Gates (MANDATORY before any commit)

**Mechanical forcing function — `git push` is REJECTED without proof:**

```bash
./scripts/install_hooks.sh    # one-time per clone — installs pre-push hook
./scripts/verify_gates.sh     # runs all gates + writes .git/.last-gates-pass marker
git push                      # hook validates marker fresh + HEAD-matched, else REJECTS
```

Hook at `.githooks/pre-push` (activated via `install_hooks.sh` → `git config core.hooksPath .githooks`) checks `.git/.last-gates-pass` for HEAD-matching marker within 30 min (override: `SOVYX_GATES_MAX_AGE_SEC`). Escape `git push --no-verify` requires explicit operator approval + commit-body rationale.

The gates (in order):

```bash
# 1-5 backend: ruff check / ruff format --check / mypy (strict) / bandit / pytest --timeout=30 -q
# 6-7 dashboard (from dashboard/): npx tsc -b tsconfig.app.json / npx vitest run --reporter=dot
# 8-10 STRICT: check_boundary_round_trip_coverage.py (C2) / check_ladder_iteration_discipline.py (C3) / check_degraded_signal_surface.py (C4)
# 11 check_dashboard_bundle_integrity.py     — LENIENT v0.47.x; STRICT v0.48.0    (C5, V-C5-7)
# 12 check_llm_provider_discipline.py        — LENIENT v0.49.x; STRICT v0.50.0    (C6, V-C6-11)
# 13 check_platform_neutral_event_names.py   — LENIENT v0.49.x; STRICT v0.51.0    (H2, V-H2-11)
# 14 check_quarantine_reason_discipline.py   — LENIENT v0.49.10..v0.52.x; STRICT v0.53.0 (H3, V-H3-11)
# 15 check_resource_hygiene_discipline.py    — LENIENT v0.49.14..v0.53.x; STRICT v0.54.0 (H4, V-H4-13)
```

Plus `uv lock --check` on version bumps. If running gates ad-hoc, grep the summary line — never trust harness exit code alone (pre-v0.42.2 `pytest ... 2>&1 | tail -N` masked 6 failures; see `feedback_ci_preflight.md`).

**Version bump:** any change to `pyproject.toml` `version` requires `uv lock` — CI enforces `uv lock --check`.

**Post-tag CI verification:** after `git push origin <tag>`, run `gh run list --workflow=publish.yml --limit 3` to confirm the previous tag passed BEFORE bumping the next. Skipping shipped 6 tags atop a broken pipeline in v0.41.x.

## Repo Layout

```
src/sovyx/
├── engine/        # Config, bootstrap, lifecycle, events, registry, RPC (LRULockDict in _lock_dict.py)
├── cognitive/     # Perceive → Attend → Think → Act → Reflect loop (safety/, reflect/)
├── brain/         # Concepts, episodes, relations, embedding, scoring, retrieval
├── bridge/channels/  # telegram.py, signal.py
├── persistence/   # SQLite pool manager (WAL, round-robin readers), migrations
├── observability/ # Logging (structlog), health, alerts, SLOs, tracing
├── llm/           # Multi-provider router (Anthropic, OpenAI, Google, Ollama)
├── mind/, context/  # Mind config + LLM context assembly
├── cli/           # Typer CLI: sovyx start/stop/init/logs/doctor
├── dashboard/     # FastAPI; server.py wires routers, routes/ holds APIRouter per domain
├── tiers.py, license.py  # ServiceTier enum + Ed25519 offline license validator
├── voice/         # STT, TTS, VAD, wake word, Wyoming. Per-mind via MindConfig.
│   ├── _capture_task.py  # AudioCaptureTask composes mixins from capture/
│   ├── capture/   # Ring buffer + lifecycle + loop + restart strategy mixins
│   └── pipeline/  # State machine + output queue + barge-in
├── plugins/       # Loader + sandbox + SDK. Official plugins MUST use SandboxedHttpClient.
├── upgrade/       # Doctor, importer, blue-green, backup manager
└── benchmarks/    # Budget baselines

dashboard/         # React SPA — part of main repo (NOT a submodule)
├── src/pages/     # Route pages
├── src/stores/    # Zustand (dashboard.ts + slices/)
├── src/components/  # dashboard/, ui/, auth/, chat/, settings/, layout/, common
├── src/hooks/     # use-auth, use-websocket, use-mobile, use-onboarding, use-resolved-mind-id
├── src/types/     # api.ts (compile-time) + schemas.ts (zod runtime)
└── src/lib/       # api.ts (apiFetch + api.{get,post,…}), safe-json.ts, format.ts, i18n.ts

tests/             # unit/ integration/ dashboard/ plugins/ property/ security/ stress/ smoke/(excluded)
docs/              # Public MkDocs source
docs-internal/     # Internal planning, missions, ADRs (gitignored)
```

## Conventions

### Python

- **Logging:** `from sovyx.observability.logging import get_logger` → `logger = get_logger(__name__)`. Never `print()` or `logging.getLogger()` directly.
- **Config:** All via `EngineConfig` (pydantic-settings). Env: `SOVYX_*` prefix, `__` for nesting. Tuning knobs under `EngineConfig.tuning.{safety,brain,voice}` — overridable via `SOVYX_TUNING__*`.
- **Errors:** Custom exceptions in `engine/errors.py`; always include `context` dict.
- **Type hints:** Fully typed. `from __future__ import annotations` in every file. `TYPE_CHECKING` block for type-only imports (ruff `TCH`).
- **Async:** All DB/IO async. Sync CPU-bound (ONNX, boto3) MUST be wrapped in `asyncio.to_thread()`. Tests: `pytest-asyncio` w/ `mode=auto`.
- **Docstrings:** Every public class/function. Imperative first line. No other comments unless WHY is non-obvious.

### Dashboard (TypeScript)

- **Types:** Compile-time `src/types/api.ts`; runtime zod `src/types/schemas.ts`. Pass `{ schema }` to `api.{get,post,put,patch,delete}` for safeParse.
- **State:** Zustand at `src/stores/dashboard.ts` w/ slices pattern.
- **API calls:** ALWAYS via `src/lib/api.ts` — `api.*` for JSON, `apiFetch(path, init, overrideToken?)` for raw `Response`. Defaults: 30s timeout, exp-backoff retry on 429/503/5xx for idempotent verbs.
- **Auth token:** `sessionStorage` + in-memory fallback. NEVER `localStorage`.
- **Hot-path memo:** `React.memo` on virtualized list rows; `useMemo`/`useCallback` for derived values + stable props.
- **i18n:** All user-visible strings via `useTranslation()`.
- **Mind id:** Use `useResolvedMindId` — never hardcode `"default"` (#35). ESLint rule guards this.
- **Tests:** Colocated `*.test.tsx`.

### Git

- **Commits:** Conventional (`feat:`, `fix:`, `refactor:`, `test:`, `chore:`, `perf:`, `docs:`).
- **Tags:** `vX.Y.Z` triggers `publish.yml` — full CI gate → PyPI (OIDC) + Docker + GitHub Release. Tag must match `pyproject.toml` version.
- **Dashboard:** part of main repo; stage dashboard changes alongside backend in the same commit when related.
- **Branch:** Always `main`. No feature branches.

## Anti-Patterns (bugs that already happened)

Each entry is **rule + why + pointer**. Forensic detail lives in the referenced commit/mission/file. Cross-references use the entry number — preserve numbering when adding (append, never renumber).

**Index by category:** Logging & Config: 1, 3, 4, 5, 6, 7, 17, 23, 35 · Imports & Test Patches: 2, 11, 20, 36, 38 · Concurrency & Async: 14, 15, 30 · Cross-Platform: 21, 22, 24 · Voice Subsystem: 25, 26, 27, 28, 29, 39 · Tests: 8, 9, 10, 12, 31 · Architecture & Design: 13, 16, 18, 19, 32, 33, 34, 37, 39, 40, 41, 42, 43, 44, 45, 46, 47, 48, 49, 50, 51, 52, 53, 54

---

1. **Circular imports in `observability/__init__.py`:** lazy `__getattr__`. Never add eager imports.
2. **`sys.modules` stubs miss aliased imports:** `import X as Y` captures the real module at import time. Use `patch.object(real_module, "attr", mock)`. Reserve `sys.modules` for first-time imports.
3. **`LoggingConfig.console_format` (not `format`):** renamed v0.5.24; legacy YAML auto-migrates. File handler ALWAYS writes JSON.
4. **`log_file` resolved by `EngineConfig` validator:** `LoggingConfig.log_file` defaults `None`; resolved to `data_dir/logs/sovyx.log`. Never hardcode log paths.
5. **Dashboard `EngineConfig` from registry:** resolved via `ServiceRegistry`, never `EngineConfig()` instantiation.
6. **httpx logs at WARNING in `setup_logging()`:** raw HTTP lines in console = `setup_logging()` wasn't called.
7. **`LogEntry` 4 required fields:** `timestamp`, `level`, `logger`, `event`. Backend normalizes `ts→timestamp`, `severity→level`, `message→event`, `module→logger`.
8. **xdist class identity:** pytest-xdist can reimport modules → duplicate classes. Never `pytest.raises(InternalClass)`; use `pytest.raises(Exception)` + `assert type(exc).__name__ == "X"`. In prod, dispatch on `type(exc).__name__`, never `isinstance`.
9. **Enums are `StrEnum`:** every string-valued enum inherits `StrEnum`, never plain `Enum`. Guarantees value-based comparison + xdist namespace safety.
10. **Auth in tests via `create_app(token="...")`:** never monkeypatch `_ensure_token` or `_server_token`. The `token` parameter bypasses filesystem + global state.
11. **Prefer `patch.object` over string-path patches:** `patch("module.attr")` can resolve to different module objects under xdist or after refactors. `patch.object(imported_module, "attr")` is stable.
12. **Defense-in-depth in tests is a smell:** if 3 layers make a test pass, you don't know which one works. One layer understood > three mysterious. When a fix makes a workaround unnecessary, delete it.
13. **Plugins use `SandboxedHttpClient`, never raw `httpx`:** raw `httpx.AsyncClient(...)` from plugin code bypasses allowed-domains + rate-limit + size-cap, turning the sandbox into theater.
14. **Sync CPU-bound in `async def` blocks the event loop:** ONNX inference (Piper, Kokoro, Silero, Moonshine, OpenWakeWord), `boto3`, any blocking CPU/IO MUST be wrapped in `asyncio.to_thread(fn, *args)`.
15. **Unbounded `defaultdict(asyncio.Lock)` leaks memory:** one-lock-per-key uses `sovyx.engine._lock_dict.LRULockDict(maxsize=N)` so unused keys evict.
16. **God files (>500 LOC, mixed responsibilities) split into subpackage:** `__init__.py` re-exports public surface; underscore-prefixed sub-files are internal. Migrate test patches in the same commit (#20). Examples: `cognitive/safety/`, `cognitive/reflect/`, `voice/pipeline/`, `voice/capture/`, `dashboard/routes/`.
17. **Hardcoded tuning constants:** thresholds, timeouts, URLs, SHAs live in `EngineConfig.tuning.{safety,brain,voice}`. Module-level `_CONST = _TuningCls().field` keeps import-time access + `SOVYX_TUNING__*` env override.
18. **Raw `fetch()` in frontend:** every network call via `src/lib/api.ts` — `api.*` for JSON (auth+retry+timeout+schema), `apiFetch` for raw `Response`. Loose `fetch("/api/…")` drifts from auth injection + 401 handler.
19. **`localStorage` for auth tokens is XSS-exposed:** use `sessionStorage` (tab-scoped) + in-memory fallback (`src/lib/api.ts`). Boot-time migrator reads legacy `localStorage`.
20. **Test patches must follow module splits:** extracting a helper turns every `patch("old.module.X")` into silent no-op. Migrate paths in the same commit. Extends to: lazy `from X import Y` (#38); `caplog.set_level(logger=...)` widening; `patch.object(mod, "sys", ...)` across submodule boundaries.
21. **Windows capture APOs corrupt mic before PortAudio sees it:** Voice Clarity (`VocaEffectPack`/`voiceclarityep`) destroys Silero VAD input — max speech prob < 0.01 despite healthy RMS. Fix: WASAPI exclusive (`capture_wasapi_exclusive`) bypasses APO chain. Auto-detected (`voice._apo_detector`); auto-bypasses on deaf heartbeats (`voice_clarity_autofix=True`). Never tune VAD or add AGC — signal destroyed upstream. Surfaces: `sovyx doctor voice_capture_apo`, `GET /api/voice/capture-diagnostics`.
22. **Windows `time.monotonic()` ticks at ~15.6 ms without `timeBeginPeriod`:** `time.sleep(0.01)` can yield zero-tick delta. Timer-sensitive tests: sleeps ≥ 50 ms or fake clock; perf uses `time.perf_counter`. Linux sub-µs masks this on CI.
23. **`EngineConfig.data_dir` defaults to `~/.sovyx`; bootstrap re-seeds env from it:** `bootstrap()` reads `<data_dir>/{channel,secrets}.env` into process env. Tests MUST pass both `data_dir=tmp_path` AND `database=DatabaseConfig(data_dir=tmp_path)`. Use `monkeypatch.delenv` (auto-restored), not `os.environ.pop`. Auto-detect checks 9 cloud-LLM keys (`ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `GOOGLE_API_KEY`, `XGROK_API_KEY`, `DEEPSEEK_API_KEY`, `MISTRAL_API_KEY`, `GROQ_API_KEY`, `TOGETHER_API_KEY`, `FIREWORKS_API_KEY`).
24. **Strict `>` on `time.monotonic()` deadlines is silently wrong on coarse clocks:** when `now` and deadline share a tick, `>` never fires (`ttl_sec=0` never expires). Prefer `>=` — inclusive + coarse-safe.
25. **Frame-typed pipeline is observability, NOT state-machine rewrite (Hybrid Option C):** `PipelineFrame` + 8 subclasses in `voice/pipeline/_frame_types.py` instrument transitions/cancellations; authoritative state stays in `VoicePipelineState` + flags. Frames → 256-entry ring buffer via `PipelineStateMachine.record_frame`; surfaced at `GET /api/voice/frame-history`. Never couple prod logic to frame presence. Full Pipecat rewrite deferred to v0.24.0+.
26. **KB profile signing — dev key in repo, prod rotation via HSM:** `voice/health/_mixer_kb/_trusted_keys/v1.pub` is dev. Private at `.signing-keys/sovyx_kb_v1.priv` is gitignored + STAYS LOCAL. Loader `Mode.LENIENT` v0.23.x; flips `STRICT` after one minor cycle. Prod: HSM-backed (YubiKey/AWS KMS/GCP Cloud KMS), multi-key trust store w/ overlapping windows. Procedure: `docs/contributing/voice-kb-rotation.md`.
27. **`contextlib.suppress` + `logger.debug(..._skipped, reason=…)` is the canonical "intentional ignore":** replaces raw `try/except: pass` for benign failures. Explicit intent + observability, debug-stripped in prod. Reject: silent suppression w/ no log; WARN floods; raising errors callers can't handle.
28. **Cold probe MUST validate signal energy, not callback count (Furo W-1):** APOs leave PortAudio callbacks firing while delivering exact-zero PCM. v0.24.0: `_diagnose_cold` reads `rms_db`; strict mode returns `Diagnosis.NO_SIGNAL` when `rms_db < probe_rms_db_no_signal`; lenient emits `voice.probe.cold_silence_rejected`. **Generalizes:** any acceptance gate downstream of a real-world signal source MUST verify the signal itself, not just the wrapping mechanics.
29. **`CaptureRestartFrame` is observability, NOT state-machine rewrite (sibling of #25):** every restart method emits a frame BEFORE the ring-buffer epoch increments; orchestrator records via `PipelineStateMachine.record_frame`. Surfaced at `GET /api/voice/restart-history`. Schema fields stay `.optional()` for one minor cycle.
30. **`psutil.open_files()`/`net_connections()` hang during async teardown on Windows:** psutil iterates kernel handles + `os.stat()` per handle; closing handles cause indefinite blocks — `try/except` catches exceptions, NOT blocked syscalls. CI symptom: 6+ min timeout in `_capture_psutil_metrics`. Fix: `skip_expensive: bool` kwarg on metrics-emit path. Site: `observability/resources.py::_capture_psutil_metrics` + `_emit_snapshot(final=True)` (`003a63f`). **Generalizes:** shutdown hooks MUST avoid handle-iterating syscalls or wrap in `asyncio.wait_for` w/ strict deadline.
31. **Perf gate p99 ratio is tail-sensitive — even median-of-5 can flake under shared-runner contention:** `scripts/check_perf_regression.py` aggregates `bench_observability.py` across N runs and enforces `async/minimal ≤ 2.0×` and `redacted/minimal ≤ 3.0×` ratio budgets. **Triage:** if `git diff` doesn't touch `observability/logging.py`, `_async_handler.py`, or the structlog chain, prior is contention — confirm by comparing the same commit's parallel `CI / Perf Regression Gate` vs `Publish to PyPI / CI Gate / Perf Regression Gate`; matching split = contention, both-fail = real regression. **Escalation ladder (each layer added when the previous flaked again):** (a) v0.27.0 single-shot → median-of-3 (canonical `cargo bench` recipe); (b) v0.45.7 median-of-3 → median-of-5 PAIRED with `perf-regression-gate-global` concurrency group in `ci.yml` (serialises the parallel `publish.yml` + `ci.yml` workflow_call instances on the same self-hosted host); (c) v0.49.34 median-of-5 → `_DEFAULT_REPEATS=7` + `_trimmed_mean` aggregation (drop max + min, average inner 5) — survives up to 2 noisy runs of 7 vs median-of-5's 2-of-5 break-point. **Next layer if it recurs:** bump `_TRIM_COUNT` 1 → 2 (drop top-2 + bottom-2, average inner 3 of 7) BEFORE touching the budgets — recalibrating 2.0× / 3.0× risks masking real `AsyncQueueHandler.enqueue` regressions which would otherwise present as 5–10× spikes.
32. **Mixin stubs silently shadow real methods later in MRO:** `def foo(self) -> None: ...` on `MixinA` is a real method (the `...` body returns `None`) and wins MRO over the real `foo` on `MixinB`. Patterns: (a) target BEFORE caller in MRO → naked stub fine; (b) target AFTER caller → declare cross-mixin reference inside `if TYPE_CHECKING:` (erased at runtime → MRO falls through). See `voice/capture/_loop_mixin.py`.
33. **Per-mind config from RPC handlers: best-effort YAML, never assume registry methods exist:** `MagicMock`-typed `registry.resolve(...).method(...)` returns `Any` and masks `AttributeError` at test time → prod blows up. Before `await registry.resolve(X).method(y)`, grep `class X:` for `def method`. Privacy-sensitive paths (retention) MUST fall through to global defaults on malformed config — compliance > perfect resolution. Ref: `_load_mind_config_best_effort` in `engine/_rpc_handlers.py`.
34. **Schedulers with kill-switch flags default OFF + skip instantiation when disabled:** default-OFF = default-ABSENT, not default-PRESENT-but-no-op. Bootstrap: `if config.X.enabled: register_instance(...)`. Lifecycle: `if registry.is_registered(X): start ...`. Applied: ConsolidationScheduler/DreamScheduler/RetentionScheduler.
35. **Cross-layer config defaults are sentinels, not values:** `VoicePipelineConfig.mind_id: str = "default"` is a sentinel callers MUST overwrite; every caller path that omits it is a silent bug. Prior: voice pipeline launched under phantom `"default"` because `dashboard/routes/voice.py` read `getattr(request.app.state, "mind_id", "default")` while no production code ever assigned `app.state.mind_id`. Patterns: (a) **make field required** (preferred for NEW fields); (b) **detect sentinel at top wire-up + structured WARN** for already-shipped sentinels (`voice/factory/__init__.py`, `dashboard/_shared.resolve_active_mind_id_for_request`). **Recurring — surfaced 5+ times in voice flow.** Frontend: `useResolvedMindId` hook + ESLint rule.
36. **`patch.object` on async functions auto-detects `AsyncMock`** (Python 3.8+ uses `iscoroutinefunction`); string-path `patch` follows the same autodetect at import time. Prefer `patch.object(module, "name", return_value=X)` over `patch("path", new_callable=AsyncMock, return_value=X)`.
37. **Cryptographic verifier verdict ordering — cheapest + most-common-failure first:** in `_persistence.py::_verify_calibration_signature` 5-way verdict, order: (1) `pubkey is None` (else `pubkey.verify` crashes); (2) `signature is None`; (3) signature shape malformed (b64 invalid OR length != 64); (4) actual `pubkey.verify` (expensive).
38. **Lazy `from X import Y` inside a function body invalidates module-level patches:** the lazy import resolves on the SOURCE module at call-time, not on the caller's top-level binding. Patch `X.Y` (source attr), NOT `caller.Y`. Mixed: a single test may patch BOTH `caller.eager_attr` AND `source.lazy_attr`. Extends #20. **Cross-platform corollary:** when production references a POSIX-only attribute (`signal.SIGKILL`, `os.killpg`), Windows tests patching `sys.platform="linux"` MUST also `patch.object(target, "ATTR", value, create=True)`.
39. **Probe-verdict misrouting + cross-platform event-name drift.** Two paired subrules.

    **(a) Verdict-disjoint remediation.** Acceptance gates + remediation routers MUST consume the probe **verdict** (categorical), not the wrapping symptom. `vad_mute` (user silent) vs `no_signal` (driver silent) are orthogonal — same ladder loses working hardware. Sibling of #28. v0.44.0 restored disjoint dispatch w/ `assert_never`. LENIENT corollary (`c5791e40`): new verdict-disjoint field → every consumer MUST consult new field first w/ fallback to legacy — bare legacy reads silently disable dispatch. Mission: `MISSION-c1-vad-mute-reclassification-2026-05-14.md`.

    **(b) Cross-platform event-name drift.** Event names MUST be neutral; platform terminology (`apo.*`, `wasapi.*`, `dsound.*`) MUST be `sys.platform`-gated or behind a neutral wrapper. Event names are public API. Sibling of #21. **Closure: #45 at v0.51.0 + Quality Gate 13 STRICT.**
40. **Typed response boundary drifts from producer dict shape when both evolve independently:** `Model.model_validate(helper_dict)` at a route boundary is only as strict as the LAST round-trip test exercising the producer's prod shape. `extra="allow"` on response models is load-bearing for forward-additive evolution; pair w/ producer→boundary round-trip test or drift escapes CI. Reference: Mission C2 — `VoiceStatusResponse.capture.input_device` narrowed `str|None` (`aee85844`) while producer emitted `int|str|None`; every `/api/voice/status` 500'd until widened at `00cb6e72`. Quality Gate 8 AST-enforces pairing on `routes/voice.py`. Mission: `MISSION-c2-voice-status-response-contract-2026-05-16.md`.
41. **Candidate-list dispatch MUST iterate the full list within a single attempt window before collapsing to a fallback.** Single-shot dispatch is anti-shape: cross-invocation cooldowns block retries, upstream latches terminal, downstream loses per-candidate observability. Reference: Mission C3 — `_runtime_failover.py:109` single-shot left Razer USB mic quarantined 3600s while two healthy candidates were never tried. **Pattern:** loop-in-place w/ per-candidate cooldown + per-ladder cap + telemetry (`voice.failover.candidate_{attempted,failed,skipped}` + `ladder_id`) + shared exclusion set + `ProbeResultCache.is_known_unopenable` pre-dispatch. Quality Gate 9 AST-rejects functions taking `candidates`/`targets`/`entries`/`endpoints` that dispatch outside a loop. **Generalizes:** bridge channels, plugin loaders, retry-broker routers. **Sibling of #39(a):** #39 routes to a ladder; #41 iterates within it. Mission: `MISSION-c3-failover-ladder-iteration-2026-05-16.md`.
42. **Operator-actionable degraded state MUST be surfaced through a single composite store/endpoint, never N independent log lines.** Detection site MUST: (a) emit existing structured log line (playbooks reference these); AND (b) call `EngineDegradedStore.record(DegradedEntry(...))` so banner + doctor + `/api/engine/degraded` all see state without log-grep. Reference: Mission C4 — v0.43.1 emitted three WARNs over 12 min w/ ZERO dashboard surface. **Pattern:** `record` at every emit site (axis + reason + severity + i18n + action chips); composite `/api/engine/degraded`; severity escalation by axis count (1=warn, 2=error, 3+=critical); server-side ack in `operator_acks` SQLite (never client storage, #19); TTL re-surface; auto-recovery governor w/ bounded retry budget. Quality Gate 10 AST-rejects `*_degraded`/`no_*_provider`/`*language_{coerced,unsupported}` WARNs lacking paired `record_*`/`clear_axis`. Allowlist: `# c4-allowlist: <rationale>`. **Generalizes:** bridge channels, plugin sandbox, persistence pool, brain embedding. **Sibling of #40, #41.** Mission: `MISSION-c4-degraded-mode-banner-2026-05-17.md`.
43. **Static-asset distribution contracts MUST be enforced at THREE independent points:** (a) build-time AST scan — every `<script src=...>` / `<link href=...>` in SPA `index.html` matches a hashed-chunk in the bundle; (b) install-time `create_app()` boot scan via `_integrity.scan_bundle_integrity()` against installed wheel's `static/`; (c) runtime composite-banner via `EngineDegradedStore.record(axis="dashboard", ...)` — never a raw 404 cascade. **Pattern:** stdlib AST scanner → `BundleVerdict{FULLY_PRESENT,PARTIAL,MISSING,ASSETS_DIR_ABSENT,INDEX_HTML_ABSENT}` → composite-store (`partial=error`, `missing=critical`) + dual-emission window during LENIENT (ADR-D14) + reactive on-404 debounce ≥60s in `_IntegrityAwareStaticFiles(StaticFiles)`. Allowlist: `# c5-allowlist: <rationale>`. Quality Gate 11 STRICT in `publish.yml`; **LENIENT in `verify_gates.sh` until v0.48.0 — STRICT-flip operator-gated on V-C5-7**. Reference: Mission C5 — v0.43.1 22-byte JSON 404 cascade on `dashboard-BLNxX04a.js` w/ ZERO banner. **Generalizes:** ONNX weights, signing-key trust store, locale JSON, plugin manifest. **Sibling of #15, #26, #34, #42** (producer-side of #42 for dashboard-axis). Mission: `MISSION-c5-dashboard-distribution-integrity-2026-05-17.md`.
44. **Workers whose primary function depends on an external dependency (LLM router, embedding model, classifier weights, signing-key store, plugin sandbox, persistence pool) MUST verify at `start()`, emit `started_in_degraded_mode` + composite-store entry when absent, AND gate every iteration on the dependency.** A worker firing `started` w/ broken dep produces invisible no-op work. Reference: Mission C6 — v0.43.1 `cognitive_loop_started` ran 439s w/ zero perception events because `llm_router._providers = []`. **Pattern:** start-time dep-check WARN + `EngineDegradedStore.record(axis=<dep_axis>)`; per-iteration `asyncio.Event` gate w/ `wait_for(..., timeout=1.0)` + throttled WARN ≤1/min; synthetic fail-fast on inbound (`failed=True, reason="<dep>_dependency_missing"`) gated by default-True `<worker>_degraded_mode_fail_fast`; ONE liveness probe task per dep (#15); kill-switch defaults always-on (inverse of #34). Quality Gate 12 STRICT in `publish.yml`; **LENIENT in `verify_gates.sh` until v0.50.0 — STRICT-flip operator-gated on V-C6-11**. **Strict:** per-iteration gating is NOT optional. **Generalizes:** schedulers, bridge channels, plugin invokers, TTS workers. **Sibling of #14, #15, #25/#29, #34, #41, #42, #43.** Mission: `MISSION-c6-llm-provider-cognitive-loop-integrity-2026-05-18.md`.
45. **Platform-specific event names MUST be EITHER (a) emitted from a `sys.platform`-gated block with platform-name in the suffix, OR (b) wrapped in a neutral cross-platform event carrying the platform token in `voice.platform` / `voice.<subsystem>_family` metadata.** Raw platform terminology (`apo.*`, `wasapi.*`, `dsound.*`, `pulseaudio.*`, `pipewire.*`, `coreaudio.*`, `voice_clarity_*`, `module_echo_cancel_*`, `voice_isolation_*`) without a platform gate creates operator-triage drift. **Pattern:** neutral wrapper helper `_capture_integrity_emit.py` takes platform-token + neutral-event-name + metadata, emits neutral + (during LENIENT per ADR-D14) legacy; STRICT-flip drops legacy. `sys.platform`-gated debug emits permissible inside `# h2-allowlist: <rationale>`. Event names are public API — renames break observability. Quality Gate 13 STRICT in `publish.yml`; **LENIENT in `verify_gates.sh` until v0.51.0 — STRICT-flip operator-gated on V-H2-11**. **Closure of #39(b).** **Sibling of #21, #40, #43.** Mission: `MISSION-h2-platform-neutral-event-naming-2026-05-18.md`.
46. **Quarantine reason values (and any operator-actionable acceptance-gate enum field) MUST be resolved through a single-source-of-truth verdict→reason map w/ exhaustive `assert_never` coverage.** String-literal reasons at call sites create silent failure-class drift, misroute rechecker filters + i18n + composite-banner action chips. SSoT lives in a leaf module re-exported via package `__init__.py`; consumers import the `StrEnum`, never hand-write the string. **Pattern:** SSoT `voice/health/_quarantine_reasons.py` exposes `QuarantineReason(StrEnum)` (8 members: `apo_degraded`, `vad_frontend_dead`, `silent_capture`, `endpoint_open_failed`, `endpoint_unconfigured`, `capture_dead`, `host_api_unhealthy`, `unclassified`) + 2 exhaustive `match`+`assert_never` resolvers + 3 classifiers; producer: `_quarantine.add(reason=QuarantineReason.X)`; consumer recheck filters compare via enum; boundary `routes/voice.py` types `reason: QuarantineReason` (per #40; zod twin `z.nativeEnum`); i18n `degraded.voice.quarantine.<reason>` w/ catch-all `unclassified` fallback. Allowlist: `# h3-allowlist: <rationale>`. Quality Gate 14 STRICT in `publish.yml`; **LENIENT in `verify_gates.sh` until v0.53.0 — STRICT-flip operator-gated on V-H3-11 (absorbs C1 Phase 4 `derived_reason` field drop)**. **Strict:** resolver MUST cover all verdict-enum members via `match`+`assert_never` (new verdict without resolver update = hard mypy failure). **Generalizes:** failover ladder verdict tokens, bridge channel disconnect reasons, plugin sandbox quarantine reasons. **Sibling of #39(a), #40, #42.** Mission: `MISSION-h3-quarantine-reason-verdict-map-2026-05-18.md`.
47. **Resource-cohort instrumentation MUST cover every cardinality-bounded shared resource (ONNX `InferenceSession`, asyncio default-executor thread-pool, `LRULockDict` per-owner cardinality, `ExceptionGroup` chain-depth + retained-bytes, gc per-gen counts, tracemalloc current/peak); consumer field-names MUST match producer field-names.** SSoT is `_HEALTH_SNAPSHOT_FIELDS` in `_resource_registry.py`. Gate 15 rejects if (a) snapshotter emits field absent from SSoT, (b) consumer reads field absent from SSoT, (c) `LRULockDict(...)` / `ort.InferenceSession(...)` construction not paired w/ `ResourceRegistry.register_*`. **Pattern:** SSoT registry `observability/_resource_registry.py` (`_HEALTH_SNAPSHOT_FIELDS: frozenset[str]` + `ResourceRegistry` singleton w/ weakref tracking + per-label `to_thread` counter + `CohortAxis(StrEnum)`); wrapped dispatch `_thread_dispatch.dispatch_to_thread(label="<ctx>.<op>", ...)` auto-increments cohort counter (Gate 15 INFORMATIONAL on bare `asyncio.to_thread` even POST-STRICT — mass-rename deferred to `MISSION-asyncio-to-thread-cohort-labeling-FUTURE.md`); canonical-key consumer `anomaly.py` reads `process.rss_bytes` w/ LENIENT fallback to `system.rss_bytes` (STRICT-flip v0.54.0 drops fallback — closes silent-dead `anomaly.memory_growth_spike` where v0.43.1's +1.1 GB RSS Δ never fired because consumer read wrong key); cohort governor `evaluate_snapshot()` returns 5 verdicts (`RSS_GROWTH`, `THREAD_COUNT`, `LOCK_DICT_CARDINALITY`, `ONNX_SESSION`, `EXCEPTION_COHORT`) → `EngineDegradedStore.record(axis="engine_resources", ...)` (5th C4 axis per ADR-D5); heap-snapshot `tracemalloc=True` opt-in (25-30% overhead) → `~/.sovyx/diagnostics/heap-snapshot-<ts>.json` on N=5 deaf-cluster coupling on `voice.deaf_warnings_consecutive`. Allowlist: `# h4-allowlist: <rationale>` for `lifecycle-bootstrap` + `legacy-alias`. Quality Gate 15 STRICT in `publish.yml`; **LENIENT in `verify_gates.sh` until v0.54.0 — STRICT-flip operator-gated on V-H4-13**. **Strict:** every producer↔consumer field-name pair MUST be SSoT-routed. **Generalizes:** file handles, sockets, DB connections, GPU memory, embeddings cache. **Sibling of #15, #30, #34, #40, #42, #43.** Mission: `MISSION-h4-resource-hygiene-instrumentation-2026-05-19.md`.
48. **Falsifiability gates verify literal-string parity, NOT semantic correctness.** A producer that aliases one field to another to satisfy a spec's literal field-name requirement passes Gate 15 (or any AST scan) but emits a semantic lie — the field name promises behavior the runtime cannot deliver. Reference: Mission A.1 F-006 — `to_thread.active_workers` was emitted as a literal alias of `pool_size` because Python's `ThreadPoolExecutor` has no per-worker busy/idle metric. ADR-D4 explicitly chose the alias "so the falsifiability gate passes literally." A regression test even enforced `active_workers == pool_size` as a contract, encoding the lie. **Pattern:** when a spec demands a field the runtime cannot deliver authentically, EITHER (a) change the spec, OR (b) leave the field unimplemented and document the gap. **Aliasing-to-pass is FORBIDDEN** — the gate is necessary-not-sufficient; semantic verification belongs in operator-trust tests + ADRs, not in AST scans. If a temporary alias must ship for spec-compliance reasons, EVERY test asserting alias equality MUST be classified explicitly as a SUNSET-WINDOW contract (with sunset version named in the test docstring) so future fixes that distinguish the values don't get reverted to make the test pass. ADR-D15 supersedes ADR-D4. **Closure of MISSION-A.1 F-006.** **Sibling of #49 (counter naming), #50 (self-observing fields), #52 (comment-vs-code filter).** Mission: `MISSION-A1-runtime-truth-remediation-2026-05-20.md`.
49. **Counter fields named `*_estimate` or `*_count` MUST decay or be explicitly suffixed `_lifetime_*` / `_cumulative_*`.** A monotonic `+=` accumulator emitted under a name like `retained_bytes_estimate` violates operator expectation that the field is a point-in-time gauge. Reference: Mission A.1 F-002 + F-003 — `exception_cohort.retained_bytes_estimate` at `_resource_registry.py:632` was `+=` with no decay; `distinct_group_ids` was an unbounded `set`. The cohort governor compared the lifetime sum against a real-time cap → after a single ExceptionGroup storm the verdict became permanently BUDGET_EXCEEDED with no natural recovery path (no production callers existed at HEAD `eaec56dc` so the bug was latent; Phase 1.E wiring would have activated it). **Pattern:** every cardinality-bounded resource emits TWO fields — `<resource>.cumulative_*` (monotonic since process start; forensic-only) AND `<resource>.window_*` (rolling, sized by `tuning.<resource>_window_s`; decays as observations age out of the bounded deque). The cohort governor reads the WINDOW field. Legacy pre-split keys may LENIENT-dual-emit for one minor cycle via the SSoT `legacy_alias=` mechanism + a snapshotter-side `# a1-allowlist: legacy alias, sunset vX.Y.Z` annotation (matches the `system.rss_bytes` precedent — legacy emit lives in the snapshotter, not in the registry's `snapshot_fields()`). STRICT-flip drops the legacy keys at the next minor cycle. **Detection:** integration test that records observations, advances clock past `window_s`, asserts the windowed field decays to 0 while the cumulative field persists; second test that constructs a snapshot with `window_*=tiny` + legacy `*_estimate=huge` and asserts the governor verdicts HEALTHY (proves it reads the window, not the legacy). **Closure of MISSION-A.1 F-002 + F-003.** ADR-D14. **Sibling of #47 (resource-cohort SSoT), #52 (comment-vs-code filter inversion).** Mission: `MISSION-A1-runtime-truth-remediation-2026-05-20.md`.
50. **Self-observing fields MUST declare themselves; OBSERVATION PARADOX is forbidden.** A field captured from inside the observer that returns state of the observer instead of the observed system is a lie. Reference: Mission A.1 F-005 — `asyncio.current_running_task_name` was captured via `asyncio.current_task()` from inside the `ResourceSnapshotter.run()` coroutine; from that vantage point `current_task()` is always the snapshotter task itself. The field promised "correlate snapshot ticks to specific coroutines"; the field delivered "this snapshot was emitted by the snapshotter". **Pattern:** every "current X" / "active Y" field captured from inside the observer MUST EITHER (a) prefix its name with `observer.` / suffix with `_at_observer_time` to declare the paradox, OR (b) be replaced with a list-typed snapshot of the OTHER tasks/sessions/whatever (e.g. `asyncio.all_task_names: list[str]` capped to a bound). **Detection:** any field whose value, sampled across many snapshots in production, never varies despite varying workload is a candidate observation paradox — flag via a property test that asserts the field's value distribution under synthetic workload diversity. **Closure of MISSION-A.1 F-005.** ADR-D15. **Sibling of #48 (falsifiability gate semantic gap).** Mission: `MISSION-A1-runtime-truth-remediation-2026-05-20.md`.
51. **Twin-name fields whose values differ by freshness or math semantics MUST disambiguate via suffix.** Pre-fix Mission A.1 emitted `to_thread.pool_size` (STALE — recorded at last `dispatch_to_thread()` call) AND `asyncio.default_executor_state.pool_size` (LIVE — read via `len(executor._threads)` at snapshot time): twin-named, divergent freshness, no operator-facing disclosure. Same class: `asyncio.running_count` counted `not t.done()` (includes await-blocked tasks) under a name implying "executing on the loop step." Operators reading the field can't tell which is "true"; dashboards rendering both look incoherent. **Pattern:** freshness divergence → suffix `_at_<event>` on the stale side (`pool_size_at_last_dispatch`, `max_workers_at_last_dispatch`, `queue_depth_at_last_dispatch`); math-vs-name divergence → rename to match the math (`not_done_count`, `awaiting_count`). Legacy keys LENIENT-emitted by the snapshotter (NOT by registry's `snapshot_fields()` — matches the `system.rss_bytes` precedent) for one minor cycle via the SSoT `legacy_alias=` mechanism + `# a1-allowlist: legacy alias, sunset vX.Y.Z`; STRICT-flip drops them. **Detection:** (a) `_HEALTH_SNAPSHOT_FIELDS` audit — any two field keys sharing a base name across cohort namespaces are a candidate twin pair; verify source paths converge in freshness OR add `_at_<event>` suffix on the stale side. (b) Math-vs-name property test — synthetic workload of N awaiting tasks; "executing" field MUST NOT scale with N. **Closure of MISSION-A.1 F-007 + F-014.** ADR-D16. **Sibling of #48 (falsifiability gate semantic gap), #49 (counter naming), #50 (self-observing fields).** Mission: `MISSION-A1-runtime-truth-remediation-2026-05-20.md`.
52. **Comment-vs-code mismatch in filter logic is a P0 incident class.** A list comprehension or filter predicate of the shape `[x for x in collection if ts <= window_start]` followed by a comment claiming "oldest in-window" (or vice versa) silently inverts the contract: the code selects samples *outside* the window while the consumer assumes in-window semantics. Producer/consumer pairs that share a window (`window_start = now - window_s`) MUST use consistent direction (`ts >= window_start` for in-window; `ts < window_start` for strictly-before). Anti-pattern: the comment promises one semantics and the predicate silently delivers the opposite — no type-checker, ruff, or mypy will catch it. Reference: Mission A.1 F-001 — `anomaly.py:331` filtered `ts <= window_start` (pre-window samples) while the inline comment claimed "oldest in-window snapshot"; the corresponding governor at `_resource_cohort_governor.py` used `ts >= window_start` (correct). `anomaly.memory_growth` event therefore fired against a baseline up to deque-maxlen × snapshot-interval old (~20 min default) instead of `_memory_window_s` (60s default), producing false negatives on bursts and false positives on slow steady growth. **Detection:** baseline-age assertions in regression tests (assert `baseline_age_s ≈ window_s ± tick_jitter`) flag inverted filters mechanically. **Pattern:** when a `ts <`/`ts <=`/`ts >`/`ts >=` predicate appears, the adjacent comment MUST describe the SET being selected (e.g., "in-window: samples taken within last `window_s` seconds") and a test MUST anchor the predicate's direction via an observable downstream field. **Closure of MISSION-A.1 F-001.** **Sibling of #40 (typed boundary drift).** Mission: `MISSION-A1-runtime-truth-remediation-2026-05-20.md`.
53. **HTTP path strings constituting the producer↔consumer contract MUST round-trip through ONE shared symbol — never independent literals on each side.** Frontend `api.{post,put,patch,delete}("/api/...")` + server `@router.post("...")` are two strings; if they drift, both sides' unit tests pass (each tests its own literal) but the actual contract is broken. Reference: Mission B B-P0-1 — 16 sites across server / frontend / tests / docs / CHANGELOG / mission spec all agreed on `/api/voice/degraded/ack`; FastAPI registered only `/api/engine/degraded/ack`; the ack feature was structurally inert v0.46.4..v0.49.36; `.catch(() => {})` in the calling mounted component swallowed every 404. **Pattern:** every frontend POST/PUT/PATCH/DELETE call site MUST be exercised by an integration test against `TestClient(create_app())` asserting `status_code != 404`, OR import the path from a shared constants module also asserted-against by a Python boundary test enumerating `app.routes`. Frontend vitest assertions of the shape `mockApiPost.toHaveBeenCalledWith(...)` verify INTENT only, not contract. GET endpoints are partially shielded because their callers' zod safeParse fails on a 404 HTML body; POST/PUT/PATCH/DELETE need an explicit gate because their callers commonly swallow errors via `.catch(() => {})`. **Detection:** scan `dashboard/src/**/*.{ts,tsx}` for `api.{post,put,patch,delete}("/api/...")` literals; assert each maps to a registered route on `app.routes` from a seeded `create_app()`. **Closure of Mission B B-P0-1.** **Sibling of #40 (typed boundary schema drift) and #45 (event-name drift across platform consumers).** Mission: `MISSION-B-REMEDIATION-PLAN-2026-05-21.md` §11.
54. **Every `EngineDegradedStore.record()` call site MUST have a paired clear-edge (`clear_reason()` per-reason preferred, or `clear_axis()` axis-scoped) tied to a verifiable HEALTHY verdict.** Record-without-clear leaves stale operator-actionable banner entries past their relevance; this is the consumer-side recreation of #49's producer-side "permanently breached" pathology. Reference: Mission B B-P0-3 — the cohort governor's class docstring at `_resource_cohort_governor.py:222-228` promised "HEALTHY clears prior engine_resources.<axis> entries"; `emit_axis_entries` at line 589 silently skipped every non-BUDGET_EXCEEDED verdict; banner stuck-at-breach forever; every multi-mind installation (3 minds × 5 ONNX = 15 > 8 soft_cap) saw a permanent banner that the broken B-P0-1 ack endpoint could never dismiss. **Pattern:** categorical recovery edges (verdict-driven like LLM/dashboard) clear on the first HEALTHY tick; numeric-threshold-driven producers (cohort governor) clear after N consecutive HEALTHY ticks (hysteresis prevents flicker on threshold-adjacent workloads; default `tuning.cohort_clear_consecutive_healthy_threshold=3`). The clear path SHOULD also clear the matching `OperatorAcksStore` row — else a re-breach within the original ack TTL is silently suppressed (B-P1-15). Feature-flag-gated (`observability.features.cohort_axis_auto_clear` default True per anti-pattern #34 INVERSE — the clear IS the operator-trust feature; default-False would ship the bug forever). Allowlist `# b-p0-3-allowlist: <reason>: <justification>` when a producer genuinely owns no recovery edge (e.g. terminal failure modes). **Detection:** AST scan over `src/sovyx/**/*.py` for `EngineDegradedStore.record(DegradedEntry(...))` call sites; walk the same module's symbol table for a corresponding `clear_reason(<same_reason>)` or `clear_axis(<same_axis>)` call. **Closure of Mission B B-P0-3.** **Sibling of #42 (producer mandate — composite store as single operator-actionable surface) and #49 (counter naming — same "permanently breached" pathology one layer down).** Mission: `MISSION-B-REMEDIATION-PLAN-2026-05-21.md` §5.

## Testing Patterns

```python
# Test class naming — TestFeatureName w/ short docstring; per-test docstring states expected behavior.
# Async tests: asyncio_mode=auto in pyproject; per-test @pytest.mark.asyncio() optional.

# File handler cleanup fixture (autouse) — yields then closes all RotatingFileHandler on root
@pytest.fixture(autouse=True)
def _clean_handlers() -> Generator[None, None, None]:
    yield
    root = logging.getLogger()
    for h in list(root.handlers):
        if isinstance(h, logging.handlers.RotatingFileHandler):
            h.close()
    root.handlers.clear()

# Auth in dashboard/API tests — use token parameter, never monkeypatch
_TOKEN = "test-token-fixo"

@pytest.fixture()
def app() -> FastAPI: return create_app(token=_TOKEN)

@pytest.fixture()
def client(app: FastAPI) -> TestClient:
    return TestClient(app, headers={"Authorization": f"Bearer {_TOKEN}"})

# Exception assertions — xdist-safe (see #8)
with pytest.raises(Exception) as exc_info:
    do_something_that_raises()
assert type(exc_info.value).__name__ == "LLMError"

# Mocking SandboxedHttpClient plugins — internal call is ._client.request(METHOD, url, ...), NOT .get().
# Wire MockClient.return_value to the mock (NOT the async-with __aenter__ path).
with patch("httpx.AsyncClient") as MockClient:
    mock_client = AsyncMock()
    mock_client.request = AsyncMock(return_value=mock_resp)
    mock_client.aclose = AsyncMock()
    MockClient.return_value = mock_client
    result = await my_plugin_func()

# Aliased imports (#2): patch real module, not sys.modules
import onnxruntime
with patch.object(onnxruntime, "InferenceSession", return_value=mock_sess): ...

# After a module split (#20): patch the NEW path
with patch("sovyx.brain._model_downloader.httpx.AsyncClient", ...): ...
```

## Debugging Rules

1. **Audit first.** Grep the codebase for ALL instances of the same pattern. Map the size before solving any single instance.
2. **Group by root cause.** If 28 tests fail, find how many distinct root causes. Fix causes, not symptoms.
3. **Don't band-aid.** If you can't explain WHY a fix works, it's not ready.
4. **One commit per root cause.** No partial pushes to CI for incremental testing.
5. **No shotgun debugging.** If setting the same value in 3 places hoping one sticks, stop and trace the actual read path.
6. **Local suite before push.** Each CI round-trip wastes minutes and fragments reasoning.
7. **Check the full chain.** A config bug might affect CLI, dashboard, and API.
8. **Write regression tests.** The bug must never recur.
9. **Third fix→push→CI-fail cycle = STOP.** The approach is wrong. Step back, reassess.
10. **Windows mypy noise:** local `uv run mypy src/` reports 9 platform-specific false positives (`AF_UNIX`, `os.sysconf`, `getrusage`, `open_unix_server`). Only errors OUTSIDE that list are real. CI runs Linux — the true baseline.
11. **Closure protocol on a bug class.** When fixing one site, grep ALL consumers of the same flag/sentinel before declaring the fix complete. State the closure assertion in the commit body. Bug classes surface in waves; each unaudited consumer is the next RC.

## Working Style

**On any task:** (1) read scope + dependencies; (2) check existing patterns; (3) follow conventions above; (4) tests ≥95% coverage on modified files + edge cases; (5) run `./scripts/verify_gates.sh`; (6) conventional commit, body explains WHY.

**When modifying tests:** never introduce workarounds (if a test needs patching to pass, production may need a better interface — e.g. `create_app(token=...)` over monkeypatch); prefer explicit parameters over mocking (DI > monkeypatch); one assertion pattern (xdist-safe form #8); remove dead code — if a fix makes a workaround unnecessary, delete it in the same commit.

**When splitting a god file:** public surface stays stable (`__init__.py` re-exports); one responsibility per underscore-prefixed sub-file; migrate tests in the same commit — old `patch("old.module.X")` becomes silent no-op (#20); preserve the public docstring on `__init__.py` if the class was the module's face.

## Deploy Flow

1. Bump `version` in `pyproject.toml` (single source — `src/sovyx/__init__.py` reads via `importlib.metadata.version`).
2. `uv lock` (CI enforces `uv lock --check`).
3. `git commit` + `git tag vX.Y.Z` + `git push origin main` + `git push origin vX.Y.Z`.
4. Tag triggers `publish.yml`: CI gate → dashboard build → `uv build` → PyPI (OIDC) → GitHub Release → Docker (parallel).
5. CI fail on tagged commit: fix + commit + re-tag (`git tag -d vX.Y.Z && git tag vX.Y.Z && git push origin vX.Y.Z --force`).

Per `feedback_ci_watching`: don't `gh run watch` after tag push — operator surfaces failures via the validation backlog.

### Two-Tier GA Strategy (voice subsystem)

Per `MISSION-voice-final-skype-grade-2026.md`: **v0.30.0** = single-mind GA (Phases 1-7: cold-probe, bypass tiers, telemetry/IMM listener, multi-platform). **v0.31.0** = FINAL multi-mind GA (Phase 8: per-mind wake word, voice ID, language, accent, cadence). Phase 8 work goes into v0.30.x patches or directly v0.31.0 — never blocks v0.30.0 release.

## Mission Lifecycle

Multi-version work is coordinated via long-running structured missions.

- **Active:** `docs-internal/missions/MISSION-*.md` with task IDs (T1.1…) + Phase boundaries mapped to versions.
- **ADRs** at `docs-internal/ADR-*.md` are CANONICAL — referenced from code docstrings. Never delete; supersede via new ADR referencing the old.
- **Completed/superseded** missions archive to `docs-internal/archive/missions-completed/` w/ `## Archive Footer` (status, code refs, predecessor/successor). Update `archive/INDEX.md`.
- **Forensic resolution docs** → `docs-internal/archive/forensics-resolved/` w/ same footer.
- **Never delete** a mission or ADR that produced shipped code — reference value > cleanliness. Pure orphans (no-code-produced, byte-identical dupes) are the only valid DELETE targets.

When closing a mission task in a commit, reference the mission file + task ID in the body (e.g. `Mission: docs-internal/missions/MISSION-voice-final-skype-grade-2026.md §Phase 1.T2`) and update the mission spec to mark ✅ in a follow-up `docs(mission):` commit.

## Deep Reference

- Public docs (MkDocs): `docs/` — architecture, getting-started, configuration, api-reference, security, `docs/modules/`.
- Internal planning + audits + specs (IMPL/SPE/ADR): `docs-internal/` (gitignored), searchable by number.
- Code patterns: existing tests are canonical — `tests/unit/` mirrors `src/sovyx/`.
- Frontend types: `dashboard/src/types/api.ts` (compile-time) + `schemas.ts` (runtime).

## Persistent Memory

Auto-memory persists across sessions. **Location:** `C:\Users\guipe\.claude\projects\E--sovyx\memory\`. **Index:** `MEMORY.md` — load every linked entry at session start. Keep index lines ≤ 150 chars; detail in linked file.

- **Authority:** `feedback_*` carry the SAME authority as CLAUDE.md and OVERRIDE default behavior (see `## Rule Precedence`). The North Star is the canonical summary of the current `feedback_*` set.
- **Project memories** (`project_*`): historical context — missions, incidents, paranoid investigations.
- **User memories** (`user_*`): preferences and role context.
- **Reference memories** (`reference_*`): external systems.

Before recommending from memory, verify the referenced file/function still exists. **Memory state at write time ≠ current state.** When a memory recommends a flag/file/path, grep before relying on it.
