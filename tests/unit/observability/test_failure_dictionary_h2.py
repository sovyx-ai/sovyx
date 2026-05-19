"""Mission H2 §T2.5 unit tests — failure_dictionary hint extensions.

Verifies the FailureSignature entries at lines 87 + 141 of
:mod:`sovyx.observability.failure_dictionary` carry references to BOTH
the legacy `audio.apo.bypassed` / `voice.apo.bypass_triggered` event
names AND the new Mission H2 neutral siblings
(`voice.capture_integrity.bypassed` / `voice.capture_integrity.bypass_triggered`)
so operator runbook auto-linkers resolve to the correct hint regardless
of which event-name shape grep'd the saga.

Mission anchor:
``docs-internal/missions/MISSION-h2-platform-neutral-event-naming-2026-05-18.md``
§T2.5 + §10.1.
"""

from __future__ import annotations

from sovyx.observability.failure_dictionary import _SIGNATURES


def _signature_hints() -> list[str]:
    """Return every FailureSignature hint string (the operator-facing copy)."""
    return [str(sig.hint) for sig in _SIGNATURES if getattr(sig, "hint", None)]


class TestFailureDictionaryH2HintExtensions:
    """The H2 dual-emission documentation is reflected in the failure-dictionary hints."""

    def test_voice_stt_eof_hint_references_neutral_event(self) -> None:
        """The voice.stt.* EOFError hint must mention the neutral
        ``voice.capture_integrity.bypassed`` event so operator log-triage
        tools that grep on the new name resolve correctly.
        """
        joined = "\n".join(_signature_hints())
        assert "voice.capture_integrity.bypassed" in joined, (
            "Mission H2 §T2.5 — voice.stt.* hint must reference the neutral "
            "voice.capture_integrity.bypassed event name."
        )

    def test_voice_stt_eof_hint_preserves_legacy_event(self) -> None:
        """The same hint must continue mentioning the legacy
        ``audio.apo.bypassed`` per ADR-D14 — operator playbooks pinning
        on the legacy name continue to resolve.
        """
        joined = "\n".join(_signature_hints())
        assert "audio.apo.bypassed" in joined, (
            "Mission H2 ADR-D14 — legacy audio.apo.bypassed reference MUST "
            "remain in the failure_dictionary hint through v0.51.0 STRICT."
        )

    def test_wake_word_deaf_hint_references_neutral_trigger(self) -> None:
        """The voice.heartbeat.deaf hint must reference the neutral
        ``voice.capture_integrity.bypass_triggered`` companion event.
        """
        joined = "\n".join(_signature_hints())
        assert "voice.capture_integrity.bypass_triggered" in joined, (
            "Mission H2 §T2.5 — wake-word deaf hint must reference the "
            "neutral voice.capture_integrity.bypass_triggered event."
        )

    def test_wake_word_deaf_hint_preserves_legacy_trigger(self) -> None:
        """Legacy ``voice.apo.bypass_triggered`` reference preserved."""
        joined = "\n".join(_signature_hints())
        assert "voice.apo.bypass_triggered" in joined, (
            "Mission H2 ADR-D14 — legacy voice.apo.bypass_triggered "
            "reference MUST remain through v0.51.0 STRICT."
        )

    def test_both_hints_use_legacy_parenthetical_format(self) -> None:
        """The dual-reference format is `<neutral> (legacy: <legacy>)` —
        keeps the canonical-first / legacy-second discipline so operator
        eyes naturally land on the neutral name even when grep'ing for
        either.
        """
        joined = "\n".join(_signature_hints())
        # Order of citation: neutral first, legacy parenthetical second.
        idx_neutral = joined.find("voice.capture_integrity.bypassed")
        idx_legacy = joined.find("audio.apo.bypassed")
        assert 0 <= idx_neutral < idx_legacy, (
            "Mission H2 — the failure_dictionary hint must list the "
            "neutral name FIRST and the legacy parenthetical SECOND so "
            "the canonical surface leads."
        )
