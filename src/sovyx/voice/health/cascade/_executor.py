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
from typing import TYPE_CHECKING, Protocol

from sovyx.engine.config import VoiceTuningConfig as _VoiceTuning
from sovyx.observability.logging import get_logger
from sovyx.voice.health._metrics import (
    record_cascade_attempt,
    record_combo_store_hit,
    record_probe_result,
)
from sovyx.voice.health._quarantine import (
    EndpointQuarantine,
    get_default_quarantine,
)
from sovyx.voice.health._user_remediation import (
    homogeneous_diagnosis_remediation,
)
from sovyx.voice.health.cascade._alignment import (
    _lookup_override,
    _lookup_store,
    _run_mixer_sanity,
)
from sovyx.voice.health.cascade._budget import (
    _DEFAULT_ATTEMPT_BUDGET_S,
    _DEFAULT_TOTAL_BUDGET_S,
    _VOICE_CLARITY_AUTOFIX_FIRST_ATTEMPT,
    _default_locks,
    _quarantine_endpoint,
    _record_winner,
)
from sovyx.voice.health.cascade._planner import (
    LINUX_CASCADE,
    _platform_cascade,
    build_linux_cascade_for_device,
)
from sovyx.voice.health.contract import (
    CandidateEndpoint,
    CascadeResult,
    Combo,
    Diagnosis,
    ProbeMode,
    ProbeResult,
)
from sovyx.voice.health.probe import (
    _classify_open_error,
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


logger = get_logger(__name__)


__all__ = [
    "ProbeCallable",
    "run_cascade",
    "run_cascade_for_candidates",
]


# T6.9 — diagnoses that share the "physical cure required" semantic
# with KERNEL_INVALIDATED. The cascade short-circuits on these
# (quarantine + return) instead of trying remaining combos because
# the failure is below the host-API layer — every alternative combo
# will fail identically until the user replugs / reboots.
#
# - KERNEL_INVALIDATED: IAudioClient::Initialize stuck, surfaces as
#   paInvalidDevice / AUDCLNT_E_DEVICE_INVALIDATED (Windows-canonical).
# - STREAM_OPEN_TIMEOUT (T6.2): driver accepted open + start but
#   never delivered audio in ≥ 5 s. Same root-cause family observed
#   via the callback-not-fired surface.
_PHYSICAL_CURE_DIAGNOSES: frozenset[Diagnosis] = frozenset(
    {
        Diagnosis.KERNEL_INVALIDATED,
        Diagnosis.STREAM_OPEN_TIMEOUT,
    },
)


# ── Probe callable typing ────────────────────────────────────────────────


class ProbeCallable(Protocol):
    """Structural type for the probe function used by the cascade.

    Tests inject a fake matching this shape; production calls
    :func:`sovyx.voice.health.probe.probe`.
    """

    async def __call__(
        self,
        *,
        combo: Combo,
        mode: ProbeMode,
        device_index: int,
        hard_timeout_s: float,
    ) -> ProbeResult: ...


async def _call_probe(
    probe_fn: ProbeCallable,
    *,
    combo: Combo,
    mode: ProbeMode,
    device_index: int,
    hard_timeout_s: float,
) -> ProbeResult:
    """Invoke the probe with just the cascade's required kwargs.

    Trims the interface so tests don't have to mock every optional
    keyword of :func:`sovyx.voice.health.probe.probe` — only the four
    that the cascade explicitly drives are forwarded.
    """
    return await probe_fn(
        combo=combo,
        mode=mode,
        device_index=device_index,
        hard_timeout_s=hard_timeout_s,
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
    deadline = clock() + total_budget_s
    attempts: list[ProbeResult] = []
    attempts_count = 0

    # §4.4.7 / §4.4.8 short-circuit: a previously quarantined endpoint
    # is known to be in a state that no *boot-time* cascade can cure —
    # either kernel-invalidated (reason ``"probe_*"`` /
    # ``"watchdog_recheck"`` / ``"factory_integration"``) or APO-degraded
    # (reason ``"apo_degraded"``). Skip every attempt — the factory
    # integration layer will fail-over to the next viable
    # :class:`DeviceEntry` and the watchdog recheck loop retries after
    # the quarantine TTL. The log surfaces the live entry's ``reason``
    # token so operators can distinguish the two root causes without
    # reading two separate events.
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
            attempts=attempts,
            attempts_count=attempts_count,
            budget_exhausted=False,
            source="quarantined",
        )

    # 1. Pinned override.
    pinned = _lookup_override(capture_overrides, endpoint_guid, platform_key)
    if pinned is not None:
        logger.info(
            "voice_cascade_pinned_lookup",
            endpoint=endpoint_guid,
            combo=_combo_tag(pinned),
        )
        _log_probe_call(
            endpoint_guid=endpoint_guid,
            attempt=0,
            device_index=device_index,
            combo=pinned,
            mode=mode,
            attempt_budget_s=attempt_budget_s,
        )
        result = await _try_combo(
            probe_fn=probe_fn,
            combo=pinned,
            mode=mode,
            device_index=device_index,
            attempt_budget_s=attempt_budget_s,
        )
        _log_probe_result(
            endpoint_guid=endpoint_guid,
            attempt=0,
            device_index=device_index,
            combo=pinned,
            result=result,
        )
        attempts.append(result)
        attempts_count += 1
        record_cascade_attempt(
            platform=platform_key,
            host_api=pinned.host_api,
            success=result.diagnosis is Diagnosis.HEALTHY,
            source="pinned",
        )
        if result.diagnosis is Diagnosis.HEALTHY:
            # T1 — uniform winner telemetry across pinned/store/cascade.
            logger.info(
                "voice_cascade_winner_selected",
                endpoint=endpoint_guid,
                source="pinned",
                attempts=1,
                combo_host_api=pinned.host_api,
                combo_sample_rate=pinned.sample_rate,
                combo_channels=pinned.channels,
                combo_exclusive=pinned.exclusive,
                combo_auto_convert=pinned.auto_convert,
                device_index=device_index,
                device_friendly_name=device_friendly_name,
            )
            return _make_result(
                endpoint_guid=endpoint_guid,
                winning_combo=pinned,
                winning_probe=result,
                attempts=attempts,
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
            quarantine=quarantine,
            endpoint_guid=endpoint_guid,
            device_friendly_name=device_friendly_name,
            device_interface_name=device_interface_name,
            host_api=pinned.host_api,
            platform_key=platform_key,
            reason="probe_pinned",
            physical_device_id=physical_device_id,
        ):
            logger.warning(
                "voice_cascade_physical_cure_required",
                endpoint=endpoint_guid,
                friendly_name=device_friendly_name,
                host_api=pinned.host_api,
                source="pinned",
                diagnosis=result.diagnosis.value,
            )
            return _make_result(
                endpoint_guid=endpoint_guid,
                winning_combo=None,
                winning_probe=None,
                attempts=attempts,
                attempts_count=attempts_count,
                budget_exhausted=False,
                source="quarantined",
            )
        logger.warning(
            "voice_cascade_pinned_failed",
            endpoint=endpoint_guid,
            host_api=pinned.host_api,
            combo=_combo_tag(pinned),
            diagnosis=str(result.diagnosis),
        )

    # 2. ComboStore fast path.
    store_combo = _lookup_store(combo_store, endpoint_guid)
    if store_combo is None:
        record_combo_store_hit(
            endpoint_class=device_class or "unknown",
            result="miss",
        )
    if store_combo is not None:
        if clock() >= deadline:
            return _make_result(
                endpoint_guid=endpoint_guid,
                winning_combo=None,
                winning_probe=None,
                attempts=attempts,
                attempts_count=attempts_count,
                budget_exhausted=True,
                source="none",
            )
        logger.info(
            "voice_cascade_store_lookup",
            endpoint=endpoint_guid,
            combo=_combo_tag(store_combo),
        )
        _log_probe_call(
            endpoint_guid=endpoint_guid,
            attempt=0,
            device_index=device_index,
            combo=store_combo,
            mode=mode,
            attempt_budget_s=attempt_budget_s,
        )
        result = await _try_combo(
            probe_fn=probe_fn,
            combo=store_combo,
            mode=mode,
            device_index=device_index,
            attempt_budget_s=attempt_budget_s,
        )
        _log_probe_result(
            endpoint_guid=endpoint_guid,
            attempt=0,
            device_index=device_index,
            combo=store_combo,
            result=result,
        )
        attempts.append(result)
        success = result.diagnosis is Diagnosis.HEALTHY
        record_cascade_attempt(
            platform=platform_key,
            host_api=store_combo.host_api,
            success=success,
            source="store",
        )
        record_combo_store_hit(
            endpoint_class=device_class or "unknown",
            result="hit" if success else "needs_revalidation",
        )
        if success:
            # Fast-path hit: do NOT re-record (combo already in store).
            # T1 — uniform winner telemetry across pinned/store/cascade.
            logger.info(
                "voice_cascade_winner_selected",
                endpoint=endpoint_guid,
                source="store",
                attempts=1,
                combo_host_api=store_combo.host_api,
                combo_sample_rate=store_combo.sample_rate,
                combo_channels=store_combo.channels,
                combo_exclusive=store_combo.exclusive,
                combo_auto_convert=store_combo.auto_convert,
                device_index=device_index,
                device_friendly_name=device_friendly_name,
            )
            return _make_result(
                endpoint_guid=endpoint_guid,
                winning_combo=store_combo,
                winning_probe=result,
                attempts=attempts,
                attempts_count=0,
                budget_exhausted=False,
                source="store",
            )
        # §4.4.7 + T6.9 — physical-cure state observed on the fast path.
        # Invalidate the (now misleading) store entry too, then quarantine
        # the endpoint and short-circuit the rest of the cascade.
        # KERNEL_INVALIDATED + STREAM_OPEN_TIMEOUT both route here.
        if result.diagnosis in _PHYSICAL_CURE_DIAGNOSES and _quarantine_endpoint(
            quarantine=quarantine,
            endpoint_guid=endpoint_guid,
            device_friendly_name=device_friendly_name,
            device_interface_name=device_interface_name,
            host_api=store_combo.host_api,
            platform_key=platform_key,
            reason="probe_store",
            physical_device_id=physical_device_id,
        ):
            if combo_store is not None:
                combo_store.invalidate(endpoint_guid, reason=result.diagnosis.value)
            logger.warning(
                "voice_cascade_physical_cure_required",
                endpoint=endpoint_guid,
                friendly_name=device_friendly_name,
                host_api=store_combo.host_api,
                source="store",
                diagnosis=result.diagnosis.value,
            )
            return _make_result(
                endpoint_guid=endpoint_guid,
                winning_combo=None,
                winning_probe=None,
                attempts=attempts,
                attempts_count=attempts_count,
                budget_exhausted=False,
                source="quarantined",
            )
        # Invalidate the stale store entry so the next boot runs the
        # full cascade fresh rather than re-probing the known-bad combo.
        # The metric is emitted inside ``ComboStore.invalidate`` — single
        # source of truth for every invalidation path.
        if combo_store is not None:
            combo_store.invalidate(endpoint_guid, reason="fast_path_probe_failed")
            logger.warning(
                "voice_cascade_store_invalidated",
                endpoint=endpoint_guid,
                host_api=store_combo.host_api,
                combo=_combo_tag(store_combo),
                diagnosis=str(result.diagnosis),
            )

    # 2.5. L2.5 mixer sanity — runs only when the caller opts in via
    # ``mixer_sanity`` AND we are on Linux. Fire-and-forget from the
    # cascade's perspective: on HEALED the ALSA mixer is corrected and
    # the subsequent platform walk succeeds against the healed state;
    # on any other decision the cascade proceeds unchanged. L2.5 does
    # NOT pick a PortAudio combo (that's the platform cascade's
    # responsibility) — it only repairs the mixer state so the
    # platform walk has a chance. See ADR-voice-mixer-sanity-l2.5-
    # bidirectional + V2 Master Plan Part C.1.
    #
    # The ``try/except BaseException`` here is defence-in-depth:
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

    # 3. Platform cascade.
    cascade = (
        tuple(cascade_override)
        if cascade_override is not None
        else _platform_cascade(platform_key)
    )
    start_idx = 0 if voice_clarity_autofix else _VOICE_CLARITY_AUTOFIX_FIRST_ATTEMPT
    if platform_key != "win32":
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
                endpoint=endpoint_guid,
                attempt=idx,
                combo=_combo_tag(combo),
            )
            continue
        if clock() >= deadline:
            logger.warning(
                "voice_cascade_budget_exhausted",
                endpoint=endpoint_guid,
                attempts_run=attempts_count,
                total_budget_s=total_budget_s,
                # T6.11 — diagnosis histogram for at-a-glance triage.
                # Empty when the deadline trips before any attempt
                # completes (first-iteration timeout). See
                # :func:`_compute_diagnosis_histogram` for shape.
                diagnosis_histogram=_compute_diagnosis_histogram(attempts),
            )
            return _make_result(
                endpoint_guid=endpoint_guid,
                winning_combo=None,
                winning_probe=None,
                attempts=attempts,
                attempts_count=attempts_count,
                budget_exhausted=True,
                source="none",
            )
        attempts_count += 1
        logger.info(
            "voice_cascade_attempt",
            endpoint=endpoint_guid,
            attempt=idx,
            combo=_combo_tag(combo),
        )
        _log_probe_call(
            endpoint_guid=endpoint_guid,
            attempt=idx,
            device_index=device_index,
            combo=combo,
            mode=mode,
            attempt_budget_s=attempt_budget_s,
        )
        result = await _try_combo(
            probe_fn=probe_fn,
            combo=combo,
            mode=mode,
            device_index=device_index,
            attempt_budget_s=attempt_budget_s,
        )
        _log_probe_result(
            endpoint_guid=endpoint_guid,
            attempt=idx,
            device_index=device_index,
            combo=combo,
            result=result,
        )
        attempts.append(result)
        record_cascade_attempt(
            platform=platform_key,
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
            quarantine=quarantine,
            endpoint_guid=endpoint_guid,
            device_friendly_name=device_friendly_name,
            device_interface_name=device_interface_name,
            host_api=combo.host_api,
            platform_key=platform_key,
            reason="probe_cascade",
            physical_device_id=physical_device_id,
        ):
            logger.warning(
                "voice_cascade_physical_cure_required",
                endpoint=endpoint_guid,
                friendly_name=device_friendly_name,
                host_api=combo.host_api,
                source="cascade",
                attempt=idx,
                diagnosis=result.diagnosis.value,
            )
            return _make_result(
                endpoint_guid=endpoint_guid,
                winning_combo=None,
                winning_probe=None,
                attempts=attempts,
                attempts_count=attempts_count,
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
                combo_store=combo_store,
                endpoint_guid=endpoint_guid,
                device_friendly_name=device_friendly_name,
                device_interface_name=device_interface_name,
                device_class=device_class,
                endpoint_fxproperties_sha=endpoint_fxproperties_sha,
                detected_apos=detected_apos,
                combo=combo,
                probe=result,
                cascade_attempts_before_success=attempts_count,
            )
            # T1 — DoD #3 requires this event to be present in the log
            # after a successful cascade run. Future T3 will extend it
            # with ``winning_candidate`` / ``candidate_source`` fields
            # once the candidate-set refactor lands.
            logger.info(
                "voice_cascade_winner_selected",
                endpoint=endpoint_guid,
                source="cascade",
                attempts=attempts_count,
                combo_host_api=combo.host_api,
                combo_sample_rate=combo.sample_rate,
                combo_channels=combo.channels,
                combo_exclusive=combo.exclusive,
                combo_auto_convert=combo.auto_convert,
                device_index=device_index,
                device_friendly_name=device_friendly_name,
            )
            return _make_result(
                endpoint_guid=endpoint_guid,
                winning_combo=combo,
                winning_probe=result,
                attempts=attempts,
                attempts_count=attempts_count,
                budget_exhausted=False,
                source="cascade",
            )

    histogram = _compute_diagnosis_histogram(attempts)
    logger.error(
        "voice_cascade_exhausted",
        endpoint=endpoint_guid,
        attempts=attempts_count,
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
            endpoint=endpoint_guid,
            attempts=attempts_count,
            diagnosis=diagnosis_value,
            remediation=remediation,
        )
    return _make_result(
        endpoint_guid=endpoint_guid,
        winning_combo=None,
        winning_probe=None,
        attempts=attempts,
        attempts_count=attempts_count,
        budget_exhausted=False,
        source="none",
    )


# ── helpers ─────────────────────────────────────────────────────────────


async def run_cascade_for_candidates(
    *,
    candidates: Sequence[CandidateEndpoint],
    mode: ProbeMode,
    platform_key: str,
    combo_store: ComboStore | None = None,
    capture_overrides: CaptureOverrides | None = None,
    probe_fn: ProbeCallable | None = None,
    lifecycle_locks: LRULockDict[str] | None = None,
    total_budget_s: float = _DEFAULT_TOTAL_BUDGET_S,
    attempt_budget_s: float = _DEFAULT_ATTEMPT_BUDGET_S,
    voice_clarity_autofix: bool = True,
    clock: Callable[[], float] = time.monotonic,
    quarantine: EndpointQuarantine | None = None,
    kernel_invalidated_failover_enabled: bool | None = None,
    mixer_sanity: MixerSanitySetup | None = None,
    tuning: _VoiceTuning | None = None,
) -> CascadeResult:
    """Run the cascade against an ordered set of capture candidates.

    This is the candidate-set entry point introduced by the
    ``voice-linux-cascade-root-fix`` mission (VLX-002). It iterates the
    caller-supplied :class:`~sovyx.voice.health.contract.CandidateEndpoint`
    list in order, delegating each to :func:`run_cascade` with the
    candidate's per-endpoint identity. The first healthy winner wins.

    Division of labour vs. :func:`run_cascade`:

    * :func:`run_cascade` — cross-combo, single endpoint. Pinned →
      ComboStore fast-path → platform cascade table walk.
    * :func:`run_cascade_for_candidates` — cross-endpoint, delegates to
      :func:`run_cascade` per candidate. Source of truth for the
      session-manager-escape path at boot time on Linux (VLX-002).

    The total wall-clock ``total_budget_s`` is shared across all
    candidates. Each :func:`run_cascade` call gets the remaining budget,
    so the last candidate may get a shorter window than the first. This
    matches the pre-refactor behaviour (one endpoint, one budget) when
    called with ``len(candidates) == 1``.

    Args:
        candidates: Ordered list from
            :func:`~sovyx.voice.health._candidate_builder.build_capture_candidates`.
            Must be non-empty; the first candidate is the user-preferred
            one (``CandidateSource.USER_PREFERRED``).
        mode: :attr:`ProbeMode.COLD` at boot, :attr:`ProbeMode.WARM`
            during the wizard.
        platform_key: ``"win32"`` / ``"linux"`` / ``"darwin"``.
        combo_store: Persistent fast-path store — forwarded verbatim to
            each :func:`run_cascade` invocation. Each candidate hits the
            store under its own ``endpoint_guid``, so a stored combo for
            ``pipewire`` is still consulted when the user-preferred
            hardware candidate's own fast-path is stale.
        capture_overrides: User-pinned combos — forwarded verbatim.
        probe_fn: Probe entry point. Defaults to
            :func:`~sovyx.voice.health.probe.probe`.
        lifecycle_locks: Per-endpoint lock dict. Each candidate gets its
            own lock; parallel invocations of this function against
            disjoint candidate sets do not serialize.
        total_budget_s: Shared wall-clock budget across all candidates.
            On exhaustion the function returns ``budget_exhausted=True``
            with attempts from candidates tried so far.
        attempt_budget_s: Per-probe hard timeout.
        voice_clarity_autofix: Forwarded to each :func:`run_cascade` call.
        clock: Monotonic clock. Swappable for deterministic tests.
        quarantine: Shared quarantine store. All candidates check the
            same instance — a quarantined ``pipewire`` endpoint does
            not re-probe even if ``hw:1,0`` just finished quarantining.
        kernel_invalidated_failover_enabled: Master toggle for the
            §4.4.7 quarantine behaviour.
        mixer_sanity: Optional L2.5 dependency bundle. When set AND
            ``platform_key == "linux"``, L2.5 runs ONCE for the
            whole candidate-set pass (using the first candidate's
            identity) before the per-candidate cascade loop. The
            inner :func:`run_cascade` invocations receive
            ``mixer_sanity=None`` so healing is not re-attempted
            for every candidate. Default ``None`` preserves pre-L2.5
            behaviour.

    Returns:
        :class:`CascadeResult` with:

        * ``winning_candidate`` populated when any candidate produced a
          healthy combo.
        * ``endpoint_guid`` set to the winning candidate's guid, or the
          first candidate's guid on exhaustion (log correlation).
        * ``attempts`` containing the concatenation of every attempt
          across all tried candidates, in iteration order.

    Raises:
        ValueError: ``candidates`` is empty.
    """
    if not candidates:
        msg = "candidates must be non-empty (build_capture_candidates contract)"
        raise ValueError(msg)

    deadline = clock() + total_budget_s
    aggregated_attempts: list[ProbeResult] = []
    total_attempts_count = 0
    last_result: CascadeResult | None = None

    logger.info(
        "voice_cascade_candidate_set_started",
        platform=platform_key,
        candidate_count=len(candidates),
        candidate_kinds=[str(c.kind) for c in candidates],
        candidate_sources=[str(c.source) for c in candidates],
    )

    # 2.5 — L2.5 mixer sanity runs ONCE per candidate-set pass (the ALSA
    # mixer is system-wide state; healing per-candidate would repeat work).
    # Uses the first candidate's identity for telemetry / endpoint_guid
    # (by candidate-builder contract that's the user-preferred one). We
    # pass mixer_sanity=None to the inner run_cascade calls so L2.5 does
    # NOT fire again under each per-endpoint lock — the healing already
    # happened (or was skipped) at this layer.
    if mixer_sanity is not None and platform_key == "linux":
        try:
            await _run_mixer_sanity(
                mixer_sanity=mixer_sanity,
                endpoint_guid=candidates[0].endpoint_guid,
                device_index=candidates[0].device_index,
                device_friendly_name=candidates[0].friendly_name,
                combo_store=combo_store,
                capture_overrides=capture_overrides,
                tuning=tuning,
            )
        except asyncio.CancelledError:
            # Paranoid-QA CRITICAL #1: cancel propagates.
            raise
        except Exception as exc:  # noqa: BLE001 — cascade must continue
            logger.warning(
                "voice_cascade_candidate_set_mixer_sanity_raised",
                error_type=type(exc).__name__,
                detail=str(exc)[:200],
            )

    # T4 — defensive invariant: dedup by (device_index, host_api_name)
    # must already hold (build_capture_candidates guarantees this), but
    # an ill-behaved injected builder in tests or a future refactor could
    # re-introduce collisions. Log-warn + continue rather than raise; the
    # cascade loop is already O(n×m) and probe idempotency absorbs dupes.
    seen_candidate_keys: set[tuple[int, str]] = set()

    for candidate_idx, candidate in enumerate(candidates):
        remaining = max(0.0, deadline - clock())
        if remaining <= 0.0:
            logger.warning(
                "voice_cascade_candidate_set_budget_exhausted",
                tried=candidate_idx,
                remaining_candidates=len(candidates) - candidate_idx,
            )
            break

        dedup_key = (candidate.device_index, candidate.host_api_name)
        if dedup_key in seen_candidate_keys:
            logger.warning(
                "voice_cascade_candidate_duplicate",
                candidate_rank=candidate.preference_rank,
                device_index=candidate.device_index,
                host_api=candidate.host_api_name,
            )
        seen_candidate_keys.add(dedup_key)

        logger.info(
            "voice_cascade_candidate_started",
            candidate_rank=candidate.preference_rank,
            candidate_source=str(candidate.source),
            candidate_kind=str(candidate.kind),
            device_index=candidate.device_index,
            host_api=candidate.host_api_name,
            friendly_name=candidate.friendly_name,
            endpoint_guid=candidate.endpoint_guid,
            remaining_budget_s=remaining,
        )

        # T5 — per-candidate native-rate cascade. Only prepends when
        # the candidate is HARDWARE and reports a non-canonical rate
        # that the default Linux cascade would waste attempts on.
        per_candidate_cascade: Sequence[Combo] | None = None
        if platform_key == "linux":
            tailored = build_linux_cascade_for_device(
                candidate.default_samplerate,
                str(candidate.kind),
            )
            if tailored is not LINUX_CASCADE:
                per_candidate_cascade = tailored
                logger.info(
                    "voice_cascade_native_rate_prepended",
                    candidate_rank=candidate.preference_rank,
                    device_index=candidate.device_index,
                    native_rate=candidate.default_samplerate,
                )

        per_candidate_result = await run_cascade(
            endpoint_guid=candidate.endpoint_guid,
            device_index=candidate.device_index,
            mode=mode,
            platform_key=platform_key,
            device_friendly_name=candidate.friendly_name,
            device_interface_name=candidate.canonical_name,
            physical_device_id=candidate.canonical_name,
            combo_store=combo_store,
            capture_overrides=capture_overrides,
            probe_fn=probe_fn,
            lifecycle_locks=lifecycle_locks,
            total_budget_s=remaining,
            attempt_budget_s=attempt_budget_s,
            voice_clarity_autofix=voice_clarity_autofix,
            cascade_override=per_candidate_cascade,
            clock=clock,
            quarantine=quarantine,
            kernel_invalidated_failover_enabled=kernel_invalidated_failover_enabled,
        )
        aggregated_attempts.extend(per_candidate_result.attempts)
        total_attempts_count += per_candidate_result.attempts_count
        last_result = per_candidate_result

        if per_candidate_result.winning_combo is not None:
            logger.info(
                "voice_cascade_candidate_set_resolved",
                winning_rank=candidate.preference_rank,
                winning_source=str(candidate.source),
                winning_kind=str(candidate.kind),
                device_index=candidate.device_index,
                host_api=candidate.host_api_name,
                endpoint_guid=candidate.endpoint_guid,
                tried=candidate_idx + 1,
                total=len(candidates),
            )
            return CascadeResult(
                endpoint_guid=candidate.endpoint_guid,
                winning_combo=per_candidate_result.winning_combo,
                winning_probe=per_candidate_result.winning_probe,
                attempts=tuple(aggregated_attempts),
                attempts_count=total_attempts_count,
                budget_exhausted=False,
                source=per_candidate_result.source,
                winning_candidate=candidate,
            )

        # Non-healthy candidate — advance to the next one unless budget
        # is already exhausted (we'll break on the next iteration's
        # ``remaining <= 0`` guard).
        logger.info(
            "voice_cascade_candidate_failed",
            candidate_rank=candidate.preference_rank,
            candidate_source=str(candidate.source),
            device_index=candidate.device_index,
            source_label=per_candidate_result.source,
            budget_exhausted=per_candidate_result.budget_exhausted,
        )

    # Exhausted — return aggregated result keyed on the first candidate
    # so log correlation is stable.
    logger.error(
        "voice_cascade_candidate_set_exhausted",
        candidate_count=len(candidates),
        attempts_total=total_attempts_count,
    )
    first = candidates[0]
    return CascadeResult(
        endpoint_guid=first.endpoint_guid,
        winning_combo=None,
        winning_probe=None,
        attempts=tuple(aggregated_attempts),
        attempts_count=total_attempts_count,
        budget_exhausted=last_result.budget_exhausted if last_result else False,
        source="none",
        winning_candidate=None,
    )


async def _try_combo(
    *,
    probe_fn: ProbeCallable,
    combo: Combo,
    mode: ProbeMode,
    device_index: int,
    attempt_budget_s: float,
) -> ProbeResult:
    """Invoke the probe and convert unexpected exceptions into DRIVER_ERROR results.

    The probe already classifies all known PortAudio failures into the
    :class:`Diagnosis` enum. This wrapper guards against a probe-side
    bug / test misconfiguration turning into a cascade abort — any
    exception becomes a synthetic DRIVER_ERROR so the cascade can
    still fall through.
    """
    try:
        return await _call_probe(
            probe_fn,
            combo=combo,
            mode=mode,
            device_index=device_index,
            hard_timeout_s=attempt_budget_s,
        )
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        # Belt-and-braces: after v0.20.2 Phase 1, the probe classifies
        # stream.start() failures internally, so this path should only
        # fire for genuine probe-side bugs (numpy errors in analysis,
        # test misconfiguration). Still, running the classifier on the
        # raised exception recovers the correct Diagnosis when a future
        # probe-side bug re-introduces a leak (e.g. a kernel-invalidated
        # error escaping a new analysis phase), rather than silently
        # coarsening into DRIVER_ERROR.
        #
        # Gate the classifier on OSError (PortAudio surfaces failures as
        # ``sd.PortAudioError(OSError)``) so an unrelated coding-bug
        # ``TypeError("... format ...")`` or ``AttributeError`` whose
        # message accidentally contains a keyword like "format" / "in use"
        # / "access" cannot be misclassified as a structured Diagnosis.
        # Non-OSError stays DRIVER_ERROR — the original cascade contract.
        if isinstance(exc, OSError):
            diagnosis = _classify_open_error(exc)
        else:
            diagnosis = Diagnosis.DRIVER_ERROR
        logger.error(
            "voice_cascade_probe_raised",
            host_api=combo.host_api,
            combo=_combo_tag(combo),
            diagnosis=str(diagnosis),
            error=repr(exc),
            exc_info=True,
        )
        synthetic = ProbeResult(
            diagnosis=diagnosis,
            mode=mode,
            combo=combo,
            vad_max_prob=None,
            vad_mean_prob=None,
            rms_db=float("-inf"),
            callbacks_fired=0,
            duration_ms=0,
            error=f"probe raised: {exc!r}",
        )
        # Also emit the probe-result telemetry so synthetic results
        # appear in the same dashboards as first-class probe outcomes.
        record_probe_result(synthetic)
        return synthetic


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
) -> None:
    """Emit ``voice_cascade_probe_result`` after every probe invocation (T1)."""
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
