"""Data classes + serialization helpers for the ComboStore.

* :class:`_LiveEntry` — the mutable in-memory shape of a stored row,
  serialized to / from JSON via the helper functions below.
* :class:`_SanityError` — internal exception raised by R12 validation
  in :meth:`ComboStore._build_live_entry`.
* :func:`_fingerprint_to_dict` / :func:`_combo_to_dict` /
  :func:`_history_to_dict` / :func:`_entry_to_dict` — serialization
  helpers exercised both by the store's atomic-write path and the
  test surface (capture_overrides imports ``_combo_to_dict`` to
  serialize pinned-override payloads).
* Platform helpers — :func:`_utc_now`, :func:`_platform_label`,
  :func:`_allowed_host_apis` — used by the store's R-rule validation
  and stored-entry fingerprint computation.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from sovyx.voice.health.contract import (
    ALLOWED_HOST_APIS_BY_PLATFORM,
    AudioSubsystemFingerprint,
    Combo,
    ComboEntry,
    Diagnosis,
    ProbeHistoryEntry,
    ProbeMode,
)

# ── Time + platform helpers ──────────────────────────────────────


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


# ── In-memory entry ──────────────────────────────────────────────


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
    consecutive_validation_failures: int = 0
    """C2 auto-unpin counter. Bumped on every non-HEALTHY probe of a
    pinned entry by :meth:`ComboStore.record_probe`; cleared on the
    first HEALTHY probe and on auto-unpin. Persists across boots so a
    failure run that spans a restart still triggers the unpin —
    otherwise a daemon that crashes between probe failures would
    silently reset the counter on every cold-start.

    Default ``0`` is backwards-compatible: pre-C2 entries that don't
    carry the field in their JSON read as 0 (no failures yet)."""
    # voice-linux-cascade-root-fix T11 / schema v3. :class:`DeviceKind`
    # value as a string (``"hardware"`` / ``"session_manager_virtual"``
    # / ``"os_default"`` / ``"unknown"``). Back-compat default
    # ``"unknown"`` so existing writers that don't yet populate the
    # field continue to compile — legacy v2 entries migrate to this
    # value via :func:`_migrate_v2_to_v3`.
    candidate_kind: str = "unknown"
    # T5.43 + T5.51 wire-up — stable USB fingerprint
    # ``"usb-VVVV:PPPP[-SERIAL]"`` for cross-port / cross-firmware-update
    # combo recovery. Default ``None`` is back-compat: legacy entries
    # written pre-T5.51 wire-up don't carry the field in their JSON and
    # read as ``None``. Additive field, no schema version bump.
    usb_fingerprint: str | None = None

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
            usb_fingerprint=self.usb_fingerprint,
        )


# ── Sanity-check exception ───────────────────────────────────────


class _SanityError(Exception):
    """Internal — raised by :meth:`ComboStore._build_live_entry` on R12 hit."""

    def __init__(self, field_name: str, value: object) -> None:
        super().__init__(f"{field_name}={value!r}")
        self.field = field_name
        self.value = value


# ── Serialization helpers ────────────────────────────────────────


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
        # C2 auto-unpin lifecycle (additive — no schema bump; default 0
        # on read keeps pre-C2 entries backwards-compat).
        "consecutive_validation_failures": live.consecutive_validation_failures,
        # T5.43 + T5.51 wire-up. ``None`` writes as JSON ``null`` and
        # reads back as ``None`` via ``raw_entry.get("usb_fingerprint")``;
        # legacy entries that pre-date this field also read as ``None``.
        "usb_fingerprint": live.usb_fingerprint,
    }


__all__ = [
    "_LiveEntry",
    "_SanityError",
    "_allowed_host_apis",
    "_combo_to_dict",
    "_entry_to_dict",
    "_fingerprint_to_dict",
    "_history_to_dict",
    "_platform_label",
    "_utc_now",
]
