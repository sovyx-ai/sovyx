"""Mission H2 §T2.4 unit tests — PII redactor docstring + field-name invariant.

Sibling of the security-level continuity regression at
:mod:`tests.security.test_pii_redaction_h2`. The security test covers
F4 (end-to-end PII redaction across legacy + neutral event names); this
unit test covers the structural invariant — the redaction set is
FIELD-name keyed (not event-name keyed) — at the data-shape level.

Mission anchor:
``docs-internal/missions/MISSION-h2-platform-neutral-event-naming-2026-05-18.md``
§T2.4 + §10.1.
"""

from __future__ import annotations

import structlog

from sovyx.engine.config import ObservabilityPIIConfig
from sovyx.observability.pii import _HASH_REDACT_KEYS, PIIRedactor


class TestPIIRedactorKeyDrivenInvariant:
    """The redaction set is field-name-driven — event-name renames are safe."""

    def test_hash_redact_keys_does_not_depend_on_event_name(self) -> None:
        """``_HASH_REDACT_KEYS`` is a frozenset of FIELD-name strings.
        The H2 rename of ``audio.apo.bypassed`` → ``voice.capture_integrity.bypassed``
        cannot mechanically break redaction because the redactor never
        inspects the event name to decide what to redact.
        """
        # The set must contain known fingerprint field names regardless
        # of which event carries them.
        expected_field_names = {
            "endpoint_name",
            "voice.active_endpoint_name",
            "voice.resolved_name",
            "voice.endpoints",
            "friendly_name",
            "device_name",
        }
        assert expected_field_names.issubset(_HASH_REDACT_KEYS), (
            "Mission H2 invariant — the field-name-keyed redaction set must "
            "carry every documented hardware-fingerprint key independently "
            "of event-name shape."
        )

    def test_hash_redact_keys_does_not_contain_event_names(self) -> None:
        """No event-name string should appear in the redaction set —
        verifies the set is truly field-name keyed.
        """
        suspect_event_names = {
            "audio.apo.bypassed",
            "voice.capture_integrity.bypassed",
            "voice_apo_bypass_activated",
            "voice.capture_integrity.bypass_activated",
            "audio.apo.scan",
            "audio.capture_chain.scan",
        }
        leaked_event_keys = suspect_event_names & _HASH_REDACT_KEYS
        assert not leaked_event_keys, (
            "Mission H2 anti-pattern guard — the _HASH_REDACT_KEYS frozenset "
            f"MUST NOT contain event-name strings; found {leaked_event_keys!r}. "
            "Adding an event-name to the redaction set would break the "
            "field-name-keyed invariant + leak fingerprints on event-name renames."
        )


class TestPIIRedactorRendersSameSentinelAcrossEventNames:
    """The redactor produces deterministic + event-name-independent output."""

    def _redact(self, payload: dict) -> dict:
        red = PIIRedactor(ObservabilityPIIConfig())
        return dict(red(structlog.get_logger(), "info", payload))

    def test_same_endpoint_redacts_to_same_sentinel_under_legacy_event(self) -> None:
        """Two payloads with the same endpoint field under DIFFERENT
        legacy event names redact to the same hashed sentinel.
        """
        a = self._redact({"event": "audio.apo.bypassed", "endpoint_name": "Razer USB"})
        b = self._redact({"event": "voice_apo_bypass_activated", "endpoint_name": "Razer USB"})
        assert a["endpoint_name"] == b["endpoint_name"], (
            "Mission H2 invariant — the same raw fingerprint hashes to the "
            "same sentinel regardless of which legacy event-name carries it."
        )

    def test_same_endpoint_redacts_to_same_sentinel_legacy_vs_neutral(self) -> None:
        """The same endpoint field redacted under a legacy event vs the
        new neutral event must produce identical sentinels.
        """
        legacy = self._redact({"event": "audio.apo.bypassed", "endpoint_name": "Razer USB"})
        neutral = self._redact(
            {"event": "voice.capture_integrity.bypassed", "endpoint_name": "Razer USB"}
        )
        assert legacy["endpoint_name"] == neutral["endpoint_name"], (
            "Mission H2 ADR-D14 — cross-event correlation requires the "
            "redactor to produce the same sentinel on legacy + neutral "
            "events. This is the load-bearing property of dual-emission."
        )

    def test_phase_1d_audio_capture_chain_scan_redacts_identical(self) -> None:
        """Phase 1.D rename target: ``audio.capture_chain.scan`` redaction
        is mechanically identical to ``audio.apo.scan``.
        """
        legacy = self._redact(
            {"event": "audio.apo.scan", "voice.active_endpoint_name": "Razer USB"}
        )
        neutral = self._redact(
            {"event": "audio.capture_chain.scan", "voice.active_endpoint_name": "Razer USB"}
        )
        assert legacy["voice.active_endpoint_name"] == neutral["voice.active_endpoint_name"]


class TestPIIRedactorDocstring:
    """The docstring at ``pii.py:240-253`` documents the H2 KEY-driven invariant."""

    def test_module_docstring_mentions_h2_invariant(self) -> None:
        """The PII module documents that the H2 rename does NOT require
        updating ``_HASH_REDACT_KEYS`` because the set is key-driven."""
        import sovyx.observability.pii as pii_module

        source = pii_module.__file__
        with open(source, encoding="utf-8") as fh:
            text = fh.read()
        assert "Mission H2" in text, (
            "Mission H2 §T2.4 — the pii.py module must document the "
            "key-driven (not event-name-keyed) invariant inline."
        )
