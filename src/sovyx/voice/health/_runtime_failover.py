"""Runtime hot-failover after endpoint quarantine.

Mission anchors:

* Original (single-shot dispatch):
  ``docs-internal/missions/MISSION-voice-linux-silent-mic-remediation-2026-05-04.md``
  §Phase 2 T2.6 — introduced :func:`_try_runtime_failover` as the
  closure entry point invoked by the deaf-signal closure after the
  bypass coordinator returns ineffective outcomes.
* Loop-in-place refactor (Mission C3):
  ``docs-internal/missions/MISSION-c3-failover-ladder-iteration-2026-05-16.md``
  §T1.1 — replaces the single-shot dispatch with bounded loop-in-place
  candidate iteration after the v0.43.1 operator session showed
  ``voice.failover.failed verdict=downgraded_to_source`` at L1063 with
  ``candidates_remaining = 2`` stranded (operator log L1015 → L1063,
  2026-05-14).

The closure is invoked AFTER the bypass coordinator at
:func:`sovyx.voice.pipeline._orchestrator._invoke_deaf_signal` returns
a non-empty outcome list with no :attr:`BypassVerdict.APPLIED_HEALTHY`
entries. Pre-mission C3 this helper dispatched ONE candidate per call
and returned on either success or failure; any subsequent candidate
had to wait for (a) the next deaf-signal heartbeat, (b) the
:attr:`VoiceTuningConfig.failover_cooldown_s` cooldown gate, AND
(c) the coordinator-terminated latch to NOT have fired in between.
In the operator's session none of these conditions held, so the
remaining candidates (PipeWire virtual + OS default) were never tried.

Post-Mission C3 the helper iterates every non-excluded candidate
within a single closure invocation, with a per-candidate intra-ladder
cooldown (:attr:`VoiceTuningConfig.failover_intra_ladder_cooldown_s`,
default 2.0 s) gating successive dispatches and a per-ladder cap
(:attr:`VoiceTuningConfig.failover_candidate_max_attempts_per_ladder`,
default 5) bounding runaway iteration. The outer
:attr:`VoiceTuningConfig.failover_cooldown_s` is RE-INTERPRETED as
the inter-invocation gate (it still gates the *outer* deaf-signal
closure invocation) — the docstring + mission §T1.1 ADR-D2 document
this re-interpretation.

Staged adoption (per ``feedback_staged_adoption``):

* **Lenient telemetry mode (always on):** every ladder invocation
  emits ``voice.failover.attempted`` regardless of the gate so
  dashboards can calibrate the false-positive rate against real
  production deaf-signal events. The event carries
  ``voice.gate_enabled`` so downstream consumers can split "would
  have happened" from "actually happened".
* **Behavioural mode (gated):** the actual
  ``request_device_change_restart`` dispatch + coordinator reset
  fire only when
  :attr:`sovyx.engine.config.VoiceTuningConfig.runtime_failover_on_quarantine_enabled`
  is ``True`` (default ``True`` since v0.30.13 / Mission §Phase 3 T3.2).

Event lattice (Mission C3 §T1.3 — additive, no legacy event removed
in Phase 1 LENIENT):

* Legacy (preserved):

    - ``voice.failover.attempted`` — lenient telemetry, fires once
      per closure invocation BEFORE any gate is consulted.
    - ``voice.failover.selection_failed`` — initial selection raised.
    - ``voice.failover.exhausted`` — outer cap hit OR no candidates
      AT ALL (idempotent via :attr:`RuntimeFailoverState.exhausted_emitted`).
    - ``voice.failover.cooldown_blocked`` — outer cooldown gate active.
    - ``voice.failover.succeeded`` — a candidate engaged successfully.
    - ``voice.failover.failed`` — ladder exited without success;
      carries metadata of the last attempted candidate. Fires AT MOST
      ONCE per closure invocation (per-ladder summary).
    - ``voice.failover.coordinator_reset_failed`` — pipeline reset
      raised after a successful candidate.

* New (Mission C3 §T1.3):

    - ``voice.failover.ladder_started`` — fires at ladder entry with
      ``ladder_id`` (uuid4) so dashboards correlate per-candidate
      events within a single ladder run.
    - ``voice.failover.candidate_attempted`` — fires per dispatch
      with ``index, candidate_count, candidates_remaining`` so
      operators see real-time iteration progress.
    - ``voice.failover.candidate_failed`` — fires per failed dispatch
      with ``index, verdict, error_class`` for triage detail.
    - ``voice.failover.candidate_skipped`` — schema present in
      Phase 1; only emits in Phase 2 once the probe-result cache
      wire-up (§T2.4) lands. Carries ``cached_verdict, reason``.
    - ``voice.failover.ladder_complete`` — fires exactly once per
      ladder invocation that entered the loop body; carries
      ``verdict (succeeded | exhausted), succeeded_index | null,
      candidates_tried, elapsed_ms, ladder_id``.

Forensic anchor (Mission C3):
``c:\\Users\\guipe\\Downloads\\docs_teste.txt`` lines 1015 → 1063 —
``voice.failover.attempted ... candidates_remaining=3`` at L1015,
``voice.failover.failed verdict=downgraded_to_source`` at L1063, with
the remaining 2 candidates never tried. The regression test at
``tests/regression/test_c3_failover_ladder_iteration.py`` replays
this scenario as the F2 falsifiability gate.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from sovyx.observability.logging import get_logger
from sovyx.voice.health._failover_error_classifier import (
    FailoverErrorClass,
    classify_error_code,
)
from sovyx.voice.health._probe_result_cache import (
    ProbeResultEntry,
    get_default_probe_result_cache,
)

if TYPE_CHECKING:
    from sovyx.engine.config import VoiceTuningConfig
    from sovyx.voice.device_enum import DeviceEntry

logger = get_logger(__name__)


__all__ = [
    "RuntimeFailoverState",
    "_try_runtime_failover",
]


@dataclass
class RuntimeFailoverState:
    """Per-process failover counters owned by the factory closure scope.

    Mission §Phase 2 T2.6 introduced the dataclass; Mission C3 §T1.1
    extended it with ``ladder_id`` + ``ladder_exhausted`` +
    ``last_ladder_complete_monotonic`` for ladder-level observability
    + downstream consumer surfacing (T2.8 ``VoiceStatusDegraded``).

    The state instance is constructed alongside the deaf-signal
    closure in :func:`sovyx.voice.factory.create_voice_pipeline` and
    lives for the lifetime of the pipeline. It is NOT shared across
    pipelines (each ``create_voice_pipeline`` call gets its own
    counters), so a daemon hosting multiple minds in the future
    (Phase 8 of the master voice mission) can apply per-mind cooldown
    + attempt caps independently.

    Attributes:
        attempts: Monotonically increasing count of failover LADDER
            INVOCATIONS that dispatched at least one candidate.
            Bounded by :attr:`VoiceTuningConfig.max_failover_attempts`;
            once reached, the helper emits ``voice.failover.exhausted``
            and no-ops further calls. Mission C3 §T1.1 ADR-D2: this
            counts ladder runs, NOT individual candidates — the
            per-ladder candidate cap is the separate
            :attr:`VoiceTuningConfig.failover_candidate_max_attempts_per_ladder`
            knob.
        last_attempt_monotonic: ``time.monotonic()`` timestamp of the
            most recent ladder invocation that dispatched. The outer
            cooldown gate at
            :attr:`VoiceTuningConfig.failover_cooldown_s` reads this.
        exhausted_emitted: Idempotency flag for ``voice.failover.exhausted``
            — the helper may be called repeatedly after the attempts
            cap is hit (every subsequent deaf-signal cycle), but the
            terminal event must fire ONCE per process so dashboards
            don't see a flood of duplicates.
        ladder_id: Mission C3 §T1.3. uuid4 hex of the most recent
            ladder invocation. Empty string before the first ladder
            run. Surfaced on every per-candidate event so dashboards
            correlate a single ladder run's children.
        ladder_exhausted: Mission C3 §T2.8. True once the most recent
            ladder completed with ``verdict=exhausted``. Reset to
            False when a subsequent ladder succeeds. Surfaces in
            ``VoiceStatusDegraded.reason=failover_ladder_exhausted``
            (Phase 2 T2.8) so the dashboard renders an actionable
            banner.
        last_ladder_complete_monotonic: ``time.monotonic()`` timestamp
            of the most recent ladder completion (success or
            exhaustion). 0.0 before the first ladder run. Used by the
            VoiceStatusDegraded surface (T2.8) for "last attempt at"
            display.
        last_candidates_unreachable: List of canonical endpoint names
            that failed in the most recent ladder. Bounded by the
            per-ladder cap. Surfaces in
            ``VoiceStatusDegraded.candidates_unreachable``.
    """

    attempts: int = 0
    last_attempt_monotonic: float = 0.0
    exhausted_emitted: bool = field(default=False)
    ladder_id: str = ""
    ladder_exhausted: bool = field(default=False)
    last_ladder_complete_monotonic: float = 0.0
    last_candidates_unreachable: list[str] = field(default_factory=list)


async def _try_runtime_failover(
    *,
    capture_task: Any,  # noqa: ANN401 — duck-typed AudioCaptureTask + cyclic-import avoidance
    pipeline: Any,  # noqa: ANN401 — duck-typed VoicePipeline + cyclic-import avoidance
    tuning: VoiceTuningConfig,
    state: RuntimeFailoverState,
) -> None:
    """Hot-failover entry point invoked by the deaf-signal closure.

    Mission C3 §T1.1 — loop-in-place candidate iteration.

    Decision tree (in order):

    1. **Lenient telemetry first**: emit ``voice.failover.attempted``
       regardless of every other gate so dashboards see the decision
       context (current endpoint, candidates remaining, gate state,
       attempt index). This event is the calibration data the
       v0.31.0 default-flip was validated against.
    2. **Gate check**: if
       :attr:`VoiceTuningConfig.runtime_failover_on_quarantine_enabled`
       is ``False`` → return early. No mutation; the lenient event
       above is the only side effect.
    3. **Outer cooldown check**: if ``time.monotonic() -
       state.last_attempt_monotonic < tuning.failover_cooldown_s`` →
       emit ``voice.failover.cooldown_blocked`` → return. Gates
       against deaf-signal storms across CLOSURE invocations (not
       intra-ladder iteration; per Mission C3 §T1.1 ADR-D2).
    4. **Outer exhausted check (max attempts)**: if
       ``state.attempts >= tuning.max_failover_attempts`` → emit
       ``voice.failover.exhausted`` (idempotent via
       ``state.exhausted_emitted``) → return.
    5. **Pre-loop target resolution**: call :func:`_resolve_target_safe`
       once to surface the initial candidate to the lenient telemetry
       event. On exception → emit ``voice.failover.selection_failed``
       → return. On ``target is None`` (zero candidates at all) →
       emit ``voice.failover.exhausted`` (cause=no_candidate,
       idempotent) → return.
    6. **Enter ladder loop**: set ``pipeline._failover_ladder_in_progress
       = True`` (best-effort setattr — Phase 1 schema, Phase 2 frame-
       drop gate reader at T2.5). Emit ``voice.failover.ladder_started``
       with a fresh ``ladder_id`` (uuid4). Iterate up to
       ``tuning.failover_candidate_max_attempts_per_ladder`` candidates:

       a. Resolve next candidate via
          ``_resolve_target_safe(additional_excluded_guids=
          attempted_in_this_ladder)``. On ``None`` → break
          (ladder exhausted via candidate-set exhaustion). On
          exception → emit ``voice.failover.candidate_failed{
          verdict=selection_exception}`` → break.
       b. **Defensive guard**: if the resolved target's
          ``endpoint_guid`` (or ``canonical_name`` fallback) is
          already in ``attempted_in_this_ladder`` → break. This
          protects against (i) mock-based tests that ignore the
          exclusion-set parameter, (ii) any future bug where the
          resolver returns a non-distinct candidate.
       c. **Intra-ladder cooldown**: if this is NOT the first
          dispatch in the ladder, ``await asyncio.sleep(
          tuning.failover_intra_ladder_cooldown_s)``. Skipped on
          the first iteration so the ladder responds immediately
          to the deaf signal.
       d. **First-dispatch bookkeeping**: on the FIRST iteration
          that reaches the dispatch step, bump ``state.attempts`` and
          ``state.last_attempt_monotonic``. Subsequent iterations in
          the same ladder do NOT re-bump (preserves the
          ``state.attempts`` semantics: "ladder-runs that dispatched
          at least one candidate" — backward-compatible with the
          pre-Mission-C3 tests under
          ``tests/unit/voice/health/test_runtime_failover.py``).
       e. Emit ``voice.failover.candidate_attempted{index,
          candidate_count, target_endpoint, candidates_remaining,
          ladder_id, mind_id}``.
       f. Dispatch ``await capture_task.request_device_change_restart(
          target, reason='endpoint_quarantined')``.
       g. On exception → emit ``voice.failover.candidate_failed{
          verdict=exception, ...}`` → add target to
          ``attempted_in_this_ladder`` → continue. The closure MUST
          NOT propagate exceptions back to the heartbeat (would
          crash the pipeline state machine).
       h. On ``engaged=True`` → emit ``voice.failover.succeeded``
          (legacy preserved) → call
          ``pipeline.reset_coordinator_after_failover()`` (clears
          the deaf-detection latch so the new endpoint gets its own
          cycle) → emit ``voice.failover.ladder_complete{
          verdict=succeeded, succeeded_index=i, candidates_tried=
          i+1, elapsed_ms, ladder_id}`` → reset ``state.ladder_exhausted
          = False`` → break.
       i. On ``engaged=False`` → emit ``voice.failover.candidate_failed{
          index, verdict, error_class=unknown (Phase 1; classifier
          wired in Phase 2 §T2.4), ...}`` → add target to
          ``attempted_in_this_ladder`` → continue.
    7. **Loop-exit-without-success path**: emit
       ``voice.failover.failed`` (legacy preserved, carries metadata
       of the LAST attempted candidate — "this ladder run did not
       succeed") + ``voice.failover.ladder_complete{verdict=
       exhausted, candidates_tried, elapsed_ms, ladder_id}``. Set
       ``state.ladder_exhausted = True`` for downstream consumers
       (T2.8 ``VoiceStatusDegraded``).
    8. **try/finally**: clear ``pipeline._failover_ladder_in_progress
       = False`` regardless of how the ladder exited (success,
       exhaustion, exception). Prevents a panic inside the loop from
       leaving the flag stuck True (would suppress all future
       frame-drop emissions in Phase 2's T2.5 gate).

    Never raises — every step is wrapped in best-effort handling.
    Failure to fail over is observable via the structured events but
    must NOT break the deaf-signal closure (which is in turn called
    from the pipeline heartbeat path; an exception here would crash
    the heartbeat task and freeze the pipeline state machine).

    Args:
        capture_task: The :class:`AudioCaptureTask` instance. Accessed
            via duck typing for ``active_device_guid``,
            ``active_device_name``, and ``request_device_change_restart``;
            ``Any`` typing avoids the cyclic import that would otherwise
            arise (factory → capture_task → pipeline → factory).
        pipeline: The :class:`VoicePipeline` instance. Accessed via
            duck typing for ``reset_coordinator_after_failover`` and
            ``mind_id`` (read from ``_config`` for telemetry
            namespacing). Mission C3 §T1.1 adds
            ``_failover_ladder_in_progress`` write site via
            ``setattr`` (defensive: writes do not require the
            attribute to pre-exist; Phase 2 readers use
            ``getattr(..., False)``).
        tuning: The factory's :class:`VoiceTuningConfig` snapshot.
        state: Per-process :class:`RuntimeFailoverState` instance —
            mutated in place (attempt counter, cooldown timestamp,
            exhausted flag, ladder_id, ladder_exhausted,
            last_ladder_complete_monotonic, last_candidates_unreachable).
    """
    from_endpoint, from_friendly_name = _snapshot_current_endpoint(capture_task)

    # Mission C1 §T2.1 + §T2.1.b — verdict-driven derived_reason for the
    # quarantine entry that triggered this failover. Falls back to the
    # legacy ``reason`` field for pre-mission entries (LENIENT v0.44.x
    # cycle). Surfaces on every telemetry event so dashboards can split
    # failover triggers by reason class (apo_degraded / vad_frontend_dead
    # / format_mismatch / driver_silent) without grepping per-event
    # context.
    legacy_reason, derived_reason = _snapshot_quarantine_reasons(from_endpoint)

    # Step 1 — pre-loop resolve so the lenient telemetry shows the
    # initial would-be target. If selection fails or returns None, we
    # still emit the lenient event with empty target fields so
    # dashboards see the context.
    target, candidates_remaining, selection_error = _resolve_target_safe(
        capture_task=capture_task,
    )

    mind_id = _safe_mind_id(pipeline)
    target_endpoint = target.canonical_name if target is not None else ""
    target_friendly = target.name if target is not None else ""

    # Lenient telemetry — fires regardless of gate or cooldown so
    # dashboards have a continuous calibration signal.
    logger.warning(
        "voice.failover.attempted",
        **{
            "voice.from_endpoint": from_endpoint,
            "voice.from_friendly_name": from_friendly_name,
            "voice.to_endpoint": target_endpoint,
            "voice.to_friendly_name": target_friendly,
            "voice.reason": "endpoint_quarantined",
            "voice.legacy_reason": legacy_reason,
            "voice.derived_reason": derived_reason,
            "voice.candidates_remaining": candidates_remaining,
            "voice.gate_enabled": tuning.runtime_failover_on_quarantine_enabled,
            "voice.attempt_index": state.attempts,
            "voice.max_failover_attempts": tuning.max_failover_attempts,
            "voice.mind_id": mind_id,
        },
    )

    if selection_error is not None:
        logger.error(
            "voice.failover.selection_failed",
            **{
                "voice.error": str(selection_error),
                "voice.error_type": type(selection_error).__name__,
                "voice.mind_id": mind_id,
            },
        )
        return

    # Step 2 — gate check.
    if not tuning.runtime_failover_on_quarantine_enabled:
        return

    # Step 3 — outer cooldown check (per Mission C3 §T1.1 ADR-D2 this
    # is the inter-INVOCATION gate, not the intra-ladder one). The
    # cooldown protects against deaf-signal heartbeat storms — if the
    # last ladder ran < failover_cooldown_s ago, the new closure
    # invocation defers to the existing in-flight or just-completed
    # ladder rather than firing a fresh ladder for the same condition.
    now = time.monotonic()
    cooldown_remaining = tuning.failover_cooldown_s - (now - state.last_attempt_monotonic)
    if state.last_attempt_monotonic > 0.0 and cooldown_remaining > 0.0:
        logger.info(
            "voice.failover.cooldown_blocked",
            **{
                "voice.from_endpoint": from_endpoint,
                "voice.cooldown_remaining_s": cooldown_remaining,
                "voice.last_attempt_monotonic": state.last_attempt_monotonic,
                "voice.mind_id": mind_id,
            },
        )
        return

    # Step 4 — outer exhausted check (max ladder invocations).
    if state.attempts >= tuning.max_failover_attempts:
        if not state.exhausted_emitted:
            logger.error(
                "voice.failover.exhausted",
                **{
                    "voice.last_endpoint": from_endpoint,
                    "voice.attempts": state.attempts,
                    "voice.max_failover_attempts": tuning.max_failover_attempts,
                    "voice.cause": "max_attempts",
                    "voice.action_required": (
                        "manual operator intervention — fix the underlying "
                        "audio environment (mixer state, default-source routing, "
                        "or hardware) and restart the daemon"
                    ),
                    "voice.mind_id": mind_id,
                },
            )
            state.exhausted_emitted = True
        return

    # Step 5 — no-candidate-at-all path (idempotent).
    if target is None:
        if not state.exhausted_emitted:
            logger.error(
                "voice.failover.exhausted",
                **{
                    "voice.last_endpoint": from_endpoint,
                    "voice.attempts": state.attempts,
                    "voice.max_failover_attempts": tuning.max_failover_attempts,
                    "voice.cause": "no_candidate",
                    "voice.action_required": (
                        "every input device on this host is quarantined or "
                        "unreachable — fix the underlying environment and "
                        "restart the daemon"
                    ),
                    "voice.mind_id": mind_id,
                },
            )
            state.exhausted_emitted = True
        return

    # Step 6 — ladder loop. Mission C3 §T1.1 ADR-D1.
    ladder_id = uuid.uuid4().hex[:12]
    state.ladder_id = ladder_id
    ladder_started_monotonic = time.monotonic()
    attempted_in_this_ladder: set[str] = set()
    candidates_unreachable: list[str] = []
    succeeded_index: int | None = None
    last_attempted_target_endpoint = target_endpoint
    last_attempted_target_friendly = target_friendly
    last_failure_verdict = ""
    last_failure_detail = ""
    last_failure_error_type = ""
    first_dispatch_done = False
    candidates_tried = 0

    _safe_set_ladder_in_progress(pipeline, value=True)

    logger.info(
        "voice.failover.ladder_started",
        **{
            "voice.ladder_id": ladder_id,
            "voice.from_endpoint": from_endpoint,
            "voice.initial_target_endpoint": target_endpoint,
            "voice.candidate_count_estimate": max(candidates_remaining, 1),
            "voice.max_candidates_per_ladder": tuning.failover_candidate_max_attempts_per_ladder,
            "voice.mind_id": mind_id,
        },
    )

    # Mission C3 §T2.4 — process-local probe-result cache. Resolved
    # once per ladder run; consulted before each candidate dispatch
    # for skip-on-bad-probe + populated after each dispatch outcome.
    probe_cache = get_default_probe_result_cache()

    try:
        per_ladder_cap = max(1, tuning.failover_candidate_max_attempts_per_ladder)
        current_target: DeviceEntry | None = target
        iteration_index = 0
        while iteration_index < per_ladder_cap:
            # On iterations >0, re-resolve via the exclusion set. On
            # iteration 0 we use the pre-loop ``target`` from step 5.
            if iteration_index > 0:
                excluded_snapshot = frozenset(attempted_in_this_ladder)
                next_target, next_remaining, next_err = _resolve_target_safe(
                    capture_task=capture_task,
                    additional_excluded_guids=excluded_snapshot,
                )
                if next_err is not None:
                    logger.warning(
                        "voice.failover.candidate_failed",
                        **{
                            "voice.ladder_id": ladder_id,
                            "voice.index": iteration_index,
                            "voice.target_endpoint": "",
                            "voice.verdict": "selection_exception",
                            "voice.error_class": FailoverErrorClass.UNKNOWN.value,
                            "voice.error_detail": str(next_err),
                            "voice.error_type": type(next_err).__name__,
                            "voice.mind_id": mind_id,
                        },
                    )
                    break
                if next_target is None:
                    break
                current_target = next_target
                candidates_remaining = next_remaining

            assert current_target is not None  # ladder body never enters with None
            # Defensive guard: protect against resolver returning a
            # candidate the loop already attempted (mock-based tests
            # + any future resolver bug).
            target_key = current_target.canonical_name or current_target.name or ""
            if target_key and target_key in attempted_in_this_ladder:
                break

            # Mission C3 §T2.4 — probe-result cache short-circuit.
            # Consult the cache by both the endpoint_guid (current_target's
            # canonical_name, used as the cache key by the boot cascade
            # producer) and the host_api. On cache hit, skip the candidate
            # WITHOUT dispatching — emit ``voice.failover.candidate_skipped``
            # so dashboards see the decision, mark the candidate in the
            # per-ladder exclusion set, advance.
            cache_host_api = current_target.host_api_name or ""
            cache_entry_for_skip = probe_cache.lookup(target_key, cache_host_api)
            if cache_entry_for_skip is not None and probe_cache.is_known_unopenable(
                target_key,
                cache_host_api,
            ):
                logger.info(
                    "voice.failover.candidate_skipped",
                    **{
                        "voice.ladder_id": ladder_id,
                        "voice.index": iteration_index,
                        "voice.target_endpoint": target_key,
                        "voice.target_friendly_name": current_target.name,
                        "voice.cached_verdict": cache_entry_for_skip.verdict,
                        "voice.cached_error_code": cache_entry_for_skip.error_code,
                        "voice.reason": "probe_cache_unopenable",
                        "voice.mind_id": mind_id,
                    },
                )
                if target_key:
                    attempted_in_this_ladder.add(target_key)
                    candidates_unreachable.append(target_key)
                iteration_index += 1
                continue

            # Intra-ladder cooldown between dispatches (Mission C3
            # §T1.1 ADR-D2). Sleep only between dispatches — skipped
            # on first iteration so we respond immediately to the
            # deaf signal.
            if first_dispatch_done and tuning.failover_intra_ladder_cooldown_s > 0.0:
                await asyncio.sleep(tuning.failover_intra_ladder_cooldown_s)

            # First-dispatch bookkeeping. Bump ``state.attempts`` and
            # the cooldown timestamp exactly once per ladder run that
            # reaches the dispatch step. Preserves the pre-Mission-C3
            # semantics ("ladder-runs that dispatched", per ADR-D2).
            if not first_dispatch_done:
                state.attempts += 1
                state.last_attempt_monotonic = time.monotonic()
                first_dispatch_done = True

            attempt_started = time.monotonic()
            current_endpoint = current_target.canonical_name
            current_friendly = current_target.name
            last_attempted_target_endpoint = current_endpoint
            last_attempted_target_friendly = current_friendly

            logger.info(
                "voice.failover.candidate_attempted",
                **{
                    "voice.ladder_id": ladder_id,
                    "voice.index": iteration_index,
                    "voice.candidate_count": iteration_index + 1,
                    "voice.target_endpoint": current_endpoint,
                    "voice.target_friendly_name": current_friendly,
                    "voice.candidates_remaining": candidates_remaining,
                    "voice.derived_reason": derived_reason,
                    "voice.mind_id": mind_id,
                },
            )

            try:
                result = await capture_task.request_device_change_restart(
                    current_target,
                    reason="endpoint_quarantined",
                )
            except Exception as exc:  # noqa: BLE001 — runtime-failover must not crash heartbeat
                elapsed_ms = int((time.monotonic() - attempt_started) * 1000)
                last_failure_verdict = "exception"
                last_failure_detail = str(exc)
                last_failure_error_type = type(exc).__name__
                exception_error_class = classify_error_code(
                    "",
                    error_detail=str(exc),
                )
                logger.warning(
                    "voice.failover.candidate_failed",
                    **{
                        "voice.ladder_id": ladder_id,
                        "voice.index": iteration_index,
                        "voice.target_endpoint": current_endpoint,
                        "voice.verdict": "exception",
                        "voice.error_class": exception_error_class.value,
                        "voice.error_detail": str(exc),
                        "voice.error_type": type(exc).__name__,
                        "voice.elapsed_ms": elapsed_ms,
                        "voice.mind_id": mind_id,
                    },
                )
                # Mission C3 §T2.4 — record the failed dispatch into
                # the probe-result cache so subsequent ladder runs
                # consult it. Best-effort: a panic here MUST NOT
                # break the heartbeat path.
                try:
                    probe_cache.record_probe(
                        ProbeResultEntry(
                            endpoint_guid=target_key,
                            host_api=cache_host_api,
                            verdict="exception",
                            error_code="",
                            error_detail=str(exc),
                            callbacks_fired=0,
                        ),
                    )
                except Exception as cache_exc:  # noqa: BLE001
                    logger.debug(
                        "voice.failover.cache_record_failed",
                        endpoint=target_key,
                        error=str(cache_exc),
                        error_type=type(cache_exc).__name__,
                    )
                if target_key:
                    attempted_in_this_ladder.add(target_key)
                    candidates_unreachable.append(target_key)
                candidates_tried += 1
                iteration_index += 1
                continue

            elapsed_ms = int((time.monotonic() - attempt_started) * 1000)
            candidates_tried += 1

            if getattr(result, "engaged", False):
                # Success — emit legacy succeeded event, reset
                # coordinator latch, emit ladder_complete, break.
                logger.warning(
                    "voice.failover.succeeded",
                    **{
                        "voice.from_endpoint": from_endpoint,
                        "voice.to_endpoint": current_endpoint,
                        "voice.to_friendly_name": current_friendly,
                        "voice.attempt_index": state.attempts,
                        "voice.elapsed_ms": elapsed_ms,
                        "voice.new_endpoint_guid": getattr(result, "new_endpoint_guid", ""),
                        "voice.candidate_index_in_ladder": iteration_index,
                        "voice.ladder_id": ladder_id,
                        "voice.mind_id": mind_id,
                    },
                )

                # Reset coordinator latch so the new endpoint gets
                # its own deaf-detection cycle. Without this the
                # pipeline stays latched at "coordinator terminated"
                # on the OLD endpoint state and abandons the new one
                # silently. Best-effort: a panic here does not
                # invalidate the success — we still log + complete
                # the ladder.
                try:
                    pipeline.reset_coordinator_after_failover()
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        "voice.failover.coordinator_reset_failed",
                        **{
                            "voice.error": str(exc),
                            "voice.error_type": type(exc).__name__,
                            "voice.ladder_id": ladder_id,
                            "voice.mind_id": mind_id,
                        },
                    )

                # Mission C3 §T2.4 ADR-D5 — invalidate any prior dead-
                # entry for this candidate so a re-plugged USB / restarted
                # PipeWire that becomes available is not stuck in skip-
                # state on the next ladder run. Best-effort.
                try:
                    probe_cache.record_success(target_key, cache_host_api)
                except Exception as cache_exc:  # noqa: BLE001
                    logger.debug(
                        "voice.failover.cache_record_success_failed",
                        endpoint=target_key,
                        error=str(cache_exc),
                        error_type=type(cache_exc).__name__,
                    )

                succeeded_index = iteration_index
                state.ladder_exhausted = False
                state.last_ladder_complete_monotonic = time.monotonic()
                state.last_candidates_unreachable = list(candidates_unreachable)
                # Mission C3 §T2.7 — clear the pipeline-side
                # ladder-exhausted flag so the heartbeat throttle
                # releases. Best-effort setattr.
                _safe_set_pipeline_attr(pipeline, "_failover_ladder_exhausted", False)
                _safe_set_pipeline_attr(
                    pipeline,
                    "_last_terminal_deaf_warn_monotonic",
                    0.0,
                )
                # Mission C3 §T2.8 — mirror state onto pipeline so the
                # dashboard's voice_status helper can read it without
                # plumbing the RuntimeFailoverState across the registry.
                _safe_set_pipeline_attr(
                    pipeline,
                    "_failover_last_candidates_unreachable",
                    list(candidates_unreachable),
                )
                _safe_set_pipeline_attr(
                    pipeline,
                    "_failover_last_ladder_complete_monotonic",
                    state.last_ladder_complete_monotonic,
                )

                # Mission C3 §T2.5 — emit the frame-loss summary BEFORE
                # the ladder_complete event so dashboards see the
                # window aggregate paired with the ladder verdict.
                _emit_frame_loss_window_summary(
                    pipeline=pipeline,
                    ladder_id=ladder_id,
                    candidates_tried=candidates_tried,
                    succeeded_index=succeeded_index,
                    mind_id=mind_id,
                )

                logger.info(
                    "voice.failover.ladder_complete",
                    **{
                        "voice.ladder_id": ladder_id,
                        "voice.verdict": "succeeded",
                        "voice.succeeded_index": succeeded_index,
                        "voice.candidates_tried": candidates_tried,
                        "voice.elapsed_ms": int(
                            (time.monotonic() - ladder_started_monotonic) * 1000,
                        ),
                        "voice.mind_id": mind_id,
                    },
                )
                return

            # Failed — emit per-candidate failure, record into cache, advance.
            last_failure_verdict = str(getattr(result, "verdict", "unknown") or "unknown")
            last_failure_detail = str(getattr(result, "detail", "") or "")
            last_failure_error_type = ""
            error_code_raw = str(
                getattr(result, "error_code", "") or getattr(result, "final_code", "") or "",
            )
            # Mission C3 §T2.4 — classify the open verdict + error code
            # via the failover-layer classifier; surfaces on every
            # per-candidate failure event so dashboards split by class.
            error_class = classify_error_code(error_code_raw, last_failure_detail)

            logger.warning(
                "voice.failover.candidate_failed",
                **{
                    "voice.ladder_id": ladder_id,
                    "voice.index": iteration_index,
                    "voice.target_endpoint": current_endpoint,
                    "voice.verdict": last_failure_verdict,
                    "voice.error_class": error_class.value,
                    "voice.error_code": error_code_raw,
                    "voice.error_detail": last_failure_detail,
                    "voice.elapsed_ms": elapsed_ms,
                    "voice.mind_id": mind_id,
                },
            )

            # Mission C3 §T2.4 — record the failure into the cache so
            # subsequent ladder runs (after the outer cooldown
            # releases) skip this candidate if it's UNOPENABLE_*. Best-
            # effort.
            try:
                probe_cache.record_probe(
                    ProbeResultEntry(
                        endpoint_guid=target_key,
                        host_api=cache_host_api,
                        verdict=last_failure_verdict,
                        error_code=error_code_raw,
                        error_detail=last_failure_detail,
                        callbacks_fired=0,
                    ),
                )
            except Exception as cache_exc:  # noqa: BLE001
                logger.debug(
                    "voice.failover.cache_record_failed",
                    endpoint=target_key,
                    error=str(cache_exc),
                    error_type=type(cache_exc).__name__,
                )

            if target_key:
                attempted_in_this_ladder.add(target_key)
                candidates_unreachable.append(target_key)
            iteration_index += 1
        # End while loop.

        # Loop-exit-without-success path. Emit legacy ``voice.failover.failed``
        # (preserved per Mission C3 §T1.3 — additive policy, no legacy
        # event removed) plus the new ``voice.failover.ladder_complete``.
        logger.error(
            "voice.failover.failed",
            **{
                "voice.from_endpoint": from_endpoint,
                "voice.target_endpoint": last_attempted_target_endpoint,
                "voice.to_friendly_name": last_attempted_target_friendly,
                "voice.verdict": last_failure_verdict or "unknown",
                "voice.detail": last_failure_detail,
                "voice.error_type": last_failure_error_type,
                "voice.attempt_index": state.attempts,
                "voice.candidates_tried": candidates_tried,
                "voice.ladder_id": ladder_id,
                "voice.mind_id": mind_id,
            },
        )

        state.ladder_exhausted = True
        state.last_ladder_complete_monotonic = time.monotonic()
        state.last_candidates_unreachable = list(candidates_unreachable)
        # Mission C3 §T2.7 — set the pipeline-side ladder-exhausted
        # flag so the heartbeat-mixin throttle engages on subsequent
        # deaf-warning emissions. Best-effort setattr.
        _safe_set_pipeline_attr(pipeline, "_failover_ladder_exhausted", True)
        # Mission C3 §T2.8 — mirror exhausted-path state for the
        # dashboard's voice_status surface.
        _safe_set_pipeline_attr(
            pipeline,
            "_failover_last_candidates_unreachable",
            list(candidates_unreachable),
        )
        _safe_set_pipeline_attr(
            pipeline,
            "_failover_last_ladder_complete_monotonic",
            state.last_ladder_complete_monotonic,
        )

        # Mission C3 §T2.5 — frame-loss summary for the exhausted path.
        _emit_frame_loss_window_summary(
            pipeline=pipeline,
            ladder_id=ladder_id,
            candidates_tried=candidates_tried,
            succeeded_index=None,
            mind_id=mind_id,
        )

        logger.info(
            "voice.failover.ladder_complete",
            **{
                "voice.ladder_id": ladder_id,
                "voice.verdict": "exhausted",
                "voice.succeeded_index": None,
                "voice.candidates_tried": candidates_tried,
                "voice.elapsed_ms": int(
                    (time.monotonic() - ladder_started_monotonic) * 1000,
                ),
                "voice.mind_id": mind_id,
            },
        )
    finally:
        _safe_set_ladder_in_progress(pipeline, value=False)


def _safe_set_pipeline_attr(pipeline: Any, name: str, value: object) -> None:  # noqa: ANN401
    """Best-effort ``setattr`` for ladder-related pipeline flags.

    Mission C3 §T2.5 + §T2.7 — wraps the setattr in try/except so an
    unusual pipeline that does not accept arbitrary attribute writes
    cannot crash the failover closure. Used by both the success-path
    (clear ``_failover_ladder_exhausted`` + reset terminal-warn
    timestamp) and the exhausted-path (set
    ``_failover_ladder_exhausted=True`` so the heartbeat throttle
    engages).
    """
    try:
        setattr(pipeline, name, value)
    except Exception as exc:  # noqa: BLE001 — observability hygiene only
        logger.debug(
            "voice.failover.pipeline_setattr_failed",
            pipeline_type=type(pipeline).__name__,
            attr=name,
            value=value,
            error=str(exc),
            error_type=type(exc).__name__,
        )


def _safe_set_ladder_in_progress(pipeline: Any, *, value: bool) -> None:  # noqa: ANN401
    """Best-effort ``setattr`` for the ladder-in-progress flag.

    Mission C3 §T1.1 step 8 — wrapped in try/except so a pipeline
    that does not accept arbitrary attribute writes (highly unusual)
    cannot crash the failover closure. Mission C3 §T2.5 reads this
    flag via ``getattr(pipeline, '_failover_ladder_in_progress',
    False)`` in the orchestrator's frame-drop emit site.

    Also (Mission C3 §T2.5) initializes
    ``pipeline._frame_loss_during_ladder = []`` on ladder entry so
    the orchestrator's drop-detector accumulator has a stable list
    to append into. On ladder exit, the list is preserved (the
    ``voice.failover.frame_loss_window`` summary emit reads it just
    before clearing the in-progress flag).
    """
    try:
        pipeline._failover_ladder_in_progress = value  # noqa: SLF001
        if value:
            # Fresh per-ladder accumulator.
            pipeline._frame_loss_during_ladder = []  # noqa: SLF001
    except Exception as exc:  # noqa: BLE001 — observability hygiene only
        logger.debug(
            "voice.failover.ladder_flag_setattr_failed",
            pipeline_type=type(pipeline).__name__,
            value=value,
            error=str(exc),
            error_type=type(exc).__name__,
        )


def _emit_frame_loss_window_summary(
    *,
    pipeline: Any,  # noqa: ANN401
    ladder_id: str,
    candidates_tried: int,
    succeeded_index: int | None,
    mind_id: str,
) -> None:
    """Emit ``voice.failover.frame_loss_window`` summary if drops occurred.

    Mission C3 §T2.5 — collapses the per-frame drops accumulated during
    the ladder iteration into a SINGLE structured summary event with
    ``total_gap_ms, frames_dropped, candidate_count``. Fires exactly
    once per ladder run that had ≥ 1 drop; if no drops, no event
    (NOT a zero-drop summary — observability hygiene).

    Best-effort: any attribute access or read failure is silently
    suppressed via ``contextlib.suppress`` so the load-bearing
    ladder-complete emit path is unaffected.
    """
    import contextlib

    with contextlib.suppress(Exception):
        window: list[tuple[float, float]] | None = getattr(
            pipeline,
            "_frame_loss_during_ladder",
            None,
        )
        if not window:
            return
        total_gap_ms = sum(gap_s for gap_s, _ in window) * 1000.0
        logger.warning(
            "voice.failover.frame_loss_window",
            **{
                "voice.ladder_id": ladder_id,
                "voice.duration_ms": round(total_gap_ms, 1),
                "voice.frames_dropped": len(window),
                "voice.candidate_count": candidates_tried,
                "voice.succeeded_candidate_index": succeeded_index,
                "voice.mind_id": mind_id,
            },
        )
        # Clear the accumulator so the next ladder run starts fresh
        # (also handled by _safe_set_ladder_in_progress on next entry
        # — belt-and-suspenders).
        pipeline._frame_loss_during_ladder = []  # noqa: SLF001


def _snapshot_current_endpoint(capture_task: Any) -> tuple[str, str]:  # noqa: ANN401
    """Return ``(canonical_endpoint, friendly_name)`` for the active task.

    Best-effort — empty strings on any attribute access failure so the
    failover helper continues even if the capture task is in a half-
    initialised state.
    """
    try:
        guid = getattr(capture_task, "active_device_guid", "") or ""
        name = getattr(capture_task, "active_device_name", "") or ""
    except Exception:  # noqa: BLE001
        return "", ""
    return str(guid), str(name)


def _snapshot_quarantine_reasons(endpoint_guid: str) -> tuple[str, str]:
    """Mission C1 §T2.1 — return ``(legacy_reason, derived_reason)``.

    Reads the live :class:`EndpointQuarantine` entry for ``endpoint_guid``
    and surfaces both reason fields so :func:`_try_runtime_failover`
    telemetry can split by class. Empty strings on any lookup failure
    (entry expired between quarantine and failover dispatch, or
    quarantine store transient error). Best-effort — failure to read
    the reason MUST NOT block the failover decision.

    For pre-mission quarantine entries (no ``derived_reason`` set),
    ``derived_reason`` mirrors ``legacy_reason`` so dashboards can
    treat the two fields uniformly during the LENIENT v0.44.x cycle.
    """
    if not endpoint_guid:
        return "", ""
    try:
        from sovyx.voice.health._quarantine import get_default_quarantine

        entry = get_default_quarantine().get(endpoint_guid)
    except Exception as exc:  # noqa: BLE001 — telemetry lookup is best-effort
        logger.debug(
            "voice.failover.quarantine_reason_lookup_failed",
            endpoint=endpoint_guid,
            error=str(exc),
            error_type=type(exc).__name__,
        )
        return "", ""
    if entry is None:
        return "", ""
    legacy = entry.reason or ""
    derived = entry.derived_reason or legacy
    return legacy, derived


def _safe_mind_id(pipeline: Any) -> str:  # noqa: ANN401
    """Read the per-turn authoritative mind id with best-effort fallback.

    v0.32.5 Phase 4.D Finding 2 closure: anti-pattern #35 reincidence
    sibling. Pre-fix this read ``pipeline._config.mind_id`` directly.
    Phase 3.A Layer C (commit ``c9487639``) explicitly fixed the
    perception-dispatch site at ``_orchestrator.py:2199`` from
    ``_config.mind_id`` to ``_current_mind_id`` because the orchestrator
    owns a per-turn authoritative ``_current_mind_id`` (set at
    ``_orchestrator.py:1740`` via the wake-word router match) and the
    configured-at-startup ``_config.mind_id`` is the sentinel-default
    case anti-pattern #35 warns about.

    Failover events emitted during a router-matched non-default mind's
    turn (``voice.failover.attempted`` / ``voice.failover.selection_failed``)
    were misattributed to the configured-at-startup mind. This site is
    a sibling of the cluster Phase 3.A closed; the deeper sweep at
    Phase 4.D PHASE-4-D-AUDIT.md Finding 2 surfaced it.

    Resolution order:
      1. ``pipeline._current_mind_id`` — per-turn authoritative.
      2. ``pipeline._config.mind_id`` — configured-at-startup fallback.
      3. ``"default"`` — final sentinel.
    """
    try:
        current = getattr(pipeline, "_current_mind_id", None)
        if isinstance(current, str) and current:
            return current
        config = getattr(pipeline, "_config", None)
        if config is not None:
            return str(getattr(config, "mind_id", "default"))
    except Exception as exc:  # noqa: BLE001
        # v0.32.6 Phase 5.B — anti-pattern #27 conversion. Reading
        # private attrs is best-effort defensive (the pipeline contract
        # may evolve); the fallback to ``"default"`` keeps the call
        # site safe but the debug log gives visibility into recurring
        # private-attribute access failures during dev.
        logger.debug(
            "voice.failover.safe_mind_id_failed",
            pipeline_type=type(pipeline).__name__,
            error=str(exc),
            error_type=type(exc).__name__,
        )
    return "default"


def _resolve_target_safe(
    *,
    capture_task: Any,  # noqa: ANN401
    additional_excluded_guids: frozenset[str] = frozenset(),
) -> tuple[DeviceEntry | None, int, Exception | None]:
    """Wrap :func:`select_alternative_endpoint` in a try/except.

    Mission C3 §T1.4 — extended with ``additional_excluded_guids`` so
    the ladder loop can thread its per-ladder exclusion set
    (``attempted_in_this_ladder``) into the selector without mutating
    the global quarantine state. Backward-compatible: callers that
    omit the parameter (e.g. pre-Mission-C3 tests, the lenient
    pre-loop step 1 resolve) get the unchanged single-shot semantics.

    Returns ``(target, candidates_remaining, selection_error)``:

    * ``target`` is ``None`` when no non-quarantined non-attempted
      input device exists OR when the helper raised.
    * ``candidates_remaining`` is the total non-excluded input device
      count (best-effort). Surfaced in ``voice.failover.attempted``
      so dashboards can compute "how close to exhaustion" without
      a separate query.
    * ``selection_error`` carries the exception when the helper raised;
      used by the caller to emit ``voice.failover.selection_failed``
      AFTER the lenient telemetry event.

    Args:
        capture_task: The capture-task instance — read for the
            currently-active GUID + physical name so they're never
            re-picked. Duck-typed to avoid cyclic imports.
        additional_excluded_guids: Mission C3 §T1.4 — extra GUIDs (or
            canonical names) to add to the exclusion set on top of
            the current-endpoint + quarantine state. Used by the
            ladder loop to thread its per-iteration ``attempted_in_this_ladder``
            tracker. Default empty frozenset preserves pre-mission
            single-shot behaviour.
    """
    from sovyx.voice.health._factory_integration import select_alternative_endpoint
    from sovyx.voice.health._quarantine import get_default_quarantine

    quarantine = get_default_quarantine()
    current_guid = getattr(capture_task, "active_device_guid", "") or ""
    excluded_guids: tuple[str, ...] = tuple(
        guid for guid in (current_guid, *additional_excluded_guids) if guid
    )

    # Best-effort canonical-name exclusion via the resolver helper.
    excluded_physical_set: set[str] = set()
    try:
        from sovyx.voice.capture._helpers import _resolve_input_entry

        entry = _resolve_input_entry(
            input_device=getattr(capture_task, "_input_device", None),
            enumerate_fn=None,
            host_api_name=getattr(capture_task, "_host_api_name", None),
        )
        if entry is not None and entry.canonical_name:
            excluded_physical_set.add(entry.canonical_name)
    except Exception as exc:  # noqa: BLE001 — physical exclusion is best-effort only
        # v0.32.6 Phase 5.B — anti-pattern #27 conversion. Without the
        # physical exclusion the failover may try to re-open the same
        # endpoint that just failed (one extra ineligible candidate);
        # not catastrophic but the debug log helps diagnose if this
        # path becomes the dominant cause of a failover loop.
        logger.debug(
            "voice.failover.physical_exclusion_failed",
            error=str(exc),
            error_type=type(exc).__name__,
        )
    # Mission C3 §T1.4 — also exclude any per-ladder canonical names
    # the ladder has already attempted. The selector's
    # ``exclude_physical_device_ids`` set matches against
    # ``DeviceEntry.canonical_name``, so the ladder's
    # ``attempted_in_this_ladder`` (which we key by canonical_name OR
    # name) feeds in here too. Belt-and-suspenders with the GUID set.
    for extra in additional_excluded_guids:
        if extra:
            excluded_physical_set.add(extra)

    excluded_physical: tuple[str, ...] = tuple(excluded_physical_set)

    try:
        target = select_alternative_endpoint(
            kind="input",
            exclude_endpoint_guids=excluded_guids,
            exclude_physical_device_ids=excluded_physical,
            quarantine=quarantine,
        )
    except Exception as exc:  # noqa: BLE001
        return None, 0, exc

    # Best-effort candidates_remaining count — re-enumerate and
    # subtract excluded + quarantined. Failure to count is non-fatal;
    # we surface 0 as "unknown" rather than abort the failover.
    candidates_remaining = 0
    try:
        from sovyx.voice.device_enum import enumerate_devices

        all_inputs = [entry for entry in enumerate_devices() if entry.max_input_channels > 0]
        candidates_remaining = sum(
            1
            for entry in all_inputs
            if entry.canonical_name not in excluded_physical
            and not quarantine.is_quarantined(entry.canonical_name)
        )
    except Exception as exc:  # noqa: BLE001
        # v0.32.6 Phase 5.B — anti-pattern #27 conversion. The
        # candidates-remaining count drives the dashboard's "next
        # restart will exhaust the chain" badge; failure here leaves
        # the count at its initial 0 (under-reports), which is the
        # safer default. Log for diagnostic visibility.
        logger.debug(
            "voice.failover.candidates_remaining_count_failed",
            error=str(exc),
            error_type=type(exc).__name__,
        )

    return target, candidates_remaining, None
