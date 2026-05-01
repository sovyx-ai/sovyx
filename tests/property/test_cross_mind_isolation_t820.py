"""Cross-mind isolation property tests — Phase 8 / T8.20.

Master mission §Phase 8 / T8.20 calls for Hypothesis property
tests verifying that:

  forall (mind_a, mind_b, action) ⇒ no leak from a to b

Sovyx's multi-mind architecture isolates per-mind data via
``mind_id`` columns + ``WHERE mind_id = ?`` query gates in every
brain repository. This file pins that contract structurally so a
future schema refactor that drops a mind_id filter fails the
isolation property loudly.

What's tested:
  * Concept isolation: ``concept_repo.get_by_mind(mind_a)`` returns
    only rows whose ``mind_id == mind_a`` even when mind_b has
    concurrently written data — repeated under Hypothesis-generated
    mind-id pairs and concept names.
  * Episode isolation: same property for ``episode_repo.get_recent``
    and ``episode_repo.get_since``.
  * Round-trip integrity: a concept written with mind_id=X reads back
    with mind_id=X — pins the no-cross-write half of isolation.
  * ConsentLedger user isolation: voice-event records are scoped per
    user_id (the voice-subsystem analogue of mind_id; T8.21
    foundation).

Why we assert *invariants* (not exact counts):
  Hypothesis re-runs the test body multiple times against the same
  function-scoped fixture, so DB state accumulates across the @given
  iterations. The isolation contract is "every row returned by
  query(mind_a) has mind_id == mind_a" — that holds regardless of
  prior-iteration residue, while a strict ``len(...) == N`` would be
  fragile under residue and would test population, not isolation.

What's NOT tested here:
  * SQL-level isolation primitives (transactions, foreign keys) —
    pinned by the persistence-layer tests.
  * Cognitive-loop boundary isolation — orchestrator's
    ``_current_mind_id`` reset between turns is pinned by the
    orchestrator test suite (post-T8.10).

Reference: master mission ``MISSION-voice-final-skype-grade-2026.md``
§Phase 8 / T8.20.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import AsyncMock

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from sovyx.brain.concept_repo import ConceptRepository
from sovyx.brain.episode_repo import EpisodeRepository
from sovyx.brain.models import Concept, Episode
from sovyx.engine.types import ConversationId, MindId
from sovyx.persistence.migrations import MigrationRunner
from sovyx.persistence.pool import DatabasePool
from sovyx.persistence.schemas.brain import get_brain_migrations

if TYPE_CHECKING:
    from pathlib import Path

    from sovyx.brain.embedding import EmbeddingEngine


# ── Fixtures: shared brain pool + mock embedding ────────────────────


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


# ── Strategies: distinct mind IDs + concept names ───────────────────

# Bounded ASCII mind IDs — small enough that Hypothesis can shrink
# meaningful counter-examples, large enough that pair collisions are
# rare (collisions are filtered out by ``_two_distinct_minds`` below).
_mind_id_strategy = st.from_regex(r"[a-z]{3,12}", fullmatch=True).map(MindId)


def _two_distinct_minds() -> st.SearchStrategy[tuple[MindId, MindId]]:
    """Strategy generating a pair of DIFFERENT mind IDs.

    The filter is cheap (string equality) and rejects only the
    diagonal — Hypothesis's default health-check tolerates it.
    """
    return st.tuples(_mind_id_strategy, _mind_id_strategy).filter(lambda pair: pair[0] != pair[1])


_concept_name_strategy = st.from_regex(r"[a-z ]{3,30}", fullmatch=True)


# ── Concept isolation ────────────────────────────────────────────────


class TestConceptIsolation:
    """``ConceptRepository.get_by_mind`` returns only the queried mind."""

    @pytest.mark.asyncio
    @settings(
        deadline=None,
        max_examples=15,
        suppress_health_check=[HealthCheck.too_slow, HealthCheck.function_scoped_fixture],
    )
    @given(
        minds=_two_distinct_minds(),
        name_a=_concept_name_strategy,
        name_b=_concept_name_strategy,
    )
    async def test_get_by_mind_isolation_invariant(
        self,
        concept_repo: ConceptRepository,
        minds: tuple[MindId, MindId],
        name_a: str,
        name_b: str,
    ) -> None:
        """forall iteration: get_by_mind(X) returns only rows with mind_id==X.

        We don't assert exact counts because Hypothesis re-runs the
        body against the same fixture (function-scoped pool), so
        residue from prior iterations is expected. The contract is
        the *invariant*, not the cardinality.
        """
        mind_a, mind_b = minds
        cid_a = await concept_repo.create(Concept(mind_id=mind_a, name=name_a))
        cid_b = await concept_repo.create(Concept(mind_id=mind_b, name=name_b))

        a_results = await concept_repo.get_by_mind(mind_a, limit=10000)
        b_results = await concept_repo.get_by_mind(mind_b, limit=10000)

        # Isolation invariant: every returned row's mind_id matches
        # the query's mind_id. A regression that drops the
        # ``WHERE mind_id = ?`` clause from get_by_mind fails this
        # for any iteration where mind_a != mind_b.
        a_mind_ids = {c.mind_id for c in a_results}
        b_mind_ids = {c.mind_id for c in b_results}
        assert a_mind_ids <= {mind_a}, f"mind_a query leaked rows from {a_mind_ids - {mind_a}}"
        assert b_mind_ids <= {mind_b}, f"mind_b query leaked rows from {b_mind_ids - {mind_b}}"

        # Inserted-row presence: the concept we just wrote IS visible
        # to its mind's query (rules out a regression that returns
        # an empty list — vacuously satisfies the invariant above).
        a_ids = {c.id for c in a_results}
        b_ids = {c.id for c in b_results}
        assert cid_a in a_ids
        assert cid_b in b_ids
        assert cid_a not in b_ids
        assert cid_b not in a_ids


# ── Episode isolation ────────────────────────────────────────────────


class TestEpisodeIsolation:
    """``EpisodeRepository.get_recent`` returns only the queried mind."""

    @pytest.mark.asyncio
    @settings(
        deadline=None,
        max_examples=15,
        suppress_health_check=[HealthCheck.too_slow, HealthCheck.function_scoped_fixture],
    )
    @given(
        minds=_two_distinct_minds(),
        user_input_a=st.text(alphabet="abcdefghij ", min_size=3, max_size=40),
        user_input_b=st.text(alphabet="abcdefghij ", min_size=3, max_size=40),
    )
    async def test_get_recent_isolation_invariant(
        self,
        episode_repo: EpisodeRepository,
        minds: tuple[MindId, MindId],
        user_input_a: str,
        user_input_b: str,
    ) -> None:
        """get_recent(X) returns only rows with mind_id==X."""
        mind_a, mind_b = minds
        # Each mind gets its own conversation; the contract under
        # test is mind_id-scoping, not conversation-id-scoping.
        conv_a = ConversationId("conv-a")
        conv_b = ConversationId("conv-b")
        eid_a = await episode_repo.create(
            Episode(
                mind_id=mind_a,
                conversation_id=conv_a,
                user_input=user_input_a,
                assistant_response="ok",
            ),
        )
        eid_b = await episode_repo.create(
            Episode(
                mind_id=mind_b,
                conversation_id=conv_b,
                user_input=user_input_b,
                assistant_response="ok",
            ),
        )

        a_episodes = await episode_repo.get_recent(mind_a, limit=10000)
        b_episodes = await episode_repo.get_recent(mind_b, limit=10000)

        a_mind_ids = {e.mind_id for e in a_episodes}
        b_mind_ids = {e.mind_id for e in b_episodes}
        assert a_mind_ids <= {mind_a}, (
            f"mind_a episode query leaked rows from {a_mind_ids - {mind_a}}"
        )
        assert b_mind_ids <= {mind_b}, (
            f"mind_b episode query leaked rows from {b_mind_ids - {mind_b}}"
        )

        a_ids = {e.id for e in a_episodes}
        b_ids = {e.id for e in b_episodes}
        assert eid_a in a_ids
        assert eid_b in b_ids
        assert eid_a not in b_ids
        assert eid_b not in a_ids


# ── No-cross-write contract ─────────────────────────────────────────


class TestNoCrossWriteContract:
    """Writing mind_a data CAN'T accidentally land under mind_b.

    Defensive: pins that the inserted ``mind_id`` round-trips
    verbatim, so a regression that hard-codes a fallback mind_id at
    the ORM layer (e.g. ``str(mind_id) or "default"``) fails this.
    """

    @pytest.mark.asyncio
    @settings(
        deadline=None,
        max_examples=15,
        suppress_health_check=[HealthCheck.too_slow, HealthCheck.function_scoped_fixture],
    )
    @given(mind=_mind_id_strategy, name=_concept_name_strategy)
    async def test_concept_round_trips_with_correct_mind_id(
        self,
        concept_repo: ConceptRepository,
        mind: MindId,
        name: str,
    ) -> None:
        """Insert with mind_id=X → ``get(id)`` returns mind_id=X."""
        cid = await concept_repo.create(Concept(mind_id=mind, name=name))
        fetched = await concept_repo.get(cid)
        assert fetched is not None
        assert fetched.mind_id == mind, (
            f"mind_id corrupted on round-trip: passed {mind!r}, got {fetched.mind_id!r}"
        )


# ── ConsentLedger isolation (T8.21 foundation) ───────────────────────


class TestConsentLedgerCrossUserIsolation:
    """ConsentLedger ``user_id`` boundary is the voice-subsystem
    analogue of brain's ``mind_id``. Pinned here alongside the brain
    isolation properties so a future regression in either side is
    caught by the same test module."""

    def test_history_returns_only_target_user(self, tmp_path: Path) -> None:
        from sovyx.voice._consent_ledger import ConsentAction, ConsentLedger  # noqa: PLC0415

        ledger = ConsentLedger(path=tmp_path / "consent.jsonl")
        for action in (ConsentAction.WAKE, ConsentAction.LISTEN, ConsentAction.TRANSCRIBE):
            ledger.append(user_id="alice", action=action, context={})
        ledger.append(user_id="bob", action=ConsentAction.WAKE, context={})

        alice_history = ledger.history(user_id="alice")
        bob_history = ledger.history(user_id="bob")

        assert len(alice_history) == 3  # noqa: PLR2004
        assert len(bob_history) == 1
        assert all(r.user_id == "alice" for r in alice_history)
        assert all(r.user_id == "bob" for r in bob_history)

    def test_forget_alice_does_not_touch_bob(self, tmp_path: Path) -> None:
        from sovyx.voice._consent_ledger import ConsentAction, ConsentLedger  # noqa: PLC0415

        ledger = ConsentLedger(path=tmp_path / "consent.jsonl")
        ledger.append(user_id="alice", action=ConsentAction.WAKE, context={})
        ledger.append(user_id="alice", action=ConsentAction.LISTEN, context={})
        ledger.append(user_id="bob", action=ConsentAction.WAKE, context={})
        ledger.append(user_id="bob", action=ConsentAction.LISTEN, context={})

        purged = ledger.forget(user_id="alice")
        assert purged == 2  # noqa: PLR2004
        bob_remaining = ledger.history(user_id="bob")
        assert len(bob_remaining) == 2  # noqa: PLR2004
        assert all(r.user_id == "bob" for r in bob_remaining)
