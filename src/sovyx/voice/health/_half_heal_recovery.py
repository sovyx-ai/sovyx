"""Half-heal write-ahead log recovery — L2.5 crash safety (Phase F1).

Paranoid-QA R2 HIGH #3 closure. ``apply_mixer_preset`` keeps its
rollback log in memory; a mid-apply process death (SIGKILL, OOM,
kernel panic, hard power loss) loses that state and leaves the
ALSA mixer in a partially-applied configuration. The live mixer
state survives the crash (kernel keeps the ioctl-set raw values
until the next ``alsactl restore``), so the NEXT cascade in the
same boot sees a frankenstate that neither matches the factory-bad
signature nor the post-apply target, potentially re-applying a
preset on top of the half-broken state.

This module lifts the rollback intent to disk with the same
serialisation shape as :class:`MixerApplySnapshot`. The L2.5
orchestrator writes the WAL BEFORE the first ``amixer_set`` and
clears it on *any* terminal transition — success, rollback,
exception, cancellation. A WAL file on disk at cascade entry is
the sole cross-process signal that a prior invocation died
mid-apply; the recovery function replays the WAL via the caller's
``restore_fn`` and deletes the file.

Design constraints:

* **Atomic writes only.** ``tempfile`` + ``os.replace`` — a crash
  mid-write leaves the previous WAL (or no WAL at all), never a
  truncated or invalid file.
* **Forward compatible.** Schema version pinned in the JSON; a
  mismatched version aborts recovery with a WARN + deletes the
  file (the cascade then probes the live state and makes its own
  decisions — no fail-closed behaviour that would brick L2.5).
* **Best-effort cleanup.** ``clear`` is idempotent and never
  raises into the caller. The cascade continues regardless.
* **No cross-boot recovery.** The WAL is rooted in ``data_dir``;
  operators who nuke the data directory also nuke any pending WAL.
  Cross-boot integrity is handled separately by
  ``sovyx-audio-mixer-persist.service`` + ``/var/lib/alsa/asound.state``
  under systemd.
"""

from __future__ import annotations

import contextlib
import json
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

from sovyx.observability.logging import get_logger
from sovyx.voice.health.contract import MixerApplySnapshot

if TYPE_CHECKING:
    from sovyx.engine.config import VoiceTuningConfig


logger = get_logger(__name__)


_WAL_FILENAME = "mixer_sanity_half_heal.json"
"""Shared filename for the WAL, under ``data_dir/voice_health/``.

One file per process — L2.5 serialises every cascade via the
module-level lock in :mod:`_mixer_sanity`, so concurrent writes
are impossible by construction.
"""


_WAL_SCHEMA_VERSION = 1
"""Bump on shape changes. Mismatched versions abort recovery and
delete the stale WAL."""


class RestoreSnapshotFn(Protocol):
    """Callable shape for the injected rollback function.

    Expressed as a ``Protocol`` (not a ``Callable[...]`` alias)
    because :func:`sovyx.voice.health._linux_mixer_apply.restore_mixer_snapshot`
    takes ``tuning`` as a KEYWORD-only argument. ``Callable[[X, Y],
    ...]`` types are always positional; mypy-on-Linux correctly
    rejected that form when a keyword-only callable was passed in.
    A Protocol lets us describe the exact keyword-only signature.
    """

    async def __call__(
        self,
        snapshot: MixerApplySnapshot,
        *,
        tuning: VoiceTuningConfig,
    ) -> None: ...


@dataclass(frozen=True, slots=True)
class HalfHealWal:
    """Serialisable view of a pending mixer-sanity apply.

    Mirrors :class:`MixerApplySnapshot` — the two types have
    identical payload but different intents:

    * ``MixerApplySnapshot`` — result of a completed apply; carries
      both ``reverted_controls`` (pre-apply state for rollback) and
      ``applied_controls`` (post-apply state for audit).
    * ``HalfHealWal`` — intent to apply; carries ONLY
      ``reverted_controls`` because the post-apply state isn't
      known at WAL-write time (we write before the first mutation).

    On recovery, the WAL is promoted back to a
    :class:`MixerApplySnapshot` with ``applied_controls=()`` so the
    caller's ``restore_fn`` can consume it without a type-shape
    adapter. The empty ``applied_controls`` is harmless —
    ``restore_mixer_snapshot`` only reads ``reverted_controls``.
    """

    card_index: int
    reverted_controls: tuple[tuple[str, int], ...]

    def to_apply_snapshot(self) -> MixerApplySnapshot:
        """Promote to the shape :func:`restore_mixer_snapshot` consumes."""
        return MixerApplySnapshot(
            card_index=self.card_index,
            reverted_controls=self.reverted_controls,
            applied_controls=(),
        )


def default_wal_path(data_dir: Path) -> Path:
    """Absolute WAL location derived from the engine's ``data_dir``.

    Placed under ``voice_health/`` so a future multi-concern
    voice-health WAL set can share the subdirectory without
    polluting the top level of ``data_dir``.
    """
    return data_dir / "voice_health" / _WAL_FILENAME


def write_wal(
    *,
    card_index: int,
    reverted_controls: tuple[tuple[str, int], ...],
    path: Path,
) -> bool:
    """Persist a ``HalfHealWal`` atomically. Returns ``True`` on success.

    Called BEFORE the first ``amixer_set``. Atomicity guarantees:

    * Parent directory is created if missing (first-run path).
    * Temp file created in the destination directory so
      ``os.replace`` is a same-filesystem atomic rename.
    * ``fsync`` the temp before rename so the payload survives a
      kernel panic between ``write()`` and ``rename()``.

    The function never raises; I/O failures return ``False`` and
    log at DEBUG so the caller can decide whether to proceed
    without the safety net (production callers log a WARN if this
    returns False — the alternative is aborting the whole cascade
    on a transient disk hiccup, which is worse).
    """
    payload = {
        "schema_version": _WAL_SCHEMA_VERSION,
        "card_index": card_index,
        "reverted_controls": [[name, raw] for name, raw in reverted_controls],
    }
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        logger.debug(
            "mixer_half_heal_wal_mkdir_failed",
            path=str(path.parent),
            detail=str(exc),
        )
        return False
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            delete=False,
            dir=path.parent,
            prefix=".mixer_half_heal-",
            suffix=".json",
            encoding="utf-8",
        ) as tmp:
            json.dump(payload, tmp, sort_keys=True)
            tmp.flush()
            os.fsync(tmp.fileno())
            tmp_path = Path(tmp.name)
    except OSError as exc:
        logger.debug(
            "mixer_half_heal_wal_tempfile_failed",
            detail=str(exc),
        )
        return False
    try:
        os.replace(tmp_path, path)
    except OSError as exc:
        logger.debug(
            "mixer_half_heal_wal_replace_failed",
            detail=str(exc),
        )
        # Best-effort cleanup — a leaked tempfile is the smallest
        # concern in a disk-failure scenario.
        with contextlib.suppress(OSError):
            tmp_path.unlink(missing_ok=True)
        return False
    return True


def load_wal(path: Path) -> HalfHealWal | None:
    """Parse the WAL at ``path``. Returns ``None`` if absent or invalid.

    Any parse failure (missing fields, schema mismatch, bad JSON,
    I/O error) returns ``None`` and logs at WARN. The file is NOT
    deleted by this function — the caller (recovery driver) is
    responsible for cleanup so the deletion happens atomically
    with the replay.
    """
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except OSError as exc:
        logger.warning(
            "mixer_half_heal_wal_read_failed",
            path=str(path),
            detail=str(exc),
        )
        return None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        logger.warning(
            "mixer_half_heal_wal_parse_failed",
            path=str(path),
            detail=str(exc),
        )
        return None
    if not isinstance(data, dict):
        logger.warning(
            "mixer_half_heal_wal_shape_invalid",
            path=str(path),
            reason="top-level is not an object",
        )
        return None
    if data.get("schema_version") != _WAL_SCHEMA_VERSION:
        logger.warning(
            "mixer_half_heal_wal_schema_mismatch",
            path=str(path),
            expected=_WAL_SCHEMA_VERSION,
            got=data.get("schema_version"),
        )
        return None
    card_index = data.get("card_index")
    reverted = data.get("reverted_controls")
    if not isinstance(card_index, int) or not isinstance(reverted, list):
        logger.warning(
            "mixer_half_heal_wal_fields_invalid",
            path=str(path),
        )
        return None
    try:
        controls: tuple[tuple[str, int], ...] = tuple(
            (str(entry[0]), int(entry[1]))
            for entry in reverted
            if isinstance(entry, (list, tuple)) and len(entry) == 2
        )
    except (TypeError, ValueError, IndexError) as exc:
        logger.warning(
            "mixer_half_heal_wal_controls_invalid",
            path=str(path),
            detail=str(exc),
        )
        return None
    return HalfHealWal(card_index=card_index, reverted_controls=controls)


def clear_wal(path: Path) -> None:
    """Delete the WAL if it exists. Idempotent; never raises.

    Called after every terminal transition in the apply path —
    success, rollback, exception, cancellation. Called twice in a
    row is a no-op, so even belt-and-suspenders placement is fine.
    """
    try:
        path.unlink(missing_ok=True)
    except OSError as exc:
        logger.debug(
            "mixer_half_heal_wal_unlink_failed",
            path=str(path),
            detail=str(exc),
        )


async def recover_if_present(
    *,
    path: Path,
    restore_fn: RestoreSnapshotFn,
    tuning: VoiceTuningConfig,
) -> bool:
    """If a WAL exists at ``path``, replay it and delete the file.

    Returns ``True`` iff recovery ran (a WAL was found and replayed
    — regardless of whether the replay succeeded). ``False`` means
    no WAL present, nothing to do.

    The caller (``check_and_maybe_heal``) invokes this once at the
    top of the state machine, BEFORE the normal probe. After
    recovery the mixer is back to its pre-L2.5 state and the
    cascade can probe cleanly.

    Args:
        path: Absolute WAL location.
        restore_fn: Injected callable matching
            :func:`restore_mixer_snapshot` signature — (snapshot,
            tuning) → awaitable None.
        tuning: Voice tuning config — passed through to
            ``restore_fn`` (it needs ``linux_mixer_subprocess_timeout_s``).
    """
    wal = load_wal(path)
    if wal is None:
        return False
    logger.warning(
        "mixer_half_heal_recovery_triggered",
        path=str(path),
        card_index=wal.card_index,
        controls_to_restore=len(wal.reverted_controls),
        note=(
            "previous L2.5 invocation died mid-apply — replaying "
            "write-ahead log to restore pre-apply mixer state"
        ),
    )
    snapshot = wal.to_apply_snapshot()
    try:
        await restore_fn(snapshot, tuning=tuning)
    except Exception as exc:  # noqa: BLE001 — recovery is best-effort
        logger.warning(
            "mixer_half_heal_recovery_restore_failed",
            card_index=wal.card_index,
            detail=str(exc)[:200],
            note=(
                "restore_mixer_snapshot raised during recovery — "
                "mixer may still be in a partial state; cascade "
                "will probe and attempt normal reconciliation"
            ),
        )
    # Always clear the WAL after a replay attempt, even on
    # restore failure. If we kept it, every future cascade would
    # re-attempt the same (already-failing) replay, drowning the
    # logs and masking the fact that the mixer is stuck.
    clear_wal(path)
    return True


__all__ = [
    "HalfHealWal",
    "clear_wal",
    "default_wal_path",
    "load_wal",
    "recover_if_present",
    "write_wal",
]
