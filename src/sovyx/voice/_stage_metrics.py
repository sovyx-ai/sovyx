"""Voice pipeline RED + USE metrics facade (Ring 6 — M2).

Per-stage instrumentation contract for the four cognitive voice
stages (capture, vad, stt, tts) plus the output mixer queue.
Built on top of :mod:`sovyx.observability.metrics` (single source of
truth for OpenTelemetry instruments) and :mod:`sovyx.voice._observability_pii`
(M1 cardinality protection + trace-ID minting).

Three call-site primitives — each one a one-liner at the call site,
each one bounded-cardinality by construction, each one safe to
invoke from any thread without coordination:

* :func:`record_stage_event` — RED Rate + Errors counter. One call
  per stage completion regardless of outcome. ``error_type`` is
  funnelled through a process-wide top-N
  :class:`BoundedCardinalityBucket` so a misbehaving error path
  cannot explode the metric series count.
* :func:`measure_stage_duration` — RED Duration histogram. Async
  context manager (works in sync code via the ``contextlib`` shim).
  Records the latency in ms with the resolved outcome label
  (``success`` or ``error``) so dashboards can compare success-path
  vs error-path tail latency directly.
* :func:`record_queue_depth` — USE Utilisation + Saturation. Two
  histograms updated atomically per enqueue. Saturation is computed
  from the depth/capacity ratio; capacity is a *required* argument
  so the saturation series is meaningful (a depth without a capacity
  is an unbounded gauge).

Per-utterance trace IDs (UUID4 from
:func:`sovyx.voice._observability_pii.mint_utterance_id`) are
intentionally NOT a metric label — putting them on a counter would
explode cardinality at one new series per utterance. They belong on
structured-log attributes (``logger.info("...", utterance_id=...)``)
and OpenTelemetry trace spans, where the cardinality cost is paid
once per utterance instead of N times per scrape interval.

Reference: MISSION-voice-mixer-enterprise-refactor-2026-04-25 §2.6
(Ring 6), §3.10 M2; OpenTelemetry semantic conventions general/metrics
(RED + USE pattern); Better Stack RED+USE guide; Brendan Gregg's USE
method.
"""

from __future__ import annotations

import contextlib
import time
from enum import StrEnum
from typing import TYPE_CHECKING

from sovyx.observability.logging import get_logger
from sovyx.observability.metrics import get_metrics
from sovyx.voice._observability_pii import BoundedCardinalityBucket

if TYPE_CHECKING:
    from collections.abc import Generator

logger = get_logger(__name__)


_ERROR_TYPE_BUCKET_MAXSIZE = 32
"""Top-N distinct error_type values preserved verbatim per process.

The closed-set stage label (5 values) × the closed-set kind label
(3 values) × top-32 error_type values + 1 ``"other"`` bucket =
**495 distinct series ceiling** for the rate/errors counter. Well
under the registry's 10 000-series global budget, leaving headroom
for the duration histogram (5 stages × 2 outcomes = 10 series) and
the queue histograms (5 owners each = 10 series).

32 is generous: a healthy pipeline emits at most 5–10 distinct
error classes (timeout, decode, downstream, …); a misbehaving one
that emits arbitrary exception messages still gets its first 32
captured before the rest collapse to ``"other"``.
"""


_QUEUE_SATURATION_MAX_PCT = 100.0
"""Ceiling for queue saturation %. Saturation > 100% is impossible by
construction (depth ≤ capacity is enforced by the bounded queue);
the clamp here is defensive insurance against a buggy producer that
counts depth wrong — a saturation samples that exceeds 100% would
distort the histogram tail without surfacing the underlying counting
bug. We clamp + log instead of recording the bogus sample.
"""


_QUEUE_CAPACITY_MIN = 1
"""Smallest meaningful queue capacity. A capacity of zero is either a
synchronous channel (no queue at all — caller should not record USE
for it) or a misconfigured queue (loud-fail). Either way, the metric
is meaningless; reject at call time."""


class VoiceStage(StrEnum):
    """Closed set of cognitive voice pipeline stages.

    StrEnum (anti-pattern #9) so value-based comparison is stable
    across pytest-xdist namespace duplication and JSON serialisation
    matches the metric label verbatim.
    """

    CAPTURE = "capture"
    """Audio capture from the OS device through the FrameNormalizer."""

    VAD = "vad"
    """Silero VAD inference (Ring 3)."""

    STT = "stt"
    """Speech-to-text decode (Moonshine / Whisper / cloud) — Ring 4."""

    TTS = "tts"
    """Text-to-speech synthesis (Kokoro / Piper) — Ring 5."""

    OUTPUT = "output"
    """Output mixer + sink playback queue — Ring 5 tail."""


class StageEventKind(StrEnum):
    """Outcome class for a single stage invocation."""

    SUCCESS = "success"
    """Stage completed and produced its expected output."""

    ERROR = "error"
    """Stage raised or returned a structured failure."""

    DROP = "drop"
    """Stage intentionally discarded the input (e.g. silence,
    rejected hallucination, queue saturation drop). Distinct from
    ERROR so dashboards can tell unhealthy stage failure from
    healthy intentional rejection."""


class StageOutcome(StrEnum):
    """Coarse success/error split for the duration histogram.

    Drop is folded into SUCCESS for duration purposes — a drop is
    still a *completed* stage call, just one with empty output. The
    rate/errors counter retains the three-way split so the dashboard
    can subtract drops from total to recover the success rate.
    """

    SUCCESS = "success"
    ERROR = "error"


# Process-wide error_type cardinality protector. Module-level so every
# import sees the same bucket; thread-safe via internal threading.Lock.
_ERROR_TYPE_BUCKET = BoundedCardinalityBucket(
    maxsize=_ERROR_TYPE_BUCKET_MAXSIZE,
    other_label="other",
)


def _bucket_error_type(error_type: str | None) -> str:
    """Fold an error_type into a top-N bounded label.

    Returns ``"none"`` for None / empty so the metric label is always
    present (avoiding OTel's "missing attribute" scrape ambiguity).
    Live error_type strings get truncated to 64 chars before bucketing
    so a pathological exception message (multi-KB stacktrace as a
    single string) doesn't bloat the bucket's preserved set.
    """
    if not error_type:
        return "none"
    truncated = error_type[:64]
    return _ERROR_TYPE_BUCKET.bucket(truncated)


def record_stage_event(
    stage: VoiceStage,
    kind: StageEventKind,
    *,
    error_type: str | None = None,
) -> None:
    """Bump the per-stage RED Rate + Errors counter by one.

    Emit one call per stage *completion* — success, error, or drop.
    The dashboard rate is the sum across all kinds; the error rate
    is ``kind=error / total``; the drop rate is ``kind=drop / total``.

    ``error_type`` is bounded to the top 32 distinct values per
    process plus an ``"other"`` overflow; supply a stable identifier
    (exception class name, structured failure code), not a free-form
    message. Pass ``None`` for SUCCESS / DROP — the metric label
    becomes ``"none"``.

    Args:
        stage: Which voice stage emitted the event.
        kind: SUCCESS / ERROR / DROP — see :class:`StageEventKind`.
        error_type: Optional stable error identifier. Folded through
            the cardinality bucket so misbehaving call sites cannot
            explode the metric.
    """
    metrics_registry = get_metrics()
    attrs: dict[str, str] = {
        "stage": stage.value,
        "kind": kind.value,
        "error_type": _bucket_error_type(error_type),
    }
    metrics_registry.voice_stage_events.add(1, attrs)


@contextlib.contextmanager
def measure_stage_duration(
    stage: VoiceStage,
) -> Generator[_StageDurationToken, None, None]:
    """Measure wall-clock duration of a stage invocation.

    Usage::

        with measure_stage_duration(VoiceStage.STT) as token:
            try:
                result = await stt.transcribe(audio)
            except Exception:
                token.mark_error()
                raise

    The histogram is recorded on context exit with the resolved
    outcome label (``success`` if :meth:`_StageDurationToken.mark_error`
    was never called, else ``error``). Recording happens regardless
    of whether the body raised — if the body raised AND
    :meth:`mark_error` wasn't called, the recorded outcome is still
    ``error`` because the exception itself is the failure signal.

    Yields:
        :class:`_StageDurationToken` — call ``token.mark_error()`` to
        flip the outcome label, or do nothing for the success path.
    """
    metrics_registry = get_metrics()
    token = _StageDurationToken()
    start_monotonic = time.monotonic()
    try:
        yield token
    except BaseException:
        token.mark_error()
        raise
    finally:
        elapsed_ms = (time.monotonic() - start_monotonic) * 1000.0
        outcome = StageOutcome.ERROR if token._is_error else StageOutcome.SUCCESS
        attrs: dict[str, str] = {
            "stage": stage.value,
            "outcome": outcome.value,
        }
        metrics_registry.voice_stage_duration.record(elapsed_ms, attrs)


class _StageDurationToken:
    """Mutable error flag passed to :func:`measure_stage_duration` callers.

    Exposed only as the yielded value of the context manager — never
    instantiated directly by call sites. The leading underscore signals
    "internal, accessed via the public CM".
    """

    __slots__ = ("_is_error",)

    def __init__(self) -> None:
        self._is_error = False

    def mark_error(self) -> None:
        """Flip the outcome label to ``error`` for this stage call.

        Idempotent — calling twice has no effect. The exception path
        in :func:`measure_stage_duration` calls this automatically, so
        callers only need to invoke it explicitly for *handled*
        failures (e.g. timeout that returns a sentinel instead of
        raising).
        """
        self._is_error = True


def record_queue_depth(
    owner: VoiceStage,
    depth: int,
    capacity: int,
) -> None:
    """Record USE Utilisation + Saturation for an inter-stage queue.

    Two histograms updated together per producer enqueue:

    * ``voice.queue.depth`` — raw item count (utilisation in absolute
      terms — useful for spotting a queue that's persistently
      half-full vs persistently empty even at the same saturation %).
    * ``voice.queue.saturation_pct`` — percentage of capacity in use,
      0–100. This is the canonical USE saturation signal — sustained
      p95 > 80% indicates an under-provisioned consumer.

    Args:
        owner: Which stage owns the queue (the producer side, by
            convention — e.g. capture's output queue is
            ``VoiceStage.CAPTURE``).
        depth: Current item count in the queue at sample time.
            Negative values indicate a counting bug — clamped to 0
            and logged.
        capacity: Configured maximum capacity. Must be ``>= 1`` —
            zero / negative capacities are loud-fail at call time
            because the saturation calculation is meaningless.

    Raises:
        ValueError: ``capacity < 1`` (loud-fail per anti-pattern #11).
    """
    if capacity < _QUEUE_CAPACITY_MIN:
        msg = (
            f"capacity must be >= {_QUEUE_CAPACITY_MIN} "
            f"(zero / negative capacity makes saturation_pct "
            f"meaningless), got {capacity}"
        )
        raise ValueError(msg)

    if depth < 0:
        logger.warning(
            "voice.queue.depth_negative",
            owner=owner.value,
            depth=depth,
            capacity=capacity,
            action_required="audit producer depth-counting code",
        )
        depth = 0

    raw_saturation = (depth / capacity) * 100.0
    if raw_saturation > _QUEUE_SATURATION_MAX_PCT:
        logger.warning(
            "voice.queue.saturation_overflow",
            owner=owner.value,
            depth=depth,
            capacity=capacity,
            raw_saturation=raw_saturation,
            action_required="audit producer depth-counting code",
        )
    saturation_pct = min(raw_saturation, _QUEUE_SATURATION_MAX_PCT)

    metrics_registry = get_metrics()
    attrs: dict[str, str] = {"owner": owner.value}
    metrics_registry.voice_queue_depth.record(float(depth), attrs)
    metrics_registry.voice_queue_saturation_pct.record(saturation_pct, attrs)


def reset_error_type_bucket_for_tests() -> None:
    """Reset the process-wide error_type cardinality bucket.

    Test-only helper — production code never calls this. The bucket
    is module-level so its state persists across tests; without a
    reset hook, a test that deliberately overflows the bucket would
    contaminate every subsequent test that asserts on bucket state.

    Call from a pytest fixture (autouse + reset between tests) or
    explicitly inside the test that needs a fresh bucket.
    """
    global _ERROR_TYPE_BUCKET
    _ERROR_TYPE_BUCKET = BoundedCardinalityBucket(
        maxsize=_ERROR_TYPE_BUCKET_MAXSIZE,
        other_label="other",
    )


__all__ = [
    "StageEventKind",
    "StageOutcome",
    "VoiceStage",
    "measure_stage_duration",
    "record_queue_depth",
    "record_stage_event",
    "reset_error_type_bucket_for_tests",
]
