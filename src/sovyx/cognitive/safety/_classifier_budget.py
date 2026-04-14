"""Classification budget + micro-prompt — extracted from safety_classifier.py."""

from __future__ import annotations

import time

# ── Micro-prompt ────────────────────────────────────────────────────────
# Engineered for minimal tokens, maximum accuracy, zero ambiguity.
# The prompt MUST NOT be modified without adversarial testing (TASK-387).

_SYSTEM_PROMPT = (
    "You are a content safety classifier. "
    "Classify the user message as SAFE or UNSAFE.\n"
    "If UNSAFE, append |category from: "
    "violence, weapons, self_harm, hacking, substance, sexual, "
    "gambling, hate_speech, manipulation, illegal, injection.\n"
    "Reply with ONLY one of:\n"
    "SAFE\n"
    "UNSAFE|category\n"
    "No explanation. No other text."
)

# Preferred models for classification (cheapest first)
_PREFERRED_MODELS: tuple[str, ...] = (
    "gpt-4o-mini",
    "gpt-4o-mini-2024-07-18",
    "gemini-2.0-flash",
    "claude-3-5-haiku-20241022",
)

# Timeout for LLM call
_CLASSIFY_TIMEOUT_SEC = 2.0

# ── Cost Control ────────────────────────────────────────────────────────
# Budget cap: max LLM classifications per hour (0 = unlimited)
_HOURLY_BUDGET_CAP = 0  # Default: unlimited

# Estimated cost per classification (gpt-4o-mini ~80 input + 20 output tokens)
_COST_PER_CALL_USD = 0.0001


class ClassificationBudget:
    """Tracks LLM classification spending with hourly budget cap.

    Prevents runaway costs from high traffic or attack floods.
    When budget is exhausted, classifier falls back to regex-only.
    """

    def __init__(self, hourly_cap: int = _HOURLY_BUDGET_CAP) -> None:
        self._hourly_cap = hourly_cap
        self._calls_this_hour = 0
        self._total_calls = 0
        self._hour_start = time.monotonic()
        self._total_cost_usd = 0.0

    def can_classify(self) -> bool:
        """Check if budget allows another classification."""
        self._maybe_reset_hour()
        if self._hourly_cap <= 0:
            return True  # Unlimited
        return self._calls_this_hour < self._hourly_cap

    def record_call(self) -> None:
        """Record a classification call."""
        self._maybe_reset_hour()
        self._calls_this_hour += 1
        self._total_calls += 1
        self._total_cost_usd += _COST_PER_CALL_USD

    def _maybe_reset_hour(self) -> None:
        """Reset counter if hour has elapsed."""
        now = time.monotonic()
        if now - self._hour_start >= 3600:
            self._calls_this_hour = 0
            self._hour_start = now

    @property
    def calls_this_hour(self) -> int:
        """Classifications in current hour."""
        self._maybe_reset_hour()
        return self._calls_this_hour

    @property
    def total_calls(self) -> int:
        """Total classifications ever."""
        return self._total_calls

    @property
    def estimated_cost_usd(self) -> float:
        """Estimated total cost in USD."""
        return round(self._total_cost_usd, 4)

    @property
    def hourly_cap(self) -> int:
        """Current hourly cap (0 = unlimited)."""
        return self._hourly_cap

    def set_cap(self, cap: int) -> None:
        """Update the hourly budget cap."""
        self._hourly_cap = cap


# ── Budget accessor (delegates to SafetyContainer) ─────────────────────


def get_classification_budget() -> ClassificationBudget:
    """Get the ClassificationBudget from the global container.

    Returns:
        The ClassificationBudget instance managed by SafetyContainer.
    """
    from sovyx.cognitive.safety_container import get_safety_container

    return get_safety_container().classification_budget
