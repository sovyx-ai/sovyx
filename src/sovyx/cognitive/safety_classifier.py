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

Batch mode:
    batch_classify_content() classifies multiple texts concurrently with:
    - Deduplication: identical texts → single LLM call
    - Concurrency limiter: max 5 parallel LLM calls (configurable)
    - Cache-first: cached results skip LLM entirely
    - Fail-open per item: one failure doesn't block others

Cost: ~$0.0001 per classification at gpt-4o-mini rates.
At 10k messages/day = ~$1/day.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

from sovyx.observability.logging import get_logger
from sovyx.observability.metrics import get_metrics

if TYPE_CHECKING:
    from sovyx.llm.router import LLMRouter

logger = get_logger(__name__)

# ── Re-exports for backward compat ──
from sovyx.cognitive.safety._classifier_budget import (  # noqa: E402, F401
    _CLASSIFY_TIMEOUT_SEC,
    _PREFERRED_MODELS,
    _SYSTEM_PROMPT,
    ClassificationBudget,
    get_classification_budget,
)
from sovyx.cognitive.safety._classifier_cache import (  # noqa: E402, F401
    CacheStats,
    ClassificationCache,
    _CacheEntry,
    get_cache_stats,
    get_classification_cache,
)
from sovyx.cognitive.safety._classifier_types import (  # noqa: E402, F401
    SAFE_VERDICT,
    SafetyCategory,
    SafetyVerdict,
)

__all__ = [
    "BatchClassificationResult",
    "CacheStats",
    "ClassificationBudget",
    "ClassificationCache",
    "SAFE_VERDICT",
    "SafetyCategory",
    "SafetyVerdict",
    "_CLASSIFY_TIMEOUT_SEC",
    "_CacheEntry",
    "_PREFERRED_MODELS",
    "_SYSTEM_PROMPT",
    "_parse_llm_response",
    "_select_model",
    "batch_classify_content",
    "classify_content",
    "get_cache_stats",
    "get_classification_budget",
    "get_classification_cache",
]


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

    # ── Budget check ──
    budget = get_classification_budget()
    if not budget.can_classify():
        logger.debug("safety_classify_budget_exceeded")
        return SAFE_VERDICT  # Fall back to regex-only

    # ── Cache lookup ──
    cache = get_classification_cache()
    cached = cache.get(text)
    if cached is not None:
        elapsed_ms = int((time.monotonic() - start) * 1000)
        m.safety_llm_classifications.add(
            1,
            {
                "result": "safe" if cached.safe else "unsafe",
                "method": "cache",
                "category": (cached.category.value if cached.category else "none"),
            },
        )
        logger.debug(
            "safety_classified_cached",
            safe=cached.safe,
            latency_ms=elapsed_ms,
        )
        return SafetyVerdict(
            safe=cached.safe,
            category=cached.category,
            confidence=cached.confidence,
            method="cache",
            latency_ms=elapsed_ms,
        )

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

        # ── Cache store ���─
        cache.put(text, verdict)
        budget.record_call()

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


# ── Batch Classification ───────────────────────────────────────────────
# Classifies multiple texts concurrently with deduplication and
# bounded parallelism. Ideal for pre-screening tool outputs or
# multi-turn conversation history.

# Default max concurrent LLM calls during batch classification
_BATCH_MAX_CONCURRENT = 5


@dataclass(frozen=True, slots=True)
class BatchClassificationResult:
    """Result of batch classification.

    Attributes:
        verdicts: List of SafetyVerdict in same order as input texts.
        total_ms: Total wall-clock time for the batch in milliseconds.
        cache_hits: Number of results served from cache.
        llm_calls: Number of actual LLM calls made (after dedup + cache).
    """

    verdicts: list[SafetyVerdict]
    total_ms: int
    cache_hits: int
    llm_calls: int


async def batch_classify_content(
    texts: list[str],
    llm_router: LLMRouter,
    *,
    timeout: float = _CLASSIFY_TIMEOUT_SEC,
    max_concurrent: int = _BATCH_MAX_CONCURRENT,
) -> BatchClassificationResult:
    """Classify multiple texts concurrently with deduplication and caching.

    Strategy:
    1. Check cache for all texts → serve hits immediately.
    2. Deduplicate remaining texts (same text → single LLM call).
    3. Classify unique uncached texts concurrently (bounded semaphore).
    4. Reassemble results in original order.

    Args:
        texts: List of texts to classify.
        llm_router: LLM router for model access.
        timeout: Per-item timeout in seconds.
        max_concurrent: Maximum concurrent LLM calls.

    Returns:
        BatchClassificationResult with verdicts in same order as input.
    """
    if not texts:
        return BatchClassificationResult(
            verdicts=[],
            total_ms=0,
            cache_hits=0,
            llm_calls=0,
        )

    start = time.monotonic()
    cache = get_classification_cache()
    m = get_metrics()

    # Phase 1: Cache lookup for all texts
    results: list[SafetyVerdict | None] = [None] * len(texts)
    uncached_indices: dict[str, list[int]] = {}  # text → [indices]
    cache_hits = 0

    for i, text in enumerate(texts):
        cached = cache.get(text)
        if cached is not None:
            results[i] = SafetyVerdict(
                safe=cached.safe,
                category=cached.category,
                confidence=cached.confidence,
                method="cache",
                latency_ms=0,
            )
            cache_hits += 1
        else:
            # Group by text for deduplication
            if text not in uncached_indices:
                uncached_indices[text] = []
            uncached_indices[text].append(i)

    # Phase 2: Classify unique uncached texts concurrently
    semaphore = asyncio.Semaphore(max_concurrent)
    llm_calls = 0

    async def _classify_one(text: str) -> SafetyVerdict:
        async with semaphore:
            return await classify_content(text, llm_router, timeout=timeout)

    if uncached_indices:
        unique_texts = list(uncached_indices.keys())
        tasks = [_classify_one(t) for t in unique_texts]
        verdicts = await asyncio.gather(*tasks, return_exceptions=True)

        llm_calls = len(unique_texts)

        # Phase 3: Reassemble — map verdicts back to original indices
        for text, verdict in zip(unique_texts, verdicts, strict=True):
            if isinstance(verdict, BaseException):
                # Shouldn't happen (classify_content catches all), but be safe
                logger.warning(
                    "batch_classify_unexpected_error",
                    text_prefix=text[:50],
                    error=str(verdict),
                )
                safe_fallback = SafetyVerdict(
                    safe=True,
                    method="error",
                    latency_ms=0,
                )
                for idx in uncached_indices[text]:
                    results[idx] = safe_fallback
            else:
                for idx in uncached_indices[text]:
                    results[idx] = verdict

    elapsed_ms = int((time.monotonic() - start) * 1000)

    # Record batch metrics
    m.safety_llm_classifications.add(
        len(texts),
        {
            "result": "batch",
            "method": "batch",
            "category": "none",
        },
    )

    logger.info(
        "batch_classify_complete",
        total_items=len(texts),
        cache_hits=cache_hits,
        llm_calls=llm_calls,
        unique_texts=len(uncached_indices),
        total_ms=elapsed_ms,
    )

    # Type assertion: all results should be populated
    final_verdicts: list[SafetyVerdict] = []
    for v in results:
        assert v is not None, "Bug: result slot was not populated"
        final_verdicts.append(v)

    return BatchClassificationResult(
        verdicts=final_verdicts,
        total_ms=elapsed_ms,
        cache_hits=cache_hits,
        llm_calls=llm_calls,
    )
