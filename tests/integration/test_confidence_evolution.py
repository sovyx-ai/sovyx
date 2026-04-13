"""Integration tests — confidence evolution on corroboration.

Tests the confidence increase logic when concepts are re-encountered,
using real SQLite (no mocks on persistence). Tests the dedup path
at the repository level since BrainService has circular import issues
in unit test context.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from pathlib import Path

from sovyx.brain.concept_repo import ConceptRepository
from sovyx.brain.embedding import EmbeddingEngine
from sovyx.brain.learning import HebbianLearning
from sovyx.brain.models import Concept
from sovyx.brain.relation_repo import RelationRepository
from sovyx.engine.types import ConceptCategory, ConceptId, MindId
from sovyx.persistence.migrations import MigrationRunner
from sovyx.persistence.pool import DatabasePool
from sovyx.persistence.schemas.brain import get_brain_migrations

MIND = MindId("test-mind")


@pytest.fixture
async def brain_pool(tmp_path: Path) -> DatabasePool:
    """Real SQLite pool with brain schema."""
    pool = DatabasePool(db_path=tmp_path / "brain.db", read_pool_size=1)
    await pool.initialize()
    runner = MigrationRunner(pool)
    await runner.initialize()
    await runner.run_migrations(get_brain_migrations(has_sqlite_vec=pool.has_sqlite_vec))
    yield pool  # type: ignore[misc]
    await pool.close()


@pytest.fixture
async def concept_repo(brain_pool: DatabasePool) -> ConceptRepository:
    """Real ConceptRepository with SQLite backend."""
    embedding = EmbeddingEngine()
    return ConceptRepository(pool=brain_pool, embedding_engine=embedding)


async def _learn_with_confidence(
    repo: ConceptRepository,
    name: str,
    content: str,
    category: ConceptCategory,
    emotional_valence: float = 0.0,
) -> ConceptId:
    """Mimic BrainService.learn_concept dedup path with confidence evolution.

    This replicates the exact logic from service.py for testing.
    """
    existing = await repo.search_by_text(name, MIND, limit=3)
    for concept, _rank in existing:
        if concept.name.lower() == name.lower() and concept.category == category:
            # Dedup path — same as service.py
            if len(content) > len(concept.content):
                concept.content = content

            # Confidence evolution
            corr_raw = concept.metadata.get("corroboration_count", 0)
            corr = int(corr_raw) if isinstance(corr_raw, (int, float, str)) else 0
            corr += 1
            concept.metadata["corroboration_count"] = corr
            concept.confidence = min(1.0, concept.confidence + 0.1)

            # Importance reinforcement
            concept.importance = min(1.0, concept.importance + 0.05)

            # Emotional valence weighted average (2:1 existing:new)
            if emotional_valence != 0.0:
                old_v = concept.emotional_valence
                concept.emotional_valence = max(
                    -1.0,
                    min(1.0, (old_v * 2 + emotional_valence) / 3),
                )

            await repo.update(concept)
            await repo.record_access(concept.id)
            return concept.id

    # New concept
    concept = Concept(
        mind_id=MIND,
        name=name,
        content=content,
        category=category,
        emotional_valence=max(-1.0, min(1.0, emotional_valence)),
    )
    return await repo.create(concept)


class TestConfidenceEvolution:
    """Confidence grows with repeated corroboration."""

    async def test_first_encounter_default_confidence(
        self, concept_repo: ConceptRepository
    ) -> None:
        """First time learning a concept → confidence = 0.5."""
        cid = await _learn_with_confidence(
            concept_repo, "Python", "User knows Python", ConceptCategory.SKILL
        )
        concept = await concept_repo.get(cid)
        assert concept is not None
        assert concept.confidence == pytest.approx(0.5, abs=0.01)
        assert concept.metadata.get("corroboration_count") is None

    async def test_second_encounter_increases_confidence(
        self, concept_repo: ConceptRepository
    ) -> None:
        """Re-encountering same concept → confidence 0.5 → 0.6."""
        await _learn_with_confidence(
            concept_repo, "Python", "User knows Python", ConceptCategory.SKILL
        )
        cid = await _learn_with_confidence(
            concept_repo, "Python", "User knows Python", ConceptCategory.SKILL
        )
        concept = await concept_repo.get(cid)
        assert concept is not None
        assert concept.confidence == pytest.approx(0.6, abs=0.01)
        assert concept.metadata.get("corroboration_count") == 1

    async def test_three_encounters_confidence_grows(
        self, concept_repo: ConceptRepository
    ) -> None:
        """3 encounters → confidence 0.5 → 0.6 → 0.7."""
        cid = ConceptId("")
        for _ in range(3):
            cid = await _learn_with_confidence(
                concept_repo,
                "Python",
                "User knows Python",
                ConceptCategory.SKILL,
            )
        concept = await concept_repo.get(cid)
        assert concept is not None
        assert concept.confidence == pytest.approx(0.7, abs=0.01)
        assert concept.metadata.get("corroboration_count") == 2

    async def test_six_encounters_confidence_capped(self, concept_repo: ConceptRepository) -> None:
        """7 encounters → confidence caps at 1.0."""
        cid = ConceptId("")
        for _ in range(7):
            cid = await _learn_with_confidence(
                concept_repo,
                "Python",
                "User knows Python",
                ConceptCategory.SKILL,
            )
        concept = await concept_repo.get(cid)
        assert concept is not None
        assert concept.confidence == pytest.approx(1.0, abs=0.01)
        assert concept.metadata.get("corroboration_count") == 6

    async def test_content_update_on_longer_content(self, concept_repo: ConceptRepository) -> None:
        """Longer content replaces shorter on dedup."""
        await _learn_with_confidence(
            concept_repo,
            "Python",
            "knows Python",
            ConceptCategory.SKILL,
        )
        cid = await _learn_with_confidence(
            concept_repo,
            "Python",
            "User is an expert Python developer with 10 years",
            ConceptCategory.SKILL,
        )
        concept = await concept_repo.get(cid)
        assert concept is not None
        assert "expert" in concept.content

    async def test_valence_weighted_average_on_dedup(
        self, concept_repo: ConceptRepository
    ) -> None:
        """Emotional valence uses weighted average (2:1 existing:new)."""
        await _learn_with_confidence(
            concept_repo,
            "GraphQL",
            "Uses GraphQL",
            ConceptCategory.SKILL,
            emotional_valence=0.0,
        )
        cid = await _learn_with_confidence(
            concept_repo,
            "GraphQL",
            "Uses GraphQL",
            ConceptCategory.SKILL,
            emotional_valence=0.9,
        )
        concept = await concept_repo.get(cid)
        assert concept is not None
        # (0.0 * 2 + 0.9) / 3 = 0.3
        assert concept.emotional_valence == pytest.approx(0.3, abs=0.05)

    async def test_different_category_no_dedup(self, concept_repo: ConceptRepository) -> None:
        """Same name but different category → separate concepts."""
        cid1 = await _learn_with_confidence(
            concept_repo,
            "Python",
            "Python programming language",
            ConceptCategory.SKILL,
        )
        cid2 = await _learn_with_confidence(
            concept_repo,
            "Python",
            "Python the snake",
            ConceptCategory.ENTITY,
        )
        assert str(cid1) != str(cid2)

    async def test_confidence_never_exceeds_one(self, concept_repo: ConceptRepository) -> None:
        """Property: confidence always in [0.0, 1.0] after any N."""
        cid = ConceptId("")
        for _ in range(20):
            cid = await _learn_with_confidence(
                concept_repo,
                "Python",
                "User knows Python",
                ConceptCategory.SKILL,
            )
        concept = await concept_repo.get(cid)
        assert concept is not None
        assert 0.0 <= concept.confidence <= 1.0

    async def test_metadata_survives_json_roundtrip(self, concept_repo: ConceptRepository) -> None:
        """corroboration_count persists through DB read/write cycle."""
        await _learn_with_confidence(concept_repo, "Rust", "knows Rust", ConceptCategory.SKILL)
        # Second encounter sets corroboration_count=1
        cid = await _learn_with_confidence(
            concept_repo, "Rust", "knows Rust", ConceptCategory.SKILL
        )
        # Third encounter: count should be 2 (read 1, increment to 2)
        cid = await _learn_with_confidence(
            concept_repo, "Rust", "knows Rust", ConceptCategory.SKILL
        )
        concept = await concept_repo.get(cid)
        assert concept is not None
        # Verify the metadata round-tripped through JSON correctly
        assert concept.metadata["corroboration_count"] == 2
        # Also check the raw JSON storage
        raw_meta = json.dumps(concept.metadata)
        parsed = json.loads(raw_meta)
        assert parsed["corroboration_count"] == 2


class TestImportanceReinforcement:
    """Importance grows with repeated access and high co-activation."""

    async def test_importance_grows_on_dedup(self, concept_repo: ConceptRepository) -> None:
        """Repeated encounters boost importance by 0.05 each."""
        # First encounter: default importance 0.5
        cid = await _learn_with_confidence(
            concept_repo, "Python", "knows Python", ConceptCategory.SKILL
        )
        c1 = await concept_repo.get(cid)
        assert c1 is not None
        initial = c1.importance

        # Second encounter: +0.05
        await _learn_with_confidence(concept_repo, "Python", "knows Python", ConceptCategory.SKILL)
        c2 = await concept_repo.get(cid)
        assert c2 is not None
        assert c2.importance == pytest.approx(initial + 0.05, abs=0.01)

    async def test_importance_capped_at_one(self, concept_repo: ConceptRepository) -> None:
        """Importance never exceeds 1.0."""
        cid = ConceptId("")
        for _ in range(30):
            cid = await _learn_with_confidence(
                concept_repo,
                "Python",
                "knows Python",
                ConceptCategory.SKILL,
            )
        concept = await concept_repo.get(cid)
        assert concept is not None
        assert 0.0 <= concept.importance <= 1.0

    async def test_boost_importance_method(self, concept_repo: ConceptRepository) -> None:
        """ConceptRepository.boost_importance works correctly."""
        concept = Concept(
            mind_id=MIND,
            name="Test",
            content="test",
            category=ConceptCategory.FACT,
        )
        cid = await concept_repo.create(concept)

        # Boost by 0.1
        await concept_repo.boost_importance(cid, 0.1)
        c = await concept_repo.get(cid)
        assert c is not None
        assert c.importance == pytest.approx(0.6, abs=0.01)

        # Boost to max
        await concept_repo.boost_importance(cid, 10.0)
        c = await concept_repo.get(cid)
        assert c is not None
        assert c.importance == pytest.approx(1.0, abs=0.01)

    async def test_boost_negative_delta_ignored(self, concept_repo: ConceptRepository) -> None:
        """Negative delta is clamped to 0."""
        concept = Concept(
            mind_id=MIND,
            name="Neg",
            content="test",
            category=ConceptCategory.FACT,
        )
        cid = await concept_repo.create(concept)
        await concept_repo.boost_importance(cid, -0.5)
        c = await concept_repo.get(cid)
        assert c is not None
        assert c.importance == pytest.approx(0.5, abs=0.01)

    async def test_hebbian_high_coactivation_boosts_importance(
        self, brain_pool: DatabasePool, concept_repo: ConceptRepository
    ) -> None:
        """High co-activation (>0.7) boosts both concepts' importance."""
        relation_repo = RelationRepository(pool=brain_pool)
        hebbian = HebbianLearning(
            relation_repo=relation_repo,
            concept_repo=concept_repo,
        )

        # Create two concepts
        c1 = Concept(
            mind_id=MIND,
            name="A",
            content="a",
            category=ConceptCategory.FACT,
        )
        c2 = Concept(
            mind_id=MIND,
            name="B",
            content="b",
            category=ConceptCategory.FACT,
        )
        id1 = await concept_repo.create(c1)
        id2 = await concept_repo.create(c2)

        # Strengthen with high activation (>0.7)
        activations = {id1: 0.9, id2: 0.85}
        await hebbian.strengthen([id1, id2], activations=activations)

        r1 = await concept_repo.get(id1)
        r2 = await concept_repo.get(id2)
        assert r1 is not None
        assert r2 is not None
        # Both should have importance > 0.5 (boosted by 0.02)
        assert r1.importance == pytest.approx(0.52, abs=0.01)
        assert r2.importance == pytest.approx(0.52, abs=0.01)

    async def test_hebbian_low_coactivation_no_boost(
        self, brain_pool: DatabasePool, concept_repo: ConceptRepository
    ) -> None:
        """Low co-activation (<=0.7) does NOT boost importance."""
        relation_repo = RelationRepository(pool=brain_pool)
        hebbian = HebbianLearning(
            relation_repo=relation_repo,
            concept_repo=concept_repo,
        )

        c1 = Concept(
            mind_id=MIND,
            name="X",
            content="x",
            category=ConceptCategory.FACT,
        )
        c2 = Concept(
            mind_id=MIND,
            name="Y",
            content="y",
            category=ConceptCategory.FACT,
        )
        id1 = await concept_repo.create(c1)
        id2 = await concept_repo.create(c2)

        # Low activation
        activations = {id1: 0.5, id2: 0.6}
        await hebbian.strengthen([id1, id2], activations=activations)

        r1 = await concept_repo.get(id1)
        r2 = await concept_repo.get(id2)
        assert r1 is not None
        assert r2 is not None
        # Importance unchanged at 0.5
        assert r1.importance == pytest.approx(0.5, abs=0.01)
        assert r2.importance == pytest.approx(0.5, abs=0.01)

    async def test_hebbian_no_concept_repo_no_boost(
        self, brain_pool: DatabasePool, concept_repo: ConceptRepository
    ) -> None:
        """Without concept_repo, no importance boost (backward compat)."""
        relation_repo = RelationRepository(pool=brain_pool)
        hebbian = HebbianLearning(
            relation_repo=relation_repo,
            # concept_repo NOT passed
        )

        c1 = Concept(
            mind_id=MIND,
            name="P",
            content="p",
            category=ConceptCategory.FACT,
        )
        c2 = Concept(
            mind_id=MIND,
            name="Q",
            content="q",
            category=ConceptCategory.FACT,
        )
        id1 = await concept_repo.create(c1)
        id2 = await concept_repo.create(c2)

        activations = {id1: 0.9, id2: 0.9}
        await hebbian.strengthen([id1, id2], activations=activations)

        r1 = await concept_repo.get(id1)
        r2 = await concept_repo.get(id2)
        assert r1 is not None
        assert r2 is not None
        # No boost without concept_repo
        assert r1.importance == pytest.approx(0.5, abs=0.01)
        assert r2.importance == pytest.approx(0.5, abs=0.01)
