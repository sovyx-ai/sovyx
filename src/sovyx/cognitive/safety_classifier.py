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
import enum
import hashlib
import time
from collections import OrderedDict
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


# ── Classification Cache ────────────────────────────────────────────────
# LRU cache with TTL. Key = hash of first 200 chars (deterministic for
# same input). Bounded to prevent memory growth.

_CACHE_TTL_SEC = 300.0  # 5 minutes
_CACHE_MAX_SIZE = 1024


@dataclass(slots=True)
class _CacheEntry:
    """Cached classification result with expiry."""

    verdict: SafetyVerdict
    expires_at: float


class ClassificationCache:
    """Thread-safe LRU cache for safety classifications.

    Key: SHA-256 of first 200 chars of input text.
    Value: SafetyVerdict with TTL.
    Max size: bounded with LRU eviction.

    GIL-protected (single-threaded asyncio) — no explicit locking needed.
    """

    def __init__(
        self,
        *,
        max_size: int = _CACHE_MAX_SIZE,
        ttl_sec: float = _CACHE_TTL_SEC,
    ) -> None:
        self._cache: OrderedDict[str, _CacheEntry] = OrderedDict()
        self._max_size = max_size
        self._ttl_sec = ttl_sec
        self._hits = 0
        self._misses = 0

    @staticmethod
    def _key(text: str) -> str:
        """Generate cache key from text prefix."""
        return hashlib.sha256(text[:200].encode()).hexdigest()[:16]

    def get(self, text: str) -> SafetyVerdict | None:
        """Look up cached verdict. Returns None on miss or expired."""
        key = self._key(text)
        entry = self._cache.get(key)
        if entry is None:
            self._misses += 1
            return None
        if time.monotonic() > entry.expires_at:
            # Expired — remove and miss
            del self._cache[key]
            self._misses += 1
            return None
        # Hit — move to end (most recently used)
        self._cache.move_to_end(key)
        self._hits += 1
        return entry.verdict

    def put(self, text: str, verdict: SafetyVerdict) -> None:
        """Store verdict in cache. Evicts LRU if at capacity."""
        key = self._key(text)
        self._cache[key] = _CacheEntry(
            verdict=verdict,
            expires_at=time.monotonic() + self._ttl_sec,
        )
        self._cache.move_to_end(key)
        # Evict oldest if over capacity
        while len(self._cache) > self._max_size:
            self._cache.popitem(last=False)

    @property
    def hit_rate(self) -> float:
        """Cache hit rate (0.0-1.0). Returns 0.0 if no lookups."""
        total = self._hits + self._misses
        if total == 0:
            return 0.0
        return self._hits / total

    @property
    def size(self) -> int:
        """Current number of cached entries."""
        return len(self._cache)

    def clear(self) -> None:
        """Clear all cached entries (for testing)."""
        self._cache.clear()
        self._hits = 0
        self._misses = 0


# Module-level singleton cache
_classification_cache = ClassificationCache()


def get_classification_cache() -> ClassificationCache:
    """Get the global classification cache instance."""
    return _classification_cache


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
        for text, verdict in zip(unique_texts, verdicts):
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


# ── Cache Statistics ────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class CacheStats:
    """Snapshot of cache statistics.

    Attributes:
        size: Current number of entries.
        max_size: Maximum capacity.
        hit_rate: Hit rate (0.0-1.0).
        hits: Total cache hits.
        misses: Total cache misses.
        ttl_sec: TTL for entries in seconds.
    """

    size: int
    max_size: int
    hit_rate: float
    hits: int
    misses: int
    ttl_sec: float


def get_cache_stats() -> CacheStats:
    """Get a snapshot of the classification cache statistics.

    Useful for dashboard display and monitoring.
    """
    cache = get_classification_cache()
    return CacheStats(
        size=cache.size,
        max_size=cache._max_size,
        hit_rate=cache.hit_rate,
        hits=cache._hits,
        misses=cache._misses,
        ttl_sec=cache._ttl_sec,
    )
