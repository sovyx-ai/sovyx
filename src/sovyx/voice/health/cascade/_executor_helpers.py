"""Cascade executor helpers — result builders + structured logging.

Phase 5.F.7 god-file extraction from
``voice/health/cascade/_executor.py`` (anti-pattern #16). Owns the
6 internal helpers + 1 logging cap constant that the cascade entry
points (``run_cascade`` / ``run_cascade_for_candidates``) share:

* :func:`_make_result` — :class:`CascadeResult` constructor with
  consistent field defaults across pinned/store/walk paths.
* :func:`_compute_diagnosis_histogram` — Phase 6 / T6.20
  cascade-exhausted triage histogram (``{diagnosis_value: count}``).
* :func:`_combo_tag` — compact :class:`Combo` representation for
  structured log fields. Note: a sibling implementation lives in
  :mod:`sovyx.voice.health.probe._dispatch`; the two are
  intentionally independent — the probe-side version is owned by
  the probe layer, the cascade-side by this module, and both have
  the same shape.
* :func:`_truncate_detail` + :data:`_LOG_DETAIL_MAX_CHARS` — clamp
  for ``error_detail`` fields in cascade/probe events. Mirrors the
  cap used by ``anomaly.latency_spike`` so structured fields stay
  within OTLP attribute-size limits.
* :func:`_log_probe_call` + :func:`_log_probe_result` — uniform
  pre-/post-probe structured log records emitted across pinned /
  store / cascade walk paths so post-mortem log greps see the same
  key set regardless of which source fed the probe call.

All helpers are pure / observability-only — no side effects beyond
log emission. Anti-pattern #20 covered: parent module
``cascade/_executor.py`` re-exports every symbol so existing
internal callers continue to resolve via standard module-namespace
lookup.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from sovyx.observability.logging import get_logger

if TYPE_CHECKING:
    from collections.abc import Sequence

    from sovyx.voice.health.contract import (
        CascadeResult,
        Combo,
        ProbeMode,
        ProbeResult,
    )

logger = get_logger(__name__)


def _make_result(
    *,
    endpoint_guid: str,
    winning_combo: Combo | None,
    winning_probe: ProbeResult | None,
    attempts: list[ProbeResult],
    attempts_count: int,
    budget_exhausted: bool,
    source: str,
) -> CascadeResult:
    from sovyx.voice.health.contract import CascadeResult

    return CascadeResult(
        endpoint_guid=endpoint_guid,
        winning_combo=winning_combo,
        winning_probe=winning_probe,
        attempts=tuple(attempts),
        attempts_count=attempts_count,
        budget_exhausted=budget_exhausted,
        source=source,
    )


def _compute_diagnosis_histogram(attempts: Sequence[ProbeResult]) -> dict[str, int]:
    """Build ``{diagnosis_value: count}`` from a list of cascade attempts.

    Phase 6 / T6.20 — operators page on
    :data:`voice.cascade.exhausted` and need ONE log line that
    summarises the failure-mode distribution across N attempts. The
    per-attempt ``voice_cascade_attempt`` lines + the existing
    OTel ``voice_health_cascade_attempts`` counter give the drill-
    down; the histogram is the at-a-glance triage signal.

    Empty / missing attempts return ``{}`` defensively. The cascade
    in production always has ≥ 1 attempt before exhaustion (every
    platform cascade table is non-empty), but the budget-exhausted
    site can fire with zero attempts when the deadline trips on the
    first iteration — the empty histogram is the correct surface
    for that case.

    Diagnosis enum values are :class:`StrEnum` so ``.value`` returns
    the canonical lowercase wire form (``"healthy"``, ``"no_signal"``,
    etc.) — same shape monitoring tooling already consumes from the
    per-attempt log lines.

    Returns:
        Dict mapping diagnosis-value strings to integer counts.
        Iteration order matches first-seen order in ``attempts``;
        ``json.dumps`` sorts by key, so the wire shape is stable
        across boots regardless of attempt-order randomness.
    """
    histogram: dict[str, int] = {}
    for attempt in attempts:
        key = attempt.diagnosis.value
        histogram[key] = histogram.get(key, 0) + 1
    return histogram


def _combo_tag(combo: Combo) -> str:
    """Compact string representation for structured log fields."""
    excl = "excl" if combo.exclusive else "shared"
    return (
        f"{combo.host_api}/{combo.sample_rate}Hz/{combo.channels}ch/"
        f"{combo.sample_format}/{excl}/{combo.frames_per_buffer}f"
    )


_LOG_DETAIL_MAX_CHARS = 512
"""Cap on ``error_detail`` truncation in cascade/probe events (T1).

Matches the cap used by ``anomaly.latency_spike`` so structured fields
stay within OTLP attribute-size limits without surprising operators.
"""


def _truncate_detail(detail: str | None) -> str:
    """Clamp ``detail`` for structured log fields; safe for ``None``."""
    if not detail:
        return ""
    if len(detail) <= _LOG_DETAIL_MAX_CHARS:
        return detail
    return detail[: _LOG_DETAIL_MAX_CHARS - 1] + "…"


def _log_probe_call(
    *,
    endpoint_guid: str,
    attempt: int,
    device_index: int,
    combo: Combo,
    mode: ProbeMode,
    attempt_budget_s: float,
) -> None:
    """Emit ``voice_cascade_probe_call`` before every probe invocation (T1).

    Uniform across cascade/pinned/store paths so post-mortem log greps
    see the same structured key set regardless of which source fed the
    probe call.
    """
    logger.info(
        "voice_cascade_probe_call",
        endpoint=endpoint_guid,
        attempt=attempt,
        device_index=device_index,
        combo_host_api=combo.host_api,
        combo_sample_rate=combo.sample_rate,
        combo_channels=combo.channels,
        combo_sample_format=combo.sample_format,
        combo_exclusive=combo.exclusive,
        combo_auto_convert=combo.auto_convert,
        combo_frames_per_buffer=combo.frames_per_buffer,
        mode=str(mode),
        attempt_budget_s=attempt_budget_s,
    )


def _log_probe_result(
    *,
    endpoint_guid: str,
    attempt: int,
    device_index: int,
    combo: Combo,
    result: ProbeResult,
    physical_device_id: str = "",
) -> None:
    """Emit ``voice_cascade_probe_result`` after every probe invocation (T1).

    Mission C3 §T2.4 — also writes the result into the process-local
    :class:`ProbeResultCache` so the runtime failover ladder can consult
    it (the loop body's direct pre-dispatch skip-guard AND the candidate
    selector via ``select_alternative_endpoint(recent_probe_results=...)``,
    wired from ``_resolve_target_safe``) to skip candidates that probed
    dead at boot without paying the open-thrash per skipped device.

    AP #53 (HEALTH-3, 2026-07-02): the runtime consumer keys its lookup
    by ``DeviceEntry.canonical_name``, not the endpoint GUID — so when
    the caller supplies ``physical_device_id`` (the canonical name, same
    identity the quarantine stores) a TWIN cache entry is recorded under
    it. The host-API half of the key is normalised inside the cache
    (:func:`~sovyx.voice.health._probe_result_cache._normalize_host_api`),
    so the planner literal recorded here matches the PortAudio label the
    consumer passes.

    The cache write is best-effort — a failure here MUST NOT block the
    probe-result logging path (which is the historically-load-bearing
    observability surface).
    """
    logger.info(
        "voice_cascade_probe_result",
        endpoint=endpoint_guid,
        attempt=attempt,
        device_index=device_index,
        combo_host_api=combo.host_api,
        combo_sample_rate=combo.sample_rate,
        diagnosis=str(result.diagnosis),
        rms_db=result.rms_db,
        callbacks_fired=result.callbacks_fired,
        duration_ms=result.duration_ms,
        error_detail=_truncate_detail(result.error),
    )

    # Mission C3 §T2.4 — populate the runtime probe-result cache so
    # the failover loop body has a queryable history. Lazy import so
    # this module stays loadable when probe-cache is absent (test
    # fixtures that mock out the voice.health surface). Best-effort:
    # any exception is swallowed via ``contextlib.suppress`` per
    # anti-pattern #27.
    import contextlib

    with contextlib.suppress(Exception):
        from sovyx.voice.health._probe_result_cache import (
            ProbeResultEntry,
            get_default_probe_result_cache,
        )

        cache = get_default_probe_result_cache()
        for cache_key_guid in {endpoint_guid, physical_device_id}:
            if not cache_key_guid:
                continue
            cache.record_probe(
                ProbeResultEntry(
                    endpoint_guid=cache_key_guid,
                    host_api=combo.host_api or "",
                    verdict=str(result.diagnosis),
                    error_code="",  # boot cascade probe doesn't surface PA codes
                    error_detail=result.error or "",
                    callbacks_fired=result.callbacks_fired,
                    rms_db=result.rms_db,
                ),
            )


__all__ = [
    "_LOG_DETAIL_MAX_CHARS",
    "_combo_tag",
    "_compute_diagnosis_histogram",
    "_log_probe_call",
    "_log_probe_result",
    "_make_result",
    "_truncate_detail",
]
