# Voice subsystem — Privacy & compliance

This page is the operator-facing privacy story for the Sovyx voice
subsystem: what audio data Sovyx captures, how long it lives, where
it's stored, what biometric processing happens (none, by default),
and how the data lifecycle aligns with **GDPR** (EU 2016/679),
**LGPD** (Brazil 13.709/2018), **CCPA / CPRA** (California),
**BIPA** (Illinois Biometric Information Privacy Act), and **HIPAA**
(US healthcare) where applicable.

This document is a **map, not a legal opinion** — it tells operators
which Sovyx surface satisfies which obligation so a compliance
review has somewhere to start. The regulations themselves remain the
authoritative source.

> Phase 7 / T7.31 + T7.36 + T7.40 — voice-privacy posture
> documentation per master mission acceptance criteria.

---

## TL;DR — what Sovyx does NOT do

By design, Sovyx's local-first architecture means voice processing
runs entirely on the user's machine. Specifically:

* **No raw audio is persisted to disk.** The capture path is
  `mic → frames → STT → discard`. Audio frames live in memory only
  for the duration of the active utterance; the moment STT returns a
  transcript, the frame buffer is freed.
* **No voiceprint / speaker-ID derivation runs by default.**
  `voice_biometric_processing_enabled` defaults to `False`.
  Phase 8 multi-mind voice ID (per master mission) lights up
  speaker identification only when the operator explicitly opts in.
* **No audio leaves the machine** unless the operator wires a cloud
  STT engine. The default Moonshine + Piper stack is fully on-device.
* **No third-party trackers** in the voice subsystem. The only
  external IO from voice is the optional cloud STT/TTS engines the
  operator explicitly configures.

The "no audio persistence" architecture is the simplest GDPR-Art-5(1)(e)
("storage limitation") posture: we don't keep what we don't need.

---

## What Sovyx records (the consent ledger)

While raw audio is not persisted, Sovyx does record **privacy-relevant
events** in the
[`ConsentLedger`](https://github.com/sovyx-ai/sovyx/blob/main/src/sovyx/voice/_consent_ledger.py)
— an append-only JSONL file at `<data_dir>/voice/consent.jsonl`. Each
record is one of six action types:

| Action | When emitted | Why recorded |
|---|---|---|
| `WAKE` | Wake-word detector matched the trigger phrase | Audit trail of every "Sovyx is now listening" boundary |
| `LISTEN` | Audio capture started for an utterance | Records the RECORDING-state entry |
| `TRANSCRIBE` | STT produced a transcript | Records the speech→text conversion (no transcript content stored — just the event) |
| `STORE` | Transcript was persisted to long-term memory | Records the brain-write boundary |
| `SHARE` | Transcript was sent to an external service (cloud LLM, web tool) | Records the external-egress boundary; `context` names the destination |
| `DELETE` | Right-to-erasure invoked via CLI / dashboard | Tombstone left after a `forget` so the audit trail survives the deletion |

The ledger record format (one JSON object per line):

```json
{
  "timestamp_utc": "2026-05-01T12:34:56Z",
  "user_id": "<opaque hash>",
  "action": "wake",
  "context": {}
}
```

**No PII is stored** in the ledger — `user_id` is a stable opaque hash
the caller passes in (Sovyx never sees raw user names), `context` is
free-form caller metadata that's PII-rejected at append time (see
`_OBVIOUS_PII_KEYS` in `_consent_ledger.py`).

---

## Operator API surface — right-of-access & right-to-erasure

Two equivalent surfaces (CLI + dashboard) operate on the same ledger:

### Right of access — GDPR Art. 15 / LGPD Art. 18 I

```
sovyx voice history --user-id=<opaque-hash>
```

Output: chronological JSONL dump of every record for the user.
Pipe to `jq` or save to file:

```
sovyx voice history --user-id=u-12345 > history.jsonl
```

Dashboard equivalent: `GET /api/voice/forget` is the **erasure**
endpoint; the read-side history surface is exposed via the
operator-side CLI only (avoids the dashboard becoming a one-click
data-export channel without explicit auth narrative).

### Right to erasure — GDPR Art. 17 / LGPD Art. 18 VI

```
sovyx voice forget --user-id=<opaque-hash>
```

Or via dashboard:

```
POST /api/voice/forget
Authorization: Bearer <token>
Content-Type: application/json

{"user_id": "<opaque-hash>"}
```

Both paths:

1. Walk the active ledger segment + every rotated segment
2. Drop every record whose `user_id` matches
3. Append a single `DELETE` tombstone so the audit trail survives
4. Return the count of records purged

Both paths are **idempotent** — running twice is safe (the second
call finds no records to purge but still writes a fresh tombstone).

---

## Configuration knobs

These tuning flags live under `EngineConfig.tuning.voice` and
support the standard `SOVYX_TUNING__VOICE__*` env-var override:

| Knob | Default | Purpose | Article |
|---|---|---|---|
| `voice_audio_retention_days` | `0` | Days to retain raw audio recordings; `0` = no persistence (default) | GDPR Art. 5(1)(e) |
| `voice_biometric_processing_enabled` | `False` | Opt-in voiceprint / speaker-ID derivation | GDPR Art. 9(1) (special category); BIPA written-consent obligation |

Both knobs are **forward-compatible foundations** for features that
Sovyx may opt into later (operator-side accessibility recordings,
Phase 8 speaker identification). The defaults reflect the safest
posture: no persistence, no biometrics.

---

## Compliance matrix — voice-specific articles

| Requirement | Article | Sovyx posture | Status |
|---|---|---|---|
| **Storage limitation** | GDPR Art. 5(1)(e); LGPD Art. 16 | No raw audio persisted by default (`voice_audio_retention_days=0`) | **Implemented** |
| **Lawful basis for biometric processing** | GDPR Art. 9; BIPA §15 | `voice_biometric_processing_enabled=False` default; flip requires operator-side written consent capture (Sovyx provides the technical control + audit trail; the legal-basis chain is the operator's responsibility) | **Implemented** |
| **Right of access** | GDPR Art. 15; LGPD Art. 18 I | `sovyx voice history` CLI | **Implemented** |
| **Right to erasure** | GDPR Art. 17; LGPD Art. 18 VI | `sovyx voice forget` CLI + `POST /api/voice/forget` dashboard | **Implemented** |
| **Records of processing activities** | GDPR Art. 30 | ConsentLedger captures every WAKE / LISTEN / TRANSCRIBE / STORE / SHARE event | **Implemented** |
| **Audit trail** | LGPD Art. 37 | Tombstone DELETE record survives erasure; tamper-evident chain on the main audit log (see `docs/compliance.md` for the cross-subsystem chain) | **Implemented** |
| **Encryption at rest** | GDPR Art. 32 | The only voice-related on-disk state is the ConsentLedger (event log; no transcripts, no audio). Filesystem-level encryption (LUKS / BitLocker / FileVault) at the operator's OS layer is the canonical defense — Sovyx does not implement application-layer encryption for the ledger because the threat model is "device theft", which the OS layer addresses uniformly across all Sovyx data files. | **Operator-managed** |
| **Encryption in transit** | GDPR Art. 32 | Cloud STT/TTS engines (when configured) require TLS 1.3+; the dashboard expects HTTPS at the operator's reverse proxy | **Implemented** (when cloud engines are in use) |

---

## CCPA / CPRA — California

The CCPA / CPRA additions to the GDPR baseline:

* **Right to know** — covered by `sovyx voice history`.
* **Right to delete** — covered by `sovyx voice forget`.
* **Right to correct** — N/A. ConsentLedger records EVENTS not
  user-supplied data; the records aren't subject to correction.
* **Right to opt-out of sale** — Sovyx never sells voice data. The
  `SHARE` action only fires when the operator explicitly wires a
  cloud LLM/tool, in which case the destination is named in
  `context` and the operator's DPA with the cloud provider governs
  re-use.

---

## BIPA — Illinois Biometric Information Privacy Act

BIPA imposes additional obligations when **biometric identifiers**
(voiceprints, fingerprints, face geometry) are collected:

* **Written informed consent** before any collection
* **Public retention + destruction policy**
* **No sale or disclosure of biometric data**
* **Reasonable standard of care** for storage and transmission

Sovyx's default `voice_biometric_processing_enabled=False` means no
biometric collection occurs without explicit operator opt-in.

When the operator flips to `True` (Phase 8 multi-mind voice ID per
master mission), the operator MUST:

1. Capture explicit written consent from each enrolled speaker via
   their own onboarding flow — Sovyx provides the technical control
   + audit trail, the legal-basis chain is the operator's
   responsibility.
2. Publish a retention policy stating how long voiceprints live
   (Sovyx's default upper bound: indefinite while
   `voice_biometric_processing_enabled=True`; flip to `False` to
   purge).
3. Document the destruction trigger (BIPA requires destruction at
   the earlier of "purpose fulfilled" or "3 years since last
   interaction").

---

## HIPAA — US healthcare

HIPAA applies when Sovyx is deployed inside a covered entity (hospital
EMR integration, telehealth platform). The voice subsystem's HIPAA
posture:

* **Minimum necessary standard** — no audio retention by default
  satisfies the minimum-necessary rule.
* **Audit trail** — ConsentLedger captures every privacy-relevant
  event with timestamps; covered entities can use this for HIPAA
  log-retention obligations.
* **Encryption at rest** — operator-managed via the OS filesystem
  layer (see Compliance matrix above).
* **Business Associate Agreement (BAA)** — when a cloud STT/TTS
  engine is wired, the operator MUST execute a BAA with the cloud
  vendor; Sovyx neither imposes nor enforces this — it's the
  operator's compliance obligation.

---

## Operator responsibilities

Sovyx ships the technical controls. The controller still owns the
policy decisions. Before a Sovyx voice deployment can be considered
compliant, the operator must:

1. **Document the lawful basis** for processing (GDPR Art. 6 / LGPD
   Art. 7) — typically "consent" for personal-use deployments,
   "legitimate interest" or "contractual necessity" for
   business-use.
2. **Sign a DPA / sub-processor contract** with any cloud STT/TTS
   provider. Sovyx does not enforce the contract; the operator's
   configuration UI lets the operator route data wherever they
   please. The `SHARE` action records which destination was hit.
3. **Sign a BAA** if HIPAA-covered.
4. **Capture biometric-specific written consent** if flipping
   `voice_biometric_processing_enabled=True` (BIPA §15(b)).
5. **Publish a retention policy** that aligns with
   `voice_audio_retention_days` (default 0 = "no retention" is the
   simplest policy).
6. **Configure filesystem-level encryption** (LUKS / BitLocker /
   FileVault) for the `<data_dir>` to satisfy GDPR Art. 32 / HIPAA
   §164.312(a)(2)(iv).

---

## See also

* [`docs/compliance.md`](../compliance.md) — cross-subsystem
  compliance map (logging, audit, brain, voice).
* [`docs/security.md`](../security.md) — threat model + security
  posture.
* `src/sovyx/voice/_consent_ledger.py` — the ledger implementation
  with full design invariants in the module docstring.
* `MISSION-voice-final-skype-grade-2026.md` §Phase 7 / T7.31-T7.40 —
  the design spec for this surface.
