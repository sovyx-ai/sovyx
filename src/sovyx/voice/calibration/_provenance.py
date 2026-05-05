"""Provenance recorder: builds ProvenanceTrace tuples during engine execution.

The calibration engine (T2.4 -- ``engine.py``) maintains one
:class:`ProvenanceRecorder` per evaluation pass; each rule that fires
records its matched conditions + produced decisions through
:meth:`ProvenanceRecorder.record`, and the engine freezes the resulting
list into the ``CalibrationProfile.provenance`` tuple at the end.

The recorder is intentionally a stateful builder (NOT frozen) because
it accumulates across rule firings within a single evaluation pass.
The frozen tuples it produces (:class:`ProvenanceTrace`) are the
persisted artifact; the recorder itself is engine-internal scratchpad.

Operator-facing surfaces:

* ``sovyx doctor voice --calibrate --explain`` (T2.9) walks the
  ``provenance`` tuple and renders each entry as
  ``<rule_id>@v<rule_version> matched: ... produced: ...``.
* ``CalibrationProfile.provenance`` round-trips through JSON
  persistence (T2.7) so post-mortem replay can reconstruct exactly
  which rule fired and why -- critical for forensic debugging when
  a calibration produces an unexpected config diff.

History: introduced in v0.30.15 as T2.1 of mission
``MISSION-voice-self-calibrating-system-2026-05-05.md`` Layer 2.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

from sovyx.voice.calibration.schema import CalibrationConfidence, ProvenanceTrace

if TYPE_CHECKING:
    from collections.abc import Iterable


class ProvenanceRecorder:
    """Accumulates ProvenanceTrace entries during one engine evaluation pass.

    Thread-unsafe by design: the engine evaluates rules sequentially
    in priority order on a single thread, so no locking overhead is
    needed. If the engine ever moves to parallel rule evaluation, the
    recorder gains a lock here -- not at the rule call site -- so the
    rules themselves stay pure.
    """

    __slots__ = ("_traces",)

    def __init__(self) -> None:
        self._traces: list[ProvenanceTrace] = []

    def record(
        self,
        *,
        rule_id: str,
        rule_version: int,
        matched_conditions: Iterable[str],
        produced_decisions: Iterable[str],
        confidence: CalibrationConfidence,
        fired_at_utc: str | None = None,
    ) -> None:
        """Record one rule-firing event.

        Args:
            rule_id: Stable identifier of the rule (e.g. ``"R10_mic_attenuated"``).
            rule_version: Bumped on the rule's logic change. Used for
                cache invalidation when the same rule_id has been
                modified between profile generation and replay.
            matched_conditions: Human-readable strings explaining
                which preconditions matched (e.g.
                ``"fingerprint.audio_stack == 'pipewire'"``,
                ``"mixer.boost_pct == 0"``). Rendered verbatim by
                ``--explain``.
            produced_decisions: Human-readable strings naming the
                decisions emitted by this rule firing (e.g.
                ``"set: voice_capture_mode='alsa_hw_direct'"``).
            confidence: Confidence band the rule assigned to its
                produced decisions. Inherited by all decisions in
                this firing for filtering at apply time.
            fired_at_utc: Override the firing timestamp (defaults to
                ``datetime.utcnow().isoformat()``). The override is
                primarily for deterministic tests; production code
                should let it default to the natural timestamp.
        """
        ts = (
            fired_at_utc
            if fired_at_utc is not None
            else datetime.now(tz=UTC).isoformat(timespec="microseconds")
        )
        self._traces.append(
            ProvenanceTrace(
                rule_id=rule_id,
                rule_version=rule_version,
                fired_at_utc=ts,
                matched_conditions=tuple(matched_conditions),
                produced_decisions=tuple(produced_decisions),
                confidence=confidence,
            )
        )

    def freeze(self) -> tuple[ProvenanceTrace, ...]:
        """Return the recorded traces as an immutable tuple.

        After ``freeze()`` is called, further ``record()`` calls
        continue to accumulate (the recorder is single-pass-mutable
        but multi-freeze-safe), but downstream consumers MUST treat
        the returned tuple as the canonical record for that pass.
        """
        return tuple(self._traces)

    def __len__(self) -> int:
        return len(self._traces)

    def __bool__(self) -> bool:
        return bool(self._traces)
