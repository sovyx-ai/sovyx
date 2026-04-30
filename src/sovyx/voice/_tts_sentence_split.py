"""Shared sentence-splitting primitives for streaming TTS (T1.38).

Splits incoming text on sentence boundaries (``.``, ``!``, ``?``
followed by whitespace) while keeping common abbreviations
(``Dr.``, ``Mr.``, ``U.S.A.``, ``e.g.``, ``Ph.D.``, etc.) intact so
the streaming path yields one chunk per *actual* sentence rather
than mid-sentence fragments.

Pre-T1.38 the per-engine ``_split_sentences`` regex was a naive
``(?<=[.!?])\\s+`` greedy split that treated every period followed
by whitespace as a sentence boundary. ``"Dr. Smith said hello."``
fragmented into ``["Dr.", "Smith said hello."]``, producing two
TTS chunks with awkward prosody and a perceptible gap between
``Dr.`` and the rest of the sentence. Both ``tts_kokoro`` and
``tts_piper`` shared the same bug because they shipped near-
identical local copies of the regex; this module is the single
source of truth so a future regex update lands everywhere
atomically.

Public surface:

* :func:`split_sentences` — pure function returning the canonical
  list of sentences, with abbreviation runs merged back into their
  containing sentence. Mirrors the legacy ``_split_sentences``
  contract: empty input → ``[""]``; no terminator → single chunk.

The legacy underscore-prefixed name ``_split_sentences`` is also
re-exported from :mod:`sovyx.voice.tts_kokoro` and
:mod:`sovyx.voice.tts_piper` so existing test imports keep working
without an import-path migration.

Approach: greedy split + abbreviation merge-back.

The classic NLP problem with English sentence splitting is that
a period is overloaded — it terminates sentences, abbreviations,
and acronyms. Pure regex with negative lookbehind hits Python
``re``'s fixed-width-lookbehind requirement once the abbreviation
list has variable-length entries. We instead split greedily on
the legacy regex, then walk the resulting parts and merge any
chunk that ends with a known abbreviation back into the next one.
The abbreviation set is hardcoded — this is the operator-friendly
trade-off vs pulling in NLTK / Punkt / spaCy for a one-paragraph
sentence-splitter that runs in the per-utterance hot path.

Quotation handling is intentionally NOT in scope per spec — the
greedy regex already does not split on a period followed by a
non-whitespace closing quote (``"..." How are you?``), and the
T1.38 mission scope is limited to abbreviations.
"""

from __future__ import annotations

import re

__all__ = ["split_sentences"]


_GREEDY_SPLIT_RE = re.compile(r"(?<=[.!?])[ \t\n\r]+")
"""Sentence-boundary regex — ASCII whitespace class only.

The boundary is "sentence terminator (``.``/``!``/``?``) followed by
ASCII whitespace (space, tab, LF, CR)". The earlier T1.38 shape used
``\\s+`` (Python's full Unicode whitespace class), which incorrectly
treated several Unicode separators as sentence boundaries:

* ``\\xa0`` (NO-BREAK SPACE) — authors use this *intentionally* to
  keep tokens together (``Sr.\\xa0Silva``, ``10\\xa0000``,
  ``Mr.\\xa0Brown``). Treating it as a sentence boundary loses the
  character + fragments the breath group.
* ``\\u2003`` (EM SPACE), ``\\u2002`` (EN SPACE), ``\\u2009``
  (THIN SPACE) — typographic separators, not sentence boundaries.
* ``\\u200b`` (ZERO WIDTH SPACE), ``\\u2060`` (WORD JOINER) — never
  break-points; CSS / Unicode use them for invisible joining.
* ``\\u3000`` (IDEOGRAPHIC SPACE) — CJK convention, not sentence
  boundary.

Pre-fix symptom (Hypothesis-found counter-example): input ``".\\xa0"``
returned ``[".", ""]`` and the ``\\xa0`` was permanently lost. After
the fix the input returns ``[".\\xa0"]`` (single chunk) and the
character is preserved.

Whitespace classes that DO trigger sentence boundaries:

* `` `` (regular space) — the canonical between-sentence separator.
* ``\\t`` (TAB) — code samples, tabular content.
* ``\\n`` (LF) — Unix line break.
* ``\\r`` (CR) — pre-Mac-OSX style; still appears in CRLF inputs.

This matches the canonical NLP convention (NLTK Punkt, spaCy, CLD3
all treat Unicode separators as content rather than boundaries) and
keeps the post-fix behaviour deterministic across locale-rich input.
"""


_ABBREVIATIONS: frozenset[str] = frozenset(
    {
        # Titles / honorifics
        "mr",
        "mrs",
        "ms",
        "dr",
        "prof",
        "sr",
        "jr",
        "st",
        "mt",
        "rev",
        "fr",
        "gen",
        "col",
        "lt",
        "sgt",
        "cpl",
        "pvt",
        "capt",
        "cmdr",
        "hon",
        # Geographic / political (full forms with internal periods kept
        # intact — the lookup compares against the trailing-period-
        # stripped token, so "U.S.A." → "u.s.a" matches the entry below).
        "u.s",
        "u.k",
        "u.s.a",
        "u.n",
        "e.u",
        "n.y",
        "l.a",
        "d.c",
        # Latin / academic
        "i.e",
        "e.g",
        "etc",
        "viz",
        "cf",
        "vs",
        "ph.d",
        "m.d",
        "b.a",
        "m.a",
        "b.s",
        "m.s",
        "j.d",
        "ll.b",
        "ed.d",
        # Business / org suffixes
        "inc",
        "ltd",
        "co",
        "corp",
        "llc",
        "plc",
        "gmbh",
        # Time
        "a.m",
        "p.m",
        # Misc periodic short forms
        "no",
        "vol",
        "fig",
        "approx",
    }
)
"""Lowercased token set for abbreviation-merge lookup.

Each entry represents the token *without* its trailing period —
``Dr.`` → ``"dr"``, ``U.S.A.`` → ``"u.s.a"``. The comparison in
:func:`_ends_with_abbreviation` strips the candidate token's
trailing period and lowercases before lookup, so the table above
is the canonical case-folded form. Entries with internal periods
(``"u.s.a"``) match the corresponding multi-period abbreviation
(``"U.S.A."``) because the greedy splitter does not split on
internal periods that aren't followed by whitespace.

Adding an abbreviation: append the lowercase trailing-period-
stripped form. Test the new entry both as a sentence-internal
occurrence (``"... <Abbrev>. is great"``) and as a sentence-
terminator (``"... saw <Abbrev>."``). The second case must still
split correctly when followed by a new sentence — the merge logic
at :func:`split_sentences` keeps merging while every join still
ends with an abbreviation, so a final non-abbreviation chunk is
required to flush the buffer."""


def split_sentences(text: str) -> list[str]:
    """Split text on sentence boundaries, preserving abbreviations.

    Mirrors the legacy per-engine ``_split_sentences`` contract:

    * Empty input → ``[""]``.
    * No sentence terminator (``.``, ``!``, ``?``) followed by
      whitespace → single-element list with the original text.
    * Multi-sentence input → list with one element per sentence.

    Post-T1.38 enhancement: a chunk that ends with a known
    abbreviation (``Dr.``, ``Mr.``, ``U.S.A.``, ``e.g.``,
    ``Ph.D.``, ...) is merged back into the next chunk so the
    abbreviation stays attached to its containing sentence rather
    than producing an empty / fragment chunk that would synthesize
    as a one-syllable TTS hiccup.

    Args:
        text: The input text. May be empty, a fragment, or one or
            more complete sentences.

    Returns:
        List of sentence strings. The concatenation of the result
        (with whitespace re-inserted at split boundaries) is
        equivalent to the input modulo whitespace normalisation.
    """
    if not text:
        return [text]

    raw_parts = _GREEDY_SPLIT_RE.split(text)
    if len(raw_parts) <= 1:
        return raw_parts if raw_parts else [text]

    merged: list[str] = []
    buffer: list[str] = []

    for part in raw_parts:
        buffer.append(part)
        candidate = " ".join(buffer)
        if _ends_with_abbreviation(candidate):
            # Keep accumulating — the trailing period belongs to an
            # abbreviation, not a sentence terminator.
            continue
        merged.append(candidate)
        buffer = []

    if buffer:
        # Trailing buffer that never resolved (e.g. text ended on an
        # abbreviation with no following sentence). Flush as-is.
        merged.append(" ".join(buffer))

    return merged if merged else [text]


def _ends_with_abbreviation(text: str) -> bool:
    """Return True if ``text`` ends with a token in :data:`_ABBREVIATIONS`.

    A "token" here is the whitespace-separated last word of ``text``
    after the trailing sentence-terminator period is stripped. The
    comparison is case-insensitive.

    ``"Dr."`` → token ``"Dr"`` → ``"dr"`` → match.
    ``"U.S.A."`` → token ``"U.S.A"`` → ``"u.s.a"`` → match.
    ``"hello."`` → token ``"hello"`` → ``"hello"`` → no match.
    ``"Mr. Smith said hello."`` → last token ``"hello"`` → no match
    (the merge already absorbed ``Mr.`` into the buffer; the
    sentence-terminator period is correctly identified as the end).
    """
    stripped = text.rstrip()
    if not stripped.endswith((".", "!", "?")):
        return False
    if stripped[-1] in "!?":
        # ``!`` and ``?`` are unambiguous sentence terminators —
        # English doesn't use them in abbreviations.
        return False

    without_dot = stripped[:-1]
    if not without_dot:
        return False

    # Last whitespace-separated token. ``rsplit(maxsplit=1)`` returns
    # a list of length 1 when there's no whitespace, so index [-1] is
    # safe in both cases.
    last_token = without_dot.rsplit(maxsplit=1)[-1]
    return last_token.lower() in _ABBREVIATIONS
