"""Sovyx log entry schema (envelope contract v1.0.0).

This module pins the wire format of every structured log entry the
daemon emits. It exists to:

- expose ``SCHEMA_VERSION`` so downstream readers (FTS5 indexer, log
  forwarders, dashboard) can refuse incompatible payloads,
- declare ``ENVELOPE_FIELDS`` — the eight fields every entry MUST
  carry after :class:`sovyx.observability.envelope.EnvelopeProcessor`
  has run,
- catalog ``KNOWN_EVENTS`` — the registry of canonical event names
  populated incrementally per phase (empty in Phase 1; Phase 11 lands
  the full catalog),
- offer :class:`LogEntry` for tests and the CI schema gate.

The schema follows the contract in
``docs-internal/plans/IMPL-OBSERVABILITY-001-sistema-logs-surreal.md``
(§7 Task 1.2 + §22.5 — log forwarding contract).
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

SCHEMA_VERSION: Literal["1.0.0"] = "1.0.0"
"""Wire-format version. Bump on any breaking field change."""


ENVELOPE_FIELDS: frozenset[str] = frozenset(
    {
        "timestamp",
        "level",
        "logger",
        "event",
        "schema_version",
        "process_id",
        "host",
        "sovyx_version",
        "sequence_no",
    }
)
"""Mandatory fields injected by the envelope processor.

Any entry missing one of these is malformed and rejected by
:func:`validate_entry`.

The ``sequence_no`` field is a per-process monotonically increasing
integer used by downstream log forwarders (§22.5) to deduplicate
at-least-once delivery. The tuple ``(timestamp, process_id,
sequence_no)`` is globally unique across a daemon's lifetime; a
forwarder that retries a batch and ships the same entry twice is
expected to drop duplicates on this tuple.
"""


KNOWN_EVENTS: frozenset[str] = frozenset()
"""Canonical event-name registry.

Empty in Phase 1 — populated incrementally by later phases:
voice events (P3), plugin events (P5), LLM/brain/bridge events (P7),
audit events (P9), and the comprehensive catalog (P11.3).

Unknown events are not rejected; instead the validation pipeline
emits a ``logging.unknown_event`` meta-entry tagged
``meta.unknown_event=True`` and continues.
"""


class LogEntry(BaseModel):
    """Pydantic model for a single Sovyx log entry.

    Used by :func:`validate_entry`, the CI schema gate
    (``scripts/check_log_schemas.py``), and unit tests. Only the
    eight envelope fields are required; payload fields stay open via
    ``model_config(extra="allow")`` so each event type can carry its
    own typed payload (registered in :data:`KNOWN_EVENTS` per phase).
    """

    model_config = ConfigDict(extra="allow", populate_by_name=True)

    timestamp: str = Field(..., description="ISO-8601 UTC, e.g. 2026-04-20T18:30:01.234Z.")
    level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = Field(
        ..., description="Standard logging level."
    )
    logger: str = Field(..., description="Dotted module name (e.g. sovyx.voice.pipeline).")
    event: str = Field(..., description="Canonical event name (snake.dot notation).")
    schema_version: Literal["1.0.0"] = Field(
        SCHEMA_VERSION, description="Wire-format version pin."
    )
    process_id: int = Field(..., ge=1, description="OS process id of the emitting daemon.")
    host: str = Field(..., min_length=1, description="Hostname (platform.node()).")
    sovyx_version: str = Field(..., min_length=1, description="Daemon package version.")
    sequence_no: int = Field(
        ...,
        ge=0,
        description=(
            "Per-process monotonic counter (starts at 0). Combined with "
            "(timestamp, process_id) yields a globally-unique key for "
            "at-least-once dedup at the log-forwarding layer."
        ),
    )


def validate_entry(entry: dict[str, Any]) -> None:
    """Validate a log entry dict against the v1.0.0 envelope contract.

    Raises:
        ValueError: when one of the eight envelope fields is missing
            or the schema_version is not ``"1.0.0"``.
        pydantic.ValidationError: when a present field has the wrong
            type (e.g. ``process_id`` not an int).

    Notes:
        Unknown event names are *not* an error — they pass validation
        and the caller is expected to surface them as
        ``logging.unknown_event`` meta-entries. This keeps the schema
        evolution friendly: a phase can ship a new event before its
        catalog entry without breaking the gate.
    """
    missing = ENVELOPE_FIELDS - entry.keys()
    if missing:
        raise ValueError(
            f"log entry missing envelope fields: {sorted(missing)} (have: {sorted(entry.keys())})"
        )

    declared = entry.get("schema_version")
    if declared != SCHEMA_VERSION:
        raise ValueError(
            f"log entry schema_version mismatch: expected {SCHEMA_VERSION!r}, got {declared!r}"
        )

    LogEntry.model_validate(entry)


__all__ = [
    "ENVELOPE_FIELDS",
    "KNOWN_EVENTS",
    "SCHEMA_VERSION",
    "LogEntry",
    "validate_entry",
]
