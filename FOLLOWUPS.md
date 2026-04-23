# FOLLOWUPS — SVX-VOICE-LINUX-20260422 execution

Items identified during execution of IMPLEMENTATION_PLAN.md v1.3 that
were deliberately **not** implemented inside the 8 PRs. Each entry
includes the rationale for deferral so a future maintainer understands
whether the gap is intentional or a tracking debt.

## F1 — WebSocket toast handler for `voice_preflight_warning`

**Where registered:** plan §5 matrix (`dashboard/src/stores/slices/voice.ts`
— "WS handler | ~20 LOC"), plan §4.6.5 WS push.

**Status:** backend publishes the WS event via
`ws_manager.broadcast(channel="voice", event="voice_preflight_warning", ...)`
from `routes/voice.enable_voice`. Frontend currently consumes via the
`/api/voice/status.preflight_warnings` poll + the
`LinuxMicGainCard` self-poll of `/api/voice/linux-mixer-diagnostics`.

**Why deferred:** the repo has no existing WS event-router abstraction
on the frontend; the existing `use-audio-level-stream` hook is a
single-stream consumer. Wiring a general subscription registry for one
extra event type is out-of-proportion. The redundant poll-based surface
covers the user-visible case (§4.7.5 interaction matrix already
documents the redundancy as acceptable).

**Unblock trigger:** the moment a second broadcast event lands, build
the router once, subscribe both events.

## F2 — pt-BR i18n locale

**Where registered:** plan §5 matrix + CLAUDE.md
(`dashboard/public/locales/{en,pt-BR}/voice.json`).

**Status:** repo currently has only `dashboard/src/locales/en/` — no
`pt-BR/`. Added keys live under `en/voice.json` only.

**Why deferred:** creating `pt-BR/` is a whole-dashboard i18n task, not
scoped to v1.3. Adding a single partial `pt-BR/voice.json` with only
the linuxMicGain keys would produce an inconsistent locale that falls
back to English for every other surface — worse UX than English-only.

**Unblock trigger:** separate i18n initiative adds the pt-BR locale;
copy the linuxMicGain keys then.

## F3 — Plan §17.1 — `sovyx doctor voice` (no `--fix`) × stale marker

**Where registered:** plan §17.1 (considerações pós-execução).

**Status:** not implemented; plan explicitly marked it as "NÃO
implementar em v1.3".

**Rationale carried forward:** adding marker-clearing semantics to the
read-only `doctor voice` (no `--fix`) command introduces a side effect
on a command that the user reasonably expects to be diagnostic-only.
A less-invasive alternative would be a warning line in the doctor
output when the marker exists but step 9 passes ("marker indicates a
prior saturated boot; the next boot will clear it").

**Unblock trigger:** a user report surfaces this inconsistency in
practice. Until then it's a cosmetic nit R13 telemetry already tracks.

## F4 — Plan §17.4 — GitHub Actions cron for §14.G empirical reviews

**Where registered:** plan §14.G + §17.4.

**Status:** not implemented; governance out-of-scope for v1.3.

**Rationale:** the cron that polls issues with label
`empirical-review` and fires reminders on overdue due-dates is pure
governance — no production impact if missing. Current process: the
release checklist item "empirical reviews status" catches lapses
manually.

**Unblock trigger:** release cadence establishes that manual catches
are unreliable — file a dedicated governance PR at that point.

## F5 — 12 pre-existing test failures on `main` (not introduced by v1.3)

Full-suite pytest surfaced 14 failures total; 2 were introduced by v1.3
and fixed in the same execution (`test_voice_status::test_returns_all_expected_sections`
updated to include the new ``preflight_warnings`` key). The remaining
**12 failures are pre-existing** and unrelated to this work — every
failing test lives in modules v1.3 did not touch:

| Test | Module | Probable cause |
|---|---|---|
| `tests/dashboard/test_settings.py::test_returns_all_fields` | `dashboard/settings.py` | Windows path-separator drift (`\tmp` vs `/tmp`) |
| `tests/integration/dashboard/test_logs_e2e.py::test_query_logs_after_param_for_incremental` | `dashboard/routes/logs.py` | Integration test — unrelated to voice |
| `tests/plugins/test_sandbox_fs.py::test_symlink_escape_blocked` | `plugins/sandbox_fs.py` | Windows symlink permissions |
| `tests/unit/brain/test_consolidation_batching.py::test_timeout_stops_processing` | `brain/` | Flaky timing test |
| `tests/unit/cognitive/test_audit_store.py::test_flush_after_db_deleted` | `cognitive/` | Windows file-delete-while-open |
| `tests/unit/cognitive/test_safety_classifier.py::test_ttl_expiry` | `cognitive/safety/` | Flaky timing test |
| `tests/unit/engine/test_bootstrap.py::TestBootstrapOllamaAutoDetect::*` (4 tests) | `engine/bootstrap.py` | Ollama auto-detect not wired |
| `tests/unit/engine/test_bootstrap.py::test_loads_sovyx_prefixed_vars` | `engine/bootstrap.py` | Env var loading |
| `tests/unit/persistence/test_pool.py::test_find_extension_path_vec0_found` | `persistence/` | SQLite vec0 extension |
| `tests/unit/upgrade/test_importer.py::test_imports_full_smf` | `upgrade/` | Importer |

**Status:** none of these paths were modified by v1.3. Ruff + format +
mypy + bandit all pass globally; vitest + tsc all pass; Python test
suite is **9 577 passing / 12 pre-existing failures / 28 skipped /
57 deselected** on the same run that includes v1.3 changes.

**Unblock trigger:** a future maintainer diagnoses and fixes these in
a separate commit or series — they are orthogonal to
SVX-VOICE-LINUX-20260422.

## F6 — L-exec-4 real-hardware E2E gate

**Where registered:** plan §10 L-exec-4.

**Status:** release checklist `docs-internal/release_checklist_voice_recovery.md`
documents the manual validation; no automated gate.

**Why deferred:** CI has no Linux host with a saturating codec and no
safe way to mutate `amixer` on shared runners. Automation would
require a dedicated self-hosted runner with hardware fixtures.

**Unblock trigger:** the `sovyx-4core` self-hosted runner gains access
to a representative HDA card (or a USB-based codec fixture).
