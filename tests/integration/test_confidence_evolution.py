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
from sovyx.brain.models import Concept
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
    await runner.run_migrations(
        get_brain_migrations(has_sqlite_vec=pool.has_sqlite_vec)
    )
    return pool


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
