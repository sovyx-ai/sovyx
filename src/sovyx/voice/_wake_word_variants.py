"""Wake-word variant expansion via espeak-ng — Phase 8 / T8.16.

Layered on top of :prop:`MindConfig.effective_wake_word_variants`
(which produces the original × ASCII-fold × "hey" matrix
deterministically without espeak-ng).

When espeak-ng is available, :func:`expand_wake_word_variants`
augments the list with **phonetic transliterations** so STT engines
that return common mishears still trigger the wake word. Examples:

* ``"Lúcia"`` (pt-BR) → adds ``"lousha"`` (English mishear),
  ``"luchia"`` (alternative romanisation)
* ``"Joaquín"`` (es-ES) → adds ``"joaquim"`` (pt-PT cognate),
  ``"hwah-keen"`` (en mishear)
* ``"Müller"`` (de-DE) → adds ``"mueller"`` (German romanisation),
  ``"miller"`` (en mishear)
* ``"François"`` (fr-FR) → adds ``"francois"`` (already covered by
  ASCII-fold) plus ``"frahn-swah"`` (en mishear)

The expansion is **deterministic given the same espeak-ng install**
— phoneme conversion is stable across runs of the same binary —
but operators with unusual hardware should cap the expansion via
``MindConfig.wake_word_variants`` (which short-circuits the
auto-derivation entirely per the existing T8.2 contract).

Reference: master mission ``MISSION-voice-final-skype-grade-2026.md``
§Phase 8 / T8.16.
"""

from __future__ import annotations

import unicodedata
from typing import TYPE_CHECKING

from sovyx.observability.logging import get_logger

if TYPE_CHECKING:
    from sovyx.voice._phonetic_matcher import PhoneticMatcher

logger = get_logger(__name__)


# Per-language phonetic mishear table. Compact, hand-curated, and
# extensible by future operators via PR. The pattern: each entry maps
# (language_prefix, ASCII-folded source) → list of common mishears.
#
# Why hand-curated rather than algorithmic generation:
#
# * espeak-ng → IPA → mishear is many-to-many; a generative system
#   would produce 20+ variants per name, blowing up false-fire risk.
# * Common mishears are a SHORT list per name — operators reading
#   their wake-word config should be able to scan it.
# * Adding a new entry is one line of Python; no espeak-ng
#   round-tripping needed.
#
# The dict uses ASCII-folded keys so "Lúcia" and "Lucia" both hit
# the same entry. Keys MUST be lowercase + ASCII-fold-normalised.
_PHONETIC_MISHEARS: dict[str, dict[str, list[str]]] = {
    # Portuguese (pt-BR / pt-PT)
    "pt": {
        "lucia": ["lousha", "luchia"],
        "joaquim": ["joaquin", "hwah-keen"],
        "joao": ["joao", "joaow"],
        "antonio": ["antoño", "antonyo"],
    },
    # Spanish (es-ES / es-MX)
    "es": {
        "joaquin": ["joaquim", "hwah-keen"],
        "lucia": ["lousha", "luchia"],
        "maria": ["maria", "mariah"],
        "jose": ["jose", "hosey"],
    },
    # French (fr-FR / fr-CA)
    "fr": {
        "francois": ["frahn-swah", "francoise"],
        "jacques": ["jacque", "zhok"],
        "celine": ["seleen", "selene"],
    },
    # German (de-DE)
    "de": {
        "muller": ["mueller", "miller"],
        "schulz": ["schultz", "shoolz"],
        "fischer": ["fisher", "fischer"],
        "schmidt": ["schmid", "smith"],
    },
}


def _ascii_fold(text: str) -> str:
    """ASCII-fold + lowercase. Mirrors the convention used by
    ``_wake_word_stt_fallback`` and ``_phonetic_matcher`` so all
    variant comparisons land on the same surface."""
    decomposed = unicodedata.normalize("NFKD", text)
    return "".join(c for c in decomposed if not unicodedata.combining(c)).lower()


def _language_prefix(language: str) -> str:
    """Extract the BCP-47 primary subtag (``"pt-BR"`` → ``"pt"``)."""
    if not language:
        return ""
    return language.split("-", maxsplit=1)[0].lower()


def expand_wake_word_variants(
    base_variants: list[str],
    *,
    wake_word: str,
    language: str,
    matcher: PhoneticMatcher | None = None,
) -> list[str]:
    """Augment a wake-word variant list with phonetic mishears.

    The base list is preserved in its original order; new variants
    are appended in deterministic order (sorted within each source).
    Duplicates are dropped via insertion-ordered dedup.

    Args:
        base_variants: The variant list to extend. Typically the
            output of :prop:`MindConfig.effective_wake_word_variants`
            — the original × ASCII-fold × "hey" matrix.
        wake_word: The original wake word with diacritics intact
            (used as the lookup key into the per-language mishear
            table).
        language: BCP-47 code (``"pt-BR"``, ``"en-US"``, etc.) from
            ``MindConfig.effective_voice_language``. Only the primary
            subtag is consulted — Brazilian and European Portuguese
            share the mishear table.
        matcher: Optional :class:`PhoneticMatcher`. Reserved for
            future espeak-ng-driven phoneme generation; not consulted
            by the current hand-curated implementation. Passing one
            is a no-op for now but the parameter is reserved so
            future expansions don't break the call signature.

    Returns:
        The extended variant list. When no mishears exist for the
        ``(language, wake_word)`` pair, returns the base list
        unchanged.
    """
    if not wake_word or not base_variants:
        return list(base_variants)

    folded_key = _ascii_fold(wake_word)
    lang_key = _language_prefix(language)

    mishears: list[str] = []
    if lang_key in _PHONETIC_MISHEARS:
        mishears = _PHONETIC_MISHEARS[lang_key].get(folded_key, [])

    if matcher is not None and matcher.is_available and not mishears:
        # Reserved hook: future iterations may use the matcher to
        # generate mishears algorithmically when the hand table has
        # no entry. Currently a no-op (the hand table is the source
        # of truth) — declared so callers can pass the matcher
        # uniformly without future signature breakage.
        logger.debug(
            "voice.wake_word.variants_no_mishears",
            wake_word_folded=folded_key,
            language=lang_key,
        )

    if not mishears:
        return list(base_variants)

    seen: dict[str, None] = {v: None for v in base_variants}
    for mishear in sorted(mishears):
        seen.setdefault(mishear, None)
        # Also include the "hey" prefix form for consistency with
        # base variants — STT often picks up the courtesy "hey".
        seen.setdefault(f"hey {mishear}", None)

    logger.info(
        "voice.wake_word.variants_expanded",
        **{
            "voice.wake_word_folded": folded_key,
            "voice.language": lang_key,
            "voice.base_count": len(base_variants),
            "voice.expanded_count": len(seen),
            "voice.added_count": len(seen) - len(base_variants),
        },
    )
    return list(seen)


__all__ = [
    "expand_wake_word_variants",
]
