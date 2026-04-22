"""Candidate-set construction for the cascade.

Produces the ordered :class:`~sovyx.voice.health.contract.CandidateEndpoint`
list the cascade iterates — one candidate per endpoint the opener should
consider before declaring the capture path inoperative. The builder is
the mechanism that eliminates **VLX-002** (``_device_chain`` only
iterating canonical-name siblings) by making cross-PCM alternatives
first-class citizens of the cascade layer rather than implicit
behaviour of the opener.

Ordering semantics (Linux):

1. The user-resolved device (``USER_PREFERRED``, rank 0) — always
   present. Respects :attr:`~sovyx.voice.device_enum.DeviceKind.UNKNOWN`.
2. Canonical siblings of the resolved device (``CANONICAL_SIBLING``) —
   same :attr:`canonical_name`, different ``host_api_name``. Dominant
   on Windows; typically empty on modern Linux where PortAudio exposes
   only the ``ALSA`` host API.
3. Session-manager virtuals (``SESSION_MANAGER_VIRTUAL``) — ``pipewire``,
   ``pulse``, ``jack`` PCMs. **Only added when the resolved device is
   HARDWARE** (not duplicating — the user already chose a virtual).
4. OS default (``OS_DEFAULT``) — the ALSA ``default`` alias. **Only
   added when resolved is HARDWARE**.
5. Any remaining enumerated input device (``FALLBACK``). Tail of the
   list; rarely reached; exists so the cascade has a non-empty fallback
   even on exotic enumerations.

Dedup: the composite key ``(device_index, host_api_name)`` is unique in
the output. First occurrence wins — if USER_PREFERRED already claimed
``(4, "ALSA")``, a later FALLBACK pass never re-adds it.

Windows / macOS: the builder returns ``[resolved, *canonical_siblings]``
— equivalent to the pre-refactor behaviour. ``DeviceKind.UNKNOWN`` short
circuits the Linux-specific branches.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from sovyx.observability.logging import get_logger
from sovyx.voice.device_enum import DeviceKind
from sovyx.voice.health._factory_integration import derive_endpoint_guid
from sovyx.voice.health.contract import CandidateEndpoint, CandidateSource

if TYPE_CHECKING:
    from collections.abc import Sequence

    from sovyx.voice._apo_detector import CaptureApoReport
    from sovyx.voice.device_enum import DeviceEntry

logger = get_logger(__name__)


def build_capture_candidates(
    *,
    resolved: DeviceEntry,
    all_devices: Sequence[DeviceEntry],
    platform_key: str,
    apo_reports: Sequence[CaptureApoReport] = (),
) -> list[CandidateEndpoint]:
    """Return the ordered candidate set for capturing on ``resolved``.

    The set always has at least one entry (the resolved device itself);
    callers can rely on ``candidates[0].source == USER_PREFERRED`` and
    ``candidates[0].device_index == resolved.index``.

    Args:
        resolved: The :class:`DeviceEntry` returned by
            :func:`~sovyx.voice.device_enum.resolve_device`. Must have
            :attr:`max_input_channels` > 0 (the cascade never considers
            output-only endpoints for capture).
        all_devices: The enumeration snapshot the builder will search
            for siblings and virtuals. Callers pass the output of
            :func:`~sovyx.voice.device_enum.enumerate_devices` from the
            **same** invocation — stale snapshots produce stale
            ``device_index`` values.
        platform_key: ``"linux"``, ``"win32"``, ``"darwin"``. Changes
            the branch taken for session-manager / OS-default insertion.
        apo_reports: Windows-specific APO fingerprints. Forwarded to
            :func:`derive_endpoint_guid` so the winning candidate's
            ComboStore entry carries the right MMDevices GUID. Empty on
            Linux / macOS.

    Returns:
        A list of :class:`CandidateEndpoint` in iteration order. Every
        entry has a unique ``(device_index, host_api_name)`` pair and a
        deterministic ``endpoint_guid``.

    Raises:
        ValueError: ``resolved`` has no input channels (programmer
            error — callers are expected to pre-filter via
            :func:`resolve_device` with ``kind="input"``).
    """
    if resolved.max_input_channels <= 0:
        msg = (
            f"resolved device {resolved.name!r} has no input channels "
            f"(max_input_channels={resolved.max_input_channels}); "
            "build_capture_candidates is for capture endpoints only"
        )
        raise ValueError(msg)

    seen_keys: set[tuple[int, str]] = set()
    candidates: list[CandidateEndpoint] = []

    def _append(entry: DeviceEntry, source: CandidateSource) -> None:
        key = (entry.index, entry.host_api_name)
        if key in seen_keys:
            return
        seen_keys.add(key)
        candidates.append(
            CandidateEndpoint(
                device_index=entry.index,
                host_api_name=entry.host_api_name,
                kind=entry.kind,
                canonical_name=entry.canonical_name,
                friendly_name=entry.name,
                source=source,
                preference_rank=len(candidates),
                endpoint_guid=derive_endpoint_guid(
                    entry,
                    apo_reports=list(apo_reports) if apo_reports else None,
                    platform_key=platform_key,
                ),
                default_samplerate=entry.default_samplerate,
            )
        )

    # 1. USER_PREFERRED — always rank 0.
    _append(resolved, CandidateSource.USER_PREFERRED)

    # 2. Canonical siblings — same physical device under different host APIs.
    siblings = [
        e
        for e in all_devices
        if (
            e.canonical_name == resolved.canonical_name
            and e.max_input_channels > 0
            and e.index != resolved.index
        )
    ]
    # Stable ordering: by the device_enum preference rank (WASAPI >
    # DirectSound > MME on Windows; ALSA > PipeWire > PulseAudio > JACK
    # on Linux) — not by PortAudio enumeration order which is unstable.
    from sovyx.voice.device_enum import _host_api_rank

    siblings.sort(key=lambda e: (_host_api_rank(e.host_api_name), e.index))
    for sibling in siblings:
        _append(sibling, CandidateSource.CANONICAL_SIBLING)

    # 3 + 4 — Linux-only session-manager + OS-default fallbacks. Skipped
    # when the resolved device is already a virtual or the default (we
    # don't duplicate the same PCM under a different source label).
    if platform_key == "linux" and resolved.kind == DeviceKind.HARDWARE:
        session_virtuals = [
            e
            for e in all_devices
            if e.kind == DeviceKind.SESSION_MANAGER_VIRTUAL and e.max_input_channels > 0
        ]
        # Within the virtual group, sort by name so "pipewire" comes
        # before "pulse" alphabetically — stable, easy to reason about.
        session_virtuals.sort(key=lambda e: (e.canonical_name, e.index))
        for virtual in session_virtuals:
            _append(virtual, CandidateSource.SESSION_MANAGER_VIRTUAL)

        os_defaults = [
            e for e in all_devices if e.kind == DeviceKind.OS_DEFAULT and e.max_input_channels > 0
        ]
        os_defaults.sort(key=lambda e: (e.canonical_name, e.index))
        for default in os_defaults:
            _append(default, CandidateSource.OS_DEFAULT)

    # 5. FALLBACK — any remaining enumerated input not already claimed.
    #    Dedup by (device_index, host_api_name) guarantees no duplicates;
    #    we sort by host-API rank so the "best" remaining device comes
    #    first in the tail.
    remaining = [
        e
        for e in all_devices
        if e.max_input_channels > 0 and (e.index, e.host_api_name) not in seen_keys
    ]
    remaining.sort(key=lambda e: (_host_api_rank(e.host_api_name), e.index))
    for extra in remaining:
        _append(extra, CandidateSource.FALLBACK)

    logger.info(
        "voice_boot_candidates_built",
        platform=platform_key,
        resolved_index=resolved.index,
        resolved_host_api=resolved.host_api_name,
        resolved_kind=str(resolved.kind),
        candidate_count=len(candidates),
        candidates=[
            {
                "rank": c.preference_rank,
                "device_index": c.device_index,
                "host_api": c.host_api_name,
                "kind": str(c.kind),
                "source": str(c.source),
                "friendly_name": c.friendly_name,
                "endpoint_guid": c.endpoint_guid,
            }
            for c in candidates
        ],
    )
    return candidates


__all__ = ["build_capture_candidates"]
