"""Sovyx Text Normalizer — decode obfuscated content before safety checks.

Attackers encode malicious content to bypass regex patterns:
- Base64 encoding
- Hex encoding (\\x41, 0x41)
- Leetspeak (1337sp34k)
- Unicode homoglyphs (Cyrillic а vs Latin a)
- Zero-width characters
- URL encoding (%20, %41)

This module normalizes text BEFORE safety pattern matching,
making regex patterns effective against encoded attacks.

Architecture:
    Raw text → [normalize_text()] → decoded text → safety patterns

Each decoder is independent and fail-safe (never raises).
Order matters: zero-width removal → unicode normalize → homoglyphs →
  base64 → hex → URL decode → leetspeak.
"""

from __future__ import annotations

import base64
import re
import unicodedata
from urllib.parse import unquote

# ── Zero-width characters ───────────────────────────────────────────────
# Used to break up words: "ig\u200bnore" → "ignore"
_ZERO_WIDTH_RE = re.compile(
    r"[\u200b\u200c\u200d\u200e\u200f\u2060\u2061\u2062\u2063\u2064\ufeff]"
)

# ── Base64 detection ────────────────────────────────────────────────────
# Match base64 strings (at least 16 chars, proper padding)
_BASE64_RE = re.compile(r"(?<!\w)[A-Za-z0-9+/]{16,}={0,2}(?!\w)")

# ── Hex escape sequences ───────────────────────────────────────────────
# Match \x41 or 0x41 style hex sequences
_HEX_ESCAPE_RE = re.compile(r"(?:\\x|0x)([0-9a-fA-F]{2})")

# ── URL encoding ────────────────────────────────────────────────────────
_URL_ENCODED_RE = re.compile(r"%[0-9a-fA-F]{2}")

# ── Leetspeak mapping ──────────────────────────────────────────────────
_LEET_MAP: dict[str, str] = {
    "0": "o",
    "1": "i",
    "3": "e",
    "4": "a",
    "5": "s",
    "7": "t",
    "8": "b",
    "@": "a",
    "$": "s",
    "!": "i",
    "|": "l",
    "(": "c",
    ")": "d",
    "{": "c",
    "}": "d",
}

# ── Unicode homoglyph mapping ──────────────────────────────────────────
# Common Cyrillic/Greek lookalikes → Latin equivalents
_HOMOGLYPH_MAP: dict[str, str] = {
    "\u0430": "a",  # Cyrillic а
    "\u0435": "e",  # Cyrillic е
    "\u043e": "o",  # Cyrillic о
    "\u0440": "p",  # Cyrillic р
    "\u0441": "c",  # Cyrillic с
    "\u0443": "y",  # Cyrillic у
    "\u0445": "x",  # Cyrillic х
    "\u0456": "i",  # Cyrillic і
    "\u0458": "j",  # Cyrillic ј
    "\u0455": "s",  # Cyrillic ѕ
    "\u04bb": "h",  # Cyrillic һ
    "\u0501": "d",  # Cyrillic ԁ
    "\u051b": "q",  # Cyrillic ԛ
    "\u050d": "k",  # Cyrillic Ԍ→k (approx)
    "\u0391": "A",  # Greek Α
    "\u0392": "B",  # Greek Β
    "\u0395": "E",  # Greek Ε
    "\u0396": "Z",  # Greek Ζ
    "\u0397": "H",  # Greek Η
    "\u0399": "I",  # Greek Ι
    "\u039a": "K",  # Greek Κ
    "\u039c": "M",  # Greek Μ
    "\u039d": "N",  # Greek Ν
    "\u039f": "O",  # Greek Ο
    "\u03a1": "P",  # Greek Ρ
    "\u03a4": "T",  # Greek Τ
    "\u03a5": "Y",  # Greek Υ
    "\u03a7": "X",  # Greek Χ
    "\u2010": "-",  # Hyphen
    "\u2011": "-",  # Non-breaking hyphen
    "\u2012": "-",  # Figure dash
    "\u2013": "-",  # En dash
    "\u2014": "-",  # Em dash
    "\uff0d": "-",  # Fullwidth hyphen
    "\u2018": "'",  # Left single quote
    "\u2019": "'",  # Right single quote
    "\u201c": '"',  # Left double quote
    "\u201d": '"',  # Right double quote
}


def _strip_zero_width(text: str) -> str:
    """Remove zero-width characters used to break up words."""
    return _ZERO_WIDTH_RE.sub("", text)


def _normalize_unicode(text: str) -> str:
    """Apply NFKC normalization (fullwidth → ASCII, etc.)."""
    return unicodedata.normalize("NFKC", text)


def _replace_homoglyphs(text: str) -> str:
    """Replace common Unicode homoglyphs with Latin equivalents."""
    result = []
    for ch in text:
        result.append(_HOMOGLYPH_MAP.get(ch, ch))
    return "".join(result)


def _decode_base64_segments(text: str) -> str:
    """Find and decode base64-encoded segments inline."""

    def _try_decode(match: re.Match[str]) -> str:
        segment = match.group(0)
        try:
            decoded = base64.b64decode(segment).decode("utf-8", errors="replace")
            # Only use decoded if it looks like readable text
            if decoded.isprintable() and len(decoded) >= 4:
                return f"{segment} [{decoded}]"
        except Exception:  # noqa: BLE001
            pass
        return segment

    return _BASE64_RE.sub(_try_decode, text)


def _decode_hex_escapes(text: str) -> str:
    """Decode \\x41 or 0x41 style hex sequences."""

    def _hex_to_char(match: re.Match[str]) -> str:
        try:
            return chr(int(match.group(1), 16))
        except (ValueError, OverflowError):
            return match.group(0)

    return _HEX_ESCAPE_RE.sub(_hex_to_char, text)


def _decode_url_encoding(text: str) -> str:
    """Decode URL-encoded sequences (%20 → space, etc.)."""
    if _URL_ENCODED_RE.search(text):
        try:
            return unquote(text)
        except Exception:  # noqa: BLE001
            pass
    return text


def _decode_leetspeak(text: str) -> str:
    """Convert common leetspeak substitutions to letters.

    Only appends decoded version to avoid false positives on
    legitimate text with numbers.
    """
    # Only process if text has suspicious leet patterns
    leet_count = sum(1 for c in text if c in _LEET_MAP)
    if leet_count < 3:
        return text

    decoded = []
    for ch in text:
        decoded.append(_LEET_MAP.get(ch, ch))
    decoded_str = "".join(decoded)

    if decoded_str != text:
        return f"{text} [{decoded_str}]"
    return text


def normalize_text(text: str) -> str:
    """Apply all normalizations to text before safety checking.

    Each step is independent and fail-safe. The pipeline:
    1. Strip zero-width characters (word splitting attacks)
    2. NFKC unicode normalization (fullwidth chars, etc.)
    3. Replace homoglyphs (Cyrillic/Greek lookalikes)
    4. Decode base64 segments (inline, appended)
    5. Decode hex escapes (\\x41 → A)
    6. Decode URL encoding (%41 → A)
    7. Decode leetspeak (append decoded version)

    Args:
        text: Raw input text.

    Returns:
        Normalized text with decoded content appended where applicable.
    """
    result = text
    result = _strip_zero_width(result)
    result = _normalize_unicode(result)
    result = _replace_homoglyphs(result)
    result = _decode_base64_segments(result)
    result = _decode_hex_escapes(result)
    result = _decode_url_encoding(result)
    result = _decode_leetspeak(result)
    return result
