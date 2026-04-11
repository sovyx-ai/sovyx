"""Sovyx LLM Safety Classifier — language-agnostic content safety via LLM.

Uses a micro-prompt (~80 tokens) to classify text as safe or unsafe with
category attribution. Designed as a universal fallback for regex patterns
that only cover EN/PT.

Architecture:
    Input → [Regex fast-path] → [LLM Classifier] → SafetyVerdict

    Regex catches obvious EN/PT violations in <1ms.
    LLM catches everything else in any language (~200-400ms).
    If LLM is unavailable, regex result is final (graceful degradation).

The classifier uses:
- Temperature 0 for deterministic output
- Cheapest available model (gpt-4o-mini / gemini-flash / haiku)
- max_tokens=20 (response is just "SAFE" or "UNSAFE|category")
- Timeout 2s with circuit breaker

Cost: ~$0.0001 per classification at gpt-4o-mini rates.
At 10k messages/day = ~$1/day.
"""

from __future__ import annotations

import asyncio
import enum
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

from sovyx.observability.logging import get_logger
from sovyx.observability.metrics import get_metrics

if TYPE_CHECKING:
    from sovyx.llm.router import LLMRouter

logger = get_logger(__name__)


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


class SafetyCategory(enum.Enum):
    """Safety violation categories aligned with PatternCategory."""

    VIOLENCE = "violence"
    WEAPONS = "weapons"
    SELF_HARM = "self_harm"
    HACKING = "hacking"
    SUBSTANCE = "substance"
    SEXUAL = "sexual"
    GAMBLING = "gambling"
    HATE_SPEECH = "hate_speech"
    MANIPULATION = "manipulation"
    ILLEGAL = "illegal"
    INJECTION = "injection"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class SafetyVerdict:
    """Result of LLM safety classification.

    Attributes:
        safe: True if content is safe, False if unsafe.
        category: Violation category (None if safe).
        confidence: Classification confidence (1.0 for LLM, 0.8 for regex).
        method: How the verdict was reached ("llm", "regex", "timeout", "error").
        latency_ms: Classification latency in milliseconds.
    """

    safe: bool
    category: SafetyCategory | None = None
    confidence: float = 1.0
    method: str = "llm"
    latency_ms: int = 0


# Singleton safe verdict
SAFE_VERDICT = SafetyVerdict(safe=True, method="pass")


def _parse_llm_response(raw: str) -> SafetyVerdict:
    """Parse the classifier's raw response into a SafetyVerdict.

    Handles edge cases: extra whitespace, lowercase, unexpected format.
    If the response is unparseable, defaults to SAFE (fail-open for
    usability — the regex fast-path already caught obvious violations).
    """
    cleaned = raw.strip().upper()

    if cleaned == "SAFE":
        return SafetyVerdict(safe=True, method="llm")

    if cleaned.startswith("UNSAFE"):
        parts = cleaned.split("|", maxsplit=1)
        category = SafetyCategory.UNKNOWN
        if len(parts) == 2:
            cat_str = parts[1].strip().lower()
            try:
                category = SafetyCategory(cat_str)
            except ValueError:
                category = SafetyCategory.UNKNOWN
        return SafetyVerdict(
            safe=False,
            category=category,
            method="llm",
        )

    # Unparseable — log and fail-open
    logger.warning(
        "safety_classifier_unparseable",
        raw_response=raw[:100],
    )
    return SafetyVerdict(safe=True, method="llm_unparseable")


def _select_model(llm_router: LLMRouter) -> str | None:
    """Select the cheapest available model for classification.

    Returns None if no preferred model is available (will use router default).
    """
    available_models: set[str] = set()
    for provider in llm_router._providers:
        if provider.is_available:
            for model in llm_router._get_provider_models(provider):
                available_models.add(model)

    for preferred in _PREFERRED_MODELS:
        if preferred in available_models:
            return preferred

    return None  # Let router pick default


async def classify_content(
    text: str,
    llm_router: LLMRouter,
    *,
    timeout: float = _CLASSIFY_TIMEOUT_SEC,
) -> SafetyVerdict:
    """Classify text content for safety using LLM.

    This is the core classification function. It sends a micro-prompt
    to the cheapest available LLM and parses the response.

    Args:
        text: User message or LLM response to classify.
        llm_router: LLM router instance for model access.
        timeout: Maximum seconds to wait for LLM response.

    Returns:
        SafetyVerdict with classification result.
        On timeout/error, returns SAFE with method="timeout"/"error"
        (fail-open — regex fast-path already caught obvious cases).
    """
    start = time.monotonic()
    m = get_metrics()

    model = _select_model(llm_router)

    messages = [
        {"role": "system", "content": _SYSTEM_PROMPT},
        {"role": "user", "content": text[:500]},  # Truncate to limit cost
    ]

    try:
        response = await asyncio.wait_for(
            llm_router.generate(
                messages=messages,
                model=model,
                temperature=0.0,
                max_tokens=20,
                conversation_id="__safety_classifier__",
            ),
            timeout=timeout,
        )

        elapsed_ms = int((time.monotonic() - start) * 1000)

        verdict = _parse_llm_response(response.content)
        # Attach latency
        verdict = SafetyVerdict(
            safe=verdict.safe,
            category=verdict.category,
            confidence=verdict.confidence,
            method=verdict.method,
            latency_ms=elapsed_ms,
        )

        m.safety_llm_classifications.add(
            1,
            {
                "result": "safe" if verdict.safe else "unsafe",
                "method": verdict.method,
                "category": (verdict.category.value if verdict.category else "none"),
            },
        )

        logger.debug(
            "safety_classified",
            safe=verdict.safe,
            category=(verdict.category.value if verdict.category else None),
            method=verdict.method,
            latency_ms=elapsed_ms,
            model=model or "default",
        )

        return verdict

    except TimeoutError:
        elapsed_ms = int((time.monotonic() - start) * 1000)
        logger.warning(
            "safety_classifier_timeout",
            timeout_sec=timeout,
            latency_ms=elapsed_ms,
        )
        m.safety_llm_classifications.add(
            1,
            {
                "result": "timeout",
                "method": "timeout",
                "category": "none",
            },
        )
        return SafetyVerdict(
            safe=True,
            method="timeout",
            latency_ms=elapsed_ms,
        )

    except Exception:  # noqa: BLE001
        elapsed_ms = int((time.monotonic() - start) * 1000)
        logger.warning(
            "safety_classifier_error",
            latency_ms=elapsed_ms,
            exc_info=True,
        )
        m.safety_llm_classifications.add(
            1,
            {
                "result": "error",
                "method": "error",
                "category": "none",
            },
        )
        return SafetyVerdict(
            safe=True,
            method="error",
            latency_ms=elapsed_ms,
        )
