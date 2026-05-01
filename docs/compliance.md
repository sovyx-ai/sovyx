# Compliance

Sovyx is designed to be deployable in environments subject to general
data-protection regulation — most directly Brazil's **LGPD** (Lei
13.709/2018) and the EU's **GDPR** (Regulation 2016/679). This page
maps the obligations both regimes impose on a controller / processor
to the concrete features Sovyx ships, the gaps that are scheduled but
not yet released, and the operator actions required to close the
remaining gaps.

This document is a **map, not a legal opinion**. It tells an operator
which Sovyx surface satisfies which article so that a compliance
review has somewhere to start. The regulations themselves remain the
authoritative source — Sovyx implements the technical controls; the
controller is responsible for policy, contracts (DPA / TIA), and
record-keeping.

---

## Compliance matrix

| Requirement | Article | Sovyx feature | Status |
|---|---|---|---|
| **Right of access** — the data subject can obtain a copy of their data | LGPD Art. 18 I; GDPR Art. 15 | `GET /api/conversations/export?mind_id=X` (existing) plus `GET /api/logs/saga?user_id=X` (planned, post-release) | **Partial** — saga endpoint scheduled |
| **Right to erasure** — the data subject can demand deletion | LGPD Art. 18 VI; GDPR Art. 17 | `sovyx mind forget <mind_id>` CLI + `POST /api/mind/{mind_id}/forget` dashboard endpoint (Phase 8 / T8.21); `var/log/` redact-by-mind_id is scheduled | **Implemented** — log-side erasure scheduled |
| **Data minimisation** — only collect what is necessary | LGPD Art. 6 III | `PIIRedactor` processor + sampling + four verbosity modes (`raw`, `redacted`, `hashed`, `dropped`) | **Implemented** |
| **Anonymisation** — break the link between record and subject | LGPD Art. 12 | `hashed` mode keeps the record forensically useful (same hash for the same input within a rotation key) without storing the value itself | **Implemented** |
| **Audit trail of access / config changes** | LGPD Art. 37 | Dedicated `audit/` log (audit.jsonl) with config mutations, license events, plugin permission changes; tamper-evident chain when `tamper_chain` is on; boot + rotation + on-demand chain verification. Voice surface: `ConsentLedger` (per-user + per-mind, append-only JSONL) for `WAKE/LISTEN/TRANSCRIBE/STORE/SHARE/DELETE/RETENTION_PURGE` events. | **Implemented** |
| **Incident notification (72 h)** | GDPR Art. 33 | `AlertManager` emits `security.incident.detected` to a configurable webhook sink so the operator pipeline can dispatch the formal notification | **Partial** — sink configuration UI scheduled |
| **Tamper evidence (integrity)** | LGPD Art. 6 VI; GDPR Art. 32 | `HashChainHandler` over both the main log and audit.jsonl; `verify_chain` runs at boot, before each rotation, and on demand via `sovyx audit verify-chain` | **Implemented** |
| **Portability** — common, machine-readable export format | LGPD Art. 18 V; GDPR Art. 20 | JSONL is the wire format end-to-end; the export endpoints preserve the same schema operators see in `var/log/` | **Implemented** |
| **Encryption in transit** | LGPD Art. 46; GDPR Art. 32 | OTLP exporter requires TLS 1.3+; the dashboard expects HTTPS termination at the operator's reverse proxy (TLS gate inside Sovyx is scheduled) | **Partial** — internal TLS gate scheduled |
| **Explicit retention policy — logs** | LGPD Art. 16 | `LoggingConfig.retention_days` plus `RotatingFileHandler` size + count budget plus `LogJanitor` emergency prune on disk pressure | **Implemented** |
| **Explicit retention policy — per-mind data** | LGPD Art. 16; GDPR Art. 5(1)(e) | `EngineConfig.tuning.retention.*` global defaults + `MindConfig.retention.*` per-mind overrides; CLI `sovyx mind retention prune\|status`; `POST /api/mind/{mind_id}/retention/prune`; auto-prune daemon scheduler (default off; opt in via `MindConfig.retention.auto_prune_enabled`); `RETENTION_PURGE` audit tombstone in ConsentLedger. Phase 8 / T8.21 step 6. | **Implemented** |

---

## Operator responsibilities

Sovyx implements the technical controls; the controller still owns
the policy decisions. Before a Sovyx deployment can be considered
compliant, the operator must:

1. **Document the lawful basis** for processing (LGPD Art. 7 / GDPR
   Art. 6). Sovyx records *what* is logged, not *why* the controller
   is allowed to log it.
2. **Sign a DPA / sub-processor contract** with any third-party LLM
   provider routed through `sovyx.llm.router` and any external sink
   wired into `AlertManager`. Sovyx does not enforce that the
   downstream party is contracted; the configuration UI lets the
   operator route data wherever they please.
3. **Configure retention** to match the lawful basis.
   `LoggingConfig.retention_days` defaults are operationally
   reasonable, not legally minimal. A consent-based deployment will
   want shorter retention than a contract-based one.
4. **Rotate secrets on schedule.** Sovyx emits
   `security.secrets.rotation_overdue` (WARNING) at boot when
   `EngineConfig.security.secrets_rotated_at` is older than
   `rotation_warn_days` (default 90). The operator stamps the
   timestamp after each rotation; the controller documents the
   procedure in their internal security policy.
5. **Verify the audit chain** before submitting any audit extract to
   a regulator or third party — `sovyx audit verify-chain` exits non-
   zero on any broken chain. A passing verification is the only
   evidence that the extract is integrity-protected.

---

## Subject-rights workflows

### Access (LGPD Art. 18 I; GDPR Art. 15)

```bash
# Export every conversation belonging to a given mind.
curl -H "Authorization: Bearer $TOKEN" \
     "https://sovyx.local/api/conversations/export?mind_id=$MIND_ID" \
     -o subject-export.jsonl
```

The export carries the same envelope shape as the live log so the
data subject can read either format with the same tooling.

### Erasure (LGPD Art. 18 VI; GDPR Art. 17)

```bash
# Delete every per-mind persisted artefact: concepts, episodes,
# relations, embeddings, conversation history, daily stats, and the
# voice consent ledger trail. Mind configuration is preserved, so the
# operator can re-onboard the mind without re-creating its config.
sovyx mind forget "$MIND_ID"
```

The CLI prompts for confirmation; pass `--yes` for scripted use and
`--dry-run` to preview counts before committing. The controller is
expected to log the request reference (ticket id, signed letter, …)
in their own records. Log-side erasure (`var/log/`) is on the
post-v0.21.0 roadmap; until then, the operator scrubs by re-rotating
logs past the retention horizon.

The same operation is exposed via the dashboard:

```http
POST /api/mind/{mind_id}/forget
Authorization: Bearer <token>
Content-Type: application/json

{"confirm": "<mind_id>", "dry_run": false}
```

Defense-in-depth: the `confirm` field MUST equal `mind_id` verbatim
(GitHub-style "type the name to delete" pattern) so a CSRF attack or
frontend bug cannot accidentally wipe a mind.

### Storage limitation (LGPD Art. 16; GDPR Art. 5(1)(e))

Per-mind retention horizons enforce automatic time-based pruning of
aged records. Configured via `EngineConfig.tuning.retention.*`
(global defaults) plus `MindConfig.retention.*` (per-mind overrides).
Default horizons:

| Surface             | Default | Rationale                                          |
| ------------------- | ------- | -------------------------------------------------- |
| Episodes            | 30 d    | Aligns with `LoggingConfig.retention_days` baseline |
| Conversations + turns | 30 d  | Same surface class as episodes                      |
| Daily stats         | 365 d   | No PII; longer historical horizon for cost/usage    |
| Consolidation log   | 90 d    | Quarterly diagnostic window                         |
| Consent ledger      | 0 d     | Infinite — GDPR Art. 30 records-of-processing      |

`0 = disabled / infinite`. Concepts + relations are NOT subject to
time-based retention here — they have their own importance-based
decay via `MindConfig.brain.forgetting_enabled` + `decay_rate`;
layering two policies on the same surface would double-delete.

Three invocation paths, all driving the same `MindRetentionService`:

```bash
# Manual prune with optional --dry-run preview
sovyx mind retention prune <mind_id> --dry-run
sovyx mind retention status <mind_id>

# Auto-prune via daemon scheduler (off by default; opt in per mind)
# Set MindConfig.retention.auto_prune_enabled = true in mind.yaml.
# Daemon then runs prune daily at MindConfig.retention.prune_time
# (default 03:00 in mind's timezone, after typical 02:00 dream_time).

# Dashboard endpoint (no `confirm` required — less destructive than forget)
POST /api/mind/{mind_id}/retention/prune
{"dry_run": true}
```

Audit trail: scheduled prunes write `ConsentAction.RETENTION_PURGE`
records to the consent ledger (distinct from operator-invoked
`DELETE` tombstones), with the cutoff timestamp recorded in
`context.before_cutoff_iso` for forensic reconstruction.

### Portability (LGPD Art. 18 V; GDPR Art. 20)

The same export endpoint above produces the portable artefact. JSONL
is intentionally the only format — a regulator that asks for "your
machine-readable export" gets exactly what the operator's own
analytics tooling consumes.

---

## Cross-references

* **`docs/security.md`** — threat model, plugin sandbox, network
  egress controls. Anything labelled "implemented" above relies on
  the security architecture documented there.
* **`docs/observability.md`** — full event catalogue, including the
  audit and incident events the matrix relies on. Operators wiring
  SIEM rules should map them against the canonical event names.
* **`docs/configuration.md`** — concrete config keys
  (`SOVYX_SECURITY__SECRETS_ROTATED_AT`, `LoggingConfig.retention_days`,
  `ObservabilityFeaturesConfig.tamper_chain`, …).

---

## Open items (post-v0.21.0)

Tracked roughly in priority order for the next compliance-focused
release:

* `GET /api/logs/saga?user_id=X` — subject-rights view that surfaces
  every log line tied to a subject without exposing the raw
  envelope.
* `var/log/` redact-by-mind_id during erasure.
* `AlertManager` sink configuration UI so operators can wire the
  72-hour notification webhook without editing YAML.
* Internal TLS gate for the dashboard so a misconfigured reverse
  proxy can no longer expose plain HTTP.
