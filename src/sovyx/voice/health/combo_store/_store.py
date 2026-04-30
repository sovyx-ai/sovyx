"""ComboStore class — persistent memoization of (endpoint × winning_combo) tuples.

See :mod:`sovyx.voice.health` and ADR-combo-store-schema.md for the
full design. This module owns the ComboStore class — the on-disk
shape's atomic writes, cross-process file lock, and the 13
invalidation rules that make the fast path safe against drift.

Supporting types live in sibling modules:

* :mod:`._constants` — validation thresholds + tuning-derived knobs.
* :mod:`._models` — :class:`_LiveEntry`, :class:`_SanityError`,
  serialization helpers, and platform helpers.

The store is **advisory**. Every entry is re-validated on boot via at
least a cold probe; ``HEALTHY`` results clear the ``needs_revalidation``
flag, anything else falls through to the L2 cascade.
"""

from __future__ import annotations

import contextlib
import dataclasses
import json
import os
import platform
import shutil
import sys
import tempfile
from datetime import datetime, timedelta
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
from sovyx.voice.health.combo_store._constants import (
    _AGE_DEGRADED_DAYS,
    _AGE_STALE_DAYS,
    _BOOTS_VALIDATED_MIN,
    _CHANNELS_MAX,
    _CHANNELS_MIN,
    _FRAMES_PER_BUFFER_MAX,
    _FRAMES_PER_BUFFER_MIN,
    _PIN_AUTO_UNPIN_FAILURE_THRESHOLD,
    _PROBE_HISTORY_MAX,
    _RMS_DB_MAX,
    _RMS_DB_MIN,
    _RMS_DB_R14_SILENT_CEILING,
    _VAD_MAX,
    _VAD_MIN,
)
from sovyx.voice.health.combo_store._models import (
    _allowed_host_apis,
    _entry_to_dict,
    _fingerprint_to_dict,
    _LiveEntry,
    _platform_label,
    _SanityError,
    _utc_now,
)
from sovyx.voice.health.contract import (
    ALLOWED_FORMATS,
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


def _coerce_optional_str(value: object) -> str | None:
    """Coerce a JSON-deserialized value into ``str | None``.

    JSON ``null`` deserializes to Python ``None``; legacy entries
    that lack the field also surface as ``None`` via ``dict.get``.
    Non-string non-None values (corrupted writes, hand-edited JSON)
    fall back to ``None`` rather than raising — combo-store reads
    are best-effort, and a malformed optional-string field shouldn't
    bubble up as a sanity error when the field has no validator.
    """
    if value is None:
        return None
    if isinstance(value, str):
        return value or None
    return None


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
        usb_fingerprint_resolver: T5.43 + T5.51 wire-up. Optional
            callable mapping ``endpoint_guid → "usb-VVVV:PPPP[-SERIAL]"``
            (or ``None`` for non-USB endpoints). When configured,
            :meth:`record_winning` resolves + persists the fingerprint
            on each entry, and :meth:`get` falls back to a fingerprint
            scan when the primary GUID lookup misses — recovering the
            validated combo across port changes / firmware updates.
            Default ``None`` (lenient) preserves pre-wire-up behaviour:
            no fingerprint persisted, no fallback scan.
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
        usb_fingerprint_resolver: Callable[[str], str | None] | None = None,
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
        self._usb_fingerprint_resolver = usb_fingerprint_resolver
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

            # R14 — Phase 3 / T3.6 — silent_combo_evict.
            #
            # Legacy v0.23.x-and-earlier silent winners (rms_db <
            # -70 dBFS) persisted to disk before the cold-probe
            # strict signal validation landed in T11 (commit
            # ``c888c2b``). Post-T11 the probe REJECTS such combos
            # at validation time so fresh writes cannot re-introduce
            # them, but legacy entries still on disk would replicate
            # the Furo W-1 deaf state on every boot (the cascade
            # picks the silent winner by GUID, opens the same APO-
            # destroyed substrate, and the user hears nothing).
            #
            # R14 evicts those entries on load. Idempotent because
            # post-T11 there is no path to write a replacement; on
            # the second boot the ComboStore has no silent entries
            # left and R14 fires zero times. The structured event
            # mirrors the R12 sanity-failed pattern so dashboards
            # can correlate eviction telemetry with the W-1 cure
            # rollout.
            if live.rms_db_at_validation < _RMS_DB_R14_SILENT_CEILING:
                rules.append(("R14", guid))
                logger.warning(
                    "combo_store_r14_silent_evicted",
                    endpoint=guid,
                    rms_db_at_validation=live.rms_db_at_validation,
                    threshold_db=_RMS_DB_R14_SILENT_CEILING,
                    last_boot_diagnosis=live.last_boot_diagnosis.value,
                    action_taken=(
                        "evicted as legacy silent winner pre-Furo-W-1 cure; "
                        "next probe will validate a fresh combo via the "
                        "post-T11 strict cold path"
                    ),
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
        if live is not None and live.available:
            self._stats.fast_path_hits += 1
            return live.to_immutable()
        # T5.43 + T5.51 wire-up — fingerprint fallback. Either the
        # endpoint_guid was never seen (replug landed on a NEW guid
        # on Windows; surrogate hash drifted on Linux) or the entry
        # exists but R13 marked it ``available=False`` (the OLD
        # endpoint stopped enumerating). Both states mean "the
        # cascade-validated combo for this physical USB device may
        # exist under a different key" — the resolver derives the
        # canonical USB fingerprint and we scan entries for a match.
        recovered = self._fingerprint_recover(endpoint_guid)
        if recovered is not None:
            self._stats.fast_path_hits += 1
            return recovered
        self._stats.fast_path_misses += 1
        return None

    def _fingerprint_recover(self, endpoint_guid: str) -> ComboEntry | None:
        """Second-chance lookup via stable USB fingerprint.

        Returns ``None`` immediately when no resolver is configured
        (default; back-compat). Otherwise resolves the requested
        endpoint to a canonical ``"usb-VVVV:PPPP[-SERIAL]"`` shape
        and scans the in-memory entries for one carrying the same
        fingerprint.

        Match criteria intentionally include ``available=False``
        entries: post-replug the OLD endpoint_guid is no longer
        enumerated (R13 marks it unavailable), so the unavailable
        entry IS the one we want to recover. The cascade re-validates
        the returned combo against the NEW endpoint anyway —
        :class:`ComboStore` is advisory, not authoritative.

        Returns the matched :class:`ComboEntry` or ``None`` when:

        * No resolver wired (pre-wire-up default).
        * Resolver returns ``None`` (non-USB endpoint, slim-CI host
          without comtypes, missing sysfs path).
        * No stored entry carries the resolved fingerprint.
        * The stored entry's fingerprint matches but its endpoint_guid
          equals the request (defensive — we already missed primary
          lookup, so this can only happen in races).
        """
        if self._usb_fingerprint_resolver is None:
            return None
        try:
            fingerprint = self._usb_fingerprint_resolver(endpoint_guid)
        except Exception as exc:  # noqa: BLE001 — resolver best-effort
            logger.debug(
                "voice.combo_store.usb_fingerprint_resolve_failed",
                endpoint=endpoint_guid,
                reason=str(exc),
                exc_type=type(exc).__name__,
            )
            return None
        if not fingerprint:
            return None
        for stored_guid, live in self._entries.items():
            if live.usb_fingerprint != fingerprint:
                continue
            # Defensive — primary lookup already missed; if the GUIDs
            # equal here the entry must be unavailable (R13) and we
            # WANT to recover, otherwise it's a race and we'd be
            # returning the same entry twice. Skip only the
            # GUID-equal + available case (the racy one); the
            # GUID-equal + unavailable case is the whole point of
            # the fingerprint fallback.
            if stored_guid == endpoint_guid and live.available:
                continue
            logger.info(
                "voice.combo_store.usb_fingerprint_recovery_hit",
                requested_endpoint=endpoint_guid,
                matched_endpoint=stored_guid,
                usb_fingerprint=fingerprint,
                matched_available=live.available,
                matched_needs_revalidation=live.needs_revalidation,
            )
            # Recovery always implies revalidation — the matched entry's
            # combo was validated against the OLD endpoint_guid, the
            # caller asked about a NEW endpoint_guid. The cascade
            # re-validates before trusting the combo on the new key.
            recovered = live.to_immutable()
            return dataclasses.replace(recovered, needs_revalidation=True)
        return None

    def needs_revalidation(self, endpoint_guid: str) -> bool:
        self._ensure_loaded()
        live = self._entries.get(endpoint_guid)
        if live is not None and live.available:
            return live.needs_revalidation
        # T5.43 + T5.51 wire-up — fingerprint recovery always implies
        # revalidation (the recovered combo is keyed by a DIFFERENT
        # endpoint_guid, so it has never been validated against the
        # caller's endpoint). Symmetric with :meth:`get`'s recovery
        # path so cascade-side logging fires correctly:
        # ``needs_revalidation(guid)`` returns ``True`` on a recovery
        # hit and the cascade emits ``voice_cascade_store_needs_revalidation``.
        return (
            self._usb_fingerprint_resolver is not None
            and self._fingerprint_recover(endpoint_guid) is not None
        )

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
        usb_fingerprint: str | None = None,
    ) -> None:
        self._ensure_loaded()
        # T5.43 + T5.51 wire-up: when the caller didn't pre-resolve
        # the fingerprint (most call-sites don't) AND a resolver is
        # configured, derive it best-effort from the endpoint_guid.
        # Failures return ``None`` and the entry is persisted without
        # a fingerprint — back-compat with pre-wire-up behaviour.
        resolved_fingerprint = usb_fingerprint
        if resolved_fingerprint is None and self._usb_fingerprint_resolver is not None:
            try:
                resolved_fingerprint = self._usb_fingerprint_resolver(endpoint_guid)
            except Exception as exc:  # noqa: BLE001 — resolver is best-effort
                logger.debug(
                    "voice.combo_store.usb_fingerprint_resolve_failed",
                    endpoint=endpoint_guid,
                    reason=str(exc),
                    exc_type=type(exc).__name__,
                )
                resolved_fingerprint = None
        with acquire_file_lock(self._lock_path):
            # C1: refresh from disk inside the lock so concurrent
            # writes from another daemon process are merged, not
            # clobbered by our stale in-memory snapshot.
            self._refresh_entries_from_disk_locked()
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
                usb_fingerprint=resolved_fingerprint,
            )
            self._write_atomic()

    def record_probe(self, endpoint_guid: str, probe: ProbeResult) -> None:
        self._ensure_loaded()
        live = self._entries.get(endpoint_guid)
        if live is None:
            return
        with acquire_file_lock(self._lock_path):
            # C1: refresh-then-resolve. Another process may have just
            # invalidated this entry; re-read disk to pick that up,
            # then re-fetch ``live`` against the post-refresh state.
            self._refresh_entries_from_disk_locked()
            live = self._entries.get(endpoint_guid)
            if live is None:
                # Entry was invalidated by a concurrent process between
                # our pre-lock check and the refresh — drop the probe
                # silently (treating it as recorded against the
                # invalidated entry would just resurrect dead data).
                return
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
            # C2 auto-unpin lifecycle. Only applies when the entry is
            # currently pinned — counter is reset on the first HEALTHY
            # probe so an intermittent transient doesn't cause an
            # "almost-unpin" that survives across boots.
            self._maybe_auto_unpin(endpoint_guid, live, probe)
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
            # C1: refresh inside lock + re-fetch live (another process
            # may have invalidated it since our pre-lock check).
            self._refresh_entries_from_disk_locked()
            live = self._entries.get(endpoint_guid)
            if live is None:
                return
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
            # C1: refresh inside lock so a concurrent record_winning
            # by another process for OTHER GUIDs is preserved when we
            # write the post-pop snapshot.
            self._refresh_entries_from_disk_locked()
            self._entries.pop(endpoint_guid, None)
            self._stats.invalidations_by_reason[reason] = (
                self._stats.invalidations_by_reason.get(reason, 0) + 1
            )
            self._write_atomic()
        record_combo_store_invalidation(reason=reason)

    def invalidate_all(self) -> None:
        self._ensure_loaded()
        with acquire_file_lock(self._lock_path):
            # invalidate_all is by definition destructive — no need to
            # refresh from disk first; we're discarding everything
            # whether or not a concurrent write landed in the window.
            self._archive(raw_reason="invalidate-all")
            self._entries = {}
            self._write_atomic()

    # ── helpers ──────────────────────────────────────────────────────

    def _ensure_loaded(self) -> None:
        if not self._loaded:
            self.load()

    def _refresh_entries_from_disk_locked(self) -> None:
        """C1: read-modify-write — refresh ``self._entries`` from disk
        while holding the file lock so concurrent writes from another
        process are merged in instead of clobbered.

        The pre-C1 write path was:
            ``_ensure_loaded`` (no lock) → acquire_file_lock →
            mutate self._entries (in-memory snapshot) → _write_atomic.

        Window between ``_ensure_loaded`` and lock acquisition lets
        another process write a fresh snapshot. The current process
        then writes its STALE in-memory view + pending mutation, losing
        the concurrent process's writes — the canonical lost-update
        TOCTOU bug. The mission identified this as ComboStore band-aid
        #25 (concurrent boot corruption).

        Fix: re-read the file from disk INSIDE the lock immediately
        before applying the pending mutation. Caller MUST hold the
        file lock when invoking this method (asserted by precondition,
        not enforced because Python lacks an attestable lock guard).

        Invariants after this call:
        * ``self._entries`` reflects the current on-disk state (any
          concurrent writes that happened between the caller's
          ``_ensure_loaded`` and lock acquisition are now visible).
        * ``self._loaded`` stays ``True`` (it already was, as the
          caller called ``_ensure_loaded`` before the lock).
        * Read failures (corrupt JSON, missing file) leave the
          in-memory ``self._entries`` unchanged — preserving the
          last-known-good state is safer than dropping every entry
          mid-write. The corruption itself surfaces via the existing
          ``load()`` rules R1-R13 on the next full reload.

        Note: this only refreshes ``_entries``; the global metadata
        (fingerprint, model versions, OS build) doesn't change
        between writes, so a write doesn't need to re-run the R5-R8
        invalidation rules. The next ``load()`` call (next process
        boot) will re-validate everything.
        """
        if not self._path.exists():
            return  # nothing on disk yet — our in-memory state IS the truth
        try:
            text = self._path.read_text(encoding="utf-8")
            raw = json.loads(text)
            if not isinstance(raw, dict):
                return
        except (OSError, ValueError):
            # Corrupt / unreadable file — preserve in-memory state.
            # The next full load() picks up the corruption via R2.
            return
        new_entries: dict[str, _LiveEntry] = {}
        for guid, raw_entry in (raw.get("entries") or {}).items():
            try:
                live = self._build_live_entry(guid, raw_entry, [])
            except _SanityError:
                # Corrupt entry — skip it. The next load() will surface
                # it via R12 with structured logging.
                continue
            # ``needs_revalidation`` is COMPUTED state from
            # load()-time rules R6/R7/R8/R10/R11/R13 and never
            # persisted. If the entry existed in our in-memory cache
            # before the refresh, preserve its computed flag — the
            # disk read can't reproduce the load-time context that
            # set it. New entries (visible only on disk because a
            # concurrent process wrote them after our last load)
            # default to ``False``, which matches the freshly-loaded
            # semantic.
            previous = self._entries.get(guid)
            if previous is not None and previous.needs_revalidation:
                live.needs_revalidation = True
            new_entries[guid] = live
        self._entries = new_entries

    def _maybe_auto_unpin(
        self,
        endpoint_guid: str,
        live: _LiveEntry,
        probe: ProbeResult,
    ) -> None:
        """C2 auto-unpin lifecycle for a pinned entry after probe failure.

        Lifecycle rules:

        * Probe was HEALTHY — clear the failure counter so a future
          intermittent failure doesn't carry stale weight.
        * Probe was non-HEALTHY and the entry is pinned — bump the
          counter. When it reaches
          :data:`_PIN_AUTO_UNPIN_FAILURE_THRESHOLD` (2), release the
          pin and emit ``voice.combo_store.pin_auto_unpinned``.
        * Probe was non-HEALTHY and the entry is NOT pinned — no
          action; the counter is only meaningful while pinned.

        The counter persists across boots (it's serialised into the
        JSON entry), so a daemon that crashes between probe failures
        still triggers the unpin on the next failure rather than
        silently resetting on cold-start.
        """
        if probe.diagnosis is Diagnosis.HEALTHY:
            if live.consecutive_validation_failures > 0:
                logger.debug(
                    "voice.combo_store.pin_failure_counter_reset",
                    endpoint=endpoint_guid,
                    previous_count=live.consecutive_validation_failures,
                )
                live.consecutive_validation_failures = 0
            return
        if not live.pinned:
            # Counter only matters for pinned entries — no point
            # bumping for an unpinned entry that's already eligible
            # for cascade re-evaluation.
            return
        live.consecutive_validation_failures += 1
        if live.consecutive_validation_failures < _PIN_AUTO_UNPIN_FAILURE_THRESHOLD:
            logger.info(
                "voice.combo_store.pin_failure_recorded",
                endpoint=endpoint_guid,
                consecutive_failures=live.consecutive_validation_failures,
                threshold=_PIN_AUTO_UNPIN_FAILURE_THRESHOLD,
                diagnosis=probe.diagnosis.value,
            )
            return
        # Threshold met — release the pin and reset the counter so a
        # subsequent re-pin starts from a clean lifecycle.
        live.pinned = False
        failures_at_unpin = live.consecutive_validation_failures
        live.consecutive_validation_failures = 0
        live.needs_revalidation = True
        logger.warning(
            "voice.combo_store.pin_auto_unpinned",
            **{
                "voice.endpoint_guid": endpoint_guid,
                "voice.consecutive_failures": failures_at_unpin,
                "voice.threshold": _PIN_AUTO_UNPIN_FAILURE_THRESHOLD,
                "voice.last_diagnosis": probe.diagnosis.value,
                "voice.action": "pin_released_cascade_will_re_validate_next_boot",
            },
        )

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
            consecutive_validation_failures=int(
                raw_entry.get("consecutive_validation_failures", 0),
            ),
            usb_fingerprint=_coerce_optional_str(raw_entry.get("usb_fingerprint")),
        )


__all__ = [
    "ComboStore",
]
