"""Privacy helpers for observability — bounded-cardinality hashing of identifiers.

Single source of truth for the ``short_hash(value)`` helper used across the
voice calibration pipeline (engine, applier, persistence, KB cache, wizard
orchestrator, dashboard routes) to surface operator-set identifiers
(``mind_id``, ``job_id``, ``profile_id``) in OTel telemetry without leaking
the raw value to downstream consumers.

Contract:
- 16 hex chars (64 bits) of SHA256 prefix.
- Deterministic: same input → same hash, across processes + restarts.
- Bounded cardinality: caps the unique-label space for OTel attribute
  cardinality budgets; a fleet of 1M minds emits ≤ 1M distinct hashes.
- Privacy: 64-bit prefix is not reversible to the operator-set string;
  documented as low collision risk (2^-32 collision probability for 1M
  items by birthday bound — acceptable for our scale).

Usage::

    from sovyx.observability.privacy import short_hash

    logger.info(
        "voice.calibration.wizard.job_started",
        mind_id_hash=short_hash(mind_id),
        job_id_hash=short_hash(job_id),
    )

Aligned with mission ``MISSION-voice-calibration-extreme-audit-2026-05-06.md``
§4 (Phase 0) — telemetry hashing remediation.
"""

from __future__ import annotations

import hashlib

__all__ = ["short_hash"]


def short_hash(value: str) -> str:
    """Return the 16-hex-char SHA256 prefix of ``value``.

    Used for telemetry labels (``mind_id_hash``, ``job_id_hash``,
    ``profile_id_hash``) so events can be correlated across the
    voice-calibration pipeline WITHOUT shipping the raw operator-set
    identifier (which may be PII per the privacy contract).
    """
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]
