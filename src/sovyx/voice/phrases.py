"""Localised test phrases for the voice device-test flow.

Indexed by ``phrase_key`` (currently only ``"default"``) then by
language code. The language keys match :mod:`sovyx.voice.voice_catalog`'s
canonical codes — the completeness test guards the invariant that every
supported language has every phrase_key.

Why a module, not a YAML file
-----------------------------

A Python dict is typed, cheap to import, covered by mypy, and mutation
is forbidden by :class:`types.MappingProxyType`. A YAML parser would
add an import-time I/O read and a runtime failure mode (malformed
YAML) for no win — we never hot-reload phrases in prod.
"""

from __future__ import annotations

from types import MappingProxyType
from typing import TYPE_CHECKING

from sovyx.voice.voice_catalog import SUPPORTED_LANGUAGES

if TYPE_CHECKING:
    from collections.abc import Mapping

__all__ = ["DEFAULT_PHRASE_KEY", "PHRASES", "resolve_phrase"]


DEFAULT_PHRASE_KEY = "default"


# Keys must cover every language in :data:`voice_catalog.SUPPORTED_LANGUAGES`.
# Enforced by :mod:`tests.unit.voice.test_phrases`.
_RAW_PHRASES: dict[str, dict[str, str]] = {
    "default": {
        "en-us": "Audio test successful. Your voice assistant is ready.",
        "en-gb": "Audio test successful. Your voice assistant is ready.",
        "es": "Prueba de audio exitosa. Su asistente de voz está lista.",
        "fr": "Test audio réussi. Votre assistant vocal est prêt.",
        "hi": "ऑडियो परीक्षण सफल। आपकी आवाज़ सहायक तैयार है।",
        "it": "Test audio riuscito. La tua assistente vocale è pronta.",
        "ja": "音声テストに成功しました。音声アシスタントの準備ができました。",
        "pt-br": "Teste de áudio bem-sucedido. Sua assistente de voz está pronta.",
        "zh": "音频测试成功。您的语音助手已就绪。",
    },
}


# Read-only view — callers cannot mutate the catalog at runtime.
PHRASES: Mapping[str, Mapping[str, str]] = MappingProxyType(
    {k: MappingProxyType(v) for k, v in _RAW_PHRASES.items()},
)


def resolve_phrase(phrase_key: str, language: str) -> str | None:
    """Look up a phrase by key + language (already normalised).

    Returns ``None`` when either the key or the language is unknown so
    the caller can decide whether to reject (strict mode) or fall back
    (lenient mode).
    """
    entry = PHRASES.get(phrase_key)
    if entry is None:
        return None
    return entry.get(language)


# Pre-compute the set of languages covered by every key (for the
# completeness check). If a key has gaps, they're surfaced lazily at
# test time rather than erroring at import time — production doesn't
# need to crash just because a translation rotation is in flight.
_COMPLETE_KEYS: frozenset[str] = frozenset(
    key for key, entries in _RAW_PHRASES.items() if SUPPORTED_LANGUAGES.issubset(entries.keys())
)


def is_complete(phrase_key: str) -> bool:
    """Whether ``phrase_key`` covers every supported language."""
    return phrase_key in _COMPLETE_KEYS
