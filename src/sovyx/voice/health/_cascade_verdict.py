"""Boot-cascade verdict classification + alternative-endpoint selection.

Phase 5.F.3 god-file extraction from
``voice/health/_factory_integration.py`` (anti-pattern #16).

Owns two related responsibilities at the post-cascade decision boundary:

1. **Verdict classification** —
   :class:`CascadeBootVerdict` (HEALTHY / DEGRADED / INOPERATIVE) +
   :class:`CascadeBootOutcome` (verdict + reason + attempts + result)
   + :func:`classify_cascade_boot_result`. Pure helpers — no side
   effects, no dependency on factory state. The factory uses the
   verdict to decide between booting the pipeline, booting in
   degraded mode, or raising :class:`CaptureInoperativeError`.

2. **Alternative-endpoint selection** —
   :func:`select_alternative_endpoint`. After the boot cascade returns
   ``source="quarantined"`` for the OS-default capture endpoint, the
   factory needs a next-best device so the pipeline can still come up.
   Honours both endpoint-GUID and physical-device quarantine scopes
   (Razer BlackShark V2 Pro kernel-reset post-mortem; v0.20.4).

Anti-pattern #20 covered: 6 production callers
(``factory/_validate.py`` + ``capture/_restart_mixin.py`` +
``health/_runtime_failover.py``) plus 3 test patches at
``sovyx.voice.health._factory_integration.select_alternative_endpoint``
continue to resolve via the parent module's re-export shim.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING

from sovyx.voice.health._endpoint_guid import derive_endpoint_guid
from sovyx.voice.health._quarantine import EndpointQuarantine, get_default_quarantine

if TYPE_CHECKING:
    from collections.abc import Iterable

    from sovyx.voice._apo_detector import CaptureApoReport
    from sovyx.voice.device_enum import DeviceEntry
    from sovyx.voice.health.contract import CascadeResult


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
    "select_alternative_endpoint",
]
