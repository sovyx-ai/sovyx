"""Unit tests for sovyx.voice.calibration._provenance.ProvenanceRecorder."""

from __future__ import annotations

from sovyx.voice.calibration import (
    CalibrationConfidence,
    ProvenanceRecorder,
    ProvenanceTrace,
)


class TestRecorderBasics:
    """Empty + minimal recording behaviour."""

    def test_empty_recorder_is_falsy(self) -> None:
        r = ProvenanceRecorder()
        assert not r
        assert len(r) == 0
        assert r.freeze() == ()

    def test_one_recording_is_truthy(self) -> None:
        r = ProvenanceRecorder()
        r.record(
            rule_id="R10_mic_attenuated",
            rule_version=1,
            matched_conditions=("fingerprint.audio_stack == 'pipewire'",),
            produced_decisions=("advise: run sovyx doctor voice --fix",),
            confidence=CalibrationConfidence.HIGH,
        )
        assert r
        assert len(r) == 1


class TestRecordingFields:
    """Each recorded ProvenanceTrace carries the fields verbatim."""

    def test_fields_round_trip(self) -> None:
        r = ProvenanceRecorder()
        r.record(
            rule_id="R10_mic_attenuated",
            rule_version=2,
            matched_conditions=("a", "b"),
            produced_decisions=("c",),
            confidence=CalibrationConfidence.MEDIUM,
            fired_at_utc="2026-05-05T18:00:00Z",
        )
        traces = r.freeze()
        assert len(traces) == 1
        t = traces[0]
        assert isinstance(t, ProvenanceTrace)
        assert t.rule_id == "R10_mic_attenuated"
        assert t.rule_version == 2
        assert t.matched_conditions == ("a", "b")
        assert t.produced_decisions == ("c",)
        assert t.confidence == CalibrationConfidence.MEDIUM
        assert t.fired_at_utc == "2026-05-05T18:00:00Z"

    def test_iterable_inputs_frozen_to_tuples(self) -> None:
        r = ProvenanceRecorder()
        r.record(
            rule_id="R_test",
            rule_version=1,
            matched_conditions=iter(["a", "b", "c"]),  # generator
            produced_decisions=iter(["x"]),
            confidence=CalibrationConfidence.HIGH,
        )
        t = r.freeze()[0]
        # Tuples (immutable) so callers can safely share traces.
        assert isinstance(t.matched_conditions, tuple)
        assert isinstance(t.produced_decisions, tuple)
        assert t.matched_conditions == ("a", "b", "c")
        assert t.produced_decisions == ("x",)

    def test_default_timestamp_is_iso_8601_utc(self) -> None:
        r = ProvenanceRecorder()
        r.record(
            rule_id="R_test",
            rule_version=1,
            matched_conditions=(),
            produced_decisions=(),
            confidence=CalibrationConfidence.HIGH,
        )
        t = r.freeze()[0]
        # Format is YYYY-MM-DDTHH:MM:SS.microseconds+00:00 (or Z-suffix).
        # Just sanity-check that it parses as ISO-8601-ish.
        assert "T" in t.fired_at_utc
        assert len(t.fired_at_utc) >= 19


class TestMultipleRecordings:
    """Recorder accumulates entries in order."""

    def test_order_preserved(self) -> None:
        r = ProvenanceRecorder()
        for i in range(5):
            r.record(
                rule_id=f"R{i:02d}",
                rule_version=1,
                matched_conditions=(),
                produced_decisions=(),
                confidence=CalibrationConfidence.HIGH,
            )
        traces = r.freeze()
        assert [t.rule_id for t in traces] == [f"R{i:02d}" for i in range(5)]

    def test_freeze_returns_tuple_not_list(self) -> None:
        r = ProvenanceRecorder()
        r.record(
            rule_id="R_test",
            rule_version=1,
            matched_conditions=(),
            produced_decisions=(),
            confidence=CalibrationConfidence.HIGH,
        )
        assert isinstance(r.freeze(), tuple)


class TestRecorderHasNoDict:
    """ProvenanceRecorder uses __slots__."""

    def test_slots_defined(self) -> None:
        r = ProvenanceRecorder()
        assert not hasattr(r, "__dict__")
