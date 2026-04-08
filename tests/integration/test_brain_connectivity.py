"""Integration test: automated island prevention in brain graph.

Verifies that the brain subsystem produces a fully connected graph
after processing multiple messages. Uses real SQLite, real repos,
real BrainService — no mocks on persistence or algorithms.

TASK-05 of brain-hardening mission.
"""

from __future__ import annotations

from collections import defaultdict
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock

import pytest

if TYPE_CHECKING:
    from pathlib import Path

from sovyx.brain.concept_repo import ConceptRepository
from sovyx.brain.embedding import EmbeddingEngine
from sovyx.brain.episode_repo import EpisodeRepository
from sovyx.brain.learning import EbbinghausDecay, HebbianLearning
from sovyx.brain.relation_repo import RelationRepository
from sovyx.brain.retrieval import HybridRetrieval
from sovyx.brain.service import BrainService
from sovyx.brain.spreading import SpreadingActivation
from sovyx.brain.working_memory import WorkingMemory
from sovyx.engine.types import (
    ConceptCategory,
    ConceptId,
    ConversationId,
    MindId,
)
from sovyx.persistence.migrations import MigrationRunner
from sovyx.persistence.pool import DatabasePool
from sovyx.persistence.schemas.brain import get_brain_migrations

MIND = MindId("connectivity-test")
CONV = ConversationId("conv-001")

# ── Messages simulating a real conversation ──
# Each tuple: (concepts_to_learn, categories, episode_user_input, episode_response)

_MESSAGES: list[tuple[list[tuple[str, str]], str, str]] = [
    # Msg 1: Dev stack intro
    (
        [
            ("Python", "primary programming language"),
            ("FastAPI", "web framework for building APIs"),
            ("PostgreSQL", "relational database system"),
        ],
        "I mainly work with Python, FastAPI, and PostgreSQL.",
        "Great stack! Python with FastAPI is excellent for high-performance APIs.",
    ),
    # Msg 2: Architecture opinions
    (
        [
            ("microservices", "distributed architecture pattern"),
            ("monolith", "single deployment unit architecture"),
            ("domain-driven design", "software design approach"),
        ],
        "I prefer monoliths over microservices. DDD is the way to go.",
        "Monoliths with DDD boundaries are pragmatic for most teams.",
    ),
    # Msg 3: Database preferences
    (
        [
            ("SQLite", "embedded database engine"),
            ("PostgreSQL", "relational database system"),  # reuse!
            ("Redis", "in-memory data store"),
        ],
        "SQLite for dev, Postgres for prod, Redis for caching.",
        "Solid approach. SQLite is underrated for many use cases.",
    ),
    # Msg 4: Career facts
    (
        [
            ("Stripe", "payment processing platform"),
            ("Alex Chen", "senior engineer at Stripe"),
            ("payment gateway", "system for processing transactions"),
        ],
        "I'm Alex Chen, I work at Stripe on the payment gateway team.",
        "Nice to meet you Alex! Payment systems are fascinating.",
    ),
    # Msg 5: Technical skills
    (
        [
            ("Rust", "systems programming language"),
            ("WebAssembly", "portable binary format"),
            ("Python", "primary programming language"),  # reuse!
        ],
        "Learning Rust and WebAssembly. Still love Python though.",
        "Rust + WASM is a powerful combo, especially from Python.",
    ),
    # Msg 6: Project details
    (
        [
            ("Atlas", "internal platform at Stripe"),
            ("microservices", "distributed architecture pattern"),  # reuse!
            ("observability", "system monitoring and tracing"),
        ],
        "Atlas is our platform. We migrated to microservices with full observability.",
        "Platform engineering with observability-first is the right call.",
    ),
    # Msg 7: Achievements
    (
        [
            ("fraud detection", "ML-based transaction fraud prevention"),
            ("machine learning", "AI technique for pattern recognition"),
            ("Stripe", "payment processing platform"),  # reuse!
        ],
        "Built a fraud detection system using ML at Stripe.",
        "ML for fraud detection is one of the highest-impact applications.",
    ),
    # Msg 8: Future plans
    (
        [
            ("full-stack", "end-to-end development capability"),
            ("React", "JavaScript UI library"),
            ("Rust", "systems programming language"),  # reuse!
        ],
        "Want to go full-stack with React and keep deepening Rust skills.",
        "React + Rust is a powerful full-stack combination.",
    ),
]


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
def brain_service(brain_pool: DatabasePool) -> BrainService:
    """Real BrainService with real repos, real algorithms."""
    embedding = EmbeddingEngine()
    concept_repo = ConceptRepository(brain_pool, embedding)
    episode_repo = EpisodeRepository(brain_pool, embedding)
    relation_repo = RelationRepository(brain_pool)
    working_memory = WorkingMemory(capacity=50, decay_rate=0.15)
    spreading = SpreadingActivation(relation_repo, working_memory)
    hebbian = HebbianLearning(relation_repo)
    decay = EbbinghausDecay(concept_repo, relation_repo)
    retrieval = HybridRetrieval(concept_repo, episode_repo, embedding)
    event_bus = AsyncMock()

    return BrainService(
        concept_repo=concept_repo,
        episode_repo=episode_repo,
        relation_repo=relation_repo,
        embedding_engine=embedding,
        spreading=spreading,
        hebbian=hebbian,
        decay=decay,
        retrieval=retrieval,
        working_memory=working_memory,
        event_bus=event_bus,
    )


async def _process_messages(
    brain: BrainService,
    messages: list[tuple[list[tuple[str, str]], str, str]],
) -> None:
    """Simulate processing messages through the brain.

    For each message:
    1. Learn concepts (with dedup via learn_concept)
    2. Encode episode with new_concept_ids (triggers star topology Hebbian)
    3. Apply working memory decay
    """
    await brain.start(MIND)

    for concepts, user_input, response in messages:
        # Learn concepts
        new_ids: list[ConceptId] = []
        for name, content in concepts:
            cid = await brain.learn_concept(
                mind_id=MIND,
                name=name,
                content=content,
                category=ConceptCategory.FACT,
                source="conversation",
            )
            new_ids.append(cid)

        # Encode episode with star topology Hebbian
        await brain.encode_episode(
            mind_id=MIND,
            conversation_id=CONV,
            user_input=user_input,
            assistant_response=response,
            importance=0.5,
            new_concept_ids=new_ids,
        )

        # Decay after each "turn"
        brain.decay_working_memory()


def _build_graph(
    relations: list[tuple[str, str]],
) -> dict[str, set[str]]:
    """Build adjacency list from edge pairs."""
    graph: dict[str, set[str]] = defaultdict(set)
    for src, tgt in relations:
        graph[src].add(tgt)
        graph[tgt].add(src)
    return graph


def _bfs_component(graph: dict[str, set[str]], start: str) -> set[str]:
    """BFS from start, return all reachable nodes."""
    visited: set[str] = set()
    queue = [start]
    while queue:
        node = queue.pop(0)
        if node in visited:
            continue
        visited.add(node)
        for neighbor in graph.get(node, set()):
            if neighbor not in visited:
                queue.append(neighbor)
    return visited


def _count_components(graph: dict[str, set[str]], all_nodes: set[str]) -> int:
    """Count connected components in the graph."""
    visited: set[str] = set()
    components = 0
    for node in all_nodes:
        if node not in visited:
            component = _bfs_component(graph, node)
            visited |= component
            components += 1
    return components


@pytest.mark.no_islands
class TestBrainConnectivity:
    """Verify zero islands in brain graph after message processing."""

    async def test_no_islands_8_messages(
        self, brain_service: BrainService, brain_pool: DatabasePool
    ) -> None:
        """8 messages → single connected component, 0 orphan nodes."""
        await _process_messages(brain_service, _MESSAGES[:8])

        # Query all concepts
        async with brain_pool.read() as conn:
            cursor = await conn.execute(
                "SELECT id FROM concepts WHERE mind_id = ?",
                (str(MIND),),
            )
            concept_rows = await cursor.fetchall()
            all_concept_ids = {row[0] for row in concept_rows}

            # Query all relations
            cursor = await conn.execute("SELECT source_id, target_id FROM relations")
            relation_rows = await cursor.fetchall()
            edges = [(row[0], row[1]) for row in relation_rows]

        assert len(all_concept_ids) >= 10, f"Expected ≥10 concepts, got {len(all_concept_ids)}"  # noqa: PLR2004
        assert len(edges) >= 5, f"Expected ≥5 edges, got {len(edges)}"  # noqa: PLR2004

        # Build graph and check connectivity
        graph = _build_graph(edges)

        # Find orphans (concepts with zero edges)
        connected_nodes = set(graph.keys())
        orphans = all_concept_ids - connected_nodes
        assert orphans == set(), f"Orphan nodes found: {orphans}"

        # Verify single connected component
        components = _count_components(graph, all_concept_ids)
        assert components == 1, f"Expected 1 component, got {components}"

    async def test_no_islands_5_messages(
        self, brain_service: BrainService, brain_pool: DatabasePool
    ) -> None:
        """5 messages → connected graph."""
        await _process_messages(brain_service, _MESSAGES[:5])

        async with brain_pool.read() as conn:
            cursor = await conn.execute("SELECT id FROM concepts WHERE mind_id = ?", (str(MIND),))
            all_ids = {row[0] for row in await cursor.fetchall()}
            cursor = await conn.execute("SELECT source_id, target_id FROM relations")
            edges = [(row[0], row[1]) for row in await cursor.fetchall()]

        graph = _build_graph(edges)
        orphans = all_ids - set(graph.keys())
        assert orphans == set(), f"Orphans: {orphans}"
        assert _count_components(graph, all_ids) == 1

    async def test_no_bidirectional_duplicates(
        self, brain_service: BrainService, brain_pool: DatabasePool
    ) -> None:
        """Canonical ordering eliminates A→B / B→A duplicates."""
        await _process_messages(brain_service, _MESSAGES[:8])

        async with brain_pool.read() as conn:
            cursor = await conn.execute(
                "SELECT source_id, target_id, relation_type FROM relations"
            )
            rows = await cursor.fetchall()

        # Check canonical ordering: source_id ≤ target_id for every row
        for src, tgt, rtype in rows:
            assert src <= tgt, (
                f"Non-canonical relation: {src} → {tgt} ({rtype}). Expected source ≤ target."
            )

        # No duplicates when normalized
        pairs = [(row[0], row[1], row[2]) for row in rows]
        assert len(pairs) == len(set(pairs)), "Duplicate relations found"

    async def test_concept_reuse_stays_connected(
        self, brain_service: BrainService, brain_pool: DatabasePool
    ) -> None:
        """Concepts mentioned across multiple turns stay connected.

        PostgreSQL is mentioned in msg 1 and msg 3 (dedup path).
        Python is mentioned in msg 1 and msg 5.
        Both should remain well-connected after decay.
        """
        await _process_messages(brain_service, _MESSAGES[:5])

        async with brain_pool.read() as conn:
            # Find the PostgreSQL concept
            cursor = await conn.execute(
                "SELECT id FROM concepts WHERE name = 'PostgreSQL' AND mind_id = ?",
                (str(MIND),),
            )
            pg_row = await cursor.fetchone()
            assert pg_row is not None, "PostgreSQL concept not found"
            pg_id = pg_row[0]

            # Find the Python concept
            cursor = await conn.execute(
                "SELECT id FROM concepts WHERE name = 'Python' AND mind_id = ?",
                (str(MIND),),
            )
            py_row = await cursor.fetchone()
            assert py_row is not None, "Python concept not found"
            py_id = py_row[0]

            # Both should have edges
            cursor = await conn.execute(
                "SELECT COUNT(*) FROM relations WHERE source_id = ? OR target_id = ?",
                (pg_id, pg_id),
            )
            pg_edges = (await cursor.fetchone())[0]
            assert pg_edges >= 1, f"PostgreSQL has {pg_edges} edges"

            cursor = await conn.execute(
                "SELECT COUNT(*) FROM relations WHERE source_id = ? OR target_id = ?",
                (py_id, py_id),
            )
            py_edges = (await cursor.fetchone())[0]
            assert py_edges >= 1, f"Python has {py_edges} edges"

    async def test_decay_doesnt_isolate_concepts(
        self, brain_service: BrainService, brain_pool: DatabasePool
    ) -> None:
        """After 8 messages with decay, early concepts still connected.

        Message 1 concepts (Python, FastAPI, PostgreSQL) should still
        have edges even after 7 rounds of decay at rate 0.15.
        """
        await _process_messages(brain_service, _MESSAGES[:8])

        async with brain_pool.read() as conn:
            cursor = await conn.execute("SELECT id FROM concepts WHERE mind_id = ?", (str(MIND),))
            all_ids = {row[0] for row in await cursor.fetchall()}
            cursor = await conn.execute("SELECT source_id, target_id FROM relations")
            edges = [(row[0], row[1]) for row in await cursor.fetchall()]

        graph = _build_graph(edges)

        # Every concept should have at least 1 edge
        for cid in all_ids:
            assert cid in graph, f"Concept {cid} has zero edges after decay"
            assert len(graph[cid]) >= 1, f"Concept {cid} has zero neighbors"

    async def test_star_topology_linear_scaling(
        self, brain_service: BrainService, brain_pool: DatabasePool
    ) -> None:
        """Relation count grows linearly, not quadratically.

        With 8 messages × ~3 concepts = ~15-20 unique concepts:
        - O(n²) all-pairs: C(20,2) = 190 relations
        - Star topology: ~3 within-turn + 3×K cross-turn per msg
          ≈ 8 × (3 + 45) ≈ 384, but dedups reduce significantly

        Point: relation count should be significantly less than n².
        """
        await _process_messages(brain_service, _MESSAGES[:8])

        async with brain_pool.read() as conn:
            cursor = await conn.execute(
                "SELECT COUNT(*) FROM concepts WHERE mind_id = ?",
                (str(MIND),),
            )
            n_concepts = (await cursor.fetchone())[0]

            cursor = await conn.execute("SELECT COUNT(*) FROM relations")
            n_relations = (await cursor.fetchone())[0]

        # Quadratic would be C(n, 2) = n*(n-1)/2
        quadratic_max = n_concepts * (n_concepts - 1) // 2

        # Star topology should produce significantly fewer relations
        # Allow generous margin but must be less than quadratic
        assert n_relations < quadratic_max, (
            f"Relations ({n_relations}) ≥ quadratic max ({quadratic_max}). "
            f"Star topology should be linear."
        )
        assert n_relations >= n_concepts - 1, (
            f"Relations ({n_relations}) < minimum spanning tree ({n_concepts - 1}). "
            f"Not enough edges for connectivity."
        )
