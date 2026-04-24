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

import hashlib
import re
import sys
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING

from sovyx.observability.logging import get_logger
from sovyx.voice.health._quarantine import EndpointQuarantine, get_default_quarantine
from sovyx.voice.health.capture_overrides import CaptureOverrides
from sovyx.voice.health.cascade import run_cascade, run_cascade_for_candidates
from sovyx.voice.health.combo_store import ComboStore
from sovyx.voice.health.contract import CandidateEndpoint, CascadeResult, ProbeMode

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence
    from pathlib import Path

    from sovyx.engine.config import VoiceTuningConfig
    from sovyx.voice._apo_detector import CaptureApoReport
    from sovyx.voice.device_enum import DeviceEntry
    from sovyx.voice.health._mixer_sanity import MixerSanitySetup

logger = get_logger(__name__)


# Relative paths under ``data_dir`` per ADR-combo-store-schema.md §2.
_COMBO_STORE_FILENAME = "voice/capture_combos.json"
_CAPTURE_OVERRIDES_FILENAME = "voice/capture_overrides.json"


def resolve_combo_store_path(data_dir: Path) -> Path:
    """Return the canonical ``capture_combos.json`` path under ``data_dir``."""
    return data_dir / _COMBO_STORE_FILENAME


def resolve_capture_overrides_path(data_dir: Path) -> Path:
    """Return the canonical ``capture_overrides.json`` path under ``data_dir``."""
    return data_dir / _CAPTURE_OVERRIDES_FILENAME


async def _autofix_after_driver_watchdog_scan(
    *,
    resolved_name: str,
    device_interface_name: str,
    lookback_hours: int,
    timeout_s: float,
) -> bool:
    """Return the ``voice_clarity_autofix`` flag after the watchdog scan.

    Encapsulates the Phase-3 pre-flight decision so :func:`run_boot_cascade`
    stays focused on cascade orchestration. Any failure in the scan
    (subprocess error, timeout, parse) falls through to ``True`` (the
    untouched tuning default) — we never make the cascade safer *by
    accident*; we only drop to shared-mode when we have positive
    evidence that the driver is fragile.
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
    # APO-bypass on the USB headset.
    targeted = scan.matches_device(device_interface_name)
    if not targeted:
        logger.info(
            "voice_driver_watchdog_preflight_unrelated",
            device=resolved_name,
            event_count=len(scan.events),
        )
        return True
    logger.warning(
        "voice_driver_watchdog_preflight_downgrade",
        device=resolved_name,
        device_interface_name=device_interface_name,
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


def derive_endpoint_guid(
    resolved: DeviceEntry,
    *,
    apo_reports: list[CaptureApoReport] | None = None,
    platform_key: str | None = None,
) -> str:
    """Return a stable identifier for ``resolved`` across boots.

    Resolution order (ADR §1 endpoint_guid semantics):

    1. **Windows + APO report**: use the MMDevices endpoint GUID
       from the matched :class:`~sovyx.voice._apo_detector.CaptureApoReport`.
       Canonical identifier used throughout the Windows audit trail.

    2. **Linux, sysfs-available**: use
       :func:`~sovyx.voice.health._fingerprint_linux.compute_linux_endpoint_fingerprint`
       which derives a stable ID from ``/sys/class/sound/card<N>/device``
       symlink targets — PCI BDF for onboard codecs, USB VID:PID for
       USB-audio. Returns the equivalent of an MMDevice GUID on
       Linux: ``{linux-pci-<bdf>-codec-<vendor>:<device>-<pcm>-<dir>}``
       or ``{linux-usb-<vid>:<pid>-<pcm>-<dir>}``. Falls through when
       the device is a virtual alias (``"default"`` / ``"pulse"``) or
       sysfs is inaccessible.

    3. **Fallback surrogate** — SHA256 over
       ``(canonical_name, host_api_name, platform_key)`` formatted as
       ``{surrogate-8-4-4-4-12}``. Stable enough to survive normal
       reboots (``canonical_name`` is MME-truncation-normalised) but
       brittle against hotplug reorder, PipeWire/PulseAudio naming
       changes, and cross-host-API queries for the same physical
       device.

    The three distinct visual prefixes (``{...win GUID}`` /
    ``{linux-...}`` / ``{surrogate-...}``) let operators reading logs
    tell at a glance which mechanism produced the record. ComboStore
    accepts any non-empty string per its R12 sanity rule.
    """
    plat = platform_key or sys.platform

    if plat == "win32" and apo_reports:
        from sovyx.voice._apo_detector import find_endpoint_report

        report = find_endpoint_report(apo_reports, device_name=resolved.name)
        if report is not None and report.endpoint_id:
            return report.endpoint_id

    if plat == "linux":
        from sovyx.voice.health._fingerprint_linux import (  # noqa: PLC0415 — lazy-Linux
            compute_linux_endpoint_fingerprint,
        )

        linux_fp = compute_linux_endpoint_fingerprint(resolved)
        if linux_fp is not None:
            return linux_fp

    if plat == "darwin":
        from sovyx.voice.health._fingerprint_macos import (  # noqa: PLC0415 — lazy-Darwin
            compute_macos_endpoint_fingerprint,
        )

        macos_fp = compute_macos_endpoint_fingerprint(resolved)
        if macos_fp is not None:
            return macos_fp

    hasher = hashlib.sha256()
    hasher.update(resolved.canonical_name.encode("utf-8"))
    hasher.update(b"\x1f")
    hasher.update(resolved.host_api_name.encode("utf-8"))
    hasher.update(b"\x1f")
    hasher.update(plat.encode("utf-8"))
    digest = hasher.hexdigest()
    return (
        "{surrogate-"
        f"{digest[0:8]}-{digest[8:12]}-{digest[12:16]}-{digest[16:20]}-{digest[20:32]}"
        "}"
    )


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
            store = ComboStore(resolve_combo_store_path(data_dir))
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
            store = ComboStore(resolve_combo_store_path(data_dir))
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


# ── Boot-cascade verdict (v0.20.2 §4.4.7 / Bug D) ─────────────────────


class CascadeBootVerdict(StrEnum):
    """Three-state classification of a :class:`CascadeResult` for boot.

    The factory uses this to decide whether to construct an
    :class:`AudioCaptureTask` or raise
    :class:`~sovyx.voice.CaptureInoperativeError`. Pre-v0.20.2, the
    factory only logged ``has_winner`` and booted unconditionally — the
    legacy opener then fell back to MME shared, masking a deaf mic
    behind a fake "pipeline created" log.

    Members:
        HEALTHY: Cascade picked a winning combo (``source`` is
            ``"pinned"`` / ``"store"`` / ``"cascade"`` and
            ``winning_combo`` is set). Safe to boot.
        DEGRADED: Cascade declined to run (``None`` result —
            unsupported platform, store init failed). The legacy opener
            still owns the path; we boot but the pipeline is unvalidated.
        INOPERATIVE: Cascade ran and exhausted every viable combo
            (``source == "none"``) or kernel-invalidated fail-over
            yielded no alternative endpoint. Booting would silently
            produce a deaf pipeline.
    """

    HEALTHY = "healthy"
    DEGRADED = "degraded"
    INOPERATIVE = "inoperative"


@dataclass(frozen=True, slots=True)
class CascadeBootOutcome:
    """Boot-cascade verdict + structured reason for downstream callers.

    Returned by :func:`classify_cascade_boot_result`. The factory
    inspects :attr:`verdict` to decide between booting the pipeline,
    booting in degraded mode, or raising
    :class:`~sovyx.voice.CaptureInoperativeError`. The dashboard
    surfaces :attr:`reason` and :attr:`attempts` in the 503 body so
    the UI can show a meaningful "no working microphone" prompt rather
    than the generic stack-trace path.

    Attributes:
        verdict: Three-state :class:`CascadeBootVerdict`.
        reason: Stable string tag — ``"winner"`` (HEALTHY),
            ``"cascade_declined"`` (DEGRADED, no result),
            ``"no_winner"`` (INOPERATIVE, cascade exhausted),
            ``"no_alternative_endpoint"`` (INOPERATIVE, fail-over
            yielded nothing). Stable enough for dashboard i18n keys.
        attempts: Cascade probe attempt count (``0`` when the cascade
            never ran). Useful for triage.
        result: The underlying :class:`CascadeResult`, or ``None`` when
            the cascade declined to run. Kept for callers that want
            access to the full attempt list / per-combo diagnoses.
    """

    verdict: CascadeBootVerdict
    reason: str
    attempts: int
    result: CascadeResult | None


def classify_cascade_boot_result(
    result: CascadeResult | None,
) -> CascadeBootOutcome:
    """Classify a :class:`CascadeResult` into a :class:`CascadeBootOutcome`.

    Used by the factory to gate :class:`AudioCaptureTask` construction
    on the cascade verdict (v0.20.2 §4.4.7 / Bug D). Pure helper — no
    side effects, no dependency on factory state.

    Decision matrix:

    * ``result is None`` → DEGRADED (``cascade_declined``). The cascade
      could not run — unsupported platform, store init failed,
      dispatch raised. The legacy opener owns the path.
    * ``result.winning_combo is not None`` → HEALTHY (``winner``). Any
      ``source`` that produced a winner counts (pinned / store /
      cascade walk).
    * ``result.source == "quarantined"`` → INOPERATIVE
      (``no_alternative_endpoint``). The kernel-invalidated fail-over
      already ran inside :func:`_run_vchl_boot_cascade` and either
      returned a healthy alternative (would have set ``winning_combo``
      via the re-cascade) or could not find one. Reaching here means
      the latter: no viable mic exists.
    * otherwise → INOPERATIVE (``no_winner``). The cascade exhausted
      every combo and every probe failed. Booting would produce a
      deaf pipeline.
    """
    if result is None:
        return CascadeBootOutcome(
            verdict=CascadeBootVerdict.DEGRADED,
            reason="cascade_declined",
            attempts=0,
            result=None,
        )
    if result.winning_combo is not None:
        return CascadeBootOutcome(
            verdict=CascadeBootVerdict.HEALTHY,
            reason="winner",
            attempts=result.attempts_count,
            result=result,
        )
    if result.source == "quarantined":
        return CascadeBootOutcome(
            verdict=CascadeBootVerdict.INOPERATIVE,
            reason="no_alternative_endpoint",
            attempts=result.attempts_count,
            result=result,
        )
    return CascadeBootOutcome(
        verdict=CascadeBootVerdict.INOPERATIVE,
        reason="no_winner",
        attempts=result.attempts_count,
        result=result,
    )


def select_alternative_endpoint(
    *,
    kind: str = "input",
    apo_reports: list[CaptureApoReport] | None = None,
    platform_key: str | None = None,
    exclude_endpoint_guids: Iterable[str] = (),
    exclude_physical_device_ids: Iterable[str] = (),
    quarantine: EndpointQuarantine | None = None,
) -> DeviceEntry | None:
    """Pick a non-quarantined alternative ``DeviceEntry`` for fail-over.

    ADR §4.4.7. After the boot cascade returns ``source="quarantined"``
    for the OS-default capture endpoint, the factory needs a next-best
    device so the pipeline can still come up. Resolution order:

    1. Any non-excluded, non-quarantined input device that the OS marks
       as default. (``is_os_default`` survives even after dedup, so a
       USB headset that's the OS default beats the laptop array mic.)
    2. Otherwise the host-API-preferred candidate per
       :func:`~sovyx.voice.device_enum.pick_preferred`.

    The physical-device scope (``exclude_physical_device_ids`` +
    :meth:`~sovyx.voice.health._quarantine.EndpointQuarantine.is_quarantined_physical`)
    is the enterprise-grade safety net added in v0.20.4 after the
    Razer BlackShark V2 Pro kernel-reset incident: PortAudio exposes a
    single physical microphone through up to four host APIs, each with
    a distinct :func:`derive_endpoint_guid` surrogate. When a driver
    wedges, *every* alias fails; without physical-scope filtering the
    factory would fail over to a surrogate alias and re-cascade into
    the same driver, re-triggering the kernel hard-reset.

    Args:
        kind: ``"input"`` for capture, ``"output"`` for playback. The
            quarantine is capture-only today, but the helper accepts
            ``kind`` so the playback factory can reuse the skeleton.
        apo_reports: Pre-computed capture-APO reports (Windows only).
            Forwarded to :func:`derive_endpoint_guid` so we resolve
            real MMDevice GUIDs rather than surrogates whenever possible.
        platform_key: Override the runtime platform (tests).
        exclude_endpoint_guids: Endpoint GUIDs to skip on top of the
            quarantine — typically the GUID of the device that just
            got quarantined in this same boot, to keep the fail-over
            decision deterministic.
        exclude_physical_device_ids: Physical-device identities
            (``DeviceEntry.canonical_name``) to skip regardless of which
            host-API alias they are exposed through. The factory passes
            the quarantined device's canonical name here so every MME /
            DirectSound / WASAPI / WDM-KS surrogate of the same
            microphone is rejected atomically.
        quarantine: §4.4.7 store. ``None`` falls back to the process
            singleton.

    Returns ``None`` when no viable alternative exists (every device
    quarantined, or no input devices at all). Caller treats that as
    "boot in degraded mode and surface a wizard prompt".
    """
    from sovyx.voice.device_enum import enumerate_devices, pick_preferred

    plat = platform_key or sys.platform
    entries = enumerate_devices()
    if not entries:
        return None
    q = quarantine if quarantine is not None else get_default_quarantine()
    excluded = set(exclude_endpoint_guids)
    excluded_physical = {p for p in exclude_physical_device_ids if p}

    def _is_skippable(entry: DeviceEntry) -> bool:
        guid = derive_endpoint_guid(
            entry,
            apo_reports=apo_reports,
            platform_key=plat,
        )
        if guid in excluded or q.is_quarantined(guid):
            return True
        # Physical-device scope: reject any alias whose canonical
        # (MME-truncation-normalised) name matches a quarantined or
        # caller-excluded physical device. See the Razer kernel-reset
        # post-mortem in the docstring.
        physical = entry.canonical_name
        if physical and physical in excluded_physical:
            return True
        return bool(physical and q.is_quarantined_physical(physical))

    candidates = [e for e in entries if not _is_skippable(e)]
    preferred = pick_preferred(candidates, kind=kind)
    if not preferred:
        return None
    defaults = [e for e in preferred if e.is_os_default]
    if defaults:
        return defaults[0]
    return preferred[0]


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
