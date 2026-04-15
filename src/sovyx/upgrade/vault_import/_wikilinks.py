"""``[[wikilink]]`` extraction.

Obsidian's wikilink syntax is deceptively rich. What we support:

* ``[[Target]]`` — canonical form.
* ``[[Target|Display Text]]`` — pipe alias; we keep ``Target`` and
  drop the display (the relation is between concepts, not rendering).
* ``[[Target#Heading]]`` — heading fragment; we keep ``Target`` and
  drop the fragment. v1 could add headings as sub-concepts.
* ``[[Target#Heading|Display]]`` — both pipe and fragment.
* ``![[Target]]`` — embed (inline render of another note). The
  parser flags these as ``is_embed=True`` so the encoder can emit
  a ``PART_OF`` relation instead of plain ``RELATED_TO``.

What we deliberately ignore in v0:

* **Code fences** — links inside ``` ``` ``` blocks are *skipped*
  (they're usually examples, not real references). We do strip fenced
  code before running the link regex.
* **Inline code** — links inside a single pair of backticks. Same
  reasoning.
* **Markdown links** — ``[label](path.md)``. Obsidian supports these
  but the community strongly favours wikilinks for internal graph.
  v0 skips them; v1 can add them behind a config flag.

The regex is intentionally greedy across whitespace inside ``Target``
(so ``[[My Long Note Name]]`` works) but non-greedy across ``]]`` so
adjacent wikilinks don't get smashed together.
"""

from __future__ import annotations

import re

from sovyx.upgrade.vault_import._models import RawLink

# ``![[target|display]]`` or ``[[target#heading|display]]`` etc.
# Group 1: the optional "!" marking an embed.
# Group 2: the raw inside-bracket payload (target + optional #fragment
#          + optional |display), non-greedy on "]".
_WIKILINK_RE = re.compile(r"(!?)\[\[([^\[\]\n]+?)\]\]")

# Fenced code blocks — ``` ``` ``` or ``` lang ``` ``` — lazy match.
_FENCED_CODE_RE = re.compile(r"```[\s\S]*?```")

# Inline code — a single pair of backticks on one line.
_INLINE_CODE_RE = re.compile(r"`[^`\n]*`")


def strip_code(text: str) -> str:
    """Drop fenced + inline code before wikilink extraction.

    Replace code spans with spaces of equal length so character
    offsets elsewhere in the pipeline (future line numbers in
    warnings, etc.) stay meaningful.
    """

    def _blank(match: re.Match[str]) -> str:
        return " " * (match.end() - match.start())

    without_fenced = _FENCED_CODE_RE.sub(_blank, text)
    return _INLINE_CODE_RE.sub(_blank, without_fenced)


def extract_links(body: str) -> tuple[RawLink, ...]:
    """Return every wikilink in ``body`` in source order.

    Duplicates are preserved — the encoder uses the count to weight
    relation strength (a note linking to ``[[Foo]]`` three times signals
    a stronger affinity than one linking once).
    """
    scrubbed = strip_code(body)
    out: list[RawLink] = []
    for match in _WIKILINK_RE.finditer(scrubbed):
        is_embed = match.group(1) == "!"
        payload = match.group(2)
        target = _clean_target(payload)
        if target:
            out.append(RawLink(target=target, is_embed=is_embed))
    return tuple(out)


def _clean_target(payload: str) -> str:
    """Reduce ``"Target#Heading|Display"`` to ``"Target"``.

    Order matters: split on ``|`` first, then on ``#``. Obsidian
    treats the ``|`` as the display separator and it never appears
    inside a valid target name.
    """
    # Pipe alias: take the part before the first ``|``.
    display_split = payload.split("|", 1)
    target = display_split[0]
    # Heading fragment: drop everything from the first ``#`` onwards.
    fragment_split = target.split("#", 1)
    cleaned = fragment_split[0].strip()
    return cleaned
