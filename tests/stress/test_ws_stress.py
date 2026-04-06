"""Unit tests for WS stress test utilities.

POLISH-22: Validates event generation and metrics collection
without requiring a running WebSocket server.
"""

from __future__ import annotations

import json

import pytest

from tests.stress.ws_stress_test import (
    EVENT_TYPES,
    StressTestMetrics,
    make_event,
)


class TestEventGeneration:
    """Validate that generated events match WsEvent schema."""

    @pytest.mark.parametrize("event_type", EVENT_TYPES)
    def test_make_event_has_required_fields(self, event_type: str) -> None:
        event = make_event(event_type)
        assert event["type"] == event_type
        assert "timestamp" in event
        assert "correlation_id" in event
        assert "data" in event
        assert isinstance(event["data"], dict)

    @pytest.mark.parametrize("event_type", EVENT_TYPES)
    def test_event_is_json_serializable(self, event_type: str) -> None:
        event = make_event(event_type)
        serialized = json.dumps(event)
        parsed = json.loads(serialized)
        assert parsed["type"] == event_type

    def test_think_completed_has_token_counts(self) -> None:
        event = make_event("ThinkCompleted")
        data = event["data"]
        assert "tokens_in" in data
        assert "tokens_out" in data
        assert "model" in data
        assert isinstance(data["tokens_in"], int)
        assert isinstance(data["tokens_out"], int)

    def test_concept_created_has_label(self) -> None:
        event = make_event("ConceptCreated")
        assert "concept_id" in event["data"]
        assert "label" in event["data"]

    def test_unique_correlation_ids(self) -> None:
        ids = {make_event("ThinkCompleted")["correlation_id"] for _ in range(100)}
        assert len(ids) == 100, "Correlation IDs must be unique"


class TestStressMetrics:
    """Validate metrics collection and reporting."""

    def test_empty_metrics(self) -> None:
        m = StressTestMetrics()
        assert m.events_sent == 0
        assert m.send_errors == 0

    def test_record_send(self) -> None:
        m = StressTestMetrics()
        m.record_send("ThinkCompleted")
        m.record_send("ThinkCompleted")
        m.record_send("ResponseSent")
        assert m.events_sent == 3
        assert m.event_type_counts["ThinkCompleted"] == 2
        assert m.event_type_counts["ResponseSent"] == 1

    def test_record_error(self) -> None:
        m = StressTestMetrics()
        m.record_error()
        m.record_error()
        assert m.send_errors == 2

    def test_rate_calculation(self) -> None:
        m = StressTestMetrics()
        m.start_time = 0.0
        m.end_time = 10.0
        for _ in range(500):
            m.record_send("ThinkCompleted")
        assert m.actual_rate == pytest.approx(50.0)
        assert m.duration == pytest.approx(10.0)

    def test_report_pass(self) -> None:
        m = StressTestMetrics()
        m.start_time = 0.0
        m.end_time = 10.0
        for _ in range(500):
            m.record_send("ThinkCompleted")
        report = m.report()
        assert "PASS" in report
        assert "500" in report

    def test_report_fail_low_count(self) -> None:
        m = StressTestMetrics()
        m.start_time = 0.0
        m.end_time = 10.0
        for _ in range(100):
            m.record_send("ThinkCompleted")
        report = m.report()
        assert "FAIL" in report

    def test_report_fail_high_errors(self) -> None:
        m = StressTestMetrics()
        m.start_time = 0.0
        m.end_time = 10.0
        for _ in range(500):
            m.record_send("ThinkCompleted")
        for _ in range(20):
            m.record_error()
        report = m.report()
        assert "FAIL" in report
