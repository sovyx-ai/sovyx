"""Sovyx PerceivePhase — validate, enrich, and classify input.

First phase of the cognitive loop: raw input → enriched Perception.
"""

from __future__ import annotations

import dataclasses
from datetime import UTC, datetime
from typing import TYPE_CHECKING, ClassVar

from sovyx.engine.errors import PerceptionError
from sovyx.observability.logging import get_logger

if TYPE_CHECKING:
    from sovyx.engine.types import PerceptionType

logger = get_logger(__name__)

# Complex query markers (SPE-003 §3.2)
_COMPLEX_MARKERS = frozenset({
    "why",
    "how does",
    "explain",
    "compare",
    "analyze",
    "what if",
    "implications",
    "trade-off",
    "tradeoff",
    "difference between",
})

# Simple plugin triggers
_SIMPLE_MARKERS = frozenset({
    "set timer",
    "weather",
    "reminder",
    "what time",
})


@dataclasses.dataclass
class Perception:
    """Raw input to the cognitive loop."""

    id: str
    type: PerceptionType
    source: str
    content: str
    person_id: str | None = None
    channel_id: str | None = None
    priority: int = 10
    metadata: dict[str, object] = dataclasses.field(default_factory=dict)
    created_at: datetime = dataclasses.field(
        default_factory=lambda: datetime.now(tz=UTC)
    )


class PerceivePhase:
    """Validate, enrich, and classify complexity of a Perception.

    1. Validate required fields (content not empty, type valid)
    2. Normalize content (strip, truncate if > MAX_INPUT_CHARS)
    3. Classify complexity → perception.metadata["complexity"]
    4. Return enriched Perception
    """

    MAX_INPUT_CHARS: ClassVar[int] = 10_000

    async def process(self, perception: Perception) -> Perception:
        """Process and enrich a perception.

        Args:
            perception: Raw input perception.

        Returns:
            Enriched perception with complexity metadata.

        Raises:
            PerceptionError: If perception is invalid.
        """
        # Validate
        if not perception.content or not perception.content.strip():
            msg = "Perception content is empty"
            raise PerceptionError(msg)

        # Normalize
        perception.content = perception.content.strip()

        if len(perception.content) > self.MAX_INPUT_CHARS:
            logger.warning(
                "perception_truncated",
                original_length=len(perception.content),
                max_chars=self.MAX_INPUT_CHARS,
            )
            perception.content = perception.content[: self.MAX_INPUT_CHARS]

        # Classify complexity
        perception.metadata["complexity"] = self.classify_complexity(
            perception.content
        )

        logger.debug(
            "perception_processed",
            perception_id=perception.id,
            type=perception.type.value,
            complexity=perception.metadata["complexity"],
        )
        return perception

    @staticmethod
    def classify_complexity(content: str) -> float:
        """Classify complexity 0.0 (trivial) to 1.0 (complex).

        Heuristics (no LLM call — fast):
        - Length: >50 words → +0.3, >20 words → +0.15
        - Complex markers: "why", "explain", etc. → +0.15
        - Multi-question: ? count > 1 → +0.2
        - Simple triggers: "weather", "timer" → -0.2

        Result determines model routing:
        - complexity < 0.3 → fast_model (haiku)
        - complexity >= 0.3 → default_model (sonnet)
        """
        score = 0.3  # baseline

        lower = content.lower()
        words = content.split()
        word_count = len(words)

        # Length
        if word_count > 50:  # noqa: PLR2004
            score += 0.3
        elif word_count > 20:  # noqa: PLR2004
            score += 0.15

        # Complex markers
        for marker in _COMPLEX_MARKERS:
            if marker in lower:
                score += 0.15
                break

        # Multi-question
        question_count = content.count("?")
        if question_count > 1:
            score += 0.2

        # Simple triggers
        for trigger in _SIMPLE_MARKERS:
            if trigger in lower:
                score -= 0.2
                break

        return max(0.0, min(1.0, score))
