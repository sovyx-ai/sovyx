"""Reusable PII hashing + bounded-cardinality + trace-ID utilities (M1).

Ring 6 (Orchestration) observability foundation. Three small surfaces
that any voice / health / dashboard component can call to keep its
metrics + logs PII-safe and bounded:

* :func:`hash_pii` — stable truncated SHA-256 fingerprint for any
  PII string (device GUID, friendly name, mic vendor, etc.). Caller
  passes a salt so the same raw value produces a different
  fingerprint across two unrelated metric namespaces (preventing
  cross-namespace correlation attacks against the local-first
  rollup).
* :class:`BoundedCardinalityBucket` — observes a sequence of label
  values, preserves the top-N most frequent verbatim, collapses the
  long tail to ``"other"``. Without this, a label like
  ``endpoint_guid`` (one per device on the host) explodes Prometheus
  / OTLP cardinality at ~100 distinct values, eating the collector's
  per-metric budget.
* :func:`mint_utterance_id` — UUID4 generator for per-utterance
  trace propagation (Ring 6 trace contract). Caller stamps the
  result on every event in the utterance pipeline so dashboards can
  reconstruct the full capture → VAD → STT → LLM → TTS span set.

Mission §3.10 M1 identified the cardinality / PII gap; the existing
:mod:`sovyx.voice.health._telemetry` module's ``_bucket_matched_profile``
implements one instance of the pattern (mixer KB profiles). This
module extracts the reusable primitives so future cloud-bound
metrics / log emitters don't reinvent the discipline.

Reference: MISSION-voice-mixer-enterprise-refactor-2026-04-25 §2.6,
§3.10 M1, OpenTelemetry semconv (cardinality budget guidance),
Speechmatics 2026 voice-AI compliance guide.
"""

from __future__ import annotations

import hashlib
import threading
import uuid
from collections import Counter
from dataclasses import dataclass, field

from sovyx.observability.logging import get_logger

logger = get_logger(__name__)


_DEFAULT_PII_HASH_LEN = 12
"""Default truncation length for :func:`hash_pii` output (hex chars).

12 hex = 48 bits of fingerprint. At 2 ** 48 = 281 trillion buckets the
collision probability for a realistic deployment (O(10 000) distinct
values) is negligible (birthday-bound ~1.8 × 10**-9). Truncation matters
because the full SHA-256 (64 hex chars) is unwieldy in metric labels +
dashboards; 12 chars fits in a Grafana column without wrapping.
"""

_DEFAULT_CARDINALITY_OTHER_LABEL = "other"
"""Label assigned to values evicted from a
:class:`BoundedCardinalityBucket`. Stable across releases — dashboards
key on this exact string."""


def hash_pii(
    value: str,
    *,
    salt: str = "",
    length: int = _DEFAULT_PII_HASH_LEN,
) -> str:
    """Return a stable truncated SHA-256 fingerprint of ``value``.

    Pure function. The same ``(value, salt)`` pair always returns the
    same fingerprint within a Sovyx release; a different salt produces
    a different fingerprint, preventing cross-namespace correlation if
    the same raw value (e.g. an endpoint GUID) is hashed for two
    unrelated metric paths.

    The empty-string fingerprint is itself empty (caller can
    distinguish "value is empty" from "value is hashed"). Callers MUST
    not interpret the fingerprint length to determine whether hashing
    happened; check the empty-string case explicitly.

    Args:
        value: Raw PII string. ``None``-equivalent values (empty
            string) return ``""`` so the caller's downstream filter
            ``if not v: ...`` keeps working.
        salt: Optional salt string. Different salts produce different
            fingerprints for the same value — use one salt per
            namespace (e.g. ``"voice.endpoint"`` vs
            ``"voice.mic_name"``) so cross-namespace correlation is
            impossible.
        length: Hex-character truncation length. Default 12 (48 bits)
            balances collision-probability vs dashboard column width.
            Bounded to ``[8, 64]`` — 8 hex = 32 bits is the floor
            below which collisions become realistic (~1% at O(10 000)
            values); 64 hex = the full SHA-256 digest.

    Raises:
        ValueError: ``length`` outside the ``[8, 64]`` range.
    """
    _MIN_HASH_LEN = 8
    _MAX_HASH_LEN = 64
    if not (_MIN_HASH_LEN <= length <= _MAX_HASH_LEN):
        msg = (
            f"length must be in [{_MIN_HASH_LEN}, {_MAX_HASH_LEN}] "
            f"(8 hex = 32-bit floor below which collisions become realistic; "
            f"64 hex = full SHA-256 digest), got {length}"
        )
        raise ValueError(msg)
    if not value:
        return ""
    payload = f"{salt}::{value}".encode("utf-8")
    digest = hashlib.sha256(payload).hexdigest()
    return digest[:length]


@dataclass(slots=True)
class BoundedCardinalityBucket:
    """Top-N + ``"other"`` cardinality bucketer.

    Observes a sequence of label values via :meth:`bucket`. The first
    ``maxsize`` distinct values are preserved verbatim (in
    first-seen order). Subsequent distinct values collapse to
    ``"other"`` so the downstream metric label cardinality stays
    bounded.

    Thread-safe: an internal :class:`threading.Lock` serialises every
    mutation. Per-call cost is O(1) hash lookup + O(1) increment +
    rare O(maxsize) eviction comparison; cheap enough for hot paths
    (per-frame metric labels).

    Args:
        maxsize: Maximum distinct values preserved verbatim. Above
            this count, new values bucket to :data:`other_label`.
            Bounded to ``[1, 100_000]`` — 100 000 is already past
            Prometheus's typical 10 000-label budget per metric;
            higher would defeat the bucketer's purpose.
        other_label: Label assigned to evicted / overflow values.
            Default ``"other"`` matches the canonical
            cardinality-protection convention (Prometheus / OTel
            semantic conventions).

    Attributes:
        maxsize: Configured ceiling.
        other_label: Configured overflow label.
    """

    maxsize: int
    other_label: str = _DEFAULT_CARDINALITY_OTHER_LABEL
    _preserved: dict[str, None] = field(default_factory=dict)
    _other_count: int = 0
    _hits: Counter[str] = field(default_factory=Counter)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    _MIN_MAXSIZE: int = field(default=1, init=False, repr=False)
    _MAX_MAXSIZE: int = field(default=100_000, init=False, repr=False)

    def __post_init__(self) -> None:
        if not (self._MIN_MAXSIZE <= self.maxsize <= self._MAX_MAXSIZE):
            msg = (
                f"maxsize must be in [{self._MIN_MAXSIZE}, {self._MAX_MAXSIZE}] "
                f"(above 100 000 the bucket defeats the cardinality-protection "
                f"purpose), got {self.maxsize}"
            )
            raise ValueError(msg)
        if not self.other_label:
            msg = "other_label must be a non-empty string"
            raise ValueError(msg)

    def bucket(self, value: str) -> str:
        """Return ``value`` if preserved or ``"other"`` if overflowed.

        First ``maxsize`` distinct values are preserved (insertion
        order); subsequent novel values bucket to ``other_label``.
        Hits on already-seen values increment the hit counter (useful
        for top-N reporting via :meth:`top_n`) and return the
        verbatim value.

        Empty string is special: it always passes through verbatim
        without consuming a preserved slot — empty values are
        operationally common (missing field, no device label) and
        should not deplete the bucket's capacity.
        """
        if not value:
            return value
        with self._lock:
            if value in self._preserved:
                self._hits[value] += 1
                return value
            if len(self._preserved) < self.maxsize:
                self._preserved[value] = None
                self._hits[value] += 1
                return value
            self._other_count += 1
            return self.other_label

    def top_n(self, n: int = 10) -> list[tuple[str, int]]:
        """Return the ``n`` most-frequently-seen preserved values.

        Useful for periodic dashboard refresh ("which devices are
        actually being used?"). Excludes the ``other_label`` bucket
        — that's exposed separately via :attr:`other_count`.
        """
        with self._lock:
            return self._hits.most_common(n)

    @property
    def other_count(self) -> int:
        """Total number of values that bucketed to ``other_label``."""
        return self._other_count

    @property
    def preserved_count(self) -> int:
        """Number of distinct values currently preserved verbatim."""
        return len(self._preserved)

    @property
    def is_full(self) -> bool:
        """Whether the bucket has reached :attr:`maxsize`."""
        return len(self._preserved) >= self.maxsize


def mint_utterance_id() -> str:
    """Generate a fresh per-utterance UUID4 trace ID.

    Used by the orchestrator at every utterance-scope boundary
    (wake-word fired → utterance starts → propagate id through
    capture / VAD / STT / LLM / TTS spans → dashboards reconstruct
    the full chain by ``utterance_id`` label).

    UUID4 (~122 bits of entropy) collision probability is negligible
    at any realistic Sovyx deployment scale — the 50 % collision
    probability happens at ~2 ** 61 generated IDs, which exceeds the
    daemon's lifetime capture rate by many orders of magnitude.

    Returns the canonical 36-char string form (``"a8b2-..."``) so
    OpenTelemetry / Prometheus / Grafana ingest it without parsing.
    """
    return str(uuid.uuid4())


__all__ = [
    "BoundedCardinalityBucket",
    "hash_pii",
    "mint_utterance_id",
]
