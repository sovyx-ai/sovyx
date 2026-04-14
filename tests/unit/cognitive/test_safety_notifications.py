"""Tests for safety notifications.

Covers: notification sending, debounce, sinks, integration with escalation.
"""

from __future__ import annotations

from sovyx.cognitive.safety_notifications import (
    LogNotificationSink,
    SafetyNotifier,
    get_notifier,
    setup_notifier,
)


class _CaptureSink:
    """Test sink that captures messages."""

    def __init__(self) -> None:
        self.messages: list[str] = []

    def send(self, message: str) -> None:
        self.messages.append(message)


class TestSafetyNotifier:
    """Test notification sending and debounce."""

    def test_send_notification(self) -> None:
        sink = _CaptureSink()
        notifier = SafetyNotifier(sink=sink, debounce_sec=0)
        sent = notifier.notify_escalation("user-1", 10, "alerted")
        assert sent
        assert len(sink.messages) == 1
        assert "user-1" in sink.messages[0]
        assert "10" in sink.messages[0]

    def test_debounce(self) -> None:
        sink = _CaptureSink()
        notifier = SafetyNotifier(sink=sink, debounce_sec=9999)
        notifier.notify_escalation("user-1", 10, "alerted")
        sent = notifier.notify_escalation("user-1", 15, "alerted")
        assert not sent
        assert len(sink.messages) == 1  # Only first sent

    def test_debounce_different_sources(self) -> None:
        sink = _CaptureSink()
        notifier = SafetyNotifier(sink=sink, debounce_sec=9999)
        notifier.notify_escalation("user-1", 10, "alerted")
        sent = notifier.notify_escalation("user-2", 10, "alerted")
        assert sent
        assert len(sink.messages) == 2

    def test_rate_limited_message(self) -> None:
        sink = _CaptureSink()
        notifier = SafetyNotifier(sink=sink, debounce_sec=0)
        notifier.notify_escalation("user-1", 5, "rate_limited")
        assert "Rate limited" in sink.messages[0]

    def test_alert_message(self) -> None:
        sink = _CaptureSink()
        notifier = SafetyNotifier(sink=sink, debounce_sec=0)
        notifier.notify_escalation("user-1", 10, "alerted")
        assert "Owner alert" in sink.messages[0]

    def test_alert_count(self) -> None:
        sink = _CaptureSink()
        notifier = SafetyNotifier(sink=sink, debounce_sec=0)
        notifier.notify_escalation("a", 5, "alerted")
        notifier.notify_escalation("b", 5, "alerted")
        assert notifier.alert_count == 2

    def test_clear(self) -> None:
        sink = _CaptureSink()
        notifier = SafetyNotifier(sink=sink, debounce_sec=9999)
        notifier.notify_escalation("user-1", 10, "alerted")
        assert notifier.alert_count == 1
        notifier.clear()
        assert notifier.alert_count == 0
        sent = notifier.notify_escalation("user-1", 10, "alerted")
        assert sent  # Debounce cleared


class TestLogSink:
    """Test default log sink."""

    def test_log_sink_doesnt_crash(self) -> None:
        sink = LogNotificationSink()
        sink.send("test message")  # Should not raise


class TestSingleton:
    """Test get_notifier / setup_notifier."""

    def test_get_notifier(self) -> None:
        n = get_notifier()
        # Anti-pattern #8: isinstance unreliable under pytest-cov reimport.
        assert type(n).__name__ == "SafetyNotifier"

    def test_setup_notifier(self) -> None:
        sink = _CaptureSink()
        n = setup_notifier(sink=sink)
        assert type(n).__name__ == "SafetyNotifier"  # anti-pattern #8
        n.notify_escalation("test", 5, "alerted")
        assert len(sink.messages) == 1


class TestEscalationIntegration:
    """Test that escalation tracker triggers notifications."""

    def test_alert_triggers_notification(self) -> None:
        sink = _CaptureSink()
        n = setup_notifier(sink=sink)
        n._debounce_sec = 0.0  # noqa: SLF001

        from sovyx.cognitive.safety_escalation import SafetyEscalationTracker

        tracker = SafetyEscalationTracker()
        for _ in range(10):
            tracker.record_block("abuser-1")

        assert len(sink.messages) >= 1
        assert "abuser-1" in sink.messages[-1]

    def test_rate_limit_triggers_notification(self) -> None:
        sink = _CaptureSink()
        n = setup_notifier(sink=sink)
        n._debounce_sec = 0.0  # noqa: SLF001

        from sovyx.cognitive.safety_escalation import SafetyEscalationTracker

        tracker = SafetyEscalationTracker()
        for _ in range(5):
            tracker.record_block("abuser-2")

        assert any("abuser-2" in m for m in sink.messages)
