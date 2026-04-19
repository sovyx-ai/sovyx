"""Explicit per-version schema migrations for ``capture_combos.json``.

Per ADR-combo-store-schema §6: every version transition is an explicit
handler. No implicit field upserts, no "loose JSON survives a round
trip" guarantees. A migration that fails archives the source file and
returns an empty store (R4 path in :mod:`combo_store`).

Currently only v1 → v2 is implemented. Future versions add a new
:func:`_migrate_v2_to_v3` handler and extend :data:`_MIGRATIONS`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from sovyx.observability.logging import get_logger

if TYPE_CHECKING:
    from collections.abc import Callable

logger = get_logger(__name__)


CURRENT_SCHEMA_VERSION = 2


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


_MIGRATIONS: dict[int, Callable[..., dict[str, Any]]] = {
    1: _migrate_v1_to_v2,
}


__all__ = [
    "CURRENT_SCHEMA_VERSION",
    "MigrationError",
    "migrate_to_current",
]
