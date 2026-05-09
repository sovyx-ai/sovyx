"""Wire the L2 cascade into :mod:`sovyx.voice.factory` at boot.

Implements ADR §5.11 "Migration from Existing Installs":

* First daemon start under the new code — :class:`ComboStore` file
  absent → run the cold cascade once to populate the fast-path store.
* Subsequent boots — the fast path short-circuits on a store hit; no
  extra probe is attempted when a validated entry already exists.
* ``voice_clarity_autofix`` honoured as the cascade's default Windows
  gate. When the user set it to ``False`` the cascade restricts itself
  to shared-mode attempts (ADR §5.11 rule 2).

Sprint 1 scope: the cascade runs for its *memoization side-effect*
only — the winning :class:`~sovyx.voice.health.contract.Combo` is
persisted to the store so later boots benefit from the fast path and
Sprint 2+ watchdog code has a known-good reference. The actual
:class:`AudioCaptureTask` continues to open through the battle-tested
:mod:`sovyx.voice._stream_opener` pyramid this sprint; using the
cascade winner to drive the capture stream is deferred to the L4
watchdog work (Task #17).

The helper is sync-adjacent — it awaits :func:`run_cascade`, which in
turn awaits the probe. PortAudio calls inside the probe are already on
``asyncio.to_thread`` per CLAUDE.md anti-pattern #14, so the factory's
event loop never blocks.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import TYPE_CHECKING

from sovyx.observability.logging import get_logger
from sovyx.voice.health.capture_overrides import CaptureOverrides
from sovyx.voice.health.cascade import run_cascade, run_cascade_for_candidates
from sovyx.voice.health.combo_store import ComboStore
from sovyx.voice.health.contract import CandidateEndpoint, CascadeResult, ProbeMode

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from sovyx.engine.config import VoiceTuningConfig
    from sovyx.voice._apo_detector import CaptureApoReport
    from sovyx.voice.device_enum import DeviceEntry
    from sovyx.voice.health._mixer_sanity import MixerSanitySetup
    from sovyx.voice.health._quarantine import EndpointQuarantine

logger = get_logger(__name__)


# Relative paths under ``data_dir`` per ADR-combo-store-schema.md §2.
_COMBO_STORE_FILENAME = "voice/capture_combos.json"
_CAPTURE_OVERRIDES_FILENAME = "voice/capture_overrides.json"
# T5.39 — operator-contributed mixer KB profiles directory. Convention
# documented at :class:`MixerKBLookup.load_shipped_and_user` docstring
# (``~/.sovyx/mixer_kb/user/``). Resolved from ``data_dir`` so test
# harnesses with a tmp_path-rooted data dir get isolation.
_MIXER_KB_USER_SUBDIR = Path("mixer_kb") / "user"


def resolve_combo_store_path(data_dir: Path) -> Path:
    """Return the canonical ``capture_combos.json`` path under ``data_dir``."""
    return data_dir / _COMBO_STORE_FILENAME


def resolve_capture_overrides_path(data_dir: Path) -> Path:
    """Return the canonical ``capture_overrides.json`` path under ``data_dir``."""
    return data_dir / _CAPTURE_OVERRIDES_FILENAME


def _resolve_mixer_kb_user_profiles_dir(
    data_dir: Path,
    tuning: VoiceTuningConfig,
) -> Path | None:
    """Return the user-profiles directory per the T5.39 tuning gate.

    Returns ``None`` when
    :attr:`VoiceTuningConfig.voice_mixer_kb_user_profiles_enabled` is
    False (default; back-compat). Returns
    ``data_dir / "mixer_kb" / "user"`` otherwise. The directory is
    NOT created here — :func:`load_profiles_from_directory` accepts
    a missing path and returns an empty list, which is the
    operator-friendly contract: dropping a profile is the only step
    needed.
    """
    if not tuning.voice_mixer_kb_user_profiles_enabled:
        return None
    return data_dir / _MIXER_KB_USER_SUBDIR


def _build_usb_fingerprint_resolver(
    tuning: VoiceTuningConfig,
) -> Callable[[str], str | None] | None:
    """Return the ComboStore USB fingerprint resolver per tuning gate.

    T5.43 + T5.51 wire-up. Returns ``None`` when
    :attr:`VoiceTuningConfig.combo_store_usb_fingerprint_enabled` is
    False (default; back-compat) — the store falls back to its
    pre-wire-up endpoint-GUID-only behaviour. Returns the
    cross-platform façade
    :func:`sovyx.voice.health._endpoint_fingerprint.resolve_endpoint_to_usb_fingerprint`
    when True.

    Lazy-imports so non-Windows / slim-CI hosts that lack comtypes
    don't pay the import cost when the flag is off.
    """
    if not tuning.combo_store_usb_fingerprint_enabled:
        return None
    from sovyx.voice.health._endpoint_fingerprint import (  # noqa: PLC0415 — lazy import per resolver flag
        resolve_endpoint_to_usb_fingerprint,
    )

    return resolve_endpoint_to_usb_fingerprint


# Phase 5.F.2 god-file split — driver-watchdog observability
# (5 functions + 3 regex constants, ~380 LOC) lives in
# :mod:`sovyx.voice.health._driver_watchdog_observability`. Re-exported
# here so the in-file callers (run_boot_cascade + run_boot_cascade_for_candidates)
# resolve via standard module-namespace lookup AND every existing test patch
# at ``sovyx.voice.health._factory_integration._log_linux_driver_watchdog_scan``
# (etc.) continues to intercept correctly. Anti-pattern #16 + #20.
from sovyx.voice.health._driver_watchdog_observability import (  # noqa: E402  F401
    _LINUX_FP_CODEC_RE,
    _LINUX_FP_USB_RE,
    _MACOS_FP_DEVICE_RE,
    _autofix_after_driver_watchdog_scan,
    _extract_linux_watchdog_hints,
    _extract_macos_watchdog_hints,
    _log_linux_driver_watchdog_scan,
    _log_macos_driver_watchdog_scan,
)

# Phase 5.F.1 god-file split — :func:`derive_endpoint_guid` lives in
# :mod:`sovyx.voice.health._endpoint_guid`. Re-exported here for
# backward compatibility with every existing
# ``from sovyx.voice.health._factory_integration import derive_endpoint_guid``
# import path. Anti-pattern #16 reference; anti-pattern #20 covers
# the test-patch migration (the helper's only direct caller is the
# cascade boot path in this same module, so no test patch path
# changes — but tests that import the symbol from here continue to
# work via this re-export).
from sovyx.voice.health._endpoint_guid import derive_endpoint_guid  # noqa: E402  F401


async def run_boot_cascade(
    *,
    resolved: DeviceEntry,
    data_dir: Path,
    tuning: VoiceTuningConfig,
    apo_reports: list[CaptureApoReport] | None = None,
    platform_key: str | None = None,
    combo_store: ComboStore | None = None,
    capture_overrides: CaptureOverrides | None = None,
    quarantine: EndpointQuarantine | None = None,
) -> CascadeResult | None:
    """Run the cold cascade once at boot, populating :class:`ComboStore`.

    Returns ``None`` when the cascade cannot meaningfully run — no
    cascade table for the current platform (Linux / macOS pre-Sprint 4),
    :class:`ComboStore` instantiation fails, etc. Callers treat that as
    "continue with the legacy opener".

    Exceptions are swallowed and logged so a corrupt :class:`ComboStore`
    file, a transient ``OSError`` on ``data_dir``, or a probe-side bug
    cannot prevent the voice pipeline from starting. ADR §5.11 frames
    this as a migration side-effect, not a hard gate.

    Args:
        resolved: The :class:`DeviceEntry` :func:`~sovyx.voice.device_enum.resolve_device`
            just returned. Supplies the PortAudio index the probe will
            open and the canonical name used to derive the endpoint GUID.
        data_dir: Sovyx data directory. ``ComboStore`` and
            ``CaptureOverrides`` live under ``data_dir/voice/``.
        tuning: Effective :class:`VoiceTuningConfig`. Drives cascade
            budgets + ``voice_clarity_autofix``.
        apo_reports: Optional pre-computed capture-APO reports (Windows
            only). When provided, used to resolve the Windows MMDevices
            GUID for the endpoint. Factory passes this through so the
            registry walk happens once per boot, not twice.
        platform_key: Override the runtime platform key. Tests pass
            ``"win32"`` to exercise the Windows cascade on a non-Windows
            host without monkey-patching :mod:`sys`.
        combo_store: DI hook for tests. Production callers pass ``None``
            and the store is constructed from ``data_dir``.
        capture_overrides: DI hook for tests. Production callers pass
            ``None`` and the overrides file is constructed from
            ``data_dir``.
        quarantine: §4.4.7 endpoint quarantine store. ``None`` falls back
            to :func:`~sovyx.voice.health._quarantine.get_default_quarantine`
            (subject to
            :attr:`VoiceTuningConfig.kernel_invalidated_failover_enabled`).
            Tests pass a fresh :class:`EndpointQuarantine` to avoid
            cross-test state bleed.
    """
    plat = platform_key or sys.platform

    store = combo_store
    overrides = capture_overrides
    if store is None:
        try:
            store = ComboStore(
                resolve_combo_store_path(data_dir),
                usb_fingerprint_resolver=_build_usb_fingerprint_resolver(tuning),
            )
            store.load()
        except Exception:  # noqa: BLE001 — store failure must not block boot (ADR §5.11)
            logger.warning("voice_boot_cascade_combo_store_unavailable", exc_info=True)
            store = None
    if overrides is None:
        try:
            overrides = CaptureOverrides(resolve_capture_overrides_path(data_dir))
            overrides.load()
        except Exception:  # noqa: BLE001 — overrides failure must not block boot (ADR §5.11)
            logger.warning("voice_boot_cascade_capture_overrides_unavailable", exc_info=True)
            overrides = None

    endpoint_guid = derive_endpoint_guid(
        resolved,
        apo_reports=apo_reports,
        platform_key=plat,
    )

    detected_apos: tuple[str, ...] = ()
    endpoint_fxproperties_sha = ""
    device_friendly_name = resolved.name
    device_interface_name = ""
    device_class = ""
    if plat == "win32" and apo_reports:
        from sovyx.voice._apo_detector import find_endpoint_report

        report = find_endpoint_report(apo_reports, device_name=resolved.name)
        if report is not None:
            detected_apos = tuple(report.known_apos)
            device_interface_name = report.device_interface_name
            device_class = report.enumerator

    # Phase 3 — Driver Watchdog pre-flight. On Windows, when the
    # Kernel-PnP Driver Watchdog (event IDs 900/901) has recently fired
    # for the target device's hardware ID, the driver's event-queue
    # thread is likely still wedged. Force shared-mode for this boot
    # so we never issue an exclusive-init IOCTL against the unstable
    # driver — that path is what produced the Razer BlackShark V2 Pro
    # Kernel-Power 41 hard-reset (2026-04-20 post-mortem). The override
    # is logged loudly so operators see what happened.
    effective_autofix = tuning.voice_clarity_autofix
    if plat == "win32" and tuning.voice_clarity_autofix:
        effective_autofix = await _autofix_after_driver_watchdog_scan(
            resolved_name=resolved.name,
            device_interface_name=device_interface_name,
            lookback_hours=tuning.driver_watchdog_lookback_hours,
            timeout_s=tuning.driver_watchdog_scan_timeout_s,
        )

    # Linux-side driver-watchdog scan. Detection-tier only — unlike
    # Windows we don't downgrade anything based on the result, because
    # Linux PortAudio already runs shared-mode via ALSA by default.
    # The telemetry ties cascade outcomes to concrete kernel events
    # (HDA codec wedge, USB descriptor failure, XRUN flood) so
    # post-incident triage has a one-command audit trail.
    if plat == "linux":
        await _log_linux_driver_watchdog_scan(
            alsa_name=resolved.name,
            endpoint_guid=endpoint_guid,
            lookback_hours=tuning.driver_watchdog_lookback_hours,
            timeout_s=tuning.driver_watchdog_scan_timeout_s,
        )

    # macOS detection-tier scan — symmetric to the Linux branch
    # above. No behaviour change, only observability: ``log show``
    # surfaces coreaudiod distress (HAL engine error, aggregate
    # disconnect, watchdog timeout) and the helper emits correlated /
    # unrelated / clean log records the dashboard can render.
    if plat == "darwin":
        await _log_macos_driver_watchdog_scan(
            device_name=resolved.name,
            endpoint_guid=endpoint_guid,
            lookback_hours=tuning.driver_watchdog_lookback_hours,
            timeout_s=tuning.driver_watchdog_scan_timeout_s,
        )

    try:
        result = await run_cascade(
            endpoint_guid=endpoint_guid,
            device_index=resolved.index,
            mode=ProbeMode.COLD,
            platform_key=plat,
            device_friendly_name=device_friendly_name,
            device_interface_name=device_interface_name,
            device_class=device_class,
            endpoint_fxproperties_sha=endpoint_fxproperties_sha,
            detected_apos=detected_apos,
            physical_device_id=resolved.canonical_name,
            combo_store=store,
            capture_overrides=overrides,
            total_budget_s=tuning.cascade_total_budget_s,
            attempt_budget_s=tuning.cascade_attempt_budget_s,
            voice_clarity_autofix=effective_autofix,
            quarantine=quarantine,
            kernel_invalidated_failover_enabled=tuning.kernel_invalidated_failover_enabled,
        )
    except Exception:  # noqa: BLE001 — cascade crash must never block the pipeline (ADR §5.11)
        logger.error(
            "voice_boot_cascade_raised",
            endpoint=endpoint_guid,
            device_index=resolved.index,
            exc_info=True,
        )
        return None

    logger.info(
        "voice_boot_cascade_result",
        endpoint=endpoint_guid,
        source=result.source,
        attempts=result.attempts_count,
        budget_exhausted=result.budget_exhausted,
        has_winner=result.winning_combo is not None,
    )
    return result


async def run_boot_cascade_for_candidates(
    *,
    candidates: Sequence[CandidateEndpoint],
    data_dir: Path,
    tuning: VoiceTuningConfig,
    platform_key: str | None = None,
    combo_store: ComboStore | None = None,
    capture_overrides: CaptureOverrides | None = None,
    quarantine: EndpointQuarantine | None = None,
) -> CascadeResult | None:
    """Run the cold cascade against a candidate-set (VLX-002 fix).

    Symmetric companion to :func:`run_boot_cascade`: same boot-time
    semantics (best-effort, swallowed exceptions, populates
    :class:`ComboStore` on winner), but consults the
    :class:`~sovyx.voice.health.contract.CandidateEndpoint` list built
    by :func:`~sovyx.voice.health._candidate_builder.build_capture_candidates`
    rather than a single resolved device.

    When ``len(candidates) == 1`` the behaviour is indistinguishable
    from :func:`run_boot_cascade` with ``resolved = candidates[0]`` —
    this is the regression invariant that lets us migrate
    Windows / macOS callers without behavioural change.

    Args:
        candidates: Ordered candidate list. Must be non-empty. The
            first candidate is the user-preferred device.
        data_dir: Sovyx data directory.
        tuning: Effective :class:`VoiceTuningConfig`.
        platform_key: Runtime platform key. Defaults to ``sys.platform``.
        combo_store: DI hook for tests. Production callers pass ``None``.
        capture_overrides: DI hook for tests.
        quarantine: §4.4.7 endpoint quarantine store.

    Returns:
        :class:`CascadeResult` on successful cascade run (any outcome,
        including exhaustion). ``None`` when the cascade cannot run —
        :class:`ComboStore` init failed, the cascade raised, etc. The
        factory treats ``None`` as "fall back to legacy opener path".

    Raises:
        ValueError: ``candidates`` is empty (programmer error —
            ``build_capture_candidates`` always returns ≥ 1 entry).
    """
    if not candidates:
        msg = "candidates must be non-empty"
        raise ValueError(msg)

    plat = platform_key or sys.platform

    store = combo_store
    overrides = capture_overrides
    if store is None:
        try:
            store = ComboStore(
                resolve_combo_store_path(data_dir),
                usb_fingerprint_resolver=_build_usb_fingerprint_resolver(tuning),
            )
            store.load()
        except Exception:  # noqa: BLE001 — store failure must not block boot (ADR §5.11)
            logger.warning("voice_boot_cascade_combo_store_unavailable", exc_info=True)
            store = None
    if overrides is None:
        try:
            overrides = CaptureOverrides(resolve_capture_overrides_path(data_dir))
            overrides.load()
        except Exception:  # noqa: BLE001 — overrides failure must not block boot (ADR §5.11)
            logger.warning("voice_boot_cascade_capture_overrides_unavailable", exc_info=True)
            overrides = None

    # Phase 3 driver-watchdog pre-flight — only meaningful for the
    # user-preferred candidate on Windows. Other candidates (pipewire
    # virtual, etc.) don't have a kernel-PnP driver mapping.
    effective_autofix = tuning.voice_clarity_autofix
    primary = candidates[0]
    if plat == "win32" and tuning.voice_clarity_autofix:
        effective_autofix = await _autofix_after_driver_watchdog_scan(
            resolved_name=primary.friendly_name,
            device_interface_name=primary.canonical_name,
            lookback_hours=tuning.driver_watchdog_lookback_hours,
            timeout_s=tuning.driver_watchdog_scan_timeout_s,
        )

    # Linux detection-tier scan, symmetric to :func:`run_boot_cascade`
    # but scoped to the primary candidate. Secondary candidates (the
    # PipeWire / Pulse fallbacks) don't correspond to a physical
    # kernel device with its own driver-watchdog signal, so there's
    # nothing to correlate past the primary.
    if plat == "linux":
        await _log_linux_driver_watchdog_scan(
            alsa_name=primary.canonical_name,
            endpoint_guid=primary.endpoint_guid,
            lookback_hours=tuning.driver_watchdog_lookback_hours,
            timeout_s=tuning.driver_watchdog_scan_timeout_s,
        )

    # macOS detection-tier scan, same candidate-primary scoping.
    if plat == "darwin":
        await _log_macos_driver_watchdog_scan(
            device_name=primary.friendly_name,
            endpoint_guid=primary.endpoint_guid,
            lookback_hours=tuning.driver_watchdog_lookback_hours,
            timeout_s=tuning.driver_watchdog_scan_timeout_s,
        )

    # L2.5 mixer-sanity setup — Linux only, constructed once per boot
    # cascade. ``build_mixer_sanity_setup`` returns ``None`` on non-
    # Linux, on unknown driver family (no KB profile can match), or on
    # KB load failure — all of which collapse to the pre-L2.5 cascade
    # behaviour. Invariant I7: every runtime_pm write is delegated to
    # the systemd oneshot + udev rule; the daemon itself never
    # escalates. See ADR-voice-mixer-sanity-l2.5-bidirectional.
    mixer_sanity: MixerSanitySetup | None = None
    if plat == "linux":
        try:
            from sovyx.voice.health._half_heal_recovery import (  # noqa: PLC0415 — lazy-Linux
                default_wal_path,
            )
            from sovyx.voice.health._mixer_sanity import (
                build_mixer_sanity_setup,  # noqa: PLC0415 — lazy-Linux
            )
            from sovyx.voice.health._telemetry import get_telemetry  # noqa: PLC0415
            from sovyx.voice.health.probe import probe as _probe_fn  # noqa: PLC0415

            mixer_sanity = await build_mixer_sanity_setup(
                probe_fn=_probe_fn,
                telemetry=get_telemetry(),
                # Paranoid-QA R2 HIGH #3: half-heal write-ahead log
                # rooted under ``data_dir/voice_health/``. Survives
                # mid-apply process crashes so the next cascade can
                # restore pre-apply state before probing.
                half_heal_wal_path=default_wal_path(data_dir),
                # T5.39 — operator-contributed KB profiles. Lenient
                # default: ``None`` skips user-side loading entirely.
                # Flag-gated to keep production behaviour unchanged
                # until operators opt in.
                user_profiles_dir=_resolve_mixer_kb_user_profiles_dir(
                    data_dir,
                    tuning,
                ),
            )
        except Exception:  # noqa: BLE001 — L2.5 setup must never block boot
            logger.warning(
                "voice_boot_cascade_mixer_sanity_setup_failed",
                exc_info=True,
            )
            mixer_sanity = None

    try:
        result = await run_cascade_for_candidates(
            candidates=candidates,
            mode=ProbeMode.COLD,
            platform_key=plat,
            combo_store=store,
            capture_overrides=overrides,
            total_budget_s=tuning.cascade_total_budget_s,
            attempt_budget_s=tuning.cascade_attempt_budget_s,
            voice_clarity_autofix=effective_autofix,
            quarantine=quarantine,
            kernel_invalidated_failover_enabled=tuning.kernel_invalidated_failover_enabled,
            mixer_sanity=mixer_sanity,
            # Paranoid-QA CRITICAL #8: thread the operator's tuning
            # through so SOVYX_TUNING__VOICE__* env overrides reach
            # the L2.5 budget / match threshold / customization
            # thresholds / subprocess timeout.
            tuning=tuning,
        )
    except Exception:  # noqa: BLE001 — cascade crash must never block the pipeline (ADR §5.11)
        logger.error(
            "voice_boot_cascade_for_candidates_raised",
            primary_endpoint=primary.endpoint_guid,
            primary_device_index=primary.device_index,
            candidate_count=len(candidates),
            exc_info=True,
        )
        return None

    logger.info(
        "voice_boot_cascade_for_candidates_result",
        primary_endpoint=primary.endpoint_guid,
        candidate_count=len(candidates),
        winning_rank=(
            result.winning_candidate.preference_rank
            if result.winning_candidate is not None
            else None
        ),
        winning_source=(
            str(result.winning_candidate.source) if result.winning_candidate is not None else None
        ),
        source=result.source,
        attempts=result.attempts_count,
        budget_exhausted=result.budget_exhausted,
        has_winner=result.winning_combo is not None,
    )
    return result


# Phase 5.F.3 god-file split — boot-cascade verdict classification +
# alternative-endpoint selection (2 dataclass-style types + 2 functions,
# ~210 LOC) lives in :mod:`sovyx.voice.health._cascade_verdict`. Re-exported
# here so 6 production callers (factory/_validate.py +
# capture/_restart_mixin.py + health/_runtime_failover.py) and 3 test
# patches at ``sovyx.voice.health._factory_integration.select_alternative_endpoint``
# continue to resolve via standard module-namespace lookup.
# Anti-pattern #16 + #20.
from sovyx.voice.health._cascade_verdict import (  # noqa: E402  F401
    CascadeBootOutcome,
    CascadeBootVerdict,
    classify_cascade_boot_result,
    select_alternative_endpoint,
)

__all__ = [
    "CascadeBootOutcome",
    "CascadeBootVerdict",
    "classify_cascade_boot_result",
    "derive_endpoint_guid",
    "resolve_capture_overrides_path",
    "resolve_combo_store_path",
    "run_boot_cascade",
    "select_alternative_endpoint",
]
