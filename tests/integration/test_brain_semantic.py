"""Comprehensive integration test — brain semantic enrichment end-to-end.

Sends realistic messages through the reflect phase and verifies the
full semantic pipeline produces a rich graph with:
- Multiple concept categories (≥4 of 7)
- Multiple relation types (≥2 of 7)
- Confidence growth on repeated concepts
- Emotional valence on opinionated messages
- Dynamic episode importance (not all 0.5)
- Episode summary populated
- Concept merging in consolidation
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock

import pytest

if TYPE_CHECKING:
    from pathlib import Path

from sovyx.brain.concept_repo import ConceptRepository
from sovyx.brain.consolidation import ConsolidationCycle
from sovyx.brain.embedding import EmbeddingEngine
from sovyx.brain.episode_repo import EpisodeRepository
from sovyx.brain.learning import EbbinghausDecay, HebbianLearning
from sovyx.brain.models import Concept
from sovyx.brain.relation_repo import RelationRepository
from sovyx.brain.working_memory import WorkingMemory
from sovyx.cognitive.perceive import Perception
from sovyx.cognitive.reflect import ReflectPhase
from sovyx.engine.types import (
    ConceptCategory,
    ConceptId,
    ConversationId,
    MindId,
    PerceptionType,
)
from sovyx.llm.models import LLMResponse
from sovyx.persistence.migrations import MigrationRunner
from sovyx.persistence.pool import DatabasePool
from sovyx.persistence.schemas.brain import get_brain_migrations

MIND = MindId("integration-test")
CONV = ConversationId("conv-1")


def _perception(content: str) -> Perception:
    return Perception(
        id="p1",
        type=PerceptionType.USER_MESSAGE,
        source="test",
        content=content,
    )


def _response(content: str = "OK") -> LLMResponse:
    return LLMResponse(
        content=content,
        model="test",
        tokens_in=10,
        tokens_out=5,
        latency_ms=100,
        cost_usd=0.0,
        finish_reason="stop",
        provider="test",
    )


# ── Realistic LLM extraction responses ────────────────────────────────


def _c(name: str, content: str, cat: str, sent: float) -> dict[str, object]:
    return {
        "name": name,
        "content": content,
        "category": cat,
        "sentiment": sent,
    }


def _r(a: str, b: str, rel: str) -> dict[str, str]:
    return {"a": a, "b": b, "relation": rel}


_MSG_1 = "My name is Guipe and I'm a software engineer from Brazil."
_EXT_1 = json.dumps(
    [
        _c("Guipe", "User's name is Guipe", "entity", 0.0),
        _c("Software Engineer", "Is a software engineer", "skill", 0.3),
        _c("Brazil", "User is from Brazil", "entity", 0.1),
    ]
)
_REL_1 = json.dumps(
    [
        _r("Guipe", "Software Engineer", "related_to"),
        _r("Guipe", "Brazil", "related_to"),
    ]
)

_MSG_2 = "I love PostgreSQL and I think ORMs are harmful for serious applications."
_EXT_2 = json.dumps(
    [
        _c("PostgreSQL", "Loves PostgreSQL", "preference", 0.8),
        _c("ORMs Harmful", "Thinks ORMs are harmful", "belief", -0.7),
    ]
)
_REL_2 = json.dumps(
    [
        _r("PostgreSQL", "ORMs Harmful", "contradicts"),
    ]
)

_MSG_3 = "Last month I migrated our entire stack to Kubernetes."
_EXT_3 = json.dumps(
    [
        _c("K8s Migration", "Migrated to Kubernetes", "event", 0.4),
        _c("Kubernetes", "Uses Kubernetes", "skill", 0.2),
    ]
)
_REL_3 = json.dumps(
    [
        _r("K8s Migration", "Kubernetes", "part_of"),
    ]
)

_MSG_4 = "I manage a team of 5 engineers and we use PostgreSQL for everything."
_EXT_4 = json.dumps(
    [
        _c("Team Lead", "Manages 5 engineers", "relationship", 0.2),
        _c("PostgreSQL", "Team uses PostgreSQL", "preference", 0.6),
    ]
)
_REL_4 = json.dumps(
    [
        _r("Team Lead", "PostgreSQL", "related_to"),
    ]
)

_MSG_5 = "Guipe is also an expert in Python and builds systems with FastAPI."
_EXT_5 = json.dumps(
    [
        _c("Python Expert", "Expert in Python", "skill", 0.5),
        _c("FastAPI", "Builds with FastAPI", "skill", 0.3),
        _c("Guipe", "Guipe is a developer", "entity", 0.0),
    ]
)
_REL_5 = json.dumps(
    [
        _r("Python Expert", "FastAPI", "part_of"),
        _r("Guipe", "Python Expert", "related_to"),
    ]
)


@pytest.fixture
async def brain_pool(tmp_path: Path) -> DatabasePool:
    pool = DatabasePool(db_path=tmp_path / "brain.db", read_pool_size=1)
    await pool.initialize()
    runner = MigrationRunner(pool)
    await runner.initialize()
    await runner.run_migrations(get_brain_migrations(has_sqlite_vec=pool.has_sqlite_vec))
    return pool


@pytest.fixture
async def repos(
    brain_pool: DatabasePool,
) -> tuple[ConceptRepository, RelationRepository, EpisodeRepository]:
    embedding = EmbeddingEngine()
    concepts = ConceptRepository(pool=brain_pool, embedding_engine=embedding)
    relations = RelationRepository(pool=brain_pool)
    episodes = EpisodeRepository(pool=brain_pool, embedding_engine=embedding)
    return concepts, relations, episodes


class TestBrainSemanticPipeline:
    """End-to-end semantic enrichment test."""

    async def test_full_pipeline(
        self,
        brain_pool: DatabasePool,
        repos: tuple[ConceptRepository, RelationRepository, EpisodeRepository],
    ) -> None:
        """5 messages → rich semantic graph."""
        concept_repo, relation_repo, episode_repo = repos

        # Build a real-ish brain service mock that delegates to real repos
        hebbian = HebbianLearning(
            relation_repo=relation_repo,
            concept_repo=concept_repo,
        )
        memory = WorkingMemory()
        events = AsyncMock()

        # We need a lightweight wrapper that uses real repos
        brain = AsyncMock()

        # Track concept IDs per name for dedup
        concept_map: dict[str, ConceptId] = {}

        async def mock_learn_concept(
            *,
            mind_id: MindId,
            name: str,
            content: str,
            category: ConceptCategory,
            source: str = "conversation",
            emotional_valence: float = 0.0,
            **kwargs: object,
        ) -> ConceptId:
            key = f"{name.lower()}:{category.value}"
            if key in concept_map:
                cid = concept_map[key]
                c = await concept_repo.get(cid)
                if c is not None:
                    if len(content) > len(c.content):
                        c.content = content
                    corr_raw = c.metadata.get("corroboration_count", 0)
                    corr = int(corr_raw) if isinstance(corr_raw, (int, float, str)) else 0
                    corr += 1
                    c.metadata["corroboration_count"] = corr
                    c.confidence = min(1.0, c.confidence + 0.1)
                    c.importance = min(1.0, c.importance + 0.05)
                    if emotional_valence != 0.0:
                        c.emotional_valence = max(
                            -1.0,
                            min(
                                1.0,
                                (c.emotional_valence * 2 + emotional_valence) / 3,
                            ),
                        )
                    await concept_repo.update(c)
                    await concept_repo.record_access(cid)
                    memory.activate(cid, 0.5)
                    return cid

            c = Concept(
                mind_id=mind_id,
                name=name,
                content=content,
                category=category,
                source=source,
                emotional_valence=max(-1.0, min(1.0, emotional_valence)),
            )
            cid = await concept_repo.create(c)
            concept_map[key] = cid
            memory.activate(cid, c.importance)
            return cid

        async def mock_strengthen(
            concept_ids: list[ConceptId],
            *,
            relation_types: dict[tuple[str, str], str] | None = None,
        ) -> None:
            await hebbian.strengthen(concept_ids, relation_types=relation_types)

        async def mock_encode_episode(
            *,
            mind_id: MindId,
            conversation_id: ConversationId,
            user_input: str,
            assistant_response: str,
            importance: float = 0.5,
            new_concept_ids: list[ConceptId] | None = None,
            emotional_valence: float = 0.0,
            emotional_arousal: float = 0.0,
            concepts_mentioned: list[ConceptId] | None = None,
            summary: str | None = None,
            **kwargs: object,
        ) -> str:
            from sovyx.brain.models import Episode

            ep = Episode(
                mind_id=mind_id,
                conversation_id=conversation_id,
                user_input=user_input,
                assistant_response=assistant_response,
                importance=importance,
                emotional_valence=max(-1.0, min(1.0, emotional_valence)),
                emotional_arousal=max(-1.0, min(1.0, emotional_arousal)),
                concepts_mentioned=concepts_mentioned or [],
                summary=summary,
            )
            return str(await episode_repo.create(ep))

        brain.learn_concept = AsyncMock(side_effect=mock_learn_concept)
        brain.strengthen_connection = AsyncMock(side_effect=mock_strengthen)
        brain.encode_episode = AsyncMock(side_effect=mock_encode_episode)
        brain.compute_novelty = AsyncMock(return_value=0.50)  # Default moderate novelty

        # Build LLM router mock — returns extraction, relation, summary
        # for each message in sequence
        messages = [
            (_MSG_1, _EXT_1, _REL_1),
            (_MSG_2, _EXT_2, _REL_2),
            (_MSG_3, _EXT_3, _REL_3),
            (_MSG_4, _EXT_4, _REL_4),
            (_MSG_5, _EXT_5, _REL_5),
        ]

        llm_responses: list[LLMResponse] = []
        for _msg, extraction, relations in messages:
            # Extraction response
            llm_responses.append(
                LLMResponse(
                    content=extraction,
                    model="gpt-4o-mini",
                    tokens_in=50,
                    tokens_out=50,
                    latency_ms=100,
                    cost_usd=0.0001,
                    finish_reason="stop",
                    provider="openai",
                )
            )
            # Relation classification response
            llm_responses.append(
                LLMResponse(
                    content=relations,
                    model="gpt-4o-mini",
                    tokens_in=30,
                    tokens_out=30,
                    latency_ms=80,
                    cost_usd=0.0001,
                    finish_reason="stop",
                    provider="openai",
                )
            )
            # Summary response
            llm_responses.append(
                LLMResponse(
                    content="Summary of the exchange.",
                    model="gpt-4o-mini",
                    tokens_in=20,
                    tokens_out=15,
                    latency_ms=60,
                    cost_usd=0.0001,
                    finish_reason="stop",
                    provider="openai",
                )
            )

        router = AsyncMock()
        router.generate = AsyncMock(side_effect=llm_responses)

        phase = ReflectPhase(brain, llm_router=router)

        # Process all 5 messages
        for msg, _ext, _rel in messages:
            await phase.process(
                _perception(msg),
                _response(f"Response to: {msg[:30]}"),
                MIND,
                CONV,
            )

        # ── ASSERTIONS ──────────────────────────────────────────

        # 1. ≥4 distinct categories
        all_concepts = await concept_repo.get_by_mind(MIND)
        categories_present = {c.category for c in all_concepts}
        assert len(categories_present) >= 4, (  # noqa: PLR2004
            f"Expected ≥4 categories, got {len(categories_present)}: "
            f"{[c.value for c in categories_present]}"
        )

        # Verify specific categories
        cat_values = {c.value for c in categories_present}
        assert "entity" in cat_values
        assert "skill" in cat_values
        assert "preference" in cat_values
        assert "belief" in cat_values

        # 2. ≥2 distinct relation types
        all_relation_types: set[str] = set()
        for concept in all_concepts:
            rels = await relation_repo.get_relations_for(concept.id)
            for r in rels:
                all_relation_types.add(r.relation_type.value)
        assert len(all_relation_types) >= 2, (  # noqa: PLR2004
            f"Expected ≥2 relation types, got: {all_relation_types}"
        )

        # 3. Confidence increases for re-mentioned concepts
        # "PostgreSQL" mentioned in msg 2 and msg 4
        pg_concepts = [c for c in all_concepts if "postgresql" in c.name.lower()]
        assert len(pg_concepts) >= 1
        pg = pg_concepts[0]
        assert pg.confidence > 0.5, (
            f"PostgreSQL confidence should be > 0.5 (corroborated), got {pg.confidence}"
        )

        # 4. Emotional valence is non-zero for opinionated messages
        # PostgreSQL: loved (0.8, 0.6) → should be positive
        assert pg.emotional_valence > 0.0, (
            f"PostgreSQL valence should be positive, got {pg.emotional_valence}"
        )

        # ORMs Harmful: -0.7 → should be negative
        orm_concepts = [c for c in all_concepts if "orm" in c.name.lower()]
        if orm_concepts:
            assert orm_concepts[0].emotional_valence < 0.0

        # 5. Importance grows with repeated mentions
        assert pg.importance > 0.5, f"PostgreSQL importance should grow, got {pg.importance}"

        # 6. Episode importance varies (not all 0.5)
        encode_calls = brain.encode_episode.call_args_list
        importances = [c.kwargs["importance"] for c in encode_calls]
        assert not all(abs(i - 0.5) < 0.01 for i in importances), (
            f"All importances are ~0.5: {importances}"
        )

        # 7. Episode summary is populated
        summaries = [c.kwargs.get("summary") for c in encode_calls]
        non_none_summaries = [s for s in summaries if s is not None]
        assert len(non_none_summaries) >= 3, (  # noqa: PLR2004
            f"Expected ≥3 summaries, got {len(non_none_summaries)}"
        )

        # 8. concepts_mentioned is populated
        cm_lists = [c.kwargs.get("concepts_mentioned") for c in encode_calls]
        non_none_cm = [cm for cm in cm_lists if cm is not None]
        assert len(non_none_cm) >= 4, (  # noqa: PLR2004
            f"Expected ≥4 concepts_mentioned lists, got {len(non_none_cm)}"
        )

        # 9. Consolidation merging works (add duplicate, then merge)
        # Create a near-duplicate for merging test
        dup = Concept(
            mind_id=MIND,
            name="PostgreSQL DB",
            content="PostgreSQL database",
            category=ConceptCategory.PREFERENCE,
            importance=0.3,
        )
        await concept_repo.create(dup)

        count_before = await concept_repo.count(MIND)
        decay = EbbinghausDecay(concept_repo=concept_repo, relation_repo=relation_repo)
        cycle = ConsolidationCycle(
            brain_service=brain,
            decay=decay,
            event_bus=events,
            concept_repo=concept_repo,
            relation_repo=relation_repo,
        )
        result = await cycle.run(MIND)
        count_after = await concept_repo.count(MIND)

        # Should have merged at least 1 (PostgreSQL + PostgreSQL DB)
        assert result.merged >= 1, f"Expected ≥1 merge, got {result.merged}"
        assert count_after < count_before

        # Print summary for debug
        final_concepts = await concept_repo.get_by_mind(MIND)
        print("\n=== Brain Semantic Pipeline Results ===")
        print(f"Concepts: {len(final_concepts)}")
        print(f"Categories: {[c.value for c in categories_present]}")
        print(f"Relation types: {all_relation_types}")
        print(f"PostgreSQL confidence: {pg.confidence:.2f}")
        print(f"PostgreSQL valence: {pg.emotional_valence:.2f}")
        print(f"Episode importances: {[round(i, 2) for i in importances]}")
        print(f"Merges: {result.merged}")
