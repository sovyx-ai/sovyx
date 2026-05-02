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

---

## Self-assessment summary

> Phase 7 / T7.48. Last reviewed 2026-05-02 against the v0.30.0
> single-mind GA candidate.

This section is the formal pass/fail summary per regime. The
matrix above is the line-item evidence; this is the
roll-up that an operator's compliance officer signs before deployment.

### GDPR (EU 2016/679) — **Pass with documented operator obligations**

| Article | Sovyx posture | Status |
|---|---|---|
| Art. 5(1)(a) — lawful, fair, transparent processing | Operator-side policy + Sovyx audit log records the boundary at every TRANSCRIBE/STORE/SHARE event. | **Operator** owns lawful-basis declaration |
| Art. 5(1)(c) — data minimisation | `PIIRedactor` + four log verbosity modes + voice subsystem persists no raw audio by default | **Implemented** |
| Art. 5(1)(e) — storage limitation | `LoggingConfig.retention_days` (logs) + `MindConfig.retention.*` (per-mind data) + `voice_audio_retention_days` | **Implemented** (Phase 8 T8.21 step 6) |
| Art. 15 — Right of access | `GET /api/conversations/export?mind_id=X` + `sovyx voice history` | **Implemented** |
| Art. 17 — Right to erasure | `sovyx mind forget` CLI + `POST /api/mind/{mind_id}/forget` | **Implemented** (Phase 8 T8.21 steps 4-5) |
| Art. 20 — Portability | JSONL export end-to-end | **Implemented** |
| Art. 30 — Records of processing | Audit log + ConsentLedger (per-user + per-mind) | **Implemented** |
| Art. 32 — Security of processing | Hash-chain integrity + plugin sandbox + zero-trust posture | **Implemented** |
| Art. 33 — Breach notification (72h) | `AlertManager` security.incident.detected webhook | **Partial** — operator wires sink |

**Verdict:** GDPR-compliant for self-hosted deployments where the
operator owns the lawful-basis policy + DPA chain. Sovyx provides
the technical controls; the operator owns the Article 6 lawful-basis
declaration and any Article 13/14 transparency notice.

### LGPD (Brasil 13.709/2018) — **Pass with documented operator obligations**

LGPD parallels GDPR for the technical-controls subset (Art. 18 I/V/VI
mirror GDPR Art. 15/20/17). Article 16 storage-limitation is
satisfied by the same retention infrastructure as GDPR Art. 5(1)(e).
Article 37 audit trail is satisfied by the same audit log. **Same
verdict as GDPR.**

### CCPA / CPRA (California) — **Pass for self-hosted; Cloud-LLM operators add DPA**

* Right to know (Sec. 1798.110): `sovyx voice history` + conversation
  export — **Implemented**.
* Right to delete (Sec. 1798.105): `sovyx mind forget` — **Implemented**.
* Right to opt out of sale: Sovyx never sells data — **N/A by design**.

**Operator obligation:** when wiring a cloud LLM provider, the
operator MUST sign a DPA with the provider that prohibits training
on operator data (cf. OpenAI DPA, Anthropic DPA, Google Cloud DPA).

### BIPA (Illinois 740 ILCS 14) — **Pass with explicit opt-in**

Voice biometrics (voiceprint / speaker-ID) are **off by default**.
`voice_biometric_processing_enabled = False` ships as the safe
posture. When operators flip the flag to enable Phase 8 multi-mind
voice ID, they MUST capture written consent from each enrolled
speaker per BIPA §15. Sovyx provides the technical control + audit
trail (every SHARE event involving biometric features is logged);
the legal-basis chain is the operator's responsibility.

### HIPAA (US healthcare) — **Forward-compatible flag; not yet active**

`EngineConfig.compliance.hipaa_mode: bool = False` ships as a
forward-compat flag (Phase 8 T8.21 step 1). When future minor
cycles wire its effects (HIPAA-minimum retention horizons + HMAC
chain on ConsentLedger + mandatory encryption-at-rest), operators
deploying in healthcare contexts will flip it to `True`. As of
v0.30.0 the flag is reserved schema only.

**Until HIPAA mode is active**, Sovyx is **not** marketed as
HIPAA-compliant. Healthcare deployments should defer to a future
release or implement compensating controls (operator-side
encryption-at-rest, custom retention horizons).

### Aggregate verdict

* **Self-hosted, non-healthcare deployment** (typical Sovyx user):
  GDPR + LGPD + CCPA/CPRA + BIPA technical controls all pass at
  the v0.30.0 GA candidate point. Operator owns the lawful-basis
  + DPA chain.
* **Healthcare deployment**: defer; HIPAA mode active flag wires
  in v0.31.x patches per the master mission roadmap.

This summary is **not** a legal opinion. Operators should review
the line-item matrix above with their counsel and document the
verdict in their compliance file before deployment.
