"""Sovyx OutputGuard — post-LLM response safety filter.

Checks the LLM response for harmful content BEFORE delivering to the user.
Uses the same tiered pattern system as the input filter (safety_patterns).

Behavior per tier:
- **none**: no output filtering (zero overhead).
- **standard**: redact matched segments with ``[content filtered]``.
- **strict**: replace entire response with a safe generic message.
- **child_safe**: replace entire response (zero tolerance).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING

from sovyx.cognitive.safety_patterns import (
    FilterMatch,
    check_content,
    resolve_patterns,
)
from sovyx.observability.logging import get_logger

if TYPE_CHECKING:
    from sovyx.mind.config import SafetyConfig

logger = get_logger(__name__)

# Safe replacement message (professional, non-alarming)
_SAFE_REPLACEMENT = (
    "I'm not able to provide that information. "
    "Let me know if there's something else I can help with."
)

_REDACT_MARKER = "[content filtered]"


@dataclass(frozen=True, slots=True)
class OutputFilterResult:
    """Result of output safety filtering.

    Attributes:
        text: The (possibly modified) response text.
        filtered: Whether any content was filtered.
        action: What action was taken: "pass", "redact", or "replace".
        match: The FilterMatch details (None if no match).
    """

    text: str
    filtered: bool
    action: str  # "pass" | "redact" | "replace"
    match: FilterMatch | None = None


# Singleton "no filtering" result factory
def _pass_result(text: str) -> OutputFilterResult:
    return OutputFilterResult(text=text, filtered=False, action="pass")


class OutputGuard:
    """Post-LLM response safety filter.

    Reads SafetyConfig dynamically on each call so dashboard changes
    take effect immediately.
    """

    def __init__(self, safety_config: SafetyConfig) -> None:
        self._safety = safety_config

    def check(self, response_text: str) -> OutputFilterResult:
        """Check LLM response and filter if necessary.

        Args:
            response_text: Raw LLM response text.

        Returns:
            OutputFilterResult with possibly modified text.
        """
        if not response_text:
            return _pass_result(response_text)

        # No filtering when filter is "none" (and child_safe off)
        patterns = resolve_patterns(self._safety)
        if not patterns:
            return _pass_result(response_text)

        # Check for matches
        match = check_content(response_text, self._safety)
        if not match.matched:
            return _pass_result(response_text)

        # Determine action based on tier
        if self._safety.child_safe_mode:
            return self._replace(response_text, match)

        if self._safety.content_filter == "strict":
            return self._replace(response_text, match)

        # Standard: redact the matched segment
        return self._redact(response_text, match)

    def _replace(
        self,
        original: str,
        match: FilterMatch,
    ) -> OutputFilterResult:
        """Replace entire response with safe message."""
        category = match.category.value if match.category else "unknown"
        logger.warning(
            "output_filtered_replaced",
            category=category,
            tier=match.tier.value if match.tier else "unknown",
            original_length=len(original),
        )
        return OutputFilterResult(
            text=_SAFE_REPLACEMENT,
            filtered=True,
            action="replace",
            match=match,
        )

    def _redact(
        self,
        text: str,
        match: FilterMatch,
    ) -> OutputFilterResult:
        """Redact matched segments from the response."""
        if match.pattern is None:
            return _pass_result(text)

        # Replace all occurrences of the matched pattern
        redacted = match.pattern.regex.sub(_REDACT_MARKER, text)

        # Also check and redact any other matching patterns
        patterns = resolve_patterns(self._safety)
        for p in patterns:
            if p is not match.pattern:
                redacted = p.regex.sub(_REDACT_MARKER, redacted)

        # Clean up multiple consecutive markers
        redacted = re.sub(
            rf"(?:{re.escape(_REDACT_MARKER)}\s*)+",
            _REDACT_MARKER + " ",
            redacted,
        ).strip()

        category = match.category.value if match.category else "unknown"
        logger.warning(
            "output_filtered_redacted",
            category=category,
            tier=match.tier.value if match.tier else "unknown",
            redacted_chars=len(text) - len(redacted),
        )
        return OutputFilterResult(
            text=redacted,
            filtered=True,
            action="redact",
            match=match,
        )
