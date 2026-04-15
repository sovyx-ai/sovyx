"""Gemini (Google Takeout) data-export parser.

Reads the ``MyActivity.json`` that Google Takeout emits under
``Takeout/My Activity/Gemini Apps/`` (older exports use ``Bard/``).
Unlike ChatGPT and Claude, Takeout does **not** expose conversations
as first-class entities — it's a flat activity stream where each turn
is its own entry. This parser reconstructs conversations from three
signals:

1. **Role** — detected from a locale-dependent title prefix
   (``"You said: …"`` / ``"Você disse: …"`` / ``"Gemini said: …"``).
2. **Ordering** — entries ship reverse-chronological; we sort
   ascending before grouping.
3. **Session boundaries** — a time gap greater than :data:`_SESSION_GAP`
   between consecutive turns starts a new conversation.

Shape (2024-era export — fields tolerated optionally):

    [
      {
        "header": "Gemini Apps",
        "title": "You said: What's the weather in São Paulo?",
        "time": "2024-06-15T14:30:45.123Z",
        "products": ["Gemini Apps"],
        "activityControls": ["Gemini Apps Activity"],
        "subtitles": [ {"name": "Model: Gemini Pro"} ],
        "details": [ ... ]
      },
      {
        "header": "Gemini Apps",
        "title": "Gemini said: It's 22°C and partly cloudy.",
        "time": "2024-06-15T14:30:47.456Z",
        "products": ["Gemini Apps"]
      }
    ]

Synthesised fields (not present in Takeout, built here):

* ``conversation_id`` — ``sha256("gemini:" + first_turn.time).hexdigest()[:16]``.
  Stable across re-imports as long as Takeout ships the same
  timestamps (exports are immutable snapshots) AND the grouping
  threshold doesn't change. That second constraint means
  :data:`_SESSION_GAP` is effectively a migration key: changing it
  would fragment dedup on existing imported history. Don't.
* ``title`` — the first user turn truncated to 60 chars, or a
  ``"Gemini conversation YYYY-MM-DD"`` fallback when no user turn is
  present (orphaned-assistant session).

Non-goals for v1 (consistent with ChatGPT + Claude):

* Attachments, subtitles, details — ignored.
* Non-conversation activity entries (search queries, "You used
  Gemini" meta-events) — filtered out naturally by the prefix-match
  pass; unmatched titles are dropped.
* Locales outside the seeded 7-language catalog — add a tuple entry
  + a regression test.
* ZIP auto-extraction — upload the ``MyActivity.json`` directly.
"""

from __future__ import annotations

import hashlib
import html
import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from sovyx.observability.logging import get_logger
from sovyx.upgrade.conv_import._base import (
    ConversationImportError,
    MessageRole,
    RawConversation,
    RawMessage,
)

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

logger = get_logger(__name__)


# ── Configuration constants ────────────────────────────────────────


# Session-boundary threshold for grouping turns into conversations.
# A gap larger than this between consecutive turns starts a new
# conversation. Chosen to match typical "stepped away for lunch,
# continued the topic" behaviour — 30 minutes is lenient enough to
# keep natural breaks together and strict enough to separate
# unrelated next-day sessions.
#
# WARNING — DEDUP STABILITY CONTRACT:
# The ``conversation_id`` we synthesise is derived from the first
# turn of each group, so grouping is what determines which timestamp
# anchors the hash. Changing this constant retroactively shifts
# group boundaries and therefore conversation_ids — re-imports of
# the same Takeout archive would appear as entirely new
# conversations, bypassing dedup. If you need to tune the gap, do
# it by introducing a new platform identifier (e.g. ``"gemini_v2"``)
# so existing imported history is unaffected.
_SESSION_GAP: timedelta = timedelta(minutes=30)


# Accepted ``header`` / ``products`` values. "Bard" is the legacy
# brand name that still appears in Takeout exports from 2023 and
# earlier — we accept both so users who kept old exports can still
# onboard their full history.
_ACCEPTED_HEADERS: frozenset[str] = frozenset({"Gemini Apps", "Bard"})


# Locale-prefix catalog. Matched case-insensitively, longest-first
# (handled in :func:`_strip_role_prefix`). Adding a locale is a
# tuple append + one unit test.
_USER_PREFIXES: tuple[str, ...] = (
    # English
    "You said:",
    # Portuguese (Brazil + Portugal)
    "Você disse:",
    "Voce disse:",
    # Spanish
    "Dijiste:",
    "Tú dijiste:",
    # French
    "Vous avez dit :",
    # German
    "Du hast gesagt:",
    # Italian
    "Hai detto:",
)


_ASSISTANT_PREFIXES: tuple[str, ...] = (
    # English — "Gemini said" is the current brand; "Bard said" is
    # the legacy one, still present in older Takeout exports.
    "Gemini said:",
    "Bard said:",
    # Portuguese
    "O Gemini respondeu:",
    "Gemini respondeu:",
    "O Bard respondeu:",
    "Bard respondeu:",
    # Spanish
    "Gemini respondió:",
    "Bard respondió:",
    # French
    "Gemini a répondu :",
    "Bard a répondu :",
    # German
    "Gemini antwortete:",
    "Bard antwortete:",
    # Italian
    "Gemini ha risposto:",
    "Bard ha risposto:",
)


# Simple HTML-tag stripper. Conservative — only removes anything
# that looks like ``<tag>`` or ``</tag>``. Literal ``<`` characters
# in user messages are unusual in Takeout exports; the risk of
# over-stripping is negligible against the value of clean body text
# for the summary LLM.
_HTML_TAG_RE: re.Pattern[str] = re.compile(r"<[^>]+>")


# ── Internal dataclasses ───────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class _Turn:
    """Single classified activity entry, pre-grouping."""

    role: MessageRole
    text: str
    time: datetime


# ── Importer ───────────────────────────────────────────────────────


class GeminiImporter:
    """Parse a Gemini Takeout ``MyActivity.json`` into RawConversations.

    Three-pass pipeline:

    1. Classify each entry (role + text + time).
    2. Sort turns ascending by timestamp (Takeout ships newest-first).
    3. Group turns into conversations by :data:`_SESSION_GAP`.

    Fully synchronous; Takeout JSON files are typically 5-50 MB so a
    full load is acceptable in v1.
    """

    platform: str = "gemini"

    def parse(self, source: Path) -> Iterator[RawConversation]:
        """Yield reconstructed conversations from the activity stream.

        Args:
            source: Path to ``MyActivity.json`` (already extracted
                from the Takeout ZIP).

        Raises:
            ConversationImportError: If the file is missing, unreadable,
                not JSON, or not a top-level JSON array.

        Yields:
            One ``RawConversation`` per inferred session with at least
            one text-bearing turn.
        """
        if not source.is_file():
            msg = f"Gemini MyActivity.json not found: {source}"
            raise ConversationImportError(msg)

        # utf-8-sig tolerates a stray BOM that some Takeout exports
        # carry; plain utf-8 would raise.
        try:
            payload = json.loads(source.read_text(encoding="utf-8-sig"))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            msg = f"Invalid MyActivity.json: {exc}"
            raise ConversationImportError(msg) from exc

        if not isinstance(payload, list):
            msg = "MyActivity.json must be a JSON array at the top level"
            raise ConversationImportError(msg)

        # Pass 1: classify each entry. Unmatched titles (meta-events,
        # search queries, unsupported locales) are dropped silently
        # here — the caller observes the miss via zero-yield + the
        # progress tracker's warning surface.
        turns: list[_Turn] = []
        for entry in payload:
            if not isinstance(entry, dict):
                continue
            classified = _classify_entry(entry)
            if classified is not None:
                turns.append(classified)

        if not turns:
            logger.debug(
                "gemini_import_no_matched_turns",
                entry_count=len(payload),
            )
            return

        # Pass 2: sort by time ascending. Python's sort is stable, so
        # ties preserve input order (rare subsecond-identical turns
        # land in the order Takeout shipped them).
        turns.sort(key=lambda t: t.time)

        # Pass 3: group by session gap.
        for group in _group_turns(turns):
            yield _synthesise_conversation(group)


# ── Classification pipeline ────────────────────────────────────────


def _classify_entry(entry: dict[str, Any]) -> _Turn | None:
    """Turn one Takeout activity entry into a ``_Turn`` or drop it.

    An entry is kept only if it passes all three filters:
        - ``header`` or ``products`` contains an accepted Gemini label,
        - ``time`` is a parseable ISO 8601 string, and
        - ``title`` starts with a known user-or-assistant prefix after
          HTML decoding.
    """
    if not _entry_is_gemini(entry):
        return None

    time = _iso_to_datetime(entry.get("time"))
    if time is None:
        return None

    title = entry.get("title")
    if not isinstance(title, str) or not title:
        return None

    classified = _strip_role_prefix(title)
    if classified is None:
        return None

    role, body = classified
    if not body.strip():
        return None

    return _Turn(role=role, text=body, time=time)


def _entry_is_gemini(entry: dict[str, Any]) -> bool:
    """Accept entries with a Gemini/Bard header or products tag."""
    header = entry.get("header")
    if isinstance(header, str) and header in _ACCEPTED_HEADERS:
        return True
    products = entry.get("products")
    if isinstance(products, list):
        return any(isinstance(p, str) and p in _ACCEPTED_HEADERS for p in products)
    return False


def _strip_role_prefix(title: str) -> tuple[MessageRole, str] | None:
    """Match ``title`` against the prefix catalog.

    HTML entities are decoded first (Takeout titles sometimes carry
    ``&amp;``, ``&#39;`` etc.) and surviving tags stripped. Returns
    ``None`` for titles that don't start with any known prefix —
    that's how meta-events ("You used Gemini") and unsupported
    locales get filtered.
    """
    decoded = _HTML_TAG_RE.sub("", html.unescape(title)).strip()
    lowered = decoded.lower()

    # Longest-prefix-first so overlapping entries (e.g. "You:" vs
    # "You said:") match the more specific one. Sorted at lookup
    # time — the catalog is a few dozen strings, not worth a
    # precomputed table.
    for prefix in sorted(_USER_PREFIXES, key=len, reverse=True):
        if lowered.startswith(prefix.lower()):
            return "user", decoded[len(prefix) :].strip()

    for prefix in sorted(_ASSISTANT_PREFIXES, key=len, reverse=True):
        if lowered.startswith(prefix.lower()):
            return "assistant", decoded[len(prefix) :].strip()

    return None


def _iso_to_datetime(value: object) -> datetime | None:
    """Parse an ISO 8601 timestamp string to a timezone-aware datetime.

    Accepts the ``Z`` suffix natively on Python 3.11+. Returns
    ``None`` for missing or malformed values.
    """
    if not isinstance(value, str):
        return None
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        return None
    # Some Takeout exports emit naive timestamps in very old data.
    # Assume UTC (Takeout's documented reference timezone) for those.
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt


# ── Grouping + synthesis ───────────────────────────────────────────


def _group_turns(turns: list[_Turn]) -> Iterator[tuple[_Turn, ...]]:
    """Split a sorted turn stream into sessions on the time-gap rule.

    Emits one tuple per session. Caller gets immutable tuples so the
    downstream synthesiser can't accidentally mutate grouping state.
    """
    if not turns:
        return
    current: list[_Turn] = [turns[0]]
    for prev, curr in zip(turns, turns[1:], strict=False):
        if curr.time - prev.time > _SESSION_GAP:
            yield tuple(current)
            current = [curr]
        else:
            current.append(curr)
    if current:
        yield tuple(current)


def _synthesise_conversation(group: tuple[_Turn, ...]) -> RawConversation:
    """Build a ``RawConversation`` from one grouped session.

    The synthesised ``conversation_id`` is a short sha256 digest
    keyed on the first turn's ISO-8601 timestamp — stable across
    re-imports as long as Takeout ships the same snapshot (which it
    does, exports are immutable) and :data:`_SESSION_GAP` doesn't
    change (see the constant's docstring).
    """
    first = group[0]
    conversation_id = hashlib.sha256(
        f"gemini:{first.time.isoformat()}".encode(),
    ).hexdigest()[:16]

    # Synthesised title — first user turn text truncated, or a
    # date-based fallback if there's no user turn (orphaned
    # assistant session, rare but observed).
    title = _synthesise_title(group, first)

    messages = tuple(RawMessage(role=t.role, text=t.text, created_at=t.time) for t in group)

    return RawConversation(
        platform="gemini",
        conversation_id=conversation_id,
        title=title,
        created_at=first.time,
        messages=messages,
    )


def _synthesise_title(group: tuple[_Turn, ...], first: _Turn) -> str:
    """First user turn (truncated), or a date-based fallback."""
    for t in group:
        if t.role == "user" and t.text.strip():
            return _truncate(t.text.strip(), 60)
    # No user turn found — use first turn's date.
    return f"Gemini conversation {first.time.date().isoformat()}"


def _truncate(text: str, limit: int) -> str:
    """Hard-truncate ``text`` to ``limit`` chars with an ellipsis."""
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"
