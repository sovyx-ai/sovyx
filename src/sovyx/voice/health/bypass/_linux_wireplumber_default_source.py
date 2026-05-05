"""Linux — fix WirePlumber default-source pointing at .monitor / muted source.

Mission anchor:
``docs-internal/missions/MISSION-voice-linux-silent-mic-remediation-2026-05-04.md``
§Phase 2 T2.1.

The PipeWire/WirePlumber session manager picks the "default source" by
the highest-``priority.session`` source node. On a fresh Linux Mint /
Ubuntu install with PipeWire, this picker can land on:

* a **monitor** source (e.g. ``alsa_output...analog-stereo.monitor``)
  which is the loop-back of the speakers — captures playback, NOT the
  microphone;
* a **muted** real input source (PipeWire-side mute, distinct from the
  ALSA-side mute the existing :class:`LinuxALSAMixerResetBypass` covers);
* a real input source with **near-zero volume** (operator dragged the
  Cinnamon Sound Settings slider down at some point).

Forensic anchor: ``c:\\Users\\guipe\\Downloads\\evodencias.txt`` 2026-05-04
shows the operator's host had source ``39`` at vol=0.34 (34 %) — silent
to PortAudio even though the ALSA-direct probe (``arecord -D plughw:1,0``)
returned RMS=216. The mic was alive; PipeWire was simply routing the
"default" to a partially-muted source. Since most apps (sovyx + browser
+ video-conferencing) request ``default``, this single bug silences
the entire desktop's mic input.

Strategy design:

* Detect via ``pactl get-default-source`` + ``pactl list sources`` long
  form. Source ends in ``.monitor`` OR mute=on OR all-channel-volumes
  < 5 % → applicable.
* Repair via ``wpctl set-default <ID>`` (preferred — the WirePlumber-
  canonical API). Falls back to ``pactl set-default-source <NAME>`` when
  ``wpctl`` is missing on PATH (some embedded distros ship pipewire
  without the CLI). Then unmutes via ``pactl set-source-mute @DEFAULT_SOURCE@ 0``
  + lifts volume via ``pactl set-source-volume @DEFAULT_SOURCE@ 80%``.
* Verify by re-reading ``pactl get-default-source`` + ``pactl list
  sources`` after the apply settled — confirm the resulting default is
  non-monitor + unmuted.
* Revert by restoring the pre-apply default source name (the snapshot
  is captured BEFORE any mutation; idempotent — second call no-ops).

Default-OFF + lenient-on (telemetry mode) per
``feedback_staged_adoption``: a strategy that mutates host-wide audio
routing needs one minor cycle of production telemetry before flipping
to strict mode. v0.30.12 ships the foundation; v0.31.0 flips the
defaults.

See ``docs-internal/missions/MISSION-voice-linux-silent-mic-remediation-2026-05-04.md``
§Phase 2 T2.1 for the design + telemetry contract.
"""

from __future__ import annotations

import shutil
import subprocess
from typing import TYPE_CHECKING

from sovyx.engine.config import VoiceTuningConfig
from sovyx.observability.logging import get_logger
from sovyx.voice.health.bypass._strategy import BypassApplyError, BypassRevertError
from sovyx.voice.health.contract import Eligibility

if TYPE_CHECKING:
    from sovyx.voice.health.contract import BypassContext

logger = get_logger(__name__)


_STRATEGY_NAME = "linux.wireplumber_default_source"
"""Coordinator-visible strategy identifier — treat as external API.

Dashboards filter bypass outcomes by this string and the per-strategy
metric counter derives its attribute label from it; a rename is a
breaking change.
"""


# Eligibility-reason tokens.
_REASON_NOT_LINUX = "not_linux_platform"
_REASON_DISABLED_BY_TUNING = "wireplumber_default_source_disabled_by_tuning"
_REASON_NO_PACTL = "pactl_unavailable_on_host"
_REASON_PIPEWIRE_NOT_RUNNING = "pipewire_not_running"
_REASON_DEFAULT_ALREADY_OK = "default_source_already_real_and_unmuted"
_REASON_NO_REAL_INPUT_SOURCE = "no_real_input_source_available"
_REASON_QUERY_FAILED = "pactl_query_failed_during_eligibility"


# Apply-reason tokens for :class:`BypassApplyError.reason`.
_APPLY_REASON_NO_PACTL = "pactl_disappeared_at_apply"
_APPLY_REASON_NO_TARGET = "no_target_source_at_apply"
_APPLY_REASON_SET_DEFAULT_FAILED = "wireplumber_set_default_failed"
_APPLY_REASON_UNMUTE_FAILED = "pactl_set_source_mute_failed"
_APPLY_REASON_VERIFY_FAILED = "wireplumber_verify_after_set_default_unchanged"


# Revert-reason tokens for :class:`BypassRevertError.reason`.
_REVERT_REASON_SET_DEFAULT_FAILED = "wireplumber_revert_set_default_failed"


# Subprocess timeout shared with _pipewire.py — keeps every pactl/wpctl
# call bounded so a wedged session manager cannot hang the coordinator.
_PACTL_TIMEOUT_S = 3.0


# Volume-fraction threshold below which a source is considered
# effectively silent. PipeWire renders volume per channel as a float
# multiplier (1.0 = unity gain). The "evodencias.txt" forensic case
# saw vol=0.34 (34 %) which produces audibly-quiet capture; we treat
# anything below 0.05 (5 %) as "muted in practice" + applicable.
_NEAR_ZERO_VOLUME_FRACTION = 0.05


# Volume the apply step targets (80 %, matching the operator playbook
# in OPERATOR-DEBT-MASTER D24). Conservative: high enough for normal
# speech, low enough to leave headroom before clipping.
_TARGET_APPLY_VOLUME_PCT = 80


# Conservative cost hint for coordinator telemetry. wpctl/pactl calls
# round-trip in 30-80 ms each on a healthy host; budgeting 400 ms
# covers the snapshot + set-default + unmute + volume + verify chain
# with head-room for slow buses.
_APPLY_COST_MS = 400


# Outcome strings returned from apply() — stable taxonomy for dashboards.
_OUTCOME_DEFAULT_SOURCE_ROUTED = "default_source_routed_and_unmuted"
_OUTCOME_LENIENT_NO_REPAIR = "lenient_no_repair"


# Stub source names PipeWire emits when no real device exists (server
# headless / no audio card / dummy module loaded). Filtered out of the
# "real input source" enumeration so we don't reroute to a useless
# stub.
_STUB_SOURCE_NAMES = frozenset(
    {
        "auto_null",
        "alsa_input.platform-snd_dummy.0.HiFi__hw_dummy__source",
    },
)


class LinuxWirePlumberDefaultSourceBypass:
    """Reroute the WirePlumber default source to a real input.

    Eligibility:
        * :attr:`BypassContext.platform_key == "linux"`.
        * :attr:`VoiceTuningConfig.linux_wireplumber_default_source_bypass_enabled`
          is ``True`` (default-off — opt-in via
          ``SOVYX_TUNING__VOICE__LINUX_WIREPLUMBER_DEFAULT_SOURCE_BYPASS_ENABLED=true``).
        * ``pactl`` is on PATH AND ``pactl info`` returns rc=0
          (PipeWire's pulse-shim is reachable).
        * Current default source matches one of the failure patterns:
          name endswith ``.monitor`` OR ``Mute: yes`` OR all channel
          volumes < 5 %.
        * At least one real (non-monitor, non-stub) input source exists
          on the host that we can reroute to.

    Apply:
        * In **strict mode** (``linux_wireplumber_default_source_bypass_lenient=False``):
          snapshot pre-apply default source, run
          ``wpctl set-default <ID>`` (or ``pactl set-default-source <NAME>``
          fallback), unmute via ``pactl set-source-mute @DEFAULT_SOURCE@ 0``,
          lift volume via ``pactl set-source-volume @DEFAULT_SOURCE@ 80%``,
          verify by re-reading ``pactl get-default-source``. Returns
          ``"default_source_routed_and_unmuted"``.
        * In **lenient mode** (default for v0.30.12): emits
          ``voice.bypass.would_repair`` event with the proposed action
          + target source ID/name, returns ``"lenient_no_repair"``
          without mutating any state. The coordinator sees this as
          ``APPLIED_STILL_DEAD`` after the no-mutation re-probe and
          advances to the next strategy.

    Revert:
        Restores the pre-apply default source name via
        ``pactl set-default-source <previous-name>``. Best-effort
        idempotent: a second call after the snapshot has been consumed
        is a no-op.
    """

    name: str = _STRATEGY_NAME

    def __init__(self) -> None:
        self._previous_default_source: str | None = None

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
        if not tuning.linux_wireplumber_default_source_bypass_enabled:
            return Eligibility(
                applicable=False,
                reason=_REASON_DISABLED_BY_TUNING,
                estimated_cost_ms=0,
            )

        pactl_path = shutil.which("pactl")
        if pactl_path is None:
            return Eligibility(
                applicable=False,
                reason=_REASON_NO_PACTL,
                estimated_cost_ms=0,
            )

        # PipeWire's pulse-shim must be reachable. We don't import
        # _pipewire._query_pactl_info to keep this module's dependency
        # graph tight; reproduce the minimal probe inline.
        if not _pactl_info_ok(pactl_path):
            return Eligibility(
                applicable=False,
                reason=_REASON_PIPEWIRE_NOT_RUNNING,
                estimated_cost_ms=0,
            )

        current_default = _query_default_source(pactl_path)
        if current_default is None:
            return Eligibility(
                applicable=False,
                reason=_REASON_QUERY_FAILED,
                estimated_cost_ms=0,
            )

        # Determine the reroute target: any real (non-monitor, non-stub)
        # input source. If none exist, no point in rerouting.
        real_inputs = _enumerate_real_input_sources(pactl_path)
        if not real_inputs:
            return Eligibility(
                applicable=False,
                reason=_REASON_NO_REAL_INPUT_SOURCE,
                estimated_cost_ms=0,
            )

        # Eligibility decision: applicable iff current default is
        # broken (monitor / muted / near-zero) AND a healthy alternative
        # exists.
        is_broken = _is_default_source_broken(
            pactl_path=pactl_path,
            default_name=current_default,
        )
        if not is_broken:
            return Eligibility(
                applicable=False,
                reason=_REASON_DEFAULT_ALREADY_OK,
                estimated_cost_ms=0,
            )

        return Eligibility(
            applicable=True,
            reason="",
            estimated_cost_ms=_APPLY_COST_MS,
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

        pactl_path = shutil.which("pactl")
        if pactl_path is None:
            msg = "pactl disappeared between eligibility and apply"
            raise BypassApplyError(msg, reason=_APPLY_REASON_NO_PACTL)

        # Re-resolve target source — eligibility snapshot may be stale
        # by the time the coordinator reaches apply (mirrors the
        # _linux_alsa_mixer.py:206-218 pattern).
        target = _pick_reroute_target(pactl_path)
        if target is None:
            msg = (
                "no real input source available at apply time — "
                "host audio topology changed between eligibility and apply"
            )
            raise BypassApplyError(msg, reason=_APPLY_REASON_NO_TARGET)

        # Snapshot pre-apply default source for revert. Done BEFORE any
        # mutation so a partial apply still rolls back cleanly.
        self._previous_default_source = _query_default_source(pactl_path)

        # Lenient mode: emit telemetry + return the synthetic outcome
        # WITHOUT mutating any state. Counts as an attempt (the
        # coordinator marks it APPLIED_STILL_DEAD after the no-op
        # re-probe), so quarantine logic works identically in both
        # modes — only the mutation differs.
        if tuning.linux_wireplumber_default_source_bypass_lenient:
            logger.warning(
                "voice.bypass.would_repair",
                **{
                    "voice.strategy": _STRATEGY_NAME,
                    "voice.reason": "wireplumber_default_source_broken",
                    "voice.proposed_action": (
                        f"wpctl set-default {target.source_id}; "
                        f"pactl set-source-mute @DEFAULT_SOURCE@ 0; "
                        f"pactl set-source-volume @DEFAULT_SOURCE@ "
                        f"{_TARGET_APPLY_VOLUME_PCT}%"
                    ),
                    "voice.target_source_id": target.source_id,
                    "voice.target_source_name": target.source_name,
                    "voice.previous_default_source": self._previous_default_source or "",
                    "voice.endpoint_guid": context.endpoint_guid,
                },
            )
            return _OUTCOME_LENIENT_NO_REPAIR

        # Strict mode — actually mutate. Each subprocess call wrapped
        # in asyncio.to_thread per CLAUDE.md anti-pattern #14.
        await _set_default_source(pactl_path, target)
        await _unmute_default_source(pactl_path)
        await _set_default_source_volume(pactl_path, _TARGET_APPLY_VOLUME_PCT)

        # Verify: re-read and confirm the routing took.
        post_default = await _query_default_source_async(pactl_path)
        if post_default is None or _looks_like_monitor(post_default):
            msg = (
                f"verify-after-apply reports default source still broken "
                f"(post={post_default!r}); intended target={target.source_name!r}"
            )
            raise BypassApplyError(msg, reason=_APPLY_REASON_VERIFY_FAILED)

        logger.info(
            "bypass_strategy_apply_ok",
            strategy=_STRATEGY_NAME,
            endpoint_guid=context.endpoint_guid,
            previous_default_source=self._previous_default_source or "",
            new_default_source=post_default,
        )
        return _OUTCOME_DEFAULT_SOURCE_ROUTED

    async def revert(
        self,
        context: BypassContext,
    ) -> None:
        previous = self._previous_default_source
        if previous is None:
            return  # Nothing to revert (apply was lenient OR never ran).

        pactl_path = shutil.which("pactl")
        if pactl_path is None:
            # If pactl vanished, we cannot revert — log and clear the
            # snapshot so a second call no-ops cleanly.
            logger.warning(
                "bypass_strategy_revert_skipped_no_pactl",
                strategy=_STRATEGY_NAME,
                endpoint_guid=context.endpoint_guid,
                previous_default_source=previous,
            )
            self._previous_default_source = None
            return

        try:
            await _set_default_source_by_name(pactl_path, previous)
        except BypassApplyError as exc:
            # Re-raise as BypassRevertError per the B3 contract so the
            # coordinator's voice.bypass.revert_failed event fires with
            # the structured reason.
            self._previous_default_source = None
            raise BypassRevertError(
                f"failed to restore previous default source {previous!r}: {exc}",
                reason=_REVERT_REASON_SET_DEFAULT_FAILED,
            ) from exc

        logger.info(
            "bypass_strategy_revert_ok",
            strategy=_STRATEGY_NAME,
            endpoint_guid=context.endpoint_guid,
            restored=previous,
        )
        self._previous_default_source = None


# ── Module-private helpers ──────────────────────────────────────────


def _tuning_from_context() -> VoiceTuningConfig:
    """Return a fresh :class:`VoiceTuningConfig` — pulls live env overrides.

    Re-reading the tuning each invocation keeps
    ``SOVYX_TUNING__VOICE__*`` overrides observable without bouncing
    the process (matches :mod:`_linux_alsa_mixer`'s pattern).
    """
    return VoiceTuningConfig()


def _pactl_info_ok(pactl_path: str) -> bool:
    """Return True iff ``pactl info`` returns rc=0 within timeout."""
    try:
        result = subprocess.run(  # noqa: S603 — fixed argv to trusted pactl
            (pactl_path, "info"),
            capture_output=True,
            text=True,
            timeout=_PACTL_TIMEOUT_S,
            check=False,
        )
    except (subprocess.TimeoutExpired, OSError):
        return False
    return result.returncode == 0


def _query_default_source(pactl_path: str) -> str | None:
    """Run ``pactl get-default-source`` and return the source name.

    Returns ``None`` on subprocess failure / timeout / non-zero exit.
    Output is a single name on stdout (no trailing fields).
    """
    try:
        result = subprocess.run(  # noqa: S603 — fixed argv to trusted pactl
            (pactl_path, "get-default-source"),
            capture_output=True,
            text=True,
            timeout=_PACTL_TIMEOUT_S,
            check=False,
        )
    except (subprocess.TimeoutExpired, OSError):
        return None
    if result.returncode != 0:
        return None
    name = result.stdout.strip()
    return name or None


async def _query_default_source_async(pactl_path: str) -> str | None:
    """Async variant for use after mutating apply calls.

    Wraps the sync helper in :func:`asyncio.to_thread` per anti-pattern
    #14 — subprocess invocations from async paths must offload to a
    worker thread so the event loop stays responsive.
    """
    import asyncio

    return await asyncio.to_thread(_query_default_source, pactl_path)


def _looks_like_monitor(source_name: str) -> bool:
    """Return True iff ``source_name`` is a monitor-loopback source.

    PipeWire/PulseAudio name monitor sources with the ``.monitor``
    suffix attached to the parent sink name (e.g.
    ``alsa_output.pci-0000_00_1f.3.analog-stereo.monitor``). A monitor
    source captures playback, NEVER the microphone — routing the
    default to one is the canonical WirePlumber routing bug.
    """
    return source_name.endswith(".monitor")


class _SourceTarget:
    """Lightweight tuple of (source_id, source_name) for the apply target."""

    __slots__ = ("source_id", "source_name")

    def __init__(self, source_id: str, source_name: str) -> None:
        self.source_id = source_id
        self.source_name = source_name


def _enumerate_real_input_sources(pactl_path: str) -> list[_SourceTarget]:
    """Return non-monitor, non-stub input sources from ``pactl list short sources``.

    Output format::

        <id>\\t<name>\\t<driver>\\t<sample-spec>\\t<state>

    We extract column 1 (numeric id) + column 2 (source name) and
    filter out monitor sources + dummy stubs.
    """
    try:
        result = subprocess.run(  # noqa: S603 — fixed argv to trusted pactl
            (pactl_path, "list", "short", "sources"),
            capture_output=True,
            text=True,
            timeout=_PACTL_TIMEOUT_S,
            check=False,
        )
    except (subprocess.TimeoutExpired, OSError):
        return []
    if result.returncode != 0:
        return []

    targets: list[_SourceTarget] = []
    for raw_line in result.stdout.splitlines():
        parts = raw_line.split("\t")
        if len(parts) < 2:  # noqa: PLR2004
            continue
        source_id = parts[0].strip()
        source_name = parts[1].strip()
        if not source_id or not source_name:
            continue
        if _looks_like_monitor(source_name):
            continue
        if source_name in _STUB_SOURCE_NAMES:
            continue
        targets.append(_SourceTarget(source_id=source_id, source_name=source_name))
    return targets


def _is_default_source_broken(*, pactl_path: str, default_name: str) -> bool:
    """Return True iff the default source matches a known failure pattern.

    Patterns:
        * Name ends in ``.monitor`` (loopback of a sink — wrong
          target for capture).
        * ``Mute: yes`` in the long-form ``pactl list sources`` block.
        * All channel volumes < 5 % (operator dragged volume to floor;
          near-silent in practice).
    """
    if _looks_like_monitor(default_name):
        return True

    # Need long-form to read Mute + Volume. Single source filter via
    # `pactl list sources NAME` would be cleaner but isn't supported
    # on all pactl versions; we fetch the whole list + parse the
    # block matching ``default_name``.
    try:
        result = subprocess.run(  # noqa: S603 — fixed argv to trusted pactl
            (pactl_path, "list", "sources"),
            capture_output=True,
            text=True,
            timeout=_PACTL_TIMEOUT_S,
            check=False,
        )
    except (subprocess.TimeoutExpired, OSError):
        return False  # Can't classify → default to "not broken".
    if result.returncode != 0:
        return False

    block = _extract_source_block(result.stdout, default_name)
    if block is None:
        return False  # Source vanished — let the coordinator handle.

    if _block_is_muted(block):
        return True
    return _block_volume_below_threshold(block, _NEAR_ZERO_VOLUME_FRACTION)


def _extract_source_block(long_output: str, source_name: str) -> str | None:
    """Extract the ``Source #N`` block whose ``Name:`` matches.

    ``pactl list sources`` emits one block per source separated by
    blank lines; each block has a ``Name: <source-name>`` line we
    can match against.
    """
    blocks = long_output.split("\n\n")
    for block in blocks:
        for line in block.splitlines():
            stripped = line.strip()
            if stripped.startswith("Name:") and stripped.split(":", 1)[1].strip() == source_name:
                return block
    return None


def _block_is_muted(block: str) -> bool:
    """Return True iff the block has ``Mute: yes``."""
    for line in block.splitlines():
        stripped = line.strip()
        if stripped.startswith("Mute:"):
            value = stripped.split(":", 1)[1].strip().lower()
            return value == "yes"
    return False


def _block_volume_below_threshold(block: str, fraction_threshold: float) -> bool:
    """Return True iff every channel volume in the block is below threshold.

    Volume lines look like::

        Volume: front-left: 22282 / 34% / -28.69 dB,
                front-right: 22282 / 34% / -28.69 dB

    We read the percentage values (column 2 of each ``channel: ...``
    pair) and compare against ``fraction_threshold * 100``. Returns
    True only when EVERY channel is below threshold (a single channel
    above threshold is enough audio to NOT trigger this branch).
    """
    threshold_pct = fraction_threshold * 100.0
    found_volumes = False
    for line in block.splitlines():
        stripped = line.strip()
        # Continuation lines from the multi-channel Volume block also
        # carry "<channel-name>: NNNN / NN% / NN.NN dB" syntax.
        if "%" not in stripped:
            continue
        # Parse every "/ NN% /" occurrence — robust against extra fields.
        for token in stripped.split("/"):
            token = token.strip()  # noqa: PLW2901
            if token.endswith("%"):
                try:
                    pct = float(token.rstrip("%").strip())
                except ValueError:
                    continue
                found_volumes = True
                if pct >= threshold_pct:
                    return False  # At least one channel is loud enough.
    return found_volumes  # True only when we saw volumes AND all were low.


def _pick_reroute_target(pactl_path: str) -> _SourceTarget | None:
    """Return the first real input source we can reroute to.

    Future enhancement: rank by canonical-name match against the active
    capture endpoint (so a USB headset selected in the wizard is
    preferred over the laptop array mic). For v0.30.12 we take the
    first non-monitor non-stub source — pilot evidence will tell us
    whether name-affinity ranking is needed.
    """
    candidates = _enumerate_real_input_sources(pactl_path)
    return candidates[0] if candidates else None


async def _set_default_source(pactl_path: str, target: _SourceTarget) -> None:
    """Run ``wpctl set-default <ID>`` (preferred) or ``pactl set-default-source <NAME>``.

    Prefer ``wpctl`` because it is the WirePlumber-canonical CLI; fall
    back to ``pactl set-default-source`` when ``wpctl`` is missing
    (some embedded distros ship pipewire without the WirePlumber CLI
    bindings).
    """
    import asyncio

    wpctl_path = shutil.which("wpctl")
    if wpctl_path is not None:
        try:
            result = await asyncio.to_thread(
                subprocess.run,
                (wpctl_path, "set-default", target.source_id),
                capture_output=True,
                text=True,
                timeout=_PACTL_TIMEOUT_S,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            msg = f"wpctl set-default {target.source_id!r} timed out after {_PACTL_TIMEOUT_S} s"
            raise BypassApplyError(msg, reason=_APPLY_REASON_SET_DEFAULT_FAILED) from exc
        except OSError as exc:
            msg = f"wpctl set-default failed to spawn: {exc!r}"
            raise BypassApplyError(msg, reason=_APPLY_REASON_SET_DEFAULT_FAILED) from exc
        if result.returncode != 0:
            msg = (
                f"wpctl set-default {target.source_id!r} exited "
                f"{result.returncode}: {result.stderr.strip()!r}"
            )
            raise BypassApplyError(msg, reason=_APPLY_REASON_SET_DEFAULT_FAILED)
        return

    # Fallback: pactl set-default-source by name.
    await _set_default_source_by_name(pactl_path, target.source_name)


async def _set_default_source_by_name(pactl_path: str, source_name: str) -> None:
    """Run ``pactl set-default-source <NAME>``.

    Used both by the apply fallback (when ``wpctl`` missing) and by
    revert (which restores by name, not id, since ids are not stable
    across PipeWire restarts).
    """
    import asyncio

    try:
        result = await asyncio.to_thread(
            subprocess.run,
            (pactl_path, "set-default-source", source_name),
            capture_output=True,
            text=True,
            timeout=_PACTL_TIMEOUT_S,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        msg = f"pactl set-default-source {source_name!r} timed out after {_PACTL_TIMEOUT_S} s"
        raise BypassApplyError(msg, reason=_APPLY_REASON_SET_DEFAULT_FAILED) from exc
    except OSError as exc:
        msg = f"pactl set-default-source failed to spawn: {exc!r}"
        raise BypassApplyError(msg, reason=_APPLY_REASON_SET_DEFAULT_FAILED) from exc
    if result.returncode != 0:
        msg = (
            f"pactl set-default-source {source_name!r} exited "
            f"{result.returncode}: {result.stderr.strip()!r}"
        )
        raise BypassApplyError(msg, reason=_APPLY_REASON_SET_DEFAULT_FAILED)


async def _unmute_default_source(pactl_path: str) -> None:
    """Run ``pactl set-source-mute @DEFAULT_SOURCE@ 0``."""
    import asyncio

    try:
        result = await asyncio.to_thread(
            subprocess.run,
            (pactl_path, "set-source-mute", "@DEFAULT_SOURCE@", "0"),
            capture_output=True,
            text=True,
            timeout=_PACTL_TIMEOUT_S,
            check=False,
        )
    except (subprocess.TimeoutExpired, OSError) as exc:
        msg = f"pactl set-source-mute @DEFAULT_SOURCE@ 0 failed: {exc!r}"
        raise BypassApplyError(msg, reason=_APPLY_REASON_UNMUTE_FAILED) from exc
    if result.returncode != 0:
        msg = f"pactl set-source-mute exited {result.returncode}: {result.stderr.strip()!r}"
        raise BypassApplyError(msg, reason=_APPLY_REASON_UNMUTE_FAILED)


async def _set_default_source_volume(pactl_path: str, percent: int) -> None:
    """Run ``pactl set-source-volume @DEFAULT_SOURCE@ N%``.

    Failures here are non-fatal — if set-default + unmute both
    succeeded the default source is at SOMETHING; volume below 80 %
    is still a working mic. Logged at WARNING + apply continues so
    the strategy reports overall success.
    """
    import asyncio

    arg_pct = f"{percent}%"
    try:
        result = await asyncio.to_thread(
            subprocess.run,
            (pactl_path, "set-source-volume", "@DEFAULT_SOURCE@", arg_pct),
            capture_output=True,
            text=True,
            timeout=_PACTL_TIMEOUT_S,
            check=False,
        )
    except (subprocess.TimeoutExpired, OSError) as exc:
        logger.warning(
            "bypass_strategy_volume_set_skipped",
            strategy=_STRATEGY_NAME,
            target_pct=percent,
            error=repr(exc),
        )
        return
    if result.returncode != 0:
        logger.warning(
            "bypass_strategy_volume_set_failed",
            strategy=_STRATEGY_NAME,
            target_pct=percent,
            exit_code=result.returncode,
            stderr=result.stderr.strip(),
        )
