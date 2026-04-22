"""Linux ALSA mixer mutation — apply a saturation-reset + drive rollback.

Stateless writer. Consumed by
:mod:`sovyx.voice.health.bypass._linux_alsa_mixer` (the
:class:`LinuxALSAMixerResetBypass` strategy) and by the dashboard's
``POST /api/voice/linux-mixer-reset`` endpoint.

Two-phase contract:

1. :func:`apply_mixer_reset` reads the pre-apply state for every control
   we are about to touch, then attempts each ``amixer sset`` in
   sequence. On any failure (non-zero exit, timeout, OS error) or on
   :class:`asyncio.CancelledError`, the function rolls back every
   control that had already been mutated in this call and raises
   :class:`BypassApplyError` — leaving the mixer in its pre-call state.
2. :func:`restore_mixer_snapshot` walks a :class:`MixerApplySnapshot`
   in reverse and restores the recorded pre-apply raw values. It is
   best-effort: a failure on one control does not abort the rest, and
   the function itself never raises.

All subprocess calls are wrapped in :func:`asyncio.to_thread` so a
misbehaving codec driver never blocks the event loop. Argv is fixed
(no shell) and ``amixer`` is resolved via :func:`shutil.which` so we
fail fast rather than inheriting ``PATH``.

See ``docs-internal/plans/linux-alsa-mixer-saturation-fix.md`` §2.3.4
for the derivation of the reset fractions and the rollback invariant.
"""

from __future__ import annotations

import asyncio
import shutil
import subprocess  # noqa: S404 — fixed-argv subprocess to trusted alsa-utils binary
import sys
from typing import TYPE_CHECKING

from sovyx.observability.logging import get_logger
from sovyx.voice.health.bypass._strategy import BypassApplyError
from sovyx.voice.health.contract import MixerApplySnapshot

if TYPE_CHECKING:
    from collections.abc import Sequence

    from sovyx.engine.config import VoiceTuningConfig
    from sovyx.voice.health.contract import MixerControlSnapshot

logger = get_logger(__name__)


_CAPTURE_NAME_TOKENS: tuple[str, ...] = ("capture",)
"""Substrings (case-insensitive) that identify a capture-path control.

A control whose lowered name contains any of these tokens is treated as
a capture-gain (e.g. ``"Capture"``, ``"Digital Capture Volume"``) and
reset to :attr:`VoiceTuningConfig.linux_mixer_capture_reset_fraction`
of its raw range. Every other boost-class control
(``"Internal Mic Boost"``, ``"Line Boost"``) is reset to
:attr:`VoiceTuningConfig.linux_mixer_boost_reset_fraction` — which
defaults to ``0.0`` (mute the boost entirely; only the capture gain
remains active).
"""


# Stable :class:`BypassApplyError.reason` tokens emitted by this module.
# Dashboards + telemetry key on these strings, so they are part of the
# public surface — treat any rename as a breaking change.
REASON_AMIXER_UNAVAILABLE = "amixer_unavailable"
REASON_AMIXER_TIMEOUT = "amixer_timeout"
REASON_AMIXER_SET_FAILED = "amixer_set_failed"
REASON_NOT_LINUX = "not_linux"
REASON_NO_CONTROLS = "no_controls_to_reset"


async def apply_mixer_reset(
    card_index: int,
    controls_to_reset: Sequence[MixerControlSnapshot],
    *,
    tuning: VoiceTuningConfig,
) -> MixerApplySnapshot:
    """Reduce boost/capture controls on ``card_index`` to safe fractions.

    Each control in ``controls_to_reset`` is set to
    ``int(max_raw * fraction)`` where ``fraction`` is
    :attr:`VoiceTuningConfig.linux_mixer_capture_reset_fraction` for
    capture-path controls (name contains ``"capture"``) and
    :attr:`VoiceTuningConfig.linux_mixer_boost_reset_fraction` for every
    other boost-class control.

    Atomicity model: the function records every successful mutation's
    ``(name, pre_apply_raw)`` pair into an internal rollback log. If
    any subsequent mutation fails or the coroutine is cancelled, every
    recorded pre-apply value is restored before re-raising. On a clean
    return the rollback log is frozen into the returned
    :class:`MixerApplySnapshot` so the caller can revert later via
    :func:`restore_mixer_snapshot`.

    Raises:
        BypassApplyError: ``amixer`` is not on PATH
            (``reason=amixer_unavailable``), the subprocess timed out
            (``reason=amixer_timeout``), or an individual ``sset`` call
            returned non-zero (``reason=amixer_set_failed``). The
            message includes the failing control name; the chained
            cause is the underlying subprocess error.
    """
    if sys.platform != "linux":
        msg = f"apply_mixer_reset is Linux-only; running on {sys.platform}"
        raise BypassApplyError(msg, reason=REASON_NOT_LINUX)
    if shutil.which("amixer") is None:
        msg = "amixer binary not found on PATH (install alsa-utils)"
        raise BypassApplyError(msg, reason=REASON_AMIXER_UNAVAILABLE)
    if not controls_to_reset:
        msg = "controls_to_reset is empty — nothing to do"
        raise BypassApplyError(msg, reason=REASON_NO_CONTROLS)

    timeout_s = tuning.linux_mixer_subprocess_timeout_s
    boost_fraction = tuning.linux_mixer_boost_reset_fraction
    capture_fraction = tuning.linux_mixer_capture_reset_fraction

    rollback_log: list[tuple[str, int]] = []
    applied_log: list[tuple[str, int]] = []

    try:
        for control in controls_to_reset:
            target_raw = _compute_target_raw(
                control,
                boost_fraction=boost_fraction,
                capture_fraction=capture_fraction,
            )
            if target_raw == control.current_raw:
                # Already at target — no mutation, no rollback entry.
                continue
            await asyncio.to_thread(
                _amixer_set,
                card_index,
                control.name,
                target_raw,
                timeout_s,
            )
            rollback_log.append((control.name, control.current_raw))
            applied_log.append((control.name, target_raw))
    except asyncio.CancelledError:
        await _rollback_best_effort(card_index, rollback_log, timeout_s=timeout_s)
        raise
    except BypassApplyError:
        await _rollback_best_effort(card_index, rollback_log, timeout_s=timeout_s)
        raise

    return MixerApplySnapshot(
        card_index=card_index,
        reverted_controls=tuple(rollback_log),
        applied_controls=tuple(applied_log),
    )


async def restore_mixer_snapshot(
    snapshot: MixerApplySnapshot,
    *,
    tuning: VoiceTuningConfig,
) -> None:
    """Restore every control in ``snapshot`` to its pre-apply raw value.

    Walks :attr:`MixerApplySnapshot.reverted_controls` in reverse so
    the last mutation is undone first (matches the LIFO stack the
    apply phase built). Best-effort: a failure on one control is
    logged at WARNING but does not abort the rest — partial revert is
    still strictly better than no revert. The function never raises.
    """
    if sys.platform != "linux":
        return
    if shutil.which("amixer") is None:
        logger.warning(
            "linux_mixer_restore_skipped_no_amixer",
            card_index=snapshot.card_index,
            pending_controls=len(snapshot.reverted_controls),
        )
        return

    timeout_s = tuning.linux_mixer_subprocess_timeout_s
    for name, raw in reversed(snapshot.reverted_controls):
        try:
            await asyncio.to_thread(
                _amixer_set,
                snapshot.card_index,
                name,
                raw,
                timeout_s,
            )
        except BypassApplyError as exc:
            logger.warning(
                "linux_mixer_restore_control_failed",
                card_index=snapshot.card_index,
                control=name,
                target_raw=raw,
                reason=exc.reason,
                detail=str(exc),
            )
        except asyncio.CancelledError:
            # Cancelled mid-restore — surface to caller so the
            # supervising task sees it, but don't swallow the intent
            # of a best-effort teardown.
            logger.warning(
                "linux_mixer_restore_cancelled",
                card_index=snapshot.card_index,
                control=name,
                pending_after=len(snapshot.reverted_controls),
            )
            raise


async def _rollback_best_effort(
    card_index: int,
    rollback_log: Sequence[tuple[str, int]],
    *,
    timeout_s: float,
) -> None:
    """Undo every entry in ``rollback_log`` in LIFO order, swallowing errors.

    Used by the apply-phase except handlers. A failure here is logged
    but never masks the original exception that triggered the rollback
    — the caller is already in the process of re-raising.
    """
    for name, raw in reversed(rollback_log):
        try:
            await asyncio.to_thread(_amixer_set, card_index, name, raw, timeout_s)
        except BaseException as exc:  # noqa: BLE001 — rollback must never propagate
            logger.warning(
                "linux_mixer_rollback_control_failed",
                card_index=card_index,
                control=name,
                target_raw=raw,
                detail=str(exc),
            )


def _compute_target_raw(
    control: MixerControlSnapshot,
    *,
    boost_fraction: float,
    capture_fraction: float,
) -> int:
    """Derive the raw integer target value for a reset mutation.

    Capture-path controls (name contains ``"capture"``) use
    ``capture_fraction`` so the device can still produce audible
    signal. Every other boost-class control uses ``boost_fraction`` —
    defaulting to ``0.0`` so the analog amplifier stage is effectively
    disabled.

    The target is clamped to ``[min_raw, max_raw]`` so a mis-tuned
    fraction (e.g. ``1.2``) cannot drive ``amixer`` out of range.
    """
    fraction = capture_fraction if _is_capture_name(control.name) else boost_fraction
    span = control.max_raw - control.min_raw
    target = control.min_raw + int(round(span * fraction))
    if target < control.min_raw:
        return control.min_raw
    if target > control.max_raw:
        return control.max_raw
    return target


def _is_capture_name(name: str) -> bool:
    lowered = name.lower()
    return any(tok in lowered for tok in _CAPTURE_NAME_TOKENS)


def _amixer_set(
    card_index: int,
    control_name: str,
    target_raw: int,
    timeout_s: float,
) -> None:
    """Blocking helper — invoked via :func:`asyncio.to_thread`.

    Runs ``amixer -c <index> sset '<name>' <raw>`` with a fixed argv
    list (no shell interpolation). A single raw value applied via
    ``sset`` fans out to every channel of a simple control — which is
    what we want for symmetric capture/boost reset.

    Translates every subprocess error into :class:`BypassApplyError`
    with a stable reason token so the caller can branch on
    ``.reason`` without string parsing.
    """
    argv = ["amixer", "-c", str(card_index), "sset", control_name, str(target_raw)]
    try:
        proc = subprocess.run(  # noqa: S603 — fixed argv, no shell, timeout enforced
            argv,
            capture_output=True,
            timeout=timeout_s,
            check=False,
            text=True,
            errors="replace",
        )
    except subprocess.TimeoutExpired as exc:
        msg = (
            f"amixer sset timed out after {timeout_s}s on card {card_index} "
            f"control {control_name!r}"
        )
        raise BypassApplyError(msg, reason=REASON_AMIXER_TIMEOUT) from exc
    except (subprocess.SubprocessError, OSError) as exc:
        msg = f"amixer sset subprocess failed on card {card_index} control {control_name!r}: {exc}"
        raise BypassApplyError(msg, reason=REASON_AMIXER_SET_FAILED) from exc

    if proc.returncode != 0:
        stderr = (proc.stderr or "").strip()[:200]
        msg = (
            f"amixer sset returned exit={proc.returncode} on card {card_index} "
            f"control {control_name!r}: {stderr}"
        )
        raise BypassApplyError(msg, reason=REASON_AMIXER_SET_FAILED)


__all__ = [
    "REASON_AMIXER_SET_FAILED",
    "REASON_AMIXER_TIMEOUT",
    "REASON_AMIXER_UNAVAILABLE",
    "REASON_NOT_LINUX",
    "REASON_NO_CONTROLS",
    "apply_mixer_reset",
    "restore_mixer_snapshot",
]
