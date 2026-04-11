"""Sovyx AttendPhase — filter perceptions by priority and safety.

Second phase: decides if a perception should be processed or filtered.

Safety cascade (v0.7):
    1. **Regex fast-path** — compiled patterns for EN/PT (<1ms).
       If matched → block immediately (zero latency).
    2. **LLM classifier** — any language (~200-400ms).
       Only runs if regex didn't match AND content_filter != "none".
       If UNSAFE → block with LLM-provided category.
    3. If both pass → perception accepted.

The LLM classifier is optional — if no ``llm_router`` is provided,
only regex patterns are used (backward compatible).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from sovyx.cognitive.safety_audit import FilterAction, FilterDirection, get_audit_trail
from sovyx.cognitive.safety_escalation import get_escalation_tracker
from sovyx.cognitive.safety_patterns import (
    FilterMatch,
    PatternCategory,
    check_content,
)
from sovyx.observability.logging import get_logger
from sovyx.observability.metrics import get_metrics

if TYPE_CHECKING:
    from sovyx.cognitive.perceive import Perception
    from sovyx.cognitive.safety_classifier import SafetyCategory, SafetyVerdict
    from sovyx.llm.router import LLMRouter
    from sovyx.mind.config import SafetyConfig

logger = get_logger(__name__)


class AttendPhase:
    """Filter perceptions by priority and safety.

    Safety cascade:
        1. Regex fast-path (EN/PT, <1ms)
        2. LLM classifier (any language, ~200-400ms, optional)

    The safety config is read dynamically on each ``process()`` call so
    that runtime changes via the dashboard take effect immediately
    without restarting the engine.

    Args:
        safety_config: SafetyConfig reference (read dynamically per call).
        llm_router: Optional LLM router for language-agnostic classification.
            When None, only regex patterns are used.
    """

    def __init__(
        self,
        safety_config: SafetyConfig,
        llm_router: LLMRouter | None = None,
    ) -> None:
        self._safety = safety_config
        self._llm_router = llm_router

    def _should_block(self, reason: str) -> bool:
        """Check if content should be blocked or allowed (shadow mode)."""
        if self._safety.shadow_mode:
            logger.info("shadow_mode_would_block", reason=reason)
            return False
        return True

    async def process(self, perception: Perception) -> bool:
        """Check if perception should be processed.

        Cascade:
            1. Rate-limit check (escalation tracker)
            2. Regex fast-path (compiled patterns)
            3. LLM classifier (if regex passed and llm_router available)
            4. Priority check

        Args:
            perception: Enriched perception from PerceivePhase.

        Returns:
            True if perception passes all filters, False if blocked.
        """
        # ── 0. Escalation check ──
        tracker = get_escalation_tracker()
        if tracker.is_rate_limited(perception.source):
            logger.warning(
                "perception_rate_limited",
                perception_id=perception.id,
                source=perception.source,
            )
            return False

        m = get_metrics()

        # ── 0a. Multi-turn injection check ──
        if self._safety.content_filter != "none":
            from sovyx.cognitive.injection_tracker import (
                InjectionVerdict,
                get_injection_tracker,
            )

            mt_analysis = get_injection_tracker().analyze(
                perception.source,
                perception.content,
            )
            if mt_analysis.verdict in (
                InjectionVerdict.SUSPICIOUS,
                InjectionVerdict.ESCALATE,
            ):
                logger.warning(
                    "perception_filtered_multi_turn_injection",
                    perception_id=perception.id,
                    score=mt_analysis.cumulative_score,
                )
                m.safety_blocks.add(1, {"reason": "multi_turn_injection"})
                get_audit_trail().record(
                    direction=FilterDirection.INPUT,
                    action=FilterAction.BLOCKED,
                    match=FilterMatch(matched=True),
                )
                if self._should_block("multi_turn"):
                    return False

        # ── 0b. Custom rules + banned topics ──
        from sovyx.cognitive.custom_rules import check_banned_topics, check_custom_rules

        custom_match = check_custom_rules(perception.content, self._safety)
        if custom_match.matched and custom_match.action == "block":
            logger.warning(
                "perception_filtered_custom_rule",
                perception_id=perception.id,
                rule=custom_match.rule_name,
            )
            m.safety_blocks.add(1, {"reason": "custom_rule"})
            get_audit_trail().record(
                direction=FilterDirection.INPUT,
                action=FilterAction.BLOCKED,
                match=FilterMatch(matched=True),
            )
            if self._should_block("custom_rule"):
                return False

        topic_match = check_banned_topics(perception.content, self._safety)
        if topic_match.matched:
            logger.warning(
                "perception_filtered_banned_topic",
                perception_id=perception.id,
                topic=topic_match.rule_name,
            )
            m.safety_blocks.add(1, {"reason": "banned_topic"})
            get_audit_trail().record(
                direction=FilterDirection.INPUT,
                action=FilterAction.BLOCKED,
                match=FilterMatch(matched=True),
            )
            if self._should_block("banned_topic"):
                return False

        # ── 1. Regex fast-path ──
        with m.measure_latency(m.safety_filter_latency, {"direction": "input"}):
            regex_result = check_content(perception.content, self._safety)

        if regex_result.matched:
            logger.warning(
                "perception_filtered_safety",
                perception_id=perception.id,
                reason="blocked_content",
                method="regex",
                category=(regex_result.category.value if regex_result.category else "unknown"),
                tier=(regex_result.tier.value if regex_result.tier else "unknown"),
            )
            get_audit_trail().record(
                direction=FilterDirection.INPUT,
                action=FilterAction.BLOCKED,
                match=regex_result,
            )
            tracker.record_block(perception.source)
            if self._should_block("regex"):
                return False

        # ── 1b. Multi-turn injection context tracking ──
        if self._safety.content_filter != "none":
            from sovyx.cognitive.injection_tracker import (
                InjectionVerdict,
                get_injection_tracker,
            )

            conv_id = perception.metadata.get("conversation_id", perception.source)
            injection_analysis = get_injection_tracker().analyze(
                str(conv_id),
                perception.content,
            )
            if injection_analysis.verdict == InjectionVerdict.ESCALATE:
                logger.warning(
                    "perception_filtered_safety",
                    perception_id=perception.id,
                    reason="blocked_content",
                    method="multi_turn_injection",
                    cumulative_score=injection_analysis.cumulative_score,
                    consecutive=injection_analysis.consecutive_suspicious,
                    signals=injection_analysis.signals,
                )
                m.safety_blocks.add(1, {"reason": "multi_turn_injection"})
                get_audit_trail().record(
                    direction=FilterDirection.INPUT,
                    action=FilterAction.BLOCKED,
                    match=FilterMatch(
                        matched=True,
                        pattern=None,
                        category=PatternCategory.INJECTION,
                        tier=None,
                    ),
                )
                tracker.record_block(perception.source)
                if self._should_block("multi_turn"):
                    return False

        # ── 2. LLM classifier (if available and filter active) ──
        if self._llm_router is not None and self._safety.content_filter != "none":
            verdict = await self._classify_with_llm(perception.content)
            if verdict is not None and not verdict.safe:
                # Map LLM category to PatternCategory for audit trail
                llm_category = _map_safety_category(verdict.category)
                llm_match = FilterMatch(
                    matched=True,
                    pattern=None,
                    category=llm_category,
                    tier=None,
                )
                logger.warning(
                    "perception_filtered_safety",
                    perception_id=perception.id,
                    reason="blocked_content",
                    method="llm",
                    category=(llm_category.value if llm_category else "unknown"),
                    latency_ms=verdict.latency_ms,
                )
                get_audit_trail().record(
                    direction=FilterDirection.INPUT,
                    action=FilterAction.BLOCKED,
                    match=llm_match,
                )
                tracker.record_block(perception.source)
                if self._should_block("llm"):
                    return False

        # ── 3. Priority check ──
        if perception.priority < 0:
            logger.debug(
                "perception_filtered_priority",
                perception_id=perception.id,
                priority=perception.priority,
            )
            return False

        # ── 4. Shadow mode evaluation (log-only, never blocks) ──
        from sovyx.cognitive.shadow_mode import evaluate_shadow

        evaluate_shadow(
            perception.content,
            self._safety,
            FilterDirection.INPUT,
        )

        logger.debug(
            "perception_accepted",
            perception_id=perception.id,
        )
        return True

    async def _classify_with_llm(
        self,
        content: str,
    ) -> SafetyVerdict | None:
        """Classify content using LLM safety classifier.

        Returns SafetyVerdict or None if classification fails/unavailable.
        Never raises — all errors handled internally.
        """
        try:
            from sovyx.cognitive.safety_classifier import classify_content

            assert self._llm_router is not None  # Caller already checked
            return await classify_content(content, self._llm_router)
        except Exception:  # noqa: BLE001
            logger.debug(
                "attend_llm_classifier_error",
                exc_info=True,
            )
            return None


def _map_safety_category(
    category: SafetyCategory | None,
) -> PatternCategory | None:
    """Map SafetyCategory (from classifier) to PatternCategory (for audit).

    Both enums have the same values so we map by value string.
    Returns None if mapping fails.
    """
    if category is None:
        return None
    try:
        return PatternCategory(category.value)
    except (ValueError, AttributeError):
        return None
