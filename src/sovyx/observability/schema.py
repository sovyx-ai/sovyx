"""Sovyx log entry schema (envelope contract v1.0.0).

This module pins the wire format of every structured log entry the
daemon emits. It exists to:

- expose ``SCHEMA_VERSION`` so downstream readers (FTS5 indexer, log
  forwarders, dashboard) can refuse incompatible payloads,
- declare ``ENVELOPE_FIELDS`` — the nine fields every entry MUST
  carry after :class:`sovyx.observability.envelope.EnvelopeProcessor`
  has run,
- expose :class:`LogEvent` as the base class for every per-event typed
  pydantic model,
- expose :data:`KNOWN_EVENTS` — the registry mapping each canonical
  event name to its typed model (populated from the generated
  ``log_schema._models`` module),
- ship :class:`KnownEventValidator`, a structlog processor that flags
  unknown events and unknown fields without dropping them
  (forward-compatible by design — see ``log_schema/README.md``).

Source of truth for the catalog is ``scripts/_gen_log_schemas.py``;
the JSON schemas under ``log_schema/`` and the per-event pydantic
models in ``log_schema/_models.py`` are byproducts. Editing the JSON
files or ``_models.py`` directly bypasses the contract — reject in
review.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, ClassVar, Literal

from pydantic import BaseModel, ConfigDict, Field

if TYPE_CHECKING:
    from collections.abc import MutableMapping

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


class LogEntry(BaseModel):
    """Pydantic model for a single Sovyx log entry.

    Used by :func:`validate_entry`, the CI schema gate
    (``scripts/check_log_schemas.py``), and unit tests. Only the
    nine envelope fields are required; payload fields stay open via
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


class LogEvent(LogEntry):
    """Base class for every per-event typed pydantic model.

    Subclasses live in ``sovyx.observability.log_schema._models`` and
    are generated from the EVENTS table in
    ``scripts/_gen_log_schemas.py``. Each subclass:

    * pins ``event`` to a :class:`Literal` of its canonical name,
    * exposes :attr:`event_name` as a :class:`ClassVar` so the registry
      can be rebuilt without instantiation,
    * declares its payload fields with JSON aliases when the wire name
      contains a dot (``voice.probability`` → ``voice_probability``).

    Strict-mode validation is performed by :func:`validate_event` —
    extras on a known event don't raise (the wire stays
    forward-compatible) but they DO surface as ``meta.unknown_field``
    via :class:`KnownEventValidator`.
    """

    event_name: ClassVar[str] = ""


def validate_entry(entry: dict[str, Any]) -> None:
    """Validate a log entry dict against the v1.0.0 envelope contract.

    Raises:
        ValueError: when one of the nine envelope fields is missing
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


def _load_known_events() -> dict[str, type[LogEvent]]:
    """Import the generated event registry, returning ``{}`` on absence.

    The generated module (``log_schema/_models.py``) is produced by
    ``scripts/_gen_log_schemas.py``. A fresh checkout has no models
    until the generator runs at least once; failing-soft to ``{}``
    keeps imports working in that interim while still letting CI catch
    the missing artefact via the schema-gate (``check_log_schemas.py``)
    plus the assertion below.
    """
    try:
        from sovyx.observability.log_schema._models import EVENT_REGISTRY
    except ImportError:
        return {}
    return dict(EVENT_REGISTRY)


KNOWN_EVENTS: dict[str, type[LogEvent]] = _load_known_events()
"""Registry mapping canonical event names → typed pydantic model classes.

Populated at import time from ``log_schema/_models.py``. Empty when
the generated module is missing — emit sites still work (the wire
stays forward-compatible) but :class:`KnownEventValidator` will tag
*every* event as ``meta.unknown_event=True``.

Use :func:`validate_event` for strict per-event validation; iterate
this dict to enumerate the catalog.
"""


def event_payload_fields(event: str) -> frozenset[str]:
    """Return the wire-name fields declared on the model for ``event``.

    Includes envelope fields (``timestamp``, ``level``, …) plus the
    event's payload aliases. Returns an empty frozenset for unknown
    events.
    """
    model = KNOWN_EVENTS.get(event)
    if model is None:
        return frozenset()
    names: set[str] = set()
    for field_name, field_info in model.model_fields.items():
        names.add(field_info.alias if field_info.alias else field_name)
    return frozenset(names)


def validate_event(entry: dict[str, Any]) -> LogEvent | None:
    """Validate ``entry`` against its event-specific model.

    Returns:
        The hydrated :class:`LogEvent` instance on success, or
        ``None`` when the event is not in the catalog (caller should
        treat this as a forward-compat unknown event — see
        :class:`KnownEventValidator`).

    Raises:
        pydantic.ValidationError: when a known event has a payload
            field with the wrong type, or a required payload field is
            missing.
    """
    event_name = entry.get("event", "")
    model = KNOWN_EVENTS.get(event_name)
    if model is None:
        return None
    return model.model_validate(entry)


# Sentinel field names attached to records by KnownEventValidator. Tests
# and the dashboard surface these to the operator so they can spot
# catalog drift.
META_UNKNOWN_EVENT = "meta.unknown_event"
META_UNKNOWN_FIELDS = "meta.unknown_fields"


class KnownEventValidator:
    """Structlog processor that tags unknown events / unknown fields.

    Behaviour (forward-compatible by design):

    * **Unknown event** (``event_dict["event"]`` not in
      :data:`KNOWN_EVENTS`): tags the record with
      ``meta.unknown_event=True`` and lets it through. The wire stays
      open so a phase can ship a new event before the catalog catches
      up; an operator dashboard surfaces these as ``WARN``.
    * **Unknown payload field** (a key not declared in the event's
      model): tags the record with ``meta.unknown_fields=[…]`` and
      lets it through. Same forward-compat rationale.
    * Records *without* an ``event`` key (stdlib logging passthrough)
      are not tagged — they're handled upstream by
      :class:`sovyx.observability.envelope.EnvelopeProcessor`.

    The processor never drops records and never raises, so it's safe to
    install in the production processor chain.
    """

    __slots__ = ()

    def __call__(
        self,
        _logger: Any,  # noqa: ANN401  # structlog passes the bound logger here.
        _method_name: str,
        event_dict: MutableMapping[str, Any],
    ) -> MutableMapping[str, Any]:
        event_name = event_dict.get("event")
        if not isinstance(event_name, str) or not event_name:
            return event_dict

        if event_name not in KNOWN_EVENTS:
            event_dict[META_UNKNOWN_EVENT] = True
            return event_dict

        declared = event_payload_fields(event_name)
        # Compare only against keys the catalog was supposed to declare;
        # contextvars-injected ids (saga_id, span_id, …) live alongside
        # the event payload but aren't payload — they're envelope
        # extensions, never flagged as "unknown".
        ignored_extras = _CONTEXTUAL_EXTRAS
        unknown = [key for key in event_dict if key not in declared and key not in ignored_extras]
        if unknown:
            event_dict[META_UNKNOWN_FIELDS] = sorted(unknown)
        return event_dict


# Contextual-id keys lifted by EnvelopeProcessor from structlog
# contextvars. They're not part of any per-event model but they're not
# rogue payload either — never flag them as unknown.
_CONTEXTUAL_EXTRAS: frozenset[str] = frozenset(
    {"saga_id", "span_id", "event_id", "cause_id", "pid"}
)


__all__ = [
    "ENVELOPE_FIELDS",
    "KNOWN_EVENTS",
    "META_UNKNOWN_EVENT",
    "META_UNKNOWN_FIELDS",
    "SCHEMA_VERSION",
    "KnownEventValidator",
    "LogEntry",
    "LogEvent",
    "event_payload_fields",
    "validate_entry",
    "validate_event",
]
