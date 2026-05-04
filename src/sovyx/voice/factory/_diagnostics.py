"""Factory diagnostic helpers — read-only platform probes + APO emit.

Split from the legacy ``factory.py`` (CLAUDE.md anti-pattern #16
hygiene) — see ``MISSION-voice-godfile-splits-v0.24.1.md`` Part 3 / T03.

All helpers here are read-only by contract:

* They emit structured logs / dotted-namespace telemetry.
* They never mutate device state, never raise on probe failure.
* Each is gated by a ``VoiceTuningConfig.voice_*`` flag so operators
  can disable any one independently if it adds boot-time cost.

Owns:

* :func:`_maybe_log_pipewire_status` — F3 wire-up (Linux).
* :func:`_maybe_log_alsa_ucm_status` — F4 wire-up (Linux).
* :func:`_maybe_log_macos_diagnostics` — MA1 + MA5 + MA6 + MA10 + MA13 +
  MA14 wire-up (macOS).
* :func:`_maybe_log_recent_audio_etw_events` — WI1 wire-up (Windows).
* :func:`_maybe_start_audio_service_watchdog` — WI2 wire-up (Windows).
* :func:`_emit_capture_apo_detection` — Windows APO MMDevices walk.
* :func:`_emit_linux_capture_apo_detection` — Linux PA/PW filter walk.
"""

from __future__ import annotations

import asyncio
import sys

from sovyx.observability.logging import get_logger

logger = get_logger(__name__)


__all__ = [
    "_emit_capture_apo_detection",
    "_emit_group_policy_detection",
    "_emit_linux_capture_apo_detection",
    "_emit_linux_sandbox_detection",
    "_maybe_log_alsa_ucm_status",
    "_maybe_log_macos_diagnostics",
    "_maybe_log_pipewire_status",
    "_maybe_log_recent_audio_etw_events",
    "_maybe_start_audio_service_watchdog",
]


def _maybe_log_pipewire_status() -> None:
    """F3 wire-up: read-only PipeWire detection on Linux startup.
    Opt-in via
    :attr:`VoiceTuningConfig.voice_pipewire_detection_enabled` (default
    True — pure observability, never mutates state)."""
    from sovyx.engine.config import VoiceTuningConfig

    tuning = VoiceTuningConfig()
    if not tuning.voice_pipewire_detection_enabled:
        return
    try:
        from sovyx.voice.health._pipewire import detect_pipewire

        report = detect_pipewire()
    except Exception as exc:  # noqa: BLE001 — observability only
        logger.warning(
            "voice.factory.pipewire_detection_failed",
            error=str(exc),
            error_type=type(exc).__name__,
        )
        return
    logger.info(
        "voice.factory.pipewire_status",
        **{
            "voice.pipewire_status": report.status.value,
            "voice.pipewire_socket_present": report.socket_present,
            "voice.pipewire_pactl_available": report.pactl_available,
            "voice.pipewire_server_name": report.server_name or "",
            "voice.pipewire_echo_cancel_loaded": report.echo_cancel_loaded,
            "voice.pipewire_modules_count": len(report.modules_loaded),
            "voice.pipewire_notes": list(report.notes),
        },
    )


def _maybe_log_alsa_ucm_status() -> None:
    """F4 wire-up: read-only ALSA UCM detection on Linux startup.

    Opt-in via
    :attr:`VoiceTuningConfig.voice_alsa_ucm_detection_enabled` (default
    True). Iterates every ALSA card with at least one capture PCM
    (via :func:`sovyx.voice.health._alsa_input_cards.enumerate_input_card_ids`)
    and emits one ``voice.factory.alsa_ucm_status`` event per card
    with the new ``voice.ucm_card_index`` field.

    Mission anchor:
    ``docs-internal/missions/MISSION-voice-linux-silent-mic-remediation-2026-05-04.md``
    §Phase 1 T1.3.

    Forensic anchor: the user's daemon
    (``c:\\Users\\guipe\\Downloads\\logs_01.txt`` line 843) emitted
    ``voice.factory.alsa_ucm_status ucm_card_id=0 ucm_status=no_profile``
    — but on that host card 0 is HDMI-only and the real mic is on
    card 1 (never probed). The pre-T1.3 hard-coded ``card_id="0"``
    default made the event misleading on multi-card systems.

    Backward-compat: on a host with NO input cards (e.g. headless
    server) emits exactly one event with ``voice.ucm_card_index=-1``
    and ``voice.ucm_status="unavailable"`` so dashboards always see
    a baseline record (signal-of-absence is a valid telemetry signal).
    Per-card failures emit ``voice.factory.alsa_ucm_detection_failed``
    and the loop continues to the next card.
    """
    from sovyx.engine.config import VoiceTuningConfig

    tuning = VoiceTuningConfig()
    if not tuning.voice_alsa_ucm_detection_enabled:
        return

    from sovyx.voice.health._alsa_input_cards import enumerate_input_card_ids
    from sovyx.voice.health._alsa_ucm import UcmReport, UcmStatus, detect_ucm

    cards = enumerate_input_card_ids()
    if not cards:
        # Signal-of-absence: dashboards distinguish "scan ran, no
        # input cards" from "scan never ran". Emit one baseline
        # event with the sentinel card_index = -1.
        baseline = UcmReport(
            status=UcmStatus.UNAVAILABLE,
            card_id="",
            alsaucm_available=False,
            notes=("no ALSA card with capture PCM detected",),
        )
        _emit_alsa_ucm_event(baseline, card_index=-1)
        return

    for card_index, card_id in cards:
        try:
            report = detect_ucm(card_id or str(card_index))
        except Exception as exc:  # noqa: BLE001 — observability only
            logger.warning(
                "voice.factory.alsa_ucm_detection_failed",
                **{
                    "voice.card_index": card_index,
                    "voice.card_id": card_id,
                    "voice.error": str(exc),
                    "voice.error_type": type(exc).__name__,
                },
            )
            continue
        _emit_alsa_ucm_event(report, card_index=card_index)


def _emit_alsa_ucm_event(report: object, *, card_index: int) -> None:
    """Emit one ``voice.factory.alsa_ucm_status`` log record.

    Extracted from :func:`_maybe_log_alsa_ucm_status` so the per-card
    iteration body stays concise (anti-pattern #16 — keeps the parent
    function readable even after the multi-card fan-out).

    The new ``voice.ucm_card_index`` field carries the numeric kernel
    index (or ``-1`` for the empty-fallback baseline). Existing fields
    (``voice.ucm_status``, ``voice.ucm_card_id``,
    ``voice.ucm_alsaucm_available``, ``voice.ucm_verbs``,
    ``voice.ucm_active_verb``, ``voice.ucm_notes``) are PRESERVED for
    backward compatibility with existing dashboard consumers.
    """
    logger.info(
        "voice.factory.alsa_ucm_status",
        **{
            "voice.ucm_status": report.status.value,  # type: ignore[attr-defined]
            "voice.ucm_card_id": report.card_id,  # type: ignore[attr-defined]
            "voice.ucm_card_index": card_index,
            "voice.ucm_alsaucm_available": report.alsaucm_available,  # type: ignore[attr-defined]
            "voice.ucm_verbs": list(report.verbs),  # type: ignore[attr-defined]
            "voice.ucm_active_verb": report.active_verb or "",  # type: ignore[attr-defined]
            "voice.ucm_notes": list(report.notes),  # type: ignore[attr-defined]
        },
    )


async def _maybe_log_macos_diagnostics() -> None:
    """MA1+MA5+MA6 wire-up (Step 5): run the macOS audio diagnostic
    trio at boot.

    Three probes invoked sequentially via ``asyncio.to_thread`` so the
    boot path stays async-clean (the Bluetooth probe in particular
    spawns ``system_profiler`` which has a 2-5 s cold-start cost and
    must not block the event loop):

    1. :func:`detect_hal_plugins` — virtual-audio + audio-enhancement
       HAL plug-ins that intercept capture (Krisp / BlackHole /
       Loopback / SoundSource).
    2. :func:`verify_microphone_entitlement` — Hardened-Runtime mic
       entitlement verdict (PRESENT / ABSENT / UNSIGNED / UNKNOWN).
    3. :func:`detect_bluetooth_audio_profile` — A2DP-only headphones
       in input-device role (the canonical "user wonders why their
       AirPods Pro can't be heard" failure mode).

    Opt-in via
    :attr:`VoiceTuningConfig.voice_probe_macos_diagnostics_enabled`
    (default True — read-only observability). Capability-gated via
    :data:`Capability.COREAUDIO_VPIO` (validates darwin +
    ``system_profiler`` on PATH); non-darwin platforms skip silently.

    Failure isolation: each probe internally absorbs its own
    subprocess / parse failures into per-report notes. This wrapper
    additionally catches any unexpected exception per probe so a
    single broken detector never blocks the other two from running.
    """
    from sovyx.engine.config import VoiceTuningConfig

    tuning = VoiceTuningConfig()
    if not tuning.voice_probe_macos_diagnostics_enabled:
        return
    from sovyx.voice.health._capabilities import (  # noqa: PLC0415 — local import keeps factory cold-start lean
        Capability,
        get_default_resolver,
    )

    resolver = get_default_resolver()
    if not resolver.has(Capability.COREAUDIO_VPIO):
        # Non-darwin or system_profiler missing — no-op, no log
        # (avoids polluting Windows + Linux boot output with a darwin
        # capability-absent message). Operators on darwin who hit this
        # branch see it via the existing capability resolver telemetry.
        return

    # MA1 — HAL plug-in enumeration.
    try:
        from sovyx.voice._hal_detector_mac import detect_hal_plugins

        hal_report = await asyncio.to_thread(detect_hal_plugins)
    except Exception as exc:  # noqa: BLE001 — observability gate isolation
        logger.warning(
            "voice.factory.macos_hal_probe_failed",
            error=str(exc),
            error_type=type(exc).__name__,
        )
    else:
        if hal_report.plugins:
            logger.info(
                "voice.macos.hal_plugins_detected",
                **{
                    "voice.plugin_count": len(hal_report.plugins),
                    "voice.virtual_audio_active": hal_report.virtual_audio_active,
                    "voice.audio_enhancement_active": hal_report.audio_enhancement_active,
                    "voice.bundle_names": [p.bundle_name for p in hal_report.plugins],
                    "voice.categories": list(hal_report.by_category.keys()),
                },
            )
        if hal_report.notes:
            logger.debug(
                "voice.macos.hal_probe_notes",
                **{"voice.notes": list(hal_report.notes)},
            )

    # MA5 — Hardened Runtime mic entitlement.
    try:
        from sovyx.voice._codesign_verify_mac import verify_microphone_entitlement

        entitlement_report = await asyncio.to_thread(verify_microphone_entitlement)
    except Exception as exc:  # noqa: BLE001 — observability gate isolation
        logger.warning(
            "voice.factory.macos_entitlement_probe_failed",
            error=str(exc),
            error_type=type(exc).__name__,
        )
    else:
        # Always log entitlement state — operators need to see
        # PRESENT / ABSENT / UNSIGNED / UNKNOWN every boot for
        # forensic attribution if capture later fails.
        logger.info(
            "voice.macos.entitlement_verified",
            **{
                "voice.verdict": entitlement_report.verdict.value,
                "voice.executable_path": entitlement_report.executable_path,
                "voice.remediation_hint": entitlement_report.remediation_hint,
            },
        )

    # MA6 — Bluetooth audio profile.
    try:
        from sovyx.voice._bluetooth_profile_mac import detect_bluetooth_audio_profile

        bt_report = await asyncio.to_thread(detect_bluetooth_audio_profile)
    except Exception as exc:  # noqa: BLE001 — observability gate isolation
        logger.warning(
            "voice.factory.macos_bluetooth_probe_failed",
            error=str(exc),
            error_type=type(exc).__name__,
        )
    else:
        # Log every Bluetooth scan even when no devices found — the
        # baseline-zero record helps operators distinguish "scan ran,
        # no Bluetooth devices" from "scan never ran".
        a2dp_only = bt_report.a2dp_only_devices
        logger.info(
            "voice.macos.bluetooth_profile_detected",
            **{
                "voice.device_count": len(bt_report.devices),
                "voice.a2dp_only_input_count": len(a2dp_only),
                "voice.a2dp_only_input_names": [d.name for d in a2dp_only],
            },
        )
        if bt_report.notes:
            logger.debug(
                "voice.macos.bluetooth_probe_notes",
                **{"voice.notes": list(bt_report.notes)},
            )

    # MA10 — coreaudiod daemon-state probe.
    try:
        from sovyx.voice.health._coreaudiod_recovery import (
            CoreAudiodVerdict,
            probe_coreaudiod_state,
        )

        coreaudiod_report = await asyncio.to_thread(probe_coreaudiod_state)
    except Exception as exc:  # noqa: BLE001 — observability gate isolation
        logger.warning(
            "voice.factory.macos_coreaudiod_probe_failed",
            error=str(exc),
            error_type=type(exc).__name__,
        )
    else:
        # Always emit the verdict — RUNNING is a happy-path baseline,
        # MISSING / UNKNOWN carry the remediation hint.
        log_method = (
            logger.warning
            if coreaudiod_report.verdict is CoreAudiodVerdict.MISSING
            else logger.info
        )
        log_method(
            "voice.macos.coreaudiod_state",
            **{
                "voice.verdict": coreaudiod_report.verdict.value,
                "voice.remediation_hint": coreaudiod_report.remediation_hint,
            },
        )

    # MA13 — macOS App Sandbox detection.
    try:
        from sovyx.voice.health._macos_sandbox_detect import (
            SandboxVerdict,
            detect_sandbox_state,
        )

        sandbox_report = await asyncio.to_thread(detect_sandbox_state)
    except Exception as exc:  # noqa: BLE001 — observability gate isolation
        logger.warning(
            "voice.factory.macos_sandbox_probe_failed",
            error=str(exc),
            error_type=type(exc).__name__,
        )
    else:
        # SANDBOXED is informational (constraint, not error). UNKNOWN
        # at INFO too. Only log if not the standard UNSANDBOXED case.
        if sandbox_report.verdict is not SandboxVerdict.UNSANDBOXED:
            logger.info(
                "voice.macos.sandbox_state",
                **{
                    "voice.verdict": sandbox_report.verdict.value,
                    "voice.executable_path": sandbox_report.executable_path,
                    "voice.remediation_hint": sandbox_report.remediation_hint,
                },
            )

    # MA14 — recent audio events from unified logging (sysdiagnose
    # subset). Default lookback 5 m + max 100 events.
    try:
        from sovyx.voice.health._macos_sysdiagnose import query_audio_log_events

        log_result = await asyncio.to_thread(query_audio_log_events)
    except Exception as exc:  # noqa: BLE001 — observability gate isolation
        logger.warning(
            "voice.factory.macos_sysdiagnose_probe_failed",
            error=str(exc),
            error_type=type(exc).__name__,
        )
    else:
        if log_result.events:
            logger.info(
                "voice.macos.recent_audio_events",
                **{
                    "voice.event_count": len(log_result.events),
                    "voice.lookback": log_result.lookback,
                    "voice.first_event_level": log_result.events[0].level.value,
                    "voice.first_event_subsystem": log_result.events[0].subsystem,
                    "voice.first_event_process": log_result.events[0].process,
                },
            )
        if log_result.notes:
            logger.debug(
                "voice.macos.sysdiagnose_notes",
                **{"voice.notes": list(log_result.notes)},
            )


async def _maybe_log_recent_audio_etw_events() -> None:
    """WI1 wire-up (Step 4): query Windows audio ETW operational
    channels at boot and log structured ``voice.windows.etw_events``
    records.

    Opt-in via
    :attr:`VoiceTuningConfig.voice_probe_windows_etw_events_enabled`
    (default OFF — the probe spawns three ``wevtutil.exe``
    subprocesses with 5 s timeouts each, up to 15 s of additional
    cold-boot latency on busy Windows hosts).

    Capability dispatch: gated by
    :data:`Capability.ETW_AUDIO_PROVIDER`. The probe internally
    requires Windows + ``wevtutil`` on PATH. Locked-down enterprise
    images that strip the binary skip cleanly.

    Failure isolation: subprocess / parse failures are absorbed into
    per-channel notes by :func:`query_audio_etw_events` itself; this
    wrapper additionally catches any unexpected exception so a buggy
    probe never blocks pipeline boot.
    """
    from sovyx.engine.config import VoiceTuningConfig

    tuning = VoiceTuningConfig()
    if not tuning.voice_probe_windows_etw_events_enabled:
        return
    from sovyx.voice.health._capabilities import (  # noqa: PLC0415 — local import keeps factory cold-start lean
        Capability,
        get_default_resolver,
    )

    resolver = get_default_resolver()
    if not resolver.has(Capability.ETW_AUDIO_PROVIDER):
        logger.info(
            "voice.factory.etw_probe_skipped_capability_absent",
            **{
                "voice.capability": Capability.ETW_AUDIO_PROVIDER.value,
                "voice.platform": sys.platform,
            },
        )
        return
    try:
        from sovyx.voice.health._windows_etw import query_audio_etw_events

        results = await asyncio.to_thread(query_audio_etw_events)
    except Exception as exc:  # noqa: BLE001 — observability gate isolation
        logger.warning(
            "voice.factory.etw_probe_failed",
            error=str(exc),
            error_type=type(exc).__name__,
        )
        return
    for result in results:
        if result.events:
            logger.info(
                "voice.windows.etw_events",
                **{
                    "voice.channel": result.channel,
                    "voice.event_count": len(result.events),
                    "voice.lookback_seconds": result.lookback_seconds,
                    "voice.first_event_provider": result.events[0].provider,
                    "voice.first_event_id": result.events[0].event_id,
                    "voice.first_event_level": result.events[0].level.value,
                },
            )
        if result.notes:
            logger.debug(
                "voice.windows.etw_query_notes",
                **{
                    "voice.channel": result.channel,
                    "voice.notes": list(result.notes),
                },
            )


async def _maybe_start_audio_service_watchdog() -> object | None:
    """WI2 wire-up: instantiate and start the Windows audio-service
    watchdog when opt-in. Returns the watchdog instance (so the
    caller can stop it on voice-disable) or ``None`` when disabled,
    on non-Windows, or when instantiation failed.

    Default OFF via
    :attr:`VoiceTuningConfig.voice_audio_service_watchdog_enabled`.
    Operators opt in when they've observed audio-service-related
    failures and want the rolling 30 s sc.exe poll surfacing service
    state transitions in real time.

    Failure isolation: instantiation / start failures log WARN but
    NEVER prevent pipeline creation — the watchdog is supplementary
    observability, not a hard requirement."""
    from sovyx.engine.config import VoiceTuningConfig

    tuning = VoiceTuningConfig()
    if not tuning.voice_audio_service_watchdog_enabled:
        return None
    # X1 Phase 3 (mission §9.1.2 spirit): capability dispatch replaces
    # the raw ``sys.platform != "win32"`` gate. The
    # :data:`Capability.AUDIOSRV_QUERY` probe validates BOTH that we
    # are on Windows AND that ``sc.exe`` is on PATH — locked-down
    # enterprise images that strip ``sc.exe`` get a clean fall-back
    # (the watchdog skips activation; the legacy no-watchdog path
    # is what every pre-WI2 release shipped, so degradation is safe).
    from sovyx.voice.health._capabilities import (  # noqa: PLC0415 — local import keeps factory cold-start lean
        Capability,
        get_default_resolver,
    )

    resolver = get_default_resolver()
    if not resolver.has(Capability.AUDIOSRV_QUERY):
        # Opt-in but capability absent — log INFO so operators see the
        # mismatch instead of silently failing to instantiate. Includes
        # ``platform`` for parity with the legacy log shape (dashboards
        # already filter on this field).
        logger.info(
            "voice.factory.audio_service_watchdog_skipped_capability_absent",
            **{
                "voice.capability": Capability.AUDIOSRV_QUERY.value,
                "voice.platform": sys.platform,
            },
        )
        return None
    try:
        from sovyx.voice.health._windows_audio_service import AudioServiceWatchdog

        watchdog = AudioServiceWatchdog()
        await watchdog.start()
    except Exception as exc:  # noqa: BLE001 — observability gate isolation
        logger.warning(
            "voice.factory.audio_service_watchdog_start_failed",
            error=str(exc),
            error_type=type(exc).__name__,
        )
        return None
    logger.info(
        "voice.factory.audio_service_watchdog_started",
        interval_s=watchdog._interval_s,  # noqa: SLF001 — telemetry only
    )
    return watchdog


def _emit_capture_apo_detection(*, resolved_name: str | None) -> None:
    """Log ``voice_apo_detected`` once per pipeline boot.

    Non-fatal and best-effort: if the registry walk fails for any
    reason, we swallow the error so a misconfigured Windows install
    never blocks pipeline startup. On non-Windows platforms the
    detector returns an empty list and this function is a no-op.
    """
    from sovyx.voice._apo_detector import detect_capture_apos, find_endpoint_report

    try:
        reports = detect_capture_apos()
    except Exception:  # noqa: BLE001 — detector must never break startup
        logger.debug("voice_apo_detection_failed", exc_info=True)
        return

    active = find_endpoint_report(reports, device_name=resolved_name)
    if active is not None:
        logger.info(
            "voice_apo_detected",
            endpoint=active.endpoint_name,
            enumerator=active.enumerator,
            known_apos=active.known_apos,
            fx_binding_count=active.fx_binding_count,
            voice_clarity_active=active.voice_clarity_active,
        )
    elif reports:
        logger.debug(
            "voice_apo_detected_no_match",
            endpoint_count=len(reports),
            resolved_name=resolved_name,
        )

    # Dotted-namespace telemetry (IMPL-OBSERVABILITY-001 §3.6). The
    # scan event always fires once per pipeline boot — even with zero
    # endpoints — so the dashboard knows the detector ran. Per-endpoint
    # detail is folded into voice.endpoints so the timeline can render
    # the full chain without a follow-up RPC.
    voice_clarity_global = any(rep.voice_clarity_active for rep in reports)
    logger.info(
        "audio.apo.scan",
        **{
            "voice.endpoint_count": len(reports),
            "voice.active_endpoint_id": active.endpoint_id if active else None,
            "voice.active_endpoint_name": active.endpoint_name if active else None,
            "voice.resolved_name": resolved_name,
            "voice.voice_clarity_global": voice_clarity_global,
            "voice.endpoints": [
                {
                    "endpoint_id": rep.endpoint_id,
                    "endpoint_name": rep.endpoint_name,
                    "device_interface_name": rep.device_interface_name,
                    "enumerator": rep.enumerator,
                    "fx_binding_count": rep.fx_binding_count,
                    "known_apos": list(rep.known_apos),
                    "raw_clsids": list(rep.raw_clsids),
                    "voice_clarity_active": rep.voice_clarity_active,
                }
                for rep in reports
            ],
        },
    )
    logger.info(
        "audio.apo.voice_clarity_detected",
        **{
            "voice.detected": voice_clarity_global,
            "voice.active_endpoint_detected": bool(active and active.voice_clarity_active),
            "voice.active_endpoint_name": active.endpoint_name if active else None,
        },
    )


def _emit_linux_sandbox_detection() -> None:
    """Log ``voice.sandbox.detected`` once per pipeline boot (T5.45).

    Reads :func:`sovyx.voice.health._sandbox_detector.detect_linux_sandbox`
    + emits the structured event. No-op on non-Linux. Best-effort
    + non-fatal — detector errors are swallowed at DEBUG so
    sandbox-detection bugs never block startup.
    """
    from sovyx.voice.health._sandbox_detector import (
        detect_linux_sandbox,
        log_sandbox_snapshot,
    )

    try:
        snapshot = detect_linux_sandbox()
    except Exception:  # noqa: BLE001 — detector MUST never break startup
        logger.debug("voice.sandbox.detection_failed", exc_info=True)
        return

    log_sandbox_snapshot(snapshot)


def _emit_group_policy_detection() -> None:
    """Log ``voice.group_policy.*`` once per pipeline boot (T5.46 + T5.47).

    Reads voice-affecting Windows Group Policy keys via
    :mod:`sovyx.voice._group_policy_detector`. On non-Windows
    hosts the function is a no-op. Errors during the registry
    probe surface as a structured WARN with a probe-failure
    reason so operators can distinguish "no GP set" from
    "couldn't read GP".

    The most operator-actionable case is
    ``DisallowExclusiveDevice=1`` (enterprise fleet blocks
    WASAPI exclusive mode). Sovyx's Tier 3 bypass becomes a
    no-op in that scenario; Tier 1 (RAW) and Tier 2
    (host_api_rotate) handle Voice Clarity-class incidents
    instead. Surfacing the policy at boot gives operators a
    chance to coordinate with their Windows admin BEFORE
    debugging cryptic PortAudio -9988 errors mid-incident.
    """
    from sovyx.voice._group_policy_detector import (
        detect_group_policies,
        log_group_policy_snapshot,
    )

    try:
        snapshot = detect_group_policies()
    except Exception:  # noqa: BLE001 — detector MUST never break startup
        logger.debug("voice.group_policy.detection_failed", exc_info=True)
        return

    log_group_policy_snapshot(snapshot)


def _emit_linux_capture_apo_detection(*, resolved_name: str | None) -> None:
    """Log ``voice_linux_apo_*`` once per pipeline boot on Linux.

    Peer of :func:`_emit_capture_apo_detection` — same contract
    (best-effort, never raises, no-op on non-Linux) but runs the
    PulseAudio / PipeWire subprocess detector instead of the
    Windows MMDevices registry walk. Structured log surface
    mirrors the Windows path so the dashboard can render Linux
    echo-cancel / noise-suppression with the same timeline widget
    it uses for Windows Voice Clarity.

    Three log records emitted:

    * ``voice_linux_apo_detected`` (INFO, user-visible) — fires
      when the detector surfaced at least one named filter
      (module-echo-cancel, rnnoise, etc.). Carries the dominant
      session manager and deduplicated friendly labels.
    * ``audio.apo.scan.linux`` (INFO, dotted-namespace telemetry)
      — always fires so the dashboard knows the scan ran. Payload
      mirrors Windows ``audio.apo.scan`` with a ``voice.platform``
      discriminator so SLO queries can union the two topics.
    * ``audio.apo.echo_cancel_detected`` (INFO) — one bit for
      dashboards that want a single "any mic-chain processing
      active?" signal across Windows + Linux.

    Non-Linux platforms: the detector returns ``[]`` and this
    function emits nothing (the scan event is gated on platform
    to keep Windows telemetry clean of Linux zero-activity noise).
    """
    if sys.platform != "linux":
        return

    from sovyx.voice._apo_detector_linux import detect_capture_apos_linux

    try:
        reports = detect_capture_apos_linux()
    except Exception:  # noqa: BLE001 — detector must never break startup
        logger.debug("voice_linux_apo_detection_failed", exc_info=True)
        return

    # LinuxApoReport is session-wide — at most one report is
    # returned today. The list wrapper mirrors the Windows API so
    # tests and downstream consumers share a shape.
    report = reports[0] if reports else None

    if report is not None and report.known_apos:
        logger.info(
            "voice_linux_apo_detected",
            session_manager=report.session_manager,
            known_apos=report.known_apos,
            echo_cancel_active=report.echo_cancel_active,
            resolved_name=resolved_name,
        )

    echo_cancel_global = bool(report is not None and report.echo_cancel_active)
    logger.info(
        "audio.apo.scan.linux",
        **{
            "voice.platform": "linux",
            "voice.session_manager": report.session_manager if report else "unknown",
            "voice.echo_cancel_global": echo_cancel_global,
            "voice.resolved_name": resolved_name,
            "voice.known_apos": list(report.known_apos) if report else [],
            "voice.raw_entries": list(report.raw_entries) if report else [],
        },
    )
    logger.info(
        "audio.apo.echo_cancel_detected",
        **{
            "voice.detected": echo_cancel_global,
            "voice.platform": "linux",
            "voice.session_manager": report.session_manager if report else "unknown",
        },
    )
