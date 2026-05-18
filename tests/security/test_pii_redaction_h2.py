"""F4 PII redaction continuity regression (Mission H2 §T2.4).

Verifies that the field-name-keyed PII redaction in
:mod:`sovyx.observability.pii` applies IDENTICALLY to legacy
``audio.apo.bypassed`` / ``voice_apo_bypass_*`` events AND to the
new neutral ``voice.capture_integrity.*`` siblings — the redaction
is key-driven, not event-name-driven, so the H2 rename MUST NOT leak
endpoint friendly-names through the new event names.

Counterfactual F4: if the redactor silently failed on the new event
payload shape (e.g. bounded-depth issue, key-form mismatch), this
test would catch it — the assertion checks that EVERY enumerated
fingerprint field is redacted on BOTH legacy and neutral emissions.

Mission anchor: ``docs-internal/missions/MISSION-h2-platform-neutral-event-naming-2026-05-18.md``
§T2.4 + §3 falsifiability gate F4.
"""

from __future__ import annotations

from typing import Any

import structlog

from sovyx.engine.config import ObservabilityPIIConfig
from sovyx.observability.pii import _HASH_REDACT_KEYS, PIIRedactor

# Pre-mission hardware-fingerprint fields (verbatim from
# ``_HASH_REDACT_KEYS`` at HEAD). Both bare and dotted-namespace forms
# MUST redact whether they carry through a legacy or neutral event.
_FINGERPRINT_FIELDS = sorted(_HASH_REDACT_KEYS - {"voice.endpoints"})

# Stable raw fingerprint used across legacy + neutral emissions. The
# redactor hashes it to a deterministic sentinel (12-hex prefix) — the
# continuity test asserts the SAME sentinel surfaces on both legacy
# and neutral event names so cross-event operator correlation holds.
_RAW_FINGERPRINT = "Razer BlackShark V2 Pro"


def _redactor() -> PIIRedactor:
    """Build a PIIRedactor with default modes (matches setup_logging defaults)."""
    return PIIRedactor(ObservabilityPIIConfig())


def _redact(payload: dict[str, Any]) -> dict[str, Any]:
    """Run the redactor processor — mirrors how setup_logging wires it."""
    return dict(_redactor()(structlog.get_logger(), "info", payload))


def _make_payload(event_name: str) -> dict[str, Any]:
    """Construct a payload carrying fingerprint fields under one event name."""
    payload: dict[str, Any] = {"event": event_name}
    for key in _FINGERPRINT_FIELDS:
        payload[key] = _RAW_FINGERPRINT
    return payload


class TestPIIRedactionContinuity:
    """The same fingerprint fields are redacted under BOTH legacy and neutral event names."""

    def test_legacy_audio_apo_bypassed_redacts_fingerprints(self) -> None:
        """Pre-mission legacy event — every fingerprint field redacted."""
        out = _redact(_make_payload("audio.apo.bypassed"))
        for key in _FINGERPRINT_FIELDS:
            assert out[key] != _RAW_FINGERPRINT, f"Legacy event leaked fingerprint key {key!r}"

    def test_neutral_capture_integrity_bypassed_redacts_fingerprints(self) -> None:
        """Mission H2 v0.49.7 neutral event — same redaction applies."""
        out = _redact(_make_payload("voice.capture_integrity.bypassed"))
        for key in _FINGERPRINT_FIELDS:
            assert out[key] != _RAW_FINGERPRINT, f"Neutral event leaked fingerprint key {key!r}"

    def test_legacy_voice_apo_bypass_activated_redacts_fingerprints(self) -> None:
        """Snake-case legacy event — same redaction applies."""
        out = _redact(_make_payload("voice_apo_bypass_activated"))
        for key in _FINGERPRINT_FIELDS:
            assert out[key] != _RAW_FINGERPRINT, (
                f"Legacy snake-case event leaked fingerprint key {key!r}"
            )

    def test_neutral_capture_integrity_bypass_activated_redacts_fingerprints(self) -> None:
        """Neutral snake-case sibling — same redaction applies."""
        out = _redact(_make_payload("voice.capture_integrity.bypass_activated"))
        for key in _FINGERPRINT_FIELDS:
            assert out[key] != _RAW_FINGERPRINT, (
                f"Neutral snake-case event leaked fingerprint key {key!r}"
            )

    def test_phase_1d_neutral_audio_capture_chain_scan_redacts(self) -> None:
        """Phase 1.D rename target — ``audio.capture_chain.scan`` redaction
        continuity is the same regardless of event-name shape."""
        out = _redact(_make_payload("audio.capture_chain.scan"))
        for key in _FINGERPRINT_FIELDS:
            assert out[key] != _RAW_FINGERPRINT, f"Phase 1.D event leaked fingerprint key {key!r}"

    def test_redaction_is_deterministic_across_event_names(self) -> None:
        """The SAME raw fingerprint hashes to the SAME sentinel
        regardless of which event name carries it — preserves
        cross-event correlation per the pre-mission contract.
        """
        legacy = _redact(_make_payload("audio.apo.bypassed"))
        neutral = _redact(_make_payload("voice.capture_integrity.bypassed"))
        for key in _FINGERPRINT_FIELDS:
            assert legacy[key] == neutral[key], (
                f"Fingerprint hash drifted between legacy and neutral on key {key!r}"
            )
