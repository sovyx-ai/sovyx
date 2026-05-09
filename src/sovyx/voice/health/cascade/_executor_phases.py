"""Cascade-walk phase helpers — pinned / store-fast-path / cascade walk.

Phase 5.F.18 god-file extraction from
``voice/health/cascade/_executor.py`` (anti-pattern #16). Owns the
3 main phases of ``_run_cascade_locked`` factored as helper coroutines
that share state via :class:`_CascadeRunContext`:

* :func:`_run_phase_pinned` — pinned-override probe (returns
  :class:`CascadeResult` on winner / quarantine; ``None`` on
  fall-through to the next phase).
* :func:`_run_phase_store` — :class:`ComboStore` fast-path probe
  (same return convention).
* :func:`_run_phase_cascade_walk` — platform-cascade-table walk
  (terminal — always returns a :class:`CascadeResult`).

Each phase reads from + mutates a shared :class:`_CascadeRunContext`
that holds the immutable inputs + the cumulative ``attempts`` list
+ ``attempts_count`` counter + ``deadline``. The dataclass replaces
22+ kwargs threading through nested call sites.

Anti-pattern #20 covered: parent module
``voice/health/cascade/_executor.py`` re-exports every symbol so the
in-parent ``_run_cascade_locked`` orchestrator (which constructs the
context + dispatches the phases) resolves via standard module-
namespace lookup.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from sovyx.observability.logging import get_logger
from sovyx.voice.health._metrics import (
    record_cascade_attempt,
    record_combo_store_hit,
)
from sovyx.voice.health._user_remediation import (
    homogeneous_diagnosis_remediation,
)
from sovyx.voice.health.cascade._alignment import (
    _lookup_override,
    _lookup_store,
)
from sovyx.voice.health.cascade._budget import (
    _VOICE_CLARITY_AUTOFIX_FIRST_ATTEMPT,
    _quarantine_endpoint,
    _record_winner,
)
from sovyx.voice.health.cascade._executor_helpers import (
    _combo_tag,
    _compute_diagnosis_histogram,
    _log_probe_call,
    _log_probe_result,
    _make_result,
)
from sovyx.voice.health.cascade._executor_probe import (
    _PHYSICAL_CURE_DIAGNOSES,
    _try_combo,
)
from sovyx.voice.health.cascade._planner import (
    _platform_cascade,
)
from sovyx.voice.health.contract import (
    CascadeResult,
    Diagnosis,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from sovyx.voice.health._quarantine import EndpointQuarantine
    from sovyx.voice.health.capture_overrides import CaptureOverrides
    from sovyx.voice.health.cascade._executor_probe import ProbeCallable
    from sovyx.voice.health.combo_store import ComboStore
    from sovyx.voice.health.contract import (
        Combo,
        ProbeMode,
        ProbeResult,
    )

logger = get_logger(__name__)


@dataclass(slots=True)
class _CascadeRunContext:
    """Mutable state passed between cascade-walk phases.

    Replaces the 22+ kwargs that ``_run_cascade_locked`` previously
    threaded through every short-circuit branch + helper call. Each
    phase reads its inputs, mutates ``attempts`` / ``attempts_count``,
    and either returns a terminal :class:`CascadeResult` or ``None``
    to indicate the next phase should run.
    """

    # Identity (immutable inputs)
    endpoint_guid: str
    device_index: int
    mode: ProbeMode
    platform_key: str
    device_friendly_name: str
    device_interface_name: str
    device_class: str
    endpoint_fxproperties_sha: str
    detected_apos: Sequence[str]
    physical_device_id: str

    # Stores + injected callables (immutable inputs)
    combo_store: ComboStore | None
    capture_overrides: CaptureOverrides | None
    probe_fn: ProbeCallable
    quarantine: EndpointQuarantine | None

    # Budget (immutable inputs)
    total_budget_s: float
    attempt_budget_s: float
    deadline: float
    clock: Callable[[], float]

    # Cascade-walk knobs (immutable inputs)
    voice_clarity_autofix: bool
    cascade_override: Sequence[Combo] | None

    # Mutable cumulative state
    attempts: list[ProbeResult] = field(default_factory=list)
    attempts_count: int = 0


async def _run_phase_pinned(
    ctx: _CascadeRunContext,
) -> CascadeResult | None:
    """Phase 1 — pinned override probe.

    Returns a terminal :class:`CascadeResult` when the pinned combo
    wins or when the diagnosis is a physical-cure pattern requiring
    quarantine. Returns ``None`` when no pinned override exists OR
    the pinned probe failed with a recoverable diagnosis (caller
    falls through to the store fast-path).
    """
    pinned = _lookup_override(ctx.capture_overrides, ctx.endpoint_guid, ctx.platform_key)
    if pinned is None:
        return None
    logger.info(
        "voice_cascade_pinned_lookup",
        endpoint=ctx.endpoint_guid,
        combo=_combo_tag(pinned),
    )
    _log_probe_call(
        endpoint_guid=ctx.endpoint_guid,
        attempt=0,
        device_index=ctx.device_index,
        combo=pinned,
        mode=ctx.mode,
        attempt_budget_s=ctx.attempt_budget_s,
    )
    result = await _try_combo(
        probe_fn=ctx.probe_fn,
        combo=pinned,
        mode=ctx.mode,
        device_index=ctx.device_index,
        attempt_budget_s=ctx.attempt_budget_s,
    )
    _log_probe_result(
        endpoint_guid=ctx.endpoint_guid,
        attempt=0,
        device_index=ctx.device_index,
        combo=pinned,
        result=result,
    )
    ctx.attempts.append(result)
    ctx.attempts_count += 1
    record_cascade_attempt(
        platform=ctx.platform_key,
        host_api=pinned.host_api,
        success=result.diagnosis is Diagnosis.HEALTHY,
        source="pinned",
    )
    if result.diagnosis is Diagnosis.HEALTHY:
        # T1 — uniform winner telemetry across pinned/store/cascade.
        logger.info(
            "voice_cascade_winner_selected",
            endpoint=ctx.endpoint_guid,
            source="pinned",
            attempts=1,
            combo_host_api=pinned.host_api,
            combo_sample_rate=pinned.sample_rate,
            combo_channels=pinned.channels,
            combo_exclusive=pinned.exclusive,
            combo_auto_convert=pinned.auto_convert,
            device_index=ctx.device_index,
            device_friendly_name=ctx.device_friendly_name,
        )
        return _make_result(
            endpoint_guid=ctx.endpoint_guid,
            winning_combo=pinned,
            winning_probe=result,
            attempts=ctx.attempts,
            attempts_count=0,
            budget_exhausted=False,
            source="pinned",
        )
    # §4.4.7 + T6.9 — physical-cure diagnoses. Every host API will
    # fail equally; trying the ComboStore or the cascade loop just
    # wastes the user's time. KERNEL_INVALIDATED + STREAM_OPEN_TIMEOUT
    # share the same semantic (driver wedged at IAudioClient /
    # callback layer, no user-mode cure available) and route to the
    # same quarantine + short-circuit path.
    if result.diagnosis in _PHYSICAL_CURE_DIAGNOSES and _quarantine_endpoint(
        quarantine=ctx.quarantine,
        endpoint_guid=ctx.endpoint_guid,
        device_friendly_name=ctx.device_friendly_name,
        device_interface_name=ctx.device_interface_name,
        host_api=pinned.host_api,
        platform_key=ctx.platform_key,
        reason="probe_pinned",
        physical_device_id=ctx.physical_device_id,
    ):
        logger.warning(
            "voice_cascade_physical_cure_required",
            endpoint=ctx.endpoint_guid,
            friendly_name=ctx.device_friendly_name,
            host_api=pinned.host_api,
            source="pinned",
            diagnosis=result.diagnosis.value,
        )
        return _make_result(
            endpoint_guid=ctx.endpoint_guid,
            winning_combo=None,
            winning_probe=None,
            attempts=ctx.attempts,
            attempts_count=ctx.attempts_count,
            budget_exhausted=False,
            source="quarantined",
        )
    logger.warning(
        "voice_cascade_pinned_failed",
        endpoint=ctx.endpoint_guid,
        host_api=pinned.host_api,
        combo=_combo_tag(pinned),
        diagnosis=str(result.diagnosis),
    )
    return None


async def _run_phase_store(
    ctx: _CascadeRunContext,
) -> CascadeResult | None:
    """Phase 2 — :class:`ComboStore` fast-path probe.

    Returns a terminal :class:`CascadeResult` on store hit + HEALTHY,
    on physical-cure quarantine, or on budget exhaustion. Returns
    ``None`` on store miss OR fast-path probe failure (caller falls
    through to the platform cascade walk).
    """
    store_combo = _lookup_store(ctx.combo_store, ctx.endpoint_guid)
    if store_combo is None:
        record_combo_store_hit(
            endpoint_class=ctx.device_class or "unknown",
            result="miss",
        )
        return None
    if ctx.clock() >= ctx.deadline:
        return _make_result(
            endpoint_guid=ctx.endpoint_guid,
            winning_combo=None,
            winning_probe=None,
            attempts=ctx.attempts,
            attempts_count=ctx.attempts_count,
            budget_exhausted=True,
            source="none",
        )
    logger.info(
        "voice_cascade_store_lookup",
        endpoint=ctx.endpoint_guid,
        combo=_combo_tag(store_combo),
    )
    _log_probe_call(
        endpoint_guid=ctx.endpoint_guid,
        attempt=0,
        device_index=ctx.device_index,
        combo=store_combo,
        mode=ctx.mode,
        attempt_budget_s=ctx.attempt_budget_s,
    )
    result = await _try_combo(
        probe_fn=ctx.probe_fn,
        combo=store_combo,
        mode=ctx.mode,
        device_index=ctx.device_index,
        attempt_budget_s=ctx.attempt_budget_s,
    )
    _log_probe_result(
        endpoint_guid=ctx.endpoint_guid,
        attempt=0,
        device_index=ctx.device_index,
        combo=store_combo,
        result=result,
    )
    ctx.attempts.append(result)
    success = result.diagnosis is Diagnosis.HEALTHY
    record_cascade_attempt(
        platform=ctx.platform_key,
        host_api=store_combo.host_api,
        success=success,
        source="store",
    )
    record_combo_store_hit(
        endpoint_class=ctx.device_class or "unknown",
        result="hit" if success else "needs_revalidation",
    )
    if success:
        # Fast-path hit: do NOT re-record (combo already in store).
        # T1 — uniform winner telemetry across pinned/store/cascade.
        logger.info(
            "voice_cascade_winner_selected",
            endpoint=ctx.endpoint_guid,
            source="store",
            attempts=1,
            combo_host_api=store_combo.host_api,
            combo_sample_rate=store_combo.sample_rate,
            combo_channels=store_combo.channels,
            combo_exclusive=store_combo.exclusive,
            combo_auto_convert=store_combo.auto_convert,
            device_index=ctx.device_index,
            device_friendly_name=ctx.device_friendly_name,
        )
        return _make_result(
            endpoint_guid=ctx.endpoint_guid,
            winning_combo=store_combo,
            winning_probe=result,
            attempts=ctx.attempts,
            attempts_count=0,
            budget_exhausted=False,
            source="store",
        )
    # §4.4.7 + T6.9 — physical-cure state observed on the fast path.
    # Invalidate the (now misleading) store entry too, then quarantine
    # the endpoint and short-circuit the rest of the cascade.
    # KERNEL_INVALIDATED + STREAM_OPEN_TIMEOUT both route here.
    if result.diagnosis in _PHYSICAL_CURE_DIAGNOSES and _quarantine_endpoint(
        quarantine=ctx.quarantine,
        endpoint_guid=ctx.endpoint_guid,
        device_friendly_name=ctx.device_friendly_name,
        device_interface_name=ctx.device_interface_name,
        host_api=store_combo.host_api,
        platform_key=ctx.platform_key,
        reason="probe_store",
        physical_device_id=ctx.physical_device_id,
    ):
        if ctx.combo_store is not None:
            ctx.combo_store.invalidate(ctx.endpoint_guid, reason=result.diagnosis.value)
        logger.warning(
            "voice_cascade_physical_cure_required",
            endpoint=ctx.endpoint_guid,
            friendly_name=ctx.device_friendly_name,
            host_api=store_combo.host_api,
            source="store",
            diagnosis=result.diagnosis.value,
        )
        return _make_result(
            endpoint_guid=ctx.endpoint_guid,
            winning_combo=None,
            winning_probe=None,
            attempts=ctx.attempts,
            attempts_count=ctx.attempts_count,
            budget_exhausted=False,
            source="quarantined",
        )
    # Invalidate the stale store entry so the next boot runs the
    # full cascade fresh rather than re-probing the known-bad combo.
    # The metric is emitted inside ``ComboStore.invalidate`` — single
    # source of truth for every invalidation path.
    if ctx.combo_store is not None:
        ctx.combo_store.invalidate(ctx.endpoint_guid, reason="fast_path_probe_failed")
        logger.warning(
            "voice_cascade_store_invalidated",
            endpoint=ctx.endpoint_guid,
            host_api=store_combo.host_api,
            combo=_combo_tag(store_combo),
            diagnosis=str(result.diagnosis),
        )
    return None


async def _run_phase_cascade_walk(
    ctx: _CascadeRunContext,
) -> CascadeResult:
    """Phase 3 — platform cascade-table walk (terminal).

    Walks the platform-specific cascade table (or ``cascade_override``
    when supplied), probing each combo in sequence. Always returns a
    terminal :class:`CascadeResult` — either the first HEALTHY combo,
    a physical-cure quarantine, budget-exhausted, or cascade-exhausted.
    """
    cascade = (
        tuple(ctx.cascade_override)
        if ctx.cascade_override is not None
        else _platform_cascade(ctx.platform_key)
    )
    start_idx = 0 if ctx.voice_clarity_autofix else _VOICE_CLARITY_AUTOFIX_FIRST_ATTEMPT
    if ctx.platform_key != "win32":
        # voice_clarity_autofix is Windows-only; on Linux/macOS start at 0.
        start_idx = 0

    # T6.9 — set when EXCLUSIVE_MODE_NOT_AVAILABLE is observed on a
    # combo with ``exclusive=True``. Subsequent iterations skip every
    # remaining combo with ``exclusive=True`` because the endpoint
    # fundamentally doesn't permit exclusive mode — retrying other
    # exclusive combos for the same endpoint just burns the
    # per-attempt budget. Shared-mode combos (``exclusive=False``)
    # are still tried because they take a different driver code path.
    skip_remaining_exclusive = False

    for idx, combo in enumerate(cascade):
        if idx < start_idx:
            continue
        # T6.9 skip-remaining-exclusive optimisation.
        if skip_remaining_exclusive and combo.exclusive:
            logger.info(
                "voice_cascade_combo_skipped_exclusive_mode_not_available",
                endpoint=ctx.endpoint_guid,
                attempt=idx,
                combo=_combo_tag(combo),
            )
            continue
        if ctx.clock() >= ctx.deadline:
            logger.warning(
                "voice_cascade_budget_exhausted",
                endpoint=ctx.endpoint_guid,
                attempts_run=ctx.attempts_count,
                total_budget_s=ctx.total_budget_s,
                # T6.11 — diagnosis histogram for at-a-glance triage.
                # Empty when the deadline trips before any attempt
                # completes (first-iteration timeout). See
                # :func:`_compute_diagnosis_histogram` for shape.
                diagnosis_histogram=_compute_diagnosis_histogram(ctx.attempts),
            )
            return _make_result(
                endpoint_guid=ctx.endpoint_guid,
                winning_combo=None,
                winning_probe=None,
                attempts=ctx.attempts,
                attempts_count=ctx.attempts_count,
                budget_exhausted=True,
                source="none",
            )
        ctx.attempts_count += 1
        logger.info(
            "voice_cascade_attempt",
            endpoint=ctx.endpoint_guid,
            attempt=idx,
            combo=_combo_tag(combo),
        )
        _log_probe_call(
            endpoint_guid=ctx.endpoint_guid,
            attempt=idx,
            device_index=ctx.device_index,
            combo=combo,
            mode=ctx.mode,
            attempt_budget_s=ctx.attempt_budget_s,
        )
        result = await _try_combo(
            probe_fn=ctx.probe_fn,
            combo=combo,
            mode=ctx.mode,
            device_index=ctx.device_index,
            attempt_budget_s=ctx.attempt_budget_s,
        )
        _log_probe_result(
            endpoint_guid=ctx.endpoint_guid,
            attempt=idx,
            device_index=ctx.device_index,
            combo=combo,
            result=result,
        )
        ctx.attempts.append(result)
        record_cascade_attempt(
            platform=ctx.platform_key,
            host_api=combo.host_api,
            success=result.diagnosis is Diagnosis.HEALTHY,
            source="cascade",
        )
        # §4.4.7 + T6.9 — physical-cure state. Every remaining host API
        # in the cascade table will fail identically because the failure
        # is at IAudioClient::Initialize / kernel callback layer, upstream
        # of the host-API layer. Quarantine + break the loop instead of
        # burning the per-attempt budget on combos we already know will
        # fail. KERNEL_INVALIDATED + STREAM_OPEN_TIMEOUT (T6.2) share
        # this semantic.
        if result.diagnosis in _PHYSICAL_CURE_DIAGNOSES and _quarantine_endpoint(
            quarantine=ctx.quarantine,
            endpoint_guid=ctx.endpoint_guid,
            device_friendly_name=ctx.device_friendly_name,
            device_interface_name=ctx.device_interface_name,
            host_api=combo.host_api,
            platform_key=ctx.platform_key,
            reason="probe_cascade",
            physical_device_id=ctx.physical_device_id,
        ):
            logger.warning(
                "voice_cascade_physical_cure_required",
                endpoint=ctx.endpoint_guid,
                friendly_name=ctx.device_friendly_name,
                host_api=combo.host_api,
                source="cascade",
                attempt=idx,
                diagnosis=result.diagnosis.value,
            )
            return _make_result(
                endpoint_guid=ctx.endpoint_guid,
                winning_combo=None,
                winning_probe=None,
                attempts=ctx.attempts,
                attempts_count=ctx.attempts_count,
                budget_exhausted=False,
                source="quarantined",
            )
        # T6.9 — once an exclusive-mode combo returns
        # EXCLUSIVE_MODE_NOT_AVAILABLE, the endpoint definitively
        # doesn't permit exclusive mode. Mark the rest of the loop
        # to skip exclusive combos (saves wall-clock budget for
        # shared-mode candidates that have a real chance). Other
        # diagnoses are routine fall-through to the next combo.
        if result.diagnosis is Diagnosis.EXCLUSIVE_MODE_NOT_AVAILABLE and combo.exclusive:
            skip_remaining_exclusive = True
        if result.diagnosis is Diagnosis.HEALTHY:
            _record_winner(
                combo_store=ctx.combo_store,
                endpoint_guid=ctx.endpoint_guid,
                device_friendly_name=ctx.device_friendly_name,
                device_interface_name=ctx.device_interface_name,
                device_class=ctx.device_class,
                endpoint_fxproperties_sha=ctx.endpoint_fxproperties_sha,
                detected_apos=ctx.detected_apos,
                combo=combo,
                probe=result,
                cascade_attempts_before_success=ctx.attempts_count,
            )
            # T1 — DoD #3 requires this event to be present in the log
            # after a successful cascade run. Future T3 will extend it
            # with ``winning_candidate`` / ``candidate_source`` fields
            # once the candidate-set refactor lands.
            logger.info(
                "voice_cascade_winner_selected",
                endpoint=ctx.endpoint_guid,
                source="cascade",
                attempts=ctx.attempts_count,
                combo_host_api=combo.host_api,
                combo_sample_rate=combo.sample_rate,
                combo_channels=combo.channels,
                combo_exclusive=combo.exclusive,
                combo_auto_convert=combo.auto_convert,
                device_index=ctx.device_index,
                device_friendly_name=ctx.device_friendly_name,
            )
            return _make_result(
                endpoint_guid=ctx.endpoint_guid,
                winning_combo=combo,
                winning_probe=result,
                attempts=ctx.attempts,
                attempts_count=ctx.attempts_count,
                budget_exhausted=False,
                source="cascade",
            )

    histogram = _compute_diagnosis_histogram(ctx.attempts)
    logger.error(
        "voice_cascade_exhausted",
        endpoint=ctx.endpoint_guid,
        attempts=ctx.attempts_count,
        # T6.11 — diagnosis histogram. Cascade-table-exhausted is the
        # critical case (every combo failed); the histogram surfaces
        # WHICH failure modes dominated so operators can route alerts:
        # ``device_busy`` heavy → another app holds the mic;
        # ``apo_degraded`` heavy → Voice Clarity / similar APO chain;
        # ``permission_denied`` → OS gate; etc.
        diagnosis_histogram=histogram,
    )
    # T6.12 — homogeneous-failure user-actionable signal. When EVERY
    # cascade attempt died with the same diagnosis AND that diagnosis
    # has a known user-facing remediation, emit the dedicated
    # ``voice_cascade_user_actionable`` event so the dashboard banner
    # can route on it WITHOUT scraping the histogram. Heterogeneous
    # exhaustions OR homogeneous exhaustions on diagnoses without a
    # remediation entry (HEALTHY, MIXER_*, UNKNOWN) skip the event.
    homogeneous = homogeneous_diagnosis_remediation(histogram)
    if homogeneous is not None:
        diagnosis_value, remediation = homogeneous
        logger.error(
            "voice_cascade_user_actionable",
            endpoint=ctx.endpoint_guid,
            attempts=ctx.attempts_count,
            diagnosis=diagnosis_value,
            remediation=remediation,
        )
    return _make_result(
        endpoint_guid=ctx.endpoint_guid,
        winning_combo=None,
        winning_probe=None,
        attempts=ctx.attempts,
        attempts_count=ctx.attempts_count,
        budget_exhausted=False,
        source="none",
    )


__all__ = [
    "_CascadeRunContext",
    "_run_phase_cascade_walk",
    "_run_phase_pinned",
    "_run_phase_store",
]
