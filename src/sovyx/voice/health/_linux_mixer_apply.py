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
from sovyx.voice.health.contract import (
    MixerApplySnapshot,
    MixerControlRole,
    MixerPresetValueDb,
    MixerPresetValueFraction,
    MixerPresetValueRaw,
)

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from sovyx.engine.config import VoiceTuningConfig
    from sovyx.voice.health.contract import (
        MixerControlSnapshot,
        MixerPresetSpec,
        MixerPresetValue,
    )

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
REASON_PRESET_ROLE_MISSING = "preset_role_missing"
"""Preset references a role the role_mapping does not expose on this card."""
REASON_PRESET_DB_NOT_SUPPORTED = "preset_db_not_supported"
"""F1 limitation — dB-targeted preset value requires richer probe data
(full raw↔dB curve) than :class:`MixerControlSnapshot` currently
carries. Profile authors use ``fraction`` or ``raw`` instead. F2 can
lift this restriction by extending the probe to sample multiple raw
values and interpolate the dB curve.
"""


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
    # ``--`` terminates option parsing so a hostile / quirky codec
    # exposing a control name beginning with ``-`` (e.g. ``-D`` which
    # amixer would otherwise interpret as a device selector) cannot
    # smuggle a flag into our invocation (paranoid-QA HIGH #5).
    argv = [
        "amixer",
        "-c",
        str(card_index),
        "--",
        "sset",
        control_name,
        str(target_raw),
    ]
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


_AUTO_MUTE_MODE_CONTROL_NAME = "Auto-Mute Mode"
"""Canonical control name for the HDA ``auto_mute`` toggle.

HDA codecs expose a single enum control named ``Auto-Mute Mode`` with
values like ``Disabled`` / ``Enabled``. ``amixer sset`` accepts the
label directly. For F1, the apply layer assumes this exact name when
the preset requests an ``auto_mute_mode`` change. When the role map
contains an ``AUTO_MUTE`` role with a differently-named control, the
resolved name is preferred over the canonical.
"""


async def apply_mixer_preset(
    card_index: int,
    preset: MixerPresetSpec,
    role_mapping: Mapping[MixerControlRole, tuple[MixerControlSnapshot, ...]],
    *,
    tuning: VoiceTuningConfig,
) -> MixerApplySnapshot:
    """Apply a KB-driven preset to ``card_index``, with LIFO rollback.

    Walks ``preset.controls`` in order and, for each
    :class:`MixerPresetControl`, looks up every
    :class:`MixerControlSnapshot` that resolves to the preset's role
    via ``role_mapping`` (a :class:`MixerControlRole` may map to
    multiple snapshots — e.g. desktop HDA has ``Front Mic Boost`` and
    ``Rear Mic Boost`` both resolving to ``PREAMP_BOOST``; the apply
    layer mutates every one so invariant I5 rollback-records each).

    Preset-value dispatch:

    * :class:`MixerPresetValueRaw` — target clamped to ``[min_raw, max_raw]``.
    * :class:`MixerPresetValueFraction` — linear interpolation on the
      control's raw range.
    * :class:`MixerPresetValueDb` — raises
      :class:`BypassApplyError` with
      :data:`REASON_PRESET_DB_NOT_SUPPORTED` in F1 (see token doc).

    Also handles the optional ``preset.auto_mute_mode`` toggle by
    ``sset``-ing the enum label on the ``AUTO_MUTE``-resolved control
    (or the canonical ``Auto-Mute Mode`` name as fallback). Rollback
    records the pre-apply string label.

    ``preset.runtime_pm_target`` is a no-op in F1 — it is honoured by
    the systemd oneshot shipped in Phase F1.G. When the caller
    requests a non-``leave`` value, the apply layer logs
    ``linux_mixer_runtime_pm_deferred`` at INFO so operators can
    confirm the deferral is visible.

    Atomicity model: identical to :func:`apply_mixer_reset`. Every
    successful mutation appends ``(name, pre_apply_raw_or_label)``
    to the rollback log; any subsequent failure or cancellation
    triggers LIFO rollback before re-raising.

    Args:
        card_index: ALSA card index to mutate.
        preset: Immutable preset spec, sourced from a
            :class:`~sovyx.voice.health.contract.MixerKBProfile`.
        role_mapping: Output of
            :meth:`MixerControlRoleResolver.resolve_card` for the
            same card — tuple-valued per design deviation
            (Phase F1.B).
        tuning: Voice tuning config — supplies the subprocess
            timeout.

    Returns:
        :class:`MixerApplySnapshot` whose ``reverted_controls`` list
        feeds :func:`restore_mixer_snapshot`.

    Raises:
        BypassApplyError: On any platform / environment gate or
            subprocess failure. Reason tokens:

            * ``not_linux`` — wrong platform.
            * ``amixer_unavailable`` — binary missing from PATH.
            * ``preset_role_missing`` — preset references a role
              absent from ``role_mapping``.
            * ``preset_db_not_supported`` — dB variant in F1.
            * ``amixer_timeout`` / ``amixer_set_failed`` — subprocess
              layer (same as :func:`apply_mixer_reset`).
    """
    if sys.platform != "linux":
        msg = f"apply_mixer_preset is Linux-only; running on {sys.platform}"
        raise BypassApplyError(msg, reason=REASON_NOT_LINUX)
    if shutil.which("amixer") is None:
        msg = "amixer binary not found on PATH (install alsa-utils)"
        raise BypassApplyError(msg, reason=REASON_AMIXER_UNAVAILABLE)

    timeout_s = tuning.linux_mixer_subprocess_timeout_s

    # Pre-validate the entire preset so we don't partially-apply a
    # broken spec. Any REASON_PRESET_* raised here happens before a
    # single amixer call, so no rollback is needed.
    for pc in preset.controls:
        targets = role_mapping.get(pc.role)
        if not targets:
            msg = (
                f"preset targets role={pc.role.value!r} but the role_mapping "
                f"on card {card_index} has no control for it"
            )
            raise BypassApplyError(msg, reason=REASON_PRESET_ROLE_MISSING)
        if isinstance(pc.value, MixerPresetValueDb):
            msg = (
                f"preset value {{db: {pc.value.db}}} for role "
                f"{pc.role.value!r} cannot be applied in F1 — profile "
                "authors must use 'raw' or 'fraction' until F2 extends "
                "the probe with the raw↔dB curve"
            )
            raise BypassApplyError(msg, reason=REASON_PRESET_DB_NOT_SUPPORTED)

    rollback_log: list[tuple[str, int]] = []
    applied_log: list[tuple[str, int]] = []
    # Parallel rollback list for string-valued (enum) mutations —
    # Auto-Mute Mode rolls back by label, not raw int. Kept separate
    # so MixerApplySnapshot.reverted_controls stays int-typed (its
    # contract). Restore is best-effort via _rollback_enum_best_effort.
    enum_rollback_log: list[tuple[str, str]] = []

    try:
        for pc in preset.controls:
            targets = role_mapping[pc.role]
            target_raw = _translate_preset_value(pc.value, targets[0])
            for snapshot in targets:
                # Re-clamp per-snapshot so differently-ranged siblings
                # (same role, different max_raw) don't share the first
                # control's raw value blindly.
                per_snapshot_raw = _translate_preset_value(pc.value, snapshot)
                if per_snapshot_raw == snapshot.current_raw:
                    continue
                await asyncio.to_thread(
                    _amixer_set,
                    card_index,
                    snapshot.name,
                    per_snapshot_raw,
                    timeout_s,
                )
                rollback_log.append((snapshot.name, snapshot.current_raw))
                applied_log.append((snapshot.name, per_snapshot_raw))
            # Preserve the first target_raw into applied_log only via
            # the per-snapshot loop above; suppress unused warning here.
            del target_raw

        if preset.auto_mute_mode != "leave":
            await _apply_auto_mute(
                card_index,
                preset.auto_mute_mode,
                role_mapping=role_mapping,
                enum_rollback_log=enum_rollback_log,
                timeout_s=timeout_s,
            )

        if preset.runtime_pm_target != "leave":
            logger.info(
                "linux_mixer_runtime_pm_deferred",
                card_index=card_index,
                requested_target=preset.runtime_pm_target,
                note="runtime_pm is handled by systemd oneshot (F1.G)",
            )
    except asyncio.CancelledError:
        # Paranoid-QA R2 LOW #5: LIFO order requires enum rollback
        # FIRST — the ``_apply_auto_mute`` call runs AFTER the
        # numeric loop, so its rollback must come BEFORE the
        # numeric LIFO walk to preserve the "undo in reverse
        # commit order" invariant. Reverse order left a transient
        # inconsistent state (numeric reverted + enum still in
        # applied state) that, while settling to the correct
        # terminal state, violated the atomicity contract
        # apply_mixer_preset's docstring advertises.
        await _rollback_enum_best_effort(
            card_index,
            enum_rollback_log,
            timeout_s=timeout_s,
        )
        await _rollback_best_effort(card_index, rollback_log, timeout_s=timeout_s)
        raise
    except BypassApplyError:
        # Same LIFO ordering as the CancelledError branch — see above.
        await _rollback_enum_best_effort(
            card_index,
            enum_rollback_log,
            timeout_s=timeout_s,
        )
        await _rollback_best_effort(card_index, rollback_log, timeout_s=timeout_s)
        raise

    return MixerApplySnapshot(
        card_index=card_index,
        reverted_controls=tuple(rollback_log),
        applied_controls=tuple(applied_log),
    )


def _translate_preset_value(
    value: MixerPresetValue,
    control: MixerControlSnapshot,
) -> int:
    """Translate a tagged-union preset value into a raw integer target.

    Raw values are clamped to ``[min_raw, max_raw]``. Fractions
    interpolate linearly across the control's raw span. dB variants
    reach this helper only when the preset pre-validation missed —
    defensive double-check retained because the tagged-union
    invariant lives in contract.py, not here.
    """
    if isinstance(value, MixerPresetValueRaw):
        return _clamp_raw(value.raw, control)
    if isinstance(value, MixerPresetValueFraction):
        span = control.max_raw - control.min_raw
        target = control.min_raw + int(round(span * value.fraction))
        return _clamp_raw(target, control)
    if isinstance(value, MixerPresetValueDb):
        # Defensive — caller pre-validation rejects dB in F1.
        msg = f"dB preset value {value.db} on {control.name!r} not supported in F1"
        raise BypassApplyError(msg, reason=REASON_PRESET_DB_NOT_SUPPORTED)
    # mypy exhaustiveness — every MixerPresetValue variant covered above.
    msg = f"unexpected preset value type {type(value).__name__}"
    raise BypassApplyError(msg, reason=REASON_PRESET_DB_NOT_SUPPORTED)  # pragma: no cover


def _clamp_raw(target: int, control: MixerControlSnapshot) -> int:
    if target < control.min_raw:
        return control.min_raw
    if target > control.max_raw:
        return control.max_raw
    return target


async def _apply_auto_mute(
    card_index: int,
    target_mode: str,
    *,
    role_mapping: Mapping[MixerControlRole, tuple[MixerControlSnapshot, ...]],
    enum_rollback_log: list[tuple[str, str]],
    timeout_s: float,
) -> None:
    """Toggle HDA Auto-Mute Mode via amixer enum label.

    Uses the name from the :attr:`MixerControlRole.AUTO_MUTE`-resolved
    control when available; otherwise falls back to the canonical
    :data:`_AUTO_MUTE_MODE_CONTROL_NAME`. Records the pre-apply label
    so :func:`_rollback_enum_best_effort` can restore it on failure.

    ``target_mode`` is the preset's YAML value (``"disabled"`` /
    ``"enabled"``); converted to the ``amixer``-canonical
    ``"Disabled"`` / ``"Enabled"`` capitalized label here.
    """
    auto_mute_snapshots = role_mapping.get(MixerControlRole.AUTO_MUTE, ())
    control_name = (
        auto_mute_snapshots[0].name if auto_mute_snapshots else _AUTO_MUTE_MODE_CONTROL_NAME
    )
    amixer_label = "Enabled" if target_mode == "enabled" else "Disabled"
    pre_apply_label = await asyncio.to_thread(
        _amixer_get_enum,
        card_index,
        control_name,
        timeout_s,
    )
    # Some cards omit the Auto-Mute Mode control entirely — skip the
    # write silently if we couldn't read the current label (the pre-
    # apply probe would otherwise have surfaced it).
    if pre_apply_label is None:
        logger.debug(
            "linux_mixer_auto_mute_control_absent",
            card_index=card_index,
            control_name=control_name,
        )
        return
    if pre_apply_label == amixer_label:
        return
    await asyncio.to_thread(
        _amixer_set_enum,
        card_index,
        control_name,
        amixer_label,
        timeout_s,
    )
    enum_rollback_log.append((control_name, pre_apply_label))


def _amixer_get_enum(
    card_index: int,
    control_name: str,
    timeout_s: float,
) -> str | None:
    """Read the current enum label for ``control_name`` via ``amixer get``.

    Returns ``None`` when the control is absent or the output doesn't
    expose an ``Item0:`` line (the amixer convention for enum-typed
    control values). Never raises — failure upstream causes the
    caller to skip the write.
    """
    # ``--`` terminates amixer option parsing (paranoid-QA HIGH #5).
    argv = ["amixer", "-c", str(card_index), "--", "get", control_name]
    try:
        proc = subprocess.run(  # noqa: S603 — fixed argv, no shell, timeout enforced
            argv,
            capture_output=True,
            timeout=timeout_s,
            check=False,
            text=True,
            errors="replace",
        )
    except (subprocess.SubprocessError, OSError):
        return None
    if proc.returncode != 0:
        return None
    for line in proc.stdout.splitlines():
        stripped = line.strip()
        if stripped.startswith("Item0:"):
            # "Item0: 'Disabled'" → "Disabled"
            rest = stripped.removeprefix("Item0:").strip()
            return rest.strip("'\"")
    return None


def _amixer_set_enum(
    card_index: int,
    control_name: str,
    label: str,
    timeout_s: float,
) -> None:
    """Write an enum label — delegates to :func:`_amixer_set` argv shape."""
    # ``--`` terminates option parsing (paranoid-QA HIGH #5).
    argv = ["amixer", "-c", str(card_index), "--", "sset", control_name, label]
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
            f"amixer sset (enum) timed out after {timeout_s}s on card "
            f"{card_index} control {control_name!r}"
        )
        raise BypassApplyError(msg, reason=REASON_AMIXER_TIMEOUT) from exc
    except (subprocess.SubprocessError, OSError) as exc:
        msg = f"amixer sset (enum) failed on card {card_index} control {control_name!r}: {exc}"
        raise BypassApplyError(msg, reason=REASON_AMIXER_SET_FAILED) from exc

    if proc.returncode != 0:
        stderr = (proc.stderr or "").strip()[:200]
        msg = (
            f"amixer sset (enum) returned exit={proc.returncode} on card "
            f"{card_index} control {control_name!r}: {stderr}"
        )
        raise BypassApplyError(msg, reason=REASON_AMIXER_SET_FAILED)


async def _rollback_enum_best_effort(
    card_index: int,
    enum_rollback_log: Sequence[tuple[str, str]],
    *,
    timeout_s: float,
) -> None:
    """LIFO revert of enum (label) mutations. Never raises."""
    for control_name, pre_apply_label in reversed(enum_rollback_log):
        try:
            await asyncio.to_thread(
                _amixer_set_enum,
                card_index,
                control_name,
                pre_apply_label,
                timeout_s,
            )
        except BaseException as exc:  # noqa: BLE001 — rollback must never propagate
            logger.warning(
                "linux_mixer_enum_rollback_failed",
                card_index=card_index,
                control=control_name,
                target_label=pre_apply_label,
                detail=str(exc),
            )


__all__ = [
    "REASON_AMIXER_SET_FAILED",
    "REASON_AMIXER_TIMEOUT",
    "REASON_AMIXER_UNAVAILABLE",
    "REASON_NOT_LINUX",
    "REASON_NO_CONTROLS",
    "REASON_PRESET_DB_NOT_SUPPORTED",
    "REASON_PRESET_ROLE_MISSING",
    "apply_mixer_preset",
    "apply_mixer_reset",
    "restore_mixer_snapshot",
]
