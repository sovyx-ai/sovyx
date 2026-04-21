# Runbook — Observability rollback (v0.21.0)

This runbook covers what to do when v0.21.0 — the
IMPL-OBSERVABILITY-001 release — destabilises a deployment. It
follows the §26 procedure from the implementation plan and is the
single source of truth operators should consult during an incident.

There are two rollback paths: **soft** (a single feature flag is
flipped, daemon stays on the new version) and **hard** (downgrade to
v0.20.4). Always try soft first — it is faster, preserves the boot-
time observability stack, and avoids touching the SQLite migrations.

> **Disposition note.** Every behavioural change in v0.21.0 sits
> behind a flag in `EngineConfig.observability.features`. That is
> not accidental — it is design principle 5 of the IMPL plan. If a
> behaviour is misbehaving and you cannot find its flag, the bug is
> in the wiring, not in the procedure.

---

## When to roll back

Rollback is justified when **any two** of the following are observed
within a single one-hour window:

* Daemon crash loop — more than three restarts in 30 minutes.
* Voice cold-start p99 regressed by more than 50% against the
  v0.20.4 baseline (`docs-internal/_meta/perf-baseline.json`).
* Brain throughput regressed by more than 30% against the v0.20.4
  baseline.
* Ten or more user-reported issues filed against the release on
  GitHub in 24 h.
* A critical security alert from `AlertManager` (e.g.
  `audit.chain.broken`, `security.incident.detected`).

A single signal is *not* a rollback trigger — investigate first. If
the daemon is alive but a single feature is misbehaving, prefer to
disable that feature (soft rollback) over downgrading the whole
release.

---

## 1. Soft rollback — flip a feature flag

Soft rollback disables one or more observability features without
downgrading the daemon. Every feature is flagged in
`ObservabilityFeaturesConfig`; the env-var prefix is
`SOVYX_OBSERVABILITY__FEATURES__`.

### 1.1 Identify the misbehaving feature

The candidate flags map roughly to the IMPL phases:

| Symptom | Likely flag |
|---|---|
| Saga propagation produces noisy `cause_id` chains | `SAGA_PROPAGATION` |
| Voice telemetry floods the log file | `VOICE_TELEMETRY` |
| Plugin permission audit blocks legitimate calls | `PLUGIN_INTROSPECTION` |
| `audit.chain.broken` after rotation, false positives | `TAMPER_CHAIN` |
| OTLP exporter retries flood the network | `OTLP_EXPORTER` |
| Anomaly detector raises spurious alerts | `ANOMALY_DETECTION` |
| Startup cascade slows boot beyond the 5 s budget | `STARTUP_CASCADE` |
| Hot-path counter snapshots load CPU | (no flag — file an incident; this should never happen) |

`/api/observability/features/active` returns the live state of every
flag; consult it before and after the change.

### 1.2 Flip the flag

```bash
# Example: disable the tamper chain because rotation produced a
# false-positive broken-chain warning.
export SOVYX_OBSERVABILITY__FEATURES__TAMPER_CHAIN=false

# Restart so the new env var is read.
sovyx restart           # or: systemctl restart sovyx
```

### 1.3 Confirm

```bash
curl -H "Authorization: Bearer $TOKEN" \
     https://sovyx.local/api/observability/features/active \
     | jq '.tamper_chain'
# expected: false
```

If the daemon stabilises within five minutes, soft rollback
succeeded. File the incident report (§3 below) and stop here.

---

## 2. Hard rollback — downgrade to v0.20.4

Only used when soft rollback failed to stabilise the daemon, or when
the symptom cannot be tied to a single flag.

### 2.1 Back up live state

```bash
cp -r ~/.sovyx/data       ~/.sovyx/data.v0.21.0.backup
cp -r ~/.sovyx/var/log    ~/.sovyx/var/log.v0.21.0.backup
cp -r ~/.sovyx/audit      ~/.sovyx/audit.v0.21.0.backup
```

The audit directory backup is critical — it is the evidence trail
for the failed deployment and must survive the downgrade.

### 2.2 Downgrade the package

```bash
pip install --force-reinstall sovyx==0.20.4
# or, with uv:
uv pip install --reinstall sovyx==0.20.4
```

### 2.3 Schema considerations

v0.21.0 only adds indexes and nullable columns to the SQLite store —
it does not break the schema for v0.20.4. Migrations are idempotent
in both directions; no manual revert is required.

```bash
sovyx doctor   # version → 0.20.4; database opens cleanly
```

### 2.4 Configuration cleanup

`pydantic-settings` is configured with `extra="ignore"`, so
v0.21.0-specific keys (`observability.*`, `security.secrets_rotated_at`,
…) in `system.yaml` are silently dropped by v0.20.4. **No manual
cleanup is needed** — but if you run `sovyx config show` and notice
the unknown keys, leaving them in place is fine; they will become
active again on the next upgrade.

### 2.5 Restart and verify

```bash
sovyx start
sovyx logs --tail 50  # expect normal v0.20.4 boot lines
```

If voice / brain are part of the deployment, run their respective
smoke probes:

```bash
sovyx doctor voice      # PortAudio enumeration + APO scan
sovyx doctor brain      # ONNX load + episode round-trip
```

---

## 3. Post-mortem (mandatory after any rollback)

Every rollback — soft or hard — is followed by a structured post-
mortem. The output lives in three places so future operators inherit
the lesson.

1. **Incident note** at
   `docs-internal/incidents/INC-YYYYMMDD-<short-slug>.md` — timeline,
   root cause, mitigation, follow-ups.
2. **Risk update** in `IMPL-OBSERVABILITY-001-sistema-logs-surreal.md`
   §20 (Riscos) — append a new row with the failure mode the rollback
   surfaced.
3. **Auto-memory entry** under category `feedback`:
   `rollback v0.21.0 — investigate <X> before re-deploy`. The next
   conversation will see the entry in `MEMORY.md` and refuse to ship
   the offending feature again without a regression test.

---

## 4. Re-deploying after a rollback

A re-deploy is only allowed once **all three** of the following are
true:

* The post-mortem (above) is committed.
* A regression test exercising the failure mode is committed under
  `tests/` and is part of the green CI run.
* The originally tripped feature flag stays **off** in the new
  deployment until the regression test has been observed green for
  at least one full release cycle.

The third bullet is what keeps the rollback from being purely
performative — re-enabling the flag at re-deploy time is the most
common way to relive the same incident a week later.

---

## Cross-references

* `docs/observability.md` — full event catalogue; the events
  flagged in §1.1 above all appear there with payload schemas.
* `docs/configuration.md` — every observability flag with its
  default value and env-var spelling.
* `docs/compliance.md` — relevant when the failure mode involves
  audit-chain integrity.
* `docs-internal/plans/IMPL-OBSERVABILITY-001-sistema-logs-surreal.md`
  §26 — authoritative spec this runbook implements.
