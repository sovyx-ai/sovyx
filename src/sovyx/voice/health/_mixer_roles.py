"""Role-based discovery for ALSA mixer controls — L2.5 Phase F1.B.

Turns raw ``amixer`` simple-control names into a stable
:class:`~sovyx.voice.health.contract.MixerControlRole` enum the KB
loader, preset applier, and sanity orchestrator can key on.

Why role-based instead of string matching
-----------------------------------------

Control names vary across codec driver families: HDA codecs use
``Capture`` / ``Internal Mic Boost`` / ``Auto-Mute Mode``; Intel SOF
topologies use ``PGA1.0 1 Master Capture Volume`` / ``Dmic0 Capture
Volume``; USB audio-class devices expose a single ``Mic``. A single
shared KB schema only works if the profile can say "set the capture
master to 100%" and have that resolve to the right control name on
every hardware. This module provides that resolution.

Three-layer lookup
------------------

1. **Per-codec override** (``_CODEC_OVERRIDE_TABLE``, seeded from HIL):
   highest priority. Used when a codec quirk makes the generic table
   return the wrong role (or when a specific codec's control names
   don't match the driver family's convention).
2. **Driver-family table** (``_DRIVER_FAMILY_TABLES``, F1: HDA only):
   case-insensitive exact match against the control name. SOF /
   USB-audio / BT tables ship in F2.
3. **Substring fallback** (``_SUBSTRING_FALLBACK``, case-insensitive
   contains, ordered specific-before-general): catches unknown codecs
   whose control names still use the conventional HDA vocabulary.
   Superset of
   :data:`sovyx.voice.health._linux_mixer_probe._BOOST_CONTROL_PATTERNS`
   — every boost pattern the probe recognises MUST resolve to a
   non-UNKNOWN role here (regression-tested).

Controls that fall through all three layers resolve to
:attr:`MixerControlRole.UNKNOWN` and are surfaced to telemetry via
:meth:`MixerControlRoleResolver.resolve_card` but ignored by the
preset apply layer (invariant I5 — rollback records every mutated
control; mutating an UNKNOWN-role control would poison that contract).

See V2 Master Plan Part E.2 + Appendix 1.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from sovyx.observability.logging import get_logger
from sovyx.voice.health.contract import MixerControlRole

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from sovyx.voice.health.contract import (
        HardwareContext,
        MixerCardSnapshot,
        MixerControlSnapshot,
    )

logger = get_logger(__name__)


_HDA_ROLE_TABLE: Mapping[str, MixerControlRole] = {
    "capture": MixerControlRole.CAPTURE_MASTER,
    "internal mic boost": MixerControlRole.INTERNAL_MIC_BOOST,
    "mic boost": MixerControlRole.PREAMP_BOOST,
    "front mic boost": MixerControlRole.PREAMP_BOOST,
    "rear mic boost": MixerControlRole.PREAMP_BOOST,
    "line boost": MixerControlRole.PREAMP_BOOST,
    "digital capture volume": MixerControlRole.DIGITAL_CAPTURE,
    "auto-mute mode": MixerControlRole.AUTO_MUTE,
    "input source": MixerControlRole.INPUT_SOURCE_SELECTOR,
    "capture switch": MixerControlRole.CAPTURE_SWITCH,
}
"""HDA-family control-name → role (F1 scope).

Keys MUST be pre-lowercased because
:meth:`MixerControlRoleResolver.resolve` calls ``str.lower()`` on the
probe-reported name before lookup. Canonical HDA names preserve
spacing exactly (``"auto-mute mode"`` with the hyphen).
"""


_CODEC_OVERRIDE_TABLE: Mapping[str, Mapping[str, MixerControlRole]] = {
    # Conexant SN6180 — Sony VAIO VJFE69F11X pilot
    # (SVX-VOICE-LINUX-VJFE69-20260423, ADR §1.1). Ships identical to
    # the HDA table for now; the entry exists as the seed of the
    # codec-override mechanism so HIL-captured quirks for other codecs
    # can be added without reshaping this file.
    "14F1:5045": {
        "capture": MixerControlRole.CAPTURE_MASTER,
        "internal mic boost": MixerControlRole.INTERNAL_MIC_BOOST,
    },
}
"""Per-codec ``(codec_id → control_name → role)`` overrides.

Codec IDs are the ``<vendor>:<device>`` hex pair read from
``/proc/asound/card*/codec#*`` (uppercase hex, matching the
``HardwareContext.codec_id`` convention). Inner keys MUST be
pre-lowercased.
"""


_SUBSTRING_FALLBACK: tuple[tuple[str, MixerControlRole], ...] = (
    # Specific patterns MUST precede general ones — "internal mic
    # boost" matches before "mic boost", "front mic boost" before
    # "mic boost", etc. Ordering here is the single source of truth
    # for substring-fallback precedence.
    ("internal mic boost", MixerControlRole.INTERNAL_MIC_BOOST),
    ("front mic boost", MixerControlRole.PREAMP_BOOST),
    ("rear mic boost", MixerControlRole.PREAMP_BOOST),
    ("line boost", MixerControlRole.PREAMP_BOOST),
    ("mic boost", MixerControlRole.PREAMP_BOOST),
    ("digital capture", MixerControlRole.DIGITAL_CAPTURE),
    ("capture switch", MixerControlRole.CAPTURE_SWITCH),
    ("auto-mute", MixerControlRole.AUTO_MUTE),
    ("input source", MixerControlRole.INPUT_SOURCE_SELECTOR),
    # Generic last — ``Capture`` appears as a substring of several
    # more specific names above, so every compound form must be
    # resolved before we fall back to this catch-all.
    ("capture", MixerControlRole.CAPTURE_MASTER),
)
"""Ordered substring fallback (case-insensitive).

Superset of
:data:`sovyx.voice.health._linux_mixer_probe._BOOST_CONTROL_PATTERNS`:
every boost pattern the probe recognises resolves here to the
appropriate role. Regression-tested (see
``test_mixer_roles.py::TestBoostPatternConsistency``).
"""


_DRIVER_FAMILY_TABLES: Mapping[str, Mapping[str, MixerControlRole]] = {
    "hda": _HDA_ROLE_TABLE,
    # F2 adds: "sof": _SOF_ROLE_TABLE (Intel Tiger/Meteor/Lunar Lake)
    # F2 adds: "usb-audio": _USB_ROLE_TABLE (UAC1/UAC2 class)
    # F2 adds: "bt": _BT_ROLE_TABLE (BT-HFP)
    # Families not present here fall through to substring fallback.
}
"""Driver-family → exact-match role table.

F1 ships HDA only. V2 Master Plan Phase F2 extends to SOF, USB
audio-class, and BT. Unknown families (including the ``"unknown"``
sentinel on :attr:`HardwareContext.driver_family`) return an empty
lookup and fall through to :data:`_SUBSTRING_FALLBACK`.
"""


class MixerControlRoleResolver:
    """Three-layer discovery: codec override → driver-family table → substring.

    Stateless on purpose — the same instance is safe to share across
    cascade passes, threads, and endpoints. All lookup tables are
    constructor-injected so tests can exercise the layer precedence
    without monkey-patching module globals.

    Example:
        >>> resolver = MixerControlRoleResolver()
        >>> resolver.resolve("hda", "14F1:5045", "Internal Mic Boost")
        <MixerControlRole.INTERNAL_MIC_BOOST: 'internal_mic_boost'>
        >>> resolver.resolve("unknown", None, "Some Unknown Control")
        <MixerControlRole.UNKNOWN: 'unknown'>
    """

    def __init__(
        self,
        *,
        codec_override_table: (Mapping[str, Mapping[str, MixerControlRole]] | None) = None,
        driver_family_tables: (Mapping[str, Mapping[str, MixerControlRole]] | None) = None,
        substring_fallback: Sequence[tuple[str, MixerControlRole]] | None = None,
    ) -> None:
        """Instantiate with shipped tables (default) or custom ones (tests).

        Args:
            codec_override_table: Layer-1 lookup. Defaults to the
                shipped :data:`_CODEC_OVERRIDE_TABLE`. Tests pass
                ``{}`` to disable the layer, or a targeted mapping to
                assert precedence.
            driver_family_tables: Layer-2 lookup. Defaults to the
                shipped :data:`_DRIVER_FAMILY_TABLES`.
            substring_fallback: Layer-3 lookup. Defaults to
                :data:`_SUBSTRING_FALLBACK`. Order matters: first
                match wins, so specific patterns must come before
                general ones.
        """
        self._codec_override = (
            codec_override_table if codec_override_table is not None else _CODEC_OVERRIDE_TABLE
        )
        self._driver_family_tables = (
            driver_family_tables if driver_family_tables is not None else _DRIVER_FAMILY_TABLES
        )
        self._substring_fallback = (
            substring_fallback if substring_fallback is not None else _SUBSTRING_FALLBACK
        )

    def resolve(
        self,
        driver_family: str,
        codec_id: str | None,
        control_name: str,
    ) -> MixerControlRole:
        """Resolve one control name to its canonical role.

        Args:
            driver_family: One of the :data:`_HARDWARE_DRIVER_FAMILIES`
                values (accepted as plain ``str`` for testability —
                unknown families just skip Layer 2).
            codec_id: Codec vendor:device pair, or ``None`` when
                hardware detection couldn't read it (skips Layer 1).
            control_name: Raw ``amixer``-reported name; resolved
                case-insensitively.

        Returns:
            The matched :class:`MixerControlRole`, or
            :attr:`MixerControlRole.UNKNOWN` when no layer matches.
            ``UNKNOWN`` is also logged at DEBUG for diagnostic
            visibility — repeated ``UNKNOWN`` for the same
            ``(driver_family, control_name)`` pair on a user's
            hardware is a signal that the driver-family table or the
            codec-override table needs an entry.
        """
        if not control_name:
            return MixerControlRole.UNKNOWN
        lowered = control_name.lower()

        # Layer 1: per-codec override (highest priority).
        if codec_id is not None:
            codec_map = self._codec_override.get(codec_id)
            if codec_map is not None:
                codec_role = codec_map.get(lowered)
                if codec_role is not None:
                    return codec_role

        # Layer 2: driver-family exact-match table.
        family_map = self._driver_family_tables.get(driver_family)
        if family_map is not None:
            family_role = family_map.get(lowered)
            if family_role is not None:
                return family_role

        # Layer 3: ordered substring fallback.
        for pattern, role in self._substring_fallback:
            if pattern in lowered:
                return role

        logger.debug(
            "mixer_role_resolver_unknown",
            driver_family=driver_family,
            codec_id=codec_id,
            control_name=control_name,
        )
        return MixerControlRole.UNKNOWN

    def resolve_card(
        self,
        snapshot: MixerCardSnapshot,
        hw: HardwareContext,
    ) -> Mapping[MixerControlRole, tuple[MixerControlSnapshot, ...]]:
        """Group a card's controls by resolved role.

        Deviates from V2 Master Plan Part E.2's
        ``Mapping[MixerControlRole, MixerControlSnapshot]`` signature:
        desktop HDA legitimately exposes ``Front Mic Boost`` +
        ``Rear Mic Boost`` + sometimes ``Line Boost``, all three
        resolving to :attr:`MixerControlRole.PREAMP_BOOST`. A
        first-wins scalar mapping would silently drop the later
        controls — and that silently breaks invariant I5 (full
        rollback) because revert needs every mutated control
        recorded. Tuple-valued mapping preserves the complete set;
        consumers that expect one pick via ``next(iter(...))``.

        :attr:`MixerControlRole.UNKNOWN`-resolved controls are kept
        under the ``UNKNOWN`` key so the dashboard + telemetry can
        surface them for KB-growth prioritisation. The apply layer
        guards against mutating them.

        Args:
            snapshot: One card's controls as returned by
                :func:`~sovyx.voice.health._linux_mixer_probe.enumerate_alsa_mixer_snapshots`.
            hw: Hardware identity — only
                :attr:`HardwareContext.driver_family` and
                :attr:`HardwareContext.codec_id` are consulted here;
                the richer fields drive KB matching in Phase F1.C.

        Returns:
            Immutable-by-convention mapping from role to the tuple of
            controls resolving to that role, in the order they
            appear in ``snapshot.controls``. Empty mapping when the
            card has no controls.
        """
        grouped: dict[MixerControlRole, list[MixerControlSnapshot]] = {}
        for control in snapshot.controls:
            role = self.resolve(hw.driver_family, hw.codec_id, control.name)
            grouped.setdefault(role, []).append(control)
        return {role: tuple(controls) for role, controls in grouped.items()}


__all__ = [
    "MixerControlRoleResolver",
]
