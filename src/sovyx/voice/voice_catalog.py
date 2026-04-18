"""Voice catalog — single source of truth for voice-id ↔ language mapping.

The Kokoro v1.0 release ships a single ``voices-v1.0.bin`` file holding
the style vectors for all 54 voices. Having the file makes every voice
available; missing it makes none available. The piece that varies —
and that the dashboard needs at pick-time — is the mapping from a
voice id (``af_heart``, ``pf_dora``, …) to the language it speaks and
a friendly display name. That mapping is pure metadata and lives here.

Naming convention (Kokoro):

    {lang}{gender}_{name}

where ``lang`` is a single letter encoding the spoken language and
``gender`` is ``f`` (female) or ``m`` (male):

    a → American English (en-us)
    b → British English (en-gb)
    e → Spanish (es)
    f → French (fr)
    h → Hindi (hi)
    i → Italian (it)
    j → Japanese (ja)
    p → Portuguese (pt-br)
    z → Mandarin Chinese (zh)

Why this module exists
----------------------

Before the catalog, the voice-test endpoint built a single TTS engine,
cached it, and ignored the ``voice`` parameter on subsequent requests —
so the setup wizard always played English regardless of the user's
language pick. Routing this through a catalog lets the backend answer
"what voices cover Portuguese?" without reading the Kokoro binary, and
lets it pick a sane default when the UI sends ``voice=None``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

__all__ = [
    "SUPPORTED_LANGUAGES",
    "VoiceInfo",
    "all_voices",
    "language_for_voice",
    "normalize_language",
    "recommended_voice",
    "supported_languages",
    "voice_info",
    "voices_for_language",
]


Gender = Literal["female", "male"]


@dataclass(frozen=True, slots=True)
class VoiceInfo:
    """Metadata for a single Kokoro voice."""

    id: str  # Kokoro voice id, e.g. "af_bella"
    display_name: str  # Human-friendly name, e.g. "Bella"
    language: str  # Kokoro language code, e.g. "en-us", "pt-br"
    gender: Gender


# Kokoro v1.0 canonical voice list. Either all of these are available
# (voices-v1.0.bin on disk) or none are.
_KOKORO_VOICES: tuple[VoiceInfo, ...] = (
    # American English (a*)
    VoiceInfo("af_alloy", "Alloy", "en-us", "female"),
    VoiceInfo("af_aoede", "Aoede", "en-us", "female"),
    VoiceInfo("af_bella", "Bella", "en-us", "female"),
    VoiceInfo("af_heart", "Heart", "en-us", "female"),
    VoiceInfo("af_jessica", "Jessica", "en-us", "female"),
    VoiceInfo("af_kore", "Kore", "en-us", "female"),
    VoiceInfo("af_nicole", "Nicole", "en-us", "female"),
    VoiceInfo("af_nova", "Nova", "en-us", "female"),
    VoiceInfo("af_river", "River", "en-us", "female"),
    VoiceInfo("af_sarah", "Sarah", "en-us", "female"),
    VoiceInfo("af_sky", "Sky", "en-us", "female"),
    VoiceInfo("am_adam", "Adam", "en-us", "male"),
    VoiceInfo("am_echo", "Echo", "en-us", "male"),
    VoiceInfo("am_eric", "Eric", "en-us", "male"),
    VoiceInfo("am_fenrir", "Fenrir", "en-us", "male"),
    VoiceInfo("am_liam", "Liam", "en-us", "male"),
    VoiceInfo("am_michael", "Michael", "en-us", "male"),
    VoiceInfo("am_onyx", "Onyx", "en-us", "male"),
    VoiceInfo("am_puck", "Puck", "en-us", "male"),
    VoiceInfo("am_santa", "Santa", "en-us", "male"),
    # British English (b*)
    VoiceInfo("bf_alice", "Alice", "en-gb", "female"),
    VoiceInfo("bf_emma", "Emma", "en-gb", "female"),
    VoiceInfo("bf_isabella", "Isabella", "en-gb", "female"),
    VoiceInfo("bf_lily", "Lily", "en-gb", "female"),
    VoiceInfo("bm_daniel", "Daniel", "en-gb", "male"),
    VoiceInfo("bm_fable", "Fable", "en-gb", "male"),
    VoiceInfo("bm_george", "George", "en-gb", "male"),
    VoiceInfo("bm_lewis", "Lewis", "en-gb", "male"),
    # Spanish (e*)
    VoiceInfo("ef_dora", "Dora", "es", "female"),
    VoiceInfo("em_alex", "Alex", "es", "male"),
    VoiceInfo("em_santa", "Santa", "es", "male"),
    # French (f*)
    VoiceInfo("ff_siwis", "Siwis", "fr", "female"),
    # Hindi (h*)
    VoiceInfo("hf_alpha", "Alpha", "hi", "female"),
    VoiceInfo("hf_beta", "Beta", "hi", "female"),
    VoiceInfo("hm_omega", "Omega", "hi", "male"),
    VoiceInfo("hm_psi", "Psi", "hi", "male"),
    # Italian (i*)
    VoiceInfo("if_sara", "Sara", "it", "female"),
    VoiceInfo("im_nicola", "Nicola", "it", "male"),
    # Japanese (j*)
    VoiceInfo("jf_alpha", "Alpha", "ja", "female"),
    VoiceInfo("jf_gongitsune", "Gongitsune", "ja", "female"),
    VoiceInfo("jf_nezumi", "Nezumi", "ja", "female"),
    VoiceInfo("jf_tebukuro", "Tebukuro", "ja", "female"),
    VoiceInfo("jm_kumo", "Kumo", "ja", "male"),
    # Portuguese (Brazilian) (p*)
    VoiceInfo("pf_dora", "Dora", "pt-br", "female"),
    VoiceInfo("pm_alex", "Alex", "pt-br", "male"),
    VoiceInfo("pm_santa", "Santa", "pt-br", "male"),
    # Mandarin Chinese (z*)
    VoiceInfo("zf_xiaobei", "Xiaobei", "zh", "female"),
    VoiceInfo("zf_xiaoni", "Xiaoni", "zh", "female"),
    VoiceInfo("zf_xiaoxiao", "Xiaoxiao", "zh", "female"),
    VoiceInfo("zf_xiaoyi", "Xiaoyi", "zh", "female"),
    VoiceInfo("zm_yunjian", "Yunjian", "zh", "male"),
    VoiceInfo("zm_yunxi", "Yunxi", "zh", "male"),
    VoiceInfo("zm_yunxia", "Yunxia", "zh", "male"),
    VoiceInfo("zm_yunyang", "Yunyang", "zh", "male"),
)


# Recommended default voice per language. These are the "headline" voices
# — picked for quality rather than alphabetical order.
_RECOMMENDED: dict[str, str] = {
    "en-us": "af_heart",
    "en-gb": "bf_emma",
    "es": "ef_dora",
    "fr": "ff_siwis",
    "hi": "hf_alpha",
    "it": "if_sara",
    "ja": "jf_alpha",
    "pt-br": "pf_dora",
    "zh": "zf_xiaoxiao",
}


# Language aliases — normalise common UI-picked codes to Kokoro's.
# The UI may arrive with ``en`` (navigator.language), ``pt-BR`` (BCP 47),
# or ``pt_BR`` (POSIX) — all three should mean the same thing.
_LANGUAGE_ALIASES: dict[str, str] = {
    "en": "en-us",
    "en-us": "en-us",
    "en_us": "en-us",
    "en-gb": "en-gb",
    "en_gb": "en-gb",
    "pt": "pt-br",
    "pt-br": "pt-br",
    "pt_br": "pt-br",
    "es": "es",
    "es-es": "es",
    "es-mx": "es",
    "es_mx": "es",
    "fr": "fr",
    "fr-fr": "fr",
    "hi": "hi",
    "hi-in": "hi",
    "it": "it",
    "it-it": "it",
    "ja": "ja",
    "ja-jp": "ja",
    "zh": "zh",
    "zh-cn": "zh",
    "zh-tw": "zh",
}


SUPPORTED_LANGUAGES: frozenset[str] = frozenset(_RECOMMENDED.keys())


_VOICES_BY_ID: dict[str, VoiceInfo] = {v.id: v for v in _KOKORO_VOICES}


def normalize_language(language: str) -> str:
    """Normalise a UI language tag to Kokoro's canonical code.

    Accepts lowercase or uppercase region tags, underscore or hyphen
    separators. Unknown tags are lower-cased and returned as-is so the
    caller can still decide what to do (usually: reject with a structured
    error instead of silently falling back to English).
    """
    low = language.strip().lower().replace("_", "-")
    return _LANGUAGE_ALIASES.get(low, low)


def voices_for_language(language: str) -> list[VoiceInfo]:
    """Return every catalog voice that speaks ``language``.

    Unsupported languages return ``[]`` — callers differentiate between
    "no voices for language" and "language not known" via
    :func:`normalize_language` + membership in :data:`SUPPORTED_LANGUAGES`.
    """
    canonical = normalize_language(language)
    return [v for v in _KOKORO_VOICES if v.language == canonical]


def recommended_voice(language: str) -> VoiceInfo | None:
    """Pick the recommended voice for ``language``, or ``None`` if none."""
    canonical = normalize_language(language)
    voice_id = _RECOMMENDED.get(canonical)
    if voice_id is None:
        return None
    return _VOICES_BY_ID.get(voice_id)


def language_for_voice(voice_id: str) -> str | None:
    """Return the Kokoro language code of ``voice_id``, or ``None``."""
    info = _VOICES_BY_ID.get(voice_id)
    return info.language if info else None


def voice_info(voice_id: str) -> VoiceInfo | None:
    """Return the full :class:`VoiceInfo` for ``voice_id``, or ``None``."""
    return _VOICES_BY_ID.get(voice_id)


def supported_languages() -> list[str]:
    """Return the sorted list of languages covered by the catalog."""
    return sorted(SUPPORTED_LANGUAGES)


def all_voices() -> list[VoiceInfo]:
    """Return the full voice catalog (stable order, all languages)."""
    return list(_KOKORO_VOICES)
