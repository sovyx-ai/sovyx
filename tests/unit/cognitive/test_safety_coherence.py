"""Tests for safety config coherence + bypass escalation (TASK-330).

Covers:
Part 1 — Config Coherence:
- child_safe=True forces content_filter=strict
- child_safe=True forces pii_protection=True
- child_safe=True forces financial_confirmation=True
- child_safe=False → no override
- Coherence log emitted

Part 2 — Bypass Escalation:
- 3 blocks → WARNING
- 5 blocks → RATE_LIMITED
- 10 blocks → ALERTED
- Cooldown resets after 15min
- Rate-limited source rejected by AttendPhase
"""

from __future__ import annotations

import time
from unittest.mock import MagicMock

from sovyx.cognitive.safety_escalation import (
    COOLDOWN_SEC,
    EscalationLevel,
    SafetyEscalationTracker,
)
from sovyx.dashboard.config import _apply_safety
from sovyx.mind.config import MindConfig, SafetyConfig

# ── Part 1: Config Coherence ──────────────────────────────────────────


class TestCoherenceChildSafe:
    """child_safe_mode enforces strict + pii + financial."""

    def test_forces_strict_filter(self) -> None:
        cfg = MindConfig(
            name="Aria",
            safety=SafetyConfig(
                child_safe_mode=False,
                content_filter="none",
            ),
        )
        changes: dict[str, str] = {}
        _apply_safety(cfg, {"child_safe_mode": True}, changes)
        assert cfg.safety.content_filter == "strict"
        assert "enforced by child-safe" in changes.get(
            "safety.content_filter", "",
        )

    def test_forces_pii_protection(self) -> None:
        cfg = MindConfig(
            name="Aria",
            safety=SafetyConfig(
                child_safe_mode=False,
                pii_protection=False,
            ),
        )
        changes: dict[str, str] = {}
        _apply_safety(cfg, {"child_safe_mode": True}, changes)
        assert cfg.safety.pii_protection is True
        assert "enforced by child-safe" in changes.get(
            "safety.pii_protection", "",
        )

    def test_forces_financial_confirmation(self) -> None:
        cfg = MindConfig(
            name="Aria",
            safety=SafetyConfig(
                child_safe_mode=False,
                financial_confirmation=False,
            ),
        )
        changes: dict[str, str] = {}
        _apply_safety(cfg, {"child_safe_mode": True}, changes)
        assert cfg.safety.financial_confirmation is True

    def test_child_safe_true_with_none_filter(self) -> None:
        """User sets child_safe=True AND content_filter=none → override."""
        cfg = MindConfig(
            name="Aria",
            safety=SafetyConfig(child_safe_mode=False, content_filter="standard"),
        )
        changes: dict[str, str] = {}
        _apply_safety(cfg, {
            "child_safe_mode": True,
            "content_filter": "none",
        }, changes)
        # Coherence should override none→strict
        assert cfg.safety.content_filter == "strict"

    def test_already_strict_no_override(self) -> None:
        cfg = MindConfig(
            name="Aria",
            safety=SafetyConfig(
                child_safe_mode=False,
                content_filter="strict",
                pii_protection=True,
                financial_confirmation=True,
            ),
        )
        changes: dict[str, str] = {}
        _apply_safety(cfg, {"child_safe_mode": True}, changes)
        # Only child_safe_mode changed, no coherence overrides needed
        assert "safety.content_filter" not in changes
        assert "safety.pii_protection" not in changes

    def test_child_safe_false_no_override(self) -> None:
        cfg = MindConfig(
            name="Aria",
            safety=SafetyConfig(
                child_safe_mode=False,
                content_filter="none",
                pii_protection=False,
            ),
        )
        changes: dict[str, str] = {}
        _apply_safety(cfg, {"content_filter": "standard"}, changes)
        # No child-safe → no coherence enforcement
        assert cfg.safety.content_filter == "standard"
        assert cfg.safety.pii_protection is False


# ── Part 2: Bypass Escalation ─────────────────────────────────────────


class TestEscalationLevels:
    """Escalation thresholds."""

    def test_initial_none(self) -> None:
        tracker = SafetyEscalationTracker()
        assert tracker.get_level("src1") == EscalationLevel.NONE

    def test_one_block_stays_none(self) -> None:
        tracker = SafetyEscalationTracker()
        level = tracker.record_block("src1")
        assert level == EscalationLevel.NONE

    def test_three_blocks_warning(self) -> None:
        tracker = SafetyEscalationTracker()
        for _ in range(2):
            tracker.record_block("src1")
        level = tracker.record_block("src1")
        assert level == EscalationLevel.WARNING

    def test_five_blocks_rate_limited(self) -> None:
        tracker = SafetyEscalationTracker()
        for _ in range(4):
            tracker.record_block("src1")
        level = tracker.record_block("src1")
        assert level == EscalationLevel.RATE_LIMITED

    def test_ten_blocks_alerted(self) -> None:
        tracker = SafetyEscalationTracker()
        for _ in range(9):
            tracker.record_block("src1")
        level = tracker.record_block("src1")
        assert level == EscalationLevel.ALERTED

    def test_is_rate_limited(self) -> None:
        tracker = SafetyEscalationTracker()
        assert not tracker.is_rate_limited("src1")
        for _ in range(5):
            tracker.record_block("src1")
        assert tracker.is_rate_limited("src1")


class TestCooldown:
    """Cooldown resets escalation after 15min."""

    def test_cooldown_resets_level(self) -> None:
        tracker = SafetyEscalationTracker()
        for _ in range(5):
            tracker.record_block("src1")
        assert tracker.is_rate_limited("src1")

        # Simulate time passing beyond cooldown
        state = tracker._sources["src1"]
        state.last_block = time.time() - COOLDOWN_SEC - 1

        assert not tracker.is_rate_limited("src1")
        assert tracker.get_level("src1") == EscalationLevel.NONE

    def test_cooldown_allows_new_blocks(self) -> None:
        tracker = SafetyEscalationTracker()
        for _ in range(5):
            tracker.record_block("src1")
        assert tracker.is_rate_limited("src1")

        state = tracker._sources["src1"]
        state.last_block = time.time() - COOLDOWN_SEC - 1

        # New block starts fresh
        level = tracker.record_block("src1")
        assert level == EscalationLevel.NONE


class TestAlertCallback:
    """Alert callback fires at threshold."""

    def test_alert_callback_called(self) -> None:
        callback = MagicMock()
        tracker = SafetyEscalationTracker(on_alert=callback)
        for _ in range(10):
            tracker.record_block("src1")
        callback.assert_called_once_with("src1", 10)

    def test_alert_callback_not_called_below_threshold(self) -> None:
        callback = MagicMock()
        tracker = SafetyEscalationTracker(on_alert=callback)
        for _ in range(5):
            tracker.record_block("src1")
        callback.assert_not_called()


class TestMultipleSources:
    """Independent tracking per source."""

    def test_independent_sources(self) -> None:
        tracker = SafetyEscalationTracker()
        for _ in range(5):
            tracker.record_block("src1")
        tracker.record_block("src2")

        assert tracker.is_rate_limited("src1")
        assert not tracker.is_rate_limited("src2")


class TestAttendIntegration:
    """AttendPhase rejects rate-limited sources."""

    async def test_rate_limited_source_rejected(self) -> None:
        from sovyx.cognitive.attend import AttendPhase
        from sovyx.cognitive.perceive import Perception
        from sovyx.cognitive.safety_escalation import get_escalation_tracker
        from sovyx.engine.types import PerceptionType

        tracker = get_escalation_tracker()
        tracker.clear()

        # Rate-limit "test-source"
        for _ in range(5):
            tracker.record_block("test-source")
        assert tracker.is_rate_limited("test-source")

        phase = AttendPhase(SafetyConfig(content_filter="standard"))
        p = Perception(
            id="p1",
            type=PerceptionType.USER_MESSAGE,
            source="test-source",
            content="normal message",
            priority=10,
        )
        result = await phase.process(p)
        assert result is False  # Rejected due to rate limiting

    async def test_normal_source_accepted(self) -> None:
        from sovyx.cognitive.attend import AttendPhase
        from sovyx.cognitive.perceive import Perception
        from sovyx.cognitive.safety_escalation import get_escalation_tracker
        from sovyx.engine.types import PerceptionType

        tracker = get_escalation_tracker()
        tracker.clear()

        phase = AttendPhase(SafetyConfig(content_filter="standard"))
        p = Perception(
            id="p2",
            type=PerceptionType.USER_MESSAGE,
            source="clean-source",
            content="hello world",
            priority=10,
        )
        result = await phase.process(p)
        assert result is True


class TestClear:
    """Clear tracking state."""

    def test_clear(self) -> None:
        tracker = SafetyEscalationTracker()
        for _ in range(5):
            tracker.record_block("src1")
        tracker.clear()
        assert tracker.get_level("src1") == EscalationLevel.NONE
        assert not tracker.is_rate_limited("src1")
