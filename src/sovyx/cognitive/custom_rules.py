"""Sovyx Custom Rules Engine — owner-defined safety patterns.

Allows Mind owners to define custom blocking/logging rules via mind.yaml:

```yaml
safety:
  custom_rules:
    - name: "No competitor mentions"
      pattern: "\\b(competitor_a|competitor_b)\\b"
      action: block
      message: "I can't discuss competitor products."
    - name: "Log medical queries"
      pattern: "\\b(diagnosis|prescription|medication)\\b"
      action: log
  banned_topics:
    - politics
    - religion
```

Rules are compiled once at check time and cached.
Banned topics use the LLM classifier for semantic matching.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING

from sovyx.observability.logging import get_logger
from sovyx.observability.metrics import get_metrics

if TYPE_CHECKING:
    from sovyx.mind.config import SafetyConfig

logger = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class CustomRuleMatch:
    """Result of custom rule evaluation.

    Attributes:
        matched: Whether any rule matched.
        rule_name: Name of the first matched rule.
        action: Action to take (block or log).
        message: Custom message for user (if any).
    """

    matched: bool
    rule_name: str = ""
    action: str = ""
    message: str = ""


NO_RULE_MATCH = CustomRuleMatch(matched=False)

# ── Compiled rule cache ─────────────────────────────────────────────────

_compiled_cache: dict[str, re.Pattern[str]] = {}


def _get_compiled(pattern: str) -> re.Pattern[str] | None:
    """Compile and cache a regex pattern. Returns None on invalid regex."""
    if pattern in _compiled_cache:
        return _compiled_cache[pattern]
    try:
        compiled = re.compile(pattern, re.IGNORECASE)
        _compiled_cache[pattern] = compiled
        return compiled
    except re.error:
        logger.warning(
            "custom_rule_invalid_regex",
            pattern=pattern,
        )
        return None


def check_custom_rules(
    text: str,
    safety: SafetyConfig,
) -> CustomRuleMatch:
    """Check text against owner-defined custom rules.

    Rules are evaluated in order. First match wins.
    Invalid regex patterns are silently skipped (logged as warning).

    Args:
        text: Text to check.
        safety: Safety config with custom_rules.

    Returns:
        CustomRuleMatch with match details or NO_RULE_MATCH.
    """
    if not safety.custom_rules:
        return NO_RULE_MATCH

    m = get_metrics()
    lower = text.lower()

    for rule in safety.custom_rules:
        compiled = _get_compiled(rule.pattern)
        if compiled is None:
            continue

        if compiled.search(lower):
            logger.info(
                "custom_rule_matched",
                rule_name=rule.name,
                action=rule.action,
            )
            m.safety_blocks.add(1, {"reason": f"custom_rule:{rule.name}"})

            return CustomRuleMatch(
                matched=True,
                rule_name=rule.name,
                action=rule.action,
                message=rule.message,
            )

    return NO_RULE_MATCH


def check_banned_topics(
    text: str,
    safety: SafetyConfig,
) -> CustomRuleMatch:
    """Check text against banned topic keywords.

    Simple keyword matching (case-insensitive word boundary).
    For semantic matching, use LLM classifier (future enhancement).

    Args:
        text: Text to check.
        safety: Safety config with banned_topics.

    Returns:
        CustomRuleMatch if a banned topic is detected.
    """
    if not safety.banned_topics:
        return NO_RULE_MATCH

    lower = text.lower()

    for topic in safety.banned_topics:
        topic_lower = topic.lower()
        # Word boundary match to avoid partial matches
        pattern = rf"\b{re.escape(topic_lower)}\b"
        if re.search(pattern, lower):
            logger.info(
                "banned_topic_matched",
                topic=topic,
            )
            m = get_metrics()
            m.safety_blocks.add(1, {"reason": f"banned_topic:{topic}"})

            return CustomRuleMatch(
                matched=True,
                rule_name=f"banned_topic:{topic}",
                action="block",
                message=f"I'm not able to discuss {topic}.",  # Default; i18n via safety_i18n
            )

    return NO_RULE_MATCH


def clear_compiled_cache() -> None:
    """Clear the compiled regex cache (for testing)."""
    _compiled_cache.clear()
