"""Explicit per-version schema migrations for ``capture_combos.json``.

Per ADR-combo-store-schema §6: every version transition is an explicit
handler. No implicit field upserts, no "loose JSON survives a round
trip" guarantees. A migration that fails archives the source file and
returns an empty store (R4 path in :mod:`combo_store`).

Schema history:

* ``v1`` — original (pre-v0.20) layout. No ``device_class``, no
  ``endpoint_fxproperties_sha``, ``validation_mode`` implicit.
* ``v2`` — v0.20 / ADR §3 layout. Adds ``device_class``,
  ``validation_mode``, ``endpoint_fxproperties_sha``, plus the
  outer ``audio_subsystem_fingerprint`` block.
* ``v3`` — ``voice-linux-cascade-root-fix`` T11. Adds
  ``candidate_kind`` per entry so the cascade-candidate-set
  fast-path can distinguish ``hardware`` / ``session_manager_virtual``
  / ``os_default`` stored winners — useful for telemetry and for
  surfacing "running on pipewire (fallback)" vs. "running on
  hw:1,0" in the dashboard without a secondary lookup.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from sovyx.observability.logging import get_logger

if TYPE_CHECKING:
    from collections.abc import Callable

logger = get_logger(__name__)


CURRENT_SCHEMA_VERSION = 4


class MigrationError(RuntimeError):
    """Raised when a migration handler cannot finish.

    Caught by :class:`~sovyx.voice.health.combo_store.ComboStore.load`
    which then archives the source file and starts an empty store.
    """


def migrate_to_current(
    raw: dict[str, Any],
    *,
    audio_subsystem_fingerprint_factory: Callable[[], dict[str, Any]],
    endpoint_fxproperties_sha_for: Callable[[str], str],
) -> dict[str, Any]:
    """Migrate ``raw`` to :data:`CURRENT_SCHEMA_VERSION`.

    Args:
        raw: Parsed JSON dict, as read from disk. May be any version
            ``≤ CURRENT_SCHEMA_VERSION``. ``schema_version`` is read
            from the dict; missing field defaults to ``1``.
        audio_subsystem_fingerprint_factory: Function that returns the
            current OS-level audio fingerprint dict. Called once if
            v1 → v2 needs to backfill.
        endpoint_fxproperties_sha_for: Per-endpoint SHA factory. Called
            once per endpoint that lacks the field after migration.

    Returns:
        A dict with ``schema_version == CURRENT_SCHEMA_VERSION``.

    Raises:
        MigrationError: When the source data is malformed beyond
            recovery (e.g. ``entries`` is not a dict).
    """
    version = int(raw.get("schema_version", 1))
    if version > CURRENT_SCHEMA_VERSION:
        msg = f"schema_version={version} is newer than runtime ({CURRENT_SCHEMA_VERSION})"
        raise MigrationError(msg)

    while version < CURRENT_SCHEMA_VERSION:
        handler = _MIGRATIONS.get(version)
        if handler is None:
            msg = f"no migration handler for schema_version={version}"
            raise MigrationError(msg)
        raw = handler(
            raw,
            audio_subsystem_fingerprint_factory=audio_subsystem_fingerprint_factory,
            endpoint_fxproperties_sha_for=endpoint_fxproperties_sha_for,
        )
        version = int(raw.get("schema_version", version + 1))
        logger.info("combo_store_migrated", to_version=version)

    return raw


def _migrate_v1_to_v2(
    raw: dict[str, Any],
    *,
    audio_subsystem_fingerprint_factory: Callable[[], dict[str, Any]],
    endpoint_fxproperties_sha_for: Callable[[str], str],
) -> dict[str, Any]:
    """Backfill v2 fields with conservative defaults.

    Defaults match ADR-combo-store-schema §6:

    * ``device_class`` → ``"other"`` (will be reclassified on next probe)
    * ``validation_mode`` → ``"warm"`` (legacy v1 was always warm)
    * ``sample_format`` → ``"int16"``
    * ``frames_per_buffer`` → ``480``
    * ``audio_subsystem_fingerprint`` → recomputed (does not invalidate)
    * ``endpoint_fxproperties_sha`` → recomputed per-endpoint
    * ``probe_history`` → ``[]``
    * ``pinned`` → ``False``
    * ``last_boot_diagnosis`` → ``"healthy"`` (legacy entries presumed healthy)
    """
    entries = raw.get("entries")
    if not isinstance(entries, dict):
        msg = f"v1 entries field is not a dict (got {type(entries).__name__})"
        raise MigrationError(msg)

    new_entries: dict[str, Any] = {}
    for guid, entry in entries.items():
        if not isinstance(entry, dict):
            logger.warning("combo_store_migration_dropped_non_dict_entry", endpoint=guid)
            continue

        winning = entry.get("winning_combo")
        if not isinstance(winning, dict):
            logger.warning("combo_store_migration_dropped_no_combo", endpoint=guid)
            continue
        winning.setdefault("sample_format", "int16")
        winning.setdefault("frames_per_buffer", 480)
        winning.setdefault("auto_convert", False)

        new_entry: dict[str, Any] = dict(entry)
        new_entry["winning_combo"] = winning
        new_entry.setdefault("device_class", "other")
        new_entry.setdefault("validation_mode", "warm")
        new_entry.setdefault("vad_mean_prob_at_validation", None)
        new_entry.setdefault("endpoint_fxproperties_sha", endpoint_fxproperties_sha_for(guid))
        new_entry.setdefault("probe_history", [])
        new_entry.setdefault("pinned", False)
        new_entry.setdefault("last_boot_diagnosis", "healthy")
        new_entry.setdefault("last_boot_validated", entry.get("validated_at", ""))
        new_entries[guid] = new_entry

    out: dict[str, Any] = dict(raw)
    out["schema_version"] = 2
    out["entries"] = new_entries
    out.setdefault("wake_word_model_version", "")
    out.setdefault("stt_model_version", "")
    out.setdefault("audio_subsystem_fingerprint", audio_subsystem_fingerprint_factory())
    return out


def _migrate_v2_to_v3(
    raw: dict[str, Any],
    *,
    audio_subsystem_fingerprint_factory: Callable[[], dict[str, Any]],  # noqa: ARG001
    endpoint_fxproperties_sha_for: Callable[[str], str],  # noqa: ARG001
) -> dict[str, Any]:
    """Backfill ``candidate_kind`` for voice-linux-cascade-root-fix T11.

    The new field describes the winning candidate's
    :class:`~sovyx.voice.device_enum.DeviceKind` — populated verbatim
    by future writers, backfilled here as ``"unknown"`` for v2 entries
    so the reader always sees a string.

    Unrelated fields are preserved verbatim — this is a pure additive
    migration, no reshaping. A malformed ``entries`` block raises
    :class:`MigrationError` (handled by the caller's archive path).
    """
    entries = raw.get("entries")
    if not isinstance(entries, dict):
        msg = f"v2 entries field is not a dict (got {type(entries).__name__})"
        raise MigrationError(msg)

    new_entries: dict[str, Any] = {}
    for guid, entry in entries.items():
        if not isinstance(entry, dict):
            logger.warning("combo_store_migration_dropped_non_dict_entry", endpoint=guid)
            continue
        new_entry: dict[str, Any] = dict(entry)
        new_entry.setdefault("candidate_kind", "unknown")
        new_entries[guid] = new_entry

    out: dict[str, Any] = dict(raw)
    out["schema_version"] = 3
    out["entries"] = new_entries
    return out


def _migrate_v3_to_v4(
    raw: dict[str, Any],
    *,
    audio_subsystem_fingerprint_factory: Callable[[], dict[str, Any]],  # noqa: ARG001
    endpoint_fxproperties_sha_for: Callable[[str], str],  # noqa: ARG001
) -> dict[str, Any]:
    """Phase 3 / T3.10 — schema bump to v4.

    No field reshape. The version bump exists to mark the schema
    boundary at which Sovyx began applying R14 (silent_combo_evict)
    on every load. Pre-v4 stores may carry legacy silent winners
    (``rms_db_at_validation < -70 dBFS``) persisted before the
    Furo W-1 fix landed in T11 (commit ``c888c2b``); the v4 boot
    sees R14 evict them, and post-v4 the cold-probe strict path
    forbids fresh silent writes — so R14 self-extinguishes.

    The migration is a pure version-bump pass-through. Unrelated
    fields are preserved verbatim. A malformed ``entries`` block
    raises :class:`MigrationError` (handled by the caller's
    archive path).
    """
    entries = raw.get("entries")
    if not isinstance(entries, dict):
        msg = f"v3 entries field is not a dict (got {type(entries).__name__})"
        raise MigrationError(msg)

    out: dict[str, Any] = dict(raw)
    out["schema_version"] = 4
    return out


_MIGRATIONS: dict[int, Callable[..., dict[str, Any]]] = {
    1: _migrate_v1_to_v2,
    2: _migrate_v2_to_v3,
    3: _migrate_v3_to_v4,
}


__all__ = [
    "CURRENT_SCHEMA_VERSION",
    "MigrationError",
    "migrate_to_current",
]
