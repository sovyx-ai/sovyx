"""Persistent operator-acknowledgement store for the composite
degraded banner.

Mission anchor: ``docs-internal/missions/MISSION-c4-degraded-mode-banner-2026-05-17.md``
§Phase 3 §T3.2.

Backs the ``operator_acks`` SQLite table created by Migration 003
(see ``src/sovyx/persistence/schemas/system.py``). Persistence is
server-side per ADR-D2 — multi-tab divergence + lost-ack-on-refresh
both ruled out the client-side ``sessionStorage`` alternative.

Public API:

* :meth:`record_ack` — operator dismisses a degraded axis for a TTL.
* :meth:`get_ack` — lookup the current ack state for a reason (None
  iff no ack OR ack expired).
* :meth:`clear_ack` — explicit removal (e.g. underlying condition
  resolved).
* :meth:`list_active_acks` — snapshot of all non-expired acks (used
  by :func:`get_engine_degraded` to enrich the response).
* :meth:`prune_expired` — bulk removal of expired entries (called by
  the Phase 3 TTL re-surface scheduler before emitting
  ``voice.degraded_banner.resurfaced``).

TTL semantics: an ack is "active" iff
``acked_at_ts + ttl_sec > unix_now()``. The store NEVER returns
expired entries from :meth:`get_ack` / :meth:`list_active_acks`;
:meth:`prune_expired` is the housekeeping path that surfaces
expired entries to the re-surface scheduler.

Cardinality bound: ≤ 8 active acks typical (one per degraded axis
in the worst case); SQLite's PRIMARY KEY index on ``reason``
makes lookup O(log n) at any cardinality.

Anti-pattern compliance:

* #5 — singleton-via-registry; consumers MUST resolve via
  ``registry.resolve(OperatorAcksStore)``.
* #14 — every DB call goes through the async pool; never blocks the
  event loop.
* #19 — server-side persistence; never falls back to localStorage.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from sovyx.observability.logging import get_logger

if TYPE_CHECKING:
    from sovyx.persistence.pool import DatabasePool

logger = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class AckRecord:
    """One operator-acknowledgement record.

    Attributes:
        reason: Canonical reason token (e.g.
            ``"voice.failover_ladder_exhausted"`` — matches
            :attr:`DegradedEntry.reason` for the axis the ack
            applies to).
        acked_at_ts: Unix epoch seconds at which the operator acked.
        ttl_sec: Operator-chosen TTL.
        operator_id: Best-effort identification (token-hash);
            empty string when unidentifiable.
        metadata: Axis-specific JSON-encoded context captured at
            ack time.
    """

    reason: str
    acked_at_ts: int
    ttl_sec: int
    operator_id: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    def ttl_remaining_sec(self, now_ts: int | None = None) -> int:
        """Seconds remaining before this ack expires. Clamped to 0
        when already expired."""
        if now_ts is None:
            now_ts = int(time.time())
        remaining = (self.acked_at_ts + self.ttl_sec) - now_ts
        return max(0, remaining)

    def is_expired(self, now_ts: int | None = None) -> bool:
        return self.ttl_remaining_sec(now_ts) == 0


class OperatorAcksStore:
    """Persistent ack ledger backed by SQLite ``operator_acks`` table.

    Registered in :class:`ServiceRegistry` during bootstrap. Consumers
    resolve via the registry — never instantiate directly per anti-
    pattern #5.
    """

    def __init__(self, system_pool: DatabasePool) -> None:
        self._pool = system_pool

    async def record_ack(
        self,
        *,
        reason: str,
        ttl_sec: int,
        operator_id: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> AckRecord:
        """Insert or replace an ack for ``reason``.

        Operator-issued ack via ``POST /api/voice/degraded/ack``.
        Caller is responsible for bounds-validating ``ttl_sec``
        (the endpoint enforces [60, 86400] per ADR-D9); this store
        accepts whatever it is given so test fixtures can construct
        edge cases.
        """
        now_ts = int(time.time())
        metadata_json = json.dumps(metadata or {})
        async with self._pool.write() as conn:
            await conn.execute(
                """INSERT OR REPLACE INTO operator_acks
                   (reason, acked_at_ts, ttl_sec, operator_id, metadata)
                   VALUES (?, ?, ?, ?, ?)""",
                (reason, now_ts, int(ttl_sec), operator_id, metadata_json),
            )
            await conn.commit()
        logger.info(
            "voice.degraded_banner.acked",
            **{
                "voice.reason": reason,
                "voice.ttl_sec": int(ttl_sec),
                "voice.operator_id": operator_id,
            },
        )
        return AckRecord(
            reason=reason,
            acked_at_ts=now_ts,
            ttl_sec=int(ttl_sec),
            operator_id=operator_id,
            metadata=metadata or {},
        )

    async def get_ack(self, reason: str) -> AckRecord | None:
        """Return the current ack for ``reason`` if active (not
        expired), else None.

        Expired entries are NOT silently returned; the caller can
        treat the missing record as "operator should see a fresh
        banner". Use :meth:`prune_expired` to clean up.
        """
        now_ts = int(time.time())
        async with self._pool.read() as conn:
            cursor = await conn.execute(
                """SELECT reason, acked_at_ts, ttl_sec, operator_id, metadata
                   FROM operator_acks WHERE reason = ?""",
                (reason,),
            )
            row = await cursor.fetchone()
        if row is None:
            return None
        record = _row_to_record(row)
        if record.is_expired(now_ts):
            return None
        return record

    async def clear_ack(self, reason: str) -> bool:
        """Remove the ack for ``reason``. Returns ``True`` iff an
        entry was removed."""
        async with self._pool.write() as conn:
            cursor = await conn.execute(
                "DELETE FROM operator_acks WHERE reason = ?",
                (reason,),
            )
            await conn.commit()
        return cursor.rowcount > 0

    async def list_active_acks(self) -> list[AckRecord]:
        """Snapshot of all non-expired acks. Used by
        :func:`get_engine_degraded` to enrich per-axis ack state.
        """
        now_ts = int(time.time())
        async with self._pool.read() as conn:
            cursor = await conn.execute(
                """SELECT reason, acked_at_ts, ttl_sec, operator_id, metadata
                   FROM operator_acks""",
            )
            rows = await cursor.fetchall()
        return [r for r in (_row_to_record(row) for row in rows) if not r.is_expired(now_ts)]

    async def prune_expired(self) -> list[AckRecord]:
        """Bulk-remove expired acks. Returns the records that were
        removed so the Phase 3 re-surface scheduler can emit
        ``voice.degraded_banner.resurfaced`` per record."""
        now_ts = int(time.time())
        async with self._pool.write() as conn:
            cursor = await conn.execute(
                """SELECT reason, acked_at_ts, ttl_sec, operator_id, metadata
                   FROM operator_acks
                   WHERE acked_at_ts + ttl_sec <= ?""",
                (now_ts,),
            )
            rows = await cursor.fetchall()
            removed = [_row_to_record(row) for row in rows]
            if removed:
                await conn.execute(
                    """DELETE FROM operator_acks
                       WHERE acked_at_ts + ttl_sec <= ?""",
                    (now_ts,),
                )
                await conn.commit()
        return removed


def _row_to_record(row: Any) -> AckRecord:  # noqa: ANN401 — sqlite row
    metadata_raw = row[4] if len(row) > 4 else "{}"
    try:
        metadata = json.loads(metadata_raw) if metadata_raw else {}
    except (json.JSONDecodeError, ValueError):
        metadata = {}
    return AckRecord(
        reason=row[0],
        acked_at_ts=int(row[1]),
        ttl_sec=int(row[2]),
        operator_id=row[3] or "",
        metadata=metadata if isinstance(metadata, dict) else {},
    )


__all__ = ["AckRecord", "OperatorAcksStore"]
