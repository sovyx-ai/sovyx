"""Property tests for the log-schema evolution contract.

Pins the forward-compatibility guarantees of the v1.0.0 envelope and
the :class:`KnownEventValidator` processor so that adding a new event
or shipping a code path that emits an unknown payload field can never
crash the logging pipeline.

Aligned with IMPL-OBSERVABILITY-001 §11 (Phase 11 — log schema gate).
"""

from __future__ import annotations

import socket
from typing import Any

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from pydantic import ValidationError

from sovyx.observability.schema import (
    ENVELOPE_FIELDS,
    KNOWN_EVENTS,
    META_UNKNOWN_EVENT,
    META_UNKNOWN_FIELDS,
    SCHEMA_VERSION,
    KnownEventValidator,
    event_payload_fields,
    validate_entry,
    validate_event,
)

# ── Strategies ──────────────────────────────────────────────────────────

# Event names that look like real Sovyx events (snake.dot notation) but
# are *not* in the known-events catalog. We seed with a fixed prefix that
# no real event uses so collisions stay impossible even as the catalog
# grows.
_unknown_event_names = st.text(
    alphabet=st.characters(codec="ascii", categories=("Ll", "Nd"), include_characters="."),
    min_size=1,
    max_size=40,
).map(lambda s: f"hyp.unknown.{s}")

_levels = st.sampled_from(["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"])

# Field names that are explicitly OK to appear next to envelope fields:
# saga/span/cause ids lifted from contextvars by EnvelopeProcessor.
_contextual_keys = st.sampled_from(
    ["saga_id", "span_id", "event_id", "cause_id", "pid"],
)

# Generic "extra" payload-key strategy that intentionally avoids names
# in ENVELOPE_FIELDS or the contextual-extras whitelist so the
# validator's unknown-field branch is exercised.
_extra_payload_keys = st.text(
    alphabet=st.characters(codec="ascii", categories=("Ll",)),
    min_size=3,
    max_size=20,
).filter(
    lambda k: k not in ENVELOPE_FIELDS
    and k not in {"saga_id", "span_id", "event_id", "cause_id", "pid"},
)


def _envelope(event_name: str = "hyp.unknown.demo") -> dict[str, Any]:
    """Return a minimal v1.0.0 envelope with sensible defaults."""
    return {
        "timestamp": "2026-04-20T12:00:00.000000Z",
        "level": "INFO",
        "logger": "tests.property.schema_evolution",
        "event": event_name,
        "schema_version": SCHEMA_VERSION,
        "process_id": 1,
        "host": socket.gethostname() or "localhost",
        "sovyx_version": "0.20.4",
        "sequence_no": 0,
    }


# ── Schema-version pin ─────────────────────────────────────────────────


class TestSchemaVersionPin:
    """SCHEMA_VERSION is the wire contract — bumping it is a breaking change."""

    def test_schema_version_is_pinned_at_one_zero_zero(self) -> None:
        # Any change here must coincide with a deliberate breaking-change
        # release: regenerate _models.py, bump the JSON schemas, and
        # update the dashboard. This regression assertion is intentional.
        assert SCHEMA_VERSION == "1.0.0"


# ── Forward-compat: unknown events ─────────────────────────────────────


class TestUnknownEventsAreForwardCompatible:
    """Unknown event names pass the envelope check and get tagged, never dropped."""

    @settings(max_examples=80, deadline=None, suppress_health_check=[HealthCheck.too_slow])
    @given(event_name=_unknown_event_names, level=_levels)
    def test_unknown_event_passes_validate_entry(
        self, event_name: str, level: str
    ) -> None:
        entry = _envelope(event_name) | {"level": level}
        # Forward compatibility: the envelope check accepts the entry
        # regardless of whether the event is in the catalog.
        validate_entry(entry)

    @settings(max_examples=80, deadline=None, suppress_health_check=[HealthCheck.too_slow])
    @given(event_name=_unknown_event_names)
    def test_unknown_event_returns_none_from_validate_event(
        self, event_name: str
    ) -> None:
        result = validate_event(_envelope(event_name))
        assert result is None

    @settings(max_examples=80, deadline=None, suppress_health_check=[HealthCheck.too_slow])
    @given(event_name=_unknown_event_names)
    def test_known_event_validator_tags_unknown_event(self, event_name: str) -> None:
        validator = KnownEventValidator()
        record: dict[str, Any] = {"event": event_name, "level": "INFO"}
        out = validator(None, "info", record)
        assert out is record  # Identity preserved — record never copied.
        assert out[META_UNKNOWN_EVENT] is True

    def test_known_event_validator_skips_records_without_event(self) -> None:
        validator = KnownEventValidator()
        record: dict[str, Any] = {"level": "INFO"}
        out = validator(None, "info", record)
        # No event field → no tagging.
        assert META_UNKNOWN_EVENT not in out
        assert META_UNKNOWN_FIELDS not in out


# ── Envelope rejection on missing / wrong fields ────────────────────────


class TestEnvelopeContractRejection:
    """validate_entry raises for missing or malformed envelope fields."""

    @pytest.mark.parametrize("missing", sorted(ENVELOPE_FIELDS))
    def test_missing_envelope_field_raises_value_error(self, missing: str) -> None:
        entry = _envelope()
        del entry[missing]
        with pytest.raises(ValueError, match="missing envelope fields"):
            validate_entry(entry)

    def test_wrong_schema_version_raises_value_error(self) -> None:
        entry = _envelope()
        entry["schema_version"] = "2.0.0"
        with pytest.raises(ValueError, match="schema_version mismatch"):
            validate_entry(entry)

    def test_zero_process_id_is_rejected(self) -> None:
        # ge=1 on the LogEntry model — pid 0 is reserved by some kernels
        # and not a valid emitting daemon pid.
        entry = _envelope()
        entry["process_id"] = 0
        with pytest.raises(ValidationError):
            validate_entry(entry)

    def test_negative_sequence_no_is_rejected(self) -> None:
        entry = _envelope()
        entry["sequence_no"] = -1
        with pytest.raises(ValidationError):
            validate_entry(entry)


# ── KnownEventValidator: contextual extras + unknown payload fields ────


class TestKnownEventValidatorBehaviour:
    """Validator never drops records, never raises, and respects the contextual whitelist."""

    @settings(max_examples=60, deadline=None, suppress_health_check=[HealthCheck.too_slow])
    @given(
        event_name=_unknown_event_names,
        contextual=st.lists(_contextual_keys, min_size=1, max_size=5, unique=True),
    )
    def test_contextual_extras_are_never_flagged(
        self, event_name: str, contextual: list[str]
    ) -> None:
        # Pick any KNOWN event so the unknown-payload branch is what runs.
        if not KNOWN_EVENTS:
            pytest.skip("KNOWN_EVENTS catalog is empty in this build")
        known = next(iter(KNOWN_EVENTS))
        record: dict[str, Any] = {"event": known}
        for key in contextual:
            record[key] = "ctx-value"
        validator = KnownEventValidator()
        out = validator(None, "info", record)
        # No contextual key should ever surface in meta.unknown_fields.
        unknown_fields = out.get(META_UNKNOWN_FIELDS, [])
        assert all(c not in unknown_fields for c in contextual), (
            f"contextual keys leaked into unknown_fields: {unknown_fields}"
        )

    @settings(max_examples=40, deadline=None, suppress_health_check=[HealthCheck.too_slow])
    @given(extras=st.lists(_extra_payload_keys, min_size=1, max_size=5, unique=True))
    def test_extra_payload_fields_on_known_event_are_tagged(
        self, extras: list[str]
    ) -> None:
        if not KNOWN_EVENTS:
            pytest.skip("KNOWN_EVENTS catalog is empty in this build")
        known = next(iter(KNOWN_EVENTS))
        declared = event_payload_fields(known)
        # Only keep extras that aren't actually declared on the model.
        rogue = [k for k in extras if k not in declared]
        if not rogue:
            return  # Hypothesis happened to pick declared keys; skip.
        record: dict[str, Any] = {"event": known}
        for k in rogue:
            record[k] = "x"
        out = KnownEventValidator()(None, "info", record)
        flagged = set(out.get(META_UNKNOWN_FIELDS, []))
        assert set(rogue).issubset(flagged), (
            f"expected rogue keys {rogue} to be flagged, saw {sorted(flagged)}"
        )

    @settings(max_examples=80, deadline=None, suppress_health_check=[HealthCheck.too_slow])
    @given(
        event=st.one_of(st.none(), st.text(min_size=0, max_size=60), st.integers()),
        level=_levels,
    )
    def test_validator_never_raises_for_any_event_value(
        self, event: object, level: str
    ) -> None:
        validator = KnownEventValidator()
        record: dict[str, Any] = {"event": event, "level": level}
        # Any input — None, empty string, integer, any text — must
        # round-trip cleanly. Logging pipeline cannot raise mid-emit.
        validator(None, "info", record)


# ── event_payload_fields ────────────────────────────────────────────────


class TestEventPayloadFields:
    """event_payload_fields contract: empty for unknown, populated for known."""

    @settings(max_examples=40, deadline=None, suppress_health_check=[HealthCheck.too_slow])
    @given(unknown=_unknown_event_names)
    def test_unknown_event_returns_empty_frozenset(self, unknown: str) -> None:
        assert event_payload_fields(unknown) == frozenset()

    def test_every_known_event_has_a_non_empty_field_set(self) -> None:
        if not KNOWN_EVENTS:
            pytest.skip("KNOWN_EVENTS catalog is empty in this build")
        for event_name in KNOWN_EVENTS:
            fields = event_payload_fields(event_name)
            assert fields, f"event {event_name!r} has no declared fields"
            # Envelope fields are always declared on every model — they
            # come from the LogEntry parent class.
            assert ENVELOPE_FIELDS.issubset(fields), (
                f"{event_name} dropped envelope fields: "
                f"{sorted(ENVELOPE_FIELDS - fields)}"
            )

    def test_every_known_event_pins_its_event_literal_to_its_name(self) -> None:
        if not KNOWN_EVENTS:
            pytest.skip("KNOWN_EVENTS catalog is empty in this build")
        for event_name, model in KNOWN_EVENTS.items():
            # The generated model's class-var event_name MUST match the
            # registry key — otherwise validate_event would key on a
            # value the model rejects.
            assert model.event_name == event_name


# ── Round-trip: known events stay known after model_dump ───────────────


class TestKnownEventRoundtrip:
    """Every catalog entry survives validate → dump(by_alias) → revalidate."""

    def test_voice_audio_frame_roundtrips_through_validate_event(self) -> None:
        # We pick a single concrete known event so the test is
        # deterministic; the broader catalog-coverage assertion lives in
        # check_log_schemas.py (CI gate).
        if "voice.audio.frame" not in KNOWN_EVENTS:
            pytest.skip("voice.audio.frame not in catalog for this build")
        entry = _envelope("voice.audio.frame") | {
            "voice.frames": 480,
            "voice.sample_rate": 16_000,
            "voice.rms": 0.123,
        }
        validate_entry(entry)
        model = validate_event(entry)
        assert model is not None
        # Dumping with by_alias=True restores the wire field names so
        # the second validate_event sees the same shape it accepted
        # the first time. Round-trip stability proves the pydantic
        # aliases are correctly bidirectional.
        dumped = model.model_dump(by_alias=True)
        revalidated = validate_event(dumped)
        assert revalidated is not None
        assert revalidated.model_dump(by_alias=True) == dumped
