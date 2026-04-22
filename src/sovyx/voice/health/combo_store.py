"""L1 — persistent memoization of (endpoint × winning_combo) tuples.

See :mod:`sovyx.voice.health` and ADR-combo-store-schema.md for the
full design. This module owns the on-disk shape, atomic writes,
cross-process file lock, and the 13 invalidation rules that make the
fast path safe against drift (driver updates, OS cumulative updates,
new APO chains, hardware changes).

The store is **advisory**. Every entry is re-validated on boot via at
least a cold probe; ``HEALTHY`` results clear the ``needs_revalidation``
flag, anything else falls through to the L2 cascade.
"""

from __future__ import annotations

import contextlib
import json
import os
import platform
import shutil
import sys
import tempfile
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from sovyx.observability.logging import get_logger
from sovyx.voice.health._combo_store_migrations import (
    CURRENT_SCHEMA_VERSION,
    MigrationError,
    migrate_to_current,
)
from sovyx.voice.health._file_lock import acquire_file_lock
from sovyx.voice.health._fingerprint import (
    compute_audio_subsystem_fingerprint,
    compute_endpoint_fxproperties_sha,
)
from sovyx.voice.health._metrics import record_combo_store_invalidation
from sovyx.voice.health.contract import (
    ALLOWED_FORMATS,
    ALLOWED_HOST_APIS_BY_PLATFORM,
    ALLOWED_SAMPLE_RATES,
    AudioSubsystemFingerprint,
    Combo,
    ComboEntry,
    ComboStoreStats,
    Diagnosis,
    LoadReport,
    ProbeHistoryEntry,
    ProbeMode,
    ProbeResult,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator, Sequence
    from pathlib import Path

logger = get_logger(__name__)


_PROBE_HISTORY_MAX = 10
_AGE_DEGRADED_DAYS = 30
_AGE_STALE_DAYS = 90
_RMS_DB_MIN = -90.0
_RMS_DB_MAX = 0.0
_VAD_MIN = 0.0
_VAD_MAX = 1.0
_BOOTS_VALIDATED_MIN = 0
_CHANNELS_MIN = 1
_CHANNELS_MAX = 8
_FRAMES_PER_BUFFER_MIN = 64
_FRAMES_PER_BUFFER_MAX = 8192


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _platform_label() -> str:
    if sys.platform.startswith("win"):
        return "windows"
    if sys.platform == "darwin":
        return "darwin"
    return "linux"


def _allowed_host_apis() -> frozenset[str]:
    label = _platform_label()
    key = "win32" if label == "windows" else label
    return ALLOWED_HOST_APIS_BY_PLATFORM.get(key, frozenset())


@dataclass(slots=True)
class _LiveEntry:
    """Mutable view of one entry; serialized back to disk on writes."""

    endpoint_guid: str
    device_friendly_name: str
    device_interface_name: str
    device_class: str
    endpoint_fxproperties_sha: str
    winning_combo: Combo
    validated_at: str
    validation_mode: ProbeMode
    vad_max_prob_at_validation: float | None
    vad_mean_prob_at_validation: float | None
    rms_db_at_validation: float
    probe_duration_ms: int
    detected_apos_at_validation: tuple[str, ...]
    cascade_attempts_before_success: int
    boots_validated: int
    last_boot_validated: str
    last_boot_diagnosis: Diagnosis
    probe_history: list[ProbeHistoryEntry] = field(default_factory=list)
    pinned: bool = False
    needs_revalidation: bool = False
    available: bool = True
    # voice-linux-cascade-root-fix T11 / schema v3. :class:`DeviceKind`
    # value as a string (``"hardware"`` / ``"session_manager_virtual"``
    # / ``"os_default"`` / ``"unknown"``). Back-compat default
    # ``"unknown"`` so existing writers that don't yet populate the
    # field continue to compile — legacy v2 entries migrate to this
    # value via :func:`_migrate_v2_to_v3`.
    candidate_kind: str = "unknown"

    def to_immutable(self) -> ComboEntry:
        return ComboEntry(
            endpoint_guid=self.endpoint_guid,
            device_friendly_name=self.device_friendly_name,
            device_interface_name=self.device_interface_name,
            device_class=self.device_class,
            endpoint_fxproperties_sha=self.endpoint_fxproperties_sha,
            winning_combo=self.winning_combo,
            validated_at=self.validated_at,
            validation_mode=self.validation_mode,
            vad_max_prob_at_validation=self.vad_max_prob_at_validation,
            vad_mean_prob_at_validation=self.vad_mean_prob_at_validation,
            rms_db_at_validation=self.rms_db_at_validation,
            probe_duration_ms=self.probe_duration_ms,
            detected_apos_at_validation=self.detected_apos_at_validation,
            cascade_attempts_before_success=self.cascade_attempts_before_success,
            boots_validated=self.boots_validated,
            last_boot_validated=self.last_boot_validated,
            last_boot_diagnosis=self.last_boot_diagnosis,
            probe_history=tuple(self.probe_history),
            pinned=self.pinned,
            needs_revalidation=self.needs_revalidation,
            candidate_kind=self.candidate_kind,
        )


class ComboStore:
    """Persistent memoization of validated capture combos.

    All public methods are synchronous; per CLAUDE.md anti-pattern #14,
    callers in async code MUST wrap in ``asyncio.to_thread``. The file
    is tiny (< 5 KB even with 20 endpoints) so I/O is fast.

    Args:
        path: Path to ``capture_combos.json``. Parent directory is
            auto-created on first write.
        clock: Override for ``datetime.now(UTC)``. Tests pass a frozen
            clock for deterministic ``validated_at`` timestamps.
        fingerprint_fn: Override for the OS-level audio fingerprint.
            Tests pass a stub that returns a constant for R8 isolation.
        endpoint_sha_fn: Override for per-endpoint fingerprint SHA. Tests
            pass a deterministic stub for R9 isolation.
        platform_label_fn: Override for ``"windows" | "linux" | "darwin"``.
            Tests pass a stub to validate R5 cross-platform mismatch.
        os_build_fn: Override for ``platform.version()``. Tests pass a
            stub for R6.
        wake_word_model_version: Stamped into the file on write; mismatch
            triggers R7.
        stt_model_version: Same as ``wake_word_model_version`` for R7.
        vad_model_version: Same as ``wake_word_model_version`` for R7.
        live_endpoint_guids: Optional callable returning the GUIDs
            currently enumerated by PortAudio / OS APIs. R13 marks
            entries whose GUID is absent from this set as
            ``available=False`` but keeps them on disk.
    """

    def __init__(
        self,
        path: Path,
        *,
        clock: Callable[[], datetime] = _utc_now,
        fingerprint_fn: Callable[[], AudioSubsystemFingerprint] = (
            compute_audio_subsystem_fingerprint
        ),
        endpoint_sha_fn: Callable[[str], str] = compute_endpoint_fxproperties_sha,
        platform_label_fn: Callable[[], str] = _platform_label,
        os_build_fn: Callable[[], str] = platform.version,
        wake_word_model_version: str = "",
        stt_model_version: str = "",
        vad_model_version: str = "",
        live_endpoint_guids: Callable[[], frozenset[str]] | None = None,
    ) -> None:
        self._path = path
        self._lock_path = path.with_suffix(path.suffix + ".lock")
        self._bak_path = path.with_suffix(path.suffix + ".bak")
        self._clock = clock
        self._fingerprint_fn = fingerprint_fn
        self._endpoint_sha_fn = endpoint_sha_fn
        self._platform_label_fn = platform_label_fn
        self._os_build_fn = os_build_fn
        self._wake_word_model_version = wake_word_model_version
        self._stt_model_version = stt_model_version
        self._vad_model_version = vad_model_version
        self._live_endpoint_guids = live_endpoint_guids
        self._entries: dict[str, _LiveEntry] = {}
        self._stats = ComboStoreStats()
        self._loaded = False

    # ── load + invalidation rules ────────────────────────────────────

    def load(self) -> LoadReport:
        """Open the file and apply invalidation rules R1-R13. Idempotent."""
        rules: list[tuple[str, str]] = []
        backup_used = False
        archived_to: Path | None = None

        if not self._path.exists():
            self._entries = {}
            self._loaded = True
            rules.append(("R1", "<global>"))
            return LoadReport(
                rules_applied=tuple(rules),
                entries_loaded=0,
                entries_dropped=0,
                backup_used=False,
                archived_to=None,
            )

        read_result = self._read_with_backup_recovery()
        if read_result is None:
            archived_to = self._archive_corrupt()
            self._entries = {}
            self._loaded = True
            rules.append(("R2", "<global>"))
            return LoadReport(
                rules_applied=tuple(rules),
                entries_loaded=0,
                entries_dropped=0,
                backup_used=True,
                archived_to=archived_to,
            )
        backup_used, raw = read_result

        version = int(raw.get("schema_version", 1))
        if version > CURRENT_SCHEMA_VERSION:
            rules.append(("R3", "<global>"))
            archived_to = self._archive(raw_reason="version-newer")
            self._entries = {}
            self._loaded = True
            return LoadReport(
                rules_applied=tuple(rules),
                entries_loaded=0,
                entries_dropped=0,
                backup_used=backup_used,
                archived_to=archived_to,
            )
        if version < CURRENT_SCHEMA_VERSION:
            rules.append(("R4", "<global>"))
            try:
                raw = migrate_to_current(
                    raw,
                    audio_subsystem_fingerprint_factory=lambda: _fingerprint_to_dict(
                        self._fingerprint_fn(),
                    ),
                    endpoint_fxproperties_sha_for=self._endpoint_sha_fn,
                )
            except MigrationError as exc:
                logger.warning("combo_store_migration_failed", detail=str(exc))
                archived_to = self._archive(raw_reason="migration-failed")
                self._entries = {}
                self._loaded = True
                return LoadReport(
                    rules_applied=tuple(rules),
                    entries_loaded=0,
                    entries_dropped=0,
                    backup_used=backup_used,
                    archived_to=archived_to,
                )

        if raw.get("platform") != self._platform_label_fn():
            rules.append(("R5", "<global>"))
            archived_to = self._archive(raw_reason="platform-mismatch")
            self._entries = {}
            self._loaded = True
            return LoadReport(
                rules_applied=tuple(rules),
                entries_loaded=0,
                entries_dropped=0,
                backup_used=backup_used,
                archived_to=archived_to,
            )

        global_revalidate = False
        if raw.get("os_build") != self._os_build_fn():
            rules.append(("R6", "<global>"))
            global_revalidate = True
        models = (
            ("wake_word_model_version", self._wake_word_model_version),
            ("stt_model_version", self._stt_model_version),
            ("vad_model_version", self._vad_model_version),
        )
        for field_name, expected in models:
            if expected and raw.get(field_name) != expected:
                rules.append(("R7", f"<global:{field_name}>"))
                global_revalidate = True

        recomputed_fp = self._fingerprint_fn()
        stored_fp_sha = (raw.get("audio_subsystem_fingerprint") or {}).get(
            "windows_fxproperties_global_sha",
        )
        if (
            recomputed_fp.windows_fxproperties_global_sha
            and stored_fp_sha
            and stored_fp_sha != recomputed_fp.windows_fxproperties_global_sha
        ):
            rules.append(("R8", "<global>"))
            global_revalidate = True

        live_guids = self._live_endpoint_guids() if self._live_endpoint_guids else None

        loaded = 0
        dropped = 0
        new_entries: dict[str, _LiveEntry] = {}
        for guid, raw_entry in (raw.get("entries") or {}).items():
            try:
                live = self._build_live_entry(guid, raw_entry, rules)
            except _SanityError as exc:
                rules.append(("R12", guid))
                logger.warning(
                    "combo_store_sanity_failed",
                    endpoint=guid,
                    field=exc.field,
                    value=str(exc.value),
                )
                dropped += 1
                continue

            if global_revalidate:
                live.needs_revalidation = True

            recomputed_endpoint_sha = self._endpoint_sha_fn(guid)
            if (
                recomputed_endpoint_sha
                and live.endpoint_fxproperties_sha
                and recomputed_endpoint_sha != live.endpoint_fxproperties_sha
            ):
                rules.append(("R9", guid))
                live.needs_revalidation = True

            now = self._clock()
            try:
                validated_dt = datetime.fromisoformat(live.validated_at)
            except ValueError:
                validated_dt = now
            age = now - validated_dt
            if (
                age > timedelta(days=_AGE_DEGRADED_DAYS)
                and live.last_boot_diagnosis is not Diagnosis.HEALTHY
            ):
                rules.append(("R10", guid))
                live.needs_revalidation = True
            if age > timedelta(days=_AGE_STALE_DAYS):
                rules.append(("R11", guid))
                live.needs_revalidation = True

            if live_guids is not None and guid not in live_guids:
                rules.append(("R13", guid))
                live.available = False

            new_entries[guid] = live
            loaded += 1

        self._entries = new_entries
        self._loaded = True
        for code, _scope in rules:
            self._stats.invalidations_by_reason[code] = (
                self._stats.invalidations_by_reason.get(code, 0) + 1
            )
        return LoadReport(
            rules_applied=tuple(rules),
            entries_loaded=loaded,
            entries_dropped=dropped,
            backup_used=backup_used,
            archived_to=archived_to,
        )

    # ── public read API ──────────────────────────────────────────────

    def get(self, endpoint_guid: str) -> ComboEntry | None:
        self._ensure_loaded()
        live = self._entries.get(endpoint_guid)
        if live is None or not live.available:
            self._stats.fast_path_misses += 1
            return None
        self._stats.fast_path_hits += 1
        return live.to_immutable()

    def needs_revalidation(self, endpoint_guid: str) -> bool:
        self._ensure_loaded()
        live = self._entries.get(endpoint_guid)
        return bool(live and live.needs_revalidation)

    def entries(self) -> Iterator[ComboEntry]:
        self._ensure_loaded()
        for live in self._entries.values():
            yield live.to_immutable()

    def stats(self) -> ComboStoreStats:
        return self._stats

    # ── public write API ─────────────────────────────────────────────

    def record_winning(
        self,
        endpoint_guid: str,
        *,
        device_friendly_name: str,
        device_interface_name: str,
        device_class: str,
        endpoint_fxproperties_sha: str,
        combo: Combo,
        probe: ProbeResult,
        detected_apos: Sequence[str],
        cascade_attempts_before_success: int,
    ) -> None:
        self._ensure_loaded()
        with acquire_file_lock(self._lock_path):
            now_iso = self._clock().isoformat(timespec="seconds")
            self._entries[endpoint_guid] = _LiveEntry(
                endpoint_guid=endpoint_guid,
                device_friendly_name=device_friendly_name,
                device_interface_name=device_interface_name,
                device_class=device_class,
                endpoint_fxproperties_sha=endpoint_fxproperties_sha,
                winning_combo=combo,
                validated_at=now_iso,
                validation_mode=probe.mode,
                vad_max_prob_at_validation=probe.vad_max_prob,
                vad_mean_prob_at_validation=probe.vad_mean_prob,
                rms_db_at_validation=probe.rms_db,
                probe_duration_ms=probe.duration_ms,
                detected_apos_at_validation=tuple(detected_apos),
                cascade_attempts_before_success=cascade_attempts_before_success,
                boots_validated=1,
                last_boot_validated=now_iso,
                last_boot_diagnosis=probe.diagnosis,
                probe_history=[
                    ProbeHistoryEntry(
                        ts=now_iso,
                        mode=probe.mode,
                        diagnosis=probe.diagnosis,
                        vad_max_prob=probe.vad_max_prob,
                        rms_db=probe.rms_db,
                        duration_ms=probe.duration_ms,
                    ),
                ],
                pinned=False,
                needs_revalidation=False,
                available=True,
            )
            self._write_atomic()

    def record_probe(self, endpoint_guid: str, probe: ProbeResult) -> None:
        self._ensure_loaded()
        live = self._entries.get(endpoint_guid)
        if live is None:
            return
        with acquire_file_lock(self._lock_path):
            now_iso = self._clock().isoformat(timespec="seconds")
            live.probe_history.append(
                ProbeHistoryEntry(
                    ts=now_iso,
                    mode=probe.mode,
                    diagnosis=probe.diagnosis,
                    vad_max_prob=probe.vad_max_prob,
                    rms_db=probe.rms_db,
                    duration_ms=probe.duration_ms,
                ),
            )
            if len(live.probe_history) > _PROBE_HISTORY_MAX:
                live.probe_history = live.probe_history[-_PROBE_HISTORY_MAX:]
            self._write_atomic()

    def increment_boots_validated(
        self,
        endpoint_guid: str,
        diagnosis: Diagnosis,
    ) -> None:
        self._ensure_loaded()
        live = self._entries.get(endpoint_guid)
        if live is None:
            return
        with acquire_file_lock(self._lock_path):
            live.boots_validated += 1
            live.last_boot_validated = self._clock().isoformat(timespec="seconds")
            live.last_boot_diagnosis = diagnosis
            if diagnosis is Diagnosis.HEALTHY:
                live.needs_revalidation = False
            self._write_atomic()

    def invalidate(self, endpoint_guid: str, reason: str) -> None:
        self._ensure_loaded()
        if endpoint_guid not in self._entries:
            return
        with acquire_file_lock(self._lock_path):
            self._entries.pop(endpoint_guid, None)
            self._stats.invalidations_by_reason[reason] = (
                self._stats.invalidations_by_reason.get(reason, 0) + 1
            )
            self._write_atomic()
        record_combo_store_invalidation(reason=reason)

    def invalidate_all(self) -> None:
        self._ensure_loaded()
        with acquire_file_lock(self._lock_path):
            self._archive(raw_reason="invalidate-all")
            self._entries = {}
            self._write_atomic()

    # ── helpers ──────────────────────────────────────────────────────

    def _ensure_loaded(self) -> None:
        if not self._loaded:
            self.load()

    def _read_with_backup_recovery(
        self,
    ) -> tuple[bool, dict[str, Any]] | None:
        """Read main file; on parse failure try ``.bak``. Returns ``None`` on total failure."""
        try:
            text = self._path.read_text(encoding="utf-8")
            data = json.loads(text)
            if not isinstance(data, dict):
                raise ValueError("root is not a JSON object")  # noqa: TRY004, TRY301
        except (OSError, ValueError) as exc:
            logger.warning(
                "combo_store_main_read_failed",
                detail=str(exc),
                path=str(self._path),
            )
        else:
            return False, data

        if self._bak_path.exists():
            try:
                text = self._bak_path.read_text(encoding="utf-8")
                data = json.loads(text)
                if not isinstance(data, dict):
                    raise ValueError("backup root is not a JSON object")  # noqa: TRY004, TRY301
            except (OSError, ValueError) as exc:
                logger.warning(
                    "combo_store_backup_read_failed",
                    detail=str(exc),
                    path=str(self._bak_path),
                )
            else:
                logger.info("combo_store_recovered_from_backup", path=str(self._bak_path))
                return True, data

        return None

    def _archive(self, *, raw_reason: str) -> Path:
        """Move the current file out of the way with a timestamped suffix."""
        ts = self._clock().strftime("%Y%m%dT%H%M%SZ")
        dest = self._path.with_suffix(f".corrupt-{raw_reason}-{ts}.json")
        try:
            if self._path.exists():
                shutil.move(str(self._path), str(dest))
        except OSError as exc:
            logger.warning("combo_store_archive_failed", detail=str(exc))
        return dest

    def _archive_corrupt(self) -> Path:
        return self._archive(raw_reason="parse-error")

    def _write_atomic(self) -> None:
        """Atomic write: tmp → fsync → rename. Backs up the prior file first."""
        self._path.parent.mkdir(parents=True, exist_ok=True)

        if self._path.exists():
            try:
                shutil.copy2(str(self._path), str(self._bak_path))
            except OSError as exc:
                logger.warning("combo_store_backup_copy_failed", detail=str(exc))

        payload = self._serialize()
        text = json.dumps(payload, indent=2, ensure_ascii=False, sort_keys=True)

        # NamedTemporaryFile + delete=False so we can rename it; we close the
        # handle ourselves before the rename.
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
        fp = self._fingerprint_fn()
        now_iso = self._clock().isoformat(timespec="seconds")
        return {
            "schema_version": CURRENT_SCHEMA_VERSION,
            "generated_by": "sovyx",
            "last_updated": now_iso,
            "platform": self._platform_label_fn(),
            "os_build": self._os_build_fn(),
            "vad_model_version": self._vad_model_version,
            "wake_word_model_version": self._wake_word_model_version,
            "stt_model_version": self._stt_model_version,
            "audio_subsystem_fingerprint": _fingerprint_to_dict(fp),
            "entries": {
                guid: _entry_to_dict(live) for guid, live in sorted(self._entries.items())
            },
        }

    def _build_live_entry(
        self,
        guid: str,
        raw_entry: object,
        _rules: list[tuple[str, str]],
    ) -> _LiveEntry:
        if not isinstance(raw_entry, dict):
            raise _SanityError("entry", "not a dict")
        if not guid:
            raise _SanityError("endpoint_guid", "")

        winning_raw = raw_entry.get("winning_combo")
        if not isinstance(winning_raw, dict):
            raise _SanityError("winning_combo", "not a dict")
        sample_rate = winning_raw.get("sample_rate")
        if sample_rate not in ALLOWED_SAMPLE_RATES:
            raise _SanityError("sample_rate", sample_rate)
        channels = winning_raw.get("channels")
        if not (isinstance(channels, int) and _CHANNELS_MIN <= channels <= _CHANNELS_MAX):
            raise _SanityError("channels", channels)
        sample_format = winning_raw.get("sample_format", "int16")
        if sample_format not in ALLOWED_FORMATS:
            raise _SanityError("sample_format", sample_format)
        host_api = winning_raw.get("host_api")
        allowed = _allowed_host_apis()
        if not isinstance(host_api, str) or (allowed and host_api not in allowed):
            raise _SanityError("host_api", host_api)
        frames_per_buffer = winning_raw.get("frames_per_buffer", 480)
        if not (
            isinstance(frames_per_buffer, int)
            and _FRAMES_PER_BUFFER_MIN <= frames_per_buffer <= _FRAMES_PER_BUFFER_MAX
        ):
            raise _SanityError("frames_per_buffer", frames_per_buffer)

        try:
            combo = Combo(
                host_api=host_api,
                sample_rate=int(sample_rate),
                channels=channels,
                sample_format=sample_format,
                exclusive=bool(winning_raw.get("exclusive", False)),
                auto_convert=bool(winning_raw.get("auto_convert", False)),
                frames_per_buffer=frames_per_buffer,
            )
        except ValueError as exc:
            raise _SanityError("winning_combo", str(exc)) from exc

        vad_max = raw_entry.get("vad_max_prob_at_validation")
        if vad_max is not None and not (
            isinstance(vad_max, (int, float)) and _VAD_MIN <= vad_max <= _VAD_MAX
        ):
            raise _SanityError("vad_max_prob_at_validation", vad_max)
        vad_mean = raw_entry.get("vad_mean_prob_at_validation")
        if vad_mean is not None and not (
            isinstance(vad_mean, (int, float)) and _VAD_MIN <= vad_mean <= _VAD_MAX
        ):
            raise _SanityError("vad_mean_prob_at_validation", vad_mean)

        rms_db = raw_entry.get("rms_db_at_validation", 0.0)
        if not (isinstance(rms_db, (int, float)) and _RMS_DB_MIN <= rms_db <= _RMS_DB_MAX):
            raise _SanityError("rms_db_at_validation", rms_db)

        boots_validated = raw_entry.get("boots_validated", 0)
        if not (isinstance(boots_validated, int) and boots_validated >= _BOOTS_VALIDATED_MIN):
            raise _SanityError("boots_validated", boots_validated)

        validation_mode_str = raw_entry.get("validation_mode", "warm")
        if validation_mode_str not in {"cold", "warm"}:
            raise _SanityError("validation_mode", validation_mode_str)

        diag_str = raw_entry.get("last_boot_diagnosis", "healthy")
        try:
            last_boot_diagnosis = Diagnosis(diag_str)
        except ValueError as exc:
            raise _SanityError("last_boot_diagnosis", diag_str) from exc

        history_raw = raw_entry.get("probe_history") or []
        history: list[ProbeHistoryEntry] = []
        if isinstance(history_raw, list):
            for hist in history_raw[-_PROBE_HISTORY_MAX:]:
                if not isinstance(hist, dict):
                    continue
                try:
                    history.append(
                        ProbeHistoryEntry(
                            ts=str(hist.get("ts", "")),
                            mode=ProbeMode(hist.get("mode", "warm")),
                            diagnosis=Diagnosis(hist.get("diagnosis", "unknown")),
                            vad_max_prob=hist.get("vad_max_prob"),
                            rms_db=float(hist.get("rms_db", 0.0)),
                            duration_ms=int(hist.get("duration_ms", 0)),
                        ),
                    )
                except (ValueError, TypeError):
                    continue

        return _LiveEntry(
            endpoint_guid=guid,
            device_friendly_name=str(raw_entry.get("device_friendly_name", "")),
            device_interface_name=str(raw_entry.get("device_interface_name", "")),
            device_class=str(raw_entry.get("device_class", "other")),
            endpoint_fxproperties_sha=str(raw_entry.get("endpoint_fxproperties_sha", "")),
            winning_combo=combo,
            validated_at=str(raw_entry.get("validated_at", "")),
            validation_mode=ProbeMode(validation_mode_str),
            vad_max_prob_at_validation=(
                float(vad_max) if isinstance(vad_max, (int, float)) else None
            ),
            vad_mean_prob_at_validation=(
                float(vad_mean) if isinstance(vad_mean, (int, float)) else None
            ),
            rms_db_at_validation=float(rms_db),
            probe_duration_ms=int(raw_entry.get("probe_duration_ms", 0)),
            detected_apos_at_validation=tuple(
                str(x) for x in (raw_entry.get("detected_apos_at_validation") or [])
            ),
            cascade_attempts_before_success=int(
                raw_entry.get("cascade_attempts_before_success", 0),
            ),
            boots_validated=int(boots_validated),
            last_boot_validated=str(raw_entry.get("last_boot_validated", "")),
            last_boot_diagnosis=last_boot_diagnosis,
            probe_history=history,
            pinned=bool(raw_entry.get("pinned", False)),
            needs_revalidation=False,
            candidate_kind=str(raw_entry.get("candidate_kind", "unknown")),
        )


# ── module-level helpers (also imported by tests) ───────────────────────


class _SanityError(Exception):
    """Internal — raised by :meth:`ComboStore._build_live_entry` on R12 hit."""

    def __init__(self, field_name: str, value: object) -> None:
        super().__init__(f"{field_name}={value!r}")
        self.field = field_name
        self.value = value


def _fingerprint_to_dict(fp: AudioSubsystemFingerprint) -> dict[str, Any]:
    return {
        "windows_audio_endpoints_sha": fp.windows_audio_endpoints_sha,
        "windows_fxproperties_global_sha": fp.windows_fxproperties_global_sha,
        "linux_pulseaudio_config_sha": fp.linux_pulseaudio_config_sha,
        "macos_coreaudio_plugins_sha": fp.macos_coreaudio_plugins_sha,
        "computed_at": fp.computed_at,
    }


def _combo_to_dict(combo: Combo) -> dict[str, Any]:
    return {
        "host_api": combo.host_api,
        "sample_rate": combo.sample_rate,
        "channels": combo.channels,
        "sample_format": combo.sample_format,
        "exclusive": combo.exclusive,
        "auto_convert": combo.auto_convert,
        "frames_per_buffer": combo.frames_per_buffer,
    }


def _history_to_dict(entry: ProbeHistoryEntry) -> dict[str, Any]:
    return {
        "ts": entry.ts,
        "mode": entry.mode.value,
        "diagnosis": entry.diagnosis.value,
        "vad_max_prob": entry.vad_max_prob,
        "rms_db": entry.rms_db,
        "duration_ms": entry.duration_ms,
    }


def _entry_to_dict(live: _LiveEntry) -> dict[str, Any]:
    return {
        "endpoint_guid": live.endpoint_guid,
        "device_friendly_name": live.device_friendly_name,
        "device_interface_name": live.device_interface_name,
        "device_class": live.device_class,
        "endpoint_fxproperties_sha": live.endpoint_fxproperties_sha,
        "winning_combo": _combo_to_dict(live.winning_combo),
        "validated_at": live.validated_at,
        "validation_mode": live.validation_mode.value,
        "vad_max_prob_at_validation": live.vad_max_prob_at_validation,
        "vad_mean_prob_at_validation": live.vad_mean_prob_at_validation,
        "rms_db_at_validation": live.rms_db_at_validation,
        "probe_duration_ms": live.probe_duration_ms,
        "detected_apos_at_validation": list(live.detected_apos_at_validation),
        "cascade_attempts_before_success": live.cascade_attempts_before_success,
        "boots_validated": live.boots_validated,
        "last_boot_validated": live.last_boot_validated,
        "last_boot_diagnosis": live.last_boot_diagnosis.value,
        "probe_history": [_history_to_dict(h) for h in live.probe_history],
        "pinned": live.pinned,
        # voice-linux-cascade-root-fix T11 / schema v3.
        "candidate_kind": live.candidate_kind,
    }


__all__ = [
    "ComboStore",
]
