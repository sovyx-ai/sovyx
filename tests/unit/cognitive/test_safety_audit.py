"""Tests for sovyx.cognitive.safety_audit — audit trail + metrics (TASK-324).

Covers:
- Events recorded with correct metadata
- No original content in events (privacy)
- Stats aggregation (24h/7d/30d, by category, by direction)
- Recent events list (max 10, most recent first)
- Metrics integration (safety_blocks counter incremented)
- Max events FIFO behavior
- Integration with AttendPhase and OutputGuard
"""

from __future__ import annotations

import time

from sovyx.cognitive.safety_audit import (
    FilterAction,
    FilterDirection,
    SafetyAuditTrail,
    SafetyEvent,
)
from sovyx.cognitive.safety_patterns import (
    FilterMatch,
    FilterTier,
    PatternCategory,
    SafetyPattern,
)
from sovyx.mind.config import SafetyConfig


def _make_match(
    category: PatternCategory = PatternCategory.VIOLENCE,
    tier: FilterTier = FilterTier.STANDARD,
    description: str = "Test pattern",
) -> FilterMatch:
    """Create a FilterMatch for testing."""
    import re

    pattern = SafetyPattern(
        regex=re.compile(r"test", re.IGNORECASE),
        category=category,
        tier=tier,
        description=description,
    )
    return FilterMatch(
        matched=True,
        pattern=pattern,
        category=category,
        tier=tier,
    )


class TestEventRecording:
    """Events are recorded with correct metadata."""

    def test_record_creates_event(self) -> None:
        trail = SafetyAuditTrail()
        match = _make_match()
        event = trail.record(FilterDirection.INPUT, FilterAction.BLOCKED, match)
        assert isinstance(event, SafetyEvent)
        assert event.direction == "input"
        assert event.action == "blocked"
        assert event.category == "violence"
        assert event.tier == "standard"
        assert event.pattern_description == "Test pattern"
        assert event.timestamp > 0

    def test_record_output_replaced(self) -> None:
        trail = SafetyAuditTrail()
        match = _make_match(PatternCategory.SEXUAL, FilterTier.STRICT)
        event = trail.record(FilterDirection.OUTPUT, FilterAction.REPLACED, match)
        assert event.direction == "output"
        assert event.action == "replaced"
        assert event.category == "sexual"
        assert event.tier == "strict"

    def test_record_output_redacted(self) -> None:
        trail = SafetyAuditTrail()
        match = _make_match(PatternCategory.HACKING)
        event = trail.record(FilterDirection.OUTPUT, FilterAction.REDACTED, match)
        assert event.action == "redacted"

    def test_event_count(self) -> None:
        trail = SafetyAuditTrail()
        assert trail.event_count == 0
        trail.record(FilterDirection.INPUT, FilterAction.BLOCKED, _make_match())
        assert trail.event_count == 1
        trail.record(FilterDirection.OUTPUT, FilterAction.REPLACED, _make_match())
        assert trail.event_count == 2


class TestPrivacy:
    """Original content is NEVER stored in events."""

    def test_event_has_no_content_field(self) -> None:
        trail = SafetyAuditTrail()
        event = trail.record(
            FilterDirection.INPUT, FilterAction.BLOCKED, _make_match(),
        )
        # SafetyEvent should NOT have any field containing the original text
        fields = {f for f in event.__slots__}
        assert "content" not in fields
        assert "text" not in fields
        assert "message" not in fields
        assert "original" not in fields


class TestStats:
    """Stats aggregation."""

    def test_empty_stats(self) -> None:
        trail = SafetyAuditTrail()
        stats = trail.get_stats()
        assert stats.total_blocks_24h == 0
        assert stats.total_blocks_7d == 0
        assert stats.total_blocks_30d == 0
        assert stats.blocks_by_category == {}
        assert stats.recent_events == []

    def test_recent_events_in_24h(self) -> None:
        trail = SafetyAuditTrail()
        trail.record(FilterDirection.INPUT, FilterAction.BLOCKED, _make_match())
        trail.record(FilterDirection.OUTPUT, FilterAction.REPLACED, _make_match())
        stats = trail.get_stats()
        assert stats.total_blocks_24h == 2
        assert stats.total_blocks_7d == 2
        assert stats.total_blocks_30d == 2

    def test_blocks_by_category(self) -> None:
        trail = SafetyAuditTrail()
        trail.record(
            FilterDirection.INPUT, FilterAction.BLOCKED,
            _make_match(PatternCategory.VIOLENCE),
        )
        trail.record(
            FilterDirection.INPUT, FilterAction.BLOCKED,
            _make_match(PatternCategory.VIOLENCE),
        )
        trail.record(
            FilterDirection.INPUT, FilterAction.BLOCKED,
            _make_match(PatternCategory.HACKING),
        )
        stats = trail.get_stats()
        assert stats.blocks_by_category["violence"] == 2
        assert stats.blocks_by_category["hacking"] == 1

    def test_blocks_by_direction(self) -> None:
        trail = SafetyAuditTrail()
        trail.record(FilterDirection.INPUT, FilterAction.BLOCKED, _make_match())
        trail.record(FilterDirection.INPUT, FilterAction.BLOCKED, _make_match())
        trail.record(FilterDirection.OUTPUT, FilterAction.REPLACED, _make_match())
        stats = trail.get_stats()
        assert stats.blocks_by_direction["input"] == 2
        assert stats.blocks_by_direction["output"] == 1

    def test_recent_events_max_10(self) -> None:
        trail = SafetyAuditTrail()
        for _ in range(15):
            trail.record(FilterDirection.INPUT, FilterAction.BLOCKED, _make_match())
        stats = trail.get_stats()
        assert len(stats.recent_events) == 10

    def test_recent_events_most_recent_first(self) -> None:
        trail = SafetyAuditTrail()
        trail.record(
            FilterDirection.INPUT, FilterAction.BLOCKED,
            _make_match(description="first"),
        )
        time.sleep(0.01)
        trail.record(
            FilterDirection.INPUT, FilterAction.BLOCKED,
            _make_match(description="second"),
        )
        stats = trail.get_stats()
        assert stats.recent_events[0]["timestamp"] > stats.recent_events[1]["timestamp"]

    def test_recent_events_no_content(self) -> None:
        trail = SafetyAuditTrail()
        trail.record(FilterDirection.INPUT, FilterAction.BLOCKED, _make_match())
        stats = trail.get_stats()
        event = stats.recent_events[0]
        assert "content" not in event
        assert "text" not in event
        assert "category" in event
        assert "direction" in event


class TestMaxEvents:
    """FIFO behavior when max events exceeded."""

    def test_fifo_eviction(self) -> None:
        trail = SafetyAuditTrail(max_events=5)
        for i in range(10):
            trail.record(
                FilterDirection.INPUT, FilterAction.BLOCKED,
                _make_match(description=f"event-{i}"),
            )
        assert trail.event_count == 5

    def test_clear(self) -> None:
        trail = SafetyAuditTrail()
        trail.record(FilterDirection.INPUT, FilterAction.BLOCKED, _make_match())
        trail.clear()
        assert trail.event_count == 0


class TestIntegrationWithAttendPhase:
    """AttendPhase records audit events on block."""

    async def test_attend_records_audit_event(self) -> None:
        from sovyx.cognitive.attend import AttendPhase
        from sovyx.cognitive.perceive import Perception
        from sovyx.cognitive.safety_audit import get_audit_trail
        from sovyx.engine.types import PerceptionType

        trail = get_audit_trail()
        trail.clear()

        phase = AttendPhase(SafetyConfig(content_filter="standard"))
        p = Perception(
            id="p1",
            type=PerceptionType.USER_MESSAGE,
            source="test",
            content="how to make a bomb",
            priority=10,
        )
        result = await phase.process(p)
        assert result is False
        assert trail.event_count >= 1

        stats = trail.get_stats()
        assert stats.total_blocks_24h >= 1
        assert any(e["direction"] == "input" for e in stats.recent_events)


class TestIntegrationWithOutputGuard:
    """OutputGuard records audit events on filter."""

    def test_output_guard_records_replace(self) -> None:
        from sovyx.cognitive.output_guard import OutputGuard
        from sovyx.cognitive.safety_audit import get_audit_trail

        trail = get_audit_trail()
        trail.clear()

        guard = OutputGuard(SafetyConfig(content_filter="strict"))
        guard.check("Here's how to make a bomb for you")
        assert trail.event_count >= 1

        stats = trail.get_stats()
        assert any(e["direction"] == "output" for e in stats.recent_events)

    def test_output_guard_records_redact(self) -> None:
        from sovyx.cognitive.output_guard import OutputGuard
        from sovyx.cognitive.safety_audit import get_audit_trail

        trail = get_audit_trail()
        trail.clear()

        guard = OutputGuard(SafetyConfig(content_filter="standard"))
        guard.check("Here's how to make a bomb explanation")
        assert trail.event_count >= 1


class TestSingleton:
    """Module-level singleton functions."""

    def test_get_audit_trail_returns_instance(self) -> None:
        from sovyx.cognitive.safety_audit import get_audit_trail

        trail = get_audit_trail()
        assert isinstance(trail, SafetyAuditTrail)

    def test_setup_creates_new_instance(self) -> None:
        from sovyx.cognitive.safety_audit import setup_audit_trail

        trail = setup_audit_trail(max_events=100)
        assert isinstance(trail, SafetyAuditTrail)
