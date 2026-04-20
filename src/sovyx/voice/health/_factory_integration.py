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
import sys
from typing import TYPE_CHECKING

from sovyx.observability.logging import get_logger
from sovyx.voice.health._quarantine import EndpointQuarantine, get_default_quarantine
from sovyx.voice.health.capture_overrides import CaptureOverrides
from sovyx.voice.health.cascade import run_cascade
from sovyx.voice.health.combo_store import ComboStore
from sovyx.voice.health.contract import CascadeResult, ProbeMode

if TYPE_CHECKING:
    from collections.abc import Iterable
    from pathlib import Path

    from sovyx.engine.config import VoiceTuningConfig
    from sovyx.voice._apo_detector import CaptureApoReport
    from sovyx.voice.device_enum import DeviceEntry

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


def derive_endpoint_guid(
    resolved: DeviceEntry,
    *,
    apo_reports: list[CaptureApoReport] | None = None,
    platform_key: str | None = None,
) -> str:
    """Return a stable identifier for ``resolved`` across boots.

    Resolution order (ADR §1 endpoint_guid semantics):

    1. On Windows, when an APO report is available for the device's
       friendly name, use the Windows MMDevices endpoint GUID
       (``PKEY_Device_FriendlyName`` match → registry subkey). This is
       the canonical identifier used by :mod:`sovyx.voice.health._fingerprint`
       for per-endpoint SHA256 fingerprinting.

    2. Otherwise, derive a SHA256 surrogate from
       ``(canonical_name, host_api_name, platform_key)`` and format it
       as ``{surrogate-8-4-4-4-12}``. Stable across boots because
       ``canonical_name`` is the MME-normalised device name (not the
       ephemeral PortAudio index) and ``host_api_name`` is the label
       PortAudio reports from its host-API table.

    The surrogate is visually distinct from real Windows GUIDs so
    operators reading logs can tell at a glance whether a real MMDevice
    GUID was available. ComboStore accepts any non-empty string per
    its R12 sanity rule.
    """
    plat = platform_key or sys.platform

    if plat == "win32" and apo_reports:
        from sovyx.voice._apo_detector import find_endpoint_report

        report = find_endpoint_report(apo_reports, device_name=resolved.name)
        if report is not None and report.endpoint_id:
            return report.endpoint_id

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
            combo_store=store,
            capture_overrides=overrides,
            total_budget_s=tuning.cascade_total_budget_s,
            attempt_budget_s=tuning.cascade_attempt_budget_s,
            voice_clarity_autofix=tuning.voice_clarity_autofix,
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


def select_alternative_endpoint(
    *,
    kind: str = "input",
    apo_reports: list[CaptureApoReport] | None = None,
    platform_key: str | None = None,
    exclude_endpoint_guids: Iterable[str] = (),
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

    def _is_skippable(entry: DeviceEntry) -> bool:
        guid = derive_endpoint_guid(
            entry,
            apo_reports=apo_reports,
            platform_key=plat,
        )
        return guid in excluded or q.is_quarantined(guid)

    candidates = [e for e in entries if not _is_skippable(e)]
    preferred = pick_preferred(candidates, kind=kind)
    if not preferred:
        return None
    defaults = [e for e in preferred if e.is_os_default]
    if defaults:
        return defaults[0]
    return preferred[0]


__all__ = [
    "derive_endpoint_guid",
    "resolve_capture_overrides_path",
    "resolve_combo_store_path",
    "run_boot_cascade",
    "select_alternative_endpoint",
]
