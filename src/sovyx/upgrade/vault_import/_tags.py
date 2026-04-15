"""``#tag`` extraction from note bodies.

Obsidian tags have two flavours:

* **Body tags** — free-form ``#word`` anywhere in the text. Accepted
  character class is the Obsidian default: letters, digits, underscore,
  hyphen, and ``/`` for nested tags.
* **Frontmatter tags** — the ``tags:`` YAML field, already handled by
  :func:`_frontmatter.normalise_tags`. This module does not read them.

The body regex deliberately refuses matches immediately preceded by a
word character — so ``foo#bar`` is **not** a tag but ``foo #bar`` is.
Same for ``#`` inside URLs (``http://x#y`` is not a tag because ``y``
is preceded by nothing matching the tag syntax — wait, actually the
URL fragment ``#y`` *would* match). We handle URLs by skipping any
``#`` that sits in a token containing ``://``. Cheap and catches 99 %
of cases without parsing HTML.

Nested tags (``#project/alpha/beta``) are preserved as single string
entries here; the encoder is the one that expands them into a chain of
tag Concepts connected by ``PART_OF``.
"""

from __future__ import annotations

import re

from sovyx.upgrade.vault_import._models import RawTag
from sovyx.upgrade.vault_import._wikilinks import strip_code

# Word-boundary-aware tag regex.
# - (?<![\w/#]) — not preceded by a word char, ``/``, or another ``#``.
#   The ``#`` exclusion prevents ``##heading`` (a level-2 Markdown
#   heading) from matching ``#heading`` at the second ``#``.
# - #           — the literal opener.
# - [A-Za-z_][\w/-]* — start with a letter or underscore, then
#   letters/digits/underscore/hyphen/slash. This rejects
#   ``#123`` (markdown headings) and ``#-foo``.
_TAG_RE = re.compile(r"(?<![\w/#])#([A-Za-z_][\w/-]*)")


def extract_body_tags(body: str) -> tuple[RawTag, ...]:
    """Return unique ``#tag`` names in source order.

    De-duplicates — a note that mentions ``#study`` eight times creates
    a single ``RawTag``. Pipelines that want frequency info can count
    occurrences themselves; the encoder doesn't.
    """
    scrubbed = _scrub_urls(strip_code(body))
    seen: set[str] = set()
    out: list[RawTag] = []
    for match in _TAG_RE.finditer(scrubbed):
        name = match.group(1)
        if name not in seen:
            seen.add(name)
            out.append(RawTag(name=name))
    return tuple(out)


def _scrub_urls(text: str) -> str:
    """Blank out substrings that look like URLs.

    Cheap protection against URL fragments like ``http://x.com#anchor``
    being mis-detected as a tag. A full-fledged URL tokeniser is
    overkill for a best-effort filter; ``://`` + word chars + first
    whitespace is enough.
    """
    return re.sub(r"\w+://\S+", lambda m: " " * (m.end() - m.start()), text)


def merge_tags(*sources: tuple[RawTag, ...]) -> tuple[RawTag, ...]:
    """Merge multiple tag sources (body + frontmatter) de-duplicating.

    Order-preserving — first occurrence wins. Empty tuples are
    tolerated so callers can pass ``merge_tags(body_tags)`` when no
    frontmatter tags exist.
    """
    seen: set[str] = set()
    out: list[RawTag] = []
    for source in sources:
        for tag in source:
            if tag.name not in seen:
                seen.add(tag.name)
                out.append(tag)
    return tuple(out)


def expand_nested(tag_name: str) -> tuple[str, ...]:
    """Expand ``"project/alpha/beta"`` into ``("project", "project/alpha", "project/alpha/beta")``.

    Used by the encoder to emit one Concept per hierarchy level and
    connect them via ``PART_OF`` relations. A flat tag like
    ``"linguistics"`` yields a one-element tuple.
    """
    parts = [p for p in tag_name.split("/") if p]
    if not parts:
        return ()
    out: list[str] = []
    acc = ""
    for part in parts:
        acc = part if not acc else f"{acc}/{part}"
        out.append(acc)
    return tuple(out)
