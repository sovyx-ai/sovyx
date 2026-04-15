"""Lightweight typed views over Home Assistant REST responses.

The HA API returns generic ``dict[str, Any]`` payloads — entity
``state`` is always a string ("on", "off", "23.5"), ``attributes``
is an open dict that depends on the domain. Rather than scatter
defensive ``str()`` / ``float()`` calls through ``home_assistant.py``,
the parsers here normalise every read once at the boundary so the
plugin code can deal with strongly-typed objects.

Only a handful of attributes are extracted per domain — exactly the
ones the MVP tools surface to the LLM. New tools that need more
fields should extend the relevant ``parse_*`` helper rather than
poking at the raw dict.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class HAEntity:
    """A normalized Home Assistant entity, regardless of domain."""

    entity_id: str
    domain: str  # first half of the entity_id (e.g. "light")
    state: str  # raw state string — always a string in HA
    friendly_name: str
    attributes: dict[str, Any]


def parse_entity(payload: dict[str, Any]) -> HAEntity | None:
    """Convert one ``/api/states/<entity_id>`` response into HAEntity.

    Returns ``None`` for malformed payloads (missing entity_id) — the
    caller renders that as a friendly "entity not found" message
    instead of raising.
    """
    entity_id = str(payload.get("entity_id") or "").strip()
    if not entity_id or "." not in entity_id:
        return None
    domain = entity_id.split(".", 1)[0]
    state = str(payload.get("state") or "")
    raw_attrs = payload.get("attributes")
    attrs: dict[str, Any] = raw_attrs if isinstance(raw_attrs, dict) else {}
    friendly = str(attrs.get("friendly_name") or entity_id)
    return HAEntity(
        entity_id=entity_id,
        domain=domain,
        state=state,
        friendly_name=friendly,
        attributes=attrs,
    )


def parse_entity_list(
    payload: list[Any],
    *,
    domain_filter: str | None = None,
) -> list[HAEntity]:
    """Convert ``/api/states`` (a list) into HAEntity objects.

    ``domain_filter`` (e.g. ``"light"``) keeps only entities whose
    entity_id starts with ``"<domain>."``. Malformed entries are
    silently skipped — defensive against future HA additions or
    transient odd payloads.
    """
    out: list[HAEntity] = []
    for item in payload:
        if not isinstance(item, dict):
            continue
        entity = parse_entity(item)
        if entity is None:
            continue
        if domain_filter is not None and entity.domain != domain_filter:
            continue
        out.append(entity)
    return out
