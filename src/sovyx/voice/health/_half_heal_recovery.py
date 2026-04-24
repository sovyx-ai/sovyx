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

import asyncio
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


_WAL_MAX_BYTES = 64 * 1024
"""Hard size cap (bytes) on a WAL file before we even open it.

Paranoid-QA R3 HIGH #2: without this, an attacker with write
access to ``data_dir/voice_health/`` could stage a multi-gigabyte
WAL; the daemon's boot recovery would OOM-crash on
``path.read_text(...)`` before ``load_wal``'s schema checks ran.
64 KiB is two orders of magnitude over any realistic WAL (pilot
profile ~2 entries × ~30 bytes each + overhead ≈ 100 bytes).
"""


_WAL_MAX_ENTRIES = 64
"""Max ``reverted_controls`` entries accepted by ``load_wal``.

Paranoid-QA R3 HIGH #1/#3: even a well-formed but bloated WAL can
DoS the recovery path — ``recover_if_present`` calls
``restore_fn`` once per entry, each issuing an amixer subprocess
with its own ``linux_mixer_subprocess_timeout_s`` ceiling.
Bounding here forces the attacker's WAL to respect the realistic
shape (pilot preset = 2 controls; every shipped preset is ≤ 10).
"""


_WAL_MAX_CONTROL_NAME_LEN = 128
"""Max length of a single ``reverted_controls`` name string.

Realistic amixer simple-control names are 5–30 chars
(``"Capture"``, ``"Internal Mic Boost"``). 128 is a generous
ceiling that blocks ARG_MAX-style amplification attacks (a 1 MiB
name passed to ``execve`` would raise E2BIG but also consume the
attacker-chosen number of seconds). 128 bytes × 64 entries =
8 KiB total — fits comfortably inside ``_WAL_MAX_BYTES``.
"""


_WAL_CONTROL_NAME_FORBIDDEN_CHARS = frozenset(",='\"\x00\r\n\t")
"""Characters a legitimate amixer simple-control name never
contains — but which would, if passed in as argv, confuse amixer's
simple-control selector parser (``name='X',index=N,iface=CARD``
syntax). The NUL / CR / LF / TAB entries block embedded newlines
from smuggling through log scrapers.

Paranoid-QA R3 HIGH #3: rejects any WAL entry whose control name
contains any of these. Kernel-sourced control names (the normal
apply path) never include them; attacker-controlled WAL content
does.
"""


class RestoreSnapshotFn(Protocol):
    """Callable shape for the injected rollback function.

    Expressed as a ``Protocol`` (not a ``Callable[...]`` alias)
    because :func:`sovyx.voice.health._linux_mixer_apply.restore_mixer_snapshot`
    takes ``tuning`` as a KEYWORD-only argument. ``Callable[[X, Y],
    ...]`` types are always positional; mypy-on-Linux correctly
    rejected that form when a keyword-only callable was passed in.
    A Protocol lets us describe the exact keyword-only signature.

    Structurally identical to :class:`sovyx.voice.health._mixer_sanity.MixerRestoreFn`
    — defined here as well so this module stays importable without
    pulling in ``_mixer_sanity`` (which imports this module).
    Circular-import-proof by construction: two Protocol classes with
    the same ``__call__`` signature are structurally
    interchangeable for mypy.
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

    Mirrors :class:`MixerApplySnapshot` — same intent, write-ahead
    form (carries pre-apply state only; post-apply state isn't
    known at WAL-write time).

    Paranoid-QA R3 CRIT-1: also carries ``reverted_enum_controls``.
    Before this, ``_build_half_heal_wal_plan`` only captured numeric
    pre-apply state, and a mid-apply crash between the numeric
    loop and ``_apply_auto_mute`` produced a frankenstate mixer on
    next-boot recovery (numerics restored, Auto-Mute stuck in
    ``Disabled``/``Enabled``). The new field default is ``()`` so
    serialised WALs without the key (schema-v1 upgrades) still
    deserialise cleanly — no WAL-schema bump required.
    """

    card_index: int
    reverted_controls: tuple[tuple[str, int], ...]
    reverted_enum_controls: tuple[tuple[str, str], ...] = ()

    def to_apply_snapshot(self) -> MixerApplySnapshot:
        """Promote to the shape :func:`restore_mixer_snapshot` consumes."""
        return MixerApplySnapshot(
            card_index=self.card_index,
            reverted_controls=self.reverted_controls,
            applied_controls=(),
            reverted_enum_controls=self.reverted_enum_controls,
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
    reverted_enum_controls: tuple[tuple[str, str], ...] = (),
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

    Paranoid-QA R3 CRIT-1: ``reverted_enum_controls`` captures the
    pre-apply label of any enum-typed control that the preset is
    about to mutate (chiefly HDA ``Auto-Mute Mode``). Default
    ``()`` makes existing callers work unchanged; orchestrator
    populates it when ``preset.auto_mute_mode != "leave"`` so a
    mid-apply crash can be fully recovered.
    """
    payload: dict[str, object] = {
        "schema_version": _WAL_SCHEMA_VERSION,
        "card_index": card_index,
        "reverted_controls": [[name, raw] for name, raw in reverted_controls],
    }
    if reverted_enum_controls:
        payload["reverted_enum_controls"] = [
            [name, label] for name, label in reverted_enum_controls
        ]
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
    I/O error, size-cap exceeded, forbidden characters in control
    names) returns ``None`` and logs at WARN. The file is NOT
    deleted by this function — the caller (recovery driver) is
    responsible for cleanup so the deletion happens atomically
    with the replay.

    Paranoid-QA R3 HIGH #2/#3 hardening:

    * ``stat().st_size`` is checked BEFORE ``read_text`` so a
      crafted multi-gigabyte WAL cannot OOM the daemon on boot.
    * Per-entry control-name length is capped at
      ``_WAL_MAX_CONTROL_NAME_LEN`` and screened against
      ``_WAL_CONTROL_NAME_FORBIDDEN_CHARS`` so attacker content
      cannot confuse amixer's simple-control selector (`name='X',
      index=N,iface=CARD`-style smuggling) nor smuggle NUL / CRLF
      into operator logs.
    * The whole ``reverted_controls`` list is capped at
      ``_WAL_MAX_ENTRIES`` so a WAL under the byte-cap cannot
      still starve the boot path with hundreds of amixer
      subprocesses.
    """
    # Paranoid-QA R3 HIGH #2: size cap enforced via ``stat`` so we
    # never slurp a gigabyte WAL into memory. ``FileNotFoundError``
    # falls through to the ``read_text`` branch below (same handling).
    try:
        size = path.stat().st_size
    except FileNotFoundError:
        return None
    except OSError as exc:
        logger.warning(
            "mixer_half_heal_wal_stat_failed",
            path=str(path),
            detail=str(exc),
        )
        return None
    if size > _WAL_MAX_BYTES:
        logger.warning(
            "mixer_half_heal_wal_oversized",
            path=str(path),
            size_bytes=size,
            limit_bytes=_WAL_MAX_BYTES,
            note="refusing to load — WAL content is attacker-controlled",
        )
        return None
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
    # Paranoid-QA R3 HIGH #1/#3: cap entry count + per-entry
    # validation.
    if len(reverted) > _WAL_MAX_ENTRIES:
        logger.warning(
            "mixer_half_heal_wal_too_many_entries",
            path=str(path),
            count=len(reverted),
            limit=_WAL_MAX_ENTRIES,
        )
        return None
    try:
        controls_builder: list[tuple[str, int]] = []
        for entry in reverted:
            if not isinstance(entry, (list, tuple)) or len(entry) != 2:
                continue
            name = str(entry[0])
            raw_value = int(entry[1])
            if len(name) == 0 or len(name) > _WAL_MAX_CONTROL_NAME_LEN:
                logger.warning(
                    "mixer_half_heal_wal_control_name_length_invalid",
                    path=str(path),
                    name_length=len(name),
                    limit=_WAL_MAX_CONTROL_NAME_LEN,
                )
                return None
            if any(c in _WAL_CONTROL_NAME_FORBIDDEN_CHARS for c in name):
                logger.warning(
                    "mixer_half_heal_wal_control_name_forbidden_chars",
                    path=str(path),
                    note=(
                        "name contains NUL / CRLF / amixer selector "
                        "metacharacters — refusing to replay"
                    ),
                )
                return None
            controls_builder.append((name, raw_value))
        controls: tuple[tuple[str, int], ...] = tuple(controls_builder)
    except (TypeError, ValueError, IndexError) as exc:
        logger.warning(
            "mixer_half_heal_wal_controls_invalid",
            path=str(path),
            detail=str(exc),
        )
        return None

    # Paranoid-QA R3 CRIT-1: parse the optional enum-controls list.
    # Absent key → empty tuple (backwards-compatible with v1 WALs
    # written before this field existed). Same validation rules as
    # numeric entries: length cap, forbidden chars. Enum label is
    # also subject to the name-content rules because it's passed
    # as an argv element to ``amixer`` — an attacker-controlled
    # label with `,=` could confuse the selector just as much.
    enum_raw = data.get("reverted_enum_controls")
    enum_controls: tuple[tuple[str, str], ...] = ()
    if enum_raw is not None:
        if not isinstance(enum_raw, list):
            logger.warning(
                "mixer_half_heal_wal_enum_controls_not_list",
                path=str(path),
            )
            return None
        if len(enum_raw) > _WAL_MAX_ENTRIES:
            logger.warning(
                "mixer_half_heal_wal_too_many_enum_entries",
                path=str(path),
                count=len(enum_raw),
                limit=_WAL_MAX_ENTRIES,
            )
            return None
        try:
            enum_builder: list[tuple[str, str]] = []
            for entry in enum_raw:
                if not isinstance(entry, (list, tuple)) or len(entry) != 2:
                    continue
                name = str(entry[0])
                label = str(entry[1])
                for field_name, field_value in (("name", name), ("label", label)):
                    if (
                        len(field_value) == 0
                        or len(field_value) > _WAL_MAX_CONTROL_NAME_LEN
                    ):
                        logger.warning(
                            "mixer_half_heal_wal_enum_field_length_invalid",
                            path=str(path),
                            field=field_name,
                            length=len(field_value),
                            limit=_WAL_MAX_CONTROL_NAME_LEN,
                        )
                        return None
                    if any(
                        c in _WAL_CONTROL_NAME_FORBIDDEN_CHARS for c in field_value
                    ):
                        logger.warning(
                            "mixer_half_heal_wal_enum_field_forbidden_chars",
                            path=str(path),
                            field=field_name,
                        )
                        return None
                enum_builder.append((name, label))
            enum_controls = tuple(enum_builder)
        except (TypeError, ValueError, IndexError) as exc:
            logger.warning(
                "mixer_half_heal_wal_enum_controls_invalid",
                path=str(path),
                detail=str(exc),
            )
            return None

    return HalfHealWal(
        card_index=card_index,
        reverted_controls=controls,
        reverted_enum_controls=enum_controls,
    )


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
    timeout_s: float | None = None,
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
        timeout_s: Wall-clock ceiling for the replay (Paranoid-QA
            R3 HIGH #1). ``None`` disables the cap (test-only). In
            production the orchestrator passes
            ``tuning.linux_mixer_sanity_budget_s`` so a crafted
            WAL can't stall boot cascade indefinitely. When the
            replay exceeds the cap, it's cancelled, the WAL is
            cleared, and a WARN log surfaces the event.
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
        if timeout_s is None:
            await restore_fn(snapshot, tuning=tuning)
        else:
            await asyncio.wait_for(
                restore_fn(snapshot, tuning=tuning),
                timeout=timeout_s,
            )
    except TimeoutError:
        logger.warning(
            "mixer_half_heal_recovery_timeout",
            path=str(path),
            card_index=wal.card_index,
            timeout_s=timeout_s,
            note=(
                "WAL replay exceeded the wall-clock budget — "
                "cancelling to unblock the cascade; mixer may be "
                "in a partial state"
            ),
        )
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
