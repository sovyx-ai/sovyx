"""Tests for :class:`sovyx.mind.forget.MindForgetService` — Phase 8 / T8.21 step 2.

Covers:

* Brain wipe destroys every per-mind row across concepts, episodes,
  relations (cascade), embeddings, conversation_imports (cascade),
  and consolidation_log.
* Other minds are completely untouched (cross-mind isolation under
  forget).
* Dry-run reports what *would* be purged without writing.
* Empty / whitespace mind_id is rejected.
* ConsentLedger integration: when a ledger is supplied,
  :meth:`forget_mind` calls ``ledger.forget(mind_id=...)`` after the
  brain commit and the report carries the count.
* Idempotency: forgetting an empty mind succeeds and reports zeros.

Reference: master mission ``MISSION-voice-final-skype-grade-2026.md``
§Phase 8 / T8.21 (step 2 of 4).
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock

import pytest

from sovyx.brain.concept_repo import ConceptRepository
from sovyx.brain.episode_repo import EpisodeRepository
from sovyx.brain.models import Concept, Episode, Relation
from sovyx.brain.relation_repo import RelationRepository
from sovyx.engine.types import ConversationId, MindId, RelationType
from sovyx.mind.forget import MindForgetReport, MindForgetService
from sovyx.persistence.migrations import MigrationRunner
from sovyx.persistence.pool import DatabasePool
from sovyx.persistence.schemas.brain import get_brain_migrations
from sovyx.persistence.schemas.conversations import get_conversation_migrations
from sovyx.persistence.schemas.system import get_system_migrations
from sovyx.voice._consent_ledger import ConsentAction, ConsentLedger

if TYPE_CHECKING:
    from pathlib import Path

    from sovyx.brain.embedding import EmbeddingEngine


MIND_A = MindId("aria")
MIND_B = MindId("luna")


# ── Fixtures ─────────────────────────────────────────────────────────


@pytest.fixture
async def pool(tmp_path: Path) -> DatabasePool:
    p = DatabasePool(
        db_path=tmp_path / "brain.db",
        read_pool_size=1,
        load_extensions=["vec0"],
    )
    await p.initialize()
    runner = MigrationRunner(p)
    await runner.initialize()
    await runner.run_migrations(get_brain_migrations(has_sqlite_vec=p.has_sqlite_vec))
    yield p  # type: ignore[misc]
    await p.close()


@pytest.fixture
def mock_embedding() -> EmbeddingEngine:
    engine = AsyncMock()
    engine.has_embeddings = False
    engine.encode = AsyncMock(return_value=[0.1] * 384)
    return engine


@pytest.fixture
def concept_repo(pool: DatabasePool, mock_embedding: EmbeddingEngine) -> ConceptRepository:
    return ConceptRepository(pool, mock_embedding)


@pytest.fixture
def episode_repo(pool: DatabasePool, mock_embedding: EmbeddingEngine) -> EpisodeRepository:
    return EpisodeRepository(pool, mock_embedding)


@pytest.fixture
def relation_repo(pool: DatabasePool) -> RelationRepository:
    return RelationRepository(pool)


@pytest.fixture
def service(pool: DatabasePool) -> MindForgetService:
    return MindForgetService(brain_pool=pool)


@pytest.fixture
async def conversations_pool(tmp_path: Path) -> DatabasePool:
    """Conversations DB pool with the schema applied. Step-3 fixture."""
    p = DatabasePool(
        db_path=tmp_path / "conversations.db",
        read_pool_size=1,
    )
    await p.initialize()
    runner = MigrationRunner(p)
    await runner.initialize()
    await runner.run_migrations(get_conversation_migrations())
    yield p  # type: ignore[misc]
    await p.close()


@pytest.fixture
async def system_pool(tmp_path: Path) -> DatabasePool:
    """System DB pool with the schema applied. Step-3 fixture."""
    p = DatabasePool(
        db_path=tmp_path / "system.db",
        read_pool_size=1,
    )
    await p.initialize()
    runner = MigrationRunner(p)
    await runner.initialize()
    await runner.run_migrations(get_system_migrations())
    yield p  # type: ignore[misc]
    await p.close()


# ── Helpers ──────────────────────────────────────────────────────────


async def _seed_mind(
    *,
    mind_id: MindId,
    concept_repo: ConceptRepository,
    episode_repo: EpisodeRepository,
    relation_repo: RelationRepository,
    pool: DatabasePool,
    n_concepts: int = 3,
    n_episodes: int = 2,
    n_consolidation: int = 1,
) -> dict[str, int]:
    """Seed the brain DB with per-mind rows, return counts written."""
    concept_ids = []
    for i in range(n_concepts):
        cid = await concept_repo.create(Concept(mind_id=mind_id, name=f"{mind_id}-c{i}"))
        concept_ids.append(cid)
    for i in range(n_episodes):
        await episode_repo.create(
            Episode(
                mind_id=mind_id,
                conversation_id=ConversationId(f"{mind_id}-conv-{i}"),
                user_input=f"hi {i}",
                assistant_response="ok",
            ),
        )
    # One relation per consecutive concept pair.
    n_relations = 0
    for i in range(len(concept_ids) - 1):
        await relation_repo.create(
            Relation(
                source_id=concept_ids[i],
                target_id=concept_ids[i + 1],
                relation_type=RelationType.RELATED_TO,
            ),
        )
        n_relations += 1
    # Consolidation_log entries — direct insert (no repo).
    async with pool.transaction() as conn:
        for _ in range(n_consolidation):
            await conn.execute(
                """INSERT INTO consolidation_log
                   (mind_id, started_at, completed_at, concepts_created, duration_ms)
                   VALUES (?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, 0, 1)""",
                (str(mind_id),),
            )
    return {
        "concepts": n_concepts,
        "episodes": n_episodes,
        "relations": n_relations,
        "consolidation": n_consolidation,
    }


async def _table_count(pool: DatabasePool, sql: str, params: tuple) -> int:
    async with pool.read() as conn:
        cursor = await conn.execute(sql, params)
        row = await cursor.fetchone()
        return int(row[0]) if row else 0


async def _seed_conversations(
    *,
    pool: DatabasePool,
    mind_id: MindId,
    n_conversations: int = 2,
    n_turns_per_conv: int = 3,
) -> tuple[int, int]:
    """Seed conversations + conversation_turns; return (n_conv, n_turns)."""
    async with pool.transaction() as conn:
        for i in range(n_conversations):
            conv_id = f"{mind_id}-conv-{i}"
            await conn.execute(
                """INSERT INTO conversations
                   (id, mind_id, channel, started_at, last_message_at)
                   VALUES (?, ?, 'test', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)""",
                (conv_id, str(mind_id)),
            )
            for j in range(n_turns_per_conv):
                await conn.execute(
                    """INSERT INTO conversation_turns
                       (id, conversation_id, role, content)
                       VALUES (?, ?, 'user', ?)""",
                    (f"{conv_id}-t{j}", conv_id, f"hello {j}"),
                )
    return n_conversations, n_conversations * n_turns_per_conv


async def _seed_daily_stats(
    *,
    pool: DatabasePool,
    mind_id: MindId,
    n_days: int = 3,
) -> int:
    async with pool.transaction() as conn:
        for i in range(n_days):
            await conn.execute(
                """INSERT INTO daily_stats (date, mind_id, messages, llm_calls)
                   VALUES (?, ?, ?, ?)""",
                (f"2026-04-{i + 1:02d}", str(mind_id), i, i),
            )
    return n_days


# ── Validation guards ────────────────────────────────────────────────


class TestValidation:
    @pytest.mark.parametrize("bad", ["", "   ", "\t", "\n"])
    @pytest.mark.asyncio
    async def test_empty_or_whitespace_mind_id_rejected(
        self,
        service: MindForgetService,
        bad: str,
    ) -> None:
        with pytest.raises(ValueError, match="non-empty mind_id"):
            await service.forget_mind(MindId(bad))


# ── Dry run ──────────────────────────────────────────────────────────


class TestDryRun:
    @pytest.mark.asyncio
    async def test_dry_run_returns_counts_without_writing(
        self,
        pool: DatabasePool,
        service: MindForgetService,
        concept_repo: ConceptRepository,
        episode_repo: EpisodeRepository,
        relation_repo: RelationRepository,
    ) -> None:
        seeded = await _seed_mind(
            mind_id=MIND_A,
            concept_repo=concept_repo,
            episode_repo=episode_repo,
            relation_repo=relation_repo,
            pool=pool,
            n_concepts=3,
            n_episodes=2,
            n_consolidation=1,
        )

        report = await service.forget_mind(MIND_A, dry_run=True)

        assert report.dry_run is True
        assert report.mind_id == MIND_A
        assert report.concepts_purged == seeded["concepts"]
        assert report.episodes_purged == seeded["episodes"]
        assert report.relations_purged == seeded["relations"]
        assert report.consolidation_log_purged == seeded["consolidation"]

        # No actual deletes happened.
        remaining_concepts = await _table_count(
            pool,
            "SELECT COUNT(*) FROM concepts WHERE mind_id = ?",
            (str(MIND_A),),
        )
        assert remaining_concepts == seeded["concepts"]


# ── Brain wipe + cross-mind isolation ────────────────────────────────


class TestBrainWipe:
    @pytest.mark.asyncio
    async def test_wipes_only_target_mind(
        self,
        pool: DatabasePool,
        service: MindForgetService,
        concept_repo: ConceptRepository,
        episode_repo: EpisodeRepository,
        relation_repo: RelationRepository,
    ) -> None:
        seeded_a = await _seed_mind(
            mind_id=MIND_A,
            concept_repo=concept_repo,
            episode_repo=episode_repo,
            relation_repo=relation_repo,
            pool=pool,
        )
        seeded_b = await _seed_mind(
            mind_id=MIND_B,
            concept_repo=concept_repo,
            episode_repo=episode_repo,
            relation_repo=relation_repo,
            pool=pool,
        )

        report = await service.forget_mind(MIND_A)

        # Report counts match what was seeded for mind A.
        assert report.dry_run is False
        assert report.concepts_purged == seeded_a["concepts"]
        assert report.episodes_purged == seeded_a["episodes"]
        assert report.relations_purged == seeded_a["relations"]
        assert report.consolidation_log_purged == seeded_a["consolidation"]

        # Mind A is completely empty.
        for table, expected_zero in [
            ("concepts", "concepts WHERE mind_id = ?"),
            ("episodes", "episodes WHERE mind_id = ?"),
            ("consolidation_log", "consolidation_log WHERE mind_id = ?"),
        ]:
            count = await _table_count(
                pool,
                f"SELECT COUNT(*) FROM {expected_zero}",
                (str(MIND_A),),
            )
            assert count == 0, f"{table} still has {count} mind_a rows"

        # Mind B is untouched.
        count_b = await _table_count(
            pool,
            "SELECT COUNT(*) FROM concepts WHERE mind_id = ?",
            (str(MIND_B),),
        )
        assert count_b == seeded_b["concepts"]
        episodes_b = await _table_count(
            pool,
            "SELECT COUNT(*) FROM episodes WHERE mind_id = ?",
            (str(MIND_B),),
        )
        assert episodes_b == seeded_b["episodes"]

    @pytest.mark.asyncio
    async def test_relations_cascade_via_concept_fk(
        self,
        pool: DatabasePool,
        service: MindForgetService,
        concept_repo: ConceptRepository,
        episode_repo: EpisodeRepository,
        relation_repo: RelationRepository,
    ) -> None:
        """Deleting concepts cascades relations (FK ON DELETE CASCADE).

        The report's ``relations_purged`` MUST match the actual number
        of relation rows the cascade removed.
        """
        await _seed_mind(
            mind_id=MIND_A,
            concept_repo=concept_repo,
            episode_repo=episode_repo,
            relation_repo=relation_repo,
            pool=pool,
            n_concepts=4,
            n_episodes=0,
            n_consolidation=0,
        )

        before = await _table_count(pool, "SELECT COUNT(*) FROM relations", ())
        assert before == 3, "expected 3 relations from 4-concept chain"  # noqa: PLR2004

        report = await service.forget_mind(MIND_A)
        assert report.relations_purged == before

        after = await _table_count(pool, "SELECT COUNT(*) FROM relations", ())
        assert after == 0

    @pytest.mark.asyncio
    async def test_idempotent_on_empty_mind(
        self,
        service: MindForgetService,
    ) -> None:
        """Forgetting a mind that has no rows succeeds and reports zeros."""
        report = await service.forget_mind(MIND_A)
        assert report.concepts_purged == 0
        assert report.episodes_purged == 0
        assert report.relations_purged == 0
        assert report.consolidation_log_purged == 0
        assert report.total_brain_rows_purged == 0

    @pytest.mark.asyncio
    async def test_conversation_imports_cascade(
        self,
        pool: DatabasePool,
        service: MindForgetService,
        concept_repo: ConceptRepository,
        episode_repo: EpisodeRepository,
        relation_repo: RelationRepository,
    ) -> None:
        """Deleting episodes cascades conversation_imports via FK."""
        await _seed_mind(
            mind_id=MIND_A,
            concept_repo=concept_repo,
            episode_repo=episode_repo,
            relation_repo=relation_repo,
            pool=pool,
            n_concepts=0,
            n_episodes=2,
            n_consolidation=0,
        )

        # Find an episode_id to FK onto.
        async with pool.read() as conn:
            cursor = await conn.execute(
                "SELECT id FROM episodes WHERE mind_id = ? LIMIT 1",
                (str(MIND_A),),
            )
            row = await cursor.fetchone()
            assert row is not None
            episode_id = row[0]

        # Insert a conversation_imports row tied to that episode.
        async with pool.transaction() as conn:
            await conn.execute(
                """INSERT INTO conversation_imports
                   (source_hash, platform, mind_id, conversation_id, episode_id,
                    title, messages_count, concepts_learned, imported_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)""",
                (
                    "hash-x",
                    "chatgpt",
                    str(MIND_A),
                    "conv-x",
                    episode_id,
                    "title",
                    1,
                    0,
                ),
            )

        before = await _table_count(
            pool,
            "SELECT COUNT(*) FROM conversation_imports WHERE mind_id = ?",
            (str(MIND_A),),
        )
        assert before == 1

        report = await service.forget_mind(MIND_A)
        assert report.conversation_imports_purged == 1

        after = await _table_count(
            pool,
            "SELECT COUNT(*) FROM conversation_imports WHERE mind_id = ?",
            (str(MIND_A),),
        )
        assert after == 0


# ── ConsentLedger integration ────────────────────────────────────────


class TestConsentLedgerIntegration:
    @pytest.mark.asyncio
    async def test_ledger_purged_when_supplied(
        self,
        pool: DatabasePool,
        tmp_path: Path,
        concept_repo: ConceptRepository,
        episode_repo: EpisodeRepository,
        relation_repo: RelationRepository,
    ) -> None:
        ledger = ConsentLedger(path=tmp_path / "consent.jsonl")
        ledger.append(user_id="u1", action=ConsentAction.WAKE, mind_id=str(MIND_A))
        ledger.append(user_id="u2", action=ConsentAction.LISTEN, mind_id=str(MIND_A))
        ledger.append(user_id="u1", action=ConsentAction.WAKE, mind_id=str(MIND_B))

        await _seed_mind(
            mind_id=MIND_A,
            concept_repo=concept_repo,
            episode_repo=episode_repo,
            relation_repo=relation_repo,
            pool=pool,
            n_concepts=1,
            n_episodes=1,
            n_consolidation=0,
        )

        service = MindForgetService(brain_pool=pool, ledger=ledger)
        report = await service.forget_mind(MIND_A)

        assert report.consent_ledger_purged == 2  # noqa: PLR2004
        # Mind B records survive in the ledger.
        luna_after = ledger.history(mind_id=str(MIND_B))
        assert len(luna_after) == 1

    @pytest.mark.asyncio
    async def test_dry_run_does_not_touch_ledger(
        self,
        pool: DatabasePool,
        tmp_path: Path,
    ) -> None:
        ledger = ConsentLedger(path=tmp_path / "consent.jsonl")
        ledger.append(user_id="u1", action=ConsentAction.WAKE, mind_id=str(MIND_A))

        service = MindForgetService(brain_pool=pool, ledger=ledger)
        report = await service.forget_mind(MIND_A, dry_run=True)

        # Dry run reports zero ledger purge regardless of what would
        # be wiped (the brain count is still computed from live state).
        assert report.consent_ledger_purged == 0
        # The original record survives + no tombstone written.
        history = ledger.history(mind_id=str(MIND_A))
        assert len(history) == 1
        assert history[0].action is ConsentAction.WAKE

    @pytest.mark.asyncio
    async def test_no_ledger_means_zero_consent_purged(
        self,
        service: MindForgetService,
    ) -> None:
        """When ``ledger=None`` the report's consent count is always 0."""
        report = await service.forget_mind(MIND_A)
        assert report.consent_ledger_purged == 0


# ── Report shape ─────────────────────────────────────────────────────


class TestReport:
    def test_total_brain_rows_purged_sums_all_brain_tables(self) -> None:
        report = MindForgetReport(
            mind_id=MIND_A,
            concepts_purged=3,
            relations_purged=2,
            episodes_purged=4,
            concept_embeddings_purged=3,
            episode_embeddings_purged=4,
            conversation_imports_purged=1,
            consolidation_log_purged=2,
            conversations_purged=0,
            conversation_turns_purged=0,
            daily_stats_purged=0,
            consent_ledger_purged=99,  # excluded from total_brain_rows_purged
            dry_run=False,
        )
        assert report.total_brain_rows_purged == 19  # noqa: PLR2004

    def test_total_rows_purged_sums_all_relational_pools(self) -> None:
        """Brain + conversations + system; consent ledger is JSONL,
        excluded from the relational aggregate."""
        report = MindForgetReport(
            mind_id=MIND_A,
            concepts_purged=3,
            relations_purged=2,
            episodes_purged=4,
            concept_embeddings_purged=3,
            episode_embeddings_purged=4,
            conversation_imports_purged=1,
            consolidation_log_purged=2,
            conversations_purged=5,
            conversation_turns_purged=10,
            daily_stats_purged=7,
            consent_ledger_purged=99,
            dry_run=False,
        )
        # 19 brain + 15 conversations + 7 system = 41
        assert report.total_brain_rows_purged == 19  # noqa: PLR2004
        assert report.total_conversations_rows_purged == 15  # noqa: PLR2004
        assert report.total_system_rows_purged == 7  # noqa: PLR2004
        assert report.total_rows_purged == 41  # noqa: PLR2004

    def test_report_is_immutable(self) -> None:
        report = MindForgetReport(
            mind_id=MIND_A,
            concepts_purged=0,
            relations_purged=0,
            episodes_purged=0,
            concept_embeddings_purged=0,
            episode_embeddings_purged=0,
            conversation_imports_purged=0,
            consolidation_log_purged=0,
            conversations_purged=0,
            conversation_turns_purged=0,
            daily_stats_purged=0,
            consent_ledger_purged=0,
            dry_run=True,
        )
        with pytest.raises((AttributeError, TypeError)):
            report.concepts_purged = 99  # type: ignore[misc]


# ── Smoke: JSON round-trip of report (operator dashboards) ──────────


class TestReportSerialisation:
    def test_report_fields_are_json_serialisable(self) -> None:
        """Operator dashboards / CLI render the report; every field
        MUST survive ``json.dumps`` so the dashboard endpoint can
        return it as-is."""
        report = MindForgetReport(
            mind_id=MIND_A,
            concepts_purged=1,
            relations_purged=0,
            episodes_purged=2,
            concept_embeddings_purged=1,
            episode_embeddings_purged=2,
            conversation_imports_purged=0,
            consolidation_log_purged=0,
            conversations_purged=4,
            conversation_turns_purged=8,
            daily_stats_purged=2,
            consent_ledger_purged=3,
            dry_run=False,
        )
        payload = {
            "mind_id": str(report.mind_id),
            "concepts_purged": report.concepts_purged,
            "relations_purged": report.relations_purged,
            "episodes_purged": report.episodes_purged,
            "concept_embeddings_purged": report.concept_embeddings_purged,
            "episode_embeddings_purged": report.episode_embeddings_purged,
            "conversation_imports_purged": report.conversation_imports_purged,
            "consolidation_log_purged": report.consolidation_log_purged,
            "conversations_purged": report.conversations_purged,
            "conversation_turns_purged": report.conversation_turns_purged,
            "daily_stats_purged": report.daily_stats_purged,
            "consent_ledger_purged": report.consent_ledger_purged,
            "dry_run": report.dry_run,
        }
        encoded = json.dumps(payload)
        decoded = json.loads(encoded)
        assert decoded["mind_id"] == "aria"
        assert decoded["dry_run"] is False


# ── Conversations pool wipe (step 3) ────────────────────────────────


class TestConversationsPoolWipe:
    @pytest.mark.asyncio
    async def test_wipes_only_target_mind_conversations(
        self,
        pool: DatabasePool,
        conversations_pool: DatabasePool,
    ) -> None:
        """``conversations`` deletion cascades ``conversation_turns``
        via FK. Other mind's conversations / turns survive."""
        n_conv_a, n_turns_a = await _seed_conversations(
            pool=conversations_pool, mind_id=MIND_A, n_conversations=2, n_turns_per_conv=3
        )
        n_conv_b, n_turns_b = await _seed_conversations(
            pool=conversations_pool, mind_id=MIND_B, n_conversations=1, n_turns_per_conv=2
        )

        service = MindForgetService(
            brain_pool=pool,
            conversations_pool=conversations_pool,
        )
        report = await service.forget_mind(MIND_A)

        assert report.conversations_purged == n_conv_a
        assert report.conversation_turns_purged == n_turns_a

        # Mind A: conversations + turns gone.
        a_conv = await _table_count(
            conversations_pool,
            "SELECT COUNT(*) FROM conversations WHERE mind_id = ?",
            (str(MIND_A),),
        )
        assert a_conv == 0

        a_turns = await _table_count(
            conversations_pool,
            """SELECT COUNT(*) FROM conversation_turns
               WHERE conversation_id IN
                   (SELECT id FROM conversations WHERE mind_id = ?)""",
            (str(MIND_A),),
        )
        assert a_turns == 0

        # Mind B preserved.
        b_conv = await _table_count(
            conversations_pool,
            "SELECT COUNT(*) FROM conversations WHERE mind_id = ?",
            (str(MIND_B),),
        )
        assert b_conv == n_conv_b
        b_turns = await _table_count(
            conversations_pool,
            """SELECT COUNT(*) FROM conversation_turns
               WHERE conversation_id IN
                   (SELECT id FROM conversations WHERE mind_id = ?)""",
            (str(MIND_B),),
        )
        assert b_turns == n_turns_b

    @pytest.mark.asyncio
    async def test_no_conversations_pool_means_zero_counts(
        self,
        pool: DatabasePool,
    ) -> None:
        """When ``conversations_pool`` is None the report carries
        zeros for both conversations + turns. Step-2 wiring keeps
        working unchanged."""
        service = MindForgetService(brain_pool=pool)  # no conversations pool
        report = await service.forget_mind(MIND_A)
        assert report.conversations_purged == 0
        assert report.conversation_turns_purged == 0

    @pytest.mark.asyncio
    async def test_dry_run_leaves_conversations_intact(
        self,
        pool: DatabasePool,
        conversations_pool: DatabasePool,
    ) -> None:
        n_conv, n_turns = await _seed_conversations(
            pool=conversations_pool, mind_id=MIND_A, n_conversations=2, n_turns_per_conv=2
        )
        service = MindForgetService(
            brain_pool=pool,
            conversations_pool=conversations_pool,
        )
        report = await service.forget_mind(MIND_A, dry_run=True)

        assert report.dry_run is True
        assert report.conversations_purged == n_conv
        assert report.conversation_turns_purged == n_turns

        # No actual deletions.
        remaining = await _table_count(
            conversations_pool,
            "SELECT COUNT(*) FROM conversations WHERE mind_id = ?",
            (str(MIND_A),),
        )
        assert remaining == n_conv


# ── System pool wipe (step 3) ────────────────────────────────────────


class TestSystemPoolWipe:
    @pytest.mark.asyncio
    async def test_wipes_only_target_mind_daily_stats(
        self,
        pool: DatabasePool,
        system_pool: DatabasePool,
    ) -> None:
        n_a = await _seed_daily_stats(pool=system_pool, mind_id=MIND_A, n_days=3)
        n_b = await _seed_daily_stats(pool=system_pool, mind_id=MIND_B, n_days=2)

        service = MindForgetService(brain_pool=pool, system_pool=system_pool)
        report = await service.forget_mind(MIND_A)

        assert report.daily_stats_purged == n_a

        a_after = await _table_count(
            system_pool,
            "SELECT COUNT(*) FROM daily_stats WHERE mind_id = ?",
            (str(MIND_A),),
        )
        assert a_after == 0

        b_after = await _table_count(
            system_pool,
            "SELECT COUNT(*) FROM daily_stats WHERE mind_id = ?",
            (str(MIND_B),),
        )
        assert b_after == n_b

    @pytest.mark.asyncio
    async def test_does_not_touch_non_per_mind_tables(
        self,
        pool: DatabasePool,
        system_pool: DatabasePool,
    ) -> None:
        """``engine_state`` + ``persons`` + ``channel_mappings`` are
        NOT per-mind and MUST survive a mind wipe untouched."""
        async with system_pool.transaction() as conn:
            await conn.execute(
                "INSERT INTO engine_state (key, value) VALUES (?, ?)",
                ("global_setting", "preserved"),
            )
            await conn.execute(
                "INSERT INTO persons (id, name) VALUES (?, ?)",
                ("p-1", "Alice"),
            )

        service = MindForgetService(brain_pool=pool, system_pool=system_pool)
        await service.forget_mind(MIND_A)

        engine_count = await _table_count(
            system_pool,
            "SELECT COUNT(*) FROM engine_state WHERE key = ?",
            ("global_setting",),
        )
        assert engine_count == 1
        person_count = await _table_count(
            system_pool,
            "SELECT COUNT(*) FROM persons WHERE id = ?",
            ("p-1",),
        )
        assert person_count == 1

    @pytest.mark.asyncio
    async def test_no_system_pool_means_zero_count(
        self,
        pool: DatabasePool,
    ) -> None:
        service = MindForgetService(brain_pool=pool)  # no system pool
        report = await service.forget_mind(MIND_A)
        assert report.daily_stats_purged == 0


# ── End-to-end three-pool wipe (step 3) ──────────────────────────────


class TestThreePoolWipe:
    @pytest.mark.asyncio
    async def test_full_wipe_across_brain_conversations_system_ledger(
        self,
        pool: DatabasePool,
        conversations_pool: DatabasePool,
        system_pool: DatabasePool,
        tmp_path: Path,
        concept_repo: ConceptRepository,
        episode_repo: EpisodeRepository,
        relation_repo: RelationRepository,
    ) -> None:
        """End-to-end: every per-mind row across all four surfaces is
        wiped, the report's ``total_rows_purged`` matches reality, and
        the OTHER mind survives untouched."""
        ledger = ConsentLedger(path=tmp_path / "consent.jsonl")
        ledger.append(user_id="u1", action=ConsentAction.WAKE, mind_id=str(MIND_A))
        ledger.append(user_id="u1", action=ConsentAction.WAKE, mind_id=str(MIND_B))

        seeded_a = await _seed_mind(
            mind_id=MIND_A,
            concept_repo=concept_repo,
            episode_repo=episode_repo,
            relation_repo=relation_repo,
            pool=pool,
            n_concepts=3,
            n_episodes=2,
            n_consolidation=1,
        )
        seeded_b = await _seed_mind(
            mind_id=MIND_B,
            concept_repo=concept_repo,
            episode_repo=episode_repo,
            relation_repo=relation_repo,
            pool=pool,
            n_concepts=2,
            n_episodes=1,
            n_consolidation=0,
        )
        n_conv_a, n_turns_a = await _seed_conversations(
            pool=conversations_pool, mind_id=MIND_A, n_conversations=2, n_turns_per_conv=2
        )
        n_conv_b, _ = await _seed_conversations(
            pool=conversations_pool, mind_id=MIND_B, n_conversations=1, n_turns_per_conv=1
        )
        n_days_a = await _seed_daily_stats(pool=system_pool, mind_id=MIND_A, n_days=2)
        n_days_b = await _seed_daily_stats(pool=system_pool, mind_id=MIND_B, n_days=1)

        service = MindForgetService(
            brain_pool=pool,
            conversations_pool=conversations_pool,
            system_pool=system_pool,
            ledger=ledger,
        )
        report = await service.forget_mind(MIND_A)

        # Brain.
        assert report.concepts_purged == seeded_a["concepts"]
        assert report.episodes_purged == seeded_a["episodes"]
        assert report.relations_purged == seeded_a["relations"]
        assert report.consolidation_log_purged == seeded_a["consolidation"]
        # Conversations.
        assert report.conversations_purged == n_conv_a
        assert report.conversation_turns_purged == n_turns_a
        # System.
        assert report.daily_stats_purged == n_days_a
        # Ledger.
        assert report.consent_ledger_purged == 1

        # Aggregate sanity check.
        assert report.total_rows_purged == (
            report.total_brain_rows_purged
            + report.total_conversations_rows_purged
            + report.total_system_rows_purged
        )

        # Mind B fully preserved across every pool.
        b_concepts = await _table_count(
            pool,
            "SELECT COUNT(*) FROM concepts WHERE mind_id = ?",
            (str(MIND_B),),
        )
        assert b_concepts == seeded_b["concepts"]
        b_conv = await _table_count(
            conversations_pool,
            "SELECT COUNT(*) FROM conversations WHERE mind_id = ?",
            (str(MIND_B),),
        )
        assert b_conv == n_conv_b
        b_days = await _table_count(
            system_pool,
            "SELECT COUNT(*) FROM daily_stats WHERE mind_id = ?",
            (str(MIND_B),),
        )
        assert b_days == n_days_b
        b_ledger = ledger.history(mind_id=str(MIND_B))
        assert len(b_ledger) == 1
