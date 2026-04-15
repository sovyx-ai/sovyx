"""Tests for summarize_and_encode — summary-first conversation encoder."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest

from sovyx.engine.errors import LLMError
from sovyx.engine.types import ConceptCategory, ConceptId, EpisodeId, MindId
from sovyx.llm.models import LLMResponse
from sovyx.upgrade.conv_import import RawConversation, RawMessage, summarize_and_encode

# ── Helpers ────────────────────────────────────────────────────────


def _make_conv(
    messages: list[tuple[str, str]],
    *,
    platform: str = "chatgpt",
    conv_id: str = "conv-1",
    title: str = "Test conversation",
) -> RawConversation:
    """Build a RawConversation from (role, text) tuples."""
    return RawConversation(
        platform=platform,
        conversation_id=conv_id,
        title=title,
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
        messages=tuple(
            RawMessage(role=role, text=text, created_at=None)  # type: ignore[arg-type]
            for role, text in messages
        ),
    )


def _make_brain() -> AsyncMock:
    """Mock BrainService — returns stub IDs for encode/learn."""
    brain = AsyncMock()
    brain.learn_concept = AsyncMock(
        side_effect=lambda **kw: ConceptId(f"concept-{kw['name']}"),
    )
    brain.encode_episode = AsyncMock(return_value=EpisodeId("episode-1"))
    return brain


def _make_llm_response(payload: object | str) -> LLMResponse:
    """Build an LLMResponse with the given content.

    Pass a dict/list to have it JSON-encoded; pass a string to have it
    used verbatim (for testing malformed-JSON and fence-stripping).
    """
    content = payload if isinstance(payload, str) else json.dumps(payload)
    return LLMResponse(
        content=content,
        model="test-model",
        tokens_in=100,
        tokens_out=50,
        latency_ms=100,
        cost_usd=0.001,
        finish_reason="stop",
        provider="test",
    )


def _make_router(response: LLMResponse | Exception) -> AsyncMock:
    """Mock LLMRouter that returns ``response`` (or raises if exception)."""
    router = AsyncMock()
    if isinstance(response, Exception):
        router.generate = AsyncMock(side_effect=response)
    else:
        router.generate = AsyncMock(return_value=response)
    return router


MIND = MindId("test-mind")


# ── Happy path ─────────────────────────────────────────────────────


class TestHappyPath:
    @pytest.mark.asyncio()
    async def test_encodes_episode_and_concepts(self) -> None:
        """Valid LLM response → episode + concept_ids, no warnings."""
        conv = _make_conv([("user", "What's SQLite WAL?"), ("assistant", "A log.")])
        router = _make_router(
            _make_llm_response(
                {
                    "summary": "User asked about SQLite WAL mode.",
                    "concepts": [
                        {
                            "name": "SQLite",
                            "category": "entity",
                            "content": "DB lib",
                            "importance": 0.8,
                        },
                        {
                            "name": "WAL",
                            "category": "fact",
                            "content": "Log mode",
                            "importance": 0.7,
                        },
                    ],
                    "emotional_valence": 0.1,
                    "emotional_arousal": 0.2,
                    "importance": 0.6,
                }
            ),
        )
        brain = _make_brain()

        result = await summarize_and_encode(conv, brain, router, MIND)

        assert result.episode_id == EpisodeId("episode-1")
        assert len(result.concept_ids) == 2  # noqa: PLR2004
        assert result.warnings == ()

        # Both concepts learned with correct categories.
        assert brain.learn_concept.await_count == 2  # noqa: PLR2004
        concept_calls = [c.kwargs for c in brain.learn_concept.await_args_list]
        assert concept_calls[0]["name"] == "SQLite"
        assert concept_calls[0]["category"] == ConceptCategory.ENTITY
        assert concept_calls[0]["source"] == "import:chatgpt"

        # Episode carries the summary + derived emotional metadata.
        brain.encode_episode.assert_awaited_once()
        ep_kw = brain.encode_episode.await_args.kwargs
        assert ep_kw["summary"] == "User asked about SQLite WAL mode."
        assert ep_kw["importance"] == pytest.approx(0.6)
        assert ep_kw["emotional_valence"] == pytest.approx(0.1)

    @pytest.mark.asyncio()
    async def test_user_and_assistant_text_passed_to_episode(self) -> None:
        """First user + last assistant text land on the Episode."""
        conv = _make_conv(
            [
                ("user", "First question"),
                ("assistant", "Middle reply"),
                ("user", "Follow-up"),
                ("assistant", "Final reply"),
            ]
        )
        router = _make_router(
            _make_llm_response(
                {
                    "summary": "x",
                    "concepts": [],
                    "emotional_valence": 0.0,
                    "emotional_arousal": 0.0,
                    "importance": 0.5,
                }
            ),
        )
        brain = _make_brain()
        await summarize_and_encode(conv, brain, router, MIND)

        ep_kw = brain.encode_episode.await_args.kwargs
        assert ep_kw["user_input"] == "First question"
        assert ep_kw["assistant_response"] == "Final reply"


# ── Fallback path ──────────────────────────────────────────────────


class TestFallbackWithoutLLM:
    @pytest.mark.asyncio()
    async def test_none_router_uses_fallback(self) -> None:
        """Without an LLM router the encoder still creates an Episode."""
        conv = _make_conv([("user", "hi"), ("assistant", "hello")])
        brain = _make_brain()
        result = await summarize_and_encode(conv, brain, llm_router=None, mind_id=MIND)

        assert result.episode_id == EpisodeId("episode-1")
        assert result.concept_ids == ()  # no concepts in fallback
        assert any("fallback" in w for w in result.warnings)
        brain.encode_episode.assert_awaited_once()
        # Fallback importance is conservative.
        assert brain.encode_episode.await_args.kwargs["importance"] == pytest.approx(0.3)

    @pytest.mark.asyncio()
    async def test_llm_error_falls_back_not_raise(self) -> None:
        """LLMError → warning + fallback path, not propagation."""
        conv = _make_conv([("user", "q"), ("assistant", "a")])
        router = _make_router(LLMError("provider exhausted"))
        brain = _make_brain()

        result = await summarize_and_encode(conv, brain, router, MIND)

        assert result.episode_id == EpisodeId("episode-1")
        assert any("LLM failure" in w for w in result.warnings)

    @pytest.mark.asyncio()
    async def test_malformed_json_falls_back(self) -> None:
        """LLM returns non-JSON text → fallback + warning."""
        conv = _make_conv([("user", "q"), ("assistant", "a")])
        router = _make_router(_make_llm_response("not json at all"))
        brain = _make_brain()

        result = await summarize_and_encode(conv, brain, router, MIND)

        assert result.episode_id == EpisodeId("episode-1")
        assert any("JSON parse" in w for w in result.warnings)

    @pytest.mark.asyncio()
    async def test_empty_summary_falls_back(self) -> None:
        """LLM returns JSON but no summary → fallback."""
        conv = _make_conv([("user", "q"), ("assistant", "a")])
        router = _make_router(
            _make_llm_response({"summary": "", "concepts": []}),
        )
        brain = _make_brain()

        result = await summarize_and_encode(conv, brain, router, MIND)
        assert any("empty summary" in w for w in result.warnings)


# ── Content validation ────────────────────────────────────────────


class TestSanitisation:
    @pytest.mark.asyncio()
    async def test_strips_markdown_fences(self) -> None:
        """LLM wraps JSON in ```json fences — must still parse."""
        conv = _make_conv([("user", "q"), ("assistant", "a")])
        router = _make_router(
            _make_llm_response(
                '```json\n{"summary":"ok","concepts":[],'
                '"emotional_valence":0,"emotional_arousal":0,"importance":0.5}\n```',
            ),
        )
        brain = _make_brain()
        result = await summarize_and_encode(conv, brain, router, MIND)
        assert brain.encode_episode.await_args.kwargs["summary"] == "ok"
        assert result.warnings == ()

    @pytest.mark.asyncio()
    async def test_unknown_concept_category_maps_to_fact(self) -> None:
        """Category outside the enum → 'fact' with a warning."""
        conv = _make_conv([("user", "q"), ("assistant", "a")])
        router = _make_router(
            _make_llm_response(
                {
                    "summary": "s",
                    "concepts": [
                        {
                            "name": "X",
                            "category": "nonsense_category",
                            "content": "c",
                            "importance": 0.5,
                        },
                    ],
                    "emotional_valence": 0.0,
                    "emotional_arousal": 0.0,
                    "importance": 0.5,
                }
            ),
        )
        brain = _make_brain()
        result = await summarize_and_encode(conv, brain, router, MIND)

        brain.learn_concept.assert_awaited_once()
        assert brain.learn_concept.await_args.kwargs["category"] == ConceptCategory.FACT
        assert any("unknown concept category" in w for w in result.warnings)

    @pytest.mark.asyncio()
    async def test_clamps_out_of_range_importance(self) -> None:
        """Importance/valence/arousal outside their ranges are clamped."""
        conv = _make_conv([("user", "q"), ("assistant", "a")])
        router = _make_router(
            _make_llm_response(
                {
                    "summary": "s",
                    "concepts": [],
                    "emotional_valence": 5.0,  # > 1.0
                    "emotional_arousal": -3.0,  # < 0.0
                    "importance": 1.5,  # > 1.0
                }
            ),
        )
        brain = _make_brain()
        await summarize_and_encode(conv, brain, router, MIND)

        ep_kw = brain.encode_episode.await_args.kwargs
        assert ep_kw["importance"] == pytest.approx(1.0)
        assert ep_kw["emotional_valence"] == pytest.approx(1.0)
        assert ep_kw["emotional_arousal"] == pytest.approx(0.0)

    @pytest.mark.asyncio()
    async def test_caps_concept_count(self) -> None:
        """LLM returns >5 concepts — only the first 5 survive."""
        conv = _make_conv([("user", "q"), ("assistant", "a")])
        router = _make_router(
            _make_llm_response(
                {
                    "summary": "s",
                    "concepts": [
                        {"name": f"c{i}", "category": "fact", "content": "x", "importance": 0.5}
                        for i in range(10)
                    ],
                    "emotional_valence": 0.0,
                    "emotional_arousal": 0.0,
                    "importance": 0.5,
                }
            ),
        )
        brain = _make_brain()
        result = await summarize_and_encode(conv, brain, router, MIND)
        assert len(result.concept_ids) == 5  # noqa: PLR2004


# ── Long-conversation truncation ───────────────────────────────────


class TestTruncation:
    @pytest.mark.asyncio()
    async def test_long_conversation_sends_head_plus_tail(self) -> None:
        """Conversations >30 turns get head+tail+middle-omitted marker."""
        messages = []
        for i in range(40):
            role = "user" if i % 2 == 0 else "assistant"
            messages.append((role, f"turn {i} text"))
        conv = _make_conv(messages)

        router = _make_router(
            _make_llm_response(
                {
                    "summary": "s",
                    "concepts": [],
                    "emotional_valence": 0.0,
                    "emotional_arousal": 0.0,
                    "importance": 0.5,
                }
            ),
        )
        brain = _make_brain()
        await summarize_and_encode(conv, brain, router, MIND)

        # Inspect the prompt text actually sent.
        prompt = router.generate.await_args.kwargs["messages"][0]["content"]
        assert "turns omitted for brevity" in prompt
        # Head turn "turn 0" present, tail turn "turn 39" present,
        # middle turn "turn 20" absent.
        assert "turn 0 text" in prompt
        assert "turn 39 text" in prompt
        assert "turn 20 text" not in prompt
