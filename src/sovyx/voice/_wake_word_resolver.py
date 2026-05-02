"""Wake-word model resolution with phonetic fallback — Phase 8 / T8.12.

Bridges three resolution strategies for a per-mind wake word:

1. **exact** — ``<pretrained_dir>/<wake_word>.onnx`` exists. Fastest;
   the model was deliberately pre-trained or operator-installed
   for this exact name.
2. **phonetic** — no exact match, but :class:`PhoneticMatcher`
   finds a pretrained model with phoneme distance ≤ threshold.
   Re-uses the closest pre-trained model (e.g.
   ``"Jhonatan"`` → ``"jonny.onnx"``).
3. **none** — no exact match, phonetic matcher unavailable or no
   acceptable candidate. Caller falls through to STT-based
   detection (Phase 8 / T8.17).

The resolver is **pure** — no global state, no I/O at construction
time. All filesystem interactions happen at ``resolve()`` time.

Reference: master mission ``MISSION-voice-final-skype-grade-2026.md``
§Phase 8 / T8.12.
"""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING

from sovyx.observability.logging import get_logger

if TYPE_CHECKING:
    from sovyx.voice._phonetic_matcher import PhoneticMatcher

logger = get_logger(__name__)


class WakeWordResolutionStrategy(StrEnum):
    """Which path the resolver took to find the model."""

    EXACT = "exact"
    """Pretrained pool had ``<wake_word>.onnx`` directly."""

    PHONETIC = "phonetic"
    """Pretrained pool had a phonetically close enough match."""

    NONE = "none"
    """No suitable model — caller falls through to STT detection."""


@dataclass(frozen=True, slots=True)
class WakeWordResolution:
    """Outcome of a :meth:`WakeWordModelResolver.resolve` call.

    Attributes:
        strategy: Which path the resolver took.
        model_path: Resolved ``.onnx`` path, or ``None`` for
            ``strategy == NONE``.
        matched_name: For ``PHONETIC``, the pretrained name that
            matched (e.g. ``"jonny"``). For ``EXACT``, the
            ASCII-folded wake word. For ``NONE``, empty string.
        phoneme_distance: For ``PHONETIC``, the Levenshtein
            distance. ``0`` for ``EXACT``; ``-1`` sentinel for
            ``NONE``.
    """

    strategy: WakeWordResolutionStrategy
    model_path: Path | None
    matched_name: str
    phoneme_distance: int


class PretrainedModelRegistry:
    """Filesystem listing of pretrained wake-word models.

    Looks in ``<data_dir>/wake_word_models/pretrained/`` for
    ``*.onnx`` files. Filename stem (without ``.onnx``) is the
    model's name as far as the resolver is concerned — so
    ``jonny.onnx`` is matched against the wake word ``"Jonny"``.

    Names are normalised: ASCII-fold + lowercase to give a stable
    matching surface regardless of how the operator capitalises or
    accents their ``MindConfig.wake_word``.

    Args:
        models_dir: Directory containing the pretrained ``.onnx``
            files. Created on demand by training pipelines (Phase 8
            / T8.13-T8.15); the registry treats a missing directory
            as "empty pool".
    """

    def __init__(self, models_dir: Path) -> None:
        self._models_dir = Path(models_dir)

    @property
    def models_dir(self) -> Path:
        """Directory containing the pretrained ``.onnx`` files."""
        return self._models_dir

    def list_available(self) -> list[str]:
        """Return the normalised names of every pretrained model.

        Names are stripped of the ``.onnx`` extension and ASCII-folded.
        The list is sorted for deterministic iteration order.

        Returns:
            Empty list when ``models_dir`` does not exist or is
            empty. Never raises.
        """
        if not self._models_dir.is_dir():
            return []
        names: list[str] = []
        for path in self._models_dir.iterdir():
            if path.is_file() and path.suffix.lower() == ".onnx":
                names.append(_normalise(path.stem))
        return sorted(names)

    def model_path(self, name: str) -> Path | None:
        """Return the absolute path to ``<name>.onnx`` if it exists.

        Matching is normalised: ``"Jonny"`` matches ``jonny.onnx``,
        ``JONNY.onnx``, etc. via ASCII-fold + lowercase. Returns
        ``None`` when no matching file exists.
        """
        if not self._models_dir.is_dir():
            return None
        target = _normalise(name)
        if not target:
            return None
        for path in self._models_dir.iterdir():
            if (
                path.is_file()
                and path.suffix.lower() == ".onnx"
                and _normalise(path.stem) == target
            ):
                return path.resolve()
        return None


class WakeWordModelResolver:
    """Resolve a configured wake word to a pretrained model path.

    Args:
        registry: :class:`PretrainedModelRegistry` (filesystem source
            of truth).
        phonetic_matcher: Optional :class:`PhoneticMatcher` for
            phoneme-similarity fallback. When ``None`` or when
            ``matcher.is_available`` is ``False``, only ``EXACT``
            resolution is attempted.
        max_phoneme_distance: Maximum Levenshtein distance accepted
            as a phonetic match. From
            ``EngineConfig.tuning.voice.wake_word_phonetic_max_distance``.
    """

    def __init__(
        self,
        *,
        registry: PretrainedModelRegistry,
        phonetic_matcher: PhoneticMatcher | None = None,
        max_phoneme_distance: int = 3,
    ) -> None:
        self._registry = registry
        self._matcher = phonetic_matcher
        self._max_distance = max_phoneme_distance

    def resolve(self, wake_word: str) -> WakeWordResolution:
        """Resolve ``wake_word`` to a model path with strategy.

        Resolution order:

        1. Exact filename match (ASCII-fold-normalised) → ``EXACT``.
        2. Phonetic similarity match within threshold → ``PHONETIC``.
        3. Otherwise → ``NONE``.

        Args:
            wake_word: The operator-configured wake word from
                ``MindConfig.effective_wake_word``. Empty / whitespace
                inputs return ``NONE`` directly.

        Returns:
            :class:`WakeWordResolution` with the selected strategy
            + path + matched name + phoneme distance.
        """
        cleaned = wake_word.strip()
        if not cleaned:
            return WakeWordResolution(
                strategy=WakeWordResolutionStrategy.NONE,
                model_path=None,
                matched_name="",
                phoneme_distance=-1,
            )

        # Strategy 1: exact match.
        exact_path = self._registry.model_path(cleaned)
        if exact_path is not None:
            return WakeWordResolution(
                strategy=WakeWordResolutionStrategy.EXACT,
                model_path=exact_path,
                matched_name=_normalise(cleaned),
                phoneme_distance=0,
            )

        # Strategy 2: phonetic match.
        if self._matcher is not None and self._matcher.is_available:
            candidates = self._registry.list_available()
            match = self._matcher.find_closest(
                cleaned,
                candidates,
                max_distance=self._max_distance,
            )
            if match is not None:
                matched_name, distance = match
                phonetic_path = self._registry.model_path(matched_name)
                if phonetic_path is not None:
                    logger.info(
                        "voice.wake_word.phonetic_match_resolved",
                        **{
                            "voice.wake_word": cleaned,
                            "voice.matched_name": matched_name,
                            "voice.phoneme_distance": distance,
                        },
                    )
                    return WakeWordResolution(
                        strategy=WakeWordResolutionStrategy.PHONETIC,
                        model_path=phonetic_path,
                        matched_name=matched_name,
                        phoneme_distance=distance,
                    )

        # Strategy 3: nothing usable.
        return WakeWordResolution(
            strategy=WakeWordResolutionStrategy.NONE,
            model_path=None,
            matched_name="",
            phoneme_distance=-1,
        )


def _normalise(text: str) -> str:
    """ASCII-fold + lowercase normalisation.

    Mirrors the convention used by ``_wake_word_stt_fallback`` so
    matching is consistent across the wake-word stack.
    """
    decomposed = unicodedata.normalize("NFKD", text)
    return "".join(c for c in decomposed if not unicodedata.combining(c)).lower()


__all__ = [
    "PretrainedModelRegistry",
    "WakeWordModelResolver",
    "WakeWordResolution",
    "WakeWordResolutionStrategy",
]
