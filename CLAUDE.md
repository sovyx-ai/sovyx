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
uv run mypy src/                              # strict; ~400 files (drifts; verify `find src/ -name "*.py" | wc -l`)
uv run bandit -r src/sovyx/ --configfile pyproject.toml
uv run python -m pytest tests/ --ignore=tests/smoke --timeout=30   # ~12k tests (drifts; verify `pytest --collect-only -q | tail -3`)

# Dashboard (from dashboard/)
npx tsc -b tsconfig.app.json                  # zero new errors
npx vitest run                                # ~960 tests (drifts; verify `npx vitest run --reporter=verbose | grep Tests`)
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
│   └── routes/          # ~25 APIRouter modules (split from the old 2 134 LOC server.py)
│       ├── activity, brain, channels, chat, config, conversation_import,
│       ├── conversations, data, emotions, logs, onboarding, plugins,
│       ├── providers, safety, settings, setup, status, telemetry,
│       ├── voice, voice_test, websocket
│       └── _deps.py     # Shared verify_token dependency
├── tiers.py             # ServiceTier enum, feature/mind-limit maps (informational)
├── license.py           # LicenseValidator (Ed25519 public key JWT, offline)
├── voice/               # STT, TTS, VAD, wake word, Wyoming
│   │                    # Multi-mind voice identity (Phase 8 of master mission):
│   │                    # wake word, voice ID, language, accent, cadence are
│   │                    # configurable per MindConfig. See Phase 8 of
│   │                    # docs-internal/missions/MISSION-voice-final-skype-grade-2026.md
│   ├── _capture_task.py # ~760 LOC — orchestration root: AudioCaptureTask
│   │                    # composes 5 mixins from capture/, public start/stop +
│   │                    # validation helpers, legacy re-exports for back-compat
│   ├── capture/         # Split from _capture_task.py per T1.4 (was 2785 LOC):
│   │   ├── _constants.py, _exceptions.py, _helpers.py, _contention.py, _restart.py
│   │   ├── _epoch.py            # EpochMixin (ring-buffer epoch packing)
│   │   ├── _ring.py             # RingMixin (ring-buffer state + tap helpers)
│   │   ├── _lifecycle_mixin.py  # LifecycleMixin (stream open/close/shutdown)
│   │   ├── _loop_mixin.py       # LoopMixin (audio thread + consume loop)
│   │   └── _restart_mixin.py    # RestartMixin (5 restart strategies)
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
├── llm-router.md
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
16. **God files (>500 LOC with mixed responsibilities):** Don't let a single module accumulate orchestration + helpers + types + models. Once it's hard to navigate, split into a subpackage (see `cognitive/safety/`, `cognitive/reflect/`, `voice/pipeline/`, `voice/capture/`, `dashboard/routes/` as references — each sub-file owns one responsibility, `__init__.py` re-exports the public surface for back-compat). The `voice/capture/` reference is the most aggressive worked example: 2785 → 760 LOC across 12 commits via 5 mixins on a multi-mixin host (`AudioCaptureTask(EpochMixin, RingMixin, LifecycleMixin, LoopMixin, RestartMixin)`) — pattern proven for refactoring a single-class megafile while keeping public surface stable + every test patch path follows the split (anti-pattern #20).
17. **Hardcoded tuning constants:** Thresholds, timeouts, URLs, SHAs, etc. go in `EngineConfig.tuning.{safety,brain,voice}` (pydantic-settings). Module-level `_CONST = _TuningCls().field` pattern keeps import-time access while allowing `SOVYX_TUNING__*` env overrides. Never hardcode in a `.py` constant.
18. **Raw `fetch()` in the frontend:** Every network call MUST go through `src/lib/api.ts`. `api.*` wraps JSON + auth + retry + timeout + schema validation; `apiFetch` wraps raw-Response cases. A loose `fetch("/api/…")` drifts from the auth header injection and 401 handler.
19. **`localStorage` for auth tokens:** XSS-exposed. Use `sessionStorage` (tab-scoped) + in-memory fallback, which is what `src/lib/api.ts` already does. A token migrator reads any legacy `localStorage` entries into `sessionStorage` on boot.
20. **Test patches must follow module splits:** When you extract a helper (`_model_downloader`, `_event_emitter`, `_output_queue`, etc.), every `patch("old.module.X")` in the test suite becomes a silent no-op. The test still appears to mock X, but the real implementation runs. Grep for the old path and migrate patches to the new one in the same commit as the split.
21. **Windows capture APOs corrupt mic signal before PortAudio sees it:** Windows Voice Clarity (`VocaEffectPack` / `voiceclarityep`, shipped via Windows Update in early 2026) registers as a per-endpoint capture APO and destroys Silero VAD input on affected hardware — max speech probability drops below 0.01 despite healthy RMS. The durable fix is WASAPI *exclusive* mode via `capture_wasapi_exclusive` (bypasses the entire APO chain). Sovyx detects the APO at startup (`sovyx.voice._apo_detector`) and auto-bypasses on repeated deaf heartbeats (`voice_clarity_autofix=True`, default). Never try to "fix" by tuning the VAD threshold or adding AGC — those are band-aids; the signal is destroyed *upstream* of user-space. Surfaces: `sovyx doctor voice_capture_apo` + `GET /api/voice/capture-diagnostics`.
22. **Windows `time.monotonic()` ticks at ~15.6 ms:** without `timeBeginPeriod`, Python's default monotonic clock on Windows has ~15.6 ms resolution. `time.sleep(0.01)` can round down to a zero-tick delta — `now` at lookup reads the same value as before the sleep — which breaks any test that asserts "time advanced". Use sleeps ≥ 50 ms for timer-sensitive tests, or inject a fake clock. Linux sub-µs `time.monotonic()` masks this on CI; it only surfaces on Windows dev hosts. Sites fixed in the 2026-04 triagem round: `test_active_uptime`, `test_expired_entry_returns_none`, `test_query_logs_after_param_for_incremental`, `test_records_elapsed_time`.
23. **`EngineConfig.data_dir` defaults to `~/.sovyx`; bootstrap re-seeds `os.environ` from it:** `bootstrap()` reads `<data_dir>/channel.env` + `<data_dir>/secrets.env` and loads their `SOVYX_*` / API-key entries into the process env (bootstrap.py:118-127). Tests that only pass `database=DatabaseConfig(data_dir=tmp_path)` leave `EngineConfig.data_dir` at the home-dir default, so a dev host running the real daemon has its production secrets re-seeded mid-test. Always pass `EngineConfig(data_dir=tmp_path, database=DatabaseConfig(data_dir=tmp_path))` for env-sensitive tests. Companion rule: scrub the env with `monkeypatch.delenv` (auto-restored) rather than `os.environ.pop` (leaks between tests). The bootstrap auto-detect loop checks 9 cloud-LLM keys (`ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `GOOGLE_API_KEY`, `XGROK_API_KEY`, `DEEPSEEK_API_KEY`, `MISTRAL_API_KEY`, `GROQ_API_KEY`, `TOGETHER_API_KEY`, `FIREWORKS_API_KEY`) — a single leftover masks the path under test.
24. **Strict `>` on `time.monotonic()` deadlines is silently wrong on coarse clocks:** a comparison like `if time.monotonic() > entry.expires_at:` never fires when `now` and the deadline share the same monotonic tick. On Linux this is rare (sub-µs clock) so tests pass; on Windows it's the common case for any sub-tick TTL. Symptom: `ttl_sec=0` never expires, `_SCORE_TIMEOUT_S=0` processes the entire queue instead of stopping. Prefer `>=` for deadline/TTL checks — the inclusive comparison matches user intuition ("TTL=N expires AT t=N, not t=N+ε") and is coarse-clock-safe. Sites fixed: `_classifier_cache.py:69`, `brain/consolidation.py:217`.
25. **Frame-typed pipeline as observability layer, not state machine rewrite:** Mission `MISSION-voice-100pct-autonomous-2026-04-25.md` §1.1 chose Hybrid Option C — Pipecat-aligned typed frames (`PipelineFrame` + 8 subclasses in `voice/pipeline/_frame_types.py`) that wrap state-transition + atomic-cancellation events with structured metadata, while the orchestrator's authoritative state continues to live in `VoicePipelineState` + boolean flags. Frames are recorded into a bounded ring buffer via `PipelineStateMachine.record_frame` and exposed via `VoicePipeline.frame_history` + `GET /api/voice/frame-history`. The full Pipecat state-machine rewrite is deferred to v0.24.0+; doing it as a single mission would force 200+ test rewrites + dashboard websocket cutover risk. Adopting Pipecat-style frames as instrumentation FIRST gets the trace ID + atomicity-contract benefits without paying the rewrite cost. When evaluating a future v0.24.0+ full Pipecat refactor, the existing 30-day-stable O2 atomic deaf-signal lock + the 5 emission sites are the integration points to preserve.
26. **KB profile signing: dev key v1 ships in repo, production rotation via HSM:** `voice/health/_mixer_kb/_trusted_keys/v1.pub` is the dev signing key generated in mission Step 7. Public key in repo, private key (`.signing-keys/sovyx_kb_v1.priv`) gitignored + STAYS LOCAL. Loader stays in `Mode.LENIENT` for v0.23.x (warns on bad/missing signatures, doesn't reject) — flip to `Mode.STRICT` planned for v0.24.0 after one minor-version cycle of telemetry-validated lenient mode. For PRODUCTION rotation: HSM-backed key (YubiKey / AWS KMS / GCP Cloud KMS), multi-key trust store with overlapping windows during rotation, re-sign every shipped first-party profile, soak one minor cycle, drop old key. Compromise response: 24h advisory + emergency v2 roll + Mode.STRICT flip + community PR queue purge. Full procedure in `docs/contributing/voice-kb-rotation.md`.
27. **`contextlib.suppress` + `logger.debug` is the canonical "silent ignore" replacement, NOT raw `try/except: pass`:** Mission `MISSION-voice-100pct-autonomous-2026-04-25.md` §1.7 + Step 1 migrated 9 sites in `src/sovyx/voice/` from `try: ... except (Specific, Exceptions): pass` to `with contextlib.suppress(Specific, Exceptions): ...` followed by a `logger.debug("voice.<module>.<op>_skipped", reason="<context>")`. The pattern makes intent explicit (vs. raw try/except) and provides observability without runtime cost (debug-level filter strips it in prod). The migration explicitly rejected: (a) silent suppression with no log, (b) WARN-level logs that flood production, (c) raising errors that callers can't handle. The "intentional ignore" semantics + the dev-only debug log together implement the mission's "no silent failure" contract for paths where the failure is genuinely benign (best-effort cleanup, optional import probe, malformed-field skip).
28. **Cold probe MUST validate signal energy, not just callback count (Furo W-1):** The pre-v0.24.0 `voice/health/probe.py::_diagnose_cold` returned `Diagnosis.HEALTHY` whenever `callbacks_fired > 0`, ignoring `rms_db`. Microsoft Voice Clarity APO and similar upstream signal-destroyers leave the PortAudio callback chain firing while delivering exact-zero PCM, so the cold probe accepted a silent combo as the cascade winner — and `ComboStore.record_winning` then persisted that silent winner so every subsequent boot loaded the same broken combo deterministically (replication contract: see user's `sovyx.log` Razer + Win11 25H2 + VC bug). The v0.24.0 fix at `_diagnose_cold` reads `rms_db` and (in strict mode, default-flipped True in v0.25.0 per `feedback_staged_adoption`) returns `Diagnosis.NO_SIGNAL` when `rms_db < probe_rms_db_no_signal`. Lenient mode emits the structured event `voice.probe.cold_silence_rejected{mode=lenient_passthrough}` for telemetry-only calibration before the flip. The pattern generalises: any acceptance gate downstream of a real-world signal source MUST verify the signal itself, not just the wrapping mechanics. Don't accept "callback fired" as a proxy for "signal is alive".
29. **`CaptureRestartFrame` is observability, NOT a state-machine rewrite (Voice Windows Paranoid Mission §C):** The capture-task restart layer (`request_exclusive_restart`, `request_alsa_hw_direct_restart`, future `request_host_api_rotate` and `request_device_change_restart`) is the single point where the substrate beneath the audio pipeline mutates. v0.24.0 lands `voice/pipeline/_frame_types.py::CaptureRestartFrame` (frozen+slots dataclass, `CaptureRestartReason` StrEnum) as a typed observability frame mirroring the existing `BargeInInterruptionFrame` / `OutputAudioRawFrame` pattern. Wire-up (Phase 2, T31-T32) makes every restart method emit a CaptureRestartFrame BEFORE the ring-buffer epoch increments, and the orchestrator records it via `PipelineStateMachine.record_frame`. The dashboard's `GET /api/voice/restart-history` widget renders one timeline of "what happened on the mic" for post-incident forensics. **Critical contract:** the frame is observability — it does NOT replace the boolean flags + `VoicePipelineState` that own authoritative state (same hybrid-Option-C lesson as anti-pattern #25). Field shape (zod schemas in `dashboard/src/types/schemas.ts`) is `.optional()` in v0.24.0; promotion to required only after one minor cycle of in-prod observation per master rollout matrix. Do not couple production logic to frame presence; the frame ring buffer is bounded (256 entries) and may evict before dashboards poll.
30. **`psutil.open_files()` / `net_connections()` hang during async teardown on Windows:** psutil's Windows backend iterates the kernel handle table and calls `os.stat()` on each handle. During pytest-asyncio `_cancel_all_tasks` or daemon shutdown, handles in a closing state cause `os.stat()` to block indefinitely — and `try/except` catches raised exceptions, NOT blocked syscalls. CI symptom: Windows test job times out at 6+ minutes with the stack trace pointing at `_capture_psutil_metrics → proc.open_files() → psutil/_pswindows.py::isfile_strict`. Linux CI doesn't surface it because Linux `os.stat()` of closing handles is sub-µs. The durable fix is to skip these expensive psutil calls on shutdown / `final=True` paths via a keyword-only `skip_expensive: bool` flag — best-effort metrics are accepted in shutdown by design (cheap fields like `rss/vms/cpu/threads/handles_or_fds` still flow). Site fixed: `observability/resources.py::_capture_psutil_metrics` + `_emit_snapshot(final=True)` (commit 003a63f). Generalisation: any metrics-emit path on a shutdown / cancellation hook MUST avoid handle-iterating syscalls; either skip them or wrap in `asyncio.wait_for` with a strict deadline. Don't rely on `try/except` to rescue you from a blocked syscall.
31. **Perf gate p99 ratio is tail-sensitive even with median-of-N:** `scripts/check_perf_regression.py` runs `bench_observability.py` 3× and takes the median p99 to discard one outlier. Sustained shared-runner contention on GitHub-hosted Linux can push **all 3** runs over budget (`logging.emit.async / logging.emit.minimal > 2.0×`) — median = noise, gate fails on commits that don't touch `sovyx.observability.logging`. Triage protocol when this gate fails: (1) `git diff <commit>` and check for ANY path overlap with `observability/logging.py`, `_async_handler.py`, or the structlog processor chain — if zero overlap, very high prior the failure is contention not regression; (2) if there is overlap or the failure persists across reruns, suspect a real regression (likely "lost the `put_nowait` fast path on `AsyncQueueHandler.enqueue`" or "`BackgroundLogWriter` doing work on the producer thread"). Hardening fix when calibration drift is confirmed: bump `_DEFAULT_REPEATS` from 3 to 5, or switch from median to trimmed-mean (drop 1 highest + 1 lowest). The script's own docstring acknowledges the limitation ("p99 is explicitly tail-sensitive"); a 2/3 → 3/5 increase doubles the noise headroom for the cost of ~30s extra CI time.
32. **Mixin method-via-MRO stubs silently shadow real methods that live AFTER the calling mixin in MRO:** when a mixin (`MixinA`) calls `self.foo()` and `foo` lives on `MixinB` further right in the host class's bases, a naive stub on MixinA — `def foo(self) -> None: ...` — is a real Python method (the `...` body returns None) and WINS MRO resolution over MixinB's real method. The shadowed call silently returns None, the bug is invisible at type-check + ruff + bandit time, and only surfaces as runtime "method did nothing" failures. T1.4 step 9b LoopMixin caught it: `_consume_loop` in LoopMixin called `self._reopen_stream_after_device_error()`, the stub-on-LoopMixin shadowed the real method on RestartMixin (which sits AFTER LoopMixin in `class AudioCaptureTask(EpochMixin, RingMixin, LifecycleMixin, LoopMixin, RestartMixin)`), and three exclusive-restart tests caught it via `OPEN_FAILED_SHARED_FALLBACK` instead of `OPEN_FAILED_NO_STREAM`. Two safe patterns: (a) **target lives BEFORE caller in MRO** — the `def stub(self) -> ...: ...` is fine because MRO finds the real method first (the stub is just a doc + mypy hint that's never reached); (b) **target lives AFTER caller in MRO** — declare the cross-mixin reference inside `if TYPE_CHECKING:` so the body is type-check-only and erased at runtime, letting MRO fall through to the real method. Mypy strict's "compatible base classes" check still passes because the TYPE_CHECKING-block `def` matches the real signature; runtime gets no class attribute. Documented inline in `voice/capture/_loop_mixin.py` for future cross-mixin refs. The general rule: **never put a real `def` stub on a mixin if the target method lives on a mixin that comes later in the MRO** — use the TYPE_CHECKING block instead.
33. **Per-mind config resolution from RPC handlers — best-effort yaml load, never assume a registry method exists:** during T8.21 step 6 retention work I assumed `MindManager.get_mind_config(mind_id)` existed and wired the daemon-side `mind.retention.prune` RPC handler against it. The method does NOT exist — `MindManager` only exposes `load_mind`/`start_mind`/`stop_mind`/`get_active_minds`, no per-mind config retrieval. Symptom would have been an `AttributeError` at first invocation, NOT caught by mypy because `MagicMock`-typed `registry.resolve` returns `Any`. Verified-at-HEAD: `_load_mind_config_best_effort(data_dir, mind_id)` in `engine/_rpc_handlers.py` loads `<data_dir>/<mind_id>/mind.yaml` defensively + returns `None` on any failure (missing file, malformed YAML, schema violation), letting the caller fall through to global defaults from `EngineConfig.tuning`. The "best-effort" semantics matter because retention is privacy-sensitive: a malformed mind.yaml MUST NOT block retention from running on global defaults — operator's compliance posture > perfect config resolution. The general rule: **before calling `await registry.resolve(X).method(y)` from an RPC handler, grep `class X:` for `def method` to verify the method exists** — `MagicMock` masks AttributeErrors at test-mock time but production blows up.
34. **Schedulers with kill-switch flags must default OFF + check the flag at instantiation, not just at `start()`:** during T8.21 step 6 retention scheduler I had to decide whether the daemon should always instantiate `RetentionScheduler` and check `auto_prune_enabled` only at `start()`, or skip instantiation entirely when disabled. The right answer is **skip instantiation entirely** — when `MindConfig.retention.auto_prune_enabled = False` (default), bootstrap doesn't construct the service or scheduler, and the registry never registers them. Zero runtime cost when disabled (no idle task in the scheduler list, no observable surface in the registry, no metrics noise). The pattern: at the bootstrap site, `if mind_config.retention.auto_prune_enabled: ... register_instance ...`. Lifecycle's `_start_services` then uses `if registry.is_registered(RetentionScheduler):` as the gate — same pattern as ConsolidationScheduler/DreamScheduler with their respective kill switches. The anti-pattern would be: always-instantiate + start-time check. That leaks a no-op task into the asyncio event loop + a no-op entry into the registry, both observable in logs and metrics, and operators triaging "what's running?" see the dangling scheduler. Default-OFF means default-ABSENT, not default-PRESENT-but-no-op.
35. **Cross-layer config defaults are sentinels, not values:** when a low-level dataclass like `VoicePipelineConfig.mind_id: str = "default"` holds a sentinel default that upstream callers MUST overwrite, every caller path that omits the field becomes a silent bug. Forensic case (Mission `MISSION-voice-linux-silent-mic-remediation-2026-05-04.md` §Phase 1 T1.2): `dashboard/routes/voice.py:1796` read `getattr(request.app.state, "mind_id", "default")` while NO production code path ever assigned `app.state.mind_id` — the voice pipeline always launched under the phantom `"default"` mind even when the operator had created a real one. Every `voice_pipeline_heartbeat` carried `mind_id=default` for the operator's mind `jonny` (logs_01.txt line 1342). Two safe patterns: (a) **make the field required** (no default — every caller MUST pass an explicit value, type-check enforces this); preferred for NEW fields shipping in fresh dataclasses. (b) **detect the sentinel at the topmost wire-up point + fire a structured WARN** when an upstream caller passes the sentinel-value while a real value is available; safe migration when the sentinel already shipped + breaking it would force a coordinated test-suite edit. Pattern (b) is what T1.2 used: `voice/factory/__init__.py` emits `voice.factory.mind_id_default_sentinel{voice.passed_mind_id, voice.action_required}` when `mind_id == "default"` reaches the factory, AND `dashboard/_shared.resolve_active_mind_id_for_request` resolves the real value via `MindManager.get_active_minds()` at the route boundary. The general rule: **before introducing a default value on a cross-layer config field, ask "is this a real default or is it a sentinel?" — if sentinel, document at the field site + wire detection at every upstream entry point**.
36. **`patch.object` on async functions auto-detects via Python 3.8+ `AsyncMock` — string-path patches DON'T:** when a test does `patch.object(module, "async_fn", return_value=Foo(...))`, Python 3.8+ inspects the target with `inspect.iscoroutinefunction` and substitutes `AsyncMock` (whose return_value is awaitable) instead of `MagicMock` (whose return_value is NOT awaitable + crashes the awaiter with `TypeError: object Foo can't be used in 'await' expression`). String-path `patch("module.async_fn", return_value=...)` follows the SAME autodetect when the import resolves at patch time. Forensic case (Mission `MISSION-voice-calibration-extreme-audit-2026-05-06.md` P2.T3): when `run_full_diag` was renamed to `run_full_diag_async` in v0.30.30, 17 test patch sites across 4 test files were string-renamed without changing the `return_value=DiagRunResult(...)` shape — and ALL of them passed because the autodetect kicked in. Without autodetect, every patch would have needed `new_callable=AsyncMock` + the same return_value, doubling the migration LOC. The general rule: **prefer `patch.object(module, "name", return_value=X)` over `patch("path.to.name", new_callable=AsyncMock, return_value=X)` for async-function patches** — the autodetect is documented (https://docs.python.org/3/library/unittest.mock.html#patch) and load-bearing for clean async test code.
37. **Cryptographic verifier verdict ordering: NO_TRUSTED_KEY before NO_SIGNATURE before MALFORMED before BAD:** in a 5-way verdict verifier (`ACCEPTED / REJECTED_NO_SIGNATURE / REJECTED_BAD_SIGNATURE / REJECTED_MALFORMED_SIGNATURE / REJECTED_NO_TRUSTED_KEY`), the order of the early-return checks is load-bearing. Forensic case (Mission `MISSION-voice-calibration-extreme-audit-2026-05-06.md` §P4): `_persistence.py::_verify_calibration_signature` MUST check `pubkey is None` FIRST — else a later `pubkey.verify(sig_bytes, payload)` against a None pubkey crashes with `AttributeError: 'NoneType' object has no attribute 'verify'`. Then `signature is None` (cheap; avoids the expensive payload canonicalisation). Then `signature` malformed (b64 invalid OR length != 64; cheap; avoids handing a malformed payload to the cryptographic verify which would raise the LESS-informative `InvalidSignature`). Finally the actual `pubkey.verify` (expensive; only reached for valid-shape inputs). The general rule: **every verifier returning a multi-way verdict orders its early-return checks from CHEAPEST + MOST-COMMON-FAILURE-FIRST to MOST-EXPENSIVE-LAST + asserts on dependency invariants (e.g. pubkey != None) BEFORE invoking dependent operations** — anything else either crashes on legitimate-but-rare configurations or produces less-actionable verdicts.

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

### Two-Tier GA Strategy (voice subsystem)

The voice subsystem ships in two GA tiers per master mission `MISSION-voice-final-skype-grade-2026.md`:

- **v0.30.0 — single-mind production GA.** Phase 1-7 complete (cold-probe, bypass tiers wire-up, telemetry/IMM listener, multi-platform Win/Linux/macOS). Operators MAY ship v0.30.0 without waiting for Phase 8.
- **v0.31.0 — FINAL multi-mind GA.** Phase 8 complete (per-mind wake word, voice ID, language, accent, cadence — see Phase 8 task block in master mission).

Phase 8 work goes into v0.30.x patches OR directly v0.31.0 — never blocking v0.30.0 release. Operators choose tier per their mind topology (single-mind use cases are fully supported by v0.30.0).

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

## Mission Lifecycle

Sovyx uses long-running structured missions to coordinate complex multi-version work (voice hardening, multi-mind, etc.). The lifecycle is:

1. **Active missions** live in `docs-internal/missions/MISSION-*.md`. Each carries a status field, a numbered task list (T1.1, T1.2, …), and Phase boundaries that map to target versions. Active masters as of v0.24.0:
   - `MISSION-voice-final-skype-grade-2026.md` (terminal, Phase 1-8)
   - `MISSION-voice-windows-paranoid-2026-04-26.md` (companion, Phase 1-3 wire-up)
   - `MISSION-voice-godfile-splits-v0.24.1.md` (companion, hygiene splits)
2. **ADRs** live in `docs-internal/ADR-*.md`. They are CANONICAL — referenced from code docstrings (`combo_store/__init__.py:3 → ADR-combo-store-schema.md`, etc.). Never delete an ADR; supersede it via a new ADR that references the old one.
3. **Completed missions** are ARCHIVED (NOT deleted) to `docs-internal/archive/missions-completed/` with a `## Archive Footer` block citing: status at archive (SHIPPED / SUPERSEDED / ABSORBED), code references where the work landed, predecessor + successor missions. Update `docs-internal/archive/INDEX.md` with the new entry.
4. **Predecessor / superseded** missions follow the same archive flow with reason `"absorbed by <successor>"` or `"shipped via <commit-hash>"`. The archive INDEX is the audit-trail entry point.
5. **Forensic resolution docs** (post-incident ADRs, RCA closures) go to `docs-internal/archive/forensics-resolved/` with the same footer convention.
6. Never delete a mission or ADR that produced shipped code. Reference value > workspace cleanliness. Pure orphans (planning docs that never produced code, byte-identical duplicates) are the only valid DELETE targets.

When closing a mission task in a commit, reference the mission file + task ID in the body (e.g. `Mission: docs-internal/missions/MISSION-voice-final-skype-grade-2026.md §Phase 1.T2`) and update the mission spec to mark the task ✅ shipped in a follow-up `docs(mission):` commit. This keeps the trail forense intact even when subsequent tasks hit blockers.

## Deep Reference
- Public docs (MkDocs): `docs/` — architecture, getting-started, configuration, api-reference, security, per-module specs under `docs/modules/`.
- Internal planning + audits: `docs-internal/` (gitignored, local only).
- Backend specs (IMPL/SPE/ADR): live under `docs-internal/`, searchable by number.
- Code patterns: look at existing tests for real examples — `tests/unit/` mirrors `src/sovyx/`.
- Frontend types: `dashboard/src/types/api.ts` (compile-time) + `dashboard/src/types/schemas.ts` (runtime).

## Persistent Memory

Sovyx development uses an auto-memory system that persists across sessions:

- **Location:** `C:\Users\guipe\.claude\projects\E--sovyx\memory\`
- **Index file:** `MEMORY.md` — load every linked entry at session start.
- **Authority:** memories tagged `feedback_*` carry the SAME authority as CLAUDE.md instructions and OVERRIDE default behavior. Critical entries:
  - `feedback_enterprise_only` — fixes paliativos / band-aids são proibidos.
  - `feedback_no_speculation` — zero suposição; só afirmar com 100% embasamento técnico.
  - `feedback_staged_adoption` — never bundle "foundation + 5 call-site adoptions" in one commit; lenient default for new validators.
  - `feedback_ci_watching` — after tag push, skip `gh run watch` on publish.yml.
- **Project memories** (`project_*`) carry historical context: ongoing missions, incidents, paranoid investigations.
- **User memories** (`user_*`) carry preferences and role context.
- **Reference memories** (`reference_*`) point to external systems.

Before recommending from memory, verify the referenced file/function still exists (memories can drift). Memory state at write time ≠ current state.
