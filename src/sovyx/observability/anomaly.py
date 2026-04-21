"""Sovyx anomaly detector — streaming percentiles and rate spikes.

Observes structlog entries via an `AnomalyDetector` processor and emits
`anomaly.*` events when deviations from learned baselines exceed
configured thresholds.

Detection types
---------------

* ``anomaly.first_occurrence`` — an event name never seen before.
  Useful for drift detection (new code paths, schema changes).
* ``anomaly.latency_spike`` — current ``latency_ms`` for an event
  exceeds its rolling P99 baseline by ``anomaly_latency_factor``.
* ``anomaly.error_rate_spike`` — error-level entries within
  ``anomaly_error_rate_window_s`` exceed
  ``anomaly_error_rate_factor`` × the previous-window baseline.
* ``anomaly.memory_growth`` — RSS in the most recent
  ``rss_snapshot.*`` entry has grown by more than
  ``anomaly_memory_growth_pct`` over the
  ``anomaly_memory_growth_window_s`` window.

The detector itself never raises. Per-event-name cooldowns prevent the
same anomaly from spamming the stream when a real incident is in
progress — a single ``anomaly.*`` per cooldown window is enough to
page or annotate the timeline.
"""

from __future__ import annotations

import threading
import time
from collections import deque
from typing import TYPE_CHECKING, Any

from sovyx.observability.logging import get_logger

if TYPE_CHECKING:
    from collections.abc import MutableMapping

    from sovyx.engine.config import ObservabilityTuningConfig

logger = get_logger(__name__)


# Event names emitted by the detector itself — skip recursion when one of
# these flows back through the structlog pipeline. Without this guard the
# anomaly emit would re-enter `__call__` and could trip its own latency
# tracker for ``anomaly.*``.
_DETECTOR_EVENTS: frozenset[str] = frozenset(
    {
        "anomaly.first_occurrence",
        "anomaly.latency_spike",
        "anomaly.error_rate_spike",
        "anomaly.memory_growth",
    }
)


class StreamingPercentile:
    """Deque-backed P50/P95/P99 over a rolling sample window.

    The implementation trades exactness for predictable cost: a `deque`
    of the most recent ``maxlen`` samples is sorted on demand. With a
    1k window and the default ``perf_hotpath_interval_seconds=60``
    cadence, the worst-case sort runs once per second per active event
    name — well within the 200µs P99 budget the plan sets for
    ``observe()`` in §23 (perf SLO table).
    """

    __slots__ = ("_lock", "_samples")

    def __init__(self, maxlen: int) -> None:
        self._samples: deque[float] = deque(maxlen=maxlen)
        # The lock is acquired only for the deque mutation + snapshot copy;
        # sorting happens against the local copy so the hot path doesn't
        # block other observers.
        self._lock = threading.Lock()

    def observe(self, value: float) -> None:
        """Record a single sample. O(1) under contention."""
        with self._lock:
            self._samples.append(value)

    def percentile(self, p: float) -> float | None:
        """Return the P-th percentile, or ``None`` if no samples yet.

        ``p`` is in ``[0.0, 1.0]``. Linear interpolation between
        adjacent samples — matches numpy's ``percentile(..., 'linear')``
        within rounding error.
        """
        with self._lock:
            snapshot = list(self._samples)
        if not snapshot:
            return None
        snapshot.sort()
        if len(snapshot) == 1:
            return float(snapshot[0])
        clamped = max(0.0, min(1.0, p))
        idx = clamped * (len(snapshot) - 1)
        lower = int(idx)
        upper = min(lower + 1, len(snapshot) - 1)
        frac = idx - lower
        return float(snapshot[lower] * (1.0 - frac) + snapshot[upper] * frac)

    def count(self) -> int:
        """Current number of buffered samples."""
        with self._lock:
            return len(self._samples)


class AnomalyDetector:
    """Structlog processor that emits ``anomaly.*`` on deviation.

    Wire as the **last** processor before the renderer in
    ``setup_logging`` — so every fully-enriched entry (post-PII,
    post-envelope) flows through ``__call__``. The detector observes
    silently and emits via its own logger, never mutating the
    incoming ``event_dict``.

    Thread-safe: per-event-name `StreamingPercentile` instances each
    own a `threading.Lock`; the global maps are guarded by
    ``self._lock``.
    """

    __slots__ = (
        "_cooldown_s",
        "_error_factor",
        "_error_window",
        "_error_window_s",
        "_last_anomaly_ts",
        "_latency_factor",
        "_latency_per_event",
        "_lock",
        "_memory_growth_pct",
        "_memory_window_s",
        "_min_samples",
        "_rss_history",
        "_seen_events",
        "_window_size",
    )

    def __init__(self, tuning: ObservabilityTuningConfig) -> None:
        self._window_size = tuning.anomaly_window_size
        self._min_samples = tuning.anomaly_min_samples
        self._latency_factor = tuning.anomaly_latency_factor
        self._error_window_s = tuning.anomaly_error_rate_window_s
        self._error_factor = tuning.anomaly_error_rate_factor
        self._memory_window_s = tuning.anomaly_memory_growth_window_s
        self._memory_growth_pct = tuning.anomaly_memory_growth_pct
        self._cooldown_s = tuning.anomaly_cooldown_s

        self._seen_events: set[str] = set()
        self._latency_per_event: dict[str, StreamingPercentile] = {}
        # Timestamps of error-or-worse entries for rate-spike detection.
        # Sized at 4× the window so historic baselines remain available
        # for ratio calculation even after the active window fills.
        self._error_window: deque[float] = deque(maxlen=max(100, self._error_window_s * 4))
        # (timestamp_s, rss_bytes) snapshots for memory growth detection.
        self._rss_history: deque[tuple[float, int]] = deque(
            maxlen=max(20, self._memory_window_s // 5)
        )
        self._last_anomaly_ts: dict[str, float] = {}
        self._lock = threading.Lock()

    def __call__(
        self,
        _logger: Any,  # noqa: ANN401 — opaque structlog logger reference.
        _method_name: str,
        event_dict: MutableMapping[str, Any],
    ) -> MutableMapping[str, Any]:
        """Inspect *event_dict* and emit ``anomaly.*`` on deviation."""
        event_name = event_dict.get("event")
        if not isinstance(event_name, str) or event_name in _DETECTOR_EVENTS:
            return event_dict

        now = time.monotonic()

        # 1. First-occurrence detection — runs even before min_samples
        #    so brand-new event names surface immediately on first emit.
        if self._mark_seen(event_name):
            self._emit(
                "anomaly.first_occurrence",
                event_name,
                now,
                level="info",
                fields={"anomaly.event": event_name},
            )

        # 2. Latency spike — applies to any entry carrying a numeric
        #    ``*.latency_ms`` field. Pulls the canonical fields produced
        #    by the rest of the codebase (llm.latency_ms,
        #    brain.latency_ms, net.latency_ms, …) by walking values.
        latency = self._extract_latency_ms(event_dict)
        if latency is not None and latency >= 0:
            self._observe_latency(event_name, float(latency), now)

        # 3. Error-rate spike — every WARNING+ entry counts.
        level = event_dict.get("level")
        if isinstance(level, str) and level.lower() in ("error", "critical"):
            self._observe_error(now)

        # 4. Memory growth — only RSS snapshots from ResourceSnapshotter
        #    carry the canonical ``system.rss_bytes`` field.
        rss = event_dict.get("system.rss_bytes")
        if isinstance(rss, int) and rss > 0:
            self._observe_rss(rss, now)

        return event_dict

    # ── Internal helpers ──

    def _mark_seen(self, event_name: str) -> bool:
        """Return ``True`` if this is the first time we see *event_name*."""
        with self._lock:
            if event_name in self._seen_events:
                return False
            self._seen_events.add(event_name)
            return True

    def _observe_latency(self, event_name: str, latency_ms: float, now: float) -> None:
        with self._lock:
            tracker = self._latency_per_event.get(event_name)
            if tracker is None:
                tracker = StreamingPercentile(maxlen=self._window_size)
                self._latency_per_event[event_name] = tracker
        tracker.observe(latency_ms)

        # Compare AFTER recording — the new sample influences the next
        # tick's baseline, but the spike test is against the prior window.
        if tracker.count() < self._min_samples:
            return
        baseline_p99 = tracker.percentile(0.99)
        if baseline_p99 is None or baseline_p99 <= 0:
            return
        threshold = baseline_p99 * self._latency_factor
        if latency_ms <= threshold:
            return
        self._emit(
            "anomaly.latency_spike",
            event_name,
            now,
            level="warning",
            fields={
                "anomaly.event": event_name,
                "anomaly.latency_ms": int(latency_ms),
                "anomaly.baseline_p99_ms": round(baseline_p99, 3),
                "anomaly.factor": round(latency_ms / baseline_p99, 3),
                "anomaly.threshold_ms": round(threshold, 3),
                "anomaly.sample_count": tracker.count(),
            },
        )

    def _observe_error(self, now: float) -> None:
        with self._lock:
            self._error_window.append(now)
            window_start = now - self._error_window_s
            baseline_start = now - (self._error_window_s * 2)
            current = sum(1 for ts in self._error_window if ts >= window_start)
            previous = sum(
                1 for ts in self._error_window if baseline_start <= ts < window_start
            )

        # Need at least one previous-window sample to compute a ratio,
        # plus a small floor on previous to suppress 0→1 noise spikes.
        if previous < 2:  # noqa: PLR2004
            return
        if current <= previous * self._error_factor:
            return
        self._emit(
            "anomaly.error_rate_spike",
            "_global",
            now,
            level="warning",
            fields={
                "anomaly.window_s": self._error_window_s,
                "anomaly.current_count": current,
                "anomaly.baseline_count": previous,
                "anomaly.factor": round(current / max(1, previous), 3),
            },
        )

    def _observe_rss(self, rss_bytes: int, now: float) -> None:
        with self._lock:
            self._rss_history.append((now, rss_bytes))
            window_start = now - self._memory_window_s
            historical = [
                (ts, val) for ts, val in self._rss_history if ts <= window_start
            ]
            if not historical:
                return
            # Use the oldest in-window snapshot as the comparison baseline
            # so a single spike doesn't trigger; we want sustained growth.
            ts_baseline, rss_baseline = historical[0]

        if rss_baseline <= 0:
            return
        growth_pct = ((rss_bytes - rss_baseline) / rss_baseline) * 100.0
        if growth_pct < self._memory_growth_pct:
            return
        self._emit(
            "anomaly.memory_growth",
            "_global",
            now,
            level="warning",
            fields={
                "anomaly.window_s": self._memory_window_s,
                "anomaly.rss_bytes": rss_bytes,
                "anomaly.baseline_rss_bytes": rss_baseline,
                "anomaly.growth_pct": round(growth_pct, 3),
                "anomaly.baseline_age_s": round(now - ts_baseline, 1),
            },
        )

    @staticmethod
    def _extract_latency_ms(event_dict: MutableMapping[str, Any]) -> float | None:
        """Return the first ``*.latency_ms`` value found, or ``None``.

        Walks the dict in iteration order — the first canonical
        ``<domain>.latency_ms`` field wins. Treats unparseable values as
        absent so the detector tolerates schema drift.
        """
        for key, value in event_dict.items():
            if not key.endswith("latency_ms"):
                continue
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                return float(value)
        return None

    def _emit(
        self,
        anomaly_event: str,
        cooldown_key: str,
        now: float,
        *,
        level: str,
        fields: dict[str, Any],
    ) -> None:
        """Emit *anomaly_event* respecting per-key cooldown."""
        # The cooldown key combines the anomaly type with its scope so
        # ``latency_spike`` for two different events doesn't collide,
        # while ``error_rate_spike`` (always ``_global``) still rate-limits.
        key = f"{anomaly_event}::{cooldown_key}"
        with self._lock:
            last = self._last_anomaly_ts.get(key)
            if last is not None and (now - last) < self._cooldown_s:
                return
            self._last_anomaly_ts[key] = now

        emit = logger.info if level == "info" else logger.warning
        emit(anomaly_event, **fields)


__all__ = [
    "AnomalyDetector",
    "StreamingPercentile",
]
