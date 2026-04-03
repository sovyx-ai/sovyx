"""Tests for sovyx.cognitive.perceive — PerceivePhase."""

from __future__ import annotations

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from sovyx.cognitive.perceive import PerceivePhase, Perception
from sovyx.engine.errors import PerceptionError
from sovyx.engine.types import PerceptionType


def _perception(content: str = "Hello", **kwargs: object) -> Perception:
    defaults: dict[str, object] = {
        "id": "p1",
        "type": PerceptionType.USER_MESSAGE,
        "source": "telegram",
        "content": content,
    }
    defaults.update(kwargs)
    return Perception(**defaults)  # type: ignore[arg-type]


class TestProcess:
    """PerceivePhase.process()."""

    async def test_enriches_with_complexity(self) -> None:
        phase = PerceivePhase()
        p = _perception("Hello!")
        result = await phase.process(p)
        assert "complexity" in result.metadata
        assert isinstance(result.metadata["complexity"], float)

    async def test_strips_whitespace(self) -> None:
        phase = PerceivePhase()
        p = _perception("  hello  ")
        result = await phase.process(p)
        assert result.content == "hello"

    async def test_empty_content_raises(self) -> None:
        phase = PerceivePhase()
        p = _perception("")
        with pytest.raises(PerceptionError, match="empty"):
            await phase.process(p)

    async def test_whitespace_only_raises(self) -> None:
        phase = PerceivePhase()
        p = _perception("   ")
        with pytest.raises(PerceptionError, match="empty"):
            await phase.process(p)

    async def test_truncates_long_content(self) -> None:
        phase = PerceivePhase()
        p = _perception("x" * 20_000)
        result = await phase.process(p)
        assert len(result.content) == PerceivePhase.MAX_INPUT_CHARS


class TestClassifyComplexity:
    """Complexity classification."""

    def test_simple_greeting(self) -> None:
        score = PerceivePhase.classify_complexity("Hi!")
        assert score < 0.5  # noqa: PLR2004

    def test_complex_question(self) -> None:
        score = PerceivePhase.classify_complexity(
            "Why does quantum entanglement work and how does it compare "
            "to classical correlation? What are the implications?"
        )
        assert score > 0.5  # noqa: PLR2004

    def test_long_text_higher(self) -> None:
        short = PerceivePhase.classify_complexity("Hello")
        long = PerceivePhase.classify_complexity("word " * 60)
        assert long > short

    def test_multi_question_higher(self) -> None:
        single = PerceivePhase.classify_complexity("What is this?")
        multi = PerceivePhase.classify_complexity("What is this? How does it work? Why?")
        assert multi > single

    def test_simple_trigger_lower(self) -> None:
        score = PerceivePhase.classify_complexity("What's the weather?")
        assert score < 0.3  # noqa: PLR2004

    def test_range_0_to_1(self) -> None:
        assert 0.0 <= PerceivePhase.classify_complexity("") <= 1.0
        assert 0.0 <= PerceivePhase.classify_complexity("x" * 1000) <= 1.0


class TestPropertyBased:
    """Property-based tests."""

    @given(text=st.text(min_size=0, max_size=500))
    @settings(max_examples=30)
    def test_complexity_always_in_range(self, text: str) -> None:
        score = PerceivePhase.classify_complexity(text)
        assert 0.0 <= score <= 1.0
