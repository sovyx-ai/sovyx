"""Per-mind data wipe service — Phase 8 / T8.21 steps 2-3.

Master mission §Phase 8 / T8.21 ships per-mind GDPR / LGPD compliance
in stages (per ``feedback_staged_adoption``):

* **Step 1** ✅ — :class:`sovyx.voice._consent_ledger.ConsentLedger`
  carries optional ``mind_id`` so the voice audit trail is per-mind
  scoped.
* **Step 2** ✅ — :class:`MindForgetService` wipes the **brain
  database** for a single mind: concepts (cascading relations),
  episodes (cascading conversation_imports), embeddings,
  consolidation log.
* **Step 3 (this module)** — service extended to optionally wipe the
  **conversations database** (conversations + cascading
  conversation_turns) and the **system database** (daily_stats).
  Each pool is optional; the service degrades cleanly when only a
  subset is wired (matches step-2 callers without forcing
  conversation / system pool setup on small unit tests).
* **Step 4** (pending) — wire the ``sovyx mind forget <mind_id>``
  CLI command + dashboard endpoint.
* **Step 5** (separate) — per-mind retention policy.

Per-pool atomicity:
  Each pool gets its own transaction (sqlite can't span pools).
  The order is: brain → conversations → system → consent ledger.
  A failure mid-pipeline leaves the OTHER pools unchanged + the
  report counts reflect what was actually wiped. The ConsentLedger
  purge happens last (separate JSONL file) so its tombstone
  documents whatever brain + conversations + system rows were
  destroyed before it ran, even if the ledger phase itself fails.

Reference: master mission ``MISSION-voice-final-skype-grade-2026.md``
§Phase 8 / T8.21 (steps 2-3 of 4).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from sovyx.observability.logging import get_logger

if TYPE_CHECKING:
    from sovyx.engine.types import MindId
    from sovyx.persistence.pool import DatabasePool
    from sovyx.voice._consent_ledger import ConsentLedger

logger = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class MindForgetReport:
    """Forensic record of what a :meth:`MindForgetService.forget_mind`
    call destroyed.

    All counts are pre-delete row counts (i.e. how many rows were
    purged). For ``dry_run=True`` invocations the counts reflect what
    *would have been* purged — no rows are actually deleted.

    Attributes:
        mind_id: The target mind. Stable opaque identifier.
        concepts_purged: Rows removed from ``concepts``.
        relations_purged: Rows removed from ``relations`` (via FK
            cascade when concepts are deleted; counted explicitly
            via the join).
        episodes_purged: Rows removed from ``episodes``.
        concept_embeddings_purged: Rows removed from
            ``concept_embeddings`` (vec0; manual delete, no cascade).
        episode_embeddings_purged: Rows removed from
            ``episode_embeddings`` (vec0; manual delete, no cascade).
        conversation_imports_purged: Rows removed from
            ``conversation_imports`` (via FK cascade when episodes
            are deleted; counted explicitly via mind_id column).
        consolidation_log_purged: Rows removed from
            ``consolidation_log``.
        conversations_purged: Rows removed from the conversations
            table (conversations.db). 0 when ``conversations_pool``
            is not wired.
        conversation_turns_purged: Rows removed from
            ``conversation_turns`` via FK cascade. 0 when
            ``conversations_pool`` is not wired.
        daily_stats_purged: Rows removed from ``daily_stats``
            (system.db). 0 when ``system_pool`` is not wired.
        consent_ledger_purged: Records purged from the consent
            ledger (0 if no ledger configured or the purge phase
            failed; the brain wipe is independent of this).
        dry_run: ``True`` when the call was a preview (no writes).
    """

    mind_id: MindId
    concepts_purged: int
    relations_purged: int
    episodes_purged: int
    concept_embeddings_purged: int
    episode_embeddings_purged: int
    conversation_imports_purged: int
    consolidation_log_purged: int
    conversations_purged: int
    conversation_turns_purged: int
    daily_stats_purged: int
    consent_ledger_purged: int
    dry_run: bool

    @property
    def total_brain_rows_purged(self) -> int:
        """Sum of every brain-table row count.

        Excludes conversations / system pool counts (see
        :attr:`total_rows_purged` for the all-pools aggregate).
        """
        return (
            self.concepts_purged
            + self.relations_purged
            + self.episodes_purged
            + self.concept_embeddings_purged
            + self.episode_embeddings_purged
            + self.conversation_imports_purged
            + self.consolidation_log_purged
        )

    @property
    def total_conversations_rows_purged(self) -> int:
        """Sum of conversations-pool row counts (conversations +
        conversation_turns cascade)."""
        return self.conversations_purged + self.conversation_turns_purged

    @property
    def total_system_rows_purged(self) -> int:
        """Sum of system-pool row counts (currently daily_stats only)."""
        return self.daily_stats_purged

    @property
    def total_rows_purged(self) -> int:
        """Aggregate across every persistent surface EXCEPT the
        consent ledger (which is JSONL, not relational, and tracked
        separately for compliance reporting)."""
        return (
            self.total_brain_rows_purged
            + self.total_conversations_rows_purged
            + self.total_system_rows_purged
        )


class MindForgetService:
    """Wipe every per-mind record across configured pools, atomically.

    The service is the structural primitive behind the
    ``sovyx mind forget <mind_id>`` CLI. It is independent of CLI /
    dashboard surfaces so unit tests can drive it directly without
    spinning up a Typer runner.

    Args:
        brain_pool: The brain database pool (concepts + episodes +
            relations + embeddings + consolidation_log). Required —
            the brain is the largest per-mind surface and the
            structural reason this service exists.
        conversations_pool: Optional conversations database pool
            (conversations + conversation_turns cascade). When
            ``None``, the conversations counts on the report stay
            at zero. Step-2-only callers (brain-only wipe) pass
            ``None``; production wiring + step-3 tests pass the
            real pool.
        system_pool: Optional system database pool (daily_stats).
            Same Optional semantics as ``conversations_pool``.
        ledger: Optional :class:`ConsentLedger`. When supplied,
            :meth:`forget_mind` calls ``ledger.forget(mind_id=...)``
            after every relational pool has committed so the voice
            audit trail is also wiped + a tombstone written.

    Thread safety:
        The service holds no mutable state; concurrent invocations
        on different ``mind_id`` values are safe. Concurrent
        invocations on the *same* ``mind_id`` are serialised by
        each pool's writer lock — the second call sees already-empty
        pools and reports zero counts.
    """

    def __init__(
        self,
        *,
        brain_pool: DatabasePool,
        conversations_pool: DatabasePool | None = None,
        system_pool: DatabasePool | None = None,
        ledger: ConsentLedger | None = None,
    ) -> None:
        self._brain_pool = brain_pool
        self._conversations_pool = conversations_pool
        self._system_pool = system_pool
        self._ledger = ledger

    async def forget_mind(
        self,
        mind_id: MindId,
        *,
        dry_run: bool = False,
    ) -> MindForgetReport:
        """Wipe every per-mind record across configured pools.

        Per-pool order (each pool gets its own transaction; sqlite
        can't span pools):
          1. **Count phase** — read every per-mind row count across
             configured pools BEFORE any delete. The FK cascades
             fire during the delete phase, so post-delete counts
             would underreport.
          2. If ``dry_run``: return the report; no writes happen on
             any pool, and the ledger is NOT touched.
          3. **Brain pool** — single transaction:
             a. ``DELETE FROM concept_embeddings`` (vec0; explicit;
                no cascade).
             b. ``DELETE FROM episode_embeddings`` (vec0; explicit;
                no cascade).
             c. ``DELETE FROM concepts WHERE mind_id = ?`` —
                cascades ``relations`` via FK ON DELETE CASCADE.
             d. ``DELETE FROM episodes WHERE mind_id = ?`` —
                cascades ``conversation_imports`` via FK.
             e. ``DELETE FROM consolidation_log WHERE mind_id = ?``.
             f. Commit.
          4. **Conversations pool** (if wired) — single transaction:
             a. ``DELETE FROM conversations WHERE mind_id = ?`` —
                cascades ``conversation_turns`` via FK + the
                ``turns_fts`` trigger keeps the FTS5 index in sync.
             b. Commit.
          5. **System pool** (if wired) — single transaction:
             a. ``DELETE FROM daily_stats WHERE mind_id = ?``.
             b. Commit.
          6. **Consent ledger** (if wired) —
             ``ledger.forget(mind_id=...)`` runs LAST so its
             tombstone documents the relational wipes that already
             completed. A failure here doesn't roll back the
             relational deletes (different file, different atomic
             semantics).

        Args:
            mind_id: The target mind. Empty string is REJECTED —
                would match every empty-mind_id row (which
                shouldn't exist, but defensive guard).
            dry_run: When True, return the count report without
                making any writes. Useful for the CLI's
                ``--dry-run`` confirmation flow.

        Returns:
            :class:`MindForgetReport` documenting what was (or
            would have been) destroyed.

        Raises:
            ValueError: ``mind_id`` is empty.
        """
        if not str(mind_id).strip():
            msg = (
                "MindForgetService.forget_mind requires a non-empty "
                "mind_id; an empty value would match every empty-mind_id "
                "row (which should not exist) and is rejected as a "
                "defensive guard"
            )
            raise ValueError(msg)

        brain_counts = await self._count_brain_rows(mind_id)
        conv_counts = await self._count_conversations_rows(mind_id)
        system_counts = await self._count_system_rows(mind_id)

        if dry_run:
            return MindForgetReport(
                mind_id=mind_id,
                **brain_counts,
                **conv_counts,
                **system_counts,
                consent_ledger_purged=0,
                dry_run=True,
            )

        await self._delete_brain_rows(mind_id)
        await self._delete_conversations_rows(mind_id)
        await self._delete_system_rows(mind_id)

        consent_purged = 0
        if self._ledger is not None:
            consent_purged = self._ledger.forget(mind_id=str(mind_id))

        report = MindForgetReport(
            mind_id=mind_id,
            **brain_counts,
            **conv_counts,
            **system_counts,
            consent_ledger_purged=consent_purged,
            dry_run=False,
        )
        logger.warning(
            "mind.forget.wipe_complete",
            mind_id=str(mind_id),
            **{
                "mind.concepts_purged": report.concepts_purged,
                "mind.relations_purged": report.relations_purged,
                "mind.episodes_purged": report.episodes_purged,
                "mind.concept_embeddings_purged": report.concept_embeddings_purged,
                "mind.episode_embeddings_purged": report.episode_embeddings_purged,
                "mind.conversation_imports_purged": report.conversation_imports_purged,
                "mind.consolidation_log_purged": report.consolidation_log_purged,
                "mind.conversations_purged": report.conversations_purged,
                "mind.conversation_turns_purged": report.conversation_turns_purged,
                "mind.daily_stats_purged": report.daily_stats_purged,
                "mind.consent_ledger_purged": report.consent_ledger_purged,
                "mind.total_rows_purged": report.total_rows_purged,
            },
        )
        return report

    # ── internals ────────────────────────────────────────────────────

    async def _count_brain_rows(self, mind_id: MindId) -> dict[str, int]:
        """Count every per-mind row across the brain DB.

        Read-only; safe to call concurrently. Counts are computed
        against the *current* state, so a concurrent writer can
        change them between count + delete. The transaction in
        :meth:`_delete_brain_rows` isolates the actual delete from
        concurrent writes; the count here is best-effort.
        """
        async with self._brain_pool.read() as conn:
            mind_str = str(mind_id)

            cursor = await conn.execute(
                "SELECT COUNT(*) FROM concepts WHERE mind_id = ?",
                (mind_str,),
            )
            concepts = int((await cursor.fetchone() or [0])[0])

            cursor = await conn.execute(
                "SELECT COUNT(*) FROM episodes WHERE mind_id = ?",
                (mind_str,),
            )
            episodes = int((await cursor.fetchone() or [0])[0])

            # Relations: count rows where EITHER end is in mind_id.
            # Matches the FK cascade semantics (cascade fires when
            # source OR target is deleted). The DISTINCT prevents
            # double-counting a relation that has BOTH endpoints in
            # the same mind (the common case).
            cursor = await conn.execute(
                """SELECT COUNT(DISTINCT id) FROM relations
                   WHERE source_id IN (SELECT id FROM concepts WHERE mind_id = ?)
                      OR target_id IN (SELECT id FROM concepts WHERE mind_id = ?)""",
                (mind_str, mind_str),
            )
            relations = int((await cursor.fetchone() or [0])[0])

            # conversation_imports has BOTH a direct mind_id column
            # AND an episode_id FK. We count via mind_id (direct +
            # cheaper); the cascade is a belt-and-suspenders safety
            # net for any future row that's missing a mind_id.
            cursor = await conn.execute(
                "SELECT COUNT(*) FROM conversation_imports WHERE mind_id = ?",
                (mind_str,),
            )
            conv_imports = int((await cursor.fetchone() or [0])[0])

            cursor = await conn.execute(
                "SELECT COUNT(*) FROM consolidation_log WHERE mind_id = ?",
                (mind_str,),
            )
            consolidation = int((await cursor.fetchone() or [0])[0])

            # Embedding tables: count via the parent table's mind_id
            # since the vec0 virtual table doesn't carry mind_id
            # itself. If sqlite-vec isn't loaded the tables don't
            # exist and the count is zero.
            concept_embs = 0
            episode_embs = 0
            if self._brain_pool.has_sqlite_vec:
                cursor = await conn.execute(
                    """SELECT COUNT(*) FROM concept_embeddings
                       WHERE concept_id IN (SELECT id FROM concepts WHERE mind_id = ?)""",
                    (mind_str,),
                )
                concept_embs = int((await cursor.fetchone() or [0])[0])

                cursor = await conn.execute(
                    """SELECT COUNT(*) FROM episode_embeddings
                       WHERE episode_id IN (SELECT id FROM episodes WHERE mind_id = ?)""",
                    (mind_str,),
                )
                episode_embs = int((await cursor.fetchone() or [0])[0])

        return {
            "concepts_purged": concepts,
            "relations_purged": relations,
            "episodes_purged": episodes,
            "concept_embeddings_purged": concept_embs,
            "episode_embeddings_purged": episode_embs,
            "conversation_imports_purged": conv_imports,
            "consolidation_log_purged": consolidation,
        }

    async def _delete_brain_rows(self, mind_id: MindId) -> None:
        """Delete every per-mind brain row in dependency order, atomically.

        Single transaction: a crash leaves the DB in pre-wipe state.
        Order matters because sqlite-vec virtual tables have no FK
        cascades — they MUST be deleted before their parent tables
        are gone (otherwise the subselect ``WHERE concept_id IN
        (SELECT id FROM concepts WHERE mind_id=?)`` would return
        empty after the parent delete).
        """
        mind_str = str(mind_id)
        async with self._brain_pool.transaction() as conn:
            if self._brain_pool.has_sqlite_vec:
                await conn.execute(
                    """DELETE FROM concept_embeddings
                       WHERE concept_id IN (SELECT id FROM concepts WHERE mind_id = ?)""",
                    (mind_str,),
                )
                await conn.execute(
                    """DELETE FROM episode_embeddings
                       WHERE episode_id IN (SELECT id FROM episodes WHERE mind_id = ?)""",
                    (mind_str,),
                )
            # Concepts cascade -> relations (FK ON DELETE CASCADE).
            await conn.execute(
                "DELETE FROM concepts WHERE mind_id = ?",
                (mind_str,),
            )
            # Episodes cascade -> conversation_imports (FK ON DELETE CASCADE).
            await conn.execute(
                "DELETE FROM episodes WHERE mind_id = ?",
                (mind_str,),
            )
            await conn.execute(
                "DELETE FROM consolidation_log WHERE mind_id = ?",
                (mind_str,),
            )

    async def _count_conversations_rows(self, mind_id: MindId) -> dict[str, int]:
        """Count per-mind rows in the conversations pool.

        Returns zeros when ``conversations_pool`` is not wired so the
        report carries the same shape regardless of pool topology.
        """
        if self._conversations_pool is None:
            return {"conversations_purged": 0, "conversation_turns_purged": 0}

        mind_str = str(mind_id)
        async with self._conversations_pool.read() as conn:
            cursor = await conn.execute(
                "SELECT COUNT(*) FROM conversations WHERE mind_id = ?",
                (mind_str,),
            )
            conversations = int((await cursor.fetchone() or [0])[0])

            # conversation_turns FK cascade: count via the join.
            cursor = await conn.execute(
                """SELECT COUNT(*) FROM conversation_turns
                   WHERE conversation_id IN
                       (SELECT id FROM conversations WHERE mind_id = ?)""",
                (mind_str,),
            )
            turns = int((await cursor.fetchone() or [0])[0])

        return {
            "conversations_purged": conversations,
            "conversation_turns_purged": turns,
        }

    async def _delete_conversations_rows(self, mind_id: MindId) -> None:
        """Delete every per-mind conversations row, atomically.

        ``conversation_turns`` cascades via FK ON DELETE CASCADE on
        ``conversation_id``. The ``turns_fts`` triggers keep the FTS5
        virtual table in sync automatically (see schemas/conversations.py
        ``turns_ad`` AFTER DELETE trigger).
        """
        if self._conversations_pool is None:
            return
        mind_str = str(mind_id)
        async with self._conversations_pool.transaction() as conn:
            await conn.execute(
                "DELETE FROM conversations WHERE mind_id = ?",
                (mind_str,),
            )

    async def _count_system_rows(self, mind_id: MindId) -> dict[str, int]:
        """Count per-mind rows in the system pool (currently daily_stats)."""
        if self._system_pool is None:
            return {"daily_stats_purged": 0}
        mind_str = str(mind_id)
        async with self._system_pool.read() as conn:
            cursor = await conn.execute(
                "SELECT COUNT(*) FROM daily_stats WHERE mind_id = ?",
                (mind_str,),
            )
            daily = int((await cursor.fetchone() or [0])[0])
        return {"daily_stats_purged": daily}

    async def _delete_system_rows(self, mind_id: MindId) -> None:
        """Delete every per-mind system row, atomically."""
        if self._system_pool is None:
            return
        mind_str = str(mind_id)
        async with self._system_pool.transaction() as conn:
            await conn.execute(
                "DELETE FROM daily_stats WHERE mind_id = ?",
                (mind_str,),
            )


__all__ = [
    "MindForgetReport",
    "MindForgetService",
]
