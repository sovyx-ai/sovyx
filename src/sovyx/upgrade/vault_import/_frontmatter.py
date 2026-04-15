"""YAML frontmatter extraction for Obsidian notes.

Obsidian frontmatter is a standard YAML block fenced by ``---`` at
the very top of the file. This module is the only place that touches
PyYAML in the vault importer — everything else works on plain dicts
to keep parser/encoder testable without a YAML dependency in the test
environment.

Tolerant parsing: malformed frontmatter yields an empty dict plus a
warning string; the note body is returned with the (presumed-broken)
frontmatter stripped as best as we can. No note is ever rejected
outright because its frontmatter is weird — users write arbitrary
things in their vaults.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Any

import yaml

from sovyx.observability.logging import get_logger

logger = get_logger(__name__)

# A note is considered to have frontmatter only if the file starts
# *exactly* with ``---`` on its own line, followed by another ``---``
# within the first N lines. No leading whitespace tolerated — that's
# how Obsidian itself decides.
_FENCE = "---"


def extract_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    """Split frontmatter + body.

    Returns ``(frontmatter_dict, body)``. When the note has no
    frontmatter, ``frontmatter_dict`` is ``{}`` and ``body`` is the
    original text unchanged. When the frontmatter is present but
    malformed, ``frontmatter_dict`` is ``{}`` *and* the body is still
    stripped of the (broken) block so downstream wikilink/tag parsing
    doesn't stumble on the YAML syntax.
    """
    if not text.startswith(_FENCE):
        return {}, text

    # Split once on the trailing fence — first fence is already at pos 0.
    # We want the text between position 3 and the next bare ``---`` line.
    lines = text.splitlines(keepends=True)
    if not lines or lines[0].rstrip() != _FENCE:
        return {}, text

    end_idx = _find_closing_fence(lines)
    if end_idx is None:
        return {}, text

    yaml_block = "".join(lines[1:end_idx])
    body = "".join(lines[end_idx + 1 :])
    # Strip a single leading newline left from the closing fence.
    body = body.lstrip("\r\n")

    parsed = _safe_yaml_load(yaml_block)
    if not isinstance(parsed, dict):
        # YAML was valid but wasn't a mapping (e.g. the user put a
        # list at the top level). Treat as empty but still strip.
        return {}, body
    return parsed, body


def _find_closing_fence(lines: list[str]) -> int | None:
    """Locate the closing ``---`` line, or ``None`` if absent."""
    for idx in range(1, len(lines)):
        if lines[idx].rstrip() == _FENCE:
            return idx
    return None


def _safe_yaml_load(block: str) -> Any:  # noqa: ANN401 — YAML returns arbitrary shapes.
    """Parse a YAML block with a tolerant fallback.

    ``yaml.safe_load`` raises on a surprising variety of edge cases
    (tab indentation, duplicate keys on some libyaml builds). A broken
    frontmatter must never abort the whole vault import, so we swallow
    parse errors and return ``None`` so the caller defaults to ``{}``.
    """
    try:
        return yaml.safe_load(block)
    except yaml.YAMLError as exc:
        logger.debug("obsidian_frontmatter_parse_failed", error=str(exc))
        return None


def normalise_aliases(raw: Any) -> tuple[str, ...]:  # noqa: ANN401 — user-authored YAML.
    """Flatten a frontmatter ``aliases:`` field into a clean tuple.

    Obsidian accepts three shapes:
        * a single string — ``aliases: Portuguese Grammar``
        * a YAML list — ``aliases: [PT Grammar, "Port. Gram."]``
        * a block list — ``aliases:\n  - PT Grammar\n  - Port. Gram.``

    All three land here after PyYAML; this helper coerces them to a
    tuple of trimmed, non-empty strings.
    """
    if raw is None:
        return ()
    if isinstance(raw, str):
        cleaned = raw.strip()
        return (cleaned,) if cleaned else ()
    if isinstance(raw, (list, tuple)):
        out: list[str] = []
        for item in raw:
            if isinstance(item, str):
                cleaned = item.strip()
                if cleaned:
                    out.append(cleaned)
        return tuple(out)
    return ()


def normalise_tags(raw: Any) -> tuple[str, ...]:  # noqa: ANN401 — user-authored YAML.
    """Flatten the frontmatter ``tags:`` field into a clean tuple.

    Same three shapes as aliases. Additionally strips a leading ``#``
    — Obsidian UI shows tags with the ``#`` prefix but the frontmatter
    field conventionally omits it, and users get this wrong both ways.
    Nested tags (``project/alpha``) pass through unchanged.
    """
    if raw is None:
        return ()
    if isinstance(raw, str):
        return _normalise_one_tag(raw)
    if isinstance(raw, (list, tuple)):
        out: list[str] = []
        for item in raw:
            if isinstance(item, str):
                out.extend(_normalise_one_tag(item))
        return tuple(out)
    return ()


def _normalise_one_tag(raw: str) -> tuple[str, ...]:
    cleaned = raw.strip().lstrip("#").strip()
    if not cleaned:
        return ()
    return (cleaned,)


def normalise_created_at(raw: Any) -> datetime | None:  # noqa: ANN401 — YAML value.
    """Coerce a frontmatter ``created:`` value into a tz-aware datetime.

    PyYAML auto-parses ISO-8601 strings into :class:`datetime.datetime`
    (naïve) or :class:`datetime.date`. Both get attached to UTC when
    naïve — Obsidian users rarely declare timezones and UTC is the
    safe default that matches how the conversation importers handle
    missing tzinfo.
    """
    if isinstance(raw, datetime):
        return raw if raw.tzinfo is not None else raw.replace(tzinfo=UTC)
    if isinstance(raw, date):
        return datetime(raw.year, raw.month, raw.day, tzinfo=UTC)
    if isinstance(raw, str):
        try:
            parsed = datetime.fromisoformat(raw)
        except ValueError:
            return None
        return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)
    return None
