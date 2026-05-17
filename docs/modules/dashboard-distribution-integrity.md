# Dashboard distribution integrity (Mission C5)

Sovyx ships its React dashboard as a Vite-built SPA bundle baked into
the wheel under `sovyx/dashboard/static/`. Operators install Sovyx via
`pipx`, which unpacks the wheel into a virtualenv on disk. **Anything
that interferes with that unpack** — a partial pipx install, an antivirus
quarantine of a JavaScript chunk, a disk-full event mid-extraction —
can leave the installed bundle internally inconsistent: `index.html`
references a chunk that isn't on disk, the browser 404s on the missing
chunk, no JavaScript loads, the dashboard renders as a blank page.

Mission C5 closes this distribution-integrity gap with three independent
gates:

| Gate | When | Where | Failure mode it catches |
|---|---|---|---|
| Build-time AST scan (Quality Gate 11) | Every `git push`, every `publish.yml` run | `scripts/dev/check_dashboard_bundle_integrity.py` (local LENIENT v0.47.x → STRICT v0.48.0) + `publish.yml` post-build (STRICT from v0.47.0) | The wheel that publishes to PyPI references a chunk not packed in the wheel |
| Install-time boot probe | Every `create_app()` boot (every `sovyx start`) | `dashboard/server.py::create_app()` — four-state classifier | The wheel was clean at publish but the pipx unpack on this machine produced a partial install |
| Runtime reactive arm | Every `/assets/*` 404 | `_IntegrityAwareStaticFiles.get_response()` — debounced re-scan | The bundle was clean at boot but a file was deleted / quarantined mid-daemon |

All three gates feed into the **C4 composite-banner store** as a new
`axis="dashboard"` entry. Operators see the degraded state alongside
voice / LLM / STT axes in a single dashboard banner; CLI-only operators
see it via `sovyx doctor` or `sovyx dashboard doctor`.

## Verdict taxonomy

Every scan returns one of five categorical verdicts (`BundleVerdict`
StrEnum at `sovyx.dashboard._integrity`):

| Verdict | Meaning | Severity at the C4 composite store |
|---|---|---|
| `FULLY_PRESENT` | Every referenced asset is present on disk | (axis cleared) |
| `PARTIAL` | Some referenced chunks are absent on disk | `error` |
| `INDEX_HTML_MISSING` | `index.html` is absent (legacy `dashboard_static_missing` case) | `critical` |
| `STATIC_DIR_MISSING` | The `static/` directory itself is absent | `critical` |
| `LEGACY_INDEX_HTML_NO_ASSETS` | `index.html` exists but the `assets/` directory is missing or empty | `critical` |

The composite banner combines all axes' severities per ADR-D6
(1 axis = warn / 2 = error / 3+ = critical), so a partial dashboard
plus an exhausted voice ladder plus a coerced STT compounds to a
red-pulse critical banner with operator-action chips for each axis.

## Operator playbooks

### `sovyx dashboard doctor`

Prints the current bundle verdict + missing-chunk list + remediation
hint. Exit code 0 on `FULLY_PRESENT`; non-zero otherwise.

```
$ sovyx dashboard doctor
Sovyx Dashboard — bundle integrity
  ✓  FULLY_PRESENT  (42 refs, 4.2ms)
```

JSON mode for tooling:

```
$ sovyx dashboard doctor --json | jq '.verdict'
"fully_present"
```

### `sovyx doctor`

The aggregate doctor command renders the dashboard integrity surface
alongside the voice quarantine / failover history / degraded banner
surfaces. CLI-only operators get the same picture as the dashboard
banner from one command.

## Configuration knobs

All under `EngineConfig.tuning.dashboard` (env prefix
`SOVYX_TUNING__DASHBOARD__`):

| Knob | Default | Bounds | Effect |
|---|---|---|---|
| `integrity_reactive_enabled` | `True` | bool | Reactive on-404 arm kill-switch. Default ON per anti-pattern #34 inverse — always-on observability. |
| `integrity_reactive_debounce_sec` | `60.0` | `[10, 600]` | Debounce window for the reactive arm. Lower bound prevents thrash under H8-class polling failures; upper bound caps the worst-case silence between a chunk going missing and the banner re-surfacing. |
| `integrity_action_chip_reinstall_url` | `https://sovyx.dev/docs/install/troubleshooting#reinstall` | string | Operator-action chip target. Override for self-hosted docs. |
| `integrity_action_chip_doctor_url` | `https://sovyx.dev/docs/cli/doctor#dashboard` | string | Operator-action chip target — sovyx doctor dashboard reference. |

## Triage workflow

When the dashboard banner shows the `dashboard` axis (or when
`sovyx doctor` reports a non-`FULLY_PRESENT` verdict):

1. Run `sovyx dashboard doctor` for the full list of missing chunks.
2. If verdict is `PARTIAL` or `INDEX_HTML_MISSING` and Sovyx was
   pipx-installed: run `pipx reinstall sovyx`. The fresh install
   should restore the full bundle.
3. If the operator is running from a checkout (developer mode): run
   `npm run build` inside `dashboard/` to regenerate the static
   bundle.
4. Restart `sovyx start`. The boot scan re-emits
   `dashboard.distribution.bundle_scanned{verdict=fully_present}`
   and the composite banner clears the `dashboard` axis.

## Telemetry events

See [`docs/modules/voice-otel-semconv.md`](voice-otel-semconv.md)
for the full event catalog. Operator-actionable events under the
`dashboard.distribution.*` namespace:

* `dashboard.distribution.bundle_scanned` — INFO; every boot scan.
  Fields: `verdict`, `static_dir`, `referenced_count`, `missing_count`,
  `scan_duration_ms`.
* `dashboard.distribution.bundle_partial` — WARN; chunks absent on
  disk.
* `dashboard.distribution.bundle_missing` — WARN; index.html or
  static/ missing.
* `dashboard.distribution.reactive_rescan_healthy` — INFO; reactive
  arm fired and confirmed the bundle is now whole.
* `dashboard.distribution.reactive_rescan_degraded` — INFO; reactive
  arm fired and confirmed the bundle is still degraded.

The legacy `dashboard_static_missing` WARN is preserved during the
v0.47.x cycle (ADR-D14 dual-emission) and dropped at v0.48.0 STRICT
flip.

## See also

* `MISSION-c5-dashboard-distribution-integrity-2026-05-17.md` — full mission spec.
* Anti-pattern #43 (added at v0.48.0 STRICT flip) — triple-gate
  distribution integrity discipline as a general design rule.
* C4 composite-banner spec — the substrate this mission slots into.
