"""Linux — re-engage ALSA Capture switch + lift Internal Mic Boost.

Mission anchor:
``docs-internal/missions/MISSION-voice-linux-silent-mic-remediation-2026-05-04.md``
§Phase 2 T2.2.

The ALSA mixer for capture-class controls has TWO independent state
dimensions per channel:

1. **Switch** (``[on]`` / ``[off]``) — boolean toggle. When ``[off]``
   the channel emits exact-zero PCM regardless of the volume slider.
2. **Volume** + **boost** (raw values within ``Limits``) — analog gain
   stages.

The existing :class:`LinuxALSAMixerResetBypass` covers the **volume
saturation / attenuation** dimension (lifts boost-up when too low,
resets boost-down when clipping). It does NOT cover the **switch off**
case. On Linux Mint 22 + Sony VAIO + HDA-Generic codec without UCM
profile (the forensic case from
``c:\\Users\\guipe\\Downloads\\logs_01.txt`` 2026-05-04), the
``Capture`` switch defaults to ``[off]`` after kernel boot — a state
that NEVER triggers the saturation/attenuation regime detection
because the raw value is read as 0 (capture is muted, not just quiet).

T2.2 closes that gap. Detect via ``amixer -c <N> scontents`` parse for
controls matching the canonical patterns (``Capture``,
``Capture Switch``, ``Internal Mic``, ``Mic Capture Switch``,
``Internal Mic Boost``) where any channel reading line ends in
``[off]`` OR boost is at min raw value. Repair via:

* ``amixer -c <N> sset '<name>' cap`` (flips switch to ``[on]``)
* ``amixer -c <N> sset '<name>' 80%`` (sane default volume)
* ``amixer -c <N> sset 'Internal Mic Boost' 50%`` (boost to mid for
  the boost-class control if present)

Persist with ``sudo alsactl store <N>`` is OUT OF SCOPE for this
strategy — runtime mutation only. The wizard's diagnosis_hint
(shipped in v0.30.11 T2.7) tells the operator to run alsactl store
themselves so the fix survives reboot. Adding a sudo subprocess call
from a daemon strategy is the wrong layer of trust — operator opt-in.

Iteration over input cards via the helper shipped in v0.30.9 T1.3:
:func:`sovyx.voice.health._alsa_input_cards.enumerate_input_card_ids`.

Default-OFF + lenient-on (telemetry mode) per
``feedback_staged_adoption``: a strategy that mutates the host audio
mixer on ALL input cards needs one minor cycle of production
telemetry validation before strict mode flips on.
"""

from __future__ import annotations

import contextlib
import re
import shutil
import subprocess
from typing import TYPE_CHECKING

from sovyx.engine.config import VoiceTuningConfig
from sovyx.observability.logging import get_logger
from sovyx.voice.health._alsa_input_cards import enumerate_input_card_ids
from sovyx.voice.health.bypass._strategy import BypassApplyError, BypassRevertError
from sovyx.voice.health.contract import Eligibility

if TYPE_CHECKING:
    from sovyx.voice.health.contract import BypassContext

logger = get_logger(__name__)


_STRATEGY_NAME = "linux.alsa_capture_switch"


# Eligibility-reason tokens.
_REASON_NOT_LINUX = "not_linux_platform"
_REASON_DISABLED_BY_TUNING = "alsa_capture_switch_disabled_by_tuning"
_REASON_NO_AMIXER = "amixer_unavailable_on_host"
_REASON_NO_INPUT_CARDS = "no_alsa_card_with_input_channels"
_REASON_ALL_CONTROLS_OK = "no_capture_switch_off_or_boost_zero"
_REASON_PROBE_FAILED = "amixer_probe_failed_during_eligibility"


# Apply-reason tokens.
_APPLY_REASON_AMIXER_GONE = "amixer_disappeared_at_apply"
_APPLY_REASON_NO_TARGETS = "no_off_capture_or_zero_boost_at_apply"
_APPLY_REASON_SSET_FAILED = "amixer_sset_failed"
_APPLY_REASON_VERIFY_FAILED = "verify_after_sset_still_off"


# Revert-reason tokens.
_REVERT_REASON_RESTORE_FAILED = "amixer_restore_failed"


_AMIXER_TIMEOUT_S = 2.0


# Outcome strings (stable taxonomy).
_OUTCOME_CAPTURE_SWITCH_ENGAGED = "capture_switch_engaged"
_OUTCOME_CAPTURE_SWITCH_AND_BOOST_LIFTED = "capture_switch_engaged_and_boost_lifted"
_OUTCOME_LENIENT_NO_REPAIR = "lenient_no_repair"


# Apply targets — sset uses these literal level strings.
_APPLY_VOLUME_PCT = "80%"
_APPLY_BOOST_PCT = "50%"


# Conservative cost: 1-3 cards × 3-4 sset calls each, ~30 ms per call.
_APPLY_COST_MS = 600


# Control-name patterns we consider "switchable capture controls" — a
# case-insensitive substring match is intentional because vendor names
# vary (`Mic Capture Switch`, `Capture`, `Internal Mic`). The bypass
# only mutates controls that match BOTH the pattern AND have a switch
# in `[off]` state on at least one channel.
_CAPTURE_SWITCH_PATTERNS: tuple[str, ...] = (
    "capture",  # matches "Capture" + "Capture Switch" + "Mic Capture"
    "internal mic",  # matches "Internal Mic" + "Internal Mic Boost"
    "front mic",
    "rear mic",
)


# Boost-class controls — same pattern set restricted to anything
# explicitly labelled "boost". The lift-to-50% target applies only to
# these.
_BOOST_PATTERNS: tuple[str, ...] = ("boost",)


# Regex for "[off]" capture state in amixer output. Channel readings
# look like: `Front Left: Capture 0 [0%] [-34.00dB] [off]`.
_OFF_RE = re.compile(r"\[off\]", re.IGNORECASE)


# Regex for the per-channel raw value. ALSA emits two distinct shapes:
#
#   Capture-class (with the leading "Capture <N>" prefix):
#     Front Left: Capture 0 [0%] [-34.00dB] [off]
#   Boost-class (just the bare number):
#     Mono: 1 [33%] [12.00dB]
#
# Common ground: the raw integer is the value that immediately
# precedes the ``[NN%]`` percentage indicator. Anchoring on that
# pattern works for both shapes without a per-class branch.
_RAW_VALUE_RE = re.compile(r"(\d+)\s*\[\d+%\]")


# Regex for the simple-control header line: `Simple mixer control 'Capture',0`.
_CONTROL_HEADER_RE = re.compile(r"^Simple mixer control '(?P<name>[^']+)',\d+\s*$")


# Regex for the limits line: `  Limits: Capture 0 - 80`.
_LIMITS_RE = re.compile(r"^\s*Limits:\s*[A-Za-z ]*?(\d+)\s*-\s*(\d+)\s*$")


class _ControlState:
    """Compact state carrier for one mixer control."""

    __slots__ = ("name", "switch_off", "raw_value", "min_raw", "max_raw")

    def __init__(
        self,
        *,
        name: str,
        switch_off: bool,
        raw_value: int | None,
        min_raw: int | None,
        max_raw: int | None,
    ) -> None:
        self.name = name
        self.switch_off = switch_off
        self.raw_value = raw_value
        self.min_raw = min_raw
        self.max_raw = max_raw


class _CardScan:
    """Result of one card's amixer scan."""

    __slots__ = ("card_index", "card_id", "controls")

    def __init__(
        self,
        *,
        card_index: int,
        card_id: str,
        controls: list[_ControlState],
    ) -> None:
        self.card_index = card_index
        self.card_id = card_id
        self.controls = controls


class _AppliedTarget:
    """Snapshot of a control's pre-apply state for revert."""

    __slots__ = ("card_index", "name", "was_switch_off", "previous_raw")

    def __init__(
        self,
        *,
        card_index: int,
        name: str,
        was_switch_off: bool,
        previous_raw: int | None,
    ) -> None:
        self.card_index = card_index
        self.name = name
        self.was_switch_off = was_switch_off
        self.previous_raw = previous_raw


class LinuxALSACaptureSwitchBypass:
    """Engage ALSA Capture switches + lift mic-boost on input cards.

    Eligibility:
        * :attr:`BypassContext.platform_key == "linux"`.
        * :attr:`VoiceTuningConfig.linux_alsa_capture_switch_bypass_enabled`
          is ``True`` (default-off — opt-in).
        * ``amixer`` is on PATH.
        * At least one ALSA card with capture PCM exists (via the v0.30.9
          T1.3 helper :func:`enumerate_input_card_ids`).
        * At least one capture-class control on at least one input card
          has a channel in ``[off]`` state OR a boost-class control at
          minimum raw value.

    Apply:
        * **Lenient mode** (default v0.30.12): emit ``voice.bypass.would_repair``
          + return ``"lenient_no_repair"`` without touching ``amixer``.
        * **Strict mode**: per faulted control, run ``amixer -c <N> sset
          '<name>' cap`` + ``amixer -c <N> sset '<name>' 80%`` (or 50%
          for boost). Snapshot pre-apply raw value for revert. Verify
          by re-reading ``amixer -c <N> scontents`` and confirming the
          control is now ``[on]`` + non-zero.

    Revert:
        Per applied target, restore the pre-apply raw value via
        ``amixer -c <N> sset '<name>' <previous_raw>`` and re-disable
        the switch via ``amixer -c <N> sset '<name>' nocap`` if it was
        originally off. Best-effort — partial-control failures log
        WARNING but don't raise.
    """

    name: str = _STRATEGY_NAME

    def __init__(self) -> None:
        self._applied_targets: list[_AppliedTarget] = []

    async def probe_eligibility(
        self,
        context: BypassContext,
    ) -> Eligibility:
        if context.platform_key != "linux":
            return Eligibility(
                applicable=False,
                reason=_REASON_NOT_LINUX,
                estimated_cost_ms=0,
            )
        tuning = _tuning_from_context()
        if not tuning.linux_alsa_capture_switch_bypass_enabled:
            return Eligibility(
                applicable=False,
                reason=_REASON_DISABLED_BY_TUNING,
                estimated_cost_ms=0,
            )

        amixer_path = shutil.which("amixer")
        if amixer_path is None:
            return Eligibility(
                applicable=False,
                reason=_REASON_NO_AMIXER,
                estimated_cost_ms=0,
            )

        cards = enumerate_input_card_ids()
        if not cards:
            return Eligibility(
                applicable=False,
                reason=_REASON_NO_INPUT_CARDS,
                estimated_cost_ms=0,
            )

        # Probe each input card; if ANY has a faulted control we're
        # applicable. (apply() will re-probe to pick the precise target
        # set — eligibility is a yes/no decision.)
        for card_index, card_id in cards:
            try:
                scan = _scan_card(amixer_path, card_index, card_id)
            except _ProbeError:
                # One card failed — skip it but try others. Returning
                # PROBE_FAILED on the FIRST card crash would mask a
                # second healthy card with an actual fault.
                continue
            if _has_faulted_controls(scan):
                return Eligibility(
                    applicable=True,
                    reason="",
                    estimated_cost_ms=_APPLY_COST_MS,
                )

        return Eligibility(
            applicable=False,
            reason=_REASON_ALL_CONTROLS_OK,
            estimated_cost_ms=0,
        )

    async def apply(
        self,
        context: BypassContext,
    ) -> str:
        logger.info(
            "bypass_strategy_apply_begin",
            strategy=_STRATEGY_NAME,
            endpoint_guid=context.endpoint_guid,
            endpoint_name=context.endpoint_friendly_name,
            host_api=context.host_api_name,
        )
        tuning = _tuning_from_context()

        amixer_path = shutil.which("amixer")
        if amixer_path is None:
            msg = "amixer disappeared between eligibility and apply"
            raise BypassApplyError(msg, reason=_APPLY_REASON_AMIXER_GONE)

        cards = enumerate_input_card_ids()
        # Re-probe each card and collect EVERY faulted control as a
        # target — apply mutates them ALL in one strategy pass.
        targets: list[tuple[_CardScan, _ControlState]] = []
        for card_index, card_id in cards:
            try:
                scan = _scan_card(amixer_path, card_index, card_id)
            except _ProbeError:
                continue
            for ctrl in _faulted_controls(scan):
                targets.append((scan, ctrl))

        if not targets:
            msg = (
                "no faulted controls at apply time — re-probe shows "
                "the host mixer state changed between eligibility and apply"
            )
            raise BypassApplyError(msg, reason=_APPLY_REASON_NO_TARGETS)

        # Lenient mode: telemetry only.
        if tuning.linux_alsa_capture_switch_bypass_lenient:
            for scan, ctrl in targets:
                logger.warning(
                    "voice.bypass.would_repair",
                    **{
                        "voice.strategy": _STRATEGY_NAME,
                        "voice.reason": (
                            "alsa_capture_switch_off" if ctrl.switch_off else "alsa_boost_at_min"
                        ),
                        "voice.proposed_action": _proposed_action(scan, ctrl),
                        "voice.target_card_index": scan.card_index,
                        "voice.target_card_id": scan.card_id,
                        "voice.target_control_name": ctrl.name,
                        "voice.endpoint_guid": context.endpoint_guid,
                    },
                )
            return _OUTCOME_LENIENT_NO_REPAIR

        # Strict mode — actually mutate.
        boost_lifted = False
        for scan, ctrl in targets:
            self._applied_targets.append(
                _AppliedTarget(
                    card_index=scan.card_index,
                    name=ctrl.name,
                    was_switch_off=ctrl.switch_off,
                    previous_raw=ctrl.raw_value,
                ),
            )
            await _apply_control_fix(amixer_path, scan.card_index, ctrl)
            if _is_boost_control(ctrl.name):
                boost_lifted = True

        # Verify — re-scan each touched card and confirm faulted
        # controls are now healthy.
        post_targets: list[tuple[int, str]] = []
        for scan, ctrl in targets:
            try:
                post_scan = _scan_card(amixer_path, scan.card_index, scan.card_id)
            except _ProbeError:
                # Probe failed post-apply — be conservative + treat as
                # verify failure (we can't confirm we fixed it).
                post_targets.append((scan.card_index, ctrl.name))
                continue
            post_ctrl = _find_control(post_scan, ctrl.name)
            if post_ctrl is None or post_ctrl.switch_off:
                post_targets.append((scan.card_index, ctrl.name))

        if post_targets:
            msg = (
                f"verify-after-apply reports {len(post_targets)} controls "
                f"still in [off] state: {post_targets!r}"
            )
            raise BypassApplyError(msg, reason=_APPLY_REASON_VERIFY_FAILED)

        logger.info(
            "bypass_strategy_apply_ok",
            strategy=_STRATEGY_NAME,
            endpoint_guid=context.endpoint_guid,
            controls_changed=[(t.card_index, t.name) for t in self._applied_targets],
            controls_count=len(self._applied_targets),
            boost_lifted=boost_lifted,
        )
        return (
            _OUTCOME_CAPTURE_SWITCH_AND_BOOST_LIFTED
            if boost_lifted
            else _OUTCOME_CAPTURE_SWITCH_ENGAGED
        )

    async def revert(
        self,
        context: BypassContext,
    ) -> None:
        if not self._applied_targets:
            return  # No apply or lenient — nothing to revert.

        amixer_path = shutil.which("amixer")
        if amixer_path is None:
            logger.warning(
                "bypass_strategy_revert_skipped_no_amixer",
                strategy=_STRATEGY_NAME,
                endpoint_guid=context.endpoint_guid,
                pending_targets=len(self._applied_targets),
            )
            self._applied_targets = []
            return

        # Best-effort per-target revert. Per CLAUDE.md anti-pattern + the
        # _linux_alsa_mixer.py:358-374 precedent, single-control failures
        # log WARNING but don't raise — the coordinator is already in
        # teardown.
        failed: list[tuple[int, str]] = []
        for target in reversed(self._applied_targets):
            try:
                await _revert_control(amixer_path, target)
            except BypassApplyError as exc:
                failed.append((target.card_index, target.name))
                logger.warning(
                    "bypass_strategy_revert_partial",
                    strategy=_STRATEGY_NAME,
                    card_index=target.card_index,
                    control=target.name,
                    error=str(exc),
                    reason=exc.reason,
                )

        self._applied_targets = []

        if failed and len(failed) == len(self._applied_targets):
            # Every single revert failed — surface as BypassRevertError so
            # the coordinator emits voice.bypass.revert_failed.
            raise BypassRevertError(
                f"all {len(failed)} targets failed to revert: {failed!r}",
                reason=_REVERT_REASON_RESTORE_FAILED,
            )

        logger.info(
            "bypass_strategy_revert_ok",
            strategy=_STRATEGY_NAME,
            endpoint_guid=context.endpoint_guid,
            restored_count=len(self._applied_targets),
            failed_count=len(failed),
        )


# ── Module-private helpers ──────────────────────────────────────────


def _tuning_from_context() -> VoiceTuningConfig:
    """Re-read tuning each call so SOVYX_TUNING__* overrides stay live."""
    return VoiceTuningConfig()


class _ProbeError(RuntimeError):
    """Internal sentinel — propagated up to the strategy probe boundary."""


def _scan_card(amixer_path: str, card_index: int, card_id: str) -> _CardScan:
    """Run ``amixer -c <N> scontents`` and parse capture-class controls.

    Raises :class:`_ProbeError` on subprocess timeout / non-zero exit.
    """
    try:
        result = subprocess.run(  # noqa: S603 — fixed argv to trusted amixer
            (amixer_path, "-c", str(card_index), "scontents"),
            capture_output=True,
            text=True,
            timeout=_AMIXER_TIMEOUT_S,
            check=False,
        )
    except (subprocess.TimeoutExpired, OSError) as exc:
        raise _ProbeError(str(exc)) from exc
    if result.returncode != 0:
        raise _ProbeError(
            f"amixer -c {card_index} scontents exited {result.returncode}",
        )

    return _CardScan(
        card_index=card_index,
        card_id=card_id,
        controls=_parse_amixer_output(result.stdout),
    )


def _parse_amixer_output(stdout: str) -> list[_ControlState]:
    """Parse ``amixer scontents`` output into a list of control states.

    The format is a sequence of blocks per control::

        Simple mixer control 'Capture',0
          Capabilities: cvolume cswitch
          Capture channels: Front Left - Front Right
          Limits: Capture 0 - 80
          Front Left: Capture 0 [0%] [-34.00dB] [off]
          Front Right: Capture 0 [0%] [-34.00dB] [off]

    We only extract: name, whether ANY channel has ``[off]``, the
    raw value of the first channel, and the min/max raw from
    ``Limits:``.
    """
    controls: list[_ControlState] = []
    current_name: str | None = None
    current_switch_off = False
    current_raw: int | None = None
    current_min: int | None = None
    current_max: int | None = None

    def _flush() -> None:
        nonlocal current_name, current_switch_off, current_raw, current_min, current_max
        if current_name is not None:
            controls.append(
                _ControlState(
                    name=current_name,
                    switch_off=current_switch_off,
                    raw_value=current_raw,
                    min_raw=current_min,
                    max_raw=current_max,
                ),
            )
        current_name = None
        current_switch_off = False
        current_raw = None
        current_min = None
        current_max = None

    for raw_line in stdout.splitlines():
        header = _CONTROL_HEADER_RE.match(raw_line)
        if header:
            _flush()
            current_name = header.group("name").strip()
            continue
        if current_name is None:
            continue
        limits = _LIMITS_RE.match(raw_line)
        if limits:
            current_min = int(limits.group(1))
            current_max = int(limits.group(2))
            continue
        # Channel reading line. Detect [off] + capture raw value.
        if _OFF_RE.search(raw_line):
            current_switch_off = True
        if current_raw is None:
            raw_match = _RAW_VALUE_RE.search(raw_line)
            if raw_match:
                with contextlib.suppress(ValueError):
                    current_raw = int(raw_match.group(1))

    _flush()
    return controls


def _is_capture_pattern(name: str) -> bool:
    name_lower = name.lower()
    return any(p in name_lower for p in _CAPTURE_SWITCH_PATTERNS)


def _is_boost_control(name: str) -> bool:
    name_lower = name.lower()
    return any(p in name_lower for p in _BOOST_PATTERNS)


def _is_faulted(ctrl: _ControlState) -> bool:
    """Return True iff this control matches the apply contract.

    Faulted means EITHER:
    * Switch is ``[off]`` AND name matches a capture pattern, OR
    * Boost-class control AND raw value at minimum (e.g. mic boost
      slider dragged to 0).
    """
    if not _is_capture_pattern(ctrl.name):
        return False
    if ctrl.switch_off:
        return True
    return bool(
        _is_boost_control(ctrl.name)
        and ctrl.raw_value == ctrl.min_raw
        and ctrl.min_raw is not None,
    )


def _faulted_controls(scan: _CardScan) -> list[_ControlState]:
    return [c for c in scan.controls if _is_faulted(c)]


def _has_faulted_controls(scan: _CardScan) -> bool:
    return any(_is_faulted(c) for c in scan.controls)


def _find_control(scan: _CardScan, name: str) -> _ControlState | None:
    for ctrl in scan.controls:
        if ctrl.name == name:
            return ctrl
    return None


def _proposed_action(scan: _CardScan, ctrl: _ControlState) -> str:
    """Build the human-readable proposed action string for would_repair."""
    if _is_boost_control(ctrl.name):
        return f"amixer -c {scan.card_index} sset '{ctrl.name}' {_APPLY_BOOST_PCT}"
    return (
        f"amixer -c {scan.card_index} sset '{ctrl.name}' cap; "
        f"amixer -c {scan.card_index} sset '{ctrl.name}' {_APPLY_VOLUME_PCT}"
    )


async def _apply_control_fix(
    amixer_path: str,
    card_index: int,
    ctrl: _ControlState,
) -> None:
    """Run the fix-up amixer commands for one faulted control."""
    import asyncio

    if _is_boost_control(ctrl.name):
        # Boost: just lift to mid.
        await asyncio.to_thread(
            _run_amixer_sset,
            amixer_path,
            card_index,
            ctrl.name,
            _APPLY_BOOST_PCT,
        )
        return

    # Capture-class: engage switch + set sane volume.
    await asyncio.to_thread(
        _run_amixer_sset,
        amixer_path,
        card_index,
        ctrl.name,
        "cap",
    )
    await asyncio.to_thread(
        _run_amixer_sset,
        amixer_path,
        card_index,
        ctrl.name,
        _APPLY_VOLUME_PCT,
    )


def _run_amixer_sset(
    amixer_path: str,
    card_index: int,
    control_name: str,
    value: str,
) -> None:
    """Execute one ``amixer -c <N> sset '<name>' <value>`` call.

    Raises :class:`BypassApplyError` on non-zero exit / timeout / spawn
    failure so the strategy's apply() can route the failure to the
    coordinator's structured outcome.
    """
    try:
        result = subprocess.run(  # noqa: S603 — fixed argv to trusted amixer
            (amixer_path, "-c", str(card_index), "sset", control_name, value),
            capture_output=True,
            text=True,
            timeout=_AMIXER_TIMEOUT_S,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        msg = (
            f"amixer -c {card_index} sset {control_name!r} {value!r} "
            f"timed out after {_AMIXER_TIMEOUT_S} s"
        )
        raise BypassApplyError(msg, reason=_APPLY_REASON_SSET_FAILED) from exc
    except OSError as exc:
        msg = f"amixer sset failed to spawn: {exc!r}"
        raise BypassApplyError(msg, reason=_APPLY_REASON_SSET_FAILED) from exc
    if result.returncode != 0:
        msg = (
            f"amixer -c {card_index} sset {control_name!r} {value!r} "
            f"exited {result.returncode}: {result.stderr.strip()!r}"
        )
        raise BypassApplyError(msg, reason=_APPLY_REASON_SSET_FAILED)


async def _revert_control(amixer_path: str, target: _AppliedTarget) -> None:
    """Restore the pre-apply state for one control."""
    import asyncio

    if target.previous_raw is not None:
        await asyncio.to_thread(
            _run_amixer_sset,
            amixer_path,
            target.card_index,
            target.name,
            str(target.previous_raw),
        )
    if target.was_switch_off:
        await asyncio.to_thread(
            _run_amixer_sset,
            amixer_path,
            target.card_index,
            target.name,
            "nocap",
        )
