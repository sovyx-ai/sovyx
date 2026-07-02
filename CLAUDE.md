# CLAUDE.md — Sovyx Development Guide

## North Star

Override defaults when in conflict. Enforced via `feedback_*` memories (same authority as this file).

1. **Enterprise-grade, no band-aids AND no over-engineering.** Fix root causes; stop where marginal value < marginal risk. (`feedback_enterprise_only`)
2. **Zero speculation.** State only what's verified at HEAD; mark unverified explicitly. (`feedback_no_speculation`)
3. **Staged adoption.** Foundation → wire-up → default-flip across separate commits. Validators ship LENIENT; flip STRICT after one minor cycle. (`feedback_staged_adoption`)
4. **Full autonomous authority on technical scope.** `AskUserQuestion` reserved for product scope/priority/UX — never technical. (`feedback_full_autonomous_authority`, `feedback_technical_decisions_no_ask`)
5. **Validation batched at tag milestones.** Ship between checkpoints; operator validates against `OPERATOR-VALIDATION-BACKLOG-2026.md`. (`feedback_validation_batching`)
6. **Don't watch CI after tag push.** Skip `gh run watch` on `publish.yml`. (`feedback_ci_watching`)
7. **No palliative shell scripts in chat.** Diagnostic scripts ship as committed `.sh` w/ download URL. (`feedback_no_inline_scripts_in_chat`)

## Rule Precedence

Apply in order: (1) `feedback_*` memories; (2) Anti-patterns below; (3) Conventions; (4) Stack defaults. Lower cannot override higher — if tempted, surface the conflict.

## What is Sovyx

Sovereign Minds Engine — persistent AI companion with real memory, cognitive loop, brain graph. Python library + CLI daemon + React dashboard.

## Stack

- **Backend:** Python 3.11/3.12, structlog, pydantic v2, pydantic-settings, FastAPI, aiosqlite, ONNX Runtime, httpx, argon2-cffi, PyJWT.
- **Frontend:** React 19, TypeScript, Vite, Tailwind, Zustand, TanStack Virtual, zod, i18next.
- **Build:** uv (`uv.lock` committed), npm (dashboard), Hatch + `hatchling`.
- **CI:** GitHub Actions on self-hosted `sovyx-4core` → ruff + mypy + bandit + pytest (3.11/3.12) + vitest + tsc + Docker + PyPI.
- **CLI:** `sovyx` entry (`sovyx.cli.main:app`), plugin entry points under `sovyx.plugins`.

## Quality Gates (MANDATORY before `git push`)

Mechanical forcing function — `git push` is REJECTED without proof:

```bash
./scripts/install_hooks.sh    # one-time per clone
./scripts/verify_gates.sh     # writes .git/.last-gates-pass marker
git push                      # hook validates marker fresh + HEAD-matched, else REJECTS
```

Hook at `.githooks/pre-push` validates marker within 30min (override: `SOVYX_GATES_MAX_AGE_SEC`). Escape `--no-verify` requires operator approval + commit-body rationale.

**Scope — when a gate run is actually needed (read this before burning 10 min):** the marker gates `git push` and is **HEAD-matched**, so run `verify_gates.sh` *after* your final commit of a series, not before each one. A commit touching **only** gitignored surfaces (`docs-internal/`, the auto-memory store) or **only** prose/comment/docstring text has no gate dependency — it can't move gates 1–7 and (for gitignored paths) is never even pushed. The full suite matters when `src/`, `dashboard/src/`, `scripts/`, or config changed and you intend to push. A commit alone (no push) never requires gates; only a `vX.Y.Z` **tag** triggers `publish.yml`.

Gates (in order):

```
# 1-5 backend: ruff check / ruff format --check / mypy (strict) / bandit / pytest --timeout=30 -q
# 6-7 dashboard: npx tsc -b tsconfig.app.json / npx vitest run --reporter=dot
# 8-10 STRICT: boundary_round_trip (C2) / ladder_iteration (C3) / degraded_signal_surface (C4)
# 11 dashboard_bundle_integrity      — STRICT-when-applicable v0.49.x (verify_gates.sh: enforce if bundle present, SKIP if no local build; full STRICT in publish.yml)  (C5, W0.1)
# 12 llm_provider_discipline         — LENIENT v0.49.x; STRICT v0.50.0    (C6, V-C6-11)
# 13 platform_neutral_event_names    — LENIENT v0.49.x; STRICT v0.51.0    (H2, V-H2-11)
# 14 quarantine_reason_discipline    — LENIENT v0.49.10..v0.52.x; STRICT v0.53.0 (H3, V-H3-11)
# 15 resource_hygiene_discipline     — LENIENT v0.49.14..v0.53.x; STRICT v0.54.0 (H4, V-H4-13)
# 16 zod_twin_completeness           — LENIENT v0.49.38..v0.52.x; STRICT v0.53.x (C, C-P0-1)
# 17 response_model_presence         — LENIENT v0.49.38..v0.52.x; STRICT v0.53.x (C, C.4 body)
# 18 boundary_helper_real            — LENIENT v0.49.38..v0.52.x; STRICT v0.53.x (C, C.6 body)
# 19 name_lock_integrity            — LENIENT v0.49.x; STRICT v0.52.0    (Ω-3, #68 DRAFT) — every docs-internal/* path link in src/sovyx docstrings must resolve. STRICT-when-applicable: SKIPs (exit 0) where docs-internal/ is gitignored-absent (fresh checkout / CI runner / PyPI sdist) — WORKING-TREE gate; full check runs on every dev box + local verify_gates.sh. (v0.49.55 publish FAILED: end-to-end test hard-asserted on CI where docs-internal/ isn't checked out → 76 false violations; fixed v0.49.56.)
```

Plus `uv lock --check` on version bumps. Always grep gate summary line — never trust harness exit code (pre-v0.42.2 `2>&1 | tail -N` masked 6 failures; see `feedback_ci_preflight`).

**Where gates live:** gates 1-7 are stock tooling (ruff/mypy/bandit/pytest/tsc/vitest); gates 8-19 are custom AST/contract checkers in `scripts/dev/check_*.py`, invoked by `scripts/verify_gates.sh`. The OTHER `scripts/check_*.py` files (log schemas, metrics cardinality, otel semconv, exception chains, log noise, test PII, constant-time token, perf regression) are **CI-only** — they run in `.github/workflows/ci.yml`/`publish.yml`, not in the local pre-push set.

**Version bump:** any `pyproject.toml` `version` change requires `uv lock`.

**Post-tag verification:** after `git push origin <tag>`, `gh run list --workflow=publish.yml --limit 3` to confirm prior tag passed BEFORE bumping next. Skipping shipped 6 tags atop broken pipeline in v0.41.x.

## Repo Layout

```
src/sovyx/
├── engine/        # Config, bootstrap, lifecycle, events, registry, RPC (LRULockDict)
├── cognitive/     # Cognitive loop — 5 request-driven phases: Perceive → Attend → Think → Act → Reflect (safety/, reflect/ subpkgs). Phases 6-7 (Consolidate, Dream) are scheduled in brain/. Canonical: docs-internal/architecture/cognitive-loop.md
├── brain/         # Concepts, episodes, relations, embedding, scoring, retrieval; Consolidate (6h) + Dream (nightly) schedulers = cognitive phases 6-7
├── bridge/channels/  # telegram.py, signal.py
├── persistence/   # SQLite pool (WAL, round-robin readers), migrations
├── observability/ # Logging (structlog), health, alerts, SLOs, tracing
├── llm/           # Multi-provider router (Anthropic, OpenAI, Google, Ollama)
├── mind/, context/  # Mind config + LLM context assembly
├── cli/           # Typer CLI: sovyx start/stop/init/logs/doctor
├── dashboard/     # FastAPI; server.py wires routers, routes/ per domain
├── tiers.py, license.py  # ServiceTier enum + Ed25519 offline license validator
├── voice/         # STT/TTS/VAD/wake/Wyoming. Per-mind via MindConfig.
│   ├── capture/   # Ring buffer + lifecycle + loop + restart mixins
│   ├── pipeline/  # Turn state machine + output queue + barge-in + heartbeat/dwell watchdog
│   ├── health/    # Capture-health lifecycle, quarantine reasons (AP #46/#47); probe/ (cold/warm), cascade/, bypass/, combo_store/, contract/
│   ├── factory/   # create_voice_pipeline wiring + wake-word wire-up + validation + diagnostics
│   ├── calibration/  # Signed calibration profiles (AP #37), wizard, applier
│   ├── diagnostics/  # triage.py analyzer + Linux bash toolkit producer
│   ├── device_test/  # Interactive device-test session (dashboard)
│   └── wake_word_training/  # Wake-word sample synthesis + training
├── plugins/       # Loader + sandbox + SDK. Use SandboxedHttpClient.
├── upgrade/       # Doctor, importer, blue-green, backup manager
└── benchmarks/    # Budget baselines

dashboard/         # React SPA (main repo, not submodule)
├── src/pages/, components/, hooks/, stores/ (Zustand slices)
├── src/types/     # api.ts (compile-time) + schemas.ts (zod runtime)
└── src/lib/       # api.ts (apiFetch + api.{get,post,…}), safe-json.ts, format.ts, i18n.ts

tests/             # unit/ integration/ dashboard/ plugins/ property/ security/ stress/ smoke/(excluded)
docs/              # Public MkDocs
docs-internal/     # Internal missions/ADRs (gitignored)
```

## Conventions

### Python
- **Logging:** `from sovyx.observability.logging import get_logger` → `logger = get_logger(__name__)`. Never `print()` or `logging.getLogger()` directly.
- **Config:** All via `EngineConfig` (pydantic-settings). Env: `SOVYX_*`, `__` for nesting. Tuning: `EngineConfig.tuning.{safety,brain,voice,llm,retention,dashboard}` via `SOVYX_TUNING__*`.
- **Errors:** Custom exceptions in `engine/errors.py`; include `context` dict.
- **Types:** Fully typed. `from __future__ import annotations` everywhere. `TYPE_CHECKING` for type-only imports.
- **Async:** All DB/IO async. Sync CPU-bound MUST wrap in `asyncio.to_thread()`. Tests: `pytest-asyncio mode=auto`.
- **Docstrings:** Public class/function. Imperative first line. No other comments unless WHY non-obvious.

### Dashboard (TypeScript)
- **Types:** Compile-time `src/types/api.ts`; runtime zod `src/types/schemas.ts`. Pass `{ schema }` to `api.*` for safeParse.
- **State:** Zustand at `src/stores/dashboard.ts` w/ slices.
- **API:** ALWAYS via `src/lib/api.ts` — `api.*` for JSON, `apiFetch` for raw `Response`. 30s timeout, exp-backoff retry on 429/503/5xx for idempotent verbs.
- **Auth token:** `sessionStorage` + in-memory fallback. NEVER `localStorage`.
- **Hot-path memo:** `React.memo` on virtualized rows; `useMemo`/`useCallback` for derived/stable props.
- **i18n:** All user-visible strings via `useTranslation()`.
- **Mind id:** `useResolvedMindId` — never hardcode `"default"` (#35). ESLint rule guards.
- **Tests:** Colocated `*.test.tsx`.

### Git
- **Commits:** Conventional (`feat:`, `fix:`, `refactor:`, `test:`, `chore:`, `perf:`, `docs:`).
- **Tags:** `vX.Y.Z` triggers `publish.yml` → PyPI (OIDC) + Docker + Release. Tag matches `pyproject.toml` version.
- **Dashboard:** stage alongside backend in same commit when related.
- **Branch:** Always `main`. No feature branches. (Overrides the harness "branch first on default branch" default.)
- **Local-only surfaces — NEVER `git add`:** `docs-internal/` (`.gitignore:65`), the auto-memory store (`~/.claude/.../memory/`, outside the repo), and `.signing-keys/` are gitignored/external. Edits there are real but **local** — they will never appear in a commit or reach PyPI/GitHub. So: (a) doc/mission/ADR work under `docs-internal/` is local-only by design; (b) a source file (READMEs in `src/`, docstrings) that *links into* `docs-internal/` ships a dead link to PyPI consumers — prefer a `docs/` public target. When unsure if a path is tracked: `git check-ignore -v <path>` (`feedback_verify_gitignore_before_url`).

## Anti-Patterns (bugs that already happened)

Each entry = **rule + why + pointer**. Forensic detail lives in referenced commit/mission/file. Preserve numbering (append, never renumber).

**Index by category:** Logging & Config: 1, 3, 4, 5, 6, 7, 17, 23, 35 · Imports & Test Patches: 2, 11, 20, 36, 38 · Concurrency & Async: 14, 15, 30, 69 · Cross-Platform: 21, 22, 24 · Voice Subsystem: 25, 26, 27, 28, 29, 39, 69, 70 · Tests: 8, 9, 10, 12, 31 · Architecture & Design: 13, 16, 18, 19, 32, 33, 34, 37, 39, 40, 41, 42, 43, 44, 45, 46, 47, 48, 49, 50, 51, 52, 53, 54, 55, 69, 70, 71 · (56-67 reserved by the frozen Mission C/Σ-B promotion batch; 68 DRAFT per Ω-3)

---

1. **Circular imports in `observability/__init__.py`:** lazy `__getattr__`. Never add eager imports.
2. **`sys.modules` stubs miss aliased imports:** `import X as Y` captures real module at import. Use `patch.object(real_module, "attr", mock)`. Reserve `sys.modules` for first-time imports.
3. **`LoggingConfig.console_format` (not `format`):** renamed v0.5.24; legacy YAML auto-migrates. File handler ALWAYS writes JSON.
4. **`log_file` resolved by `EngineConfig` validator:** defaults `None`; resolved to `data_dir/logs/sovyx.log`. Never hardcode log paths.
5. **Dashboard `EngineConfig` from registry:** resolved via `ServiceRegistry`, never `EngineConfig()` instantiation.
6. **httpx WARNING in `setup_logging()`:** raw HTTP lines = `setup_logging()` wasn't called.
7. **`LogEntry` 4 required fields:** `timestamp`, `level`, `logger`, `event`. Backend normalizes `ts→timestamp`, `severity→level`, `message→event`, `module→logger`.
8. **xdist class identity:** never `pytest.raises(InternalClass)`; use `pytest.raises(Exception)` + `assert type(exc).__name__ == "X"`. In prod, dispatch on `type(exc).__name__`, never `isinstance`.
9. **Enums are `StrEnum`:** every string-valued enum inherits `StrEnum` — value-based comparison + xdist namespace safety.
10. **Auth in tests via `create_app(token="...")`:** never monkeypatch `_ensure_token`/`_server_token`. `token` bypasses filesystem + global state.
11. **Prefer `patch.object` over string-path patches:** `patch("module.attr")` can resolve to different module objects under xdist or after refactors.
12. **Defense-in-depth in tests is a smell:** one layer understood > three mysterious. When a fix makes a workaround unnecessary, delete it.
13. **Plugins use `SandboxedHttpClient`, never raw `httpx`:** raw bypasses allowed-domains + rate-limit + size-cap.
14. **Sync CPU-bound in `async def` blocks loop:** ONNX inference (Piper, Kokoro, Silero, Moonshine, OpenWakeWord), `boto3` MUST wrap in `asyncio.to_thread()`.
15. **Unbounded `defaultdict(asyncio.Lock)` leaks:** use `sovyx.engine._lock_dict.LRULockDict(maxsize=N)`.
16. **God files (>500 LOC) split into subpackage:** `__init__.py` re-exports; `_*.py` internals. Migrate patches in same commit (#20). E.g. `cognitive/safety/`, `voice/pipeline/`, `dashboard/routes/`.
17. **Hardcoded tuning constants:** thresholds/timeouts/URLs/SHAs live in `EngineConfig.tuning.*`. Module-level `_CONST = _TuningCls().field` keeps import-time access + env override.
18. **Raw `fetch()` in frontend:** every network call via `src/lib/api.ts` — drifts from auth injection + 401 handler.
19. **`localStorage` for auth tokens is XSS-exposed:** use `sessionStorage` + in-memory fallback. Boot-time migrator reads legacy.
20. **Test patches must follow module splits:** extracting helpers turns `patch("old.module.X")` into silent no-op. Migrate in same commit. Extends to lazy `from X import Y` (#38).
21. **Windows capture APOs corrupt mic before PortAudio:** Voice Clarity destroys VAD input. Fix: WASAPI exclusive (`capture_wasapi_exclusive`). Auto-detected; auto-bypass via `voice_clarity_autofix=True`. Never tune VAD or add AGC — signal destroyed upstream.
22. **Windows `time.monotonic()` ticks ~15.6ms without `timeBeginPeriod`:** timer-sensitive tests: sleeps ≥50ms or fake clock; perf uses `time.perf_counter`.
23. **`EngineConfig.data_dir` re-seeds env:** `bootstrap()` reads `<data_dir>/{channel,secrets}.env`. Tests MUST pass both `data_dir=tmp_path` AND `database=DatabaseConfig(data_dir=tmp_path)`. Use `monkeypatch.delenv`. Auto-detect checks 9 cloud-LLM keys.
24. **Strict `>` on `time.monotonic()` deadlines wrong on coarse clocks:** prefer `>=` — inclusive + coarse-safe.
25. **Frame-typed pipeline is observability, NOT state-machine rewrite (Hybrid Option C):** `PipelineFrame` instruments transitions; authoritative state stays in `VoicePipelineState`. Frames → 256-entry ring → `GET /api/voice/frame-history`. Never couple prod logic to frame presence.
26. **KB profile signing — dev key in repo, prod via HSM:** `_trusted_keys/v1.pub` dev. Private `.signing-keys/` gitignored, STAYS LOCAL. LENIENT v0.23.x → STRICT after one minor. Procedure: `docs/contributing/voice-kb-rotation.md`.
27. **`contextlib.suppress` + `logger.debug(_skipped, reason=…)` is canonical "intentional ignore":** replaces raw `try/except: pass`. Reject: silent suppression w/ no log; WARN floods.
28. **Cold probe MUST validate signal energy, not callback count (Furo W-1):** APOs leave callbacks firing while delivering zero PCM. `_diagnose_cold` reads `rms_db`. **Generalizes:** any acceptance gate downstream of a real-world signal source MUST verify the signal itself.
29. **`CaptureRestartFrame` is observability, NOT state-machine rewrite (sibling #25):** restart emits frame BEFORE epoch increments. `GET /api/voice/restart-history`.
30. **`psutil.open_files()`/`net_connections()` hang during async teardown on Windows:** psutil iterates kernel handles + `os.stat()`; closing handles blocks indefinitely. Fix: `skip_expensive: bool` kwarg (`003a63f`). **Generalizes:** shutdown hooks MUST avoid handle-iterating syscalls or wrap in `asyncio.wait_for`.
31. **Perf gate p99 ratio tail-sensitive — median-of-5 still flakes under shared-runner contention.** `scripts/check_perf_regression.py` enforces `async/minimal ≤ 2.0×` + `redacted/minimal ≤ 3.0×`. Escalation: v0.27.0 median-of-3 → v0.45.7 median-of-5 + concurrency group → v0.49.34 `_DEFAULT_REPEATS=7` + `_trimmed_mean`. Next: bump `_TRIM_COUNT` 1→2 BEFORE touching budgets.
32. **Mixin stubs silently shadow real methods later in MRO:** target BEFORE caller in MRO → naked stub fine; target AFTER caller → declare cross-mixin reference inside `if TYPE_CHECKING:`. See `voice/capture/_loop_mixin.py`.
33. **Per-mind config from RPC handlers: best-effort YAML:** `MagicMock`-typed `registry.resolve(X).method(y)` returns `Any` and masks `AttributeError`. Privacy-sensitive paths (retention) fall through to global defaults. Ref `_load_mind_config_best_effort`.
34. **Schedulers with kill-switch flags default OFF + skip instantiation when disabled:** default-OFF = default-ABSENT, not default-PRESENT-but-no-op. Applied: Consolidation/Dream/Retention.
35. **Cross-layer config defaults are sentinels, not values:** `VoicePipelineConfig.mind_id: str = "default"` is sentinel. Patterns: (a) make field required (preferred); (b) detect sentinel at top wire-up + structured WARN. Frontend: `useResolvedMindId` + ESLint rule. **Recurring — surfaced 5+ times.**
36. **`patch.object` on async functions auto-detects `AsyncMock`:** prefer `patch.object(module, "name", return_value=X)` over `patch("path", new_callable=AsyncMock, ...)`.
37. **Crypto verifier verdict ordering — cheapest + most-common-failure first:** in 5-way `_verify_calibration_signature`: pubkey None → signature None → shape malformed → actual `pubkey.verify`.
38. **Lazy `from X import Y` invalidates module-level patches:** lazy import resolves on SOURCE module at call-time. Patch `X.Y`, NOT `caller.Y`. Extends #20. **Cross-platform corollary:** POSIX-only attrs (`signal.SIGKILL`, `os.killpg`) + Windows `sys.platform="linux"` patches need `patch.object(target, "ATTR", value, create=True)`.
39. **Probe-verdict misrouting + cross-platform event-name drift.** (a) Gates+routers consume probe **verdict** (categorical), not symptom; v0.44.0 restored disjoint dispatch w/ `assert_never`. (b) Event names neutral; platform terms `sys.platform`-gated or wrapped. **Closure: #45 + Gate 13 STRICT v0.51.0.** Mission: `MISSION-c1-vad-mute-reclassification-2026-05-14.md`.
40. **Typed response boundary drifts from producer dict shape:** `Model.model_validate(helper_dict)` only as strict as last round-trip test. `extra="allow"` load-bearing for forward-additive evolution; pair w/ producer→boundary round-trip. Gate 8 AST-enforces on `routes/voice.py`. Mission: `MISSION-c2-voice-status-response-contract-2026-05-16.md`.
41. **Candidate-list dispatch MUST iterate full list in one attempt window before fallback.** Single-shot is anti-shape (cooldowns block retries, latches terminal). Loop + per-candidate cooldown + per-ladder cap + telemetry + shared exclusion + `ProbeResultCache.is_known_unopenable`. Gate 9 AST-rejects `candidates`/`targets`/`entries`/`endpoints` dispatching outside loop. **Sibling #39(a).** Mission: `MISSION-c3-failover-ladder-iteration-2026-05-16.md`.
42. **Operator-actionable degraded state via single composite store/endpoint, never N log lines.** (a) structured log AND (b) `EngineDegradedStore.record(DegradedEntry(...))`. Severity by axis count; server-side ack `operator_acks` SQLite; TTL re-surface; auto-recovery governor. Gate 10 AST-rejects `*_degraded`/`no_*_provider`/`*language_{coerced,unsupported}` WARNs lacking paired `record_*`/`clear_axis`. Allowlist `# c4-allowlist`. **Sibling #40, #41.** Mission: `MISSION-c4-degraded-mode-banner-2026-05-17.md`.
43. **Static-asset distribution contracts enforced at THREE points:** (a) build-time AST scan SPA `index.html` ↔ hashed-chunk; (b) install-time `_integrity.scan_bundle_integrity()`; (c) runtime `EngineDegradedStore.record(axis="dashboard")` — never raw 404 cascade. `BundleVerdict` 5-member + dual-emit LENIENT (ADR-D14) + on-404 debounce ≥60s. Allowlist `# c5-allowlist`. Gate 11 STRICT `publish.yml`; LENIENT `verify_gates.sh` until v0.48.0 (V-C5-7). **Sibling #15, #26, #34, #42.** Mission: `MISSION-c5-dashboard-distribution-integrity-2026-05-17.md`.
44. **Workers depending on external dep MUST verify at `start()`, emit `started_in_degraded_mode` + composite-store entry, AND gate every iteration.** Start-time dep-check + `EngineDegradedStore.record`; per-iter `asyncio.Event` gate w/ `wait_for(timeout=1.0)` + throttled WARN ≤1/min; synthetic fail-fast gated by default-True `<worker>_degraded_mode_fail_fast`; ONE liveness probe per dep (#15); kill-switch default-on (inverse #34). Gate 12 STRICT; LENIENT until v0.50.0 (V-C6-11). **Per-iter gating NOT optional. Sibling #14, #15, #25/#29, #34, #41, #42, #43.** Mission: `MISSION-c6-llm-provider-cognitive-loop-integrity-2026-05-18.md`.
45. **Platform-specific event names MUST be (a) `sys.platform`-gated w/ platform suffix, OR (b) wrapped in neutral event carrying token in `voice.platform`/`voice.<subsystem>_family`.** Raw `apo.*`/`wasapi.*`/`pulseaudio.*`/`pipewire.*`/`coreaudio.*`/`voice_clarity_*` without gating = triage drift. Wrapper `_capture_integrity_emit.py` dual-emits LENIENT (ADR-D14). Allowlist `# h2-allowlist`. Gate 13 STRICT; LENIENT until v0.51.0 (V-H2-11). **Closure of #39(b). Sibling #21, #40, #43.** Mission: `MISSION-h2-platform-neutral-event-naming-2026-05-18.md`.
46. **Quarantine reasons (any operator-actionable acceptance-gate enum) via SSoT verdict→reason map w/ exhaustive `assert_never`.** `voice/health/_quarantine_reasons.py` exposes `QuarantineReason(StrEnum)` (8 members) + 2 resolvers; `routes/voice.py` types `reason: QuarantineReason` (per #40; zod `z.nativeEnum`); i18n `degraded.voice.quarantine.<reason>` + `unclassified` catch-all. Allowlist `# h3-allowlist`. Gate 14 STRICT; LENIENT until v0.53.0 (V-H3-11). New verdict without resolver = hard mypy fail. **Sibling #39(a), #40, #42.** Mission: `MISSION-h3-quarantine-reason-verdict-map-2026-05-18.md`.
47. **Resource-cohort instrumentation covers every cardinality-bounded shared resource** (ONNX `InferenceSession`, asyncio default-executor, `LRULockDict`, `ExceptionGroup`, gc, tracemalloc). Consumer fields match producer via SSoT `_HEALTH_SNAPSHOT_FIELDS` in `_resource_registry.py`. Gate 15 rejects unmatched fields + unpaired `LRULockDict(...)`/`ort.InferenceSession(...)`. Wrapped `_thread_dispatch.dispatch_to_thread(label=...)` auto-increments cohort. Governor 5 verdicts → `axis="engine_resources"` (ADR-D5). Heap-snapshot via `tracemalloc=True` on N=5 deaf-cluster. Allowlist `# h4-allowlist`. Gate 15 STRICT; LENIENT until v0.54.0 (V-H4-13). **Sibling #15, #30, #34, #40, #42, #43.** Mission: `MISSION-h4-resource-hygiene-instrumentation-2026-05-19.md`.
48. **Falsifiability gates verify literal-string parity, NOT semantic correctness.** Aliasing one field to another to pass spec literal = semantic lie (F-006). When spec demands field runtime can't deliver: (a) change spec, OR (b) leave unimplemented + document gap. **Aliasing-to-pass FORBIDDEN.** Temp aliases require SUNSET-WINDOW. ADR-D15 supersedes ADR-D4. **Sibling #49, #50, #52.** Mission: `MISSION-A1-runtime-truth-remediation-2026-05-20.md`.
49. **Counter fields `*_estimate`/`*_count` MUST decay or be suffixed `_lifetime_*`/`_cumulative_*`.** Monotonic `+=` under gauge name = permanently BUDGET_EXCEEDED. Every cardinality-bounded resource emits `<resource>.cumulative_*` (forensic) + `<resource>.window_*` (rolling). Governor reads WINDOW. Legacy LENIENT dual-emit via `legacy_alias=` + `# a1-allowlist: sunset vX.Y.Z`. ADR-D14. **Sibling #47, #52.** Mission `MISSION-A1-runtime-truth-remediation-2026-05-20.md`.
50. **Self-observing fields MUST declare themselves; OBSERVATION PARADOX forbidden.** F-005: `asyncio.current_running_task_name` from inside `ResourceSnapshotter.run()` = always snapshotter itself. "current X"/"active Y" from inside observer MUST (a) prefix `observer.`/suffix `_at_observer_time`, OR (b) list-typed snapshot of OTHER tasks. ADR-D15. **Sibling #48.**
51. **Twin-name fields differing by freshness/math MUST disambiguate via suffix.** F-007: stale `to_thread.pool_size` vs live `asyncio.default_executor_state.pool_size`. F-014: `running_count` counted `not t.done()` (incl. await-blocked). Freshness → `_at_<event>`; math-vs-name → rename (`not_done_count`, `awaiting_count`). LENIENT `legacy_alias=` + sunset. ADR-D16. **Sibling #48, #49, #50.**
52. **Comment-vs-code mismatch in filter logic is P0 incident class.** F-001 (`observability/anomaly.py`, fixed — window comprehension selected samples OUTSIDE the window its comment described). Producer/consumer pairs sharing window MUST use consistent direction. Detection: baseline-age regression test (`baseline_age_s ≈ window_s ± tick_jitter`). Adjacent comment MUST describe SET selected + test anchor downstream. **Sibling #40.**
53. **HTTP path strings of producer↔consumer contract round-trip through ONE shared symbol — never independent literals.** B-P0-1: 16 sites used `/api/voice/degraded/ack`; FastAPI registered `/api/engine/degraded/ack`; ack inert v0.46.4..v0.49.36. Every frontend POST/PUT/PATCH/DELETE MUST be exercised by integration test against `TestClient(create_app())` asserting `status_code != 404`, OR import shared constants asserted by boundary test enumerating `app.routes`. Vitest `mockApiPost.toHaveBeenCalledWith` verifies INTENT, not contract. **Sibling #40, #45.** Mission: `MISSION-B-REMEDIATION-PLAN-2026-05-21.md` §11.
54. **Every `EngineDegradedStore.record()` MUST have paired clear-edge tied to verifiable HEALTHY verdict.** Record-without-clear = stale banner (consumer-side #49). B-P0-3: `emit_axis_entries` skipped non-BUDGET_EXCEEDED → stuck-at-breach forever. Categorical (LLM/dashboard) clears on first HEALTHY; numeric (governor) clears after N consecutive HEALTHY (default `tuning.cohort_clear_consecutive_healthy_threshold=3`). Clear also clears `OperatorAcksStore` row (B-P1-15). Flag `observability.features.cohort_axis_auto_clear` default True (INVERSE #34). Allowlist `# b-p0-3-allowlist`. **Sibling #42, #49.** Mission: `MISSION-B-REMEDIATION-PLAN-2026-05-21.md` §5.
55. **Mission closure referencing V-* gates MUST land corresponding rows in `OPERATOR-VALIDATION-BACKLOG-2026.md` SAME tag.** B-P0-4: v0.49.36 closed A.1+A.2 referencing V-A1/V-A2; backlog had ZERO matches; Mission B couldn't unblock. Report + backlog are SIBLING surfaces. Detection: pre-tag-cut `grep "V-<mission>-" backlog | wc -l == 0` = tag-cut hold. **Sibling #42.** Mission: `MISSION-B-REMEDIATION-PLAN-2026-05-21.md` §11.

*(56-67 reserved for the frozen Mission C/Σ-B promotion batch — superstate-gated; 68 DRAFT per Ω-3.)*

69. **A session/turn-owned state MUST have an explicit ownership flag written by the owning surface — never a live-status boolean proxy.** VTI-1: `_handle_speaking` used `output.is_playing` (False both BEFORE playback starts and after it ends) to decide SPEAKING→IDLE while TTS-out surfaces also wrote the state → per-frame SPEAKING↔IDLE flapping, duplicate lifecycle events, anti-echo duck released mid-turn, self-echo re-recording. Fix shape: `_speech_session_active` opened by speak/stream_text, closed by speak-finally/flush/cancel-chain/stop; poll handlers may take over only when ownership is released. Two uncoordinated writers of one state = the bug, whatever the proxy. **Sibling #40, #52.** Mission: `MISSION-VOICE-TURN-INTEGRITY-2026-07-01.md`.
70. **Recovery machinery shipped "observe-only" MUST carry a tracked wiring task — grep for callers before trusting a docstring that says "called by X".** VTI-5: `PipelineStateMachine.fire_watchdog`/`is_watchdog_expired` were built ("Phase 1: observe"), their docstrings claimed the heartbeat called them, and NOTHING ever did — THINKING zombies latched forever while the cure sat as dead code. Staged adoption (#3 North Star) requires the flip/wire-up to be a named task with a target version, like every LENIENT→STRICT gate. **Sibling #34, #52.** Mission: `MISSION-VOICE-TURN-INTEGRITY-2026-07-01.md`.
71. **Provider "available" ≠ "answerable" — empty-config sentinels MUST resolve to a concrete resource (or fail loudly at setup), never leak into per-request calls.** VTI-9: key-less installs left `mind.llm.default_model=""` → router normalised to `"default"` → Ollama 404 on EVERY turn while `llm doctor` said FULLY_AVAILABLE. Fix shape: the provider resolves the sentinel against its real inventory (`_ensure_concrete_model`, cached, loud WARN); explicit names still fail loudly (#48 — silent model substitution is a semantic lie). Availability probes must round-trip a unit of real work, not just reachability. **Sibling #44, #48.** Mission: `MISSION-VOICE-TURN-INTEGRITY-2026-07-01.md`.

## Testing Patterns

```python
# asyncio_mode=auto in pyproject; per-test @pytest.mark.asyncio() optional.

# Autouse: close RotatingFileHandler on root after each test
@pytest.fixture(autouse=True)
def _clean_handlers() -> Generator[None, None, None]:
    yield
    root = logging.getLogger()
    for h in list(root.handlers):
        if isinstance(h, logging.handlers.RotatingFileHandler):
            h.close()
    root.handlers.clear()

# Auth via token parameter, never monkeypatch
_TOKEN = "test-token-fixo"

@pytest.fixture()
def app() -> FastAPI: return create_app(token=_TOKEN)

@pytest.fixture()
def client(app: FastAPI) -> TestClient:
    return TestClient(app, headers={"Authorization": f"Bearer {_TOKEN}"})

# xdist-safe exception assert (#8)
with pytest.raises(Exception) as exc_info:
    do_something_that_raises()
assert type(exc_info.value).__name__ == "LLMError"

# SandboxedHttpClient mock — it builds its OWN httpx.AsyncClient and issues
# build_request(...) + send(req, stream=True) (NOT ._client.request), so mock at
# the httpx.AsyncClient boundary with a MockTransport returning a REAL response.
# Capture the real class first (factory recursing into the patched name hangs)
# and preserve follow_redirects=False (SSRF invariant — sandbox walks redirects).
_REAL_ASYNC_CLIENT = httpx.AsyncClient  # module level, before any patch

def _mock_async_client(handler):  # handler(req: httpx.Request) -> httpx.Response
    def _factory(*_a, **_kw):
        return _REAL_ASYNC_CLIENT(transport=httpx.MockTransport(handler), follow_redirects=False)
    return _factory

def handler(req: httpx.Request) -> httpx.Response:
    return httpx.Response(200, json={...})  # .json()/.text/.content all work; or content=<bytes>

with patch("httpx.AsyncClient", _mock_async_client(handler)):
    result = await my_plugin_func()
# Assert request shape (headers/url/method) via the real Request the handler sees,
# not a mock's call_args. (Plugins that patch SandboxedHttpClient itself —
# patch.object(plugin_mod, "SandboxedHttpClient", return_value=mock) — are a
# different, equally-valid boundary unaffected by the send/stream change.)

# Aliased imports (#2): patch real module, not sys.modules
import onnxruntime
with patch.object(onnxruntime, "InferenceSession", return_value=mock_sess): ...

# After module split (#20): patch the NEW path
with patch("sovyx.brain._model_downloader.httpx.AsyncClient", ...): ...
```

## Debugging Rules

1. **Audit first.** Grep all instances of same pattern. Map size before solving any single.
2. **Group by root cause.** 28 failing tests → how many distinct causes? Fix causes, not symptoms.
3. **Don't band-aid.** Can't explain WHY a fix works = not ready.
4. **One commit per root cause.** No partial pushes to CI for incremental testing.
5. **No shotgun debugging.** Setting same value in 3 places hoping one sticks → trace the actual read path.
6. **Local suite before push.** CI round-trips waste minutes + fragment reasoning.
7. **Full chain.** Config bug may affect CLI, dashboard, API.
8. **Regression tests.** Bug must never recur.
9. **Third fix→push→CI-fail = STOP.** Reassess approach.
10. **Windows mypy noise:** 9 platform-FPs (`AF_UNIX`, `os.sysconf`, `getrusage`, `open_unix_server`). Only errors OUTSIDE that list real. CI Linux = baseline.
11. **Closure protocol on bug class.** When fixing one site, grep ALL consumers of same flag/sentinel. State closure assertion in commit body. Bug classes surface in waves.
12. **`verify_gates.sh` validates ONE platform/Python — the CI matrix is the real gate.** Local gates run pytest on the dev box only; CI runs the matrix (`windows-latest` + `macos-latest` + self-hosted `sovyx-4core`, Python 3.11 **and** 3.12) and `publish.yml` SKIPS the PyPI upload if ANY matrix leg fails. A green local marker means "ready to push", NOT "CI will pass." Known **matrix-only** failure classes to pre-empt when authoring tests:
    - **`RuntimeError("Event loop is closed")` — Windows asyncio teardown** (`ProactorEventLoop`, py3.12). An async test that spawns a task/service and lets the loop close with a cancelled-but-un-awaited task (or a started pipeline never `stop()`ed) flakes ON WINDOWS ONLY. **Rule:** every async test that `start()`s a service or spawns a task MUST drain it before returning — `with contextlib.suppress(asyncio.CancelledError): await task` for each cancelled task + `await <obj>.stop()` for started services (see `tests/unit/voice/test_pipeline.py` teardown convention). The test *logic* can pass while teardown raises — it still fails the job.
    - **Tight wall-clock perf bounds** (`assert elapsed_ms < N`, `scan_duration_ms < N`) in NON-perf tests flake on slow/contended runners (windows-latest measured 795 ms for an in-memory scan against a 100 ms bound). Assert the INVARIANT (non-negative / finite / a generous sane ceiling), NOT speed — perf belongs in the perf-gate (#31) with trimmed-mean statistics, never a tight bound scattered in unit/property tests.
    - **Coarse clock** (#22) and **file handle-lock** (#30, the lint-rule fixture flake) are the other recurring Windows-CI-only classes.
    - **Gate/test self-checks that resolve against gitignored or environment-specific filesystem state** are NOT platform flakes — they pass locally (the state exists in the dev tree) and HARD-FAIL on every CI leg + a fresh `git clone` + the PyPI sdist (the state is structurally absent). **v0.49.55's publish FAILED exactly this way:** Gate 19's end-to-end test (`test_live_repo_has_no_dead_links`) resolved `docs-internal/*` docstring links against the filesystem, but `docs-internal/` is gitignored → present locally (gate PASS) → absent on the runner (76 false violations → hard fail on the Linux *hard* gates, both py3.11+3.12). No local run could ever catch it. **Rule:** any gate self-test whose inputs are gitignored / build-time / local-only MUST be applicable-gated — emit a SKIP verdict (exit 0) when its inputs are structurally absent, the same STRICT-when-applicable contract as Gate 11 (dashboard bundle). Fixed v0.49.56 (`cb3834c0`). **Sibling Gate 11 / #43.**
    Practical discipline: when a change touches async lifecycle / timers / files / subprocess **— or adds a gate whose inputs may be absent in a fresh checkout —** assume the matrix may catch what local cannot; write the teardown (or the applicability-skip) defensively up front. The operator surfaces CI-matrix failures (per `feedback_ci_watching` — don't `gh run watch`); on a surfaced failure, root-cause + fix the test hygiene (don't just re-run). **Exception — active remediation:** when YOU are shipping a fix for a known-broken pipeline (as in the v0.49.55→56 Gate-19 repair), a SINGLE post-push `gh run list`/run-status confirmation that the fix cleared the failure is responsible verification, NOT prohibited watching.
13. **Unit mocks structurally cannot catch turn-lifecycle coupling — "the realtime flow works" claims require a live-timing exercise of the DEFAULT config path.** 2026-07-01: the default streaming voice turn shipped broken in 5 coupled ways (AP #69/#70) while 17k unit tests were green — every seam was mocked, and voice had never run live anywhere (63 MB of daemon logs, zero pipeline events). Before asserting a realtime pipeline works: exercise the default path with real timing (integration/soak or a real-component smoke), and grep any live run for `pipeline.state.invalid_transition` — the observe-only validator screams exactly where the mocks were lying. Local shell note: `verify_gates.sh` launched from a NESTED background bash can fail diagnostics tests with exit `0xC0000142` (msys subprocess DLL-init) — run gates from a PowerShell-hosted bash instead; the failures are environmental, not code.

## Working Style

**On any task:** (1) read scope + deps; (2) check existing patterns; (3) follow conventions; (4) tests ≥95% on modified files + edge cases; (5) `./scripts/verify_gates.sh`; (6) conventional commit, body explains WHY.

**Tests:** never workarounds (if test needs patching to pass, prod needs better interface — e.g. `create_app(token=...)` over monkeypatch); DI > mocking; one assertion pattern (xdist-safe #8); delete dead workarounds.

**God file split:** public surface stable (`__init__.py` re-exports); one responsibility per `_*.py`; migrate tests in same commit (#20); preserve docstring on `__init__.py`.

**Parallel subagents on the shared tree:** their prompts MUST forbid `git stash`/`git checkout`/`git reset`/any tree-wide git state command (2026-07-01 incident: an agent's stash captured 38 files of concurrent work and a mis-indexed drop nearly destroyed operator WIP). To prove pre-fix behavior, invert the specific edit with the Edit tool — never via git. Prefer worktree isolation for agents that must mutate many files.

## Documentation Sync Contract (MANDATORY)

Docs are part of the change, not an afterthought. A behavior-changing commit MUST update, in the SAME commit:

1. **Module docstrings** of every touched module whose header narrates flow / state fields / step counts (AP #52: comment-vs-code mismatch is a P0 incident class; AP #69/#70 both hid behind stale docstrings). Numeric claims in comments ("four steps", "5 fields", "called by X") are liabilities — re-count them or drop the number.
2. **The Docs Map row(s)** below covering the touched subsystem — public page under `docs/` AND the internal canonical doc. If neither needs changing, say so in the commit body ("docs-sync: n/a — internals only").
3. **CLAUDE.md itself** when the change alters: gates, repo layout, conventions, tuning namespaces, CLI surface, or teaches a new anti-pattern (append, never renumber; 56-67 reserved, next free ≥ 72).
4. **Mission/ADR lifecycle:** closing a mission = archive it (footer + `archive/INDEX.md` row + GOVERNANCE-INDEX repoint) in the same session — never leave CLOSED files sitting in `missions/`. New docstring links into `docs-internal/` must respect Gate 19 + `_meta/NAME-LOCK-REGISTRY.md`.

Definition of done: *a senior dev reading only the docs would not be misled by this change.*

## Docs Map (SSoT — which doc covers what)

| Subsystem | Code root | Public doc (`docs/`) | Internal canonical (`docs-internal/`) |
|---|---|---|---|
| Cognitive loop (7 phases) | `cognitive/`, `brain/` | `modules/cognitive.md` + `modules/brain.md` | `architecture/cognitive-loop.md` (CANONICAL) + `modules/cognitive.md` |
| Voice pipeline + turn | `voice/pipeline/`, `voice/cognitive_bridge.py` | `modules/voice.md` | `archive/missions-completed/MISSION-VOICE-TURN-INTEGRITY-2026-07-01.md` (turn semantics) |
| Voice capture + health | `voice/capture/`, `voice/health/` | `modules/voice-troubleshooting-*.md` | `ADR-voice-capture-health-lifecycle.md`, `ADR-voice-bypass-tier-system.md`, `ADR-voice-mixer-sanity-l2.5-bidirectional.md` |
| Voice calibration | `voice/calibration/` | `modules/voice-calibration.md` | `ADR-voice-*` + KB rotation: `docs/contributing/voice-kb-rotation.md` |
| LLM routing/providers | `llm/` | `llm-router.md` | `missions/MISSION-c6-llm-provider-cognitive-loop-integrity-2026-05-18.md` |
| Engine/config/degraded | `engine/` | `configuration.md` | ADR-D5/D14/D15/D16 + `missions/MISSION-c4-degraded-mode-banner-2026-05-17.md` |
| Dashboard (SPA + API) | `dashboard/`, `dashboard/src/` | `modules/dashboard*.md` | `MISSION-c5-…` (bundle integrity), Mission D register |
| CLI | `cli/` | `cli-reference.md` + `modules/cli.md` | — |
| Resource hygiene/observability | `observability/` | `operations/` pages | `missions/MISSION-h4-resource-hygiene-instrumentation-2026-05-19.md`, ADR-D14/D15/D16 |
| Governance/missions | — | — | `GOVERNANCE-INDEX.md` (ENTRYPOINT) → PLATFORM-SUPERSTATE, GATE-MATRIX, NAME-LOCK-REGISTRY |

**Reading order for a new session:** CLAUDE.md → auto-memory `MEMORY.md` → `docs-internal/GOVERNANCE-INDEX.md` → the Docs Map row for your subsystem.

## Deploy Flow

1. Bump `version` in `pyproject.toml` (single source — `src/sovyx/__init__.py` reads via `importlib.metadata.version`).
2. `uv lock` (CI enforces `--check`).
3. `git commit` + `git push origin main` — then, BEFORE pushing the tag, run the **Actions-availability pre-flight** (see below).
4. `git tag vX.Y.Z` + `git push origin vX.Y.Z` → `publish.yml`: CI gate → dashboard build → `uv build` → PyPI (OIDC) → Release → Docker.
5. CI fail on tagged commit: fix + commit + re-tag (`git tag -d vX.Y.Z && git tag vX.Y.Z && git push origin vX.Y.Z --force`).

**Actions-availability pre-flight (v0.49.59 incident):** the prior-tag check (`gh run list --workflow=publish.yml`) proves the PIPELINE was healthy at the LAST tag — it cannot see an org-level outage that happened since. v0.49.59 tagged clean against a green v0.49.58 and then every hosted-runner job died in 3-4 s: **`sovyx-ai` org locked for a billing issue** (annotation: "The job was not started because your account is locked due to a billing issue"), which kills GitHub-hosted jobs at "Set up job" (zero steps) AND freezes self-hosted job assignment. So, ~60 s after pushing main (step 3):

```bash
RUN=$(gh run list --branch main --workflow=ci.yml --limit 1 --json databaseId --jq '.[0].databaseId')
gh api repos/sovyx-ai/sovyx/actions/runs/$RUN/jobs \
  --jq '.jobs[] | select(.conclusion=="failure") | {name, steps: [.steps[]|select(.conclusion=="failure")|.name]}'
```

A job with `conclusion=failure` and an EMPTY failed-steps list = infrastructure lock (billing / runner-pool outage), NOT a code failure — **hold the tag**. This is a bounded one-shot check on the main push, not prohibited CI-watching (`feedback_ci_watching` governs post-TAG watching). Recovery once the org is unlocked: `gh run rerun <publish-run-id>` (or `--failed`) — the tag and commit are intact, NO re-tag needed; a queued run that expired can be re-run from the workflow page within 30 days. **Recovery must SWEEP the whole outage window, not just the release run:** any run created during the lock carries hosted-job corpses (instant failures) alongside self-hosted jobs that ran fine after unlock — a mixed red run that resurfaces later as a false "CI broke again" alarm (this happened ~1 h after the v0.49.59 unlock). Sweep: `gh run list --limit 10`, and for every `failure` run overlapping the window whose failed jobs carry the lock annotation, `gh run rerun <id> --failed`.

Per `feedback_ci_watching`: don't `gh run watch` — operator surfaces failures via validation backlog.

### Two-Tier GA (voice)

Per `MISSION-voice-final-skype-grade-2026.md`: **v0.30.0** = single-mind GA (Phases 1-7). **v0.31.0** = FINAL multi-mind GA (Phase 8). Phase 8 goes in v0.30.x patches or v0.31.0 — never blocks v0.30.0.

## Mission Lifecycle

- **Active:** `docs-internal/missions/MISSION-*.md` w/ task IDs + Phase boundaries mapped to versions.
- **ADRs:** `docs-internal/ADR-*.md` are CANONICAL — referenced from code docstrings. Never delete; supersede via new ADR.
- **Completed/superseded:** archive to `docs-internal/archive/missions-completed/` w/ `## Archive Footer`. Update `archive/INDEX.md`.
- **Forensic resolutions:** `docs-internal/archive/forensics-resolved/`.
- **Never delete** mission/ADR that produced shipped code. Pure orphans (no-code-produced, byte-identical dupes) only valid DELETE targets.

Closing a mission task in commit: reference mission file + task ID in body (e.g. `Mission: docs-internal/missions/MISSION-voice-final-skype-grade-2026.md §Phase 1.T2`); follow-up `docs(mission):` marks ✅.

## Deep Reference

- Public docs (MkDocs): `docs/`.
- Internal planning/audits/specs: `docs-internal/` (gitignored), searchable by number.
- Code patterns: `tests/unit/` mirrors `src/sovyx/`.
- Frontend types: `dashboard/src/types/api.ts` + `schemas.ts`.

## Persistent Memory

Auto-memory at `C:\Users\guipe\.claude\projects\E--sovyx\memory\`. Index: `MEMORY.md` — load every linked entry at session start. Index lines ≤150 chars; detail in linked file.

- **Authority:** `feedback_*` = SAME authority as CLAUDE.md, OVERRIDE defaults. North Star is canonical summary of current `feedback_*` set.
- **Project (`project_*`):** historical context — missions, incidents, investigations.
- **User (`user_*`):** preferences, role.
- **Reference (`reference_*`):** external systems.

Before recommending from memory, verify referenced file/function still exists. **Memory state at write time ≠ current state.** Grep before relying.
