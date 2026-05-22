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
        "anomaly.http_error_rate_spike",
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
        "_error_floor",
        "_error_window_current",
        "_error_window_previous",
        "_error_window_s",
        "_http_error_cooldown_s",
        "_http_error_count_threshold",
        "_http_error_enabled",
        "_http_error_path_cap",
        "_http_error_per_path",
        "_http_error_window_s",
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
        # Mission B.3.P1 (B-P1-01 + B-P1-13) — operator-tunable floor on
        # the previous-window baseline. Default 2 preserves the legacy
        # ``previous < 2`` behavior (suppresses 0→1 noise); floor=0
        # detects the FIRST burst from a quiet system.
        self._error_floor = tuning.anomaly_error_rate_floor
        self._memory_window_s = tuning.anomaly_memory_growth_window_s
        self._memory_growth_pct = tuning.anomaly_memory_growth_pct
        self._cooldown_s = tuning.anomaly_cooldown_s

        # Mission C2 §T2.5 — path-keyed HTTP 5xx rate spike state.
        self._http_error_enabled = tuning.http_error_rate_spike_enabled
        self._http_error_count_threshold = tuning.http_error_rate_spike_count
        self._http_error_window_s = tuning.http_error_rate_spike_window_s
        self._http_error_cooldown_s = tuning.http_error_rate_spike_cooldown_s
        self._http_error_path_cap = tuning.http_error_rate_spike_path_cap

        self._seen_events: set[str] = set()
        self._latency_per_event: dict[str, StreamingPercentile] = {}
        # Mission B.3.P1 (B-P1-02) — dual-window split.
        #
        # Pre-mission used a SINGLE deque sized at 4× the window. Under a
        # sustained storm exceeding ~4 errors/sec the deque saturated, the
        # OLDEST baseline samples evicted, ``previous`` dropped toward
        # zero, the ratio test never fired, and the detector went silent
        # mid-storm. (Confidence-5 finding per Mission B audit §B1-F-002.)
        #
        # Post-mission: two independent deques, one per window.
        # ``_error_window_current`` holds [now-window_s, now] timestamps
        # for the active rate measurement; ``_error_window_previous``
        # holds [now-2*window_s, now-window_s] for the baseline. Each
        # bounded by ``max(1000, window_s * 100)`` so the explicit
        # ``popleft()`` aging path in ``_observe_error`` ALWAYS runs
        # before the deque saturates under any sane error storm
        # (~100/s peak × any operator-configured window_s ≤ 3600s).
        # The pre-mission single-deque path used ``max(100, window_s * 4)``
        # which silently evicted promotion candidates at sustained 10/s
        # on the default 60s window (the B-P1-02 root cause).
        # ``maxlen`` is the SAFETY NET; the explicit aging is the
        # PRIMARY mechanism — preserves window-boundary semantics
        # independent of arrival rate.
        _deque_cap = max(1000, self._error_window_s * 100)
        self._error_window_current: deque[float] = deque(maxlen=_deque_cap)
        self._error_window_previous: deque[float] = deque(maxlen=_deque_cap)
        # (timestamp_s, rss_bytes) snapshots for memory growth detection.
        self._rss_history: deque[tuple[float, int]] = deque(
            maxlen=max(20, self._memory_window_s // 5)
        )
        # Path-keyed 5xx timestamps. Bounded by ``_http_error_path_cap``
        # via LRU eviction at insert time (anti-pattern #15: never
        # ``defaultdict(deque)`` unbounded). Each deque sized at 2×
        # threshold so the "exactly threshold within window" predicate
        # never loses a hit to deque eviction within the window.
        self._http_error_per_path: dict[str, deque[float]] = {}
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
        #    carry the canonical ``process.rss_bytes`` field. Mission H4
        #    §T2.2 renamed from the legacy ``system.rss_bytes``; dual-read
        #    during the LENIENT calibration window (v0.49.15..v0.53.x) so
        #    external dashboards / log forwarders keyed on the legacy name
        #    keep working. At STRICT (v0.54.0) the legacy fallback is
        #    removed. Pre-H4 the consumer read ``system.rss_bytes`` while
        #    the producer at ``observability/resources.py:149`` emitted
        #    ``process.rss_bytes`` — the detector had been silently dead
        #    since landing. v0.43.1 forensic anchor §H4: +1.1 GB RSS over
        #    60 s never fired ``anomaly.memory_growth_spike`` because of
        #    this exact drift.
        rss = event_dict.get("process.rss_bytes")
        if rss is None:
            rss = event_dict.get("system.rss_bytes")  # h4-allowlist: legacy alias during LENIENT
        if isinstance(rss, int) and rss > 0:
            self._observe_rss(rss, now)

        # 5. Mission C2 §T2.5 — path-keyed HTTP 5xx spike. Triggered
        #    by ``HttpTelemetryMiddleware``'s ``net.http.response``
        #    structured emit (logs 5xx at WARNING level, which the
        #    global error_rate_spike's ``error|critical`` filter at
        #    step 3 never sees — closing M2 from the v0.43.1
        #    forensic audit). Disjoint from step 3: this bucket is
        #    per-``net.path`` with an absolute-count threshold,
        #    NOT a ratio-against-baseline.
        if self._http_error_enabled and event_name == "net.http.response":
            status_code = event_dict.get("net.status_code")
            if isinstance(status_code, int) and status_code >= 500:  # noqa: PLR2004
                path = event_dict.get("net.path")
                if isinstance(path, str) and path:
                    self._observe_http_error(path, status_code, now)

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
        # Mission B.3.P1 (B-P1-01 + B-P1-02 + B-P1-13) — dual-window
        # accounting + operator-tunable floor.
        #
        # Aging: every observation first promotes timestamps that have
        # aged past ``window_s`` (now stale-for-current, fresh-for-previous)
        # OR past ``2*window_s`` (stale-for-both, drop). The ``current``
        # deque appends every fresh observation; the ``previous`` deque
        # receives promoted entries and gets pruned of doubly-stale ones.
        # This preserves the LEFT edge of the previous-window even under
        # sustained 10/s+ storms (the saturation regression that
        # silenced the pre-mission single-deque path).
        window_start = now - self._error_window_s
        baseline_start = now - (self._error_window_s * 2)
        with self._lock:
            # Promote current → previous for ts that aged out of current.
            while self._error_window_current and self._error_window_current[0] < window_start:
                self._error_window_previous.append(self._error_window_current.popleft())
            # Drop previous-window entries older than baseline_start.
            while self._error_window_previous and self._error_window_previous[0] < baseline_start:
                self._error_window_previous.popleft()
            # Append fresh observation to the current window.
            self._error_window_current.append(now)
            current = len(self._error_window_current)
            previous = len(self._error_window_previous)

        # Operator-tunable floor (B-P1-01 + B-P1-13). Default 2 preserves
        # the legacy ``previous < 2`` behavior (suppresses 0→1 noise);
        # floor=0 lets the FIRST burst on a quiet system fire via the
        # below ``previous == 0`` branch.
        if previous == 0:
            # Without a baseline we cannot compute a ratio. Two paths:
            # (a) floor > 0 → suppress, same as pre-mission (the legacy
            #     `previous < 2` collapsed to this branch for previous=0);
            # (b) floor == 0 → operator opted in to detecting the FIRST
            #     burst on a previously-quiet system. Emit when the
            #     current window has at least ``ceil(factor)`` events
            #     so the rate is at least the configured threshold.
            if self._error_floor > 0:
                return
            if current < max(1, int(self._error_factor)):
                return
            # Synthesise a baseline of 1 so the emit's ``factor`` field
            # is well-defined (current / 1 = current). Downstream
            # operators reading ``anomaly.baseline_count == 0`` know
            # this fired via the floor-0 quiet-start path.
            self._emit(
                "anomaly.error_rate_spike",
                "_global",
                now,
                level="warning",
                fields={
                    "anomaly.window_s": self._error_window_s,
                    "anomaly.current_count": current,
                    "anomaly.baseline_count": 0,
                    "anomaly.factor": float(current),
                    "anomaly.floor": self._error_floor,
                },
            )
            return
        if previous < self._error_floor:
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
                # Mission B.3.P1 — expose effective floor for operator
                # post-mortems; helps distinguish "no fire because floor
                # blocked" from "no fire because ratio insufficient".
                "anomaly.floor": self._error_floor,
            },
        )

    def _observe_rss(self, rss_bytes: int, now: float) -> None:
        with self._lock:
            self._rss_history.append((now, rss_bytes))
            window_start = now - self._memory_window_s
            historical = [(ts, val) for ts, val in self._rss_history if ts >= window_start]
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
        cooldown_s_override: int | None = None,
    ) -> None:
        """Emit *anomaly_event* respecting per-key cooldown.

        ``cooldown_s_override`` lets Mission C2's path-keyed
        ``http_error_rate_spike`` honor its own
        ``http_error_rate_spike_cooldown_s`` (300 s default) instead
        of the global ``anomaly_cooldown_s`` (60 s default) — a
        sustained outage should produce one event per 5 min per
        path, not one per minute.
        """
        # The cooldown key combines the anomaly type with its scope so
        # ``latency_spike`` for two different events doesn't collide,
        # while ``error_rate_spike`` (always ``_global``) still rate-limits.
        key = f"{anomaly_event}::{cooldown_key}"
        cooldown = self._cooldown_s if cooldown_s_override is None else cooldown_s_override
        with self._lock:
            last = self._last_anomaly_ts.get(key)
            if last is not None and (now - last) < cooldown:
                return
            self._last_anomaly_ts[key] = now

        emit = logger.info if level == "info" else logger.warning
        emit(anomaly_event, **fields)

    def _observe_http_error(self, path: str, status_code: int, now: float) -> None:
        """Record a 5xx event for *path*; emit on threshold.

        Mission C2 §T2.5. Path-keyed deques bounded by
        ``_http_error_path_cap``; threshold-triggered (not
        ratio-baselined). Cooldown is per (path, anomaly_type)
        so concurrent storms on multiple endpoints surface
        independently.
        """
        with self._lock:
            bucket = self._http_error_per_path.get(path)
            if bucket is None:
                if len(self._http_error_per_path) >= self._http_error_path_cap:
                    # Anti-pattern #15: evict the LEAST-recently-added
                    # path (insertion-order via dict's stable iteration)
                    # to keep cardinality bounded without an external
                    # LRU dep. Fairness sacrifices recency precision for
                    # cardinality safety — acceptable for an alerting
                    # signal that already cooldown-throttles.
                    oldest = next(iter(self._http_error_per_path))
                    del self._http_error_per_path[oldest]
                # Sized at 2× threshold + one safety slot so the
                # in-window count below never under-reports due to
                # deque eviction during a steady storm.
                bucket = deque(maxlen=max(8, self._http_error_count_threshold * 2 + 1))
                self._http_error_per_path[path] = bucket
            bucket.append(now)
            window_start = now - self._http_error_window_s
            # Count entries that fall within the current window.
            # Anti-pattern #24: use ``>=`` (inclusive) so a tick-aligned
            # boundary timestamp counts on coarse Windows clocks.
            current = sum(1 for ts in bucket if ts >= window_start)

        if current < self._http_error_count_threshold:
            return
        self._emit(
            "anomaly.http_error_rate_spike",
            path,
            now,
            level="warning",
            fields={
                "anomaly.path": path,
                "anomaly.status_code_sample": status_code,
                "anomaly.window_s": self._http_error_window_s,
                "anomaly.count": current,
                "anomaly.threshold": self._http_error_count_threshold,
            },
            cooldown_s_override=self._http_error_cooldown_s,
        )


__all__ = [
    "AnomalyDetector",
    "StreamingPercentile",
]
