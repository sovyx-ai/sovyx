"""Process-local cache of recent device probe + open verdicts.

Mission anchor: ``docs-internal/missions/MISSION-c3-failover-ladder-iteration-2026-05-16.md``
§T2.1.

Populated by:

1. Boot cascade — ``voice/health/cascade/_executor_helpers.py``'s
   ``_log_probe_result`` is wired (in §T2.4) to also call
   :meth:`ProbeResultCache.record_probe` for every probe outcome.
2. Runtime failover — ``_runtime_failover._try_runtime_failover``
   loop body records every dispatch verdict + error code via
   :meth:`record_probe`, and invalidates dead entries on success
   via :meth:`record_success`.

Consulted by:

1. :func:`sovyx.voice.health._cascade_verdict.select_alternative_endpoint`
   via the optional ``recent_probe_results`` parameter (§T2.2) — a
   candidate flagged via :meth:`is_known_unopenable` is excluded
   from the failover selection set.
2. The failover loop body's pre-dispatch skip-guard (§T2.4) — if
   the cache short-circuits, emit ``voice.failover.candidate_skipped``
   instead of the expensive open thrash.

Lifecycle decisions (ADR-D3, ADR-D5):

* **Process-local, in-memory.** No on-disk persistence. Reset on
  ``sovyx restart``. Operator-side recovery for stuck-skip states is
  the documented restart playbook.
* **Cardinality bounded.** ``_MAX_ENTRIES = 100`` is a hard ceiling;
  evicts the oldest entry deterministically if exceeded. On real
  hardware the host device-set is typically ≤ 20 entries, so the
  ceiling is a safety belt against future host-API changes.
* **Invalidation on success.** A successful open for
  ``(endpoint_guid, host_api)`` clears the corresponding dead entry
  so a previously-broken device that becomes available (operator
  re-plugs USB, PipeWire restarts) is not stuck in skip-state.
* **No TTL.** Entries persist for the full process lifetime —
  per-boot scope is the natural TTL.
* **Module-level singleton.** Mirrors the
  :func:`sovyx.voice.health._quarantine.get_default_quarantine`
  pattern: lazy first-call construction, ``reset_default_probe_result_cache()``
  for test isolation. Avoids the bootstrap-timing dependency that a
  registry-resolved singleton would introduce.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Final

from sovyx.observability.logging import get_logger
from sovyx.voice.health._failover_error_classifier import (
    classify_error_code,
    is_skip_candidate_class,
)

logger = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class ProbeResultEntry:
    """One probe-or-open outcome keyed by ``(endpoint_guid, host_api)``.

    Frozen + slots-based for cheap hashing + low memory footprint.
    The cache stores AT MOST one entry per key (latest wins) — older
    entries are overwritten on every :meth:`ProbeResultCache.record_probe`
    call.

    Attributes:
        endpoint_guid: Canonical endpoint identifier (the
            ``DeviceEntry.canonical_name``-derived GUID surfaced by
            :func:`sovyx.voice.health.derive_endpoint_guid`). Forms
            half of the cache key.
        host_api: Host-API name (``"WASAPI"``, ``"ALSA"``,
            ``"WDMKS"``). Forms the other half of the cache key —
            the same physical device may probe healthy on one host-
            API and dead on another (e.g. WASAPI exclusive vs shared,
            ALSA hw:0 vs PipeWire passthrough).
        verdict: The probe / open verdict string. Sourced from one of:

            * Boot-cascade :class:`Diagnosis` enum value
              (``"HEALTHY"``, ``"NO_SIGNAL"``, ``"INOPERATIVE"``,
              ``"INCONCLUSIVE"``).
            * Runtime-failover :class:`DeviceChangeRestartVerdict`
              value (``"device_changed_success"``,
              ``"open_failed_no_stream"``, ``"downgraded_to_source"``,
              ``"exception"``).

            Empty string when the producer didn't surface a verdict.
        error_code: Raw PortAudio code / HRESULT / final-code mnemonic.
            Passed through :func:`classify_error_code` to determine
            the dispatch policy. Empty string when no error.
        error_detail: Free-text detail (PortAudio stderr message,
            opener fallback chain summary). Used as a fallback when
            the classifier can't recognise the canonical code.
        callbacks_fired: Count of PortAudio callbacks that fired
            during the probe (anti-pattern #28 — cold-probe signal-
            energy validation). 0 means the open succeeded but no
            audio frames flowed; treat as ``NO_SIGNAL``.
        rms_db: Mean RMS level over the probe window, in dBFS.
            ``float("nan")`` when not measured (runtime-failover
            does not run a probe; it surfaces the open verdict
            directly).
        monotonic_ts: ``time.monotonic()`` at record time. Used by
            the cardinality-cap eviction (oldest entry evicted) and
            by ``last_ladder_complete_monotonic`` surfacing.
    """

    endpoint_guid: str
    host_api: str
    verdict: str
    error_code: str = ""
    error_detail: str = ""
    callbacks_fired: int = 0
    rms_db: float = field(default=float("nan"))
    monotonic_ts: float = 0.0


class ProbeResultCache:
    """Process-local cache; latest-wins keyed by ``(endpoint_guid, host_api)``.

    See module docstring for the architectural decisions (ADR-D3,
    ADR-D4, ADR-D5).

    Thread-safety: the cache is single-process + the underlying
    methods are O(1) dict operations; concurrent reads + writes from
    the boot-cascade probe path (sync) and the failover dispatch
    path (async) coexist safely under the GIL. No lock is held —
    the worst-case race is a single dropped record under contention,
    which the next record overwrites. The cache is observability +
    short-circuit infrastructure; correctness does not depend on
    every write surviving.
    """

    _MAX_ENTRIES: Final[int] = 100
    """Cardinality ceiling. Real hardware has ≤ 20 entries typical;
    100 is a 5× safety belt against future host-API expansion."""

    def __init__(self) -> None:
        self._by_key: dict[tuple[str, str], ProbeResultEntry] = {}

    def record_probe(self, entry: ProbeResultEntry) -> None:
        """Store the latest entry for ``(endpoint_guid, host_api)``.

        Older entries for the same key are overwritten — only the
        most recent probe drives the skip decision per ADR-D5.

        If the cache is at capacity, the oldest entry (lowest
        ``monotonic_ts``) is evicted first. The eviction is
        deterministic; tests rely on the LRU-style ordering.

        Args:
            entry: A :class:`ProbeResultEntry` with at minimum the
                ``endpoint_guid`` and ``host_api`` fields set. The
                ``monotonic_ts`` field is overwritten with
                ``time.monotonic()`` at record-time if zero (so
                callers don't have to pre-populate it).
        """
        if not entry.endpoint_guid:
            return  # observability hygiene — silent no-op on bad input

        ts = entry.monotonic_ts if entry.monotonic_ts > 0.0 else time.monotonic()
        # Re-pack with a definite monotonic_ts. ``dataclass(frozen=True)``
        # forbids in-place mutation; use ``dataclasses.replace`` semantics
        # via constructor.
        normalized = ProbeResultEntry(
            endpoint_guid=entry.endpoint_guid,
            host_api=entry.host_api,
            verdict=entry.verdict,
            error_code=entry.error_code,
            error_detail=entry.error_detail,
            callbacks_fired=entry.callbacks_fired,
            rms_db=entry.rms_db,
            monotonic_ts=ts,
        )

        if (
            len(self._by_key) >= self._MAX_ENTRIES
            and (normalized.endpoint_guid, normalized.host_api) not in self._by_key
        ):
            # Cardinality safeguard. Evict the oldest entry by ts.
            oldest_key = min(self._by_key, key=lambda k: self._by_key[k].monotonic_ts)
            evicted = self._by_key.pop(oldest_key)
            logger.debug(
                "voice.probe_cache.entry_evicted",
                endpoint=evicted.endpoint_guid,
                host_api=evicted.host_api,
                age_s=time.monotonic() - evicted.monotonic_ts,
            )

        self._by_key[(normalized.endpoint_guid, normalized.host_api)] = normalized

    def record_success(self, endpoint_guid: str, host_api: str) -> None:
        """Invalidate any stale dead-entry on successful open.

        ADR-D5 — once a device opens successfully, any prior cache
        entry for the same ``(endpoint_guid, host_api)`` MUST be
        cleared so a future failover dispatch does not skip the
        device based on stale skip-state.

        No-ops when no entry exists. Silent contract — every dispatch
        success calls this regardless of cache contents.
        """
        if not endpoint_guid:
            return
        self._by_key.pop((endpoint_guid, host_api), None)

    def lookup(
        self,
        endpoint_guid: str,
        host_api: str,
    ) -> ProbeResultEntry | None:
        """Return the most recent entry for the key, or ``None``."""
        if not endpoint_guid:
            return None
        return self._by_key.get((endpoint_guid, host_api))

    def is_known_unopenable(
        self,
        endpoint_guid: str,
        host_api: str,
    ) -> bool:
        """Decide whether to skip this candidate based on cached state.

        ADR-D4 — returns ``True`` iff the cache has an entry whose:

        * ``verdict`` is in ``{"NO_SIGNAL", "INOPERATIVE",
          "no_signal", "inoperative"}`` (boot-cascade dead verdicts —
          accepted in both uppercase and lowercase form per
          ``Diagnosis`` StrEnum semantics), OR
        * ``error_code`` classifies via :func:`classify_error_code`
          to a class that :func:`is_skip_candidate_class` flags as
          skip-worthy (``UNOPENABLE_PERMANENT`` or
          ``UNOPENABLE_THIS_BOOT``).

        Returns ``False`` for any non-matching entry AND for the
        no-entry case (conservative — don't skip on absence of info).
        """
        entry = self.lookup(endpoint_guid, host_api)
        if entry is None:
            return False

        # Verdict-driven skip (boot-cascade source).
        verdict_norm = (entry.verdict or "").strip().lower()
        if verdict_norm in ("no_signal", "inoperative"):
            return True

        # Error-class driven skip (runtime-failover source).
        if entry.error_code or entry.error_detail:
            cls = classify_error_code(entry.error_code, entry.error_detail)
            if is_skip_candidate_class(cls):
                return True

        return False

    def __len__(self) -> int:
        """Number of cached entries — used by tests + dashboards."""
        return len(self._by_key)

    def entries(self) -> list[ProbeResultEntry]:
        """Snapshot of all cached entries, newest first.

        Used by the ``sovyx doctor voice`` surface (§T2.11) and the
        dashboard widget (§T2.10) to render the cache state.
        Returns a fresh list each call; safe to iterate without
        worrying about concurrent mutation.
        """
        return sorted(
            self._by_key.values(),
            key=lambda e: e.monotonic_ts,
            reverse=True,
        )


# Module-level lazy singleton — mirrors
# ``sovyx.voice.health._quarantine.get_default_quarantine`` pattern.
_SINGLETON: ProbeResultCache | None = None


def get_default_probe_result_cache() -> ProbeResultCache:
    """Return (and lazily construct) the process-wide cache.

    Tests that need a fresh instance call
    :func:`reset_default_probe_result_cache` before first use. Mirrors
    the :func:`sovyx.voice.health._quarantine.get_default_quarantine`
    contract.
    """
    global _SINGLETON  # noqa: PLW0603 — lazy singleton, not user-mutable state
    if _SINGLETON is None:
        _SINGLETON = ProbeResultCache()
    return _SINGLETON


def reset_default_probe_result_cache() -> None:
    """Drop the singleton — tests use this between cases for isolation."""
    global _SINGLETON  # noqa: PLW0603 — lazy singleton, not user-mutable state
    _SINGLETON = None


__all__ = [
    "ProbeResultCache",
    "ProbeResultEntry",
    "get_default_probe_result_cache",
    "reset_default_probe_result_cache",
]
