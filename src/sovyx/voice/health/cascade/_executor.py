"""Cascade execution loop — pinned/store/cascade walk.

Split from the legacy ``cascade.py`` (CLAUDE.md anti-pattern #16
hygiene) — see ``MISSION-voice-godfile-splits-v0.24.1.md`` Part 3 / T02.

Owns the core cascade entry points (:func:`run_cascade`,
:func:`run_cascade_for_candidates`), the per-attempt probe wrapper
(:func:`_try_combo`), the structured-log helpers
(:func:`_log_probe_call`, :func:`_log_probe_result`, :func:`_combo_tag`,
:func:`_truncate_detail`, :data:`_LOG_DETAIL_MAX_CHARS`), and the
:class:`ProbeCallable` Protocol that types the cascade's probe
dependency.

Composes:

* :mod:`._planner` — platform cascade tables + per-device tailoring.
* :mod:`._alignment` — pinned override / ComboStore fast-path lookups
  + L2.5 mixer-sanity helper.
* :mod:`._budget` — tuning constants + lifecycle locks +
  quarantine/record-winner helpers.

All public names re-exported from :mod:`sovyx.voice.health.cascade`.
"""

from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING

from sovyx.engine.config import VoiceTuningConfig as _VoiceTuning
from sovyx.observability.logging import get_logger
from sovyx.voice.health._quarantine import (
    EndpointQuarantine,
    get_default_quarantine,
)
from sovyx.voice.health.cascade._alignment import (
    _run_mixer_sanity,
)
from sovyx.voice.health.cascade._budget import (
    _DEFAULT_ATTEMPT_BUDGET_S,
    _DEFAULT_TOTAL_BUDGET_S,
    _default_locks,
)
from sovyx.voice.health.probe import (
    probe as _default_probe,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from sovyx.engine._lock_dict import LRULockDict
    from sovyx.voice.health._mixer_sanity import MixerSanitySetup
    from sovyx.voice.health.capture_overrides import CaptureOverrides
    from sovyx.voice.health.combo_store import ComboStore
    from sovyx.voice.health.contract import (
        CascadeResult,
        Combo,
        ProbeMode,
    )


logger = get_logger(__name__)


__all__ = [
    "ProbeCallable",
    "run_cascade",
    "run_cascade_for_candidates",
]


# Phase 5.F.16 god-file split: probe-invocation surface (ProbeCallable
# Protocol + _call_probe + _try_combo + _PHYSICAL_CURE_DIAGNOSES, ~135 LOC)
# extracted to _executor_probe.py. Re-exported here so the public consumer
# at cascade/__init__.py + the in-parent call sites continue to resolve
# via standard module-namespace lookup. Anti-pattern #16 + #20.
# Phase 5.F.18 god-file split: 3 cascade-walk phase helpers + their shared
# _CascadeRunContext dataclass extracted to _executor_phases.py. The
# orchestrator at _run_cascade_locked uses them directly via this import.
from sovyx.voice.health.cascade._executor_phases import (  # noqa: E402
    _CascadeRunContext,
    _run_phase_cascade_walk,
    _run_phase_pinned,
    _run_phase_store,
)
from sovyx.voice.health.cascade._executor_probe import (  # noqa: E402  F401
    _PHYSICAL_CURE_DIAGNOSES,
    ProbeCallable,
    _call_probe,
    _try_combo,
)

# ── Entry point ─────────────────────────────────────────────────────────


async def run_cascade(
    *,
    endpoint_guid: str,
    device_index: int,
    mode: ProbeMode,
    platform_key: str,
    device_friendly_name: str = "",
    device_interface_name: str = "",
    device_class: str = "",
    endpoint_fxproperties_sha: str = "",
    detected_apos: Sequence[str] = (),
    physical_device_id: str = "",
    combo_store: ComboStore | None = None,
    capture_overrides: CaptureOverrides | None = None,
    probe_fn: ProbeCallable | None = None,
    lifecycle_locks: LRULockDict[str] | None = None,
    total_budget_s: float = _DEFAULT_TOTAL_BUDGET_S,
    attempt_budget_s: float = _DEFAULT_ATTEMPT_BUDGET_S,
    voice_clarity_autofix: bool = True,
    cascade_override: Sequence[Combo] | None = None,
    clock: Callable[[], float] = time.monotonic,
    quarantine: EndpointQuarantine | None = None,
    kernel_invalidated_failover_enabled: bool | None = None,
    mixer_sanity: MixerSanitySetup | None = None,
    tuning: _VoiceTuning | None = None,
) -> CascadeResult:
    """Run the L2 cascade for ``endpoint_guid`` and return the outcome.

    Ordered attempts (any HEALTHY short-circuits):

    1. :class:`CaptureOverrides` pinned combo, if any (source ``"pinned"``).
    2. :class:`ComboStore` fast path, if any (source ``"store"``).
    3. Platform cascade (source ``"cascade"``).

    The whole call holds a per-endpoint :class:`asyncio.Lock` from
    ``lifecycle_locks`` (created automatically if not supplied). A
    module-level fallback dict is used when the caller doesn't pass one
    so standalone ``run_cascade`` calls from tests remain race-safe.

    Args:
        endpoint_guid: Stable GUID of the capture endpoint (Windows
            MMDevice id, Linux ALSA card+device, macOS CoreAudio UID).
        device_index: PortAudio device index to pass to the probe.
        mode: :attr:`ProbeMode.COLD` at boot, :attr:`ProbeMode.WARM`
            during the wizard or on first user interaction.
        platform_key: ``"win32"`` / ``"linux"`` / ``"darwin"``. Picks
            the cascade table and is echoed back to the probe for
            combo construction.
        device_friendly_name, device_interface_name, device_class,
        endpoint_fxproperties_sha, detected_apos: Forwarded to
            :meth:`ComboStore.record_winning` on a successful run so
            the store entry contains the full fingerprint for the 13
            invalidation rules.
        physical_device_id: Canonical physical-device identity
            (:attr:`~sovyx.voice.device_enum.DeviceEntry.canonical_name`)
            of the microphone behind ``endpoint_guid``. Propagated into
            the §4.4.7 quarantine entry so
            :meth:`~sovyx.voice.health._quarantine.EndpointQuarantine.is_quarantined_physical`
            can reject every host-API alias of the same wedged driver
            during fail-over selection. Empty disables physical-scope
            guarding (legacy callers).
        combo_store: Persistent fast-path store. ``None`` disables
            both fast-path lookup and the post-cascade record-winning
            side-effect.
        capture_overrides: User-pinned combos. ``None`` disables
            pinned lookup.
        probe_fn: Probe entry point. Defaults to
            :func:`sovyx.voice.health.probe.probe`; tests inject a fake
            that doesn't touch PortAudio or ONNX.
        lifecycle_locks: Pre-existing per-endpoint lock dict. Created
            at ``maxsize=64`` if omitted.
        total_budget_s: Cascade wall-clock budget. On exhaustion the
            best attempt so far is returned with ``budget_exhausted=True``.
        attempt_budget_s: Per-probe hard timeout. Matches the probe's
            ``hard_timeout_s`` so a hung driver can't stall the cascade.
        voice_clarity_autofix: When ``False`` (user disabled the APO
            bypass), skip attempts 0..4 and start at shared-mode.
        cascade_override: Override the platform cascade for this call.
            Mainly for ``--aggressive`` mode where the caller wants to
            try every combo rather than short-circuit on first HEALTHY.
        clock: Monotonic clock. Swappable for deterministic tests.
        quarantine: §4.4.7 kernel-invalidated quarantine store. When
            ``None`` the process-wide default (via
            :func:`~sovyx.voice.health._quarantine.get_default_quarantine`)
            is used if the kill-switch is on, otherwise quarantine is
            skipped. Tests pass a fresh :class:`EndpointQuarantine` to
            avoid cross-test state bleed.
        kernel_invalidated_failover_enabled: Master toggle for the
            quarantine behaviour. ``None`` resolves to
            :attr:`VoiceTuningConfig.kernel_invalidated_failover_enabled`
            at call time. When ``False``, KERNEL_INVALIDATED results
            fall through to the next cascade combo as normal — preserves
            the pre-§4.4.7 behaviour for operators who want to opt out.
        mixer_sanity: Optional L2.5 dependency bundle. When set AND
            ``platform_key == "linux"``, the cascade runs
            :func:`~sovyx.voice.health._mixer_sanity.check_and_maybe_heal`
            between the ComboStore fast-path and the platform cascade
            walk. On ``HEALED`` the mixer is corrected and the
            subsequent platform walk validates a working combo; on any
            other decision the cascade proceeds unchanged. Default
            ``None`` preserves pre-L2.5 behaviour for every existing
            caller.
    """
    # `or` treats an empty `LRULockDict` as falsy (``__len__ == 0``) and
    # silently drops the caller's shared lock — use an identity check.
    locks = lifecycle_locks if lifecycle_locks is not None else _default_locks()
    lock = locks[endpoint_guid]

    resolved_failover = (
        _VoiceTuning().kernel_invalidated_failover_enabled
        if kernel_invalidated_failover_enabled is None
        else kernel_invalidated_failover_enabled
    )
    resolved_quarantine: EndpointQuarantine | None
    if quarantine is not None:
        resolved_quarantine = quarantine
    elif resolved_failover:
        resolved_quarantine = get_default_quarantine()
    else:
        resolved_quarantine = None

    async with lock:
        return await _run_cascade_locked(
            endpoint_guid=endpoint_guid,
            device_index=device_index,
            mode=mode,
            mixer_sanity=mixer_sanity,
            tuning=tuning,
            platform_key=platform_key,
            device_friendly_name=device_friendly_name,
            device_interface_name=device_interface_name,
            device_class=device_class,
            endpoint_fxproperties_sha=endpoint_fxproperties_sha,
            detected_apos=detected_apos,
            physical_device_id=physical_device_id,
            combo_store=combo_store,
            capture_overrides=capture_overrides,
            probe_fn=probe_fn or _default_probe,
            total_budget_s=total_budget_s,
            attempt_budget_s=attempt_budget_s,
            voice_clarity_autofix=voice_clarity_autofix,
            cascade_override=cascade_override,
            clock=clock,
            quarantine=resolved_quarantine,
        )


async def _run_cascade_locked(
    *,
    endpoint_guid: str,
    device_index: int,
    mode: ProbeMode,
    platform_key: str,
    device_friendly_name: str,
    device_interface_name: str,
    device_class: str,
    endpoint_fxproperties_sha: str,
    detected_apos: Sequence[str],
    physical_device_id: str,
    combo_store: ComboStore | None,
    capture_overrides: CaptureOverrides | None,
    probe_fn: ProbeCallable,
    total_budget_s: float,
    attempt_budget_s: float,
    voice_clarity_autofix: bool,
    cascade_override: Sequence[Combo] | None,
    clock: Callable[[], float],
    quarantine: EndpointQuarantine | None,
    mixer_sanity: MixerSanitySetup | None,
    tuning: _VoiceTuning | None = None,
) -> CascadeResult:
    """Five-phase cascade orchestrator under the per-endpoint lock.

    Phase 5.F.18 refactor: extracted the 3 phase bodies (pinned /
    store fast-path / cascade walk) to ``_executor_phases.py``,
    leaving the lock-guarded orchestrator as a thin dispatcher over
    a shared :class:`_CascadeRunContext`. Phase 0 (quarantine
    short-circuit) + Phase 2.5 (L2.5 mixer sanity) remain inline
    because they are small (~20 LOC each) and have distinct shapes
    (no probe call, side-effect only).
    """
    deadline = clock() + total_budget_s

    # Phase 0 — quarantine short-circuit (§4.4.7 / §4.4.8). A previously
    # quarantined endpoint is known to be in a state that no boot-time
    # cascade can cure (kernel-invalidated or APO-degraded). The factory
    # integration layer fails-over to the next viable DeviceEntry; the
    # watchdog recheck loop retries after the quarantine TTL.
    if quarantine is not None and quarantine.is_quarantined(endpoint_guid):
        entry = quarantine.get(endpoint_guid)
        logger.warning(
            "voice_cascade_skipped_quarantined",
            endpoint=endpoint_guid,
            friendly_name=device_friendly_name,
            reason=entry.reason if entry is not None else "unknown",
        )
        return _make_result(
            endpoint_guid=endpoint_guid,
            winning_combo=None,
            winning_probe=None,
            attempts=[],
            attempts_count=0,
            budget_exhausted=False,
            source="quarantined",
        )

    ctx = _CascadeRunContext(
        endpoint_guid=endpoint_guid,
        device_index=device_index,
        mode=mode,
        platform_key=platform_key,
        device_friendly_name=device_friendly_name,
        device_interface_name=device_interface_name,
        device_class=device_class,
        endpoint_fxproperties_sha=endpoint_fxproperties_sha,
        detected_apos=detected_apos,
        physical_device_id=physical_device_id,
        combo_store=combo_store,
        capture_overrides=capture_overrides,
        probe_fn=probe_fn,
        quarantine=quarantine,
        total_budget_s=total_budget_s,
        attempt_budget_s=attempt_budget_s,
        deadline=deadline,
        clock=clock,
        voice_clarity_autofix=voice_clarity_autofix,
        cascade_override=cascade_override,
    )

    # Phase 1 — pinned override.
    pinned_result = await _run_phase_pinned(ctx)
    if pinned_result is not None:
        return pinned_result

    # Phase 2 — ComboStore fast path.
    store_result = await _run_phase_store(ctx)
    if store_result is not None:
        return store_result

    # Phase 2.5 — L2.5 mixer sanity. Fire-and-forget from the cascade's
    # perspective: on HEALED the ALSA mixer is corrected and the
    # subsequent platform walk succeeds against the healed state; on
    # any other decision the cascade proceeds unchanged. L2.5 does
    # NOT pick a PortAudio combo (that's the platform cascade's
    # responsibility) — it only repairs the mixer state so the
    # platform walk has a chance. See ADR-voice-mixer-sanity-l2.5-
    # bidirectional + V2 Master Plan Part C.1.
    #
    # The ``try/except Exception`` here is defence-in-depth:
    # ``_run_mixer_sanity`` already catches ``check_and_maybe_heal``
    # errors internally, but a failure in its setup code (e.g.,
    # ``CandidateEndpoint`` construction with malformed inputs) or a
    # misbehaving DI callable injected by the user would otherwise
    # abort the cascade — defeating the whole point of keeping L2.5
    # an opt-in, side-channel layer.
    if mixer_sanity is not None and platform_key == "linux":
        try:
            await _run_mixer_sanity(
                mixer_sanity=mixer_sanity,
                endpoint_guid=endpoint_guid,
                device_index=device_index,
                device_friendly_name=device_friendly_name,
                combo_store=combo_store,
                capture_overrides=capture_overrides,
                tuning=tuning,
            )
        except asyncio.CancelledError:
            # Paranoid-QA CRITICAL #1: cancellation must propagate —
            # the cascade loop may want to short-circuit.
            raise
        except Exception as exc:  # noqa: BLE001 — cascade must continue on non-cancel error
            logger.warning(
                "voice_cascade_mixer_sanity_helper_raised",
                endpoint=endpoint_guid,
                error_type=type(exc).__name__,
                detail=str(exc)[:200],
            )

    # Phase 3 — platform cascade walk (terminal).
    return await _run_phase_cascade_walk(ctx)


# Phase 5.F.17 god-file split: run_cascade_for_candidates (~272 LOC)
# extracted to _executor_candidates.py. Re-exported here so the public
# consumer at cascade/__init__.py + the wire-up call site at
# voice/health/_factory_integration.py continue to resolve via standard
# module-namespace lookup. Anti-pattern #16 + #20.
from sovyx.voice.health.cascade._executor_candidates import (  # noqa: E402  F401
    run_cascade_for_candidates,
)

# Phase 5.F.7 god-file split: 6 internal helpers + _LOG_DETAIL_MAX_CHARS
# constant extracted to :mod:cascade._executor_helpers. Re-exported
# below so every internal call site (run_cascade + run_cascade_for_candidates +
# _try_combo) resolves the names via standard module-namespace lookup.
# Anti-pattern #16 + #20.
from sovyx.voice.health.cascade._executor_helpers import (  # noqa: E402  F401
    _LOG_DETAIL_MAX_CHARS,
    _combo_tag,
    _compute_diagnosis_histogram,
    _log_probe_call,
    _log_probe_result,
    _make_result,
    _truncate_detail,
)
