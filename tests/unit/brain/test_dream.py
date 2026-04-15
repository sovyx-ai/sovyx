"""Tests for sovyx.brain.dream — DreamCycle.

Scope: the generative DREAM pass (pattern extraction → derived
concept learning → cross-episode Hebbian). Scheduler semantics are
in ``test_dream_scheduler.py``; episode repository extensions are in
``test_episode_repo.py::TestGetSince``.

The ``_DictEpisodeRepo`` stand-in skips aiosqlite entirely: DREAM
only reads episodes, so a synchronous dict backed by an async-looking
``get_since`` is enough to exercise all branches without pool setup.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock

import pytest

from sovyx.brain.dream import DreamCycle
from sovyx.brain.models import Episode
from sovyx.engine.events import DreamCompleted, EventBus
from sovyx.engine.types import ConceptId, ConversationId, EpisodeId, MindId
from sovyx.llm.models import LLMResponse

# ── Fixtures ────────────────────────────────────────────────────────


class _DictEpisodeRepo:
    """Minimal EpisodeRepository stand-in — backs ``get_since`` off a list."""

    def __init__(self, episodes: list[Episode]) -> None:
        self._episodes = episodes

    async def get_since(
        self,
        mind_id: MindId,
        since: datetime,
        limit: int = 500,
    ) -> list[Episode]:
        _ = (mind_id, limit)  # unused in tests; signature match matters
        return [e for e in self._episodes if e.created_at >= since]


def _make_episode(
    *,
    mind_id: str = "m",
    conv_id: str = "c",
    summary: str = "",
    user_input: str = "hi",
    assistant_response: str = "hello",
    concepts: list[str] | None = None,
    created_at: datetime | None = None,
) -> Episode:
    return Episode(
        id=EpisodeId(f"ep-{id(summary)}-{created_at}"),
        mind_id=MindId(mind_id),
        conversation_id=ConversationId(conv_id),
        user_input=user_input,
        assistant_response=assistant_response,
        summary=summary or None,
        concepts_mentioned=[ConceptId(c) for c in (concepts or [])],
        created_at=created_at or datetime.now(UTC),
    )


def _llm_with(content: str) -> AsyncMock:
    """Build an LLMRouter mock whose generate() returns ``content``."""
    router = AsyncMock()
    router.generate = AsyncMock(
        return_value=LLMResponse(
            content=content,
            model="fake",
            tokens_in=0,
            tokens_out=0,
            latency_ms=1,
            cost_usd=0.0,
            finish_reason="stop",
            provider="fake",
        ),
    )
    return router


def _build_cycle(
    *,
    episodes: list[Episode],
    llm_content: str = "[]",
    llm_raises: Exception | None = None,
    max_patterns: int = 5,
    lookback_hours: int = 24,
) -> tuple[DreamCycle, AsyncMock, AsyncMock, AsyncMock, EventBus]:
    """Wire a DreamCycle with mock collaborators. Returns the cycle + mocks."""
    brain = AsyncMock()
    brain.learn_concept = AsyncMock(side_effect=lambda **kw: ConceptId(f"id-{kw['name']}"))
    concepts = AsyncMock()
    hebbian = AsyncMock()
    hebbian.strengthen = AsyncMock(return_value=3)

    if llm_raises is not None:
        llm = AsyncMock()
        llm.generate = AsyncMock(side_effect=llm_raises)
    else:
        llm = _llm_with(llm_content)

    bus = EventBus()
    cycle = DreamCycle(
        brain_service=brain,
        episode_repo=_DictEpisodeRepo(episodes),
        concept_repo=concepts,
        hebbian=hebbian,
        llm_router=llm,
        event_bus=bus,
        lookback_hours=lookback_hours,
        max_patterns=max_patterns,
    )
    return cycle, brain, hebbian, llm, bus


@pytest.fixture()
def mind_id() -> MindId:
    return MindId("test-mind")


# ── Short-circuits ──────────────────────────────────────────────────


class TestDreamCycleShortCircuits:
    """Early-exit paths: disabled, thin window."""

    async def test_max_patterns_zero_short_circuits(self, mind_id: MindId) -> None:
        cycle, brain, hebbian, llm, _ = _build_cycle(
            episodes=[_make_episode(summary=f"s{i}") for i in range(10)],
            max_patterns=0,
        )
        result = await cycle.run(mind_id)

        assert result.patterns_found == 0
        assert result.concepts_derived == 0
        assert result.relations_strengthened == 0
        brain.learn_concept.assert_not_awaited()
        hebbian.strengthen.assert_not_awaited()
        llm.generate.assert_not_awaited()

    async def test_thin_window_skips_llm(self, mind_id: MindId) -> None:
        """< MIN_EPISODES → no LLM call, no learning, event still emitted."""
        cycle, brain, hebbian, llm, _ = _build_cycle(
            episodes=[_make_episode(summary="only one")],
        )
        result = await cycle.run(mind_id)

        assert result.episodes_analyzed == 1
        assert result.patterns_found == 0
        llm.generate.assert_not_awaited()
        brain.learn_concept.assert_not_awaited()
        hebbian.strengthen.assert_not_awaited()


# ── Pattern extraction ──────────────────────────────────────────────


class TestDreamCyclePatternExtraction:
    """LLM pattern extraction and derived concept materialization."""

    async def test_happy_path_learns_derived_concepts(self, mind_id: MindId) -> None:
        patterns = [
            {
                "name": "prefers async code",
                "content": "User consistently reaches for async/await.",
                "importance": 0.7,
            },
            {
                "name": "lives in curitiba",
                "content": "Mentions Curitiba weather repeatedly.",
                "importance": 0.5,
            },
        ]
        cycle, brain, _, llm, _ = _build_cycle(
            episodes=[_make_episode(summary=f"ep {i}") for i in range(5)],
            llm_content=json.dumps(patterns),
        )
        result = await cycle.run(mind_id)

        assert result.patterns_found == 2  # noqa: PLR2004
        assert result.concepts_derived == 2  # noqa: PLR2004
        assert brain.learn_concept.await_count == 2  # noqa: PLR2004
        llm.generate.assert_awaited_once()

        # Every learn_concept call must carry the dream source + 0.4 confidence.
        for call in brain.learn_concept.await_args_list:
            assert call.kwargs["source"] == "dream:pattern"
            assert call.kwargs["confidence"] == pytest.approx(0.4)

    async def test_caps_at_max_patterns(self, mind_id: MindId) -> None:
        """LLM returning too many patterns must be truncated to the cap."""
        patterns = [
            {"name": f"theme {i}", "content": f"body {i}", "importance": 0.5} for i in range(10)
        ]
        cycle, brain, _, _, _ = _build_cycle(
            episodes=[_make_episode(summary=f"ep {i}") for i in range(5)],
            llm_content=json.dumps(patterns),
            max_patterns=3,
        )
        result = await cycle.run(mind_id)

        assert result.patterns_found == 3  # noqa: PLR2004
        assert brain.learn_concept.await_count == 3  # noqa: PLR2004

    async def test_parse_error_yields_zero_patterns(self, mind_id: MindId) -> None:
        cycle, brain, _, _, _ = _build_cycle(
            episodes=[_make_episode(summary=f"ep {i}") for i in range(5)],
            llm_content="this is not json at all",
        )
        result = await cycle.run(mind_id)

        assert result.patterns_found == 0
        brain.learn_concept.assert_not_awaited()

    async def test_llm_exception_does_not_crash_cycle(self, mind_id: MindId) -> None:
        """Budget exhaustion / HTTP failure must collapse to 0 patterns, not crash."""
        cycle, brain, _, _, bus = _build_cycle(
            episodes=[_make_episode(summary=f"ep {i}") for i in range(5)],
            llm_raises=RuntimeError("budget exhausted"),
        )

        seen: list[DreamCompleted] = []

        async def handler(ev: object) -> None:
            seen.append(ev)  # type: ignore[arg-type]

        bus.subscribe(DreamCompleted, handler)
        result = await cycle.run(mind_id)

        assert result.patterns_found == 0
        brain.learn_concept.assert_not_awaited()
        assert len(seen) == 1

    async def test_code_fence_stripped(self, mind_id: MindId) -> None:
        """Some providers wrap JSON in ```json fences despite the prompt."""
        patterns = [
            {"name": "x", "content": "y", "importance": 0.5},
        ]
        fenced = "```json\n" + json.dumps(patterns) + "\n```"
        cycle, brain, _, _, _ = _build_cycle(
            episodes=[_make_episode(summary=f"ep {i}") for i in range(5)],
            llm_content=fenced,
        )
        result = await cycle.run(mind_id)

        assert result.patterns_found == 1
        assert brain.learn_concept.await_count == 1

    async def test_empty_pattern_fields_skipped(self, mind_id: MindId) -> None:
        """Patterns missing name or content are silently dropped."""
        patterns = [
            {"name": "", "content": "no name", "importance": 0.5},
            {"name": "no content", "content": "", "importance": 0.5},
            {"name": "good", "content": "solid", "importance": 0.5},
        ]
        cycle, brain, _, _, _ = _build_cycle(
            episodes=[_make_episode(summary=f"ep {i}") for i in range(5)],
            llm_content=json.dumps(patterns),
        )
        result = await cycle.run(mind_id)

        assert result.patterns_found == 3  # parse-time count  # noqa: PLR2004
        assert result.concepts_derived == 1  # only one survived validation
        assert brain.learn_concept.await_count == 1

    async def test_learn_concept_exception_does_not_abort_run(self, mind_id: MindId) -> None:
        """One failed learn_concept must not kill the rest."""
        patterns = [
            {"name": "a", "content": "ca", "importance": 0.5},
            {"name": "b", "content": "cb", "importance": 0.5},
            {"name": "c", "content": "cc", "importance": 0.5},
        ]
        cycle, brain, _, _, _ = _build_cycle(
            episodes=[_make_episode(summary=f"ep {i}") for i in range(5)],
            llm_content=json.dumps(patterns),
        )

        # Second call raises; the outer loop must swallow it and continue.
        brain.learn_concept.side_effect = [
            ConceptId("a-id"),
            RuntimeError("boom"),
            ConceptId("c-id"),
        ]

        result = await cycle.run(mind_id)
        assert result.patterns_found == 3  # noqa: PLR2004
        assert result.concepts_derived == 2  # noqa: PLR2004

    async def test_non_list_llm_output_yields_zero(self, mind_id: MindId) -> None:
        """LLM returning a dict or string instead of array → empty result."""
        cycle, brain, _, _, _ = _build_cycle(
            episodes=[_make_episode(summary=f"ep {i}") for i in range(5)],
            llm_content='{"not": "an array"}',
        )
        result = await cycle.run(mind_id)
        assert result.patterns_found == 0
        brain.learn_concept.assert_not_awaited()


# ── Cross-episode Hebbian ───────────────────────────────────────────


class TestDreamCycleCrossEpisodeHebbian:
    """Concepts that appear in ≥2 distinct episodes get relation boosts."""

    async def test_cooccurring_concepts_strengthened(self, mind_id: MindId) -> None:
        episodes = [
            _make_episode(
                summary="ep1",
                concepts=["c-alpha", "c-beta", "c-gamma"],
            ),
            _make_episode(
                summary="ep2",
                concepts=["c-alpha", "c-beta", "c-delta"],
            ),
            _make_episode(
                summary="ep3",
                concepts=["c-alpha", "c-gamma"],
            ),
        ]
        cycle, _, hebbian, _, _ = _build_cycle(episodes=episodes, llm_content="[]")
        await cycle.run(mind_id)

        hebbian.strengthen.assert_awaited_once()
        call = hebbian.strengthen.await_args
        passed_ids = [str(c) for c in call.kwargs["concept_ids"]]
        # alpha (3 eps), beta (2), gamma (2) — delta (1) must be excluded.
        assert "c-alpha" in passed_ids
        assert "c-beta" in passed_ids
        assert "c-gamma" in passed_ids
        assert "c-delta" not in passed_ids

    async def test_single_episode_concepts_excluded(self, mind_id: MindId) -> None:
        """Every concept appears in only one episode → no Hebbian call."""
        episodes = [_make_episode(summary=f"ep{i}", concepts=[f"c-unique-{i}"]) for i in range(4)]
        cycle, _, hebbian, _, _ = _build_cycle(episodes=episodes, llm_content="[]")
        await cycle.run(mind_id)
        hebbian.strengthen.assert_not_awaited()

    async def test_repeated_mention_within_episode_counted_once(self, mind_id: MindId) -> None:
        """Same concept listed twice in one episode counts as one episode-hit."""
        episodes = [
            _make_episode(
                summary="ep1",
                concepts=["c-a", "c-a", "c-a"],  # all within ep1
            ),
            _make_episode(
                summary="ep2",
                concepts=["c-b"],
            ),
        ]
        cycle, _, hebbian, _, _ = _build_cycle(episodes=episodes, llm_content="[]")
        await cycle.run(mind_id)
        # c-a ×3 → 1 episode-hit; c-b ×1 → 1. Neither ≥2 → no Hebbian.
        hebbian.strengthen.assert_not_awaited()

    async def test_hebbian_failure_degrades_gracefully(self, mind_id: MindId) -> None:
        episodes = [_make_episode(summary=f"ep{i}", concepts=["c-a", "c-b"]) for i in range(3)]
        cycle, _, hebbian, _, _ = _build_cycle(episodes=episodes, llm_content="[]")
        hebbian.strengthen.side_effect = RuntimeError("db is down")

        result = await cycle.run(mind_id)
        assert result.relations_strengthened == 0
        # Event still emitted — DREAM never crashes the scheduler.

    async def test_activation_damped_for_cross_episode(self, mind_id: MindId) -> None:
        """Cross-episode activations must be below 1.0 (weaker signal)."""
        episodes = [_make_episode(summary=f"ep{i}", concepts=["c-a", "c-b"]) for i in range(3)]
        cycle, _, hebbian, _, _ = _build_cycle(episodes=episodes, llm_content="[]")
        await cycle.run(mind_id)

        call = hebbian.strengthen.await_args
        activations = call.kwargs["activations"]
        assert all(v < 1.0 for v in activations.values())


# ── Event emission ──────────────────────────────────────────────────


class TestDreamCycleEventEmission:
    async def test_event_payload_shape(self, mind_id: MindId) -> None:
        episodes = [_make_episode(summary=f"ep{i}", concepts=["c-a", "c-b"]) for i in range(4)]
        pattern = [{"name": "x", "content": "y", "importance": 0.5}]
        cycle, _, hebbian, _, bus = _build_cycle(
            episodes=episodes,
            llm_content=json.dumps(pattern),
        )
        hebbian.strengthen.return_value = 7

        seen: list[Any] = []

        async def handler(ev: Any) -> None:
            seen.append(ev)

        bus.subscribe(DreamCompleted, handler)
        await cycle.run(mind_id)

        assert len(seen) == 1
        ev = seen[0]
        assert ev.patterns_found == 1
        assert ev.concepts_derived == 1
        assert ev.relations_strengthened == 7  # noqa: PLR2004
        assert ev.episodes_analyzed == 4  # noqa: PLR2004
        assert ev.duration_s >= 0


# ── Digest rendering edge cases ─────────────────────────────────────


class TestDreamCycleDigest:
    async def test_long_summary_truncated_in_prompt(self, mind_id: MindId) -> None:
        """Pathological summaries must not blow out the LLM prompt."""
        huge = "x" * 5000
        episodes = [_make_episode(summary=huge) for _ in range(5)]
        cycle, _, _, llm, _ = _build_cycle(episodes=episodes, llm_content="[]")
        await cycle.run(mind_id)

        call = llm.generate.await_args
        prompt = call.kwargs["messages"][0]["content"]
        # Each episode fits in ≤ 320 chars including timestamp + numbering.
        # 5 × 320 = 1600; the prompt plus boilerplate stays well under 5 KB.
        assert len(prompt) < 5000  # noqa: PLR2004

    async def test_missing_summary_falls_back_to_inputs(self, mind_id: MindId) -> None:
        episodes = [
            _make_episode(
                summary="",
                user_input=f"u{i}",
                assistant_response=f"a{i}",
            )
            for i in range(5)
        ]
        cycle, _, _, llm, _ = _build_cycle(episodes=episodes, llm_content="[]")
        await cycle.run(mind_id)

        call = llm.generate.await_args
        prompt = call.kwargs["messages"][0]["content"]
        assert "u0" in prompt
        assert "a0" in prompt


# ── Lookback window respected ───────────────────────────────────────


class TestDreamCycleLookback:
    async def test_lookback_filters_old_episodes(self, mind_id: MindId) -> None:
        """Episodes older than lookback_hours are excluded by get_since."""
        now = datetime.now(UTC)
        episodes = [
            _make_episode(summary="recent 1", created_at=now - timedelta(hours=1)),
            _make_episode(summary="recent 2", created_at=now - timedelta(hours=3)),
            _make_episode(summary="recent 3", created_at=now - timedelta(hours=5)),
            # The ones below are outside the 4-hour window.
            _make_episode(summary="old 1", created_at=now - timedelta(hours=48)),
            _make_episode(summary="old 2", created_at=now - timedelta(days=7)),
        ]
        cycle, _, _, _, _ = _build_cycle(
            episodes=episodes,
            llm_content="[]",
            lookback_hours=4,
        )
        result = await cycle.run(mind_id)

        # Only the first 2 fall inside a 4-hour lookback (ep at -5h is outside).
        assert result.episodes_analyzed == 2  # noqa: PLR2004
