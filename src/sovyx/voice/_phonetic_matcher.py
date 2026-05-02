"""Phonetic similarity matching via espeak-ng — Phase 8 / T8.12.

When the operator's configured wake word doesn't have a pre-trained
ONNX model in the pretrained pool (``~/.sovyx/wake_word_models/pretrained/``),
the wake-word resolver consults this module to find the
phonetically closest model. Example: ``wake_word="Jhonatan"`` falls
back to a "Jonny" pre-trained model via espeak-ng's IPA phoneme
output + Levenshtein distance.

espeak-ng is an **optional dependency**. The matcher detects
availability lazily on first use:

* When the ``espeak-ng`` binary is on the operator's PATH, the
  matcher works (subprocess call: ``espeak-ng -q --ipa "text"``).
* When absent, ``is_available`` returns ``False`` and every
  ``to_phonemes`` / ``find_closest`` call returns the empty
  string / ``None`` — callers fall through to the STT detector
  (Phase 8 / T8.17).

The dependency is intentionally subprocess-based rather than via
the ``py-espeak-ng`` Python wrapper:

* Python wrapper has C-level bindings that pin specific espeak-ng
  versions and tend to break on Windows wheels.
* Subprocess works on every OS where the espeak-ng binary is
  packaged (Linux apt, macOS brew, Windows installer).
* Performance overhead (~10 ms per word) is negligible — phoneme
  conversion happens once per mind at boot, not per audio frame.

Reference: master mission ``MISSION-voice-final-skype-grade-2026.md``
§Phase 8 / T8.12. Operator debt:
``OPERATOR-DEBT-MASTER-2026-05-01.md`` (T8.12 is autonomous-Claude,
no operator decision needed).
"""

from __future__ import annotations

import shutil
import subprocess  # nosec B404 — espeak-ng is shelled with bounded args
import unicodedata

from sovyx.observability.logging import get_logger

logger = get_logger(__name__)


_ESPEAK_BINARY = "espeak-ng"
"""Binary name. ``shutil.which`` resolves the absolute path."""

_ESPEAK_TIMEOUT_S = 5.0
"""Subprocess timeout per phoneme conversion. The binary is fast
(~10 ms typical); 5 s caps a runaway invocation. A timeout is
treated identically to "espeak unavailable" — the matcher returns
empty + the resolver falls through to STT."""


def _ascii_fold(text: str) -> str:
    """Normalize text by stripping diacritics + lowercasing.

    Used to pre-process input before phoneme conversion so
    ``"Lúcia"`` and ``"Lucia"`` compare equal at distance 0 even
    when one of the candidates wasn't normalised by the operator.
    Mirrors the existing ``_wake_word_stt_fallback`` ASCII-fold
    pattern for consistency.
    """
    decomposed = unicodedata.normalize("NFKD", text)
    return "".join(c for c in decomposed if not unicodedata.combining(c)).lower()


def _levenshtein(s: str, t: str) -> int:
    """Levenshtein edit distance between two strings.

    Same DP implementation as ``brain/concept_repo._levenshtein``.
    For phoneme strings the inputs are short (≤30 chars typical),
    so the O(n·m) cost is negligible.
    """
    if len(s) < len(t):
        return _levenshtein(t, s)
    if not t:
        return len(s)
    prev = list(range(len(t) + 1))
    for i, sc in enumerate(s):
        curr = [i + 1]
        for j, tc in enumerate(t):
            cost = 0 if sc == tc else 1
            curr.append(min(curr[j] + 1, prev[j + 1] + 1, prev[j] + cost))
        prev = curr
    return prev[-1]


class PhoneticMatcher:
    """espeak-ng-backed phonetic similarity for wake-word fallback.

    Args:
        language: BCP-47 code passed to ``espeak-ng -v <lang>``.
            Default ``"en-us"`` because the pretrained pool ships
            primarily English (per master mission §Phase 8 / T8.11).
            Operators with non-English wake words should pass the
            mind's ``MindConfig.voice_language``.
        enabled: Optional explicit override. ``True`` forces use
            (raises if espeak-ng is not on PATH); ``False`` disables
            the matcher entirely (callers see ``is_available =
            False``); ``None`` (default) auto-detects via
            ``shutil.which("espeak-ng")``.

    Thread safety:
        Safe to share — each ``to_phonemes`` call spawns its own
        subprocess; no shared mutable state.
    """

    def __init__(
        self,
        *,
        language: str = "en-us",
        enabled: bool | None = None,
    ) -> None:
        self._language = language
        if enabled is False:
            self._available = False
            self._binary_path: str | None = None
        else:
            resolved = shutil.which(_ESPEAK_BINARY)
            if enabled is True and resolved is None:
                msg = (
                    "espeak-ng binary not found on PATH; "
                    "PhoneticMatcher(enabled=True) requires installation "
                    "(apt install espeak-ng / brew install espeak / "
                    "https://github.com/espeak-ng/espeak-ng/releases)"
                )
                raise RuntimeError(msg)
            self._available = resolved is not None
            self._binary_path = resolved

    @property
    def is_available(self) -> bool:
        """``True`` when espeak-ng is installed and operational."""
        return self._available

    def to_phonemes(self, text: str) -> str:
        """Convert ``text`` to an IPA phoneme string.

        Returns the empty string when:

        * espeak-ng is not available, OR
        * ``text`` is empty / whitespace-only, OR
        * the subprocess fails (timeout, non-zero exit, OS error).

        Empty return means "no useful phoneme info" — the caller
        treats it as a non-match.
        """
        if not self._available or not self._binary_path:
            return ""
        cleaned = text.strip()
        if not cleaned:
            return ""
        try:
            result = subprocess.run(  # nosec B603 — bounded args, no shell, fixed binary
                [
                    self._binary_path,
                    "-q",
                    "--ipa",
                    "-v",
                    self._language,
                    cleaned,
                ],
                capture_output=True,
                text=True,
                timeout=_ESPEAK_TIMEOUT_S,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            logger.debug(
                "voice.phonetic.espeak_failed",
                **{
                    "voice.text_len": len(cleaned),
                    "voice.error": str(exc),
                    "voice.error_type": type(exc).__name__,
                },
            )
            return ""
        if result.returncode != 0:
            logger.debug(
                "voice.phonetic.espeak_nonzero",
                **{
                    "voice.returncode": result.returncode,
                    "voice.stderr_prefix": (result.stderr or "")[:120],
                },
            )
            return ""
        return result.stdout.strip()

    def distance(self, a: str, b: str) -> int:
        """Levenshtein distance between two phoneme strings.

        Distance is computed AFTER ASCII-folding both inputs — so
        case + diacritics on un-phonemized fallback paths don't
        inflate the metric.
        """
        return _levenshtein(_ascii_fold(a), _ascii_fold(b))

    def find_closest(
        self,
        query: str,
        candidates: list[str],
        *,
        max_distance: int,
    ) -> tuple[str, int] | None:
        """Find the candidate with the smallest phoneme distance to ``query``.

        Args:
            query: The operator-configured wake word (e.g. ``"Jhonatan"``).
            candidates: List of pretrained model names (e.g.
                ``["jonny", "lucia", "marie"]``).
            max_distance: Reject matches above this Levenshtein
                distance. Caller-controlled so operators can tune
                via ``EngineConfig.tuning.voice.wake_word_phonetic_max_distance``.

        Returns:
            ``(closest_name, distance)`` when a match within
            threshold exists; ``None`` when:

            * the matcher is unavailable, OR
            * ``candidates`` is empty, OR
            * the smallest distance exceeds ``max_distance``.

        Tie-breaker on equal distance: alphabetical (so the result
        is deterministic across runs — important for telemetry
        consistency).
        """
        if not self._available or not candidates:
            return None
        query_phonemes = self.to_phonemes(query)
        if not query_phonemes:
            # Phoneme conversion failed for the query itself; fall back
            # to direct ASCII-fold comparison of the raw text.
            query_phonemes = _ascii_fold(query)

        best: tuple[str, int] | None = None
        # Iterate sorted to stabilise tie-breaker behaviour.
        for candidate in sorted(candidates):
            cand_phonemes = self.to_phonemes(candidate) or _ascii_fold(candidate)
            d = _levenshtein(query_phonemes, cand_phonemes)
            if best is None or d < best[1]:
                best = (candidate, d)
        if best is None or best[1] > max_distance:
            return None
        return best


__all__ = [
    "PhoneticMatcher",
]
