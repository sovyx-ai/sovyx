"""Runtime hot-failover after endpoint quarantine.

Mission anchor:
``docs-internal/missions/MISSION-voice-linux-silent-mic-remediation-2026-05-04.md``
§Phase 2 T2.6.

Pre-T2.6 the deaf-signal coordinator at
:mod:`sovyx.voice.pipeline._orchestrator._maybe_trigger_bypass_coordinator`
would (1) probe integrity → (2) try every eligible bypass strategy →
(3) on `not_applicable` everywhere, log ``voice_apo_bypass_ineffective``
and quarantine the endpoint. The pipeline then kept emitting on the
quarantined endpoint until the next process boot — the hint message
admitted the lacuna explicitly: *"factory will fail over to an
alternate capture device on next boot"*.

T2.6 closes the lacuna. The factory's ``_on_deaf_signal`` closure now
calls :func:`_try_runtime_failover` after the coordinator returns
ineffective outcomes; the helper resolves the next non-quarantined
boot candidate via
:func:`sovyx.voice.health._factory_integration.select_alternative_endpoint`
and asks
:meth:`sovyx.voice._capture_task.AudioCaptureTask.request_device_change_restart`
to rebind the stream in-process.

Staged adoption (per ``feedback_staged_adoption``):

* **Lenient telemetry mode (always on):** :func:`_try_runtime_failover`
  emits ``voice.failover.attempted`` regardless of the gate so
  dashboards can calibrate the false-positive rate against real
  production deaf-signal events. The event carries
  ``voice.gate_enabled`` so downstream consumers can split "would
  have happened" from "actually happened".
* **Behavioural mode (gated):** the actual
  ``request_device_change_restart`` dispatch + coordinator reset
  fire only when
  :attr:`sovyx.engine.config.VoiceTuningConfig.runtime_failover_on_quarantine_enabled`
  is ``True`` (default ``False`` in v0.30.10; flipped to ``True`` in
  v0.31.0 after one minor cycle of telemetry validation).

Forensic anchor: ``c:\\Users\\guipe\\Downloads\\logs_01.txt`` lines
997-1003 (the Sony VAIO + Mint 22 + PipeWire silent-mic case from
2026-05-04). The user's daemon hit the exact path this helper closes:
3 strategies returned ``not_applicable``, endpoint was quarantined
for 1 h, and the pipeline stayed deaf for the remaining 90 s of the
session. With T2.6 in place + the gate flipped, the helper would
have dispatched a device-change restart against rank-1 candidate
(device 4, the SN6180 hw:1,0 mic — though it would have failed with
``PaErrorCode -9985`` per logs line 815) → cooldown → rank-2
candidate (device 6, the PipeWire virtual passthrough) → either a
healthy stream or an exhausted-failover terminal state, all
observable in the dashboard's restart-history widget instead of
silent doom.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from sovyx.observability.logging import get_logger

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

    Mission §Phase 2 T2.6. The state instance is constructed alongside
    the deaf-signal closure in :func:`sovyx.voice.factory.create_voice_pipeline`
    and lives for the lifetime of the pipeline. It is NOT shared
    across pipelines (each ``create_voice_pipeline`` call gets its own
    counters), so a daemon hosting multiple minds in the future
    (Phase 8 of the master voice mission) can apply per-mind cooldown
    + attempt caps independently.

    Attributes:
        attempts: Monotonically increasing count of failover attempts
            in this process. Bounded by
            :attr:`VoiceTuningConfig.max_failover_attempts`; once
            reached, the helper emits ``voice.failover.exhausted`` and
            no-ops further calls.
        last_attempt_monotonic: ``time.monotonic()`` timestamp of the
            most recent attempt. The cooldown gate at
            :attr:`VoiceTuningConfig.failover_cooldown_s` reads this.
        exhausted_emitted: Idempotency flag for ``voice.failover.exhausted``
            — the helper may be called repeatedly after the attempts
            cap is hit (every subsequent deaf-signal cycle), but the
            terminal event must fire ONCE per process so dashboards
            don't see a flood of duplicates.
    """

    attempts: int = 0
    last_attempt_monotonic: float = 0.0
    exhausted_emitted: bool = field(default=False)


async def _try_runtime_failover(
    *,
    capture_task: Any,  # noqa: ANN401 — duck-typed AudioCaptureTask + cyclic-import avoidance
    pipeline: Any,  # noqa: ANN401 — duck-typed VoicePipeline + cyclic-import avoidance
    tuning: VoiceTuningConfig,
    state: RuntimeFailoverState,
) -> None:
    """Hot-failover entry point invoked by the deaf-signal closure.

    Mission §Phase 2 T2.6. Called AFTER the bypass coordinator has
    returned a non-empty outcome list with no
    :attr:`BypassVerdict.APPLIED_HEALTHY` entries (i.e. the
    "ineffective" branch at
    :func:`sovyx.voice.pipeline._orchestrator._invoke_deaf_signal`).

    Decision tree (in order):

    1. **Lenient telemetry first**: emit ``voice.failover.attempted``
       regardless of every other gate so the dashboards see the
       decision context (current endpoint, candidates remaining, gate
       state, attempt index). This event is the calibration data the
       v0.31.0 default-flip will be validated against.
    2. **Gate check**: if
       :attr:`VoiceTuningConfig.runtime_failover_on_quarantine_enabled`
       is ``False`` → return early. No mutation; the lenient event
       above is the only side effect.
    3. **Exhausted check**: if ``state.attempts >=
       tuning.max_failover_attempts`` → emit ``voice.failover.exhausted``
       (idempotent via ``state.exhausted_emitted``) → return.
    4. **Cooldown check**: if ``time.monotonic() -
       state.last_attempt_monotonic < tuning.failover_cooldown_s`` →
       emit ``voice.failover.cooldown_blocked`` → return.
    5. **Resolve target** via
       :func:`sovyx.voice.health._factory_integration.select_alternative_endpoint`
       (re-uses the boot-time helper — single source of truth for
       candidate ranking, anti-pattern #16 godfile avoidance). On
       ``None`` → emit ``voice.failover.exhausted`` (cause=no_candidate)
       → return. On exception → emit ``voice.failover.selection_failed``
       → return.
    6. **Bump counters** + **dispatch**
       :meth:`AudioCaptureTask.request_device_change_restart`. On
       success: call
       :meth:`VoicePipeline.reset_coordinator_after_failover` (clears
       the deaf-detection latch so the new endpoint gets its own
       cycle) + emit ``voice.failover.succeeded``. On failure: emit
       ``voice.failover.failed`` and let the cooldown gate naturally
       rate-limit the next attempt.

    Never raises — every step is wrapped in best-effort lookups.
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
            namespacing).
        tuning: The factory's :class:`VoiceTuningConfig` snapshot.
        state: Per-process :class:`RuntimeFailoverState` instance —
            mutated in place (attempt counter, cooldown timestamp,
            exhausted flag).
    """
    from_endpoint, from_friendly_name = _snapshot_current_endpoint(capture_task)

    # Step 1 — resolve target FIRST so the lenient telemetry shows
    # the would-be target. If selection fails, we still emit the
    # event with empty target fields so dashboards see the context.
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

    # Step 2 — gate check. Default OFF per feedback_staged_adoption.
    if not tuning.runtime_failover_on_quarantine_enabled:
        return

    # Step 3 — exhausted check (max attempts).
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

    # Step 4 — cooldown check.
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

    # Step 5 — exhausted check (no candidate).
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

    # Step 6 — bump + dispatch.
    state.attempts += 1
    state.last_attempt_monotonic = now
    attempt_started = time.monotonic()

    try:
        result = await capture_task.request_device_change_restart(
            target,
            reason="endpoint_quarantined",
        )
    except Exception as exc:  # noqa: BLE001 — runtime-failover must not crash heartbeat
        logger.error(
            "voice.failover.failed",
            **{
                "voice.from_endpoint": from_endpoint,
                "voice.target_endpoint": target_endpoint,
                "voice.verdict": "exception",
                "voice.detail": str(exc),
                "voice.error_type": type(exc).__name__,
                "voice.attempt_index": state.attempts,
                "voice.mind_id": mind_id,
            },
        )
        return

    elapsed_ms = int((time.monotonic() - attempt_started) * 1000)

    if getattr(result, "engaged", False):
        # Reset coordinator latch so the new endpoint gets its own
        # deaf-detection cycle. Without this the pipeline stays
        # latched at "coordinator terminated" on the OLD endpoint
        # state and abandons the new one silently.
        try:
            pipeline.reset_coordinator_after_failover()
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "voice.failover.coordinator_reset_failed",
                **{
                    "voice.error": str(exc),
                    "voice.error_type": type(exc).__name__,
                    "voice.mind_id": mind_id,
                },
            )

        logger.warning(
            "voice.failover.succeeded",
            **{
                "voice.from_endpoint": from_endpoint,
                "voice.to_endpoint": target_endpoint,
                "voice.to_friendly_name": target_friendly,
                "voice.attempt_index": state.attempts,
                "voice.elapsed_ms": elapsed_ms,
                "voice.new_endpoint_guid": getattr(result, "new_endpoint_guid", ""),
                "voice.mind_id": mind_id,
            },
        )
    else:
        logger.error(
            "voice.failover.failed",
            **{
                "voice.from_endpoint": from_endpoint,
                "voice.target_endpoint": target_endpoint,
                "voice.verdict": getattr(result, "verdict", "unknown"),
                "voice.detail": getattr(result, "detail", "") or "",
                "voice.attempt_index": state.attempts,
                "voice.mind_id": mind_id,
            },
        )


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


def _safe_mind_id(pipeline: Any) -> str:  # noqa: ANN401
    """Read ``pipeline._config.mind_id`` with best-effort fallback."""
    try:
        config = getattr(pipeline, "_config", None)
        if config is not None:
            return str(getattr(config, "mind_id", "default"))
    except Exception:  # noqa: BLE001
        pass
    return "default"


def _resolve_target_safe(
    *,
    capture_task: Any,  # noqa: ANN401
) -> tuple[DeviceEntry | None, int, Exception | None]:
    """Wrap :func:`select_alternative_endpoint` in a try/except.

    Returns ``(target, candidates_remaining, selection_error)``:

    * ``target`` is ``None`` when no non-quarantined input device
      exists OR when the helper raised.
    * ``candidates_remaining`` is the total non-excluded input device
      count (best-effort). Surfaced in ``voice.failover.attempted``
      so dashboards can compute "how close to exhaustion" without
      a separate query.
    * ``selection_error`` carries the exception when the helper raised;
      used by the caller to emit ``voice.failover.selection_failed``
      AFTER the lenient telemetry event.
    """
    from sovyx.voice.health._factory_integration import select_alternative_endpoint
    from sovyx.voice.health._quarantine import get_default_quarantine

    quarantine = get_default_quarantine()
    current_guid = getattr(capture_task, "active_device_guid", "") or ""
    excluded = (current_guid,) if current_guid else ()

    # Best-effort canonical-name exclusion via the resolver helper.
    excluded_physical: tuple[str, ...] = ()
    try:
        from sovyx.voice.capture._helpers import _resolve_input_entry

        entry = _resolve_input_entry(
            input_device=getattr(capture_task, "_input_device", None),
            enumerate_fn=None,
            host_api_name=getattr(capture_task, "_host_api_name", None),
        )
        if entry is not None and entry.canonical_name:
            excluded_physical = (entry.canonical_name,)
    except Exception:  # noqa: BLE001 — physical exclusion is best-effort only
        pass

    try:
        target = select_alternative_endpoint(
            kind="input",
            exclude_endpoint_guids=excluded,
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
    except Exception:  # noqa: BLE001
        pass

    return target, candidates_remaining, None
