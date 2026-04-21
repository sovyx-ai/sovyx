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

| Requirement | Article | Sovyx feature | Status (v0.21.0) |
|---|---|---|---|
| **Right of access** — the data subject can obtain a copy of their data | LGPD Art. 18 I; GDPR Art. 15 | `GET /api/conversations/export?mind_id=X` (existing) plus `GET /api/logs/saga?user_id=X` (planned, post-release) | **Partial** — saga endpoint scheduled |
| **Right to erasure** — the data subject can demand deletion | LGPD Art. 18 VI; GDPR Art. 17 | `sovyx data wipe --mind=X` CLI (existing); `var/log/` redact-by-mind_id is scheduled | **Partial** — log-side erasure scheduled |
| **Data minimisation** — only collect what is necessary | LGPD Art. 6 III | `PIIRedactor` processor + sampling + four verbosity modes (`raw`, `redacted`, `hashed`, `dropped`) | **Implemented** |
| **Anonymisation** — break the link between record and subject | LGPD Art. 12 | `hashed` mode keeps the record forensically useful (same hash for the same input within a rotation key) without storing the value itself | **Implemented** |
| **Audit trail of access / config changes** | LGPD Art. 37 | Dedicated `audit/` log (audit.jsonl) with config mutations, license events, plugin permission changes; tamper-evident chain when `tamper_chain` is on; boot + rotation + on-demand chain verification | **Implemented** |
| **Incident notification (72 h)** | GDPR Art. 33 | `AlertManager` emits `security.incident.detected` to a configurable webhook sink so the operator pipeline can dispatch the formal notification | **Partial** — sink configuration UI scheduled |
| **Tamper evidence (integrity)** | LGPD Art. 6 VI; GDPR Art. 32 | `HashChainHandler` over both the main log and audit.jsonl; `verify_chain` runs at boot, before each rotation, and on demand via `sovyx audit verify-chain` | **Implemented** |
| **Portability** — common, machine-readable export format | LGPD Art. 18 V; GDPR Art. 20 | JSONL is the wire format end-to-end; the export endpoints preserve the same schema operators see in `var/log/` | **Implemented** |
| **Encryption in transit** | LGPD Art. 46; GDPR Art. 32 | OTLP exporter requires TLS 1.3+; the dashboard expects HTTPS termination at the operator's reverse proxy (TLS gate inside Sovyx is scheduled) | **Partial** — internal TLS gate scheduled |
| **Explicit retention policy** | LGPD Art. 16 | `LoggingConfig.retention_days` plus `RotatingFileHandler` size + count budget plus `LogJanitor` emergency prune on disk pressure | **Implemented** |

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
# Delete every persisted artefact tied to a mind: episodes, concepts,
# embeddings, conversations, voice transcripts.
sovyx data wipe --mind="$MIND_ID" --confirm
```

The CLI prompts for confirmation; the controller is expected to log
the request reference (ticket id, signed letter, …) in their own
records. Log-side erasure (`var/log/`) is on the post-v0.21.0
roadmap; until then, the operator scrubs by re-rotating logs past
the retention horizon.

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

Each of those has a corresponding entry in the public roadmap
(`docs/roadmap.md`).
