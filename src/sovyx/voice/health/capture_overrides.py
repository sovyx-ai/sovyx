"""L1 — user-pinned combo overrides, sibling to the ComboStore.

A pinned override is the user saying "for *this* endpoint, always try *this*
combo first, no matter what the cascade would pick". It is tried before the
ComboStore fast-path and before the platform cascade; failure in-session
surfaces a warning but never auto-unpins (see ADR §4 + §4.2).

Lives in its own JSON file so ``sovyx doctor voice --reset`` can wipe the
ComboStore without touching user intent. ``--reset --pinned`` archives this
file too. See ADR-combo-store-schema.md §4 for the on-disk shape.

File-system discipline mirrors :mod:`sovyx.voice.health.combo_store`: atomic
write (tmp → fsync → rename), single-generation ``.bak`` backup, cross-process
advisory lock via :func:`~sovyx.voice.health._file_lock.acquire_file_lock`,
and POSIX ``chmod 600`` after every write.
"""

from __future__ import annotations

import contextlib
import json
import os
import shutil
import sys
import tempfile
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from sovyx.observability.logging import get_logger
from sovyx.voice.health._file_lock import acquire_file_lock
from sovyx.voice.health.combo_store import _combo_to_dict
from sovyx.voice.health.contract import (
    ALLOWED_FORMATS,
    ALLOWED_HOST_APIS_BY_PLATFORM,
    ALLOWED_SAMPLE_RATES,
    Combo,
    OverrideEntry,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator
    from pathlib import Path

logger = get_logger(__name__)


CURRENT_OVERRIDES_SCHEMA_VERSION = 1

_ALLOWED_SOURCES: frozenset[str] = frozenset({"user", "wizard", "cli"})
_CHANNELS_MIN = 1
_CHANNELS_MAX = 8
_FRAMES_PER_BUFFER_MIN = 64
_FRAMES_PER_BUFFER_MAX = 8_192


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _platform_key() -> str:
    if sys.platform.startswith("win"):
        return "win32"
    if sys.platform == "darwin":
        return "darwin"
    return "linux"


def _allowed_host_apis() -> frozenset[str]:
    return ALLOWED_HOST_APIS_BY_PLATFORM.get(_platform_key(), frozenset())


class CaptureOverrides:
    """User-pinned combos, persisted alongside the ComboStore.

    All public methods are synchronous; per CLAUDE.md anti-pattern #14,
    callers in async code MUST wrap in ``asyncio.to_thread``. The file is
    tiny (one row per pinned endpoint, < 1 KB typical) so I/O is cheap.

    Args:
        path: Path to ``capture_overrides.json``. Parent directory is
            auto-created on first write.
        clock: Override for ``datetime.now(UTC)``. Tests pass a frozen
            clock for deterministic ``pinned_at`` timestamps.
    """

    def __init__(
        self,
        path: Path,
        *,
        clock: Callable[[], datetime] = _utc_now,
    ) -> None:
        self._path = path
        self._lock_path = path.with_suffix(path.suffix + ".lock")
        self._bak_path = path.with_suffix(path.suffix + ".bak")
        self._clock = clock
        self._entries: dict[str, OverrideEntry] = {}
        self._loaded = False

    # ── load ─────────────────────────────────────────────────────────

    def load(self) -> None:
        """Open the file, validate, populate in-memory entries. Idempotent.

        A corrupt file is archived to ``*.corrupt-<reason>-<ts>.json`` and
        the store starts empty. A file whose ``schema_version`` is newer
        than the runtime is archived the same way (downgrade protection).
        Per-entry sanity failures drop only the offending entry with a
        WARNING log.
        """
        if not self._path.exists():
            self._entries = {}
            self._loaded = True
            return

        raw = self._read_with_backup_recovery()
        if raw is None:
            self._archive(reason="parse-error")
            self._entries = {}
            self._loaded = True
            return

        version = raw.get("schema_version", 1)
        if not isinstance(version, int) or version > CURRENT_OVERRIDES_SCHEMA_VERSION:
            logger.warning(
                "capture_overrides_future_version",
                version=version,
                runtime=CURRENT_OVERRIDES_SCHEMA_VERSION,
            )
            self._archive(reason="version-newer")
            self._entries = {}
            self._loaded = True
            return

        overrides = raw.get("overrides")
        if not isinstance(overrides, dict):
            logger.warning("capture_overrides_overrides_not_dict")
            self._archive(reason="overrides-not-dict")
            self._entries = {}
            self._loaded = True
            return

        parsed: dict[str, OverrideEntry] = {}
        for guid, raw_entry in overrides.items():
            entry = self._build_entry(guid, raw_entry)
            if entry is not None:
                parsed[guid] = entry
        self._entries = parsed
        self._loaded = True

    # ── public read API ──────────────────────────────────────────────

    def get(self, endpoint_guid: str) -> Combo | None:
        """Return the pinned combo for ``endpoint_guid`` or ``None``."""
        self._ensure_loaded()
        entry = self._entries.get(endpoint_guid)
        return entry.pinned_combo if entry is not None else None

    def get_entry(self, endpoint_guid: str) -> OverrideEntry | None:
        """Return the full override entry (includes metadata) or ``None``."""
        self._ensure_loaded()
        return self._entries.get(endpoint_guid)

    def entries(self) -> Iterator[OverrideEntry]:
        """Yield every pinned entry (order stable: sorted by GUID)."""
        self._ensure_loaded()
        for guid in sorted(self._entries):
            yield self._entries[guid]

    def is_pinned(self, endpoint_guid: str) -> bool:
        self._ensure_loaded()
        return endpoint_guid in self._entries

    # ── public write API ─────────────────────────────────────────────

    def pin(
        self,
        endpoint_guid: str,
        *,
        device_friendly_name: str,
        combo: Combo,
        source: str,
        reason: str = "",
    ) -> None:
        """Pin ``combo`` to ``endpoint_guid``. Overwrites any prior pin.

        Args:
            endpoint_guid: Target endpoint GUID. Must be non-empty.
            device_friendly_name: Display name for diagnostics (GUID is
                the matching key; name is informational only).
            combo: The combo to pin. Validated by :class:`Combo` at
                construction — invalid combos never reach the file.
            source: One of ``"user"``, ``"wizard"``, ``"cli"``. Drives
                the provenance field for the dashboard.
            reason: Free-form user note (optional).

        Raises:
            ValueError: If ``endpoint_guid`` is empty or ``source`` is
                not one of the allowed values.
        """
        if not endpoint_guid:
            msg = "endpoint_guid must be non-empty"
            raise ValueError(msg)
        if source not in _ALLOWED_SOURCES:
            msg = f"source={source!r} not in {sorted(_ALLOWED_SOURCES)}"
            raise ValueError(msg)

        self._ensure_loaded()
        with acquire_file_lock(self._lock_path):
            self._entries[endpoint_guid] = OverrideEntry(
                endpoint_guid=endpoint_guid,
                device_friendly_name=device_friendly_name,
                pinned_combo=combo,
                pinned_at=self._clock().isoformat(timespec="seconds"),
                pinned_by=source,
                reason=reason,
            )
            self._write_atomic()

    def unpin(self, endpoint_guid: str) -> bool:
        """Remove pin for ``endpoint_guid``. Returns ``True`` if something was removed."""
        self._ensure_loaded()
        if endpoint_guid not in self._entries:
            return False
        with acquire_file_lock(self._lock_path):
            self._entries.pop(endpoint_guid, None)
            self._write_atomic()
        return True

    def invalidate_all(self) -> None:
        """Archive current file + wipe in-memory state. Used by ``--reset --pinned``."""
        self._ensure_loaded()
        with acquire_file_lock(self._lock_path):
            if self._path.exists():
                self._archive(reason="invalidate-all")
            self._entries = {}
            # Write an empty file so subsequent loads skip archive/restore paths.
            self._write_atomic()

    # ── helpers ──────────────────────────────────────────────────────

    def _ensure_loaded(self) -> None:
        if not self._loaded:
            self.load()

    def _read_with_backup_recovery(self) -> dict[str, Any] | None:
        """Read main file; on failure try ``.bak``. Returns ``None`` on total failure."""
        try:
            text = self._path.read_text(encoding="utf-8")
            data = json.loads(text)
            if not isinstance(data, dict):
                raise ValueError("root is not a JSON object")  # noqa: TRY004, TRY301
        except (OSError, ValueError) as exc:
            logger.warning(
                "capture_overrides_main_read_failed",
                detail=str(exc),
                path=str(self._path),
            )
        else:
            return data

        if self._bak_path.exists():
            try:
                text = self._bak_path.read_text(encoding="utf-8")
                data = json.loads(text)
                if not isinstance(data, dict):
                    raise ValueError("backup root is not a JSON object")  # noqa: TRY004, TRY301
            except (OSError, ValueError) as exc:
                logger.warning(
                    "capture_overrides_backup_read_failed",
                    detail=str(exc),
                    path=str(self._bak_path),
                )
            else:
                logger.info(
                    "capture_overrides_recovered_from_backup",
                    path=str(self._bak_path),
                )
                return data

        return None

    def _build_entry(self, guid: str, raw_entry: object) -> OverrideEntry | None:
        """Validate a single raw dict; return ``None`` (+ log) on failure."""
        if not guid:
            logger.warning("capture_overrides_drop_empty_guid")
            return None
        if not isinstance(raw_entry, dict):
            logger.warning("capture_overrides_drop_non_dict", endpoint=guid)
            return None

        pinned_raw = raw_entry.get("pinned_combo")
        if not isinstance(pinned_raw, dict):
            logger.warning("capture_overrides_drop_missing_combo", endpoint=guid)
            return None

        # Structural sanity before handing to Combo (which will raise on
        # unknown host-api / rate / format). Covers the §3.2 fields that
        # Combo.__post_init__ already re-checks; staying explicit here keeps
        # the WARNING telemetry pointing at the right field name.
        sample_rate = pinned_raw.get("sample_rate")
        if sample_rate not in ALLOWED_SAMPLE_RATES:
            logger.warning(
                "capture_overrides_drop_bad_sample_rate",
                endpoint=guid,
                value=sample_rate,
            )
            return None
        channels = pinned_raw.get("channels")
        if not (isinstance(channels, int) and _CHANNELS_MIN <= channels <= _CHANNELS_MAX):
            logger.warning(
                "capture_overrides_drop_bad_channels",
                endpoint=guid,
                value=channels,
            )
            return None
        sample_format = pinned_raw.get("sample_format", "int16")
        if sample_format not in ALLOWED_FORMATS:
            logger.warning(
                "capture_overrides_drop_bad_format",
                endpoint=guid,
                value=sample_format,
            )
            return None
        host_api = pinned_raw.get("host_api")
        allowed = _allowed_host_apis()
        if not isinstance(host_api, str) or (allowed and host_api not in allowed):
            logger.warning(
                "capture_overrides_drop_bad_host_api",
                endpoint=guid,
                value=host_api,
            )
            return None
        frames_per_buffer = pinned_raw.get("frames_per_buffer", 480)
        if not (
            isinstance(frames_per_buffer, int)
            and _FRAMES_PER_BUFFER_MIN <= frames_per_buffer <= _FRAMES_PER_BUFFER_MAX
        ):
            logger.warning(
                "capture_overrides_drop_bad_frames_per_buffer",
                endpoint=guid,
                value=frames_per_buffer,
            )
            return None

        try:
            combo = Combo(
                host_api=host_api,
                sample_rate=int(sample_rate),
                channels=channels,
                sample_format=sample_format,
                exclusive=bool(pinned_raw.get("exclusive", False)),
                auto_convert=bool(pinned_raw.get("auto_convert", False)),
                frames_per_buffer=frames_per_buffer,
            )
        except ValueError as exc:
            logger.warning(
                "capture_overrides_drop_combo_validation",
                endpoint=guid,
                detail=str(exc),
            )
            return None

        pinned_by = raw_entry.get("pinned_by", "user")
        if pinned_by not in _ALLOWED_SOURCES:
            logger.warning(
                "capture_overrides_drop_bad_source",
                endpoint=guid,
                value=pinned_by,
            )
            return None

        return OverrideEntry(
            endpoint_guid=guid,
            device_friendly_name=str(raw_entry.get("device_friendly_name", "")),
            pinned_combo=combo,
            pinned_at=str(raw_entry.get("pinned_at", "")),
            pinned_by=pinned_by,
            reason=str(raw_entry.get("reason", "")),
        )

    def _archive(self, *, reason: str) -> Path | None:
        """Move the current file out of the way with a timestamped suffix."""
        if not self._path.exists():
            return None
        ts = self._clock().strftime("%Y%m%dT%H%M%SZ")
        dest = self._path.with_suffix(f".corrupt-{reason}-{ts}.json")
        try:
            shutil.move(str(self._path), str(dest))
        except OSError as exc:
            logger.warning("capture_overrides_archive_failed", detail=str(exc))
            return None
        return dest

    def _write_atomic(self) -> None:
        """Atomic write: tmp → fsync → rename. Backs up the prior file first."""
        self._path.parent.mkdir(parents=True, exist_ok=True)

        if self._path.exists():
            try:
                shutil.copy2(str(self._path), str(self._bak_path))
            except OSError as exc:
                logger.warning("capture_overrides_backup_copy_failed", detail=str(exc))

        payload = self._serialize()
        text = json.dumps(payload, indent=2, ensure_ascii=False, sort_keys=True)

        tmp_fd, tmp_name = tempfile.mkstemp(
            prefix=self._path.name + ".",
            suffix=".tmp",
            dir=str(self._path.parent),
        )
        try:
            with os.fdopen(tmp_fd, "w", encoding="utf-8") as fh:
                fh.write(text)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp_name, str(self._path))
        except OSError:
            with contextlib.suppress(OSError):  # pragma: no cover — best-effort
                os.unlink(tmp_name)
            raise
        if sys.platform != "win32":
            with contextlib.suppress(OSError):  # pragma: no cover — best-effort
                os.chmod(self._path, 0o600)

    def _serialize(self) -> dict[str, Any]:
        return {
            "schema_version": CURRENT_OVERRIDES_SCHEMA_VERSION,
            "last_updated": self._clock().isoformat(timespec="seconds"),
            "overrides": {
                guid: _override_to_dict(entry) for guid, entry in sorted(self._entries.items())
            },
        }


def _override_to_dict(entry: OverrideEntry) -> dict[str, Any]:
    return {
        "endpoint_guid": entry.endpoint_guid,
        "device_friendly_name": entry.device_friendly_name,
        "pinned_combo": _combo_to_dict(entry.pinned_combo),
        "pinned_at": entry.pinned_at,
        "pinned_by": entry.pinned_by,
        "reason": entry.reason,
    }


__all__ = [
    "CURRENT_OVERRIDES_SCHEMA_VERSION",
    "CaptureOverrides",
]
