"""§4.4.7 kernel-invalidated endpoint quarantine — in-memory bounded store.

ADR §4.4.7 writeup: When an audio endpoint's kernel-side IAudioClient is
in an invalidated state (``paInvalidDevice`` / -9996 at
:meth:`IAudioClient::Initialize` despite the PnP layer reporting the
device as healthy), no user-mode path can recover it. The only cures
are physical — a USB replug, a ``pnputil /restart-device`` reboot, or a
driver reload. Sovyx cannot auto-recover, but it can *stop wasting
probes* on the dead endpoint and fail-over to the next viable capture
device until the user fixes the hardware.

This module provides a small in-memory quarantine store:

* **Bounded** — an :class:`~sovyx.engine._lock_dict.LRULockDict`-style
  cap (default 64, matching ``cascade_lifecycle_lock_max``) so a
  pathological flood of endpoint GUIDs cannot leak memory over a
  long-lived daemon (anti-pattern #15).
* **Timestamp-gated** — each entry expires after
  :attr:`VoiceTuningConfig.kernel_invalidated_quarantine_s`. On expiry
  the entry is evicted on next lookup; the watchdog L4 recheck loop
  pokes the store periodically to clear fresh expiries.
* **Hot-plug-clearable** — the watchdog removes an endpoint from
  quarantine when an OS-level DEVICE_REMOVED or DEVICE_ADDED event
  targets it, because a physical replug is the canonical cure.
* **Process-local** — the store is *not* persisted. Kernel-invalidated
  state is per-boot by definition (a reboot is one of the two cures),
  so re-enqueuing on next launch is exactly the desired behaviour.

The store is thread-safe under asyncio: all mutations happen on the
event loop, and the underlying :class:`dict` is not shared across
threads. The :class:`EndpointQuarantine` API is intentionally minimal —
``add``, ``is_quarantined``, ``clear``, ``snapshot`` — so callers in
:mod:`sovyx.voice.health.cascade`, :mod:`sovyx.voice.health.watchdog`,
and :mod:`sovyx.voice.health._factory_integration` share a single
vocabulary.
"""

from __future__ import annotations

import time
from collections import OrderedDict
from dataclasses import dataclass
from typing import TYPE_CHECKING

from sovyx.observability.logging import get_logger

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable


logger = get_logger(__name__)


_DEFAULT_MAXSIZE = 64
"""Upper bound on tracked endpoints — mirrors ``cascade_lifecycle_lock_max``."""


@dataclass(frozen=True, slots=True)
class QuarantineEntry:
    """Immutable snapshot of a quarantine record.

    Attributes:
        endpoint_guid: The :func:`~sovyx.voice.health._factory_integration.derive_endpoint_guid`
            identifier of the invalidated endpoint.
        device_friendly_name: Best-known friendly label ("Razer BlackShark
            V2 Pro", "Microfone (Razer BlackShark V2 Pro)"). Empty when
            the cascade didn't surface a friendly name at quarantine time.
        device_interface_name: PnP device interface path (Windows). Empty
            on other platforms. Used by operator-facing alert copy.
        host_api: Host API of the *failing* probe combo (``"Windows
            WASAPI"``, ``"Windows DirectSound"``, ...). Informational —
            every host API fails equally on a kernel-invalidated endpoint.
        added_at_monotonic: :func:`time.monotonic` value at quarantine
            time. Monotonic clock is immune to wall-clock jumps during
            daylight-savings transitions or NTP corrections.
        expires_at_monotonic: Monotonic deadline. Entries past this are
            evicted lazily on lookup.
        reason: Short tag describing the trigger — ``"probe"`` (normal
            cascade path), ``"watchdog_recheck"`` (periodic retry still
            failing), ``"factory_integration"`` (boot-time cascade).
    """

    endpoint_guid: str
    device_friendly_name: str
    device_interface_name: str
    host_api: str
    added_at_monotonic: float
    expires_at_monotonic: float
    reason: str


class EndpointQuarantine:
    """Bounded in-memory store of kernel-invalidated endpoints.

    Args:
        quarantine_s: How long each entry stays in quarantine before
            expiring. Sourced from
            :attr:`VoiceTuningConfig.kernel_invalidated_quarantine_s`.
        maxsize: Upper bound on tracked endpoints. Older entries are
            evicted LRU-style on insert when the cap is reached. Defaults
            to :data:`_DEFAULT_MAXSIZE`.
        clock: Injected monotonic clock for tests. Production code passes
            ``None`` and :func:`time.monotonic` is used.
    """

    def __init__(
        self,
        *,
        quarantine_s: float,
        maxsize: int = _DEFAULT_MAXSIZE,
        clock: Callable[[], float] | None = None,
    ) -> None:
        if quarantine_s <= 0:
            msg = f"quarantine_s must be positive, got {quarantine_s}"
            raise ValueError(msg)
        if maxsize <= 0:
            msg = f"maxsize must be positive, got {maxsize}"
            raise ValueError(msg)
        self._quarantine_s = quarantine_s
        self._maxsize = maxsize
        self._clock = clock if clock is not None else time.monotonic
        # OrderedDict preserves insertion order, so LRU eviction is a
        # single ``popitem(last=False)`` away. Lookups don't reorder —
        # expiry, not access recency, drives eviction.
        self._entries: OrderedDict[str, QuarantineEntry] = OrderedDict()

    # ── Mutations ───────────────────────────────────────────────────────

    def add(
        self,
        *,
        endpoint_guid: str,
        device_friendly_name: str = "",
        device_interface_name: str = "",
        host_api: str = "",
        reason: str = "probe",
    ) -> QuarantineEntry:
        """Add ``endpoint_guid`` to quarantine and return the new entry.

        Replaces any existing entry for the same GUID — the fresh
        ``added_at`` resets the quarantine clock, which is desirable
        because a repeat KERNEL_INVALIDATED observation means the
        underlying condition has not cleared.

        Evicts the oldest entry (by insertion order) when the store is
        at capacity.
        """
        if not endpoint_guid:
            msg = "endpoint_guid must be a non-empty string"
            raise ValueError(msg)
        now = self._clock()
        entry = QuarantineEntry(
            endpoint_guid=endpoint_guid,
            device_friendly_name=device_friendly_name,
            device_interface_name=device_interface_name,
            host_api=host_api,
            added_at_monotonic=now,
            expires_at_monotonic=now + self._quarantine_s,
            reason=reason,
        )
        # Pop-and-reinsert keeps ordering stable whether this is a new
        # entry or a replacement — OrderedDict would otherwise preserve
        # the original position on bare assignment.
        self._entries.pop(endpoint_guid, None)
        self._entries[endpoint_guid] = entry
        if len(self._entries) > self._maxsize:
            evicted_guid, evicted_entry = self._entries.popitem(last=False)
            logger.info(
                "voice_quarantine_evicted_for_capacity",
                endpoint=evicted_guid,
                friendly_name=evicted_entry.device_friendly_name,
                maxsize=self._maxsize,
            )
        logger.warning(
            "voice_endpoint_quarantined",
            endpoint=endpoint_guid,
            friendly_name=device_friendly_name,
            interface_name=device_interface_name,
            host_api=host_api,
            reason=reason,
            quarantine_s=self._quarantine_s,
        )
        return entry

    def clear(self, endpoint_guid: str, *, reason: str = "") -> bool:
        """Remove ``endpoint_guid`` from quarantine.

        Returns ``True`` when an entry was removed, ``False`` when the
        endpoint was not quarantined. ``reason`` is informational only;
        common values are ``"hotplug"``, ``"recheck_recovered"``,
        ``"manual"``.
        """
        entry = self._entries.pop(endpoint_guid, None)
        if entry is None:
            return False
        logger.info(
            "voice_endpoint_unquarantined",
            endpoint=endpoint_guid,
            friendly_name=entry.device_friendly_name,
            reason=reason or "explicit",
        )
        return True

    def purge_expired(self) -> list[QuarantineEntry]:
        """Evict every entry whose :attr:`expires_at_monotonic` has passed.

        Returns the list of evicted entries so callers (watchdog recheck
        loop) can trigger a recheck-cascade for each.
        """
        now = self._clock()
        evicted: list[QuarantineEntry] = []
        # Iterate a copy because we mutate during iteration.
        for guid, entry in list(self._entries.items()):
            if entry.expires_at_monotonic <= now:
                self._entries.pop(guid, None)
                evicted.append(entry)
                logger.info(
                    "voice_endpoint_quarantine_expired",
                    endpoint=guid,
                    friendly_name=entry.device_friendly_name,
                    age_s=now - entry.added_at_monotonic,
                )
        return evicted

    # ── Queries ─────────────────────────────────────────────────────────

    def is_quarantined(self, endpoint_guid: str) -> bool:
        """Return ``True`` when ``endpoint_guid`` has a live entry.

        Lazily purges the entry when its deadline has passed so callers
        never need to remember :meth:`purge_expired` themselves.
        """
        entry = self._entries.get(endpoint_guid)
        if entry is None:
            return False
        if entry.expires_at_monotonic <= self._clock():
            self._entries.pop(endpoint_guid, None)
            logger.info(
                "voice_endpoint_quarantine_expired",
                endpoint=endpoint_guid,
                friendly_name=entry.device_friendly_name,
            )
            return False
        return True

    def get(self, endpoint_guid: str) -> QuarantineEntry | None:
        """Return the live entry for ``endpoint_guid`` or ``None``.

        Mirrors :meth:`is_quarantined` expiry semantics.
        """
        entry = self._entries.get(endpoint_guid)
        if entry is None:
            return None
        if entry.expires_at_monotonic <= self._clock():
            self._entries.pop(endpoint_guid, None)
            return None
        return entry

    def snapshot(self) -> tuple[QuarantineEntry, ...]:
        """Return an immutable snapshot of live entries (post-expiry).

        Used by the dashboard capture-diagnostics endpoint and the CLI
        doctor check so operators can see what's quarantined without
        mutating the store.
        """
        now = self._clock()
        # Drop expired on the way out so the snapshot is always accurate.
        live = tuple(e for e in self._entries.values() if e.expires_at_monotonic > now)
        # Purge anything that didn't make the cut so the store doesn't
        # drift from the snapshot.
        for guid in [g for g, e in list(self._entries.items()) if e.expires_at_monotonic <= now]:
            self._entries.pop(guid, None)
        return live

    def __len__(self) -> int:
        """Number of *live* entries — expired rows are purged first."""
        return len(self.snapshot())

    def __contains__(self, endpoint_guid: object) -> bool:
        if not isinstance(endpoint_guid, str):
            return False
        return self.is_quarantined(endpoint_guid)

    def endpoints(self) -> Iterable[str]:
        """Iterate live endpoint GUIDs (expired entries skipped)."""
        return (e.endpoint_guid for e in self.snapshot())


# ── Module-level singleton ───────────────────────────────────────────────
# The cascade / watchdog / factory-integration layers all need to agree
# on a single in-memory store. A lazy module-level accessor keeps the
# dependency graph simple without forcing callers to thread a store
# reference through every constructor.


_SINGLETON: EndpointQuarantine | None = None


def get_default_quarantine(
    *,
    quarantine_s: float | None = None,
    maxsize: int | None = None,
) -> EndpointQuarantine:
    """Return (and lazily construct) the process-wide quarantine store.

    Args:
        quarantine_s: Override for the quarantine TTL. First call wins —
            subsequent calls that pass a different value log a warning
            and return the existing instance. When ``None`` the value is
            sourced from :class:`VoiceTuningConfig.kernel_invalidated_quarantine_s`.
        maxsize: Override for the capacity cap. First-call-wins semantics
            as above. ``None`` picks :data:`_DEFAULT_MAXSIZE`.

    Tests that need a fresh instance call :func:`reset_default_quarantine`
    before first use.
    """
    global _SINGLETON  # noqa: PLW0603 — lazy singleton, not user-mutable state
    if _SINGLETON is not None:
        if quarantine_s is not None and quarantine_s != _SINGLETON._quarantine_s:
            logger.warning(
                "voice_quarantine_reinit_ignored",
                requested_quarantine_s=quarantine_s,
                active_quarantine_s=_SINGLETON._quarantine_s,
            )
        return _SINGLETON
    if quarantine_s is None:
        from sovyx.engine.config import VoiceTuningConfig as _VoiceTuning

        quarantine_s = _VoiceTuning().kernel_invalidated_quarantine_s
    _SINGLETON = EndpointQuarantine(
        quarantine_s=quarantine_s,
        maxsize=maxsize if maxsize is not None else _DEFAULT_MAXSIZE,
    )
    return _SINGLETON


def reset_default_quarantine() -> None:
    """Drop the singleton — tests use this between cases for isolation."""
    global _SINGLETON  # noqa: PLW0603 — lazy singleton, not user-mutable state
    _SINGLETON = None


__all__ = [
    "EndpointQuarantine",
    "QuarantineEntry",
    "get_default_quarantine",
    "reset_default_quarantine",
]
