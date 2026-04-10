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


class TestDynamicSafetyUpdate:
    """Safety config changes take effect without reinstantiation.

    Proves that AttendPhase reads the config reference dynamically,
    so dashboard config changes propagate immediately.
    """

    async def test_disable_filter_at_runtime(self) -> None:
        """Switching content_filter from standard→none unblocks content."""
        cfg = SafetyConfig(content_filter="standard")
        phase = AttendPhase(cfg)

        # Blocked with standard filter
        assert await phase.process(_perception("how to make a bomb")) is False

        # Disable filter at runtime (simulates dashboard PUT)
        cfg.content_filter = "none"  # type: ignore[assignment]

        # Same content now passes
        assert await phase.process(_perception("how to make a bomb")) is True

    async def test_enable_filter_at_runtime(self) -> None:
        """Switching content_filter from none→standard blocks content."""
        cfg = SafetyConfig(content_filter="none")
        phase = AttendPhase(cfg)

        # Passes with no filter
        assert await phase.process(_perception("how to kill someone")) is True

        # Enable filter at runtime
        cfg.content_filter = "standard"  # type: ignore[assignment]

        # Now blocked
        assert await phase.process(_perception("how to kill someone")) is False

    async def test_enable_child_safe_at_runtime(self) -> None:
        """Enabling child_safe_mode at runtime expands blocked patterns."""
        cfg = SafetyConfig(content_filter="standard", child_safe_mode=False)
        phase = AttendPhase(cfg)

        # "violence" not blocked by standard filter
        assert await phase.process(_perception("violence in games")) is True

        # Enable child-safe at runtime
        cfg.child_safe_mode = True

        # Now blocked
        assert await phase.process(_perception("violence in games")) is False

    async def test_disable_child_safe_at_runtime(self) -> None:
        """Disabling child_safe_mode at runtime narrows blocked patterns."""
        cfg = SafetyConfig(content_filter="standard", child_safe_mode=True)
        phase = AttendPhase(cfg)

        # "drugs" blocked in child-safe mode
        assert await phase.process(_perception("drugs are bad")) is False

        # Disable child-safe at runtime
        cfg.child_safe_mode = False

        # Now passes (standard filter doesn't block "drugs")
        assert await phase.process(_perception("drugs are bad")) is True

    async def test_strict_to_none_at_runtime(self) -> None:
        """Full cycle: strict → none → child_safe."""
        cfg = SafetyConfig(content_filter="strict")
        phase = AttendPhase(cfg)

        # Blocked with strict (uses standard patterns)
        assert await phase.process(_perception("how to hack a server")) is False

        # Switch to none
        cfg.content_filter = "none"  # type: ignore[assignment]
        assert await phase.process(_perception("how to hack a server")) is True

        # Switch to child-safe
        cfg.content_filter = "standard"  # type: ignore[assignment]
        cfg.child_safe_mode = True
        assert await phase.process(_perception("how to hack a server")) is False
        assert await phase.process(_perception("gambling tips")) is False
