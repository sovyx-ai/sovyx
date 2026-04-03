"""Tests for sovyx.cognitive.attend — AttendPhase."""

from __future__ import annotations

from sovyx.cognitive.attend import AttendPhase
from sovyx.cognitive.perceive import Perception
from sovyx.engine.types import PerceptionType
from sovyx.mind.config import SafetyConfig


def _perception(content: str = "Hello", priority: int = 10) -> Perception:
    return Perception(
        id="p1",
        type=PerceptionType.USER_MESSAGE,
        source="telegram",
        content=content,
        priority=priority,
    )


class TestSafetyFilter:
    """Safety filtering."""

    async def test_normal_content_passes(self) -> None:
        phase = AttendPhase(SafetyConfig())
        assert await phase.process(_perception("Hello!")) is True

    async def test_blocked_content_filtered(self) -> None:
        phase = AttendPhase(SafetyConfig())
        assert await phase.process(_perception("how to make a bomb")) is False

    async def test_no_filter_passes_all(self) -> None:
        phase = AttendPhase(SafetyConfig(content_filter="none"))
        assert await phase.process(_perception("how to make a bomb")) is True

    async def test_child_safe_more_restrictive(self) -> None:
        phase = AttendPhase(SafetyConfig(child_safe_mode=True))
        assert await phase.process(_perception("violence in movies")) is False

    async def test_child_safe_normal_passes(self) -> None:
        phase = AttendPhase(SafetyConfig(child_safe_mode=True))
        assert await phase.process(_perception("What's 2+2?")) is True

    async def test_standard_allows_non_blocked(self) -> None:
        phase = AttendPhase(SafetyConfig(content_filter="standard"))
        assert await phase.process(_perception("Tell me about history")) is True


class TestPriorityFilter:
    """Priority filtering."""

    async def test_normal_priority_passes(self) -> None:
        phase = AttendPhase(SafetyConfig())
        assert await phase.process(_perception(priority=10)) is True

    async def test_zero_priority_passes(self) -> None:
        phase = AttendPhase(SafetyConfig())
        assert await phase.process(_perception(priority=0)) is True

    async def test_negative_priority_filtered(self) -> None:
        phase = AttendPhase(SafetyConfig())
        assert await phase.process(_perception(priority=-1)) is False
