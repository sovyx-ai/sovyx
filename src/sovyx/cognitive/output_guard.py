"""Sovyx OutputGuard — post-LLM response safety filter.

Checks the LLM response for harmful content BEFORE delivering to the user.
Uses the same tiered pattern system as the input filter (safety_patterns).

Safety cascade (v0.7):
    1. **Regex fast-path** — compiled patterns for EN/PT (<1ms).
    2. **LLM classifier** — any language (~200-400ms, optional).
    3. Action based on tier: replace (strict/child_safe) or redact (standard).

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

from sovyx.cognitive.safety_audit import FilterAction, FilterDirection, get_audit_trail
from sovyx.cognitive.safety_patterns import (
    FilterMatch,
    PatternCategory,
    check_content,
    resolve_patterns,
)
from sovyx.observability.logging import get_logger
from sovyx.observability.metrics import get_metrics

if TYPE_CHECKING:
    from sovyx.cognitive.safety_classifier import SafetyCategory, SafetyVerdict
    from sovyx.llm.router import LLMRouter
    from sovyx.mind.config import SafetyConfig

logger = get_logger(__name__)

# Safe replacement message (professional, non-alarming)
_SAFE_REPLACEMENT = (
    "I'm not able to provide that information. "  # Default EN; use safety_i18n for localized
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

    Args:
        safety_config: SafetyConfig reference (read dynamically per call).
        llm_router: Optional LLM router for language-agnostic classification.
    """

    def __init__(
        self,
        safety_config: SafetyConfig,
        llm_router: LLMRouter | None = None,
    ) -> None:
        self._safety = safety_config
        self._llm_router = llm_router

    def check(self, response_text: str) -> OutputFilterResult:
        """Check LLM response via regex only (synchronous).

        For async cascade (regex→LLM), use ``check_async()``.

        Args:
            response_text: Raw LLM response text.

        Returns:
            OutputFilterResult with possibly modified text.
        """
        if not response_text:
            return _pass_result(response_text)

        patterns = resolve_patterns(self._safety)
        if not patterns:
            return _pass_result(response_text)

        m = get_metrics()
        with m.measure_latency(
            m.safety_filter_latency,
            {"direction": "output"},
        ):
            match = check_content(response_text, self._safety)

        if not match.matched:
            # Shadow mode evaluation (log-only, never blocks)
            from sovyx.cognitive.shadow_mode import evaluate_shadow

            evaluate_shadow(
                response_text,
                self._safety,
                FilterDirection.OUTPUT,
            )
            return _pass_result(response_text)

        return self._act_on_match(response_text, match)

    async def check_async(self, response_text: str) -> OutputFilterResult:
        """Check LLM response via regex→LLM cascade (async).

        Cascade:
            1. Regex fast-path — if matched, act immediately.
            2. LLM classifier — if regex passed and llm_router available.
            3. If both pass → no filtering.

        Args:
            response_text: Raw LLM response text.

        Returns:
            OutputFilterResult with possibly modified text.
        """
        if not response_text:
            return _pass_result(response_text)

        patterns = resolve_patterns(self._safety)
        if not patterns:
            return _pass_result(response_text)

        # ── 1. Regex fast-path ──
        m = get_metrics()
        with m.measure_latency(
            m.safety_filter_latency,
            {"direction": "output"},
        ):
            regex_match = check_content(response_text, self._safety)

        if regex_match.matched:
            return self._act_on_match(response_text, regex_match)

        # ── 2. LLM classifier ──
        if self._llm_router is not None:
            verdict = await self._classify_with_llm(response_text)
            if verdict is not None and not verdict.safe:
                llm_category = _map_safety_category(verdict.category)
                llm_match = FilterMatch(
                    matched=True,
                    pattern=None,
                    category=llm_category,
                    tier=None,
                )
                logger.warning(
                    "output_filtered_llm",
                    category=(llm_category.value if llm_category else "unknown"),
                    latency_ms=verdict.latency_ms,
                )
                return self._act_on_match(response_text, llm_match)

        # ── 3. Shadow mode evaluation (log-only, never blocks) ──
        from sovyx.cognitive.shadow_mode import evaluate_shadow

        evaluate_shadow(
            response_text,
            self._safety,
            FilterDirection.OUTPUT,
        )

        return _pass_result(response_text)

    def _act_on_match(
        self,
        response_text: str,
        match: FilterMatch,
    ) -> OutputFilterResult:
        """Determine action based on tier and execute."""
        if self._safety.child_safe_mode:
            return self._replace(response_text, match)

        if self._safety.content_filter == "strict":
            return self._replace(response_text, match)

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
        get_audit_trail().record(
            direction=FilterDirection.OUTPUT,
            action=FilterAction.REPLACED,
            match=match,
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
            # LLM-detected: no regex pattern to redact with → replace
            return self._replace(text, match)

        redacted = match.pattern.regex.sub(_REDACT_MARKER, text)

        patterns = resolve_patterns(self._safety)
        for p in patterns:
            if p is not match.pattern:
                redacted = p.regex.sub(_REDACT_MARKER, redacted)

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
        get_audit_trail().record(
            direction=FilterDirection.OUTPUT,
            action=FilterAction.REDACTED,
            match=match,
        )
        return OutputFilterResult(
            text=redacted,
            filtered=True,
            action="redact",
            match=match,
        )

    async def _classify_with_llm(
        self,
        content: str,
    ) -> SafetyVerdict | None:
        """Classify output using LLM safety classifier.

        Returns SafetyVerdict or None on error. Never raises.
        """
        try:
            from sovyx.cognitive.safety_classifier import (
                classify_content,
            )

            assert self._llm_router is not None
            return await classify_content(content, self._llm_router)
        except Exception:  # noqa: BLE001
            logger.debug(
                "output_guard_llm_classifier_error",
                exc_info=True,
            )
            return None


def _map_safety_category(
    category: SafetyCategory | None,
) -> PatternCategory | None:
    """Map SafetyCategory to PatternCategory by value string."""
    if category is None:
        return None
    try:
        return PatternCategory(category.value)
    except (ValueError, AttributeError):
        return None
