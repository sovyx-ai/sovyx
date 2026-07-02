"""Boot-time driver-watchdog observability + Windows autofix decision.

Phase 5.F.2 god-file extraction from
``voice/health/_factory_integration.py`` (anti-pattern #16).

Owns three responsibilities, all in the boot-cascade pre-flight
window:

1. **Windows autofix gate** — :func:`_autofix_after_driver_watchdog_scan`
   reads the recent ``Driver Watchdog`` event log and returns
   ``False`` (forcing the cascade into shared-mode) when events are
   correlated to the device about to be probed. Returns ``True``
   (the operator's tuning default) on every other path — we only
   downgrade with positive evidence.

2. **Linux journalctl observability** —
   :func:`_log_linux_driver_watchdog_scan` mirrors the Windows
   pre-flight without the autofix downgrade (PortAudio on Linux is
   already shared-mode). Surfaces one of four log records:
   ``unavailable`` / ``clean`` / ``events_unrelated`` /
   ``events_correlated``. Pure observability for post-incident triage.

3. **macOS log show observability** —
   :func:`_log_macos_driver_watchdog_scan` peer of the Linux
   helper. Same four-state log surface keyed off a macOS
   coreaudiod scan.

Shared helpers — :func:`_extract_linux_watchdog_hints` and
:func:`_extract_macos_watchdog_hints` — back-parse identity hints
from the composed endpoint GUID + the alsa/coreaudio device name so
the scan-result correlator (``LinuxDriverWatchdogScan.matches_device``
/ ``MacOSDriverWatchdogScan.matches_device``) can tie kernel-log
events to the specific endpoint about to be probed.

Anti-pattern #20 covered: the parent module
``voice/health/_factory_integration.py`` re-exports every name on
this module so existing direct-import tests + production callers
continue to resolve correctly.
"""

from __future__ import annotations

import asyncio
import re

from sovyx.observability.logging import get_logger

logger = get_logger(__name__)


# Parses the T5.43 USB fingerprint shape ``usb-VVVV:PPPP[-SERIAL]``
# produced by :func:`sovyx.voice.health._usb_fingerprint.fingerprint_usb_device`
# into the (vid, pid) pair the Windows watchdog correlator needs.
_USB_FP_VID_PID_RE = re.compile(r"^usb-([0-9a-f]{4}):([0-9a-f]{4})(?:-|$)")


# Extracts the three identity pieces from a composed Linux endpoint
# fingerprint:
#   {linux-usb-1532:0543-0-capture}                 → usb  = 1532:0543
#   {linux-pci-0000_00_1f.3-codec-14F1:5045-0-cap}  → codec = 14F1:5045
#   {linux-pci-0000_00_1f.3-0-capture}              → no codec
# Kept as narrow regexes so a future fingerprint-shape change
# surfaces as a local test failure in this module, not a silent
# correlation drop in production.
_LINUX_FP_USB_RE = re.compile(
    r"^\{linux-usb-(?P<vidpid>[0-9A-F]{4}:[0-9A-F]{4})-",
    re.IGNORECASE,
)
_LINUX_FP_CODEC_RE = re.compile(
    r"-codec-(?P<codec>[0-9A-F]{4}:[0-9A-F]{4})-",
    re.IGNORECASE,
)


# Extracts the two identity pieces we can read back from a composed
# macOS endpoint fingerprint:
#   {macos-usb-<uid>-input}       → device_uid = <uid>
#   {macos-builtin-<uid>-input}   → device_uid = <uid>
#   {macos-bluetooth-<uid>-duplex}→ device_uid = <uid>
# The transport slot can be anything (including ``unknown``); the
# identity slot is captured as ``[^-]+`` because we sanitise hyphens
# out of UIDs at fingerprint-compose time.
_MACOS_FP_DEVICE_RE = re.compile(
    r"^\{macos-(?P<transport>[A-Za-z0-9_]+)-(?P<uid>[A-Za-z0-9_.+]+)-",
    re.IGNORECASE,
)


def _derive_windows_usb_vid_pid(endpoint_id: str) -> tuple[str, str] | None:
    """Resolve a Windows endpoint identifier to its USB ``(vid, pid)``.

    WINDOWS-5 correlation key: Kernel-PnP Driver Watchdog event
    messages carry ONLY the PnP device instance path
    (``USB\\VID_1532&PID_0528&MI_00\\…``), never the vendor friendly
    name — so the pre-flight needs the target device's USB VID/PID
    to tie an event to the device about to be probed. The chain is
    the same one the combo store uses:
    :func:`~sovyx.voice.health._endpoint_fingerprint_win.resolve_endpoint_to_usb_fingerprint`
    (IMMDevice → PKEY_Device_InstanceId → ``usb-VVVV:PPPP[-SERIAL]``).

    Accepts either endpoint-id shape in circulation:

    * the full ``IMMDevice::GetId`` form ``{0.0.1.00000000}.{guid}``;
    * the bare MMDevices registry-subkey form ``{guid}`` (what
      :class:`~sovyx.voice._apo_detector.CaptureApoReport.endpoint_id`
      / the win32 ``derive_endpoint_guid`` carry) — retried with the
      capture-flow prefix, since ``GetDevice`` requires the full form.

    Best-effort + blocking (COM IPC): returns ``None`` for non-USB
    endpoints, surrogate / Linux / macOS fingerprints, or any COM
    failure. Async callers MUST wrap in ``asyncio.to_thread`` (#14).
    """
    candidate = (endpoint_id or "").strip()
    if not candidate.startswith("{"):
        return None
    if candidate.startswith(("{surrogate-", "{linux-", "{macos-")):
        return None
    from sovyx.voice.health._endpoint_fingerprint_win import (  # noqa: PLC0415 — lazy-Windows COM import
        resolve_endpoint_to_usb_fingerprint,
    )

    fingerprint = resolve_endpoint_to_usb_fingerprint(candidate)
    if fingerprint is None and "." not in candidate:
        # Bare registry-subkey GUID → prepend the capture-flow prefix
        # IMMDeviceEnumerator::GetDevice requires.
        fingerprint = resolve_endpoint_to_usb_fingerprint(
            "{0.0.1.00000000}." + candidate,
        )
    if not fingerprint:
        return None
    match = _USB_FP_VID_PID_RE.match(fingerprint)
    if match is None:
        return None
    return match.group(1), match.group(2)


async def _autofix_after_driver_watchdog_scan(
    *,
    resolved_name: str,
    device_interface_name: str,
    lookback_hours: int,
    timeout_s: float,
    endpoint_id: str = "",
) -> bool:
    """Return the ``voice_clarity_autofix`` flag after the watchdog scan.

    Encapsulates the Phase-3 pre-flight decision so :func:`run_boot_cascade`
    stays focused on cascade orchestration. Any failure in the scan
    (subprocess error, timeout, parse) falls through to ``True`` (the
    untouched tuning default) — we never make the cascade safer *by
    accident*; we only drop to shared-mode when we have positive
    evidence that the driver is fragile.

    Correlation (WINDOWS-5 fix): the primary matcher is hardware
    identity — ``endpoint_id`` is resolved to the device's USB
    VID/PID (via :func:`_derive_windows_usb_vid_pid`, off-loop) and
    matched against the ``vid_xxxx&pid_xxxx`` / ``vid_xxxx#pid_xxxx``
    forms embedded in the event's PnP instance path. The
    ``device_interface_name`` friendly-name substring remains a
    SECONDARY heuristic only — real watchdog messages never carry
    vendor display names, so before this fix the downgrade safety
    net (v0.20.3 Razer Kernel-Power-41 post-mortem) never fired.
    """
    from sovyx.voice.health._driver_watchdog_win import (
        scan_recent_driver_watchdog_events,
    )

    scan = await scan_recent_driver_watchdog_events(
        lookback_hours=lookback_hours,
        timeout_s=timeout_s,
    )
    if not scan.scan_attempted or scan.scan_failed:
        # No signal → trust the operator's tuning default.
        return True
    if not scan.any_events:
        logger.debug(
            "voice_driver_watchdog_preflight_clean",
            device=resolved_name,
            lookback_hours=lookback_hours,
        )
        return True
    # Events exist — but only override when we can tie at least one to
    # this specific device. A drift-printer watchdog should not disable
    # APO-bypass on the USB headset. Hardware identity (USB VID/PID)
    # is the load-bearing matcher; the friendly-name substring is a
    # secondary heuristic for needles that are already PnP-path-shaped.
    # VID/PID derivation is COM IPC → off-loop per anti-pattern #14,
    # and only paid when events actually exist.
    vid_pid = await asyncio.to_thread(_derive_windows_usb_vid_pid, endpoint_id)
    hardware_targeted = vid_pid is not None and scan.matches_hardware_id(
        usb_vid=vid_pid[0],
        usb_pid=vid_pid[1],
    )
    targeted = hardware_targeted or scan.matches_device(device_interface_name)
    if not targeted:
        logger.info(
            "voice_driver_watchdog_preflight_unrelated",
            device=resolved_name,
            event_count=len(scan.events),
            usb_vid_pid=":".join(vid_pid) if vid_pid else None,
        )
        return True
    logger.warning(
        "voice_driver_watchdog_preflight_downgrade",
        device=resolved_name,
        device_interface_name=device_interface_name,
        matched_by="hardware_id" if hardware_targeted else "friendly_name",
        usb_vid_pid=":".join(vid_pid) if vid_pid else None,
        event_count=len(scan.events),
        lookback_hours=lookback_hours,
        remediation=(
            "Driver Watchdog fired for this device recently — forcing "
            "shared-mode for this boot to avoid a kernel-resource "
            "timeout. Replug the device or reboot after the driver "
            "queue clears to restore exclusive-mode APO bypass."
        ),
    )
    return False


def _extract_linux_watchdog_hints(
    *,
    alsa_name: str,
    endpoint_guid: str,
) -> tuple[str | None, str | None, str | None]:
    """Extract (alsa_card_id, usb_vid_pid, codec_vendor_id) hints.

    Used on Linux to correlate ``journalctl -k`` distress events with
    the device the cascade is about to probe. Hints are best-effort:
    any piece we can't extract → ``None``, and
    :meth:`LinuxDriverWatchdogScan.matches_device` accepts partial
    hint sets.

    Sources:

    * ``alsa_card_id`` — parsed from the ALSA PCM name
      (``alsa_name``). ``hw:N,M`` → ``"N"``; ``plughw:CARD=id,DEV=M``
      → ``"id"`` (case-preserved). These match kernel-log device hints
      like ``card0`` / ``card[PCH]`` via substring. Both single-device
      and candidate-set callers pass a PortAudio / ALSA device name
      here (``DeviceEntry.name`` or ``CandidateEndpoint.canonical_name``).
    * ``usb_vid_pid`` and ``codec_vendor_id`` — regex-extracted from
      the already-composed endpoint GUID. The fingerprint format is
      internally owned (:mod:`sovyx.voice.health._fingerprint_linux`),
      so back-parsing is safe as long as the shape is pinned.
    """
    alsa_card_id: str | None = None
    name = alsa_name.strip()
    hw_match = re.match(r"^(?:plug)?hw:(\d+)", name, re.IGNORECASE)
    if hw_match is not None:
        alsa_card_id = hw_match.group(1)
    else:
        card_eq_match = re.match(
            r"^[\w-]+:CARD=([^,\s]+)",
            name,
            re.IGNORECASE,
        )
        if card_eq_match is not None:
            alsa_card_id = card_eq_match.group(1)

    usb_vid_pid: str | None = None
    usb_match = _LINUX_FP_USB_RE.match(endpoint_guid)
    if usb_match is not None:
        usb_vid_pid = usb_match.group("vidpid").lower()

    codec_vendor_id: str | None = None
    codec_match = _LINUX_FP_CODEC_RE.search(endpoint_guid)
    if codec_match is not None:
        # The kernel-log codec hint is the vendor's 8-hex blob
        # (e.g. ``0x14f15045``). Strip the colon and lower-case so
        # the ``matches_device`` substring check lands inside the
        # ``0x<hex>`` kernel form without bus-form noise.
        codec_vendor_id = codec_match.group("codec").replace(":", "").lower()

    return alsa_card_id, usb_vid_pid, codec_vendor_id


async def _log_linux_driver_watchdog_scan(
    *,
    alsa_name: str,
    endpoint_guid: str,
    lookback_hours: int,
    timeout_s: float,
) -> None:
    """Detection-tier scan of ``journalctl -k`` for audio distress.

    Mirrors the Windows driver-watchdog pre-flight for observability,
    but without the ``voice_clarity_autofix`` downgrade — on Linux
    PortAudio already routes through ALSA shared mode, so there is
    no symmetric "safer" mode to fall back to. The helper emits one
    of four log records:

    * ``voice_driver_watchdog_linux_unavailable`` — scan couldn't
      run (non-systemd host, journalctl missing, permission denied).
      DEBUG level so desktop installs don't spam warnings.
    * ``voice_driver_watchdog_linux_clean`` — scan ran, zero events.
    * ``voice_driver_watchdog_linux_events_unrelated`` — events
      exist but none tie to this device's hints. INFO level.
    * ``voice_driver_watchdog_linux_events_correlated`` — one or
      more events matched this device. WARNING level, with full
      event tuple serialised so operators can triage.

    Exceptions from the scan are logged at DEBUG and swallowed —
    boot must never block on the best-effort diagnostic.
    """
    from sovyx.voice.health._driver_watchdog_linux import (
        scan_recent_linux_driver_watchdog_events,
    )

    try:
        scan = await scan_recent_linux_driver_watchdog_events(
            lookback_hours=lookback_hours,
            timeout_s=timeout_s,
        )
    except Exception:  # noqa: BLE001 — scan is best-effort, never block boot
        logger.debug(
            "voice_driver_watchdog_linux_scan_raised",
            exc_info=True,
        )
        return

    if not scan.scan_attempted or scan.scan_failed:
        logger.debug(
            "voice_driver_watchdog_linux_unavailable",
            endpoint=endpoint_guid,
            attempted=scan.scan_attempted,
            failed=scan.scan_failed,
        )
        return

    if not scan.any_events:
        logger.debug(
            "voice_driver_watchdog_linux_clean",
            endpoint=endpoint_guid,
            lookback_hours=lookback_hours,
        )
        return

    alsa_card_id, usb_vid_pid, codec_vendor_id = _extract_linux_watchdog_hints(
        alsa_name=alsa_name,
        endpoint_guid=endpoint_guid,
    )
    correlated = scan.matches_device(
        alsa_card_id=alsa_card_id,
        usb_vid_pid=usb_vid_pid,
        codec_vendor_id=codec_vendor_id,
    )

    severities = sorted({event.severity for event in scan.events})
    pattern_names = sorted({event.pattern_name for event in scan.events})

    if not correlated:
        logger.info(
            "voice_driver_watchdog_linux_events_unrelated",
            endpoint=endpoint_guid,
            event_count=len(scan.events),
            severities=severities,
            patterns=pattern_names,
        )
        return

    logger.warning(
        "voice_driver_watchdog_linux_events_correlated",
        endpoint=endpoint_guid,
        event_count=len(scan.events),
        severities=severities,
        patterns=pattern_names,
        alsa_card_id=alsa_card_id,
        usb_vid_pid=usb_vid_pid,
        codec_vendor_id=codec_vendor_id,
        remediation=(
            "journalctl -k surfaced audio distress events correlated "
            "to this endpoint within the lookback window. Inspect the "
            "pattern names (hda_codec_timeout, usb_descriptor_read_fail, "
            "etc.) and consider replugging / reloading the audio module "
            "before further debugging cascade outcomes."
        ),
    )


def _extract_macos_watchdog_hints(
    *,
    device_name: str,
    endpoint_guid: str,
) -> tuple[str | None, str | None]:
    """Extract ``(device_uid, device_name)`` hints for macOS scan
    correlation.

    Used on macOS to tie ``log show`` distress events to the device
    the cascade is about to probe. Hints are best-effort:

    * ``device_uid`` — back-parsed from the composed endpoint GUID.
      The fingerprint format is internally owned by
      :mod:`sovyx.voice.health._fingerprint_macos`; a shape change
      there must keep this regex in sync (tests pin the expected
      forms).
    * ``device_name`` — passed through verbatim from the caller. The
      PortAudio / CoreAudio name is what ``log show`` usually carries
      in ``name=…`` hints.

    We intentionally don't try to derive USB VID:PID on macOS: the
    fingerprint doesn't carry it, and matching substrings against
    VID:PID-style hints in log output has a much worse
    precision/recall tradeoff than matching against the deviceUID or
    human name.
    """
    device_uid: str | None = None
    match = _MACOS_FP_DEVICE_RE.match(endpoint_guid)
    if match is not None:
        uid = match.group("uid").strip()
        if uid:
            device_uid = uid

    stripped_name = device_name.strip() or None
    return device_uid, stripped_name


async def _log_macos_driver_watchdog_scan(
    *,
    device_name: str,
    endpoint_guid: str,
    lookback_hours: int,
    timeout_s: float,
) -> None:
    """Detection-tier scan of ``log show`` for coreaudiod distress.

    Peer of :func:`_log_linux_driver_watchdog_scan`. Same four-state
    log surface — *unavailable*, *clean*, *events-unrelated*,
    *events-correlated* — keyed off a macOS scan. No behaviour
    change: macOS has no user-space "safer mode" to fall back to, so
    the signal is pure observability for post-incident triage.

    Exceptions from the scan are logged at DEBUG and swallowed so
    boot never blocks on the best-effort diagnostic.
    """
    from sovyx.voice.health._driver_watchdog_macos import (
        scan_recent_macos_driver_watchdog_events,
    )

    try:
        scan = await scan_recent_macos_driver_watchdog_events(
            lookback_hours=lookback_hours,
            timeout_s=timeout_s,
        )
    except Exception:  # noqa: BLE001 — scan is best-effort, never block boot
        logger.debug(
            "voice_driver_watchdog_macos_scan_raised",
            exc_info=True,
        )
        return

    if not scan.scan_attempted or scan.scan_failed:
        logger.debug(
            "voice_driver_watchdog_macos_unavailable",
            endpoint=endpoint_guid,
            attempted=scan.scan_attempted,
            failed=scan.scan_failed,
        )
        return

    if not scan.any_events:
        logger.debug(
            "voice_driver_watchdog_macos_clean",
            endpoint=endpoint_guid,
            lookback_hours=lookback_hours,
        )
        return

    device_uid, hinted_name = _extract_macos_watchdog_hints(
        device_name=device_name,
        endpoint_guid=endpoint_guid,
    )
    correlated = scan.matches_device(
        device_uid=device_uid,
        device_name=hinted_name,
    )

    severities = sorted({event.severity for event in scan.events})
    pattern_names = sorted({event.pattern_name for event in scan.events})

    if not correlated:
        logger.info(
            "voice_driver_watchdog_macos_events_unrelated",
            endpoint=endpoint_guid,
            event_count=len(scan.events),
            severities=severities,
            patterns=pattern_names,
        )
        return

    logger.warning(
        "voice_driver_watchdog_macos_events_correlated",
        endpoint=endpoint_guid,
        event_count=len(scan.events),
        severities=severities,
        patterns=pattern_names,
        device_uid=device_uid,
        device_name=hinted_name,
        remediation=(
            "log show surfaced coreaudiod distress events correlated "
            "to this endpoint within the lookback window. Inspect the "
            "pattern names (hal_io_engine_error, aggregate_device_"
            "disconnected, coreaudiod_watchdog_reset, etc.) and "
            "consider replugging the device or restarting coreaudiod "
            "(`sudo killall coreaudiod`) before further debugging "
            "cascade outcomes."
        ),
    )


__all__ = [
    "_LINUX_FP_CODEC_RE",
    "_LINUX_FP_USB_RE",
    "_MACOS_FP_DEVICE_RE",
    "_autofix_after_driver_watchdog_scan",
    "_derive_windows_usb_vid_pid",
    "_extract_linux_watchdog_hints",
    "_extract_macos_watchdog_hints",
    "_log_linux_driver_watchdog_scan",
    "_log_macos_driver_watchdog_scan",
]
