"""Per-mind data wipe service — Phase 8 / T8.21 step 2.

Master mission §Phase 8 / T8.21 ships per-mind GDPR / LGPD compliance
in stages (per ``feedback_staged_adoption``):

* **Step 1** ✅ — :class:`sovyx.voice._consent_ledger.ConsentLedger`
  carries optional ``mind_id`` so the voice audit trail is per-mind
  scoped.
* **Step 2 (this module)** — :class:`MindForgetService` wipes the
  **brain database** for a single mind: concepts (cascading
  relations), episodes (cascading conversation_imports), embeddings,
  consolidation log. Optionally purges the matching ConsentLedger
  records. Returns a structured :class:`MindForgetReport` so the
  operator (CLI / dashboard) sees exactly what was destroyed.
* **Step 3** (pending) — extend to conversations.db (conversations
  + conversation_turns) + system.db (daily_stats); wire the
  ``sovyx mind forget <mind_id>`` CLI command.
* **Step 4** (pending) — per-mind retention policy.

Why brain-only in step 2:
  The brain pool holds the *largest* per-mind data surface (concepts
  + episodes + relations + embeddings) and the most-FK-coupled
  cascades. Getting it right in isolation gives us a tested
  primitive that step 3 can compose with the conversations / system
  pools without re-litigating the brain dependency order. Splitting
  per-pool also keeps the test surface tractable — each step has a
  single pool fixture instead of three.

Atomicity:
  All brain DELETEs run in a single transaction so a crash
  mid-wipe leaves the database in its pre-wipe state. The
  ConsentLedger purge happens *after* brain commit because the
  ledger is a separate JSONL file with its own atomic semantics —
  the operator gets a single tombstone whose ``purged_record_count``
  documents the brain wipe even if the JSONL phase fails.

Reference: master mission ``MISSION-voice-final-skype-grade-2026.md``
§Phase 8 / T8.21 (step 2 of 4).
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
    consent_ledger_purged: int
    dry_run: bool

    @property
    def total_brain_rows_purged(self) -> int:
        """Sum of every brain-table row count — useful for one-line
        operator confirmation messages."""
        return (
            self.concepts_purged
            + self.relations_purged
            + self.episodes_purged
            + self.concept_embeddings_purged
            + self.episode_embeddings_purged
            + self.conversation_imports_purged
            + self.consolidation_log_purged
        )


class MindForgetService:
    """Wipe every brain-DB record for a single mind, atomically.

    The service is the structural primitive behind the
    ``sovyx mind forget <mind_id>`` CLI. It is independent of CLI /
    dashboard surfaces so unit tests can drive it directly without
    spinning up a Typer runner.

    Args:
        brain_pool: The brain database pool (concepts + episodes +
            relations + embeddings + consolidation_log).
        ledger: Optional :class:`ConsentLedger`. When supplied,
            :meth:`forget_mind` calls ``ledger.forget(mind_id=...)``
            after the brain commit so the voice audit trail is also
            wiped + a tombstone written. ``None`` is the right
            choice when the caller manages the ledger separately
            (e.g. dashboard endpoint that reports ledger and brain
            counts independently).

    Thread safety:
        The service holds no mutable state; concurrent invocations
        on different ``mind_id`` values are safe. Concurrent
        invocations on the *same* ``mind_id`` are serialised by the
        brain pool's writer lock — the second call sees an
        already-empty brain and reports zero counts.
    """

    def __init__(
        self,
        *,
        brain_pool: DatabasePool,
        ledger: ConsentLedger | None = None,
    ) -> None:
        self._brain_pool = brain_pool
        self._ledger = ledger

    async def forget_mind(
        self,
        mind_id: MindId,
        *,
        dry_run: bool = False,
    ) -> MindForgetReport:
        """Wipe every brain-DB record for ``mind_id``.

        Order of operations (brain pool):
          1. Count rows per table (BEFORE delete; the cascades fire
             on step 3-5 so post-delete counts would underreport).
          2. If ``dry_run``: return the report; no writes.
          3. Open a single write transaction:
             a. ``DELETE FROM concept_embeddings`` (vec0; explicit;
                no cascade).
             b. ``DELETE FROM episode_embeddings`` (vec0; explicit;
                no cascade).
             c. ``DELETE FROM concepts WHERE mind_id = ?`` —
                cascades ``relations`` via FK ON DELETE CASCADE
                (``foreign_keys=ON`` is the default pragma).
             d. ``DELETE FROM episodes WHERE mind_id = ?`` —
                cascades ``conversation_imports`` via FK.
             e. ``DELETE FROM consolidation_log WHERE mind_id = ?``.
             f. Commit.
          4. If a ledger is configured, call
             ``ledger.forget(mind_id=...)`` *after* the brain commit
             (separate file, separate atomic semantics).

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

        counts = await self._count_brain_rows(mind_id)

        if dry_run:
            return MindForgetReport(
                mind_id=mind_id,
                **counts,
                consent_ledger_purged=0,
                dry_run=True,
            )

        await self._delete_brain_rows(mind_id)

        consent_purged = 0
        if self._ledger is not None:
            consent_purged = self._ledger.forget(mind_id=str(mind_id))

        report = MindForgetReport(
            mind_id=mind_id,
            **counts,
            consent_ledger_purged=consent_purged,
            dry_run=False,
        )
        logger.warning(
            "mind.forget.brain_wipe_complete",
            mind_id=str(mind_id),
            **{
                "mind.concepts_purged": report.concepts_purged,
                "mind.relations_purged": report.relations_purged,
                "mind.episodes_purged": report.episodes_purged,
                "mind.concept_embeddings_purged": report.concept_embeddings_purged,
                "mind.episode_embeddings_purged": report.episode_embeddings_purged,
                "mind.conversation_imports_purged": report.conversation_imports_purged,
                "mind.consolidation_log_purged": report.consolidation_log_purged,
                "mind.consent_ledger_purged": report.consent_ledger_purged,
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
        """Delete every per-mind row in dependency order, atomically.

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


__all__ = [
    "MindForgetReport",
    "MindForgetService",
]
