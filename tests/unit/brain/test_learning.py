"""Tests for sovyx.brain.learning — Hebbian learning and Ebbinghaus decay."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import AsyncMock

import pytest

from sovyx.brain.concept_repo import ConceptRepository
from sovyx.brain.learning import EbbinghausDecay, HebbianLearning
from sovyx.brain.models import Concept
from sovyx.brain.relation_repo import RelationRepository
from sovyx.engine.types import ConceptId, MindId
from sovyx.persistence.migrations import MigrationRunner
from sovyx.persistence.pool import DatabasePool
from sovyx.persistence.schemas.brain import get_brain_migrations

if TYPE_CHECKING:
    from pathlib import Path

MIND = MindId("aria")


@pytest.fixture
async def pool(tmp_path: Path) -> DatabasePool:
    """Pool with brain schema."""
    p = DatabasePool(db_path=tmp_path / "brain.db", read_pool_size=1)
    await p.initialize()
    runner = MigrationRunner(p)
    await runner.initialize()
    await runner.run_migrations(get_brain_migrations(has_sqlite_vec=False))
    yield p  # type: ignore[misc]
    await p.close()


@pytest.fixture
def mock_embedding() -> AsyncMock:
    engine = AsyncMock()
    engine.has_embeddings = False
    return engine


@pytest.fixture
def concept_repo(pool: DatabasePool, mock_embedding: AsyncMock) -> ConceptRepository:
    return ConceptRepository(pool, mock_embedding)


@pytest.fixture
def relation_repo(pool: DatabasePool) -> RelationRepository:
    return RelationRepository(pool)


async def _seed_concepts(repo: ConceptRepository, *names: str) -> list[ConceptId]:
    """Create concepts and return their IDs."""
    ids: list[ConceptId] = []
    for name in names:
        c = Concept(mind_id=MIND, name=name)
        cid = await repo.create(c)
        ids.append(cid)
    return ids


class TestHebbianLearning:
    """Hebbian strengthening."""

    async def test_strengthen_two_concepts(
        self,
        relation_repo: RelationRepository,
        concept_repo: ConceptRepository,
    ) -> None:
        ids = await _seed_concepts(concept_repo, "A", "B")
        hebbian = HebbianLearning(relation_repo)

        count = await hebbian.strengthen(ids)
        assert count == 1

        # Relation should exist
        relations = await relation_repo.get_relations_for(ids[0])
        assert len(relations) == 1
        assert relations[0].weight > 0.5  # default 0.5 + delta

    async def test_strengthen_creates_relation(
        self,
        relation_repo: RelationRepository,
        concept_repo: ConceptRepository,
    ) -> None:
        ids = await _seed_concepts(concept_repo, "X", "Y")
        hebbian = HebbianLearning(relation_repo)

        await hebbian.strengthen(ids)
        neighbors = await relation_repo.get_neighbors(ids[0])
        assert len(neighbors) == 1

    async def test_weight_converges_to_one(
        self,
        relation_repo: RelationRepository,
        concept_repo: ConceptRepository,
    ) -> None:
        """Repeated strengthening converges to 1.0 but never exceeds."""
        ids = await _seed_concepts(concept_repo, "A", "B")
        hebbian = HebbianLearning(relation_repo, learning_rate=0.5)

        for _ in range(20):
            await hebbian.strengthen(ids)

        relations = await relation_repo.get_relations_for(ids[0])
        # Find the actual relation (not just co-occurrence created ones)
        assert len(relations) >= 1
        for rel in relations:
            assert rel.weight <= 1.0

    async def test_weight_never_exceeds_one(
        self,
        relation_repo: RelationRepository,
        concept_repo: ConceptRepository,
    ) -> None:
        """Even with high co_activation, weight stays ≤ 1.0."""
        ids = await _seed_concepts(concept_repo, "A", "B")
        hebbian = HebbianLearning(relation_repo, learning_rate=0.9)

        # High activation values
        activations = {ids[0]: 5.0, ids[1]: 5.0}
        await hebbian.strengthen(ids, activations=activations)

        relations = await relation_repo.get_relations_for(ids[0])
        for rel in relations:
            assert rel.weight <= 1.0

    async def test_single_concept_no_op(self, relation_repo: RelationRepository) -> None:
        hebbian = HebbianLearning(relation_repo)
        count = await hebbian.strengthen([ConceptId("c1")])
        assert count == 0

    async def test_empty_list_no_op(self, relation_repo: RelationRepository) -> None:
        hebbian = HebbianLearning(relation_repo)
        count = await hebbian.strengthen([])
        assert count == 0

    async def test_three_concepts_creates_three_pairs(
        self,
        relation_repo: RelationRepository,
        concept_repo: ConceptRepository,
    ) -> None:
        ids = await _seed_concepts(concept_repo, "A", "B", "C")
        hebbian = HebbianLearning(relation_repo)

        count = await hebbian.strengthen(ids)
        assert count == 3  # noqa: PLR2004  # A-B, A-C, B-C

    async def test_with_activations(
        self,
        relation_repo: RelationRepository,
        concept_repo: ConceptRepository,
    ) -> None:
        ids = await _seed_concepts(concept_repo, "A", "B")
        hebbian = HebbianLearning(relation_repo)

        activations = {ids[0]: 0.8, ids[1]: 0.3}
        await hebbian.strengthen(ids, activations=activations)

        relations = await relation_repo.get_relations_for(ids[0])
        assert len(relations) >= 1


class TestHebbianImportanceBoost:
    """Hebbian importance boost with scorer integration (TASK-14)."""

    async def test_scorer_based_boost_diminishing(
        self,
        relation_repo: RelationRepository,
        concept_repo: ConceptRepository,
    ) -> None:
        """With scorer, importance boost has diminishing returns."""
        from sovyx.brain.scoring import ImportanceScorer

        ids = await _seed_concepts(concept_repo, "A", "B")
        scorer = ImportanceScorer()
        hebbian = HebbianLearning(
            relation_repo, concept_repo=concept_repo, importance_scorer=scorer,
        )

        # Set high co-activation to trigger boost
        activations = {ids[0]: 0.9, ids[1]: 0.9}
        await hebbian.strengthen(ids, activations)

        # Check that importance was boosted
        a = await concept_repo.get(ids[0])
        assert a is not None
        assert a.importance > 0.5  # Default was 0.5

    async def test_scorer_boost_above_090_damped(
        self,
        relation_repo: RelationRepository,
        concept_repo: ConceptRepository,
    ) -> None:
        """Above 0.90, boost is 80% damped."""
        from sovyx.brain.scoring import ImportanceScorer

        ids = await _seed_concepts(concept_repo, "High", "High2")
        # Set concepts to high importance
        for cid in ids:
            c = await concept_repo.get(cid)
            assert c is not None
            c.importance = 0.95
            await concept_repo.update(c)

        scorer = ImportanceScorer()
        hebbian = HebbianLearning(
            relation_repo, concept_repo=concept_repo, importance_scorer=scorer,
        )
        activations = {ids[0]: 0.9, ids[1]: 0.9}
        await hebbian.strengthen(ids, activations)

        c = await concept_repo.get(ids[0])
        assert c is not None
        # Boost is heavily damped: should be barely above 0.95
        assert c.importance < 0.96

    async def test_fallback_flat_boost_without_scorer(
        self,
        relation_repo: RelationRepository,
        concept_repo: ConceptRepository,
    ) -> None:
        """Without scorer, falls back to flat +0.02 boost."""
        ids = await _seed_concepts(concept_repo, "C", "D")
        hebbian = HebbianLearning(
            relation_repo, concept_repo=concept_repo,
        )  # No scorer
        activations = {ids[0]: 0.9, ids[1]: 0.9}
        await hebbian.strengthen(ids, activations)

        c = await concept_repo.get(ids[0])
        assert c is not None
        assert c.importance == pytest.approx(0.52, abs=0.01)  # 0.5 + 0.02

    async def test_no_boost_below_threshold(
        self,
        relation_repo: RelationRepository,
        concept_repo: ConceptRepository,
    ) -> None:
        """Low co-activation → no importance boost."""
        from sovyx.brain.scoring import ImportanceScorer

        ids = await _seed_concepts(concept_repo, "E", "F")
        scorer = ImportanceScorer()
        hebbian = HebbianLearning(
            relation_repo, concept_repo=concept_repo, importance_scorer=scorer,
        )
        # Low co-activation (below 0.7 threshold)
        activations = {ids[0]: 0.3, ids[1]: 0.3}
        await hebbian.strengthen(ids, activations)

        c = await concept_repo.get(ids[0])
        assert c is not None
        assert c.importance == pytest.approx(0.5, abs=0.01)  # No boost


class TestEbbinghausDecay:
    """Ebbinghaus forgetting curve."""

    async def test_decay_reduces_importance(
        self,
        concept_repo: ConceptRepository,
        relation_repo: RelationRepository,
    ) -> None:
        c = Concept(mind_id=MIND, name="test", importance=0.8)
        cid = await concept_repo.create(c)

        decay = EbbinghausDecay(concept_repo, relation_repo)
        concepts_decayed, _ = await decay.apply_decay(MIND)
        assert concepts_decayed >= 1

        fetched = await concept_repo.get(cid)
        assert fetched is not None
        assert fetched.importance < 0.8

    async def test_access_count_reduces_decay(
        self,
        concept_repo: ConceptRepository,
        relation_repo: RelationRepository,
    ) -> None:
        """Higher access_count → less decay (rehearsal effect)."""
        # Concept A: never accessed
        ca = Concept(mind_id=MIND, name="never accessed", importance=0.8)
        cid_a = await concept_repo.create(ca)

        # Concept B: accessed 10 times
        cb = Concept(mind_id=MIND, name="well accessed", importance=0.8)
        cid_b = await concept_repo.create(cb)
        for _ in range(10):
            await concept_repo.record_access(cid_b)

        decay = EbbinghausDecay(concept_repo, relation_repo)
        await decay.apply_decay(MIND)

        a = await concept_repo.get(cid_a)
        b = await concept_repo.get(cid_b)
        assert a is not None
        assert b is not None
        # B should retain more importance than A
        assert b.importance > a.importance

    async def test_highly_accessed_nearly_immune(
        self,
        concept_repo: ConceptRepository,
        relation_repo: RelationRepository,
    ) -> None:
        """100 accesses → nearly immune to decay."""
        c = Concept(mind_id=MIND, name="veteran", importance=0.8)
        cid = await concept_repo.create(c)
        for _ in range(100):
            await concept_repo.record_access(cid)

        decay = EbbinghausDecay(concept_repo, relation_repo, decay_rate=0.1)
        await decay.apply_decay(MIND)

        fetched = await concept_repo.get(cid)
        assert fetched is not None
        # decay_factor = 1/(1 + 100*0.1) = 1/11 ≈ 0.09
        # loss = 0.8 * 0.1 * 0.09 = ~0.007
        assert fetched.importance > 0.78

    async def test_relation_decay(
        self,
        concept_repo: ConceptRepository,
        relation_repo: RelationRepository,
    ) -> None:
        """Relations also decay based on co_occurrence_count."""
        ids = await _seed_concepts(concept_repo, "A", "B")
        from sovyx.brain.models import Relation

        rel = Relation(source_id=ids[0], target_id=ids[1], weight=0.8)
        rid = await relation_repo.create(rel)

        decay = EbbinghausDecay(concept_repo, relation_repo)
        _, relations_decayed = await decay.apply_decay(MIND)
        assert relations_decayed >= 1

        fetched = await relation_repo.get(rid)
        assert fetched is not None
        assert fetched.weight < 0.8

    async def test_prune_weak_concepts(
        self,
        concept_repo: ConceptRepository,
        relation_repo: RelationRepository,
    ) -> None:
        """Concepts below min_strength are pruned."""
        weak = Concept(mind_id=MIND, name="weak", importance=0.005)
        strong = Concept(mind_id=MIND, name="strong", importance=0.9)
        await concept_repo.create(weak)
        cid_strong = await concept_repo.create(strong)

        decay = EbbinghausDecay(concept_repo, relation_repo, min_strength=0.01)
        concepts_pruned, _ = await decay.prune_weak(MIND)
        assert concepts_pruned == 1

        # Strong concept still exists
        assert await concept_repo.get(cid_strong) is not None
        assert await concept_repo.count(MIND) == 1

    async def test_prune_weak_relations(
        self,
        concept_repo: ConceptRepository,
        relation_repo: RelationRepository,
    ) -> None:
        """Relations below threshold are pruned."""
        ids = await _seed_concepts(concept_repo, "A", "B", "C")
        from sovyx.brain.models import Relation

        weak_rel = Relation(source_id=ids[0], target_id=ids[1], weight=0.001)
        strong_rel = Relation(source_id=ids[0], target_id=ids[2], weight=0.9)
        await relation_repo.create(weak_rel)
        rid_strong = await relation_repo.create(strong_rel)

        decay = EbbinghausDecay(concept_repo, relation_repo, min_strength=0.05)
        _, relations_pruned = await decay.prune_weak(MIND)
        assert relations_pruned == 1

        assert await relation_repo.get(rid_strong) is not None


class TestStarTopology:
    """Star topology Hebbian: strengthen_star()."""

    async def test_star_new_to_existing(
        self, concept_repo: ConceptRepository, relation_repo: RelationRepository
    ) -> None:
        """5 new + 15 existing → within-turn + cross-turn pairs."""
        new_names = [f"new-{i}" for i in range(5)]
        existing_names = [f"existing-{i}" for i in range(15)]
        new_ids = await _seed_concepts(concept_repo, *new_names)
        existing_ids = await _seed_concepts(concept_repo, *existing_names)

        activations = {cid: 0.5 for cid in [*new_ids, *existing_ids]}
        hebbian = HebbianLearning(relation_repo=relation_repo)
        count = await hebbian.strengthen_star(new_ids, existing_ids, activations)

        # Within-turn: C(5,2) = 10 pairs
        # Cross-turn: 5 new × 15 existing = 75 pairs
        # Existing reinforcement: 0 (no pre-existing relations)
        assert count == 85  # noqa: PLR2004

        # Every new concept should have edges
        for nid in new_ids:
            neighbors = await relation_repo.get_neighbors(nid)
            assert len(neighbors) >= 1, f"New concept {nid} has no neighbors"

    async def test_star_no_cap_50_concepts(
        self, concept_repo: ConceptRepository, relation_repo: RelationRepository
    ) -> None:
        """50 concepts total — no cap, all new connected to existing."""
        new_names = [f"n{i}" for i in range(5)]
        existing_names = [f"e{i}" for i in range(45)]

        new_ids = []
        for name in new_names:
            c = Concept(mind_id=MIND, name=name)
            new_ids.append(await concept_repo.create(c))
        existing_ids = []
        for name in existing_names:
            c = Concept(mind_id=MIND, name=name)
            existing_ids.append(await concept_repo.create(c))

        activations = {cid: float(i) * 0.01 for i, cid in enumerate([*existing_ids, *new_ids])}
        hebbian = HebbianLearning(relation_repo=relation_repo)
        count = await hebbian.strengthen_star(new_ids, existing_ids, activations)

        # Within: C(5,2)=10, Cross: 5×15=75 (K=15), existing: 0 pre-existing
        assert count == 85  # noqa: PLR2004

        # All new concepts connected
        for nid in new_ids:
            neighbors = await relation_repo.get_neighbors(nid)
            assert len(neighbors) >= 1

    async def test_star_existing_only_updates(
        self, concept_repo: ConceptRepository, relation_repo: RelationRepository
    ) -> None:
        """Existing-only reinforcement: updates pre-existing, no new spurious."""
        ids = await _seed_concepts(concept_repo, "A", "B", "C")

        # Pre-create a relation between A and B
        from sovyx.brain.models import Relation

        rel = Relation(source_id=ids[0], target_id=ids[1], weight=0.5)
        await relation_repo.create(rel)

        hebbian = HebbianLearning(relation_repo=relation_repo)
        # No new concepts, all existing
        count = await hebbian.strengthen_star([], ids)

        # Only A-B reinforced (pre-existing), not A-C or B-C
        assert count == 1

        # A-C should NOT have a relation
        relations_c = await relation_repo.get_relations_for(ids[2])
        assert len(relations_c) == 0

    async def test_star_empty_new(
        self, concept_repo: ConceptRepository, relation_repo: RelationRepository
    ) -> None:
        """No new concepts → only existing reinforcement (graceful)."""
        ids = await _seed_concepts(concept_repo, "X", "Y")
        hebbian = HebbianLearning(relation_repo=relation_repo)
        count = await hebbian.strengthen_star([], ids)
        # No pre-existing relations → 0
        assert count == 0

    async def test_star_empty_both(self, relation_repo: RelationRepository) -> None:
        """Both empty → no-op."""
        hebbian = HebbianLearning(relation_repo=relation_repo)
        count = await hebbian.strengthen_star([], [])
        assert count == 0

    async def test_star_vs_allpairs_connectivity(
        self, concept_repo: ConceptRepository, relation_repo: RelationRepository
    ) -> None:
        """Star topology produces a connected graph for mixed new+existing."""
        new_ids = await _seed_concepts(concept_repo, "alpha", "beta", "gamma")
        existing_ids = await _seed_concepts(concept_repo, "delta", "epsilon", "zeta")

        activations = {cid: 0.8 for cid in [*new_ids, *existing_ids]}
        hebbian = HebbianLearning(relation_repo=relation_repo)
        await hebbian.strengthen_star(new_ids, existing_ids, activations)

        # BFS from alpha should reach all new + existing it connected to
        visited: set[str] = set()
        queue = [new_ids[0]]
        while queue:
            node = queue.pop(0)
            if str(node) in visited:
                continue
            visited.add(str(node))
            neighbors = await relation_repo.get_neighbors(node, limit=50)
            for neighbor_id, _ in neighbors:
                if str(neighbor_id) not in visited:
                    queue.append(neighbor_id)

        # All new concepts must be reachable from alpha
        for nid in new_ids:
            assert str(nid) in visited, f"{nid} not reachable"
        # At least some existing concepts reachable
        existing_reached = sum(1 for eid in existing_ids if str(eid) in visited)
        assert existing_reached >= 1

    async def test_star_k_custom(
        self, concept_repo: ConceptRepository, relation_repo: RelationRepository
    ) -> None:
        """Custom K limits cross-turn connections."""
        new_ids = await _seed_concepts(concept_repo, "new1")
        existing_ids = await _seed_concepts(concept_repo, "ex1", "ex2", "ex3", "ex4", "ex5")

        activations = {cid: float(i) for i, cid in enumerate(existing_ids)}
        hebbian = HebbianLearning(relation_repo=relation_repo)
        count = await hebbian.strengthen_star(new_ids, existing_ids, activations, k=2)

        # Within: C(1,2)=0, Cross: 1×2=2 (K=2), existing: 0
        assert count == 2  # noqa: PLR2004

        neighbors = await relation_repo.get_neighbors(new_ids[0])
        assert len(neighbors) == 2  # noqa: PLR2004

    async def test_star_without_activations(
        self, concept_repo: ConceptRepository, relation_repo: RelationRepository
    ) -> None:
        """Star works without activations — uses positional K selection."""
        new_ids = await _seed_concepts(concept_repo, "n1", "n2")
        existing_ids = await _seed_concepts(concept_repo, "e1", "e2", "e3")

        hebbian = HebbianLearning(relation_repo=relation_repo)
        count = await hebbian.strengthen_star(new_ids, existing_ids)

        # Within: C(2,2)=1, Cross: 2×3=6 (K=15 > 3), existing: 0
        assert count == 7  # noqa: PLR2004

    async def test_star_existing_reinforce_without_activations(
        self, concept_repo: ConceptRepository, relation_repo: RelationRepository
    ) -> None:
        """Existing reinforcement works without activations (co_activation=1.0)."""
        ids = await _seed_concepts(concept_repo, "P", "Q", "R")
        from sovyx.brain.models import Relation

        # Pre-create P-Q relation
        await relation_repo.create(Relation(source_id=ids[0], target_id=ids[1], weight=0.5))

        hebbian = HebbianLearning(relation_repo=relation_repo)
        count = await hebbian.strengthen_star([], ids)
        assert count == 1  # only P-Q reinforced

    async def test_star_single_existing_no_reinforce(
        self, concept_repo: ConceptRepository, relation_repo: RelationRepository
    ) -> None:
        """Single existing concept — no reinforcement needed (< 2)."""
        ids = await _seed_concepts(concept_repo, "solo")
        new_ids = await _seed_concepts(concept_repo, "fresh")

        hebbian = HebbianLearning(relation_repo=relation_repo)
        count = await hebbian.strengthen_star(new_ids, ids)

        # Within: 0 (single new), Cross: 1×1=1, Existing: 0 (<2)
        assert count == 1


class TestLookupRelationType:
    """Tests for _lookup_relation_type static method."""

    def test_lookup_found(self) -> None:
        """Known pair returns the mapped RelationType."""
        from sovyx.engine.types import ConceptId, RelationType

        rt = HebbianLearning._lookup_relation_type(
            ConceptId("a"),
            ConceptId("b"),
            {("a", "b"): "part_of"},
        )
        assert rt == RelationType.PART_OF

    def test_lookup_canonical_order(self) -> None:
        """Lookup uses canonical (min, max) key ordering."""
        from sovyx.engine.types import ConceptId, RelationType

        rt = HebbianLearning._lookup_relation_type(
            ConceptId("z"),
            ConceptId("a"),
            {("a", "z"): "causes"},
        )
        assert rt == RelationType.CAUSES

    def test_lookup_missing_returns_none(self) -> None:
        """Missing pair returns None."""
        from sovyx.engine.types import ConceptId

        rt = HebbianLearning._lookup_relation_type(
            ConceptId("a"),
            ConceptId("b"),
            {("x", "y"): "part_of"},
        )
        assert rt is None

    def test_lookup_none_map_returns_none(self) -> None:
        """None relation_types returns None."""
        from sovyx.engine.types import ConceptId

        rt = HebbianLearning._lookup_relation_type(
            ConceptId("a"),
            ConceptId("b"),
            None,
        )
        assert rt is None

    def test_lookup_invalid_value_returns_none(self) -> None:
        """Invalid relation type string returns None."""
        from sovyx.engine.types import ConceptId

        rt = HebbianLearning._lookup_relation_type(
            ConceptId("a"),
            ConceptId("b"),
            {("a", "b"): "INVALID"},
        )
        assert rt is None

    def test_all_relation_types_resolvable(self) -> None:
        """Every RelationType value resolves correctly."""
        from sovyx.engine.types import ConceptId, RelationType

        for rtype in RelationType:
            rt = HebbianLearning._lookup_relation_type(
                ConceptId("a"),
                ConceptId("b"),
                {("a", "b"): rtype.value},
            )
            assert rt == rtype
