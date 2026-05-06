# Changelog

All notable changes to Sovyx will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

(none — every shipped delta is in v0.31.0-rc.7 below)

## [0.31.0-rc.7] — 2026-05-06

Operator-UX paranoid pre-GA round 5 on top of v0.31.0-rc.6. Four
parallel QA agents re-validated rc.6 with deep edge-case coverage;
Agent 2's UX simulation found that rc.6's most-prominent fix (A.4
"verdict shows signed/unsigned banner") was **broken in both
directions** because the renderer read `profile.signature` from the
in-memory frozen profile (always `None`) instead of the disk-side
truth from persistence. Plus 3 other operator-visible UX gaps. rc.7
closes all 4 without paliativos per `feedback_enterprise_only`.

### Fixed

- **Verdict renderer signing-status surface broken (Agent 2 NEW.2/NEW.3, CRITICAL).**
  Pre-rc.7 `_render_calibration_verdict` read `profile.signature is not
  None` to render the green "✓ signed" or dim "unsigned" banner. But
  `engine.evaluate()` at `engine.py:307` sets `signature=None`
  unconditionally on the frozen `CalibrationProfile`, and
  `save_calibration_profile` injects the signature into a serialized
  dict copy at `_persistence.py:334` — never mutates the in-memory
  profile object. Result: every clean `--calibrate` run rendered
  "Profile is unsigned (LENIENT only; STRICT rejects)" — even when
  `--signing-key` worked. The "✓ signed" branch was UNREACHABLE from
  `--calibrate`. Defeated the operator-validation gate Step 6 contract.

  Fix: `save_calibration_profile` now returns `SaveProfileResult(path,
  signed)` (NamedTuple); `CalibrationApplier.apply` propagates `signed`
  to `ApplyResult.signed: bool | None` (None on dry-run paths); the
  renderer reads `apply_result.signed` — the disk-side truth. The 3
  scenarios render correctly:
  * Signed profile → green ✓ + "Loadable in STRICT mode"
  * Unsigned profile → dim hint + "--signing-key on next --calibrate"
  * Dry-run → no banner (nothing persisted)
  Regression: `tests/unit/cli/test_doctor_calibrate.py::TestRenderCalibrationVerdictSignedStatus`
  (3 cases pinning the contract).

- **`_ProfileReview` text promised rollback button that didn't exist (Agent 2 NEW.4, CRITICAL).**
  Pre-rc.7 the rewritten i18n in en/pt-BR/es promised "If the
  calibration didn't help, you can roll back below" — but
  `_ProfileReview.tsx:36-62` rendered ONLY a confirm button. The
  `calibration.review.rollback` i18n key existed but was unused.
  Operator on the success state looked below for a rollback button,
  found nothing, got confused. Rewrote the text in all 3 locales to
  honestly point at `sovyx doctor voice --calibrate --rollback` (the
  CLI command that's functional today) instead of a non-existent UI
  affordance.

- **`docs/getting-started.md` claimed "signed CalibrationProfile" by default (Agent 2 NEW.5, HIGH).**
  Two doc lines (165 + 197) said `--calibrate` produces a "signed
  CalibrationProfile". The actual default produces an UNSIGNED profile
  (LENIENT-loadable) unless `--signing-key` is passed. Operator who
  read the doc, ran `--calibrate`, then saw the rc.6/rc.7 verdict say
  "unsigned" would panic. Updated both lines to honestly state
  "(unsigned by default; pass `--signing-key` to sign)".

- **`--signing-key` fail-fast missed malformed PEM + non-Ed25519 (Agent 2 NEW.1, MEDIUM).**
  Pre-rc.7 the rc.6 fail-fast only checked `Path.is_file()`. Garbage
  bytes / RSA keys / wrong-algorithm PEMs all passed the check, ran
  the 8-12 min diag, then landed unsigned with the only forensic
  surface being a structlog WARN. Now the validation also calls
  `_load_private_signing_key()` at flag-parse time (cost <1ms) and
  converts its `RuntimeError` (unparseable PEM, wrong algorithm) into
  a Click `BadParameter` with the underlying reason. Operator typo
  with bad key contents now also fails in milliseconds.
  Regression: `TestSigningKeyFailFast::test_signing_key_malformed_pem_rejected_at_flag_parse`
  + `::test_signing_key_rsa_not_ed25519_rejected_at_flag_parse` +
  `::test_signing_key_valid_ed25519_passes_validation` (replaces the
  prior placeholder-PEM happy-path test which would now fail the
  deeper validation).

### Mission

Operator-UX paranoid pre-GA round 5; rc.6 archive footer remains
accurate. rc.7 closes the gap between "rc.6 fixes look right at the
code level" (Agents 1/3/4 said SHIP) and "rc.6 fixes are demonstrably
broken on the operator path" (Agent 2 simulated the rendered output
+ found 4 critical regressions introduced by rc.6 itself). The
operator's directive "garantir que o usuário não tenha nenhuma dor
de cabeça" is now genuinely honored: every clean `--calibrate` run
renders a verdict that matches reality, every dashboard text matches
what's on screen, every operator typo on `--signing-key` (typo path /
malformed PEM / wrong algorithm) fails fast with an actionable error.

## [0.31.0-rc.6] — 2026-05-06

Operator-UX paranoid pre-GA round 4 on top of v0.31.0-rc.5. Four
parallel QA agents re-validated rc.5 with a NEW emphasis from the
operator: "garantir que o usuário não tenha nenhuma dor de cabeça"
(zero operator headaches). Agent 2's UX-focused pass identified 11
UX FAIL items where the operator's real-world journey hit
silent/confusing/missing surfaces despite the mission §0 contract
being fully met in code. rc.6 closes the 3 critical + 5 secondary UX
findings without paliativos per ``feedback_enterprise_only``.

### Fixed

- **`--signing-key /tmp/nope` silently unsigned (Agent 2 A.4/A.5/G.6, HIGH).**
  Pre-rc.6 an operator typo on `--signing-key` would (a) silently
  proceed through the 8-12 min diag and (b) land an unsigned profile
  with the only forensic surface being a structlog WARN buried in
  ``$data_dir/logs/sovyx.log``. The operator validation gate Step 6
  was effectively NOT runnable without log-grep. Two fixes:
  - Fail-fast `Path.is_file()` check at flag-parse time in
    ``cli/commands/doctor.py``. Operator typo now hits a Click
    BadParameter error in milliseconds with an actionable hint
    pointing to `scripts/dev/generate_calibration_signing_key.py`.
  - Verdict renderer now surfaces signing status: green
    "✓ Profile is signed (Ed25519)" or dim "Profile is unsigned".
    Operator no longer needs `tail sovyx.log | grep` to verify.
  Regression: `tests/unit/cli/test_doctor_calibrate.py::TestSigningKeyFailFast`
  (2 cases).
- **`--evaluate-rules` crash on non-Linux (Agent 2 A.3/G.4, MEDIUM).**
  Pre-rc.6 a Windows operator following `--help` got a Python
  exception from the Linux-only fingerprint/amixer probes. Now
  short-circuits at function entry with EXIT_DOCTOR_UNSUPPORTED (5)
  + a friendly message pointing at `sovyx doctor voice` (cross-
  platform). Regression: `test_evaluate_rules_non_linux_returns_unsupported`.
- **`_ProfileReview` text promised decisions but listed none (Agent 2 B.7, MEDIUM).**
  Pre-rc.6 the green terminal banner displayed
  "Sovyx applied the following decisions for your hardware:" with no
  list rendered below — operator confusion at the most-celebratory
  state. Rewrote the i18n text in en/pt-BR/es to point at the
  persisted profile path (already shown above) + cite the
  `sovyx doctor voice --calibrate --show` CLI for decision details
  + highlight the rollback affordance.
- **Hardcoded English fallback strings in calibration store (Agent 2 B.6).**
  pt-BR/es operators saw English "Failed to capture fingerprint" /
  "Failed to start calibration" / "Failed to load calibration job" /
  "Failed to cancel calibration" / "Calibration WebSocket connection error"
  fallbacks despite full dashboard i18n. Wired all 5 to ``i18n.t()``
  with new ``voice.calibration.error.api_*`` + ``ws_connection_error``
  keys in en/pt-BR/es voice.json.
- **bash `--only` accepts unknown layer letters silently (Agent 2 C.3).**
  Operator typo `--only A,J,Z` produced a successful-looking run with
  empty tarball (no layer matched). Now validates against the canonical
  letter set (A..K) at entrypoint + rejects with exit 2 + actionable
  error citing the bad letter + the valid set. Whitespace-tolerant.
  Regression: `tests/unit/voice/diagnostics/test_bash_only_flag.py::TestOnlyFlagEntrypointValidation`
  (4 cases).

### Added

- **README + `sovyx init` breadcrumb to calibration wizard (Agent 2 D.6/E.2).**
  Sony VAIO + Linux Mint + PipeWire operator (the canonical case)
  following the README §3 "5-minute voice" path could hit silent-mic
  with zero pointer to the calibration system. README §3 now includes
  Step 4 with `sovyx doctor voice --calibrate --non-interactive` +
  rationale. `sovyx init` post-creation hint also points operators
  at the wizard for hardware-pinned issues.
- **`docs/configuration.md` documents `voice.calibration_wizard_enabled` (Agent 2 D.2).**
  Canonical config reference page now has a "Voice calibration wizard"
  subsection covering the YAML key, env override, and runtime toggle.

### Mission

Operator-UX paranoid pre-GA round 4; rc.5 archive footer remains
accurate. rc.6 closes the gap between "mission §0 promises met in
code" and "operator real-world journey runnable without dor-de-cabeça".
The 5 remaining UX UNCERTAINs from Agent 2 (operator validation Step
5/8 instructions, --explain rule trace semantics, etc.) are documented
backlog items — all 10 operator validation gate steps now genuinely
runnable from rc.6's surfaces. v0.31.0 GA promotion proceeds once the
operator confirms rc.6 on the canonical Sony VAIO host.

## [0.31.0-rc.5] — 2026-05-06

Paranoid pre-GA closure round 3 on top of v0.31.0-rc.4. Four parallel
QA agents re-validated rc.4 (zero src/ touches, test/CI hardening
only); this RC closes the 2 FAIL items + 4 priority UNCERTAIN items
without paliativos per ``feedback_enterprise_only``. rc.5 is a strict
test/CI/docs superset of rc.4 with **zero production code changes**.

### Fixed

- **8 misclassified `@pytest.mark.integration` markers (Agent 2 A.4).**
  rc.4 closed E.3 by removing the marker from `TestConcurrentStartRaceQaFix5`
  on the rationale that httpx + ASGITransport in-process tests don't
  fit the marker's documented criteria ("real ML, SQLite heavy IO,
  cross-component wiring"). The same logic applies to 8 OTHER classes
  in the same file: `TestPublicSurface`, `TestRuleRegistry`,
  `TestEngineConfigFlag`, `TestDashboardEndpointWiring`, `TestCLISurface`,
  `TestStartEndpointBehavior`, `TestCancelEndpointBehavior`,
  `TestWebSocketAuthBehavior`. Pre-rc.5 these 20 audit tests were
  silently skipped from default CI; a regression to public surface,
  rule registry, route registration, CLI flags, or auth would have
  shipped green. Marker removed; tests now run on every push.
  `TestCorpusSynth` keeps the marker (genuine cross-component:
  end-to-end `triage_tarball` invocation across 8 scenarios).
- **Race tests leaked `_START_LOCKS` instance (Agent 2 A.2).** Pre-rc.5
  `TestConcurrentStartRaceQaFix5` reassigned `vc_route._START_LOCKS`
  to a fresh `LRULockDict(maxsize=256)` but never restored the
  original in `finally`. Functionally safe (locks self-prune by key)
  but contradicted the test docstring's "no state leaks into the next
  test" promise. Both tests now capture `_original_start_locks` before
  reassignment + restore in `finally`.

### Added

- **Strengthened Promise 11 `--explain` assertion (Agent 3 #1).** Pre-rc.5
  `test_evaluate_rules_with_explain_flag_invokes_explain_renderer`
  asserted `"R10" in clean` — but `rule_id` always renders in the
  decisions table at `doctor.py:979`, regardless of `--explain`. A
  regression that wired `explain=True` to a no-op would have landed
  green. Now asserts `"Rule trace" in clean` AND `"matched:" in clean`
  — both fire only inside the explain-only block at `doctor.py:994-1003`.
- **Multi-token revert continuation e2e test (Agent 3 #2).** New
  `test_lifo_walker_continues_past_middle_revert_failure` drives 3
  decisions where decision[1]'s revert raises RuntimeError — asserts
  the LIFO walker continues to revert[0] anyway (production code at
  `_applier.py:563-606` is correct; this test pins the contract).
- **Privacy gate widened to engine/measurer/fingerprint/runner (Agent 3 #3).**
  Pre-rc.5 the privacy CI gate covered `_wizard_orchestrator`,
  `_wizard_progress`, `_persistence`, `_kb_cache`, `_applier` but NOT
  the 4 modules in `voice.calibration.engine`, `voice.calibration._measurer`,
  `voice.calibration._fingerprint`, `voice.diagnostics._runner`. All
  4 were CLEAN by inspection but UNGATED. New test classes
  `TestEngineEmissionPrivacy`, `TestMeasurerEmissionPrivacy`,
  `TestFingerprintEmissionPrivacy`, `TestRunnerEmissionPrivacy` walk
  each module's logger through the privacy heuristic with synthetic
  emission scenarios; the runner test covers both success
  (`full_diag_started/completed`) and failure (`full_diag_failed`)
  paths.
- **CLAUDE.md anti-pattern #38 (Agent 2 F.4).** Documents the
  lazy-import patch-target lesson from rc.4's `--evaluate-rules`
  tests + the cross-platform `create=True` injection pattern from
  rc.4's POSIX cancellation tests. Extends anti-pattern #20 (test
  patches must follow module splits) to lazy-import boundaries.

### Mission

Paranoid pre-GA closure round 3; rc.4 archive footer remains accurate.
rc.5 is a strict superset of rc.4 — zero contract changes, zero src/
touches, only test/CI/docs hardening. Operator validation gate steps
unchanged from rc.4; v0.31.0 GA promotion proceeds once the operator
confirms rc.5 on the canonical Sony VAIO host.

## [0.31.0-rc.4] — 2026-05-06

Paranoid pre-GA closure round 2 on top of v0.31.0-rc.3. Four parallel
QA agents re-validated rc.3; this RC closes the 2 FAIL items + the 3
top-priority coverage gaps without paliativos per
``feedback_enterprise_only``. Mission §0 verdict from Agent 4 was
``GO with operator validation gate``; rc.4 lifts the remaining "real
findings" before promoting to v0.31.0 GA.

### Fixed

- **CI silently skipped `TestConcurrentStartRaceQaFix5` (Agent 2 E.3).**
  The class was decorated ``@pytest.mark.integration`` per the rc.3
  pattern, but the marker's documented criteria are "real ML models,
  SQLite heavy IO, or cross-component wiring" — none of which apply
  to a httpx + ASGITransport in-process test. The default
  ``-m 'not integration'`` filter in ``pyproject.toml`` excluded the
  race regression from CI; a future regression to ``_START_LOCKS``
  would have slipped past CI green. Marker removed.
- **`reason_kind` added to `CLOSED_ENUM_FIELDS` but never emitted (Agent 2 D.3).**
  Speculative addition in rc.3; production emits ``reason=`` (already
  in DYNAMIC_TEXT_FIELDS) with bounded values. Removed the unused
  entry to keep the test contract aligned with production.

### Added

- **POSIX cancellation path coverage (Agent 3 Promise 2).** Pre-rc.4
  every ``TestCancellation`` test forced ``sys.platform == "win32"``.
  The production cancellation chain on Linux uses ``os.killpg`` +
  ``start_new_session=True`` — ZERO direct test coverage. New tests:
  - ``test_posix_spawn_passes_start_new_session_true``: asserts the
    POSIX spawn kwarg.
  - ``test_windows_spawn_does_not_pass_start_new_session``: asserts
    Windows spawn does NOT carry the kwarg.
  - ``test_posix_cancel_calls_killpg_with_sigterm``: asserts the
    POSIX path uses ``os.killpg`` (not ``os.kill``) with SIGTERM.
  - ``test_posix_cancel_escalates_to_killpg_sigkill``: asserts the
    grace-expired escalation uses ``os.killpg(SIGKILL)``.
  - ``test_posix_cancel_completes_within_grace_plus_sigkill_wait``:
    wallclock-bounded assertion on the graceful exit path.
- **VoiceStep single-flow conditional coverage (Agent 3 Promise 8).**
  Pre-rc.4 the single-mount conditional at ``VoiceStep.tsx:212`` had
  no vitest coverage; a regression that re-introduced dual-mount of
  ``<HardwareDetection /> + <VoiceCalibrationStep />`` would land
  green. New tests:
  - flag OFF → renders HardwareDetection only.
  - flag ON → renders VoiceCalibrationStep only.
  - asserts NEVER dual-mounts under any feature-flag state.
- **`--evaluate-rules` behavior coverage (Agent 3 Promise 11).** Pre-rc.4
  only the help-text registration was tested. New tests drive the
  full dry-eval flow through ``_run_voice_calibrate_evaluate_rules``:
  - asserts ``capture_fingerprint`` + ``capture_measurements`` +
    ``CalibrationEngine.evaluate`` invoked exactly once with
    ``triage_result=None`` + ``diag_tarball_root=None``.
  - asserts ``run_full_diag`` / ``triage_tarball`` /
    ``CalibrationApplier.apply`` are NEVER invoked (zero diag, zero
    apply contract per Mission §0 promise 11).
  - asserts ``--explain`` propagates into the verdict renderer.
  - asserts fingerprint failure → EXIT_DOCTOR_GENERIC_FAILURE without
    invoking downstream stages.

### Mission

Paranoid pre-GA closure round 2; rc.3 archive footer remains accurate.
rc.4 is a strict superset of rc.3 — zero contract changes, only
test/CI hardening. Operator validation gate steps unchanged from
rc.3; v0.31.0 GA promotion proceeds once the operator confirms rc.4
on the canonical Sony VAIO host.

## [0.31.0-rc.3] — 2026-05-06

Paranoid pre-GA audit pass on top of v0.31.0-rc.2. Four parallel
QA agents re-validated rc.2 with deeper coverage; this RC closes
2 real bugs + 1 contract gap + 1 missing test + 1 hygiene leak
without paliativos per ``feedback_enterprise_only``. Mission §0
verdict from Agent 4 was ``GO for v0.31.0 GA``; rc.3 lifts the
remaining "real findings" before GA promotion.

### Fixed

- **`extras["current_prompt"]` leaks into terminal snapshots (Agent 3 #10).**
  The slow-path tail loop populates ``extras["current_prompt"]``
  during SLOW_PATH_DIAG; pre-rc.3 a mid-flight failure (e.g.
  ``triage_tarball`` raising) exited via ``_emit_failed`` /
  ``_emit_fallback`` / ``_emit_cancelled`` without clearing the
  key, so the dashboard kept rendering a "say X" / "stay silent"
  ``<CapturePrompt>`` card on top of a TerminalView. ``_transition``
  now strips ``current_prompt`` whenever the destination
  ``WizardStatus.is_terminal`` is true (single shared mutation
  point — every emitter inherits the contract uniformly). Regression
  in `tests/unit/voice/calibration/test_wizard_orchestrator.py::TestTerminalCurrentPromptStrip`
  (6 cases covering FAILED / FAILED+rolled_back / CANCELLED /
  FALLBACK / run() top-level handler / non-terminal preservation).
- **`_revert_mind_config_voice` swallowed write failures silently (Agent 1 #6).**
  Pre-rc.3 ``_restore_mind_yaml_voice_field`` ``return``-ed silently
  on OSError / yaml.YAMLError during revert, so a partial-rollback
  that left mind.yaml in an inconsistent state was indistinguishable
  from a clean revert in the logs — operators triaging "auto-rollback
  fired but my voice config is still broken" had no forensic evidence.
  Helper now raises ``_MindYamlMutateError``; the LIFO rollback
  walker catches via its generic ``except Exception`` clause and
  emits ``voice.calibration.applier.rollback_step_failed`` with
  ``exception_type="_MindYamlMutateError"``, then continues to the
  next token (best-effort semantics preserved). Regression in
  `tests/unit/voice/calibration/test_applier.py::TestRestoreMindYamlSurfacesErrors`
  (4 cases) + `TestRollbackEmitsStepFailedOnRevertWriteError` (1
  end-to-end case driving the canonical chain).

### Added

- **Privacy CI gate covers `_applier` emission sites (Agent 3 #1).**
  Pre-rc.3 ``tests/integration/test_telemetry_privacy_audit.py``
  patched the ``wo`` / ``wizard_progress`` / ``persistence`` /
  ``kb_cache`` loggers but NOT ``_applier.logger``; the wizard
  end-to-end tests mocked ``CalibrationApplier.apply`` so the 7
  applier emission sites (``apply_started`` / ``apply_succeeded`` /
  ``apply_failed`` / ``apply_failed_with_rollback`` /
  ``rollback_step_failed`` / ``linux_mixer_applied`` /
  ``mind_config_voice_applied`` and revert pairs) were never
  walked through the privacy heuristic. A future regression
  emitting raw ``mind_id`` instead of ``mind_id_hash`` on (e.g.)
  ``apply_failed_with_rollback`` would have slipped past CI. New
  ``TestApplierEmissionPrivacy`` class drives the real apply chain
  through synthetic ``register_target_class_pair`` registrations
  (4 scenarios: success / dry-run / sync-fail / rollback-chain).
- **Concurrent-POST integration test for QA-FIX-5 race (Agent 2 #18).**
  rc.2 added a per-mind ``LRULockDict``-backed asyncio.Lock around
  ``start_calibration_job``'s ``(in-flight check + register)`` but
  shipped without an integration test. New
  ``TestConcurrentStartRaceQaFix5`` fires
  ``asyncio.gather`` of two POSTs (same mind_id → exactly one 202
  + one 409; distinct mind_ids → both 202).
- **`_cleanup` removes per-pid prompts-err capture file (Agent 2 #8).**
  rc.2's ``prompt_emit_structured`` writes stderr to
  ``/tmp/.sovyx_prompts_err.$$``; the inline ``rm -f`` covers the
  happy path but a SIGTERM/SIGINT/SIGHUP between the echo and the
  rm leaks the file. Trap-EXIT ``_cleanup`` now mops it up. SIGKILL
  inherently leaks one such file per process death (no userspace
  handler can run); the file is ≤ 4 KB so the leak is bounded by
  max PID. Regression in
  `tests/unit/voice/diagnostics/test_bash_only_flag.py::TestPromptsErrFileCleanup`
  (2 cases — grep-regression + functional cleanup verification).

### Mission

Paranoid pre-GA audit closure; rc.2 archive footer remains
accurate. rc.3 is a strict superset of rc.2 — every fix landed
without altering rc.2 contracts. Operator validation gate steps
unchanged from rc.2; v0.31.0 GA promotion proceeds once the
operator confirms rc.3 on the canonical Sony VAIO host.

## [0.31.0-rc.2] — 2026-05-06

QA-driven hardening pass on top of v0.31.0-rc.1. Five-agent
enterprise audit identified one critical regression + four
observability/correctness gaps; this RC closes them all without
paliativos per ``feedback_enterprise_only``.

### Fixed

- **`ApplyError(decision=None)` AttributeError chain (CRITICAL).**
  The ``_mutate_mind_yaml_voice_field`` helper raised
  ``ApplyError(decision=None)`` when ``mind.yaml`` was missing /
  malformed / unwritable; the outer ``except ApplyError as exc:`` in
  ``CalibrationApplier.apply`` then crashed at ``exc.decision.target``
  AttributeError, masking the original failure. Helper now raises a
  private ``_MindYamlMutateError`` and the async handler
  ``_apply_mind_config_voice`` wraps with the actual decision context.
  Regression: `tests/unit/voice/calibration/test_applier.py::TestMindYamlHelperRaiseRegressions`
  (3 cases).
- **Signing-key path-not-found silent degradation (MEDIUM).** Operator
  passing ``--signing-key /tmp/nope`` (typo / CI misconfig / file
  deleted between resolve+apply) silently degraded to unsigned write
  with zero observability. New event
  ``voice.calibration.profile.signing_skipped{reason="key_path_missing"}``
  fires when the path is supplied but ``is_file()`` returns False.
  Regression: `tests/unit/voice/calibration/test_signing_verification.py::TestSigningKeyPathMissingObservability`
  (2 cases).
- **`prompts.jsonl` write-failure swallowed silently (MEDIUM).** Bash
  helper ``prompt_emit_structured`` used ``2>/dev/null || true`` after
  the append; ENOSPC/EACCES failures were invisible — dashboard saw
  zero prompts mid-run with zero forensic evidence. Now: stderr
  captured, first failure logs ``log_warn`` with path + errno, a
  session-level guard suppresses subsequent failures so a sustained
  write-fail doesn't flood the runlog.
- **Migration walker exception narrowness (LOW).**
  ``migrate_to_current``'s per-step ``except (KeyError, TypeError,
  ValueError)`` let RuntimeError / AttributeError / OSError /
  AssertionError from custom migrations propagate uncaught, defeating
  the typed ``CalibrationProfileMigrationError`` contract that the
  loader's catch site expects. Walker now catches Exception (with
  ``CalibrationProfileMigrationError`` re-raised as-is so we don't
  double-wrap). Regression: `tests/unit/voice/calibration/test_migrations_registry.py`
  (3 new cases — RuntimeError, AttributeError, no-double-wrap).
- **Race in `start_calibration_job` between in-flight check + spawn (LOW).**
  Two near-simultaneous POSTs for the same mind_id could both pass
  the file-based ``_job_in_flight`` check before either registered
  into ``_active_jobs``, racing into duplicate spawns that would
  corrupt the JSONL progress file. Now: per-mind asyncio.Lock
  (``LRULockDict``-backed for memory hygiene per anti-pattern #15)
  serialises the (check + register) so concurrent submissions for
  the same mind serialise to "first wins, second gets 409".

### Added

- ``CLAUDE.md`` anti-patterns #36 + #37 from this mission's lessons:
  - #36: ``patch.object`` on async functions auto-detects via Python
    3.8+ ``AsyncMock`` (covers the 17 P2.T3 test patch sites that
    migrated cleanly via string-rename without ``new_callable=AsyncMock``).
  - #37: cryptographic verifier verdict ordering (NO_TRUSTED_KEY
    before NO_SIGNATURE before MALFORMED before BAD) — covers the
    P4 5-way verdict invariant where pubkey-None must short-circuit
    before any ``pubkey.verify(...)`` call.

### Mission

QA closure pass; mission archived at v0.31.0-rc.1 per the
SHIPPED-and-archived flow. v0.31.0-rc.2 is a strict superset; the
rc.1 archive footer remains accurate (per-phase commit table +
operator validation gate steps still apply).

## [0.31.0-rc.1] — 2026-05-06

Release candidate of the **multi-mind FINAL GA** + **calibration extreme-audit closure**.
Operators must validate on canonical Sony VAIO + at least one Windows host
before promotion to 0.31.0 final per the staged-adoption discipline.

### Highlights

- **Voice calibration is now self-applying.** SET dispatch via a
  registered handler table replaces the v0.30.x advise-only loop.
  Operators on canonical Sony VAIO go probe → diag → triage → engine
  → applier mutates state directly → DONE in 1 click; no follow-up
  ``sovyx doctor voice --fix`` needed.
- **Mid-stage cancel is now ≤ 10 s** (vs. up-to-10-min before). The
  bash diag subprocess is cancellable via ``asyncio.create_subprocess_exec``
  + SIGTERM/SIGKILL escalation; the dashboard's POST /cancel touches
  the durable ``.cancel`` file AND calls ``task.cancel()`` for the
  fast path.
- **Capture prompts render in real time** during the 8-12 min slow-path
  diag. Bash writes structured prompts to a side-channel JSONL the
  orchestrator tails; the dashboard renders ``<CapturePrompt>`` for
  each "say X" / "stay silent for Y" instruction.
- **Real Ed25519 signing.** The pre-P4 LENIENT/STRICT toggle was
  field-presence theater; v0.30.32 wires real ``cryptography.hazmat``
  Ed25519 verification against the shipped trust store.
- **Schema migrations.** ``_migrations`` registry with explicit
  ``(from, to)`` edges + chain walker + identity v1→v2 placeholder.
  Future schema bumps need ONE migration function + tests; loader
  unchanged.
- **Privacy contract enforced.** ``voice.calibration.*`` /
  ``voice.diagnostics.*`` events carry hashed identifiers
  (``mind_id_hash`` / ``job_id_hash`` / ``profile_id_hash`` —
  16-hex SHA256 prefix via ``sovyx.observability.privacy.short_hash``);
  zero raw filesystem paths; CI gate at
  ``tests/integration/test_telemetry_privacy_audit.py``.

### Added

- ``sovyx.observability.privacy.short_hash`` — single source of truth
  for 16-hex SHA256 identifier hashing across calibration telemetry.
- ``sovyx.voice.calibration._applier._TARGET_CLASS_HANDLERS`` — async
  handler registry; two ship: ``LinuxMixerApply`` (boost_up / reset
  intents via the proven ``apply_mixer_boost_up`` / ``apply_mixer_reset``
  paths) and ``MindConfig.voice`` (per-field setattr + ``mind.yaml``
  persist).
- ``sovyx.voice.calibration._applier._PreApplySnapshot`` — pre-apply
  state captured once before any mutation; LIFO rollback replays
  every applied decision in reverse on ``ApplyError``.
- Confidence-band gating: ``HIGH`` auto-applies, ``MEDIUM`` requires
  ``allow_medium=True`` (CLI ``--yes`` / frontend confirm), ``LOW``
  is advise-only, ``EXPERIMENTAL`` is skipped.
- New telemetry events:
  - ``voice.calibration.applier.apply_failed_with_rollback{decisions_rolled_back, rollback_duration_s}``
  - ``voice.calibration.applier.rollback_step_failed{decision_index, exception_type}``
  - ``voice.diagnostics.cancel_grace_expired{grace_period_s}``
  - ``voice.diagnostics.cancel_completed{duration_s, escalated_to_sigkill}``
  - ``voice.calibration.wizard.capture_prompt{prompt_type, phrase}``
  - ``voice.calibration.profile.signature.invalid{verdict, mode}``
  - ``voice.calibration.profile.migration_failed{from_version, to_version, step}``
- ``sovyx.voice.diagnostics.run_full_diag_async`` + ``_cancel_process_tree``
  helper — async-native bash diag runner with SIGTERM → 10s grace
  → SIGKILL escalation on cancel.
- ``sovyx.voice.diagnostics._bash.lib.common.sh::prompt_emit_structured``
  — structured prompt emission to ``$SOVYX_DIAG_PROMPTS_FILE``
  (NO-OP when env unset; CLI-direct invocations unaffected).
- ``sovyx.voice.calibration._migrations`` — schema migration registry
  with ``MIGRATIONS: dict[tuple[int, int], MigrationFunc]`` + chain
  walker + ``CalibrationProfileMigrationError``; identity ``v1→v2``
  placeholder.
- ``sovyx.voice.calibration._persistence._verify_calibration_signature``
  — real Ed25519 verification with 5-way verdict; 3-way operator-
  facing ``signature_status`` (accepted/missing/invalid).
- ``sovyx.voice.calibration._persistence.inspect_migrated_profile_dict``
  — operator dry-run path; returns the migrated dict without
  constructing a profile or running the signature gate.
- CLI ``sovyx doctor voice --calibrate --signing-key <path>`` — sign
  the persisted profile with an Ed25519 PEM key.
- CLI ``sovyx doctor voice --calibrate --evaluate-rules`` — dry-eval
  rules without diag/triage/apply (~5 s vs. ~10 min full run).
- Frontend ``<CapturePrompt>`` invocation, lazy ``preview-fingerprint``
  fetch, auto-rollback amber banner on FAILED-after-rollback.
- ``docs/security.md`` — Calibration telemetry retention section.
- CI ``Voice Bash Diag Smoke`` gate — runs the actual bundled
  ``sovyx doctor voice --full-diag --surgical`` end-to-end on
  sovyx-4core; ~30 s gate.
- 16 new signing tests, 14 migration registry tests, 3 Hypothesis
  property tests for migration idempotency, 7 capture-prompt
  protocol tests, 5 cancellation tests, 28 applier tests for the
  registry/snapshot/LIFO/confidence-band, 9 telemetry privacy
  scenarios.

### Changed

- **R10 promoted from ADVISE to SET** targeting ``LinuxMixerApply``
  (``rule_version`` 1 → 2; ``RULE_SET_VERSION`` 10 → 11). Operators
  on canonical Sony VAIO no longer need the manual ``--fix``.
- ``CalibrationApplier.apply`` is now ``async``. Sync callers (CLI)
  wrap with ``asyncio.run``; the wizard orchestrator awaits directly.
- ``run_full_diag`` is now a thin sync wrapper around
  ``run_full_diag_async``; CLI callers unchanged.
- ``WizardJobSnapshot.extras`` is now an open-ended bag with typed
  slots ``current_prompt`` (P3) + ``rolled_back`` (P6); zod schema
  uses ``passthrough()`` for forward-compat.
- ``VoiceStep`` renders a single setup flow at a time (no more
  parallel ``HardwareDetection`` + ``VoiceCalibrationStep``). Flag
  ON → calibration; FALLBACK terminal flips to legacy.
- ``RecalibrateButton`` always renders; ``disabled={!flagEnabled}``
  with a tooltip pointing at the toggle when the flag is off.
- ``_IdleView`` no longer auto-fetches the hardware preview on mount;
  operators click "Show detected hardware" when they want it.

### Removed

- Pre-P0 raw operator-set strings in ``voice.calibration.*`` /
  ``voice.diagnostics.*`` telemetry. The deprecated aliases
  (``mind_id`` / ``job_id`` / ``cached_mind_id`` / ``path``)
  shipped briefly in v0.30.28 for one minor cycle and were dropped
  in v0.30.29.
- The "is signature field present?" theater check in
  ``_persistence.py``.

### Fixed

- The bash diag's interactive prompts are no longer invisible during
  dashboard-initiated calibration runs (orphan ``<CapturePrompt>``
  component shipped in v0.30.25 finally has a data source).
- ``ApplyError`` now carries a ``rolled_back: bool`` attribute set by
  the LIFO rollback path before re-raising; downstream catchers
  (orchestrator + dashboard banner) surface "auto-rollback fired"
  without parsing message strings.

### Deferred to v0.31.0 GA

- STRICT signing default flip — v0.31.0-rc.1 keeps LENIENT default
  per ``feedback_staged_adoption``; one minor cycle of telemetry-
  validated lenient operation precedes the flip.
- Settings → Voice 4-card → 1-accordion consolidation (D8 from the
  audit) — UI-only refactor; bundled with the GA tag's polish pass.
- ``FAST_PATH_VALIDATE`` real implementation — invasive (new bash
  ``--only`` invocation + new state machine branch). The enum stays
  in the closed set; deferral documented in ``_wizard_state.py``.

### Mission

Closes ``MISSION-voice-calibration-extreme-audit-2026-05-06.md``
(7 phases v0.30.28..v0.31.0-rc.1; 25 audit gaps closed across
telemetry hashing, SET dispatch + auto-rollback, mid-stage cancel,
capture prompts, real signing, schema migration, UX consolidation).
Predecessor: ``MISSION-voice-self-calibrating-system-2026-05-05.md``
(v0.30.14..v0.30.27 SHIPPED — infrastructure).

## [0.30.7] — 2026-05-04

### Hot fix — CI failure on v0.30.6 (test asserting obsolete message)

The `test_no_pretrained_pool_raises` regression test in
`tests/unit/voice/test_wake_word_runtime_wireup_t1.py` asserted two
strings against the NONE-strategy refuse-to-start error message:
* `"STT-fallback"` (literal hyphenated form).
* `"v0.28.3"` (citation of the deferral mission target version).

Mission `MISSION-wake-word-stt-fallback-2026-05-04` (shipped at
v0.30.6) reworded the message to cite the env var
`SOVYX_TUNING__VOICE__STT_FALLBACK_FOR_NONE_STRATEGY` as the
operator's discoverable opt-in path. The "v0.28.3 deferred" citation
became OBSOLETE after v0.30.6 — the gap was filled, not deferred.

Failed on all 4 CI runners (windows-latest, macos-latest, sovyx-4core
× 3.11/3.12). The local pre-tag sweep falsely reported exit-0 because
comtypes' Windows shutdown noise truncated the pytest summary line in
the captured output.

### Fix

* `tests/unit/voice/test_wake_word_runtime_wireup_t1.py` — assertion
  updated to accept either spelling (`"STT fallback"` or
  `"STT-fallback"`) and to require the actionable env var name
  `STT_FALLBACK_FOR_NONE_STRATEGY` instead of the obsolete `v0.28.3`
  reference. Class docstring updated to reflect post-v0.30.6 semantics
  (refuse-to-start is now flag-OFF behaviour, not unimplemented
  behaviour).
* No production code touched — the message in
  `_wake_word_wire_up.py` is correct as shipped in v0.30.6; the test
  was the stale piece.

### Process correction

CLAUDE.md "Local suite before push" was followed in form but not in
substance: the captured output exit code was trusted without grepping
the summary line. v0.30.7+ runs always grep `"passed|failed"` to
verify the summary, not just the exit code. This patch's pre-tag
verification ran `pytest -q ... | grep -E "passed|failed"` and saw
**13844 passed, 26 skipped, 0 failed** explicitly.

### Quality gates (full sweep at HEAD)

* uv lock --check ✅
* ruff + format ✅
* mypy strict ✅
* bandit ✅
* pytest ✅ **13844 passed, 0 failed** (verified summary)
* tsc ✅
* vitest ✅ 1172/1172

## [0.30.6] — 2026-05-04

### STT-fallback wake-word path — opt-in wire-up (Phase 8 / T8.17 closure)

Closes the queued gap from `MISSION-claude-autonomous-batch-2026-05-03`
§D7 deferral via dedicated research-first mission
`MISSION-wake-word-stt-fallback-2026-05-04.md`. Operators with
NONE-strategy minds (wake word doesn't resolve to a pretrained ONNX
after EXACT + PHONETIC) can now opt into STT-based fallback
detection — no daemon restart needed when the operator later trains a
model (T8.18 hot-swap unchanged).

The foundational class (`STTWakeWordDetector`) and router method
(`register_mind_stt_fallback`) were already shipped in v0.28.x; the
gap was the factory-side wire-up that previously raised `VoiceError`
on NONE strategy. R0 research re-validated the gap as narrow + the 3
v0.28.2 R3 blockers reduced to "how to build the transcribe_fn" — a
construction-time concern, not a re-architecture.

### Added

* **Tuning knob** `EngineConfig.tuning.voice.stt_fallback_for_none_strategy`
  (`bool`, default `False` per `feedback_staged_adoption`). Operators
  flip via `SOVYX_TUNING__VOICE__STT_FALLBACK_FOR_NONE_STRATEGY=true`.
* **Sync↔async bridge** `voice/factory/_stt_fallback_bridge.py` —
  `make_stt_fallback_transcribe_fn(*, engine, loop, lock, timeout_s)`
  builds the sync `Callable[[NDArray], str]` expected by
  `STTWakeWordDetector`. Uses `asyncio.run_coroutine_threadsafe` (R2
  conclusion). Failure isolation: timeout / loop-closed / engine-error
  → empty string → no-match per detector contract.
* **Factory wire-up** `voice/factory/_wake_word_wire_up.py` accepts
  optional `stt_engine`, `event_loop`, `stt_fallback_enabled`. On
  NONE-strategy + flag-on + engine + loop → registers an
  `STTWakeWordDetector` instead of raising. Shared `asyncio.Lock`
  across all NONE minds (R1 defense-in-depth against undocumented
  `moonshine_voice` C library concurrency).

### Operator action — flip procedure

1. Set `SOVYX_TUNING__VOICE__STT_FALLBACK_FOR_NONE_STRATEGY=true` in
   the daemon environment (or `system.yaml`).
2. Restart Sovyx (the flag is read at factory wire-up only).
3. Mind cards in the dashboard's voice page that previously showed
   "Configuration error" pill now flip to "Registered" pill.
4. Detection latency for NONE-strategy minds increases from infinite
   (no detection) to ~500 ms (vs ~80 ms ONNX). Telemetry counter
   `sovyx.voice.wake_word.detection_method{method=stt_fallback}`
   tracks fired detections.
5. After training a model via `sovyx voice train-wake-word --mind X`,
   the dashboard auto-hot-swaps to ONNX (T8.18); no restart needed.

### Decisions ratified under operator delegation

* **D1 (mission §R3)**: GO — gap is concrete, foundation in
  production, operator explicitly requested.
* **D2 (R2 bridge primitive)**: `asyncio.run_coroutine_threadsafe` —
  `asyncio.run` REJECTED (broken in running-loop + creates fresh loop
  per call); dedicated worker thread / janus.Queue REJECTED
  (over-engineered for ~0.5 calls/s/mind).
* **D3 (concurrency)**: shared `asyncio.Lock` defensively serialises
  the engine across multiple NONE-strategy minds.
* **D4 (staged adoption)**: default OFF; operators flip post-deploy
  after observing telemetry. Default-flip deferred to a future minor
  version once production data validates the latency + match-rate
  envelope.
* **D5 (timeout)**: 5 s default per-call. Calibrated against
  Moonshine tiny ≈ 240 ms / small ≈ 530 ms / medium ≈ 800 ms.

### Tests

11 new pytest cases (all passing):
* `tests/unit/voice/factory/test_stt_fallback_bridge.py` (6) — happy
  path text passthrough, engine raises → "", timeout → "" + cancel,
  loop closed → "", lock serialises 3 concurrent calls, reentrant.
* `tests/unit/voice/factory/test_wake_word_stt_fallback_wireup.py`
  (5) — flag OFF preserves raise contract, flag ON + engine None
  raises (defense), flag ON + engine + loop None raises (defense),
  flag ON + all present registers STT detector, flag ON + multiple
  NONE minds → all registered.

### Quality gates (full sweep at HEAD)

* uv lock --check ✅
* ruff + format ✅ (1015/1015)
* mypy strict ✅ (0 issues, 477 source files — +2 new modules)
* bandit ✅ (0 issues)
* pytest ✅ (exit 0, +11 cases vs v0.30.5)
* tsc ✅
* vitest ✅ (1172/1172 unchanged — backend-only patch)

### Out of scope (intentional)

* Default-flip is deferred — operator opt-in is the staged-adoption-
  correct posture for a new audio-thread-blocking path.
* Per-mind STT engine (vs the shared one + lock): premature; lock
  overhead is < 0.1 ms.
* STT fallback for `sovyx[voice]`-not-installed environments —
  factory engine is `None` there, fallback path correctly degrades to
  the legacy raise + factory-level try/except.

## [0.30.5] — 2026-05-04

### Onboarding components — full i18n migration

Closes the queued gap from `MISSION-claude-autonomous-batch-2026-05-03`
v0.30.3 §T3.0 inventory. Every onboarding-flow string is now driven
by `useTranslation()`. Operators in pt-BR or es see their
configuration wizard from the very first screen — no more "switched
language but onboarding is still in English" UX trap.

### Added

* New `onboarding` i18n namespace registered in `lib/i18n.ts` across
  en + pt-BR + es. ~120 keys covering page chrome, voice setup,
  provider selection, personality presets, channel connection, and
  first-chat demo. Translation-completeness gate (T3.5) extended to
  44 cases (40 + 4 onboarding).
* New `i18n-key-usage` namespace registration so VAL-24 static
  scanner recognizes onboarding t() calls.

### Changed

* `pages/onboarding.tsx` — page chrome (step counter, "I'll
  configure manually" skip-all link).
* `components/onboarding/ProviderStep.tsx` — title, subtitle,
  detected/local/cloud badges, model line, Ollama not-running
  guidance, API-key labels, test/configure buttons.
* `components/onboarding/PersonalityStep.tsx` — title with name,
  4 preset cards (warm / direct / playful / professional) with
  translated names + descriptions, companion name + language +
  user name labels, skip + continue buttons. PRESETS const replaced
  by id-driven lookup.
* `components/onboarding/ChannelsStep.tsx` — Telegram + Signal
  cards, BotFather instructions split into lead + tail keys for
  inline link insertion, connection state suffixes (active /
  deferred), token field labels, skip + continue buttons.
* `components/onboarding/VoiceStep.tsx` — title, subtitle, success
  banner, missing-deps install card (title + hint + copy aria),
  4 error message keys (429 rate limit / no audio / generic
  fallbacks), skip + enable + continue buttons. Wizard opt-in
  strings stay in `voice` namespace (shared with voice.tsx);
  consumed via `tVoice`.
* `components/onboarding/FirstChatStep.tsx` — title, subtitle with
  provider+model interpolation, thinking indicator, input
  placeholder, explore/skip buttons, error reply fallback.

### Decisions ratified under operator delegation

* **D1**: dedicated `onboarding` namespace, not split across
  per-step files. Single domain, single import.
* **D2**: companion welcome strings (FirstChatStep) stay as code
  data, NOT i18n keys. They reflect the COMPANION's chosen
  language (operator-picked), independent of dashboard locale.
* **D3**: PersonalityStep language dropdown shows NATIVE names
  ("English", "Português", "Español", "Français", "Deutsch", "日本語",
  "한국어", "中文", "Русский") regardless of dashboard locale — same
  rationale as `LanguageSelector` D5.
* **D4**: Personality preset names + descriptions ARE translated —
  operator reads them in dashboard locale to decide companion
  behavior.
* **D5**: Backend error messages surfaced verbatim; only fallback
  paths get i18n keys.

### Out of scope (intentional)

* `OnboardingState` API field names (`provider_configured`,
  `default_model`, etc.) — these are network protocol, not UI.
* Personality preset IDs (`warm`, `direct`, etc.) — wire-format
  identifiers between UI and backend.
* Welcome message strings keyed by mind-language code — they ARE
  multi-language but conceptually data, not UI translation.

### Quality gates (full sweep at HEAD)

* uv lock --check ✅
* ruff + format ✅ (1015/1015)
* mypy strict ✅ (0 issues, 475 files)
* bandit ✅ (0 issues)
* pytest ✅ (exit 0)
* tsc ✅
* vitest ✅ 1172/1172 (translation-completeness 44/44 + key-usage
  3/3 + 4 existing onboarding component tests still green
  unchanged — i18n migration validated against the same regex
  selectors)

## [0.30.4] — 2026-05-04

### Self-correction patch — migrate T1.1 hardcoded English to i18n

Closes a self-inflicted gap from v0.30.1 §T1.1. The opt-in wizard
affordance shipped 4 hardcoded English strings inside `VoiceStep`
on the first day of the autonomous batch — knowing T3 i18n landed
3 days later. Per `feedback_enterprise_only` ("fixes paliativos /
band-aid são proibidos"), shipping a self-introduced band-aid AND
declaring the mission "done" violates the contract. This patch
closes that gap before declaring v0.30.x stable.

### Changed

* `VoiceStep.tsx` now uses `useTranslation("voice")` for the wizard
  opt-in copy.
* `VoiceStep.test.tsx` imports `@/lib/i18n` so `t()` resolves in test
  context (the file uses raw testing-library; the test-utils wrapper
  bootstraps i18n elsewhere).

### Added i18n keys (en + pt-BR + es)

* `wizard.openHintOptional` — "Walk through 4 steps to pick + test
  your microphone (optional)." (and translations).
* `wizard.testedProceedHint` — "Microphone tested — proceed to
  enable voice." (and translations).
* `wizard.reopenButton` — "Re-open Setup Wizard" (and translations).
* `wizard.openButton` reused for the initial "Open Setup Wizard"
  label.

### Out of scope (still queued)

* **Onboarding components i18n migration (full)** — `ProviderStep`,
  `PersonalityStep`, `ChannelsStep`, `FirstChatStep`, plus the
  pre-existing hardcoded strings in `VoiceStep` (`Failed to enable
  voice…`, `Enable Voice`, etc.) remain. This patch ONLY closes
  strings I introduced during the autonomous batch. Full migration
  is its own queued mission tied to the next onboarding refresh.
* **STT-fallback NONE strategy** — DEFERRED per D7 ratification;
  separate research-first mission queued.

### Quality gates

* ruff / mypy / bandit / pytest / tsc / vitest all green at HEAD.
* Translation-completeness gate (40 cases) confirms parity across
  en + pt-BR + es for all 3 new keys.

## [0.30.3] — 2026-05-04

### Multi-locale i18n (pt-BR + es) + Settings switcher + auto-detect

Phase 3 of `MISSION-claude-autonomous-batch-2026-05-03` (gitignored).
Adds Brazilian Portuguese and Spanish dashboard locales, the
operator-facing language picker in Settings, and a first-visit
auto-detect with one-click toast undo.

### Added

* **T3.1 + T3.2 — Full pt-BR + es translations** (`bf42a39`). 20 new
  JSON files (10 namespaces × 2 locales) covering every key in the
  English source. Technical-term fidelity glossary anchored: wake
  word → palavra de ativação / palabra de activación; STT/TTS/VAD/
  ONNX/WASAPI/Wyoming/Kokoro preserved as technical acronyms; Mind
  → Mente; brain → cérebro/cerebro; embedding kept English. GDPR
  and LGPD article references preserved verbatim.
* **T3.3 — `LanguageSelector` in Settings + i18n registration**
  (`76dbfd8`). 3-option dropdown ("English" / "Português (Brasil)"
  / "Español") under "Display & Language" section. Native option
  labels kept untranslated by design — operators who broke their UX
  by selecting an unfamiliar language can still find the way back.
  Persists choice to `localStorage["sovyx_locale"]`.
* **T3.4 — First-visit auto-detect + `LocaleAutoDetectToast`**
  (next commit). Silent BCP 47 prefix-matching detection in
  `lib/i18n-detect.ts` (pt-PT → pt-BR, es-MX → es) with toast undo
  ("Use English") when detection picks anything other than en.
  StrictMode-safe via consume-once accessor. Toast auto-dismisses
  after 5 s.
* **T3.5 — Translation-completeness CI gate** (`356cbe2`).
  Recursive key-parity assertion across all 10 namespaces × 3
  locales (40 vitest cases). Adding a new EN key without translating
  fails CI with a concrete diff: "Missing pt-BR keys in voice:
  mind.forget.newKey". Bidirectional — also catches stale keys
  carried over from removed EN entries.

### Operator decisions ratified (D5 / D6)

* **D5: locale switcher in Settings, NOT navbar.** Operators change
  language ~once. Navbar is for primary actions; burying infrequent
  settings matches Apple/GitHub/Slack conventions.
* **D6: auto-detect with toast undo, NOT opt-in prompt.** Modal
  prompts annoy operators who already know their language; silent
  switches surprise those who don't want browser language used. Toast
  + undo threads the needle: acknowledges + escapes + non-blocking.

### Out of scope

* Onboarding components (`VoiceStep`, `ProviderStep`, `PersonalityStep`,
  `ChannelsStep`, `FirstChatStep`) still carry hardcoded English.
  Migration deferred to a sibling mission tied to the next
  onboarding refresh — known gap, not blocking v0.30.3 ship.
* Browser-pilot validation flagged for the next D22 batch run
  alongside Phase 1 + Phase 2 surfaces.

## [0.30.2] — 2026-05-04

### Mind-management UI pattern migration

Phase 2 of `MISSION-claude-autonomous-batch-2026-05-03` (gitignored).
Migrates the two mind-mutation endpoints (`/forget` and
`/retention/prune`) from CLI-only to dashboard surfaces using the
per-mind card pattern established by v0.29.0's wake-word UI.

### Added

* **T2.1 — `PerMindForgetCard` with typed-confirm UX** (`f096c7b`).
  Destructive right-to-erasure card. Mirrors GitHub's repo-deletion
  pattern + the backend's defense-in-depth (`routes/mind.py:173`
  requires `confirm: <mind_id>` typed verbatim). Dry-run by default
  so first click previews counts; operator un-checks + re-confirms
  to actually delete. Per-table count breakdown after success so
  operator forensically verifies what was wiped.
* **T2.2 — `PerMindRetentionCard` with preview-then-apply UX**
  (`570f5f8`). Time-based scheduled-policy prune card. No `confirm`
  field (retention removes only AGED records, not arbitrary rows).
  Two-step UX is operator-side, not backend-required: the
  `effective_horizons` map is server-computed, so operators must
  preview to know what gets pruned. After preview lands, button
  switches to "Apply prune" with warning tone. Collapsible horizons
  details surface per-surface days in the report panel.
* **T2.3 — Mount mind-management cards in voice.tsx per-mind grid**
  (`52213de`). New Section after the wake-word per-mind grid. Section
  visible only when `perMindStatus.length > 0` (reuses the wake-word
  list as the canonical "minds onboarded" signal). Each mind gets
  both cards stacked vertically.

### Slice

New `mindManagement` Zustand slice (`dashboard/src/stores/slices/mindManagement.ts`):
per-mind keyed state for `forgetReports` / `forgetPending` /
`forgetErrors` and `retentionReports` / `retentionPending` /
`retentionErrors`. Pessimistic updates (destructive ops MUST NOT be
optimistic). Mirrors the wakeWord slice's zod-schema-validation +
ApiError-detail-extraction contract.

### i18n

New key namespaces under `voice.json`:
* `mind.forget.*` — title, subtitle, warning banner copy, button
  labels (preview / submit / cancel / close), report panel labels.
* `mind.retention.*` — title, subtitle, button labels, horizons
  copy, cutoff label, report panel titles.

All shipped in en only — pt-BR + es land in Phase 3 (`v0.30.3`).

### Tests

14 new vitest cases:
* `PerMindForgetCard.test.tsx` (7) — collapsed default, expand-to-
  reveal, disable-until-match, button-label-flips, submit-fires-
  slice-with-typed-value, report-panel-renders, error-banner.
* `PerMindRetentionCard.test.tsx` (7) — collapsed default, expand-
  to-preview-only, preview-fires-dry-run-true, apply-replaces-
  preview, apply-fires-dry-run-false, horizons-map-renders, error-
  banner.

### Out of scope (deferred to D22 batch)

Browser-pilot validation of the new UI surfaces flagged for the
next D22 batch run alongside v0.30.0's Train UI + Wizard +
v0.30.1's onboarding integration.

## [0.30.1] — 2026-05-03

### Closure batch — onboarding wizard integration + A/B telemetry

Phase 1 of `MISSION-claude-autonomous-batch-2026-05-03` (gitignored).
Two operator-facing improvements + the regen of the operator debt
ledger reflecting v0.30.0's closures.

### Added

* **T1.1 — `VoiceSetupWizard` opt-in affordance in onboarding flow**
  (`9fbec87`). New collapsible "Open setup wizard" button mounted
  inside the existing `VoiceStep` between `HardwareDetection` and
  the success/missing-deps banners. Mirrors the existing voice.tsx
  Section pattern from v0.30.0 §T2. The wizard is OPT-IN — operators
  who want the 4-step record + diagnostic check click the button;
  operators who don't get the same enable flow as before. Zero
  regression on the production path; flagged for D22 batch
  validation.
* **T1.2 — Voice wizard A/B telemetry instruments + frontend hooks**
  (`dd1d793`). Two low-cardinality OTel instruments answer the
  post-D22-pilot question "is the wizard better than the legacy
  modal?":
  * `sovyx.voice.wizard.step.dwell.latency` (histogram, attribute
    `step` ∈ {devices, record, results, save, done}).
  * `sovyx.voice.wizard.completion.rate` (counter, attributes
    `outcome` ∈ {completed, abandoned} + `exit_step`).

  New `POST /api/voice/wizard/telemetry` (204 No Content) accepts a
  discriminated-union body with pydantic-bounded `duration_ms` ∈
  [0, 1 h]. Frontend `VoiceSetupWizard.tsx` instrumented via two
  refs + two `useEffect` hooks; emission is best-effort (network
  failures swallowed so wizard UX is never blocked). 18 new backend
  cases + 3 new frontend cases.

### Changed

* **T1.3 — Operator debt master ledger regen**. In-place update of
  `OPERATOR-DEBT-MASTER-2026-05-03.md` (gitignored) reflecting
  v0.30.0 closures: D15 (T27 Tier 1 RAW DEPRECATED), D23 (Train
  Wake Word UI shipped). Two enterprise-grade ratifications under
  operator delegation (per `RESPONSIBILITIES-MAP-2026-05-03.md`
  PART 1B): D10 → option (b) framework-only (PHONETIC + Train UI
  cover the use cases at zero GPU cost); D4 → DEFER UNTIL D14
  telemetry validates < 0.1 % mismatch rate (LENIENT remains
  correct).

### Out of scope (deferred — see mission PART 6)

* STT-fallback NONE strategy → own research-first mission per
  "logic 100 % internalized" contract; the v0.28.2 R3 blockers
  still hold and require deep design clarity.
* 4 Phase 3 default-flag flips → operator-blocked on D1/D2/D3
  telemetry pilots.
* v0.31.0 final GA tag → 6-8 weeks gated on D22 batch validation +
  D2 + D1 + 30-day soak.

## [0.30.0] — 2026-05-03

### Single-mind production GA + Train Wake Word UI

This minor release closes the last code-side gaps between v0.29.1
and the master mission's "single-mind production GA" criterion
(``MISSION-voice-final-skype-grade-2026.md`` §Two-Tier GA Strategy
352-359). Five tracks (12 fix commits + 1 closure = 13 commits)
covering the full v0.30.0 mission scope:

* **T1 — Train Wake Word UI (D23)** — closes the operator workflow
  gap surfaced in the v0.29.0 review. Operators on the dashboard
  can now train a wake-word ONNX model end-to-end without dropping
  to the CLI: click "Train this wake word" on a NONE-strategy mind
  card → modal with adjustable target_samples / voices / variants /
  negatives_dir → submit fires HTTP 202 spawn → live progress via
  WebSocket → cancel mid-flight or Use-this-model on success.
* **T2 — Phase 7 Wizard React Frontend (T7.25-T7.30)** — closes the
  Phase 7 GA gate. 5-step microphone setup wizard (devices → record
  → results → save → done) accessible via voice.tsx as a
  collapsible Section.
* **T3 — T27 Tier 1 RAW deprecation (D5)** — formalizes the
  architectural deferral with explicit DEPRECATED-PENDING-PHASE-3-
  TELEMETRY status + re-activation triggers. Code + ADR + ROADMAP
  in lockstep.
* **T4 — Cleanup folds (D6)** — drops the unnecessary
  ``asyncio.to_thread`` wrapper on ``_create_wake_word_stub`` (the
  stub does no async work) + consolidates duplicate
  ``VoiceTuningConfig()`` env-reads in the factory. ~5-7 ms boot
  savings per pipeline construction.

Mission spec: ``docs-internal/missions/MISSION-v0.30.0-single-mind-ga-2026-05-03.md``
(gitignored).

### Added

* **T1.1 — `POST /api/voice/training/jobs/start` endpoint**
  (`e73de80`). HTTP 202 Accepted with ``{job_id, stream_url}``;
  spawns the orchestrator via ``observability.tasks.spawn`` (same
  primitive used by ``brain/consolidation.py::consolidation-scheduler``).
  Idempotency via slugified ``job_id`` (re-submit while in flight
  returns 409 Conflict). Fail-fast on missing trainer backend (503
  with operator remediation in detail). 11 new endpoint tests.
* **T1.2 — `WS /api/voice/training/jobs/{id}/stream`** (`9179b5b`).
  Live progress streaming via WebSocket — auth via query-param
  token (logs.py pattern). Tail-based JSONL push at 0.5 s with
  discriminated-union messages (``snapshot`` / ``terminal`` /
  ``error``). Frontend race-tolerance: connecting right after
  POST 202 supported. 10 new WS tests.
* **T1.3 — Frontend training types + zod + Zustand slice**
  (`6ad58e6`). New ``WakeWordPerMindStatus``-style additions:
  ``StartTrainingRequest`` + ``StartTrainingResponse`` +
  ``TrainingJobStreamMessage`` (discriminated union). Slice with
  ``startTraining`` (returns job_id on 202) + ``cancelTrainingJob``
  + ``subscribeToTrainingJob`` (manages WS lifecycle) +
  ``unsubscribeFromTrainingJob`` (idempotent close). 24 new vitest
  cases (12 schema + 12 slice).
* **T1.4 — TrainWakeWordButton + Modal in PerMindWakeWordCard**
  (`0355eec`). Conditional rendering — button visible ONLY when
  ``resolution_strategy === "none"`` AND ``wake_word_enabled === true``.
  Modal pre-fills wake_word/mind_id/language from the entry; operator
  adjusts target_samples (slider 100-10000) + voices/variants CSV +
  required negatives_dir. Optimistic submit; on 202 modal closes +
  page subscribes to live stream. 2 new render-conditional tests.
* **T1.5 — TrainingJobsPanel for live progress + cancel**
  (`6c56573`). Pure observer of slice state. UI states: in-flight
  (progress bar + samples counter + Cancel button), terminal
  (pill + output_path + Use-this-model OR error_summary disclosure
  + Dismiss button). a11y: ``role="progressbar"`` with valuenow/min/max.
  i18n: 15 new strings under ``training.panel.*``.
* **T1.6 — Component vitest tests** (`67a9bac`). 15 new cases
  pinning the modal + panel contracts (render conditions, prefill,
  close behavior, error display, terminal-state UIs, cancel flow,
  Dismiss action clears slice state).
* **T2 — VoiceSetupWizard 5-step component** (`ad2cdd7`). React
  frontend for the Phase 7 backend (T7.21-T7.24 already shipped).
  ``useReducer`` state machine with discriminated-union steps.
  Mounted in voice.tsx as a collapsible Section. 6 new vitest
  cases covering devices fetch + record + results + retry + save +
  done end-to-end.

### Changed

* **T3 — Tier 1 RAW marked DEPRECATED-PENDING-PHASE-3-TELEMETRY**
  (`fedabbc`). Per anti-pattern #21, Tier 3 (``voice_clarity_autofix
  =True``) is THE durable fix; Tier 1 RAW is performance optimization
  only. v0.30.0 ships with the placeholder strategy + flag intact;
  re-activation triggers documented in code + ADR + ROADMAP A5
  (telemetry showing Tier 3 covers <99%, engagement_denied rate
  ≥ 5%, or explicit operator ask). ABANDON trigger documented for
  the future archive-as-superseded path.
* **T4 — Factory boot perf cleanup** (`bdd360a`). ``_create_wake_word_stub``
  no longer goes through ``asyncio.to_thread`` (pure-Python class
  instantiation; the thread-spawn was theatrical). ``VoiceTuningConfig()``
  consolidated to one read in ``create_voice_pipeline`` instead of
  two (the wake-word router block + the device-resolution block now
  share one frozen instance). Saves ~5-7 ms per pipeline boot.

### Validation

* All quality gates green: ruff lint + format, mypy strict (475
  source files), bandit zero issues, pytest (6,617 voice + dashboard
  tests pass with zero regressions; +21 net from T1.1+T1.2),
  ``npx tsc -b`` zero new errors, ``npx vitest run`` 1,092 tests
  pass (was 1,045 pre-v0.30.0; +47 net from T1.3+T1.4+T1.5+T1.6+T2).

### Operator-only follow-ups (T6 / D22)

The mission's operator-pendency map (PART 6 of the mission spec)
identifies ~14h of operator-only work still owed for the v0.31.0
multi-mind FINAL GA cycle. Top priority post-v0.30.0:

1. **D22 browser pilot** (~30-45 min) — validate v0.29.0 wake-word
   UI + v0.29.1 matched_name disclosure + v0.30.0 Train UI + Setup
   Wizard end-to-end in a browser.
2. **D2 / D3 / D1 telemetry pilots** (~3h total) — Phase 4 AEC ERLE
   + DNSMOS extras + Phase 3 telemetry inspection. Unblocks the
   default-flip cycle.
3. **D5 voice 100pct pilots** (~2-3h) — B7/C5/E3 harness runs.
4. **D10 — pretrained pool decision** (~1h decision + GPU-hours per
   chosen path) — sole remaining v0.31.0 multi-mind FINAL GA blocker.

## [0.29.1] — 2026-05-03

### Tightening pass

Operator's 2026-05-03 enterprise-grade review of the v0.29.0 ship
surfaced **2 HIGH-priority items** + **1 documentation hygiene item**.
None are critical / regressions; all are residual gaps from v0.29.0
that should be tightened before expanding scope into a v0.30.0 with
substantive new features. Mission spec at
``docs-internal/missions/MISSION-v0.29.1-tightening-2026-05-03.md``
(gitignored). 2 fix commits + 1 closure = 3 git commits total
(T3 is a docs regen in gitignored `docs-internal/`).

### Added

- **T1 — `matched_name` + `phoneme_distance` surface for PHONETIC
  strategy** (`c5f868b`). The v0.29.0 per-mind status endpoint
  returned ``model_path`` + ``resolution_strategy`` only. The
  resolver knew more: for PHONETIC matches, ``matched_name`` carries
  the actual matched-file name (e.g., ``"lucia"`` for a
  ``wake_word: "Lúcia"``) + ``phoneme_distance`` carries the
  Levenshtein-on-phonemes value. Pre-v0.29.1, both signals were
  log-only at ``voice/_wake_word_resolver.py:216-228``; the dashboard
  reading them from logs would have required log-grep. T1 surfaces
  both fields end-to-end (backend dataclass + pydantic + TypeScript
  + zod + i18n + UI), with the frontend rendering the disclosure
  ONLY when ``resolution_strategy === "phonetic"`` (EXACT case is
  redundant with the file name; EXACT renders no extra line). The
  dashboard now shows: "Matched as `lucia.onnx` (distance: 0)" for
  diacritic / phonetic matches. Drift-prevention motivation: an
  operator who edits ``wake_word`` later sees the matched-file +
  distance and can catch unintended cross-matches before they ship.
  Resolver's ``-1`` sentinel for ``phoneme_distance`` is converted
  to ``None`` at the dataclass boundary so the wire format only
  carries non-negative ``int | None`` (the zod
  ``nonnegative().nullable()`` schema rejects negatives explicitly).
  4 new vitest cases + 4 new Python test cases.

### Fixed

- **T2 — Bare-assertion sweep + line 371 fix** (`56feccf`). Companion
  to the line-236 fix in `fbbfae2` (v0.29.0 closure). Re-grep at
  HEAD found one remaining bare ``expect(...).toHaveBeenCalledTimes(2)``
  outside ``waitFor`` at
  ``voice-platform-diagnostics.test.tsx:371`` — same race as line
  236 (BypassTierStatusCard's mount-time fetch fires async AFTER
  the page DOM renders; bare assertion races the second fetch).
  Pre-existing pattern; not yet flaking but inevitable under
  contention. Sweep audited every other dashboard
  ``toHaveBeenCalledTimes(N>=2)`` and confirmed all siblings are
  already correctly wrapped (brain.test.tsx:127/175/200,
  use-voice-catalog.test.ts:139, plugins.test.ts:280,
  api.test.ts:191/208/236). Per ``feedback_enterprise_only``,
  closing the anti-pattern instances completely beats fix-and-forget.

### Documentation

- **T3 — Operator debt master regeneration** (gitignored;
  ``docs-internal/OPERATOR-DEBT-MASTER-2026-05-03.md``). The
  predecessor file was dated v0.28.0 (HEAD `7ce681f`); the repo
  shipped v0.28.1, v0.28.2, v0.28.3, v0.29.0 since. T3 ships a
  current-truth status overlay: per-debt status pills updated
  (D1-D8, D10-D16, D20-D21 STILL OPEN; D19 PARTIAL — 5 of ~7 tags
  shipped; D22 PARTIAL — wake-word UI shipped + browser-validation
  owed); new D23 added (Train Wake Word dashboard button, deferred
  to v0.30.0 mission); cross-references to v0.28.1+ commit SHAs
  cite-anchored throughout. Predecessor file retained as the
  authoritative DEEP SPEC for D1-D22 operator-action recipes
  (PART 2-4 unchanged); supersede pointers added to both files for
  audit trail.

### Validation

- All quality gates green: ruff lint + format, mypy strict (475
  source files), bandit zero issues, pytest (6,596 voice + dashboard
  tests pass), ``npx tsc -b`` zero new errors,
  ``npx vitest run`` 1,045 tests pass (+4 net from T1; -0 from T2
  test-hardening commit).

## [0.29.0] — 2026-05-03

### Wake-word UI

Closes the v0.28.3 silent-degradation observability gap + ships the
first user-facing wake-word management UI. Mission spec at
``docs-internal/missions/MISSION-wake-word-ui-2026-05-03.md``
(gitignored). 5 fix commits + 1 closure = 6 commits total.

The v0.29.0 release ALSO establishes the per-mind dashboard mutation
pattern (Zustand slice + optimistic update + zod runtime validation
+ React component) that future missions will reuse for migrating
``/api/mind/{id}/forget`` and ``/api/mind/{id}/retention/prune`` to
the same UX shape.

### Added

- **T1 — Per-mind wake-word status endpoint** (`5d840f8`). New
  ``GET /api/voice/wake-word/status`` returns per-mind health
  snapshot via re-run resolution + cross-reference with the live
  ``WakeWordRouter``. Idempotent + stateless. Closes the v0.28.3 T2
  silent-degrade gap: an operator who persisted
  ``wake_word_enabled: true`` for a mind whose ONNX is missing now
  sees ``runtime_registered=false`` + ``last_error=<remediation>``
  in the dashboard. New ``query_per_mind_wake_word_status`` helper
  in ``voice/factory/_wake_word_wire_up.py`` + ``WakeWordPerMindStatusEntry``
  frozen+slotted dataclass + pydantic ``WakeWordPerMindStatusItem``
  + ``WakeWordPerMindStatusResponse`` wire-format models. 18 new
  Python tests (10 helper + 8 endpoint).
- **T2 — Frontend types + zod schemas** (`bfe3518`). Strict 1:1
  mirror of the backend pydantic models in ``api.ts`` +
  ``schemas.ts``. New ``WakeWordResolutionStrategy`` discriminated
  union (``z.enum()`` for runtime rejection of unknown strategies).
  9 new vitest cases in ``schemas.test.ts``.
- **T3 — Zustand wakeWord slice** (`4859948`). New
  ``slices/wakeWord.ts`` with ``perMindStatus`` + ``wakeWordLoading``
  + ``wakeWordError`` state and ``fetchPerMindStatus`` +
  ``toggleMind`` (optimistic update + 422/500 rollback) actions.
  Wired into ``DashboardState`` master type. The ``_extractToggleError``
  helper preserves the resolver's full remediation text from
  ``ApiError.body.detail`` so operators see the same diagnostic
  the backend logs would have shown. 9 new vitest cases.
- **T4 — Per-mind wake-word section + toggle UI** (`e8efc36`). New
  ``<PerMindWakeWordCard>`` sub-component in ``voice.tsx`` rendered
  inside a new ``<Section>`` block immediately after the existing
  global "Wake Word" section. Three-state status pill (registered /
  not-registered / error), toggle switch with optimistic update,
  error-details disclosure with resolver remediation text, top-
  level error banner with dismiss button, empty-state placeholder.
  Reuses existing visual primitives (no new components).
- **T5 — i18n + component vitest tests** (`7224b1b`). New
  ``perMindWakeWord`` namespace in ``en/voice.json`` (8 strings).
  ``aria-label`` on the toggle input (a11y compliance). 5 new
  vitest cases pinning the rendered states.

### Documentation correction

- The v0.28.3 release notes claimed "16 new Python tests" via the
  math ``6 + 4 + 6 = 16`` for T1+T2+T3 of that mission. The actual
  net delta was 12 tests: T1 added +2 net (3 new in
  ``TestT1PreValidateContract`` + 1 inverted-rename + 1 placeholder
  removal = +2 net), T2 added +4 net, T3 added +6 net. The math in
  the original CHANGELOG entry double-counted T1's pre-existing
  tests as "new". This is strictly release-notes accounting hygiene
  — no code regressions; all 16 tests are present and passing.

### Validation

- All quality gates green: ruff lint + format, mypy strict (475
  source files), bandit zero issues, pytest (existing 13,774+ + 18
  new Python tests = 13,792 passing), ``npx tsc -b`` zero new
  errors, ``npx vitest run`` 1,041 tests pass (+23 net from
  T2+T3+T5: 9 schemas + 9 slice + 5 component).
- Operator-facing acceptance: dashboard ``/voice`` page renders
  per-mind wake-word section with toggle + status pill + error
  disclosure; ``GET /api/voice/wake-word/status`` returns per-mind
  health; ``POST /api/mind/{id}/wake-word/toggle`` with optimistic
  rollback on 422/500.

## [0.28.3] — 2026-05-03

### Pre-wake-word-UI hardening

Operator's 2026-05-03 enterprise-grade review of the v0.28.2 ship
surfaced **1 CRITICAL regression** + **2 HIGH-priority gaps** + **1
MEDIUM tech-debt** that should be addressed BEFORE the v0.29.0
wake-word UI mission. Mission spec at
``docs-internal/missions/MISSION-pre-wake-word-ui-hardening-2026-05-03.md``
(gitignored). 4 fix commits + 1 closure = 5 commits total.

The v0.29.0 wake-word UI mission can now start with zero arquitetural
blockers — every persisted state is recoverable; every endpoint has
full type safety; every operator failure mode produces actionable
diagnostics.

### Fixed

- **T1 — Refuse-to-persist on wake-word toggle when ONNX missing**
  (`2bbe9ef`). Closes the v0.28.2 footgun where
  ``POST /api/mind/{id}/wake-word/toggle`` with ``enabled=true``
  persisted ``wake_word_enabled: true`` to ``mind.yaml`` even when
  no pretrained ONNX resolved. Next daemon boot would fire
  ``VoiceError`` from ``build_wake_word_router_for_enabled_minds``
  and brick the entire voice subsystem. Pre-validates the resolution
  BEFORE persist; returns HTTP 422 with the resolver's full
  remediation message (train via ``sovyx voice train-wake-word`` /
  drop ONNX into the pool / set false). The yaml is NOT touched.
  Disable path skips pre-validate (nothing to resolve when
  disabling). Test contract updated per D5: the v0.28.2 test that
  asserted the broken behavior was renamed + assertions inverted in
  the same commit (no silent regression). Three new sibling tests
  cover the symmetric disable case, the malformed-yaml 500 path, and
  the happy-path pin.
- **T2 — Factory boot tolerates stale wake-word config**
  (`7bb247e`). Defense-in-depth pair to T1. T1 prevents NEW bricked
  configs; T2 catches OLD bricked configs that already exist on
  disk (operators upgrading from v0.28.2.0 → v0.28.3 may have
  pre-existing ``wake_word_enabled: true`` without a model). The
  factory call site at ``voice/factory/__init__.py`` wraps the
  helper in ``try/except VoiceError``: on raise, logs the structured
  ERROR ``voice.factory.wake_word_router_init_failed`` with
  remediation text + degrades to ``wake_word_router=None`` (same
  backward-compat path operators with zero opted-in minds use).
  Catching ``VoiceError`` only (not blanket ``Exception``)
  preserves loud-failure for genuine helper bugs.

### Changed

- **T3 — Phonetic matcher auto-detect with kill-switch** (`3facd98`).
  Stop hardcoding ``phonetic_matcher=None`` in the wake-word factory
  helper. Build a per-mind :class:`PhoneticMatcher(language=mind.voice_language,
  enabled=None)` so operators on Linux/macOS with espeak-ng installed
  get the PHONETIC fallback strategy fired (``"Lúcia"`` matches
  ``lucia.onnx`` via espeak-ng phoneme similarity). Auto-detect via
  ``enabled=None`` semantics inside ``PhoneticMatcher.__init__``
  handles the espeak-ng-absent case without raising — Windows hosts
  without espeak-ng manually installed get ``is_available=False`` →
  graceful degrade to EXACT-only (bit-exact match v0.28.2 behavior).
  Per-mind matcher (not shared) because espeak-ng phonemes are
  language-specific; ``"Lúcia"`` phonemes differ in pt-BR vs en-US.
  Kill-switch via ``EngineConfig.tuning.voice.wake_word_phonetic_fallback_enabled``
  (default ``True``; reuses the existing knob — no schema addition).
  Both call sites updated symmetrically: boot-time builder AND
  dashboard hot-apply path. Asymmetry would have surfaced as
  operator-visible drift between toggle-time and boot-time
  resolution.

### Added

- **T4 — Frontend types + zod for wake-word toggle endpoint**
  (`28fe599`). The v0.28.2 backend shipped
  ``POST /api/mind/{id}/wake-word/toggle`` but the frontend had no
  TypeScript types or zod schemas. T4 closes that drift atomically:
  ``WakeWordToggleRequest`` + ``WakeWordToggleResponse`` interfaces
  in ``api.ts`` (strict 1:1 mirror of the pydantic models) + paired
  zod schemas in ``schemas.ts`` with ``.nullable()`` on
  ``hot_apply_detail`` matching the pydantic ``str | None``
  default-None. 8 new vitest cases pin the contract.

### Validation

- All quality gates green: ruff lint + format, mypy strict
  (475 source files), bandit zero issues, pytest (existing tests +
  3 new test files: 6 + 4 + 6 = 16 new Python tests; +8 vitest
  cases). Zero regressions in the 6,576-test sweep
  (tests/unit/voice/ + tests/dashboard/) AND the 1,018-test vitest
  sweep.

## [0.28.2] — 2026-05-03

### Wake-word runtime wire-up

Closes the critical gap identified in the 2026-05-02 review: pre-T07,
``MindConfig.wake_word_enabled=True`` had ZERO runtime effect because
the voice factory always created a no-op stub and never passed
``wake_word_router=`` to ``VoicePipeline``. v0.28.1 made the toggle
config-driven; v0.28.2 makes it load-bearing. Mission spec:
``docs-internal/missions/MISSION-wake-word-runtime-wireup-2026-05-03.md``
(gitignored). 5 implementation commits (T1, T2/T4 atomic, T3, T5, T6).

STT-fallback for the NONE-strategy path is DEFERRED to v0.28.3 per the
mission's D3 amendment — Phase 0 R3 surfaced 3 verified blockers in
the adapter contract (sync↔async mismatch, broken ``asyncio.run``
adapter pattern, race condition on shared MoonshineSTT state). Refuse-
to-start beats silent failure: an operator who flips
``wake_word_enabled=True`` for a mind with no trained model gets a
clear remediation message immediately.

### Added

- **T1 — Factory builds WakeWordRouter for enabled minds** (`64df704`).
  New helper ``voice/factory/_wake_word_wire_up.py`` enumerates
  ``<data_dir>/<mind_id>/mind.yaml`` (filesystem-as-source-of-truth
  per R1 audit; NOT ``MindManager.get_active_minds()`` which only
  sees currently-loaded minds), filters to ``wake_word_enabled=True``,
  resolves each via ``WakeWordModelResolver`` against
  ``<data_dir>/wake_word_models/pretrained/``, and registers a detector
  per mind on a fresh ``WakeWordRouter``. Backward-compat: zero opted-in
  minds → ``router=None`` → bit-exact match v0.28.1 behaviour.
- **T2/T4 — wake_word.unregister_mind RPC handler + orchestrator
  method** (`1dd77c9`). Symmetric inverse of ``wake_word.register_mind``.
  ``WakeWordRouter.unregister_mind`` now returns ``bool`` (was ``None``)
  so callers can distinguish "actually disabled" from "already disabled".
  ``VoicePipeline.unregister_mind_wake_word`` raises ``VoiceError``
  in single-mind mode (no router); the RPC handler validates non-empty
  ``mind_id``, confirms voice subsystem is registered, then delegates.
- **T3 — Dashboard wake-word toggle endpoint** (`2bffb13`). New
  ``POST /api/mind/{mind_id}/wake-word/toggle`` mounted on the existing
  ``/api/mind`` router (alongside ``/forget`` and ``/retention/prune``).
  Two-phase contract: PERSIST always runs via ``ConfigEditor.set_scalar``
  (atomic + per-path locked + comment-preserving); HOT-APPLY is
  best-effort. Cold-start (voice subsystem not registered yet),
  single-mind mode, and NONE strategy all produce
  ``applied_immediately=False`` with operator-facing diagnostic in
  ``hot_apply_detail``. Next pipeline boot picks up the persisted YAML
  via T1's filesystem-enumeration helper. Companion helper
  ``resolve_wake_word_model_for_mind`` for single-mind hot-apply
  (mirrors T1's refuse-to-start contract).
- **T5 — Per-state pipeline dwell histogram** (`000f9f2`). New
  ``sovyx.voice.pipeline.state_dwell`` OTel histogram wired inside
  ``PipelineStateMachine.record_transition`` so every state mutation
  produces one sample, attributed by the FROM state (``IDLE`` |
  ``WAKE_DETECTED`` | ``RECORDING`` | ``TRANSCRIBING`` | ``THINKING``
  | ``SPEAKING`` — bounded cardinality). Decomposes the per-turn voice
  latency budget for regression attribution. Self-loops are recorded
  too — the canonical table allows IDLE/THINKING/SPEAKING self-loops
  and dropping their samples would skew per-state percentiles.

### Validation

- All quality gates green: ruff lint + format, mypy strict (475
  source files), bandit, pytest (existing + 4 new test files: 12 + 10 +
  13 + 6 = 41 new tests). No regressions in the 6,423-test broader
  unit + cli + engine sweep.

## [0.28.1] — 2026-05-02

### Pre-wake-word-UI hardening pass

This patch release closes the 5 CRITICAL + 2 RECOMMENDED fixes
identified in the 2026-05-02 enterprise-grade review of the
codebase pre wake-word UI implementation. Mission spec at
``docs-internal/missions/MISSION-pre-wake-word-hardening-2026-05-02.md``
(gitignored). 7 commits + 1 closure = 8 commits total. **No
default flips** — defaults stay conservative; v0.28.2 will land
the AEC/NS flips after D2/D3 pilots.

### Fixed

- **T01 — `circuit_breaker_reset_seconds` config consumption**
  (`e935969`). Previously
  ``LLMProviderConfig.circuit_breaker_reset_seconds=300`` was
  defined but never consumed; LLMRouter used its own default 60 s.
  Setting the env var produced zero effect. Now ``LLMTuningConfig``
  carries ``circuit_breaker_failures: int = 3`` +
  ``circuit_breaker_reset_seconds: int = 60`` and ``bootstrap.py``
  consumes them. Industry-triangulated default of 60 s (Hystrix 5,
  LiteLLM 5, Polly 30, Resilience4j 60) — full citation chain in
  ``LLMTuningConfig`` docstring.
- **T02 — CLI flag triage** (`4253297`). Removed
  ``sovyx start --foreground`` (semantically redundant; ``start``
  already blocks in ``run_forever``) and ``sovyx init --quick`` (init
  is non-interactive; no prompts to skip). ``sovyx plugin install --yes``
  documented to clarify intentional asymmetry: skips permission
  prompt for local-dir installs; no-op for pip / git installs
  matching apt / pip / brew industry pattern.
- **T03 — `extract_signals.has_tool_use` signal**
  (`5df4c2a`). The complexity-tier router consumed the signal but
  ``extract_signals`` never set it — tool-using conversations could
  route to providers lacking native tool support. Now derived from
  a 5-message sliding window: ``has_tool_use=True`` if any recent
  message has ``role=="tool"`` or carries a non-empty ``tool_calls``
  list. Window size matches the Sovyx ReAct loop shape (3-5
  messages per cycle per ``cognitive/act.py:380-403``).
- **T07 — `MindConfig.wake_word_enabled` per-mind config**
  (`a528216`). Replaces the hardcoded ``wake_word_enabled=False``
  in ``dashboard/routes/voice.py:1793`` with per-mind config field.
  Default ``False`` preserves backward-compat (always-listening UX);
  operators opt in per mind via ``mind.yaml: wake_word_enabled: true``.
  This is the foundation commit unblocking the upcoming wake-word UI
  mission — adding a UI toggle on top of the hardcoded literal would
  have been a band-aid by definition.

### Added

- **T04 — `voice.wake_word.router.dispatch_latency` Histogram**
  (`2ccaf0f`). The master mission §T8.10 + README §11 promise of
  "≤ 50 ms multi-mind dispatch" was log-only previously. Now
  recorded as an OTel histogram with ``mind_id`` attribute alongside
  the existing log. Operators can verify the SLA contract in
  dashboards.
- **T05 — 4 plugin observability metrics** (`9ee7227`). Plugin
  observability was log-event-only before T05 (zero structured
  metrics). Added: ``sovyx.plugins.tool_executed{plugin,tool,outcome}``
  Counter, ``sovyx.plugins.tool_latency_ms{plugin,tool}`` Histogram,
  ``sovyx.plugins.sandbox_denial{plugin,layer}`` Counter (5 layers:
  ast/import/http/fs/permission), ``sovyx.plugins.auto_disabled``
  ``{plugin,reason}`` Counter. New helper module
  ``src/sovyx/plugins/_metrics.py`` with closed-set ``Literal`` types
  + defensive no-op when registry attribute is missing. Wire-up
  across 5 sandbox-layer denial sites + 2 auto-disable trigger
  sites + the manager's tool-execution emission point.
- **T06 — `sovyx.cognitive.phase_latency` Histogram**
  (`5765015`). Previously only the full-loop ``cognitive.latency``
  histogram existed. Per-phase latencies (Perceive/Attend/Think/
  Act/Reflect) were untimed. Now recorded with ``phase`` attribute
  (5 closed-set values) via new ``_measure_phase_latency`` context
  manager. Wired across both ``_execute_loop`` (sync) and
  ``_execute_loop_streaming`` paths — 10 wraps total. Records even
  when the wrapped phase raises.

### Quality posture

- 13,690+ backend tests pass; 1,009+ frontend tests pass
- ruff + ruff format + mypy strict + bandit all clean
- ``uv lock --check`` green; CI matrix Linux 3.11/3.12 + Win/macOS 3.12
- Per-commit gates verified for each of the 7 fix commits

### Roadmap impact

This release lands the FOUNDATION for the wake-word UI mission. Two
mission-blocker observability gaps closed (T04 + T06); two
config-system band-aids removed (T01 + T07); three trust-killers
fixed (T02 + T03 + T05).

## [0.28.0] — 2026-05-02

### Phase 8 — Multi-mind voice (21/22 tasks shipped; only v0.31.0 final tag remains)

- **T8.6 — `WakeWordRouter`** — N concurrent ONNX detectors per mind,
  first-hit wins, ≤ 50 ms mind context dispatch. `voice/_wake_word_router.py`.
- **T8.7-T8.10** — lazy-load contract, per-mind cooldown, false-fire
  counter `voice.wake_word.false_fire_count{mind_id}`, mind context
  switching.
- **T8.12** — phonetic similarity matching (`PhoneticMatcher` espeak-ng
  subprocess + `WakeWordModelResolver` EXACT/PHONETIC/NONE strategies +
  `voice.wake_word.resolution_strategy` counter). Commit `93ab9d6`.
- **T8.13 wake-word training pipeline foundation + operator surface** —
  `voice/wake_word_training/` package: `TrainingStatus` 6-state
  StrEnum + `TrainingJobState` frozen+slots dataclass +
  `is_legal_transition` guard + `ProgressTracker` JSONL + `TrainerBackend`
  Protocol + `KokoroSampleSynthesizer` (deterministic filenames + skip-
  existing resume + ASCII sanitisation + 24kHz→16kHz resample) +
  `TrainingOrchestrator` (state machine + `.cancel` polling +
  on_complete callback) + `sovyx voice train-wake-word` CLI (8 flags +
  Ctrl+C → exit 130) + dashboard `/api/voice/training/*` (3 endpoints)
  + frontend types + zod schemas. Commits `845e9cc`, `7e0548d`,
  `ba3a68a`, `659eb72`, `5c46e28`.
- **T8.13 ML backend deferral RESOLVED-by-design** — verified PyPI/
  GitHub research 2026-05-02 proved no viable default backend
  (OpenWakeWord 0.6.0 incompatible deps + dormant; lgpearson1771 fork
  script-only; sherpa-onnx semantic mismatch). Pluggable Protocol IS
  the design. Install hints in `NoBackendRegisteredError` carry 3
  verified operator paths (external train + drop, custom backend
  impl, STT fallback). Commit `a3f28d4`.
- **T8.15 hot-reload primitive end-to-end** —
  `VoicePipeline.register_mind_wake_word` public delegate +
  `wake_word.register_mind` daemon RPC (5-step defense-in-depth
  validation) + CLI `_attempt_hot_reload` with 4 outcome paths.
  Commits `96f8abe`, `2fff082`.
- **T8.16** — diacritic + accent variant expansion: 4-variant matrix
  `(original × ASCII-fold) × (bare × hey-prefix)` + per-language
  mishears (pt/es/fr/de). 39 tests. Commit `886a688`.
- **T8.20** — cross-mind isolation Hypothesis property tests at
  `tests/property/test_cross_mind_isolation_t820.py`. Pins
  `forall (mind_a, mind_b, action) ⇒ no leak` for ConceptRepository,
  EpisodeRepository, ConsentLedger.
- **T8.21 per-mind retention pipeline (6 sub-steps)**:
  - `ConsentLedger` per-mind audit boundary (step 1)
  - `MindForgetService` brain DB wipe (step 2)
  - Service extension to conversations + system pools (step 3)
  - `sovyx mind forget` CLI + `mind.forget` RPC (step 4)
  - `POST /api/mind/{mind_id}/forget` dashboard endpoint with
    defense-in-depth `confirm: <mind_id>` field (step 5)
  - Time-based retention: `RetentionTuningConfig` + `MindRetentionConfig`
    + `MindRetentionService.prune_mind` per-pool prune (episodes/
    conversations/cascade/consolidation_log/daily_stats/consent_ledger)
    + `ConsentLedger.prune_old` time-axis primitive with
    `RETENTION_PURGE` tombstone + `sovyx mind retention prune|status`
    CLI + `mind.retention.prune` RPC + `POST /api/mind/{id}/retention/prune`
    dashboard endpoint + `RetentionScheduler` daemon auto-prune
    (default-OFF; opt-in via `MindConfig.retention.auto_prune_enabled`)
    + `ComplianceConfig.hipaa_mode` forward-compat flag (step 6)
  - Frontend types + zod schemas + docs/compliance.md +
    docs/modules/voice-privacy.md updated.

### Phase 7 — Single-mind GA close-out

- **T7.11-T7.16** — multi-language wake variants per BCP-47 locale
  (pt/es/fr/de/it/zh + Pinyin/Hanzi). Commit `d6ac23f`.
- **T7.21-T7.24** — wizard backend cluster: `GET /api/voice/wizard/devices`,
  `POST /api/voice/wizard/test-record`, `GET /api/voice/wizard/test-result/{id}`,
  `GET /api/voice/wizard/diagnostic`. Commit `ee8489f`.
- **T7.27 + T7.28** — audio error translation: `voice/_error_messages.py`
  with `translate_audio_error` + 11-class `AudioErrorClass` StrEnum +
  23 patterns across 5 platform families (Windows AUDCLNT_E_*, MMSYSERR,
  macOS Core Audio, PortAudio, POSIX errno). Commit `d935e12`.
- **T7.43, T7.45-T7.48** — final docs cluster: cross-platform parity
  matrix, 7 voice docs, 13,512-test pass evidence, security audit
  summary, 6-regime compliance self-assessment. Commit `7fea5d7`.

### Voice Windows Paranoid Mission — T35 Scenario 2

- Coordinator-level integration test at
  `tests/integration/voice/test_paranoid_mission_chain.py::TestScenario2Tier1FailsTier2Succeeds`.
  4 sub-tests pin: fall-through happy path, ring-buffer epoch
  increment-once contract (Risk #3), tap-only-on-success
  (`capture_integrity.py:586`), no-spurious-revert. Commit `131461a`.
- Mission spec audit corrected: Scenario 1 blocked on T27 (Tier 1 RAW
  COM bindings, operator-deferred per ADR); Scenario 3 blocked on
  `request_device_change_restart` wire-up (out of scope per runtime
  listener mission Phase 2); Scenario 4 ✅ shipped via cold-probe
  strict validation.

### Documentation — enterprise-grade rewrite

- **README** — 26-section enterprise-grade rewrite (1,309 LOC, 7,985
  words, 73 file:line citations, 6 mermaid diagrams). Sections cover
  every subsystem, every install path, voice training step-by-step,
  multi-mind architecture, 6-regime compliance, 31 CLI commands, 91
  dashboard endpoints, 78 OTel instruments, 34 anti-patterns. Commit
  `927c33a`.
- **6 mermaid diagrams committed** at `docs/_assets/diagrams/`:
  system architecture, cognitive loop detail, voice subsystem detail,
  multi-mind dispatch, wake-word training pipeline, compliance data
  flow.
- **Mission spec** at `docs-internal/missions/MISSION-readme-enterprise-grade-2026-05-02.md`
  (gitignored). 5-phase mission with 50 tasks. 15 internal research
  files + 5 best-practice external research files (gitignored,
  forensic-only).
- **`OPERATOR-DEBT-MASTER-2026-05-02.md`** consolidated ledger (D1..D22)
  + **`ROADMAP-POST-V0.31.0.md`** 4-tier post-Phase-8 future features
  catalogue (Tier A adjacent missions / Tier B quality / Tier C v1.0
  product expansion / Tier D commercial).
- **.md hygiene cleanup** — 4 stale files deleted (tmp/ ephemera +
  zero-friction-install plan that never shipped); 5 items archived
  per CLAUDE.md mission lifecycle (T1.4 mixin surgery plan, T1.50
  audit, F1 inventory, voice-failure-analysis, voice-mission-research)
  with archive footers naming code references and successor missions.

### Quality posture

- 13,690+ backend tests (unit, integration, dashboard, plugin,
  Hypothesis property, security, stress)
- 1,009+ frontend vitest cases + Zod runtime schema validation
- `ruff check` + `ruff format --check` clean across 994+ files
- `mypy --strict` clean across 432 source files
- `bandit -r src/sovyx/` 0 issues across all severities
- `tsc -b` clean

### Default-OFF release posture (deliberate)

This release ships with **conservative defaults**: 27 voice features
remain default-OFF pending operator pilot validation (D1-D14 in
`OPERATOR-DEBT-MASTER-2026-05-02.md`). The Phase 8 multi-mind code
is fully shipped and exercised by tests, but operator pilots
(macOS / Linux distros / BT headsets / 3+ minds concurrent / 30-day
soak) are required before flipping defaults to True. The next patch
release (v0.28.1) will land the 6 voice default flips
(`voice_aec_enabled`, `voice_noise_suppression_enabled`, etc.) when
D2 AEC ERLE pilot returns p50 ≥ 35 dB AND p95 ≥ 30 dB.

### Voice Subsystem — Phase 4 + 5 (partial) + 6 since v0.26.0

This unreleased surface accumulates 44 commits since v0.26.0 spanning
three phases of the master mission
``docs-internal/missions/MISSION-voice-final-skype-grade-2026.md``.
The voice subsystem is software-complete for v0.30.0 GA-readiness
per master mission acceptance criteria; only hardware-validation
gates (operator-scheduled) remain.

### Added

- **Phase 4 — AEC + audio quality (T4.* series, 36 commits).**
  WebRTC AEC3 wrapper (``voice/_aec.py``) + RNNoise NS wrapper
  (``voice/_noise_suppression.py``) wired into ``FrameNormalizer``
  with ERLE measurement, double-talk detection, and SNR-aware STT
  confidence factor. Per-session SNR p50/p95 in heartbeat, low-SNR
  alerts with de-flap, noise-floor drift trend alert, AGC2 VAD
  feedback gate (suppresses noise pumping), A/B perceptual-quality
  validation, dashboard quality-snapshot panel + endpoint. Tuning
  flags ``voice_aec_enabled``, ``voice_noise_suppression_enabled``,
  ``voice_use_os_dsp_when_available``.

- **Phase 5 — Cross-platform parity (T5.* series, partial, 12
  tasks).** Windows: WMI subscription for audio driver updates,
  IMMDevice → stable USB fingerprint resolver, Group Policy
  detection at boot + classified exclusive-open failures
  (BUSY/UNSUPPORTED/GP_BLOCKED). Linux: PipeWire 1.0+ version
  detection + hybrid PA conflict detection, pyudev once-per-process
  WARN + Flatpak/Snap sandbox detection, stable USB-audio
  fingerprint (vendor:product:serial), user-side mixer KB profile
  loading. Remaining Phase 5 work (T5.1-T5.30 macOS native + T5.33
  Linux mint test rigs + T5.40-T5.42 JACK/PA/Bluetooth) is
  hardware-blocked.

- **Phase 6 — Stress + chaos + soak (T6.* series, 35 commits).**
  Six new ``Diagnosis`` variants closing observability gaps:
  ``STREAM_OPEN_TIMEOUT`` (T6.2), ``EXCLUSIVE_MODE_NOT_AVAILABLE``
  (T6.3), ``INSUFFICIENT_BUFFER_SIZE`` (T6.4),
  ``INVALID_SAMPLE_RATE_NO_AUTO_CONVERT`` (T6.5),
  ``HEARTBEAT_TIMEOUT`` (T6.6) — driver delivered audio briefly then
  wedged mid-probe, ``PERMISSION_REVOKED_RUNTIME`` (T6.8) —
  permission existed at open then revoked at start. Cascade
  fallthrough mapping (T6.9). Production closures: diagnosis
  histogram telemetry, user-actionable cascade banner, watchdog
  ``last_diagnosis`` field, capture-integrity unrecoverable
  emission, INCONCLUSIVE retry, quarantine ping-pong + rapid
  re-quarantine detection, probe history default 10→100,
  ``GET /api/voice/service-health`` endpoint with closed-enum
  ``reason`` field.

- **Watchdog DEGRADED periodic re-probe (T6.13).** Background loop
  fires ``re_cascade`` every ``watchdog_degraded_reprobe_interval_s``
  seconds (default 5 min) so the pipeline self-heals from transient
  WASAPI / USB / CPU saturation root causes without waiting for
  hot-plug.

- **End-to-end pipeline test (T6.38).** Full IDLE → WAKE_DETECTED →
  RECORDING → TRANSCRIBING → THINKING → SPEAKING → IDLE drive with
  cognitive callback invoking ``pipeline.speak()`` to close the
  LLM→TTS hand-off. Pins the operator-grade contract "the pipeline
  can complete a full turn from cold IDLE to delivered TTS audio
  with no hardware dependency."

### Changed

- **Cold + warm probe diagnosis tables** gain
  ``silence_after_last_callback_ms`` parameter (T6.6) +
  ``context`` parameter for OPEN vs START (T6.8). Backwards-compat
  preserved via ``None`` / ``"open"`` defaults — pre-T6.6/T6.8
  callers see legacy behaviour.

- **``_classify_open_error``** signature gains optional ``combo``
  (T6.5 routing) and ``context`` (T6.8 routing). Cascade-executor
  + probe-open call sites use defaults; probe-start call site
  passes ``context="start"``.

- **Diagnosis enum** expanded from 17 to 23 values with the new
  Phase 6 variants. ``Diagnosis`` remains ``StrEnum`` per
  anti-pattern #9.

- **Probe submodule coverage** raised to 99% — the last 3
  uncovered statements in ``_warm.py::_analyse_vad`` were closed
  via 2D-block warmup-only tests + mis-shaped-window skip tests.

### Fixed

- **NaN/Inf in RMS computation (T6.34).** ``_compute_rms_db`` now
  guards ``mean_sq`` for finiteness; previously a buggy upstream
  layer leaking float garbage propagated NaN through ``math.sqrt``
  and ``math.log10``, returning NaN/+Inf which then misclassified
  as HEALTHY (NaN < ceiling evaluates False). Fix mirrors the
  capture-integrity ``_compute_rms_db`` finiteness guard.

### Tests

- **22 new test classes** spanning property tests, stress storms,
  chaos injection, and E2E integration. Property tests for
  ``_classify_open_error`` totality + RMS monotonicity. Stress
  storms: 20-concurrent barge-in (T6.28), 100-event hot-plug
  (T6.30), 50-cycle restart cascade (T6.27), 10K-frame load +
  queue overflow (T6.27). Chaos: random ``PortAudioError`` /
  ``StreamOpenError`` / ``BaseException`` audio-callback /
  ``NaN/Inf RMS``. Total voice test count: 5167 passed (up from
  ~5050).

### CLAUDE.md anti-patterns

- No new anti-patterns this surface; the existing AP-26 (KB profile
  signing v0.24.x lenient → v0.25.0+ strict) remains overdue for
  default flip pending operator-validated lenient telemetry.

### Promotion gates remaining (operator-validation, hardware-required)

- **T6.7** — Linux PipeWire mid-probe disconnect (HW: Linux PW rig)
- **T6.35–T6.37** — Golden audio captures (Voice Clarity APO, ALSA
  session-manager contention, WDM-KS hard-reset) — HW rigs needed
- **T5.1–T5.30** — macOS native bypass strategies + listeners (HW:
  macOS dev rig with PyObjC)
- **AP-26 default flip** — KB profile signing Mode.LENIENT →
  Mode.STRICT after one minor cycle of telemetry-validated lenient
  mode (operator decision per ``feedback_staged_adoption``)

## [0.24.0] — 2026-04-26

### Voice Windows Paranoid Mission — Foundation phase

This release lands the **foundation** layer of the Voice Windows
Paranoid Mission (mission spec
``docs-internal/missions/MISSION-voice-windows-paranoid-2026-04-26.md``).
The mission addresses the production failure mode where Microsoft
Voice Clarity APO (VocaEffectPack, shipped via Windows 11 25H2
cumulative updates) destroys the capture signal upstream of PortAudio
on USB-mic endpoints. Foundation phase ships **plumbing without
behaviour change** — every new feature flag defaults False; the
cure is operator-flippable today, default-on in v0.25.0 / v0.26.0
per the staged-adoption rollout matrix.

### Added

- **5 new tuning flags on ``VoiceTuningConfig``** for the Paranoid
  Mission's bypass / cascade / listener / cold-probe surface. All
  default ``False`` (foundation-phase plumbing). Cross-validator
  ``_enforce_paranoid_mission_dependencies`` rejects the contradictory
  configuration ``bypass_tier2_host_api_rotate_enabled=True`` +
  ``cascade_host_api_alignment_enabled=False`` at boot with a
  remediation hint. Flags:
  * ``probe_cold_strict_validation_enabled`` — Furo W-1 cold-probe
    stricter signal validation (env: ``SOVYX_TUNING__VOICE__PROBE_COLD_STRICT_VALIDATION_ENABLED``)
  * ``bypass_tier1_raw_enabled`` — Tier 1 RAW + Communications via
    ``IAudioClient3::SetClientProperties`` (env: ``…BYPASS_TIER1_RAW_ENABLED``)
  * ``bypass_tier2_host_api_rotate_enabled`` — Tier 2 host-API
    rotate-then-exclusive (env: ``…BYPASS_TIER2_HOST_API_ROTATE_ENABLED``)
  * ``mm_notification_listener_enabled`` — IMMNotificationClient
    device-change auto-recovery (env: ``…MM_NOTIFICATION_LISTENER_ENABLED``)
  * ``cascade_host_api_alignment_enabled`` — opener honours cascade
    winner's ``host_api`` + ``capture_fallback_host_apis`` bucket sort
    (env: ``…CASCADE_HOST_API_ALIGNMENT_ENABLED``)

- **Cold-probe signal validation (Furo W-1 cure).** The
  ``voice/health/probe.py::_diagnose_cold`` function now reads
  ``rms_db`` and (in strict mode) returns ``Diagnosis.NO_SIGNAL`` when
  the captured signal is silent (``rms_db < probe_rms_db_no_signal``,
  default −70 dBFS). This closes the v0.23.x silent-combo persistence
  loop that was the deterministic cause of the user's reported bug
  (Razer + Win11 25H2 + Voice Clarity, silent combo with
  ``rms=-96.43, callbacks=49`` persisting as the cascade winner across
  every boot). Strict mode emits ``voice.probe.cold_silence_rejected
  {mode=strict_reject}``; lenient mode (foundation-phase default)
  preserves v0.23.x acceptance but emits ``mode=lenient_passthrough``
  for telemetry-only calibration. ``vad_max_prob`` kwarg added for
  signature symmetry with ``_diagnose_warm`` (cold path ignores it).

- **``CaptureRestartFrame`` typed pipeline observability frame**
  (``voice/pipeline/_frame_types.py``) wrapping every capture-task
  restart that mutates the substrate (default device, host_api,
  exclusive/shared mode, or APO bypass tier). 9 payload fields
  (``restart_reason``, old/new ``host_api`` + ``device_id`` +
  ``signal_processing_mode``, ``recovery_latency_ms``,
  ``bypass_tier``) plus ``CaptureRestartReason`` ``StrEnum``
  discriminator (DEVICE_CHANGED, APO_DEGRADED, OVERFLOW, MANUAL).
  Pure observability layer — emitters land in v0.25.0 wire-up.

- **Dashboard zod schemas + TypeScript types** for
  ``CaptureRestartFrame``, ``VoiceRestartHistoryResponse``, and
  ``VoiceBypassTierStatusResponse``. All payload fields ``.optional()``
  in v0.24.0 per the master rollout matrix; promotion to required
  considered v0.26.0 after one minor cycle of in-prod observation.
  Forward-compat: ``restart_reason`` accepts the StrEnum values OR
  any fallback string so a backend-side variant addition lands
  without flooding ``safeParse`` mismatch warnings.

- **CLAUDE.md anti-patterns 28 + 29.**
  * AP-28 — Cold probe MUST validate signal energy, not just callback
    count (Furo W-1 generalisation: any acceptance gate downstream of
    a real-world signal source MUST verify the signal itself).
  * AP-29 — ``CaptureRestartFrame`` is observability, NOT a state-
    machine rewrite (extends Hybrid Option C lesson from AP-25).

- **Public docs:** ``docs/modules/voice-troubleshooting-windows.md``
  — operator-facing guide covering symptom table, every paranoid-
  mission feature flag (env var, default per phase, when to flip),
  master kill switch, doctor subcommand surface, telemetry events to
  grep for, and the rollback procedure per flag.

- **Internal ADRs (``docs-internal/``, gitignored):**
  * ``ADR-voice-bypass-tier-system.md`` — design of the 3-tier
    bypass coordinator (Tier 1 RAW / Tier 2 host_api_rotate / Tier 3
    WASAPI exclusive).
  * ``ADR-voice-cascade-runtime-alignment.md`` — design of the
    opener's 3-tier bucket sort closing Furo W-4.
  * ``ADR-voice-imm-notification-recovery.md`` — design of the
    IMMNotificationClient device-change listener with the non-
    blocking-post pattern + AST-level CI lint.

### Tests

- 9 new tests in ``TestVoiceTuningParanoidMissionFlags`` (defaults,
  env-var override per flag, cross-validator rejection + both
  successful combinations, EngineConfig nested-env path).
- 17 new tests in ``TestDiagnoseCold`` and ``TestFuroW1UserReplay``
  including 1 Hypothesis property test for the strict diagnosis-
  table invariants and a regression test for the user's exact
  silent-combo bug repro.
- 7 new tests in ``TestCaptureRestartReason`` and
  ``TestCaptureRestartFrame`` pinning the variant set + frame shape
  + serialisation round trip + frozen-mutation rejection.
- 15 new vitest tests in ``dashboard/src/types/schemas.test.ts``
  pinning the wire contract for the new dashboard schemas.

### Deferred to v0.24.1 — v0.25.0

The following mission tasks ship in dedicated commits / minor
versions, with each one getting its own focused commit + full CI
per ``feedback_staged_adoption`` (no bundling foundation +
wire-up):

* T01-T06 — God-file splits (``contract.py``, ``cascade.py``,
  ``factory.py``, ``_capture_task.py``, ``probe.py``,
  ``combo_store.py``). Recon mapping (Wave 3) saved as the
  blueprint for the v0.24.1 dedicated split mission.
* T13/T14 — Tier 1 RAW + Tier 2 host_api_rotate strategy classes
  (Windows ctypes COM bindings). Land in v0.25.0 wire-up.
* T15/T16 — ``preferred_host_api`` opener param + wire
  ``capture_fallback_host_apis`` config. Land in v0.25.0 wire-up.
* T17 — IMMNotificationClient stub with comtypes COM bindings + AST
  lint rule. Lands in v0.25.0 wire-up.
* T10/T18 — Telemetry counter vocabulary + dashboard route stubs.
  Lands in v0.25.0 wire-up.
* Default flips of ``probe_cold_strict_validation_enabled`` and
  ``cascade_host_api_alignment_enabled`` to ``True`` — v0.25.0,
  after Phase 1 production telemetry validates the cold-probe
  rejection rate stays within the predicted population.

## [Unreleased — Linux mixer L2.5]

### Added

- **L2.5 Voice Mixer Sanity — bidirectional ALSA mixer healing on
  Linux.** A new opt-in layer inside the Voice Capture Health Lifecycle
  that sits between the ComboStore fast-path and the platform cascade
  walk. Detects both saturation (pre-existing concern covered by
  `LinuxALSAMixerResetBypass`) AND attenuation (newly-uncovered failure
  class — pilot: Sony VAIO VJFE69F11X with Conexant SN6180 where
  factory defaults ship `Capture = 40/80 = -34 dB` + `Internal Mic
  Boost = 0`, putting voice at -60 dBFS vs Silero VAD's training range
  of -25 to -15 dBFS). Surface:
  * `check_and_maybe_heal(endpoint, hw, *, kb_lookup, role_resolver,
    validation_probe_fn, tuning, ...)` — 7-step state machine: probe
    → classify → detect_customization → apply → validate → (persist
    | rollback) → done. Hard 5s wall-clock budget; full LIFO rollback
    on any validation gate failure; persistence via `alsactl store`.
  * `MixerKBLookup` — YAML-loaded hardware-profile catalogue with
    weighted fnmatch scoring (codec_id hard gate; driver_family +
    system_vendor + system_product + audio_stack + kernel +
    factory_signature soft signals). Ambiguous match (top-2 tie
    within 0.05) → defer for dashboard choice card.
  * `MixerControlRoleResolver` — 3-layer discovery: per-codec
    override (seeded with Conexant SN6180) → HDA driver-family
    table → substring fallback (superset of existing boost-control
    patterns).
  * `detect_user_customization` — 7-signal heuristic (weights sum
    to 1.0): mixer-vs-factory delta, `~/.asoundrc`, PipeWire/
    WirePlumber user confs, recent `asound.state` mtime, ComboStore
    drift, `capture_overrides` pins. Three branches (tunable
    thresholds): auto-apply / defer / skip-silently. User tuning
    is sacred.
  * `detect_hardware_context` — read-only hardware identity from
    `/proc/asound/card*/codec#*` + `/sys/class/dmi/id/*` +
    `/etc/os-release` + XDG runtime sockets. No subprocess, no
    `dmidecode`, no root (invariant I7).
  * `apply_mixer_preset` — KB-driven preset applier with raw /
    fraction / dB value dispatch, HDA auto-mute enum handling, and
    LIFO rollback. dB variant raises in F1 (requires richer probe
    data; F2 extends).
  * `run_cascade(mixer_sanity=...)` — opt-in kwarg; `None` (default)
    preserves byte-for-byte the pre-L2.5 behaviour for every existing
    caller. When set AND `platform_key == "linux"`, the orchestrator
    runs between ComboStore fast-path and platform walk.
  * `check_linux_mixer_sanity` preflight now detects attenuation in
    addition to saturation. Surfaces the distinct
    `MIXER_CALIBRATION_NEEDED` regime on hardware with both low
    Capture AND zeroed Internal Mic Boost.
  * `packaging/systemd/sovyx-audio-runtime-pm.service` +
    `audio-runtime-pm-setup` POSIX sh + `packaging/udev/60-sovyx-
    audio-power.rules`. Tight sandboxing
    (`NoNewPrivileges` + empty `CapabilityBoundingSet` +
    `ProtectSystem=strict`). The daemon never writes to `/sys` at
    runtime (invariant I7). Operator escape hatch: kernel cmdline
    `sovyx.audio.no_pm_override`.
  * New `Diagnosis` values — `MIXER_ZEROED`, `MIXER_SATURATED`,
    `MIXER_UNKNOWN_PATTERN`, `MIXER_CUSTOMIZED`, `MIXER_CALIBRATION_NEEDED`.
  * New tuning knobs under `VoiceTuningConfig`:
    `linux_mixer_user_customization_threshold_apply` (0.5),
    `..._skip` (0.75), `linux_mixer_sanity_kb_match_threshold`
    (0.6), `linux_mixer_sanity_budget_s` (5.0). Override via
    `SOVYX_TUNING__VOICE__*` env.
  * See
    [`docs/modules/voice-capture-health.md`](docs/modules/voice-capture-health.md)
    § Mixer sanity (L2.5) for full architecture + KB contribution
    workflow + CLI usage.

F1 ships empty `_mixer_kb/profiles/` — production KB profiles
(pilot VAIO + 5 reference HDA codecs) land in F1.H alongside HIL
validation fixtures. F1.I ships the dashboard Mixer Health card +
`sovyx doctor voice --mixer-preset` CLI flags. F2 extends role
tables to SOF (Intel Meteor/Lunar Lake) + USB-audio + BT-HFP,
adds user-contributed KB loader, and lifts the dB-preset
limitation via multi-sample probe.

### Fixed

- **L2.5 Voice Mixer Sanity — Round-2 paranoid-audit closure.**
  Closed 23 findings across CRITICAL/HIGH/MEDIUM/LOW severity from
  the Round-2 audit. Highlights:
  * **CRITICAL #1** — `.gitattributes` pins packaging artefacts
    (systemd units, udev rule, POSIX helpers) to LF. Windows
    checkout with `core.autocrlf=true` could have silently
    corrupted shebangs + unit options for everyone who builds a
    wheel from that checkout.
  * **CRITICAL #2** — `pyproject.toml` force-includes
    `packaging/*` inside the wheel at `sovyx/_packaging/`. Prior
    state shipped wheels to PyPI carrying zero packaging assets,
    leaving pipx/pip installs non-functional for the
    systemctl-delegated alsactl path.
  * **CRITICAL #3** — telemetry preserved on `CancelledError`
    shutdown. `check_and_maybe_heal` now records the partial
    outcome BEFORE re-raising, so daemon shutdown doesn't drop
    the observability signal.
  * **CRITICAL #4** — explicit telemetry sentinel. An explicit
    `_NoopTelemetry()` injection (legitimate in tests) is no
    longer silently swapped for the module-level singleton.
  * **CRITICAL #5** — integer hard gate on factory_signature.
    Replaced fragile `sig_score == 0.0` float comparison with
    `roles_matched == 0` integer gate; immune to future
    partial-credit scoring changes.
  * **HIGH #1** — cross-cascade `asyncio.Lock` serialises every
    L2.5 invocation regardless of endpoint. Two concurrent
    cascades can't race the shared ALSA mixer state.
  * **HIGH #2** — contextvars-based reentrancy guard. Nested
    `check_and_maybe_heal` calls (e.g., validation-probe
    triggering a watchdog recascade) short-circuit with
    `ERROR/MIXER_SANITY_REENTRANT_GUARD`.
  * **HIGH #3** — half-heal write-ahead log
    (`_half_heal_recovery.py`). Mid-apply process deaths
    (SIGKILL, OOM, kernel panic) now self-heal on next boot:
    cascade detects the WAL, replays pre-apply state via
    `restore_fn`, deletes the WAL, then probes fresh.
  * **HIGH #4** — idempotent `rollback_if_needed`. Validation-
    fail → `_step_rollback` → top-level handler no longer
    double-restores.
  * **HIGH #7** — cardinality-bounded telemetry buckets. User-
    contributed profile IDs fold to `"user:<8-hex-hash>"` so
    arbitrary strings can't blow up Prometheus/OTLP labels.
  * **HIGH #8** — role-resolver coverage gap visibility. Added
    `roles_unmappable` to `FactorySignatureMatch` + WARNING log
    so KB authors can correlate silent L2.5 no-ops with resolver
    TODO.
  * Plus HIGH #5/#6/#9/#10, MEDIUM #1/#2/#3, LOW #1-#5 —
    log-volume hygiene, validation-truth preservation, shape
    invariants, strict threshold ordering, udev helper hardening,
    Unicode normalisation, LIFO rollback ordering, and more.
  See commits tagged `paranoid-QA R2 *` for full per-fix rationale
  and regression tests.

- **Timing-flake in `test_scheduler_survives_cycle_failure`**
  (`tests/unit/brain/test_consolidation.py`). Fixed
  `asyncio.sleep(0.3)` window replaced by event-based polling with
  a 5s deadline — the scheduler coroutine sometimes got starved on
  CI runners under load, causing 1 cycle instead of the expected
  ≥2. Same fix applied to the sibling
  `test_scheduler_runs_cycle` as preventive hardening.

## [0.16.13] — 2026-04-18

### Fixed

- **Voice playback no longer stalls the event loop.** `AudioOutput._play_chunk`
  wrapped the blocking `sd.play` / `sd.wait` pair in `asyncio.to_thread`, so
  the bridge / dashboard / pipeline coroutines keep ticking while a chunk
  plays (anti-pattern #14). Regression covered by a threading-ticker test.
- **`SOVYX_TUNING__*` env overrides now reach module-level constants.**
  `SafetyTuningConfig` / `BrainTuningConfig` / `VoiceTuningConfig` /
  `LLMTuningConfig` inherited `BaseModel` instead of `BaseSettings`, so the
  documented `_CONST = _TuningCls().field` pattern silently ignored env
  overrides (anti-pattern #17). 19 constants across 10 files
  (`voice/stt.py`, `voice/stt_cloud.py`, `voice/auto_select.py`,
  `voice/_capture_task.py`, `brain/learning.py`, `brain/_model_downloader.py`,
  `llm/router.py`, `cognitive/audit_store.py`, `cognitive/pii_guard.py`,
  `cognitive/safety_notifications.py`) now honour `SOVYX_TUNING__{SUBSYS}__*`.
- **`ApiError.body` exposes structured error codes.** `src/lib/api.ts` now
  parses the response body into `err.body`, so the setup wizard can branch
  on codes like `models_not_downloaded` / `pipeline_active` instead of
  regexing the message. `TtsTestButton` tests switched to real `ApiError`
  instances — the hand-crafted shape only matched in tests, silently
  broken in production.

### Added

- **Voice-model status + download flow.** New `voice.model_status` module
  is the single source of truth for "are the Piper / Kokoro ONNX files
  on disk"; dashboard routes expose status + download-trigger; setup
  wizard surfaces a "Download voice models" CTA on `HardwareDetection`
  and `TtsTestButton` error states, driven by the new
  `use-voice-models` hook.

## [0.16.12] — 2026-04-17

### Added

- **Voice device test — dashboard wiring.** The setup wizard's
  `HardwareDetection` card now consumes the device-test backend: an
  `AudioLevelMeter` (60 Hz canvas, VAD-aware, clipping indicator)
  hooked to `WS /api/voice/test/input`, plus a `TtsTestButton` that
  POSTs `/api/voice/test/output` and polls the playback job. New hook
  `use-audio-level-stream` encapsulates reconnect + token auth. Zod
  schemas for `VoiceTest*` types — backend remains the source of truth.
- **Voice device test — backend foundation** (`voice/device_test/`).
  Meter WebSocket (`GET /api/voice/test/devices`, `WS
  /api/voice/test/input`) emits per-frame RMS/peak + VAD at ~60 Hz;
  TTS playback job (`POST /api/voice/test/output`,
  `GET /api/voice/test/output/{job_id}`) returns a decoded 16-bit PCM
  WAV via the active Piper/Kokoro engine. OpenTelemetry metrics +
  unit tests for both paths. Configurable under
  `SOVYX_TUNING__VOICE__DEVICE_TEST__*`.

### Changed

- **Enterprise doc audit sweep.** Cross-checked every public doc
  against v0.16.11 source. Reconciled test counts (real: ~7,960
  backend + ~820 frontend = ~8,780) across root, CLAUDE, COVERAGE,
  FAQ, roadmap, llm-router, CONTRIBUTING. `docs/architecture.md` now
  lists all 10 LLM providers (was 4) with the correct repo layout.
  `docs/llm-router.md` + `docs/modules/llm.md` stop hardcoding model
  IDs and point to `src/sovyx/llm/pricing.py` as the single source of
  truth — IDs rotate every release. `docs/modules/dashboard.md`
  enumerates the real 21 routers under `dashboard/routes/` (old list
  named files that don't exist) and drops the stale "submodule"
  reference. `docs/modules/engine.md` + `docs/contributing.md` fix
  the data-dir path `~/.local/share/sovyx` → `~/.sovyx`.
  `docs/contributing.md` drops the dashboard-submodule flow
  (dashboard lives in the main repo). `docs/getting-started.md`
  replaces "Aria" default-name examples with explicit required
  `<name>` — the CLI argument is required.

### Fixed

- **Kokoro ONNX session pinned to `CPUExecutionProvider`.** Forcing the
  CPU provider avoids spurious "CUDA not available" warnings and the
  startup failure path when the installed `onnxruntime` build enumerates
  GPU providers it can't actually load.

## [0.16.11] — 2026-04-17

### Fixed

- **Embedding model mirror works for real now.** The `_model_downloader`
  fallback pointed at a GitHub release (`models-v1`) that had never
  been cut — every primary-HuggingFace failure would drop into a
  guaranteed 404, so the fallback was theater. Published the release
  at `sovyx-ai/sovyx/releases/tag/models-v1` with
  `e5-small-v2.onnx` + `tokenizer.json` (SHA-256 verified against the
  constants in `_model_downloader.py`). Added an opt-in `network`-marked
  integration test that HEAD-checks every URL in `MODEL_URLS` /
  `TOKENIZER_URLS` so a future drift from the release surfaces in CI
  rather than in a user's first boot. The release is intentionally
  decoupled from Sovyx version tags — it only changes when the
  embedding model itself changes.
- **FTS5 fallback log is quiet.** Removed `exc_info=True` from
  `embedding.py:151`. The FTS5 fallback is the graceful-success path
  (search still works, just without vector similarity); it should not
  dump a full traceback.

## [0.16.10] — 2026-04-17

### Fixed

- **Model-download retry logs no longer spam tracebacks.** Removed
  `exc_info=True` from the transient-retry warning in
  `_model_downloader`. Under a DNS or connect failure the CLI was
  printing a full Rich traceback (100+ lines) per retry × 5 attempts × 2
  URLs, drowning the diagnosis in noise. The structured warning still
  carries `filename`, `source`, `attempt`, `wait`, `error`, and now
  `error_type`; the full traceback still surfaces once when all URLs
  exhaust (via `EmbeddingError` chaining).

## [0.16.9] — 2026-04-17

### Fixed

- **Voice pipeline finally reaches the cognitive loop (gap #5).**
  Transcribed text from `MoonshineSTT` was being dropped silently
  because `on_perception` was never passed to
  `create_voice_pipeline()`. The pipeline captured audio, ran VAD+STT,
  then hit `if self._on_perception is not None` with `None` and
  returned — users saw "Running" cards but got no response. Now
  `enable_voice` resolves the `CognitiveLoop` from the registry, builds
  an `on_perception` closure that wraps the text in a `CognitiveRequest`
  and calls `VoiceCognitiveBridge.process()`, registers the bridge in
  the service registry, and deregisters it on `/api/voice/disable`.
  Streaming defaults to `mind_config.llm.streaming` (Jarvis-illusion
  path — tokens stream into `pipeline.stream_text` as the LLM produces
  them). `VoiceCognitiveBridge` was previously dead code; it is now the
  real bridge between STT transcriptions and the cognitive loop.

### Tests

- **PortAudio reconnect test given a generous wait budget.** The test
  asserts that a second `sounddevice.InputStream` is opened after a
  `PortAudioError`, but on loaded Linux CI runners the
  `to_thread(close) → sleep → to_thread(open)` chain can exceed the old
  500 ms budget. Poll window raised to ~5 s — no production change.

## [0.16.8] — 2026-04-17

### Added

- **Microphone capture loop wired to the voice pipeline.**
  `VoicePipeline` is push-based (`feed_frame`) but nothing opened a mic
  stream, so "Running" in the dashboard meant "state=True, silent".
  `AudioCaptureTask` now owns an `sd.InputStream`; a consumer task
  forwards 16 kHz int16 frames into `pipeline.feed_frame`. Recovers
  from `PortAudioError` by closing + sleeping + reopening. Frames that
  overflow the queue drop the oldest (never block the audio thread).
  `VoiceFactory` returns a `VoiceBundle(pipeline, capture_task)` and
  threads `input_device` / `output_device` through. `/api/voice/enable`
  creates the bundle, starts capture, registers both services; on
  capture failure it tears the pipeline down and returns 500 instead of
  leaving a half-wired registry. `/api/voice/disable` stops capture
  first, then pipeline, and deregisters both.
  `ServiceRegistry.deregister(interface)` added for targeted
  hot-disable. Tunables `capture_reconnect_delay_seconds` /
  `capture_queue_maxsize` under `SOVYX_TUNING__VOICE__*`.
- **Status cards show the real voice engines.** `/api/voice/status`
  previously reported defaults because only `VoicePipeline` was
  registered — the dashboard reads `STTEngine` / `TTSEngine` /
  `SileroVAD` / `WakeWordDetector` individually, so every card showed
  "No engine configured" even with an active pipeline. `/api/voice/enable`
  now registers each sub-component (plus `WakeWordDetector` when
  enabled); `/api/voice/disable` deregisters them so the next enable
  gets fresh instances. `VoicePipeline` exposes
  `vad` / `stt` / `tts` / `wake_word` properties. "Running" now requires
  `pipeline.is_running AND capture.is_running` — the only honest
  semantics. `voice_status` reports a `capture` block (`running` +
  `input_device`) and derives `pipeline.running` from both states.

## [0.16.7] — 2026-04-17

### Added

- **Enterprise-grade model downloads (SileroVAD + Kokoro TTS).**
  1. SHA-256 checksum verification — downloaded file hash is validated
     before atomic rename; mismatch deletes the temp file and raises.
     Hashes hardcoded in `VoiceModelInfo` for all 3 downloadable models.
  2. Retry with exponential backoff — 3 attempts with 1 s / 2 s / 4 s
     delays. Each attempt logged. Final failure raises `RuntimeError`
     with context.
  3. Progress logging — logs `downloaded_mb` / `total_mb` / `percent`
     every 10 MB. Users on slow connections see incremental progress
     instead of silence for 2+ minutes.
  4. Temp file cleanup verified for all paths.

## [0.16.6] — 2026-04-17

### Added

- **Auto-download Kokoro TTS model on first use.** Kokoro TTS
  (88 MB int8) + voices file (27 MB) are auto-downloaded from the
  GitHub release on first pipeline creation, same pattern as SileroVAD.
  Files land in `~/.sovyx/models/voice/kokoro/`
  (`kokoro-v1.0.int8.onnx`, `voices-v1.0.bin`). Download timeout raised
  from 60 s to 300 s for larger models.

### Fixed

- **Kokoro model filename.** `_MODEL_Q8` was `kokoro-v1.0-q8.onnx`
  but the release uses `kokoro-v1.0.int8.onnx` — silent 404 before.
- **Device-select dropdowns follow the dark theme.** Replaced the
  transparent bg + custom chevron layout with the same select styling
  used by `PersonalityStep`'s language dropdown (`bg-elevated`,
  `border-default`, `text-primary`, `colorScheme: dark`).

## [0.16.5] — 2026-04-17

### Added

- **Audio device dropdown selectors for the voice pipeline.** Backend
  `GET /api/voice/hardware-detect` returns devices as
  `{index, name, is_default}` objects, deduplicated by name (Windows
  exposes duplicates per host API). The OS default device is marked
  via `sd.default.device`. Frontend Input/Output cards are now dropdown
  selectors showing all detected devices, with the default
  pre-selected from OS preference. Selected devices are passed to
  `POST /api/voice/enable` and persisted to `voice` config in
  `mind.yaml`. Updated in both `VoiceStep` (onboarding) and
  `VoiceSetupModal` (settings).

## [0.16.4] — 2026-04-17

### Fixed

- **Audio device detection silently skipped every device.**
  `sounddevice.query_devices()` returns `DeviceList` (a `tuple`
  subclass, not `list`); the old `isinstance(devices, list)` guard
  returned empty lists for everyone. Iterate directly now. Split
  exception handling: `ImportError` → silent skip, other exceptions
  → logged. Fixes both `/api/voice/hardware-detect` and
  `/api/voice/enable`.
- **Voice-enable error propagation.** Frontend only parsed the response
  body for 400 errors. For 500 (pipeline creation failure) it showed a
  generic "Failed to enable voice pipeline" instead of the server's
  actual error. Now parses the body for all `ApiError` statuses in
  both `VoiceStep.tsx` (onboarding) and `VoiceSetupModal.tsx`
  (settings).

## [0.16.3] — 2026-04-17

### Fixed

- **Auth-gated WebSocket + polling.** The dashboard connected its
  WebSocket with an empty token before the user authenticated, and
  periodic status/health polling fired 401s for the same reason. Both
  now gate on the `authenticated` store state — no WS connection or
  API polling until the token is validated.
- **Telegram channel setup no longer crashes with 502.** The handler
  imported `aiohttp` (not a declared dependency), so the first webhook
  registration 500'd with `ModuleNotFoundError`. Replaced with
  `httpx.AsyncClient`, which is already in deps.

## [0.16.2] — 2026-04-17

### Fixed

- **Full Windows-compat audit — 5 remaining Unix-only APIs eliminated.**
  1. `lifecycle.py`: `_is_process_alive()` uses `ctypes.OpenProcess`
     on Windows instead of `os.kill(pid, 0)` (which raises `OSError`
     for signal 0 on Windows).
  2. `lifecycle.py`: `_notify_systemd()` early-returns on Windows,
     eliminating the `AF_UNIX` mypy error.
  3. `health.py`: `_check_memory()` uses `psutil` instead of Unix-only
     `resource` + `/proc/meminfo`.
  4. `doctor.py`: `_check_memory_usage()` uses `psutil` instead of
     `resource`; removed the dead `_get_total_memory_mb()` helper.
  5. `auto_select.py`: `detect_hardware()` uses
     `psutil.virtual_memory()` instead of `os.sysconf()`
     (doesn't exist on Windows).
  All 5 were the last remaining Unix-only APIs in `src/sovyx/`. Zero
  Windows mypy errors remain (previously 6).

## [0.16.1] — 2026-04-17

### Fixed

- **Lifecycle signal handlers on Windows.**
  `loop.add_signal_handler()` raises `NotImplementedError` on Windows.
  Uses a `sys.platform` conditional: `signal.signal()` fallback on
  Windows, `loop.add_signal_handler()` on Unix/macOS (unchanged
  behavior).

## [0.16.0] — 2026-04-17

### Added

- **RPC TCP fallback on Windows.** `asyncio.start_unix_server` /
  `AF_UNIX` don't exist on Windows; the RPC server and client now
  branch on `sys.platform`:
  - Unix/macOS: Unix domain socket (unchanged).
  - Windows: TCP 127.0.0.1 on an ephemeral port, with the port written
    to a `.port` file next to the socket path.
  `DaemonClient._read_port()` validates that the port is in `1–65535`.
  Tests are platform-aware: mock daemons use TCP on Windows, and the
  Unix-only permission test is skipped there.

## [0.15.7] — 2026-04-17

### Added

- **Memory consciousness in system prompt** — the mind now knows
  it has persistent memory. Instructs the LLM to reference retrieved
  concepts as first-person knowledge, confirm it will remember when
  asked, and never claim it cannot store information.

### Fixed

- **Unicode em-dash** in ChannelsStep — `\u2014` rendered as
  literal text in JSX, replaced with actual `—` character.

## [0.15.6] — 2026-04-17

### Fixed

- **Mind name discovery** — `sovyx start` now scans data directory
  for the first mind.yaml instead of hardcoding path to "aria/".
  Fixes mind name showing as "Aria" when user created a mind with
  a different name via `sovyx init MyName`.
- **PortAudio OSError** — voice dependency check catches OSError
  (PortAudio library not found) in addition to ImportError. Returns
  structured 400 with platform-specific install instructions instead
  of crashing with 500.

## [0.15.5] — 2026-04-17

### Fixed

- **Default mind name** — `sovyx init` default changed from
  "Aria" to "Sovyx" to match frontend fallback and project name.

## [0.15.4] — 2026-04-17

**Chat redesign — SSE streaming with cognitive transparency.**

### Added

- **SSE streaming chat** (`POST /api/chat/stream`) — token-by-token
  rendering via Server-Sent Events. Automatic fallback to batch
  endpoint when SSE fails.
- **Cognitive transparency** — real-time phase indicators during
  message processing (perceiving, attending, thinking, acting,
  reflecting) with detail strings inline in the SSE stream.
- **Inline cost/tokens/latency** — each AI message shows tokens,
  cost, latency, and model below the bubble. Ollama shows "local".
- **Conversation sidebar** — collapsible sidebar in chat page with
  conversation list, search, click-to-load history.
- **Mood indicator** — PAD emotional state dot + label in chat
  header from /api/emotions/current.
- **Typing cursor** — blinking cursor during streaming.
- **Smart scroll** — auto-scroll only when near bottom, floating
  "scroll to bottom" button.
- **Retry button** — error banner shows "Retry" to resend last
  message.

### Fixed

- **Safety filter feedback** — filtered messages now return
  "I can't respond to that request." instead of empty string.
- **Telegram hot-add in Overview** — channel setup now uses
  hot-add endpoint (zero restart).
- **Unified formatCost** — single function across all cost displays.
- **ConversationTracker Protocol** — metadata kwarg for add_turn.

## [0.15.3] — 2026-04-16

### Fixed

- **Language directive in system prompt** — changed `Language: pt`
  (ambiguous label) to `Language: Always respond in Portuguese.`
  (direct instruction). LLMs now follow the configured language.
- **Translated welcome messages** — onboarding Step 5 welcome
  message available in pt, es, fr, de instead of English-only.

## [0.15.2] — 2026-04-16

### Added

- **Emotions page** — full PAD 3D emotional state visualization
  replacing the Coming Soon stub. Current mood card with human
  labels, valence timeline (recharts AreaChart), PAD scatter plot
  with projection toggle (VxA/VxD/AxD), emotional triggers list,
  mood distribution pie chart. 4 backend endpoints, 5 components.
- **Voice setup in onboarding** — Step 4 (optional) with hardware
  detection + hot-enable when deps installed, or install command
  with copy button when deps missing. Onboarding is now 5 steps.

### Fixed

- **Live Feed health icons** — dynamic icon based on status
  (green=checkmark, yellow=triangle, red=X) instead of always
  showing warning triangle.
- **user_name in system prompt** — field added to MindConfig,
  saved by onboarding, injected as "You are talking to {name}".
- **Knowledge plugin** — missing `permissions` (BRAIN_READ/WRITE)
  and `setup()`. All 5 tools were silently failing.
- **Web-intelligence plugin** — ddgs+trafilatura moved to default
  deps, httpx fallback for DuckDuckGo, permissions+setup() for
  brain access, setup_schema with provider select.
- **Plugin tags in conversations** — tags persisted in turn
  metadata column, returned by API, rendered in ChatBubble.
- **LLM pricing** — 6 price corrections (gpt-4o, deepseek,
  gemini-2.5-flash, mistral-large, claude-3-5-haiku), 15 new
  models added, provider defaults updated.

## [0.15.1] — 2026-04-16

### Fixed

- **LLM pricing table** — 6 price corrections: gpt-4o ($5 -> $2.50
  input), deepseek-chat/reasoner (V3.2 unified), gemini-2.5-flash
  (preview -> GA), mistral-large-latest, claude-3-5-haiku. 15 new
  models added (Claude 4.5-4.7, GPT-4.1, o3, Gemini GA, Grok 4,
  Llama 3.3). Provider defaults updated. Baseline pinning test (16
  models) catches future drift.
- **Live Feed events** — 5 event types were defined, subscribed by
  DashboardEventBridge, and expected by the frontend but never
  emitted: PerceptionReceived (now in chat + bridge), ResponseSent
  (now after response delivery), ServiceHealthChanged (now on
  health poll status change), ChannelConnected/Disconnected (now on
  channel register/stop).

## [0.15.0] — 2026-04-16

**First-run onboarding -- zero to first conversation in 90 seconds.**

New users opening the dashboard for the first time are guided through
a three-step wizard that configures an LLM provider, personalizes
Aria, and lands them in a live conversation. API keys are validated,
persisted to `secrets.env`, and hot-registered in the LLM router
without restarting the daemon.

### Added

- **Three-step onboarding wizard.** Full-page flow outside the
  dashboard layout: Choose Your Brain (provider + API key),
  Meet Aria (personality preset), Say Hello (live chat).
- **API key hot-registration.** `LLMRouter.add_provider()` registers
  a new provider at runtime. No daemon restart needed after entering
  an API key in the wizard.
- **`secrets.env` persistence.** API keys saved to
  `~/.sovyx/secrets.env` (chmod 0600). Loaded by bootstrap alongside
  `channel.env`.
- **4 personality presets** — Warm & Friendly, Direct & Concise,
  Playful & Creative, Professional. Each maps to a combination of
  PersonalityConfig values.
- **Ollama auto-detection** in wizard. If Ollama is running, it
  appears first in the provider grid with a "Detected" badge and
  model picker. Zero API key needed.
- **Provider metadata** (`providers-data.ts`) — 10 providers with
  names, descriptions, default models, key URLs, pricing info.
- **`MindConfig.onboarding_complete`** — boolean flag persisted to
  mind.yaml. Dashboard checks this to decide whether to show the
  wizard or the normal overview.
- **Auto-redirect** — Overview page redirects to `/onboarding` on
  first run when no LLM provider is configured.
- **4 onboarding API endpoints:**
  - `GET /api/onboarding/state` — completion status, provider
    detection, Ollama availability + models
  - `POST /api/onboarding/provider` — validate key, persist, hot-register
  - `POST /api/onboarding/personality` — save preset or custom values
  - `POST /api/onboarding/complete` — mark onboarding done
- **16 new backend tests** — state, provider validation (cloud +
  Ollama), personality presets, completion, E2E flow.

## [0.14.0] — 2026-04-16

**Setup Wizard -- declarative plugin configuration + voice hot-enable.**

Plugins can now declare a `setup_schema` in their manifest and get
automatic UI generation in the dashboard. Users configure plugins
through a wizard with provider presets, test-connection validation,
and type-safe form fields. Voice can be enabled at runtime from the
dashboard without restarting the daemon.

### Added

- **Declarative setup wizard framework.** Plugins declare
  `setup_schema` (providers, fields, test_connection) in `plugin.yaml`.
  Dashboard auto-renders forms with provider presets, input validation,
  and connection testing. Zero plugin-specific UI code needed.
- **`ISovyxPlugin.test_connection()`** — SDK method for validating
  config before persisting. Returns `TestResult(success, message)`.
- **`PluginManager.reconfigure()`** — runtime config update: teardown,
  rebuild context, re-setup, without daemon restart.
- **`ConfigEditor`** — `ruamel.yaml`-based atomic YAML writer with
  per-file locking. Preserves comments and formatting.
- **Setup wizard manifest models** — `SetupSchema`, `SetupField`,
  `SetupProvider`, `SetupFieldOption` in `plugins/manifest.py`.
- **5 setup API endpoints** — `/api/setup/{name}/schema`,
  `test-connection`, `configure`, `enable`, `disable`.
- **Dashboard setup wizard components** — `SetupWizardModal`,
  `DynamicForm`, `ProviderSelect`, `TestConnectionButton`.
- **CalDAV setup schema** — 5 providers (Fastmail, iCloud, Google,
  Nextcloud, Radicale), 5 fields, test_connection via PROPFIND.
- **Home Assistant setup schema** — 2 fields (URL, token),
  test_connection via `GET /api/`.
- **Voice hot-enable** — `POST /api/voice/enable` instantiates the
  full voice pipeline (SileroVAD + MoonshineSTT + TTS + WakeWord)
  in-process without daemon restart. Dependency detection returns
  structured error with install command.
- **Voice factory** (`voice/factory.py`) — async factory creating all
  5 components with ONNX loads in `to_thread`. TTS fallback chain:
  Piper > Kokoro > error.
- **Voice model registry** (`voice/model_registry.py`) —
  `check_voice_deps()`, `detect_tts_engine()`, `ensure_silero_vad()`
  with auto-download (2.3 MB, atomic write).
- **Hardware detection endpoint** — `GET /api/voice/hardware-detect`
  returns CPU, RAM, GPU, audio devices, tier, recommended models.
- **Voice disable endpoint** — `POST /api/voice/disable` for graceful
  pipeline shutdown with config persistence.
- **`HardwareDetection` component** — auto-detects hardware, shows
  CPU/RAM/GPU/audio summary with tier badge and model list.
- **`VoiceSetupModal` component** — handles success (hot-enable) and
  failure (missing deps panel with copy-able install command, audio
  hardware warning panel).
- **Plugin card "Configure" button** — visible for plugins with
  `has_setup: true`, opens the setup wizard modal.
- **`[voice]` extras group** in `pyproject.toml` — `moonshine-voice`,
  `piper-tts`, `sounddevice`, `kokoro-onnx`.
- **51 new tests** — `test_voice_factory.py` (7), `test_model_registry.py`
  (17), `test_voice_routes.py` (10), expanded `test_setup_routes.py` (17).

### Changed

- `PluginManifest` gains `setup_schema: SetupSchema | None` field.
- `PluginInfo` API response includes `has_setup: bool`.
- Voice page shows "Set up Voice" banner when pipeline not configured.

## [0.13.3] — 2026-04-16

**Open-core GA release — clean public repo with enterprise audit.**

Consolidates all changes since v0.13.1: open-core separation,
enterprise audit fixes, docs alignment, and quality hardening.

### Changed

- **Open-core separation.** Commercial layer (`cloud/` module — billing,
  marketplace, license issuer, LLM proxy, backup R2, dunning, flex,
  usage, API keys) extracted to private `sovyx-cloud` package. Public
  repo runs 100% standalone with zero cloud dependencies.
- **Tier nomenclature aligned** with sovyx-cloud: `STARTER` → `SYNC`
  ($3.99), `SYNC` → `BYOK_PLUS` ($5.99). `ServiceTier` enum in
  `sovyx.tiers` matches `SubscriptionTier` in sovyx-cloud so license
  JWTs validate correctly.
- `argon2-cffi` removed from dependencies (was used only by cloud
  crypto, now in sovyx-cloud). `cryptography` retained for Ed25519
  license validation.

### Added

- **`sovyx.tiers`** — `ServiceTier` enum, `TIER_FEATURES`,
  `TIER_MIND_LIMITS`, `VALID_TIERS` (informational — resolution
  requires sovyx-cloud).
- **`sovyx.license`** — `LicenseValidator` (Ed25519 public key JWT),
  `LicenseStatus`, `LicenseClaims`, `LicenseInfo`. Validates offline.
- **`BackupEncryptor` Protocol** in `upgrade/backup_manager.py` —
  typed interface for at-rest encryption (implemented by sovyx-cloud).
- **`GET /api/brain/search/vector`** — pure KNN vector search endpoint
  (sqlite-vec, separate from hybrid FTS+vector).
- **`LLMTuningConfig`** — complexity classification thresholds
  (`simple_max_length`, `simple_max_turns`, `complex_min_length`,
  `complex_min_turns`) moved from hardcoded constants to
  `EngineConfig.tuning.llm` (overridable via `SOVYX_TUNING__LLM__*`).
- **VoiceCognitiveBridge streaming gate** — `streaming` kwarg respects
  `LLMConfig.streaming` flag (False → batch TTS, True → chunk TTS).
- **7 public module docs** added (16/16 complete): mind, persistence,
  upgrade, observability, cli, context, benchmarks.
- **30 new tests**: `test_tiers.py` (11), `test_license.py` (16),
  `test_public_api_imports.py` (6 smoke tests for sovyx-cloud
  consumer surface).
- All 266 `except Exception` handlers annotated with `# noqa: BLE001`.

### Removed

- `src/sovyx/cloud/` (14 files) — moved to sovyx-cloud.
- `tests/unit/cloud/` (15 files) — moved to sovyx-cloud.
- `tests/property/test_billing_invariants.py` — moved to sovyx-cloud.
- `tests/property/test_dunning_invariants.py` — moved to sovyx-cloud.
- `docs/modules/cloud.md` — moved to sovyx-cloud.
- Cloud optional deps (boto3, litellm, stripe, argon2-cffi).
- Git history rewritten (`git filter-repo`) to eliminate all traces
  of commercial code from public repo.

## [0.13.2] — 2026-04-16

**Open-core separation — commercial layer moved to sovyx-cloud.**

### Changed

- **`cloud/` module removed** — billing, licensing, marketplace, backup
  orchestration, dunning, flex balance, usage cascade, API keys, LLM proxy,
  and all Stripe integration moved to the private `sovyx-cloud` package.
  The open-source daemon runs 100% standalone without cloud services.

### Added

- **`sovyx.tiers`** — `ServiceTier` enum, `TIER_FEATURES`, `TIER_MIND_LIMITS`,
  `VALID_TIERS`. Informational only — tier resolution requires `sovyx-cloud`.
- **`sovyx.license`** — `LicenseValidator` (Ed25519 public key JWT verification),
  `LicenseStatus`, `LicenseClaims`, `LicenseInfo`. Validates licenses offline;
  token issuance lives in `sovyx-cloud`.

### Removed

- `src/sovyx/cloud/` (14 files, ~6 460 LOC) — moved to `sovyx-cloud`.
- `src/sovyx/dashboard/routes/marketplace.py` — moved to `sovyx-cloud`.
- `src/sovyx/persistence/schemas/marketplace.py` — moved to `sovyx-cloud`.
- `tests/unit/cloud/` (12 test files) — moved to `sovyx-cloud`.
- `tests/property/test_billing_invariants.py` — moved to `sovyx-cloud`.
- `tests/property/test_dunning_invariants.py` — moved to `sovyx-cloud`.

## [0.13.1] — 2026-04-15

**6 new LLM providers via OpenAI-compatible base class.**

### Added

- **`OpenAICompatibleProvider`** base class
  (`llm/providers/_openai_compat.py`) — shared `generate()` +
  `stream()` logic for any provider that speaks the OpenAI Chat
  Completions wire format. ~200 LOC replaces what would be ~1800 LOC
  of copy-paste across providers.
- **xAI (Grok)** — `api.x.ai/v1`, `XGROK_API_KEY`, models:
  `grok-2`, `grok-3`.
- **DeepSeek** — `api.deepseek.com/v1`, `DEEPSEEK_API_KEY`, models:
  `deepseek-chat`, `deepseek-reasoner`.
- **Mistral** — `api.mistral.ai/v1`, `MISTRAL_API_KEY`, models:
  `mistral-large-latest`, `mistral-small-latest`.
- **Together AI** — `api.together.xyz/v1`, `TOGETHER_API_KEY`,
  models: `meta-llama/Llama-3.1-70B-Instruct-Turbo` and others.
- **Groq** — `api.groq.com/openai/v1`, `GROQ_API_KEY`, models:
  `llama-3.1-70b-versatile`, `mixtral-8x7b-32768`.
- **Fireworks AI** — `api.fireworks.ai/inference/v1`,
  `FIREWORKS_API_KEY`, models:
  `accounts/fireworks/models/llama-v3p1-70b-instruct`.
- All 6 providers support both `generate()` and `stream()` from day 1.
- Pricing table extended with 12 new model entries.
- Router equivalence map extended: flagship tier (grok-3,
  mistral-large ↔ claude-sonnet, gpt-4o, gemini-pro), fast tier
  (deepseek-chat, mistral-small ↔ haiku, gpt-4o-mini, gemini-flash),
  reasoning tier (+deepseek-reasoner ↔ o1, claude-opus).
- Auto-detection priority chain: Anthropic > OpenAI > Google > xAI >
  DeepSeek > Mistral > Groq > Together > Fireworks.

### Changed

- **`OpenAIProvider` refactored** to subclass
  `OpenAICompatibleProvider` — same public interface, zero duplication
  with the 6 new providers. Existing tests pass unchanged.

### Tests

- 16 unit tests: base class properties, generate with mocked httpx,
  stream with mocked SSE, all 7 subclass shapes, Together's org/
  prefix matching.

## [0.13.0] — 2026-04-15

**LLM streaming — router to voice pipeline (SPE-007 §streaming).**
First-token latency drops from 3-7 s (full LLM response) to ~300 ms
(first SSE chunk → TTS synthesis). The voice pipeline's speculative
TTS path (`stream_text` / `flush_stream` / `start_thinking`) was
scaffolded in v0.9 but never wired — this release closes the loop.

### Added

- `LLMStreamChunk` + `ToolCallDelta` models in `llm/models.py`.
- `LLMProvider.stream()` method added to the Protocol — yields
  `LLMStreamChunk` per token.
- Streaming implementations for all 4 providers:
  **Anthropic** (Messages SSE), **OpenAI** (Chat Completions SSE),
  **Google** (Gemini `streamGenerateContent?alt=sse`), **Ollama**
  (NDJSON `stream: true`).
- `LLMRouter.stream()` — provider selection + complexity routing
  identical to `generate()`; failover only before first chunk;
  cost/metrics/events deferred to the final `is_final` chunk.
- `ThinkStreamStarted` event with `ttft_ms` (time-to-first-token).
  `ThinkCompleted` gains `streamed: bool` + `ttft_ms: int`.
- `ThinkPhase.process_streaming()` — streaming counterpart of
  `process()`. Degradation path yields a single fake chunk.
- `CognitiveLoop.process_request_streaming(request, on_text_chunk)` —
  streaming cognitive loop that reconstructs `LLMResponse` from
  accumulated chunks for ActPhase + ReflectPhase. Tool-call streams
  fall back to the normal ReAct path (no voice streaming during
  tool execution — fillers continue playing).
- `VoiceCognitiveBridge` (`voice/cognitive_bridge.py`) — wires
  `pipeline.start_thinking()` → `cogloop.process_request_streaming`
  → `pipeline.stream_text` → `pipeline.flush_stream`.
- Shared SSE/NDJSON parsers in `llm/providers/_streaming.py`
  (`iter_sse_events`, `iter_ndjson_lines`).

### Design decisions

- **Output guard**: runs on the FINAL text only (option A). If the
  guard rejects, `pipeline.output.interrupt()` stops playback. Per-
  chunk regex guard deferred to V2.
- **Tool-use mid-stream**: when `finish_reason="tool_use"`, no chunks
  reach the voice pipeline — filler continues. Only the final post-
  tool response is spoken (non-streamed — V2 work).
- **Failover**: only before the first chunk. Once a provider starts
  emitting, mid-stream errors propagate to the caller.
- **Cost accounting**: waits for the `is_final` chunk because cloud
  providers emit usage only at SSE stream end.

### Tests

- 12 unit tests: SSE parser, NDJSON parser, LLMStreamChunk shape,
  Router stream provider selection + accounting, CognitiveLoop
  streaming chunk forwarding + LLMResponse reconstruction.

## [0.12.1] — 2026-04-15

**PAD 3D emotional model (ADR-001).** The single highest-priority
architectural divergence from the spec — the 1D emotional model
(concepts) / 2D (episodes) moves to unified 3D Pleasure-Arousal-
Dominance (Mehrabian 1996). Additive, backward-compatible: existing
rows backfill to neutral (0.0) on all new axes, no data migration
required beyond ALTER TABLE ADD COLUMN.

### Changed

- **Concepts** gain `emotional_arousal` (activation, [-1, +1]) and
  `emotional_dominance` (agency, [-1, +1]).
- **Episodes** gain `emotional_dominance` ([-1, +1]). `emotional_arousal`
  was already there from earlier work.
- **Importance scoring** — the existing `emotional` signal weight
  (0.10) is now apportioned across the three axes via fixed
  sub-weights: valence 0.45, arousal 0.30, dominance 0.25. Total
  emotional contribution stays at 0.10, so the formula's overall
  calibration is unchanged — a purely-valence concept at |v|=1 now
  lands at 0.045 of emotional weight (down from 0.10), but a concept
  that's emotional on all three axes saturates at the full 0.10.
  Both axes use `abs()` — fear (low-dominance, high-arousal) and
  triumph (high-dominance, high-arousal) are equally memorable.
- **Consolidation** — weighted-average merge now applies independently
  to all three axes (valence, arousal, dominance) during concept
  reinforcement. Guard: only averages an axis when the incoming signal
  is non-zero, so neutral baselines don't drag existing affect toward
  zero on every reinforcement.
- **REFLECT phase** — concept-extraction LLM prompt now asks for
  arousal + dominance alongside sentiment/valence. Clamps to
  [-1, +1], defaults to 0.0 when the LLM omits a field. Episode
  arousal prefers the explicit LLM value when any is present, falls
  back to the legacy peak-magnitude heuristic otherwise.
- **Conversation import (IMPL-SUP-015)** — summariser prompt extracts
  `emotional_dominance` alongside the existing valence/arousal; the
  summary-first encoder passes all three axes into `learn_concept`
  and `encode_episode`.
- **Exports** — SMF / .sovyx-mind archives now carry the three axes
  in concept + episode frontmatter. Legacy archives lacking the new
  fields re-import cleanly with 0.0 fallbacks.
- **Dashboard** — `/api/brain/graph` node payloads now include
  `emotional_arousal` and `emotional_dominance` alongside valence
  (3dp rounding, frontend-compatible additive change).

### Added

- Migration 006 on brain.db: ALTER TABLE ADD COLUMN for the three
  new fields with DEFAULT 0.0.
- `_emotional_intensity(v, a, d)` helper in `brain/scoring.py` — the
  single source of truth for how PAD axes combine into the scorer's
  scalar `emotional` signal.

### Non-goals for v0.12.1 (deferred)

Deliberate MVP scope — the following PAD consumers stay on the roadmap
for a later patch but are not load-bearing for v0.12.1:

- Homeostasis processing (baseline drift from recent PAD exposure).
- Personality prompt modulation (PAD → system-prompt coloring).
- TTS affective modulation (PAD → voice prosody).
- Frontend types + visualisations (dashboard currently exposes the
  fields but no UI widget renders them).

### Migration notes

- **Backward compatibility.** `_row_to_concept` / `_row_to_episode`
  defensively fall back to 0.0 when a row predates migration 006 —
  handles edge cases like partial SELECT on mid-migration DBs.
- **Existing rows stay neutral.** We do NOT LLM-backfill historical
  concepts/episodes. Neutral (0.0 on all three axes) is the honest
  "we don't know" signal, and scoring treats 0.0 as contributing
  nothing to the emotional boost — rows just look emotionally silent
  until they're re-learned or consolidated.

## [0.11.9] — 2026-04-15

CalDAV calendar integration as a plugin — IMPL-009 v0, scope-tightened
from spec to read-only.

### Added

- **CalDAV plugin** (`plugins/official/caldav.py`) — 6 read-only tools
  (`list_calendars`, `get_today`, `get_upcoming`, `get_event`,
  `find_free_slot`, `search_events`). Compatible with Nextcloud,
  iCloud, Fastmail, Radicale, SOGo, and Baikal. Talks PROPFIND /
  REPORT XML directly through the existing `SandboxedHttpClient`
  (with the new public `request()` method) — does **not** use the
  third-party `caldav` package because it routes its own HTTP and
  bypasses the sandbox. iCalendar parsing via the lightweight
  `icalendar` library; RRULE expansion via `python-dateutil`, capped
  at 200 instances. `defusedxml` parses every server-controlled XML
  body to defuse XXE risk on REPORT/PROPFIND responses. Per-window
  event cache (5 min TTL). Configuration in `mind.yaml` under
  `plugins_config.caldav` with `base_url`, `username`, `password`
  (use app-specific passwords for iCloud / Fastmail), optional
  `verify_ssl`, `default_calendar`, `allow_local`, `timezone`.
- **`SandboxedHttpClient.request(method, url, ...)`** — public
  arbitrary-method entry point for plugins that speak HTTP-extension
  protocols (CalDAV PROPFIND/REPORT, WebDAV). Every existing sandbox
  guard — URL allowlist, local-IP block, DNS rebinding check, rate
  limit, response size cap, timeout — applies unchanged.
- New deps: `icalendar>=5.0`, `defusedxml>=0.7`.

### Non-goals (deliberate)

- No write surface — events are read-only. No create / edit / delete.
- No incremental sync (no ctag/etag) — every refresh re-issues a full
  REPORT for the time window. Acceptable for v0 (~50 KB per request);
  ctag/etag is on the next-PR list.
- No subscribe / push notifications.
- One calendar source per plugin instance (multi-account is v0.2).
- **Google Calendar discontinued CalDAV in 2023** — not supported.

### Tests

- 43 unit tests covering metadata, lifecycle, every tool's success
  and error paths (auth failure / not-found / malformed XML / empty
  results), calendar discovery + cache TTL, calendar-name filtering,
  free-slot algorithm pure logic, helpers.

## [0.11.8] — 2026-04-15

Home Assistant integration as a plugin — IMPL-008 v0.

### Added

- **Home Assistant plugin** (`plugins/official/home_assistant.py`) —
  4 domains, 8 LLM-callable tools across light (`list_lights`,
  `turn_on_light`, `turn_off_light`), switch (`turn_on_switch`,
  `turn_off_switch`), sensor (`read_sensor`, `list_sensors`), and
  climate (`set_temperature`, the only confirmation-required tool in
  v0). Talks REST to the user's Home Assistant instance via
  `SandboxedHttpClient` with `allow_local=True` (HA usually lives at
  `http://homeassistant.local:8123` or a private IP). Per-domain
  in-memory entity cache (60 s TTL) with eviction on service-call.
  Declares `Permission.NETWORK_LOCAL`.
- **Architectural decision**: HA was originally specced as a bridge
  (IMPL-008). Shipped as a **plugin** instead — HA exposes a device
  API, not a conversational channel; the plugin substrate gives it
  sandbox, permissions, lifecycle, dashboard UI, and HACS-compatible
  packaging for free.

### Non-goals (deliberate)

- No WebSocket subscription — entity state is fetched on demand. The
  mind doesn't see a light flipped manually until the next tool call.
- No mDNS discovery — caller supplies `base_url` explicitly.
- Only 4 domains in v0 — covers / locks / fans / media_player /
  scenes / scripts ship one PR per domain.

### Tests

- 50 unit tests covering metadata, lifecycle, the not-configured
  guard, every tool's happy path, every tool's error paths
  (401 / 404 / 500 / network exception / invalid entity_id / wrong
  domain), cache TTL behaviour (hit / invalidation / staleness /
  fallback), and module-level helpers.

## [0.11.7] — 2026-04-15

Interactive CLI REPL — `sovyx chat` (SPE-015 §3.1). Closes a long-
standing gap noted in the CLI module spec.

### Added

- **`sovyx chat`** — line-oriented REPL over the existing JSON-RPC
  Unix socket (not HTTP). Runs even when the dashboard is disabled.
  prompt_toolkit session with persistent history at
  `~/.sovyx/history` (chmod 0600), word-completer over the slash
  command vocabulary, history search.
- **7 slash commands**: `/help` (also `/?`), `/exit` / `/quit`
  (Ctrl+D works too), `/new` (rotate `conversation_id`), `/clear`
  (wipe screen + rotate), `/status`, `/minds`, `/config`. Every
  unknown command returns a friendly help-pointer instead of
  raising. Every boundary handler wraps the call in a `try` that
  renders the error inline — one bad turn never crashes the session.
- **3 new RPC handlers** wired in `engine/_rpc_handlers.py`:
  `chat`, `mind.list`, `config.get`. The `chat` handler reuses
  `dashboard.chat.handle_chat_message` (the same entry point
  `POST /api/chat` uses) with `ChannelType.CLI` and a stable
  `cli-user` channel id, so `PersonResolver` keeps CLI sessions on
  a separate identity from the dashboard.
- New dep: `prompt_toolkit>=3.0`.

### Tests

- 47 tests across slash-command parsing + dispatch (24) and REPL
  loop integration with mocked client + fake session (23). Covers
  every command, every error path, EOF handling, history-file
  permissions on POSIX, and the full driven-session entry point.

## [0.11.6] — 2026-04-15

DREAM phase — the seventh and final phase of the cognitive loop
(SPE-003 §1.1, "nightly: discover patterns"). Closes Top-10 gap #9.

### Added

- **DREAM phase** (`brain/dream.py`) — `DreamCycle` + `DreamScheduler`
  in the same module, mirroring `brain/consolidation.py`. Unlike the
  request-driven phases (Perceive → Reflect), DREAM runs on a
  time-of-day schedule (default `02:00` in the mind's timezone) while
  the user is likely asleep — biologically inspired by REM-era
  hippocampal replay (Buzsáki 2006).
- **3-phase pipeline per run**: (1) fetch episodes in
  `dream_lookback_hours` window (default 24 h) via the new
  `EpisodeRepository.get_since`, (2) short-circuit if fewer than 3
  episodes, (3) one LLM call extracts up to `dream_max_patterns`
  recurring themes (default 5) → each pattern becomes a `Concept`
  with `source="dream:pattern"`, `category=BELIEF`, and a modest
  `confidence=0.4` (lifts via access). Concepts that appear in two
  or more distinct episodes get fed to `HebbianLearning.strengthen`
  with attenuated activation (0.5) — cross-episode is a weaker
  signal than within-turn. Capped at 12 concepts per run to bound
  the O(n²) within-pair cost.
- **Time-of-day scheduler** — `DreamScheduler._loop` sleeps until
  the next `dream_time` occurrence in the mind's timezone, with
  ±15 min jitter. Survives cycle exceptions (logged, not bubbled).
  Time arithmetic in `_seconds_until_next_dream(now=...)` accepts an
  injectable clock so tests can drive it deterministically.
- **`DreamCompleted` event** — `patterns_found, concepts_derived,
  relations_strengthened, episodes_analyzed, duration_s`. Emitted on
  every run (including short-circuits). Subscribed by the dashboard
  WebSocket bridge with a Moon icon in the activity feed.
- **Kill-switch via config**: `dream_max_patterns: 0` in `mind.yaml`
  causes bootstrap to skip `DreamScheduler` registration entirely.
  No flag sprawl, zero runtime overhead when disabled.
- **`EpisodeRepository.get_since(mind_id, since, limit=500)`** — new
  method returning episodes created at or after `since` in
  chronological order.
- **`BrainConfig.dream_lookback_hours`** (default 24, range 1–168)
  and `BrainConfig.dream_max_patterns` (default 5, range 0–50).

### Tests

- 27 cycle tests across short-circuits, pattern extraction (LLM
  failure, malformed JSON, code-fenced wrappers, empty fields),
  cross-episode Hebbian (co-occurring boost, single-episode skip,
  Hebbian failure, activation damping), event payload, digest
  rendering (long summary truncation, missing summary fallback),
  and lookback window respect.
- 13 scheduler tests on time arithmetic (target later today, target
  passed, exactly-now rolls to tomorrow, midnight edge, naive `now`
  treated as scheduler tz, delta never exceeds one day), fallbacks
  (invalid HH:MM, unknown timezone), lifecycle idempotency.
- 4 `EpisodeRepository.get_since` tests.

### Fixed

- `lifecycle.py`: gate `MindManager.resolve` behind scheduler
  registration. The DREAM wiring originally hoisted the resolve out
  of the per-scheduler `if`-block to share `mind_id`, which broke
  seven lifecycle tests on Linux CI that wire only the cognitive
  loop without `MindManager`. Resolve now happens only when at least
  one scheduler is registered.

## [0.11.5] — 2026-04-15

Claude and Gemini conversation importers — second and third of four
planned platforms (ChatGPT shipped in v0.11.4; Obsidian remains).

### Added

- **ClaudeImporter** (`upgrade/conv_import/claude.py`) — parses the
  `conversations.json` that Anthropic emails users on data export.
  Substantially simpler shape than ChatGPT's regeneration-capable
  tree: a flat array of conversation objects, each with a flat
  `chat_messages` list in chronological order. Maps `sender:"human"`
  → `role:"user"`, prefers the newer typed `content[]` array over
  the legacy flat `text` field, parses ISO-8601 timestamps via
  `datetime.fromisoformat` (Z-suffix tolerated). Attachments and
  files explicitly ignored in v1 (consistent non-goal across all
  importers).
- **GeminiImporter** (`upgrade/conv_import/gemini.py`) — handles
  Google Takeout's activity-stream format (no native conversation
  boundaries, no role field — just a flat stream of localized
  "You said:" / "Gemini said:" title strings). Three-pass pipeline:
  (1) classify + filter — keep entries from `Gemini Apps` /
  `Bard` headers, drop meta-activity ("You used Gemini"); (2) sort
  chronologically (Takeout emits newest-first); (3) group by time
  gap — consecutive turns within 30 minutes form one conversation.
  Locale prefix catalogs for EN, PT, ES, FR, DE, IT, plus legacy
  `Bard` headers. HTML entities decoded (`&aacute;`, `&#39;`); `<b>`
  / `<i>` tags stripped. Synthesized `conversation_id` =
  `sha256(f"gemini:{first_turn_iso}").hexdigest()[:16]` — re-importing
  the same archive produces identical IDs, so the
  `conversation_imports` dedup table skips previously-seen sessions.
  The 30-minute session-gap is a load-bearing constant: changing it
  retroactively shifts group boundaries and therefore IDs (documented
  as a dedup-stability contract in the constant's docstring).
- Both importers wired into
  `dashboard/routes/conversation_import.py::_IMPORTERS` and the
  frontend `ConversationImportPlatform` type extended to accept
  `"claude"` and `"gemini"`.

### Tests

- ~70 parser tests across the two new platforms (role detection,
  session grouping boundaries, content[]+text fallback, meta-activity
  filtering, HTML handling, ID stability, malformed input,
  unsupported-locale drop, title synthesis).
- HTTP smoke tests assert the dashboard router accepts
  `platform=claude` and `platform=gemini` and starts a job for each.

## [0.11.4] — 2026-04-15

New-user onboarding: import existing conversation history from other assistants so the mind already knows you on day one. Ships ChatGPT this release; Claude / Gemini follow the same shape in later releases.

### Added

- **ChatGPT conversation importer** (IMPL-SUP-015 first tranche). Parses a ChatGPT data-export `conversations.json`, walks the `mapping` tree from `current_node` up through parents to extract the mainline (forks from regeneration stay abandoned), and encodes each conversation as one `Episode` plus up to five extracted `Concept` rows. Architecture is **summary-first** (Option C in IMPL-SUP-015): one fast-model LLM call per conversation produces `{summary, concepts, emotional_valence/arousal, importance}`. Target cost ~$0.001-0.003 per conversation — $3 and ~20 minutes for a 1000-conversation import. A synchronous fallback path preserves the Episode even when the LLM router is missing or returns malformed JSON.
- **New subpackage `sovyx.upgrade.conv_import`** housing the import machinery: platform-neutral `RawConversation`/`RawMessage` dataclasses, a `ConversationImporter` Protocol, the `ChatGPTImporter`, `summarize_and_encode` encoder, `ImportProgressTracker` (async-lock-guarded, snapshot-returning), `source_hash` dedup helper. Follow-up platform parsers (Claude, Gemini) drop a sibling file and register in the endpoint's platform map; the HTTP surface and tracker stay unchanged.
- **New endpoints**: `POST /api/import/conversations` (multipart: `platform` + `file`) → `202 Accepted {job_id, conversations_total}` with a background `asyncio.Task` driving the encode loop; and `GET /api/import/{job_id}/progress` → live snapshot `{state, conversations_total/processed/skipped, episodes_created, concepts_learned, warnings, error, elapsed_ms}`. Same 100 MiB upload cap + Bearer-token auth as every other dashboard route.
- **Dedup at conversation level** via a new `conversation_imports` table keyed by `sha256(platform||conversation_id)`. Re-importing the same export is a no-op per conversation; verified by an end-to-end HTTP test. Backed by a new migration 005 on `brain.db`.
- Frontend types: `ConversationImportPlatform`, `ConversationImportState`, `StartConversationImportResponse`, `ConversationImportProgress` in `dashboard/src/types/api.ts` with mirrored zod schemas in `schemas.ts` — ready for a UI follow-up PR.
- Test fixture `tests/fixtures/chatgpt/sample_conversations.json` (3 synthetic conversations: linear, branched, multimodal) plus 54 new tests across parser / hash / tracker / summary-encoder / HTTP endpoints.

### Fixed

- `test_brain_schema.py` migration-count assertions and three test function names bumped for migration 005.

### Non-goals (explicit — roadmap candidates for later releases)

- Claude, Gemini, Obsidian importers — same Protocol + HTTP surface, follow-up PRs.
- Deep-import mode (per-turn REFLECT) — expensive; deferred.
- Attachments / multimodal asset extraction — v1 stringifies with a marker only.
- PII scrubbing on import — user's own data, explicit decision.
- WebSocket progress events — polling only for v1.
- Resuming interrupted imports — daemon restart means re-submit.
- Frontend UI for import — this release ships backend + types only; dashboard wiring lands in a follow-up.

## [0.11.3] — 2026-04-15

Quality pass: exhaustive bare-`except` audit + cleanup across the backend, plus a latent React render bug in the brain-graph accessibility fallback.

### Changed

- **BLE001 sweep across `src/sovyx/`** (4 commits). Ruff's `flake8-blind-except` rule is now enabled (`BLE` added to `[tool.ruff.lint] select`), so any new `except Exception:` fails CI. Net effect: **77 un-justified broad catches → 0**. Categorised cleanup:
  - **Batch 1** (`4d1833f`) — 49 legitimate boundaries explicitly annotated with `# noqa: BLE001 — <reason>`. Covers health-check runners (`engine/health.py` + `observability/health.py`), CLI command handlers (`cli/main.py`), boundary translation into domain exceptions (`engine/bootstrap.py`, `engine/rpc_server.py`, `cognitive/reflect/phase.py`, `bridge/manager.py`, `upgrade/blue_green.py`, `upgrade/schema.py`, `voice/pipeline/_orchestrator.py`), and background loops that must not die on single failures (`cognitive/loop.py`, `bridge/channels/{telegram,signal}.py`, `voice/wyoming.py`, `llm/router.py`).
  - **Batch 2** (`069d3eb`) — 9 silent-swallow sites narrowed to typed exception tuples with `exc_info=True` added where missing: `plugins/sdk.py` `get_type_hints`, `engine/bootstrap.py` YAML read/write, `brain/_model_downloader.py` retry loop, `llm/providers/ollama.py` ping/list-models, `voice/jarvis.py` filler synthesis, `cognitive/reflect/phase.py` novelty compute, `brain/contradiction.py` LLM detection, `cognitive/financial_gate.py` intent classification.
  - **Batch 3** (`4e696fe`) — brain + persistence + cost DB narrows: `brain/consolidation.py` centroid refresh + per-pair merge, `brain/embedding.py` ONNX model load, `brain/retrieval.py` vector/episode search, `brain/service.py` `_safe_record_access`, `persistence/pool.py` WAL checkpoint + extension load, `llm/cost.py` restore/persist/daily-flush.
  - **Batch 5** (`853c8d3`) — voice + bridge API narrows: `voice/pipeline/_orchestrator.py` STT transcribe + TTS synthesize (all 4 call sites), `voice/tts_kokoro.py` `list_voices`, `bridge/channels/telegram.py` `edit_message_text` (narrowed to `AiogramError`).
- Pre-existing `# noqa: BLE001` catches triaged in the earlier Sprint 2 sweep were spot-checked and left as-is — all 12 sampled were legitimate resilience boundaries with fallback + logging.
- `tests/**/*.py` added to BLE001 per-file-ignores: security fuzz (`tests/security/test_frontend_attack.py`) and stress loops (`tests/stress/ws_stress_test.py`) legitimately need broad catches to probe attack surfaces / keep harnesses alive.

### Fixed

- **React error #31 on `/brain`** (`c74aab9`). `react-force-graph-2d` (via d3-force) mutates link objects in place once the simulation starts — `link.source` and `link.target` are replaced with references to the node objects themselves. The screen-reader fallback table was rendering the raw mutated object as a `<td>` child, triggering "Objects are not valid as a React child". A silent correctness bug also lived in the same paths: `connectionCounts` was keying its Map by the mutated objects, so every concept silently showed "0" connections in the SR table. Introduced `linkEndpointId()` coercion helper applied at every leak site (memo, render keys, table cells); regression test constructs a link with fully mutated endpoints and asserts both symptoms.
- Tests that seeded typed exceptions (`LLMError`, `SearchError`) in `AsyncMock.side_effect` were updated to use the builtin/stdlib equivalents already present in the narrow tuples (`ValueError`, `sqlite3.OperationalError`). Internal-class seeding is covered by CLAUDE.md anti-pattern #8 — under pytest-cov's trace-based source rewriting, the test-side and production-side class objects can diverge, causing `except (..., SearchError, ...)` to miss. Seeding builtins avoids the class-identity drift while keeping the production narrow unchanged.

### Diagnostic improvements

- 15 `logger.*` call sites gained `exc_info=True`. Previously-silent degradation paths — TTS/STT failures, Ollama ping, YAML persist, cost-guard errors, model-download retry, filler synthesis, Kokoro voice listing — now emit full tracebacks at their existing log level, so real bugs can be told apart from expected fallback.
- `react_iteration` log line now carries `tools=[...]` and `plugins=[...]` fields alongside the per-iteration counts, completing the observability parity promised by the v0.11.2 module-tags feature.

## [0.11.2] — 2026-04-15

### Added

- **Module/plugin tags on every chat response.** Every assistant message now carries at least one visible tag (pill) indicating which modules produced the reply. Pure cognitive replies show `brain`; tool-backed replies show the plugin name(s) followed by `brain`. Tags are derived from the ReAct loop's `tool_calls_made` list (no new data plumbing — plugin names come from the existing namespaced `plugin.tool` format) and rendered above the assistant bubble via a new `MessageTags` React component with i18n labels and raw-name fallback for unknown plugins.
- `react_iteration` log call now includes `tools` and `plugins` fields for observability parity with the new wire-format contract.
- `ChatResponse.tags?: string[]` and matching zod schema on the frontend; `ChatMessage` extended with the same field for thread-level rendering.

## [0.11.1] — 2026-04-15

Sprint 6 — 90 % → 95 % enterprise polish. Thirteen focused items across accessibility, resilience, observability, and schema hygiene. All CI gates green.

### Fixed

- 10 pre-existing TypeScript errors (schema drift `SafetyConfig`).
- Pricing tables unified into single source (`llm/pricing.py`).
- `BatchSpanProcessor` replaces `SimpleSpanProcessor` (IMPL-015).
- Last raw `httpx` in plugins migrated to `SandboxedHttpClient`.
- OTel `setup_tracing` resilient to prior shutdown.

### Added

- Emotional baseline config (`EmotionalBaselineConfig` in `EngineConfig`).
- Per-section `ErrorBoundary`s with telemetry reporting.
- `brain-graph` screen-reader fallback table.
- `log-row` keyboard accessibility (role, tabIndex, onKeyDown).
- i18n aria-label sweep (9 hardcoded → `useTranslation`).
- `safeStringify` with secret redaction.
- Vector search documented as implemented.

### Security

- Sidebar cookie hardened (`SameSite=Strict`, `Secure`).

## [0.11.0] — 2026-04-14

The v0.11 line is an enterprise hardening pass across backend, frontend, and CI infrastructure. Five focused sprints: security P0, god-file splits, concurrency + config hardening, frontend hardening, and 90% polish.

### Security

- Wyoming voice server: bearer-token auth, rate limit, payload cap, read timeout.
- Plugins: every official plugin now routes HTTP through `SandboxedHttpClient`; raw `httpx` from plugin code is no longer permitted.
- AST scanner: blocks `builtins`, `tempfile`, `gc`, `inspect`, `mmap`, `pty`, plus the `().__class__.__base__.__subclasses__()` escape chain.
- CLI: `sovyx init --name` validated via regex; path traversal closed.
- Dashboard: import endpoint size cap (100 MB) + streaming parse; chat max-length 10 000 chars.
- LLM providers: Google API key moved from URL parameter to `x-goog-api-key` header.
- Frontend: token migrated from `localStorage` to `sessionStorage` + in-memory fallback; `window.prompt()` replaced with Radix Dialog; WS URL derived from `location.protocol`; `use-auth` now fail-closed on network errors.
- Frontend: `safeStringify` (size clamp + secret redaction) applied to `plugin-detail` manifest, tool parameters, and `log-row` extra fields.

### Added

- `engine/_lock_dict.LRULockDict` — bounded `asyncio.Lock` dict with LRU eviction; shared by `bridge/manager.py`, `cloud/flex.py`, `cloud/usage.py`.
- `EngineConfig.tuning.{safety,brain,voice}` — tuning knobs previously hardcoded now overridable via `SOVYX_TUNING__*` env variables.
- Frontend runtime response validation: `src/types/schemas.ts` holds zod schemas for 11 response shapes; `api.get/post/put/patch/delete` accept an optional `{ schema }` option that runs `safeParse` and logs mismatches.
- Frontend: `api.patch()`, `buildQuery()` helper, default 30 s timeout via composable `AbortController`, retry with exponential backoff + jitter on 408/429/502/503/504 for idempotent verbs.
- Frontend error telemetry: `POST /api/telemetry/frontend-error` endpoint (rate-limited 20 / 60 s, pydantic length caps) + `ErrorBoundary.componentDidCatch` hook.
- Virtualization on `chat-thread.tsx` and `cognitive-timeline.tsx` via TanStack Virtual.
- 56 new component tests across 13 `components/dashboard/` files + 3 `components/ui/` primitives.
- 5 new critical tests: `plugins.tsx` page-level, command palette Cmd+K, `router.tsx` lazy + ErrorBoundary, settings slider/preset/save interactions.
- `src/lib/safe-json.ts` with 9 tests — size clamp and secret-key redaction for DOM-rendered JSON.
- `persistence/pool._read_index_lock` — round-robin cursor now atomic under contention.
- `observability/alerts._state_lock` — evaluate() serialized; concurrent callers no longer double-fire `AlertFired`.

### Changed

- **God files split into subpackages** (public surface preserved via `__init__.py` re-exports):
  - `dashboard/server.py` (2 134 LOC) → `dashboard/routes/` (16 APIRouter modules).
  - `cognitive/safety_patterns.py` (1 165 LOC) → `cognitive/safety/patterns_{en,pt,es,child_safe}.py`.
  - `cognitive/safety_classifier.py` (704 LOC) → `cognitive/safety/_classifier_*`.
  - `cognitive/reflect.py` (1 021 LOC) → `cognitive/reflect/` (phase.py + 5 helpers).
  - `voice/pipeline.py` (840 LOC) → `voice/pipeline/` (orchestrator + state + output queue + barge-in + config).
  - `plugins/manager.py` (819 LOC) — `_event_emitter.py`, `_manager_types.py`, `_dependency.py` extracted.
  - `brain/service.py` (712 LOC) — `_novelty.py` + `_centroid.py` extracted.
  - `brain/embedding.py` (705 LOC) — `_model_downloader.py` extracted.
- ONNX inference (Piper, Kokoro, Silero, Moonshine, openWakeWord) now runs via `asyncio.to_thread()`; the event loop no longer stalls during synthesis or wake-word checks.
- `cloud/backup` boto3 calls (upload / list / batch-delete) in the scheduler wrapped in `asyncio.to_thread()` so backup cycles don't block the loop.
- BLE001 sweep: `except Exception:` turned into typed handlers with explicit `log + re-raise` where appropriate; blanket exception catches removed from cognitive/, plugins/, cloud/, cli/.
- Frontend hot paths memoized: `LogRow`, `ChatBubble`, `PluginCard`, `TimelineRow`, `ToolItem`, `LetterAvatar`, `PluginStatusDot`.
- `nameToHue` consolidated in `dashboard/src/lib/format.ts`; duplicate copies in `plugin-card` and `plugin-detail` removed.
- `apiFetch` helper centralizes Bearer-header injection; `token-entry-modal` and `settings/export-import` no longer call raw `fetch()`.

### Fixed

- `bridge/manager`: `defaultdict(asyncio.Lock)` replaced with `LRULockDict(maxsize=500)` — long-running daemons no longer leak locks.
- Hardcoded timeouts / thresholds across cognitive/, brain/, voice/ now route through `EngineConfig.tuning`.
- Dashboard `CommandDialog` (shadcn/ui) wasn't wrapping children in `<Command>` — caused cmdk internals to crash on render in tests; fixed.
- Dashboard tests for `chat-thread` / `cognitive-timeline` adapted to virtualized rendering (setup.ts now stubs `offsetWidth/Height` and fires ResizeObserver synchronously).

### Tests

- Backend: ~7 820 tests on Python 3.11 and 3.12 matrix.
- Dashboard: 767 vitest tests (was 676 pre-v0.11).
- Every quality gate green on `sovyx-4core` runners: `uv lock --check`, ruff, ruff format, mypy strict, bandit, pytest, vitest, `tsc -b`.

## [0.10.1] — 2026-04-13

### Fixed

- Plugin manager: handle `PluginStateChanged` serialization edge case when an auto-disabled plugin emits during teardown.
- Cognitive: `safety_classifier` cache eviction under high fan-in.

## [0.10.0] — 2026-04-13

### Added

- **Web Intelligence plugin** (6 tools — `search`, `fetch`, `research`, `lookup`, `learn_from_web`, `recall_web`). Three backends: DuckDuckGo (no key), SearXNG (self-hosted), Brave (API key). Intent-adaptive cache, source credibility tiers, SSRF protection, per-tool rate limits. 224 tests (200 unit + 24 Hypothesis).
- **Financial Math plugin** — 9 Decimal-native tools (`calculate`, `percentage`, `interest`, `tvm`, `amortization`, `portfolio`, `position_size`, `currency`). Banker's rounding, 28-digit precision, zero external deps. 228 tests.

### Changed

- `CalculatorPlugin` is now a backward-compatibility wrapper over `FinancialMathPlugin.calculate`.

## [0.9.0] — 2026-04-12

### Added — Knowledge plugin v2.0

- **Semantic deduplication** — cosine similarity ≥ 0.88 detects near-duplicates.
- **LLM-assisted conflict resolution** — classifies as SAME / EXTENDS / CONTRADICTS / UNRELATED.
- **Confidence reinforcement** — "established" status after 5+ confirmations.
- **Auto-relation creation** — new concepts linked to related existing concepts (similarity 0.65–0.87).
- **Episode-aware recall** — `recall_about()` enriches results with conversation history.
- **Person-scoped memory** — `remember(about_person="X")` and `search(about_person="X")`.
- **Real forget with cascade** — deletes concept + relations + embeddings + working memory; emits `ConceptForgotten`.
- **Structured JSON output** — all 5 tools return `{action, ok, message, ...}`.
- **Rate limiting** — sliding window: 30 writes/min, 60 reads/min.
- `BrainAccess` API: `classify_content`, `reinforce`, `create_relation`, `boost_importance`, `get_stats`, `get_top_concepts`, `forget_all`.

### Tests

- 659 plugin tests (unit + integration + contract + E2E).

## [0.8.2] — 2026-04-11

### Fixed

- ReAct loop: sanitize tool function names in re-invocation messages — OpenAI requires `^[a-zA-Z0-9_-]+$` but Sovyx uses dots (`calculator.calculate`). Now properly converts to `calculator--calculate` before sending back.

## [0.8.1] — 2026-04-11

### Fixed

- ReAct loop: tool re-invocation now includes `tool_calls` on assistant message and `tool_call_id` on tool results — fixes OpenAI 400 that caused raw fallback output.
- Plugin detail panel redesign: proper spacing, sections in cards, labeled action buttons, collapse animations.
- Plugin card polish: larger badges, readable text (10 → 11 px), health warnings in styled cards.
- Cognitive timeline: scrollbar no longer overlaps right-aligned timestamps.
- Metric chart: `YAxis` width increased (40 → 52) so cost labels aren't clipped.

## [0.8.0] — 2026-04-11

### Added — Plugin dashboard

- `/plugins` page with grid layout, search, filters by status / category, real-time stats.
- `PluginCard` hero card (glass morphism, status badges, tool / permission indicators).
- Plugin Detail slide-over panel — description, version, author, permissions, tools, config.
- Reusable badge system — tools count, permission levels, category tags, pricing.
- Enable / disable / remove flow with confirmation dialogs + double-click guard.
- Permission Approval Dialog: users explicitly review and approve each permission before activation.
- `/api/plugins` REST endpoints with enriched data.
- Zustand plugin slice with optimistic updates + WebSocket sync.
- Engine-state awareness: distinguishes "plugin engine off" from "no plugins installed".

### Testing

- 25 contract tests (backend ↔ frontend type parity).
- 12 E2E tests through real `PluginManager` + FastAPI.
- 20 vitest plugin-slice tests.

## [0.7.1] — 2026-04-11

### Fixed — Plugin SDK deep validation

- `ImportGuard` PEP 451 (CRITICAL): replaced deprecated `find_module` with `find_spec` — runtime import guard now actually runs on Python 3.12+.
- Tool name separator `__` → `--` (manifests block consecutive hyphens; Python methods can't have hyphens).
- Disabled plugins now filtered from `get_tool_definitions()`.
- Empty `enabled` set no longer falls through to "load everything" via `or None`.
- `ThinkPhase` tools=[] normalized to `None` so providers don't receive empty tools arrays.
- Entry-points group alignment: `sovyx.plugins` everywhere (was split `sovyx_plugins` / `sovyx.plugins`).

### Added

- Marketplace manifest fields (`category`, `tags`, `icon_url`, `screenshots`, `pricing`, `price_usd`, `trial_days`).
- `PluginManager` wired into bootstrap — `load_all()` on startup, cleanup on shutdown.
- 72 new validation tests (VAL-001 … VAL-014).

## [0.7.0] — 2026-04-11

### Added — Plugin SDK

- `sovyx.plugins.sdk`: `ISovyxPlugin` ABC, `@tool` decorator, `ToolDefinition` schema.
- `sovyx.plugins.manager`: load, unload, execute, lifecycle with auto-disable on 5 consecutive failures.
- `sovyx.plugins.permissions`: capability-based (`network:internet`, `brain:read`, `fs:write`, …).
- `sovyx.plugins.sandbox_http` / `sandbox_fs`: domain-whitelisted HTTP + scoped filesystem.
- `sovyx.plugins.security`: AST scanner blocks `eval`, `exec`, `subprocess`, `__import__`; runtime `ImportGuard`.
- `sovyx.plugins.events`: `PluginLoaded`, `PluginUnloaded`, `PluginAutoDisabled`, `PluginToolExecuted`, `PluginStateChanged`.
- Plugin config whitelist / blacklist model in `mind.yaml`.
- LLM tool integration across all 4 providers (Anthropic, OpenAI, Google, Ollama).
- ReAct loop in `ActPhase`: LLM → tool_call → `PluginManager.execute()` → result → LLM re-invoke (max 3 iterations).
- `sovyx plugin` CLI: `list`, `info`, `install` (local / pip / git), `enable`, `disable`, `remove`, `create`, `validate`.
- Hot reload via `watchdog` for dev mode.
- Built-in plugins: Calculator, Weather (Open-Meteo), Knowledge.
- Testing harness: `MockPluginContext`, `MockBrainAccess`, `MockEventBus`, `MockHttpClient`, `MockFsAccess`.
- Plugin Developer Guide (docs).

### Tests

- 504 new plugin tests, 97.61 % coverage across plugin modules.

## [0.6.0] — 2026-04-10

### Added

- Financial Gate v2: language-agnostic with inline buttons + LLM fallback.

## [0.5.x] — 2026-04-06 … 2026-04-10

### Added

- Safety guardrails: enterprise multilingual safety system.
- Enterprise audit tooling (13-task compliance suite).
- Dashboard chat (`POST /api/chat` + `ChannelType.DASHBOARD`).
- `sovyx token` CLI command + startup banner.
- Welcome banner, channel status card, request-ID middleware.
- Dashboard build step + attack testing suite (74 security tests).
- `publish.yml` workflow with OIDC trusted publishing.
- Voice pipeline: wake word, Silero VAD, Moonshine STT, Piper + Kokoro TTS, Wyoming protocol.
- Dashboard: brain viz, conversations, logs, settings, system status, WebSocket live updates.
- Cloud backup: zero-knowledge encryption (Argon2id + AES-256-GCM) to Cloudflare R2, Stripe billing.
- Signal channel via signal-cli-rest-api.
- Observability: SLO monitoring, Prometheus `/metrics`, structured logging, cost tracking.
- Zero-downtime upgrades: blue-green with automatic rollback, schema migrations.
- Performance benchmarks: hardware-tier budgets (Pi 5, N100, GPU).
- Security headers middleware, timing-safe token auth.

### Changed

- `__version__` derived from `importlib.metadata`.

## [0.1.0] — 2026-04-03

### Added

- Cognitive Loop (Perceive → Attend → Think → Act → Reflect).
- Brain system: concept / episode / relation storage in SQLite + `sqlite-vec`.
- Working memory with activation-based geometric decay.
- Spreading activation (multi-hop retrieval).
- Hebbian learning (co-occurrence strengthening).
- Ebbinghaus decay with rehearsal reinforcement.
- Hybrid retrieval: RRF fusion of FTS5 + vector KNN.
- Memory consolidation (scheduled decay + pruning).
- Personality engine (OCEAN model).
- Context assembly with Lost-in-Middle ordering (Liu et al. 2023).
- LLM router: multi-provider failover + circuit breaker (Anthropic, OpenAI, Ollama).
- Cost guard: per-conversation and daily USD budgets.
- Telegram channel (`aiogram` 3.x with exponential-backoff reconnect).
- Person resolver, conversation tracker (30-min timeout, 50-turn history).
- CLI (`init` / `start` / `stop` / `status` / `doctor` / `brain` / `mind`) with Typer + Rich.
- Daemon: JSON-RPC 2.0 over Unix socket.
- Lifecycle manager: PID lock, SIGTERM / SIGINT graceful shutdown, `sd_notify`.
- Health checker: 10 concurrent checks.
- Service registry, event bus, Docker multi-stage build, systemd unit file.

### Tests

- 1 138 tests, ≥ 95 % coverage, mypy strict, ruff, bandit — zero errors.
- Python 3.11 + 3.12 CI matrix.

[Unreleased]: https://github.com/sovyx-ai/sovyx/compare/v0.16.12...HEAD
[0.16.12]: https://github.com/sovyx-ai/sovyx/compare/v0.16.11...v0.16.12
[0.11.9]: https://github.com/sovyx-ai/sovyx/compare/v0.11.8...v0.11.9
[0.11.8]: https://github.com/sovyx-ai/sovyx/compare/v0.11.7...v0.11.8
[0.11.7]: https://github.com/sovyx-ai/sovyx/compare/v0.11.6...v0.11.7
[0.11.6]: https://github.com/sovyx-ai/sovyx/compare/v0.11.5...v0.11.6
[0.11.5]: https://github.com/sovyx-ai/sovyx/compare/v0.11.4...v0.11.5
[0.11.4]: https://github.com/sovyx-ai/sovyx/compare/v0.11.3...v0.11.4
[0.11.3]: https://github.com/sovyx-ai/sovyx/compare/v0.11.2...v0.11.3
[0.11.2]: https://github.com/sovyx-ai/sovyx/compare/v0.11.1...v0.11.2
[0.11.1]: https://github.com/sovyx-ai/sovyx/compare/v0.11.0...v0.11.1
[0.11.0]: https://github.com/sovyx-ai/sovyx/compare/v0.10.1...v0.11.0
[0.10.1]: https://github.com/sovyx-ai/sovyx/compare/v0.10.0...v0.10.1
[0.10.0]: https://github.com/sovyx-ai/sovyx/compare/v0.9.0...v0.10.0
[0.9.0]: https://github.com/sovyx-ai/sovyx/compare/v0.8.2...v0.9.0
[0.8.2]: https://github.com/sovyx-ai/sovyx/compare/v0.8.1...v0.8.2
[0.8.1]: https://github.com/sovyx-ai/sovyx/compare/v0.8.0...v0.8.1
[0.8.0]: https://github.com/sovyx-ai/sovyx/compare/v0.7.1...v0.8.0
[0.7.1]: https://github.com/sovyx-ai/sovyx/compare/v0.7.0...v0.7.1
[0.7.0]: https://github.com/sovyx-ai/sovyx/compare/v0.6.0...v0.7.0
[0.6.0]: https://github.com/sovyx-ai/sovyx/compare/v0.5.40...v0.6.0
[0.1.0]: https://github.com/sovyx-ai/sovyx/releases/tag/v0.1.0
