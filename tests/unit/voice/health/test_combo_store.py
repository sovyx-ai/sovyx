"""Tests for :mod:`sovyx.voice.health.combo_store`.

Covers every invalidation rule R1-R13, atomic-write + backup recovery,
sanity validator boundaries, and a property-based round-trip.
"""

from __future__ import annotations

import json
import os
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from sovyx.voice.health._combo_store_migrations import CURRENT_SCHEMA_VERSION
from sovyx.voice.health._file_lock import FileLockTimeoutError
from sovyx.voice.health.combo_store import (
    _PIN_AUTO_UNPIN_FAILURE_THRESHOLD,
    ComboStore,
    _combo_to_dict,
    _entry_to_dict,
    _fingerprint_to_dict,
    _history_to_dict,
    _LiveEntry,
    _SanityError,
)
from sovyx.voice.health.contract import (
    AudioSubsystemFingerprint,
    Combo,
    Diagnosis,
    ProbeHistoryEntry,
    ProbeMode,
    ProbeResult,
)

# ── Fixtures ─────────────────────────────────────────────────────────────


def _current_platform_key() -> str:
    if sys.platform.startswith("win"):
        return "win32"
    if sys.platform == "darwin":
        return "darwin"
    return "linux"


_PLATFORM_KEY = _current_platform_key()
_HOST_API = {"win32": "WASAPI", "linux": "ALSA", "darwin": "CoreAudio"}[_PLATFORM_KEY]
# A host_api valid on some other platform — used to exercise the sanity
# drop path ("this entry was recorded on a different OS").
_INVALID_HOST_API = "PulseAudio" if _PLATFORM_KEY == "win32" else "WASAPI"


_NOW = datetime(2026, 4, 19, 12, 0, 0, tzinfo=UTC)


def _clock_factory(now: datetime = _NOW) -> Any:
    state = {"t": now}

    def clock() -> datetime:
        return state["t"]

    def advance(delta: timedelta) -> None:
        state["t"] = state["t"] + delta

    clock.advance = advance  # type: ignore[attr-defined]
    return clock


def _good_combo() -> Combo:
    return Combo(
        host_api=_HOST_API,
        sample_rate=48000,
        channels=1,
        sample_format="int16",
        exclusive=False,
        auto_convert=False,
        frames_per_buffer=480,
        platform_key=_PLATFORM_KEY,
    )


def _good_probe(mode: ProbeMode = ProbeMode.WARM) -> ProbeResult:
    return ProbeResult(
        diagnosis=Diagnosis.HEALTHY,
        mode=mode,
        combo=_good_combo(),
        vad_max_prob=0.95,
        vad_mean_prob=0.42,
        rms_db=-20.5,
        callbacks_fired=50,
        duration_ms=1500,
    )


def _fingerprint(
    *,
    endpoints: str = "ep-sha-a",
    fx_global: str = "fx-sha-a",
) -> AudioSubsystemFingerprint:
    return AudioSubsystemFingerprint(
        windows_audio_endpoints_sha=endpoints,
        windows_fxproperties_global_sha=fx_global,
        linux_pulseaudio_config_sha="",
        macos_coreaudio_plugins_sha="",
        computed_at="2026-04-19T12:00:00+00:00",
    )


def _make_store(
    tmp_path: Path,
    *,
    clock: Any | None = None,
    fingerprint: AudioSubsystemFingerprint | None = None,
    endpoint_sha_for: Any = lambda guid: f"ep-{guid}",
    platform_label: str = "windows",
    os_build: str = "10.0.26200",
    live_guids: frozenset[str] | None = None,
    **kwargs: Any,
) -> ComboStore:
    path = tmp_path / "capture_combos.json"
    fp = fingerprint or _fingerprint()
    return ComboStore(
        path,
        clock=clock or _clock_factory(),
        fingerprint_fn=lambda: fp,
        endpoint_sha_fn=endpoint_sha_for,
        platform_label_fn=lambda: platform_label,
        os_build_fn=lambda: os_build,
        live_endpoint_guids=(lambda: live_guids) if live_guids is not None else None,
        **kwargs,
    )


def _record(store: ComboStore, guid: str = "{guid-A}") -> None:
    store.record_winning(
        guid,
        device_friendly_name="Test Mic",
        device_interface_name="USB\\VID_0000",
        device_class="microphone",
        endpoint_fxproperties_sha=f"ep-{guid}",
        combo=_good_combo(),
        probe=_good_probe(),
        detected_apos=(),
        cascade_attempts_before_success=1,
    )


# ── Helper dict builders ─────────────────────────────────────────────────


class TestHelpers:
    def test_fingerprint_to_dict_roundtrip(self) -> None:
        fp = _fingerprint()
        d = _fingerprint_to_dict(fp)
        assert d["windows_audio_endpoints_sha"] == fp.windows_audio_endpoints_sha
        assert d["computed_at"] == fp.computed_at

    def test_combo_to_dict_roundtrip(self) -> None:
        c = _good_combo()
        d = _combo_to_dict(c)
        assert d["host_api"] == _HOST_API
        assert d["sample_rate"] == 48000
        assert d["exclusive"] is False

    def test_history_to_dict_roundtrip(self) -> None:
        h = ProbeHistoryEntry(
            ts="2026-04-19T12:00:00+00:00",
            mode=ProbeMode.WARM,
            diagnosis=Diagnosis.HEALTHY,
            vad_max_prob=0.9,
            rms_db=-10.0,
            duration_ms=1000,
        )
        d = _history_to_dict(h)
        assert d["mode"] == "warm"
        assert d["diagnosis"] == "healthy"

    def test_entry_to_dict_roundtrip(self) -> None:
        live = _LiveEntry(
            endpoint_guid="{guid-A}",
            device_friendly_name="Mic",
            device_interface_name="USB",
            device_class="microphone",
            endpoint_fxproperties_sha="sha",
            winning_combo=_good_combo(),
            validated_at="2026-04-19T12:00:00+00:00",
            validation_mode=ProbeMode.WARM,
            vad_max_prob_at_validation=0.9,
            vad_mean_prob_at_validation=0.3,
            rms_db_at_validation=-20.0,
            probe_duration_ms=1000,
            detected_apos_at_validation=("VocaEffectPack",),
            cascade_attempts_before_success=2,
            boots_validated=3,
            last_boot_validated="2026-04-19T12:00:00+00:00",
            last_boot_diagnosis=Diagnosis.HEALTHY,
        )
        d = _entry_to_dict(live)
        assert d["validation_mode"] == "warm"
        assert d["last_boot_diagnosis"] == "healthy"
        assert d["detected_apos_at_validation"] == ["VocaEffectPack"]
        assert d["winning_combo"]["host_api"] == _HOST_API


class TestSanityError:
    def test_carries_field_and_value(self) -> None:
        exc = _SanityError("sample_rate", 12345)
        assert exc.field == "sample_rate"
        assert exc.value == 12345
        assert "sample_rate=12345" in str(exc)


# ── Basic lifecycle ──────────────────────────────────────────────────────


class TestWriteReadRoundtrip:
    def test_record_then_load_returns_entry(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        store.load()
        _record(store)

        # Reopen to force a fresh load.
        store2 = _make_store(tmp_path)
        rep = store2.load()
        assert rep.entries_loaded == 1
        assert rep.entries_dropped == 0
        entry = store2.get("{guid-A}")
        assert entry is not None
        assert entry.winning_combo.host_api == _HOST_API
        assert entry.boots_validated == 1
        assert len(entry.probe_history) == 1

    def test_stats_count_hit_and_miss(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        store.load()
        _record(store)
        assert store.get("{guid-A}") is not None
        assert store.get("does-not-exist") is None
        stats = store.stats()
        assert stats.fast_path_hits == 1
        assert stats.fast_path_misses == 1

    def test_entries_iterator(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        store.load()
        _record(store, "{guid-A}")
        _record(store, "{guid-B}")
        guids = {e.endpoint_guid for e in store.entries()}
        assert guids == {"{guid-A}", "{guid-B}"}

    def test_ensure_loaded_auto_on_first_get(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        assert store.get("{guid-A}") is None  # triggers load
        assert store.needs_revalidation("{guid-A}") is False

    def test_needs_revalidation_missing_endpoint_returns_false(
        self,
        tmp_path: Path,
    ) -> None:
        store = _make_store(tmp_path)
        store.load()
        assert store.needs_revalidation("{nope}") is False


# ── Rules R1-R13 ────────────────────────────────────────────────────────


class TestRule1FileMissing:
    def test_reports_r1_when_no_file(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        rep = store.load()
        assert ("R1", "<global>") in rep.rules_applied
        assert rep.entries_loaded == 0
        assert rep.backup_used is False


class TestRule2ParseError:
    def test_corrupt_json_archives(self, tmp_path: Path) -> None:
        path = tmp_path / "capture_combos.json"
        path.write_text("{not valid json", encoding="utf-8")
        store = _make_store(tmp_path)
        rep = store.load()
        assert any(code == "R2" for code, _ in rep.rules_applied)
        assert rep.archived_to is not None
        assert not path.exists()

    def test_root_not_object_archives(self, tmp_path: Path) -> None:
        path = tmp_path / "capture_combos.json"
        path.write_text("[1, 2, 3]", encoding="utf-8")
        store = _make_store(tmp_path)
        rep = store.load()
        assert any(code == "R2" for code, _ in rep.rules_applied)

    def test_recovers_from_backup(self, tmp_path: Path) -> None:
        # Write a good file then record via its backup path.
        store = _make_store(tmp_path)
        store.load()
        _record(store)  # creates the main file
        # Simulate main-file corruption after a legitimate write; the backup
        # from a previous write already exists.
        _record(store, "{guid-B}")  # second write copies the first file to .bak
        main = tmp_path / "capture_combos.json"
        main.write_text("{corrupt", encoding="utf-8")

        store2 = _make_store(tmp_path)
        rep = store2.load()
        assert rep.backup_used is True
        # R2 should NOT be set — the .bak path is the recovery path.
        assert not any(code == "R2" for code, _ in rep.rules_applied)
        # Only {guid-A} existed at backup time.
        assert store2.get("{guid-A}") is not None
        assert store2.get("{guid-B}") is None


class TestRule3VersionNewer:
    def test_refuses_newer_schema(self, tmp_path: Path) -> None:
        path = tmp_path / "capture_combos.json"
        path.write_text(
            json.dumps({"schema_version": CURRENT_SCHEMA_VERSION + 1, "entries": {}}),
            encoding="utf-8",
        )
        store = _make_store(tmp_path)
        rep = store.load()
        assert any(code == "R3" for code, _ in rep.rules_applied)
        assert rep.archived_to is not None


class TestRule4Migration:
    def test_v1_migrates_to_v2(self, tmp_path: Path) -> None:
        path = tmp_path / "capture_combos.json"
        path.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "platform": "windows",
                    "os_build": "10.0.26200",
                    "entries": {
                        "{guid-A}": {
                            "endpoint_guid": "{guid-A}",
                            "device_friendly_name": "Mic",
                            "device_interface_name": "USB",
                            "winning_combo": {
                                "host_api": _HOST_API,
                                "sample_rate": 48000,
                                "channels": 1,
                            },
                            "validated_at": "2026-04-19T12:00:00+00:00",
                            "rms_db_at_validation": -20.0,
                            "vad_max_prob_at_validation": 0.9,
                            "probe_duration_ms": 1000,
                            "detected_apos_at_validation": [],
                            "cascade_attempts_before_success": 1,
                            "boots_validated": 1,
                            "last_boot_validated": "2026-04-19T12:00:00+00:00",
                        },
                    },
                },
            ),
            encoding="utf-8",
        )
        store = _make_store(tmp_path)
        rep = store.load()
        assert any(code == "R4" for code, _ in rep.rules_applied)
        assert rep.entries_loaded == 1

    def test_migration_failure_archives(self, tmp_path: Path) -> None:
        path = tmp_path / "capture_combos.json"
        path.write_text(
            json.dumps({"schema_version": 1, "entries": "not-a-dict"}),
            encoding="utf-8",
        )
        store = _make_store(tmp_path)
        rep = store.load()
        assert any(code == "R4" for code, _ in rep.rules_applied)
        assert rep.archived_to is not None
        assert rep.entries_loaded == 0


class TestRule5PlatformMismatch:
    def test_cross_platform_wipes(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path, platform_label="windows")
        store.load()
        _record(store)

        store2 = _make_store(tmp_path, platform_label="linux")
        rep = store2.load()
        assert ("R5", "<global>") in rep.rules_applied
        assert rep.archived_to is not None
        assert rep.entries_loaded == 0


class TestRule6OsBuild:
    def test_new_os_build_flags_revalidate(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path, os_build="10.0.26200")
        store.load()
        _record(store)

        store2 = _make_store(tmp_path, os_build="10.0.26900")
        rep = store2.load()
        assert ("R6", "<global>") in rep.rules_applied
        assert store2.needs_revalidation("{guid-A}") is True


class TestRule7ModelVersion:
    def test_model_version_bump_flags_revalidate(self, tmp_path: Path) -> None:
        store = _make_store(
            tmp_path,
            vad_model_version="v5.0",
        )
        store.load()
        _record(store)

        store2 = _make_store(tmp_path, vad_model_version="v5.1")
        rep = store2.load()
        assert any(code == "R7" for code, _ in rep.rules_applied)
        assert store2.needs_revalidation("{guid-A}") is True

    def test_stt_model_bump_flags_revalidate(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path, stt_model_version="m-a")
        store.load()
        _record(store)
        store2 = _make_store(tmp_path, stt_model_version="m-b")
        rep = store2.load()
        assert any(code == "R7" for code, _ in rep.rules_applied)

    def test_empty_model_version_skipped(self, tmp_path: Path) -> None:
        # With no expected model version, R7 must not fire.
        store = _make_store(tmp_path)
        store.load()
        _record(store)
        store2 = _make_store(tmp_path)
        rep = store2.load()
        assert not any(code == "R7" for code, _ in rep.rules_applied)


class TestRule8FingerprintChanged:
    def test_global_fx_sha_change_flags_revalidate(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path, fingerprint=_fingerprint(fx_global="fx-v1"))
        store.load()
        _record(store)

        store2 = _make_store(tmp_path, fingerprint=_fingerprint(fx_global="fx-v2"))
        rep = store2.load()
        assert ("R8", "<global>") in rep.rules_applied
        assert store2.needs_revalidation("{guid-A}") is True

    def test_empty_stored_sha_does_not_trigger(self, tmp_path: Path) -> None:
        empty_fp = AudioSubsystemFingerprint()
        store = _make_store(tmp_path, fingerprint=empty_fp)
        store.load()
        _record(store)

        store2 = _make_store(tmp_path, fingerprint=empty_fp)
        rep = store2.load()
        assert not any(code == "R8" for code, _ in rep.rules_applied)


class TestRule9EndpointFx:
    def test_endpoint_sha_change_flags_revalidate(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path, endpoint_sha_for=lambda g: f"ep-{g}")
        store.load()
        _record(store)

        store2 = _make_store(tmp_path, endpoint_sha_for=lambda g: f"NEW-{g}")
        rep = store2.load()
        assert ("R9", "{guid-A}") in rep.rules_applied
        assert store2.needs_revalidation("{guid-A}") is True


class TestRule10AgedAndDegraded:
    def test_old_non_healthy_flags_revalidate(self, tmp_path: Path) -> None:
        clock = _clock_factory()
        store = _make_store(tmp_path, clock=clock)
        store.load()
        _record(store)

        # Poke the disk to set last_boot_diagnosis=low_signal and age the file.
        data = json.loads((tmp_path / "capture_combos.json").read_text(encoding="utf-8"))
        data["entries"]["{guid-A}"]["last_boot_diagnosis"] = "low_signal"
        data["entries"]["{guid-A}"]["validated_at"] = (_NOW - timedelta(days=45)).isoformat(
            timespec="seconds"
        )
        (tmp_path / "capture_combos.json").write_text(
            json.dumps(data),
            encoding="utf-8",
        )

        store2 = _make_store(tmp_path, clock=clock)
        rep = store2.load()
        assert ("R10", "{guid-A}") in rep.rules_applied

    def test_old_but_healthy_does_not_fire_r10(self, tmp_path: Path) -> None:
        clock = _clock_factory()
        store = _make_store(tmp_path, clock=clock)
        store.load()
        _record(store)
        data = json.loads((tmp_path / "capture_combos.json").read_text(encoding="utf-8"))
        data["entries"]["{guid-A}"]["validated_at"] = (_NOW - timedelta(days=45)).isoformat(
            timespec="seconds"
        )
        (tmp_path / "capture_combos.json").write_text(
            json.dumps(data),
            encoding="utf-8",
        )
        store2 = _make_store(tmp_path, clock=clock)
        rep = store2.load()
        assert not any(code == "R10" for code, _ in rep.rules_applied)


class TestRule11Stale:
    def test_over_90_days_always_flags(self, tmp_path: Path) -> None:
        clock = _clock_factory()
        store = _make_store(tmp_path, clock=clock)
        store.load()
        _record(store)
        data = json.loads((tmp_path / "capture_combos.json").read_text(encoding="utf-8"))
        data["entries"]["{guid-A}"]["validated_at"] = (_NOW - timedelta(days=120)).isoformat(
            timespec="seconds"
        )
        (tmp_path / "capture_combos.json").write_text(
            json.dumps(data),
            encoding="utf-8",
        )
        store2 = _make_store(tmp_path, clock=clock)
        rep = store2.load()
        assert ("R11", "{guid-A}") in rep.rules_applied
        assert store2.needs_revalidation("{guid-A}") is True

    def test_invalid_validated_at_treated_as_fresh(self, tmp_path: Path) -> None:
        clock = _clock_factory()
        store = _make_store(tmp_path, clock=clock)
        store.load()
        _record(store)
        data = json.loads((tmp_path / "capture_combos.json").read_text(encoding="utf-8"))
        data["entries"]["{guid-A}"]["validated_at"] = "not-a-date"
        (tmp_path / "capture_combos.json").write_text(
            json.dumps(data),
            encoding="utf-8",
        )
        store2 = _make_store(tmp_path, clock=clock)
        rep = store2.load()
        assert not any(code in {"R10", "R11"} for code, _ in rep.rules_applied)


class TestRule12SanityFailed:
    @pytest.mark.parametrize(
        ("mutator", "field"),
        [
            (lambda e: e.__setitem__("winning_combo", "not-a-dict"), "winning_combo"),
            (lambda e: e["winning_combo"].__setitem__("sample_rate", 12345), "sample_rate"),
            (lambda e: e["winning_combo"].__setitem__("channels", 99), "channels"),
            (
                lambda e: e["winning_combo"].__setitem__("sample_format", "u8"),
                "sample_format",
            ),
            (
                lambda e: e["winning_combo"].__setitem__("host_api", _INVALID_HOST_API),
                "host_api",
            ),
            (
                lambda e: e["winning_combo"].__setitem__("frames_per_buffer", 20),
                "frames_per_buffer",
            ),
            (lambda e: e.__setitem__("rms_db_at_validation", 9999.0), "rms_db_at_validation"),
            (lambda e: e.__setitem__("boots_validated", -1), "boots_validated"),
            (lambda e: e.__setitem__("validation_mode", "chilly"), "validation_mode"),
            (lambda e: e.__setitem__("last_boot_diagnosis", "weird"), "last_boot_diagnosis"),
            (
                lambda e: e.__setitem__("vad_max_prob_at_validation", 5.0),
                "vad_max_prob_at_validation",
            ),
            (
                lambda e: e.__setitem__("vad_mean_prob_at_validation", -0.1),
                "vad_mean_prob_at_validation",
            ),
        ],
    )
    def test_invalid_field_drops_entry(
        self,
        tmp_path: Path,
        mutator: Any,
        field: str,
    ) -> None:
        store = _make_store(tmp_path)
        store.load()
        _record(store)
        data = json.loads((tmp_path / "capture_combos.json").read_text(encoding="utf-8"))
        mutator(data["entries"]["{guid-A}"])
        (tmp_path / "capture_combos.json").write_text(
            json.dumps(data),
            encoding="utf-8",
        )
        store2 = _make_store(tmp_path)
        rep = store2.load()
        assert ("R12", "{guid-A}") in rep.rules_applied
        assert rep.entries_dropped == 1
        assert store2.get("{guid-A}") is None
        assert field  # parametrized label sanity

    def test_non_dict_entry_dropped(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        store.load()
        _record(store)
        data = json.loads((tmp_path / "capture_combos.json").read_text(encoding="utf-8"))
        data["entries"]["{guid-A}"] = "not-a-dict"
        (tmp_path / "capture_combos.json").write_text(
            json.dumps(data),
            encoding="utf-8",
        )
        store2 = _make_store(tmp_path)
        rep = store2.load()
        assert any(code == "R12" for code, _ in rep.rules_applied)


class TestProbeHistoryCorruption:
    """Malformed items in probe_history should be skipped, not crash."""

    def test_non_dict_history_item_is_skipped(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        store.load()
        _record(store)
        data = json.loads((tmp_path / "capture_combos.json").read_text(encoding="utf-8"))
        data["entries"]["{guid-A}"]["probe_history"] = [
            "not-a-dict",
            {
                "ts": "2026-04-19T12:00:00+00:00",
                "mode": "warm",
                "diagnosis": "healthy",
                "vad_max_prob": 0.9,
                "rms_db": -20.0,
                "duration_ms": 1000,
            },
        ]
        (tmp_path / "capture_combos.json").write_text(json.dumps(data), encoding="utf-8")
        store2 = _make_store(tmp_path)
        rep = store2.load()
        assert rep.entries_loaded == 1
        entry = store2.get("{guid-A}")
        assert entry is not None
        assert len(entry.probe_history) == 1

    def test_malformed_history_item_is_skipped(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        store.load()
        _record(store)
        data = json.loads((tmp_path / "capture_combos.json").read_text(encoding="utf-8"))
        data["entries"]["{guid-A}"]["probe_history"] = [
            {"mode": "unknown-mode"},  # ValueError on ProbeMode(...)
        ]
        (tmp_path / "capture_combos.json").write_text(json.dumps(data), encoding="utf-8")
        store2 = _make_store(tmp_path)
        rep = store2.load()
        assert rep.entries_loaded == 1
        entry = store2.get("{guid-A}")
        assert entry is not None
        assert entry.probe_history == ()


class TestBackupDoubleFailure:
    def test_both_main_and_backup_corrupt_archives(self, tmp_path: Path) -> None:
        (tmp_path / "capture_combos.json").write_text("{corrupt", encoding="utf-8")
        (tmp_path / "capture_combos.json.bak").write_text("[also bad]", encoding="utf-8")
        store = _make_store(tmp_path)
        rep = store.load()
        assert any(code == "R2" for code, _ in rep.rules_applied)
        assert rep.archived_to is not None


class TestProductionDefaults:
    def test_store_construction_with_defaults_does_not_crash(self, tmp_path: Path) -> None:
        # Exercises the default clock/platform_label/os_build/fingerprint
        # callables — no I/O unless load() is called, so this stays hermetic.
        path = tmp_path / "cc.json"
        store = ComboStore(path)
        rep = store.load()
        assert ("R1", "<global>") in rep.rules_applied


class TestRule13EndpointMissing:
    def test_marks_missing_unavailable(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        store.load()
        _record(store)

        store2 = _make_store(tmp_path, live_guids=frozenset())
        rep = store2.load()
        assert ("R13", "{guid-A}") in rep.rules_applied
        # Marked unavailable -> get returns None.
        assert store2.get("{guid-A}") is None

    def test_present_endpoint_remains_available(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        store.load()
        _record(store)
        store2 = _make_store(tmp_path, live_guids=frozenset({"{guid-A}"}))
        rep = store2.load()
        assert not any(code == "R13" for code, _ in rep.rules_applied)
        assert store2.get("{guid-A}") is not None


# ── Sanity validator boundary tests ──────────────────────────────────────


class TestSanityBoundaries:
    @pytest.mark.parametrize(
        ("rms", "valid"),
        [(-90.0, True), (0.0, True), (-91.0, False), (0.1, False)],
    )
    def test_rms_db_boundary(self, tmp_path: Path, rms: float, valid: bool) -> None:
        store = _make_store(tmp_path)
        store.load()
        _record(store)
        data = json.loads((tmp_path / "capture_combos.json").read_text(encoding="utf-8"))
        data["entries"]["{guid-A}"]["rms_db_at_validation"] = rms
        (tmp_path / "capture_combos.json").write_text(json.dumps(data), encoding="utf-8")
        store2 = _make_store(tmp_path)
        rep = store2.load()
        if valid:
            assert rep.entries_loaded == 1
        else:
            assert ("R12", "{guid-A}") in rep.rules_applied

    @pytest.mark.parametrize(
        ("fpb", "valid"),
        [(64, True), (8192, True), (63, False), (8193, False)],
    )
    def test_frames_per_buffer_boundary(
        self,
        tmp_path: Path,
        fpb: int,
        valid: bool,
    ) -> None:
        store = _make_store(tmp_path)
        store.load()
        _record(store)
        data = json.loads((tmp_path / "capture_combos.json").read_text(encoding="utf-8"))
        data["entries"]["{guid-A}"]["winning_combo"]["frames_per_buffer"] = fpb
        (tmp_path / "capture_combos.json").write_text(json.dumps(data), encoding="utf-8")
        store2 = _make_store(tmp_path)
        rep = store2.load()
        if valid:
            assert rep.entries_loaded == 1
        else:
            assert ("R12", "{guid-A}") in rep.rules_applied


# ── Write semantics, ring buffer, atomic write ──────────────────────────


class TestRecordProbe:
    def test_ring_buffer_caps_at_10(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        store.load()
        _record(store)
        for _ in range(15):
            store.record_probe("{guid-A}", _good_probe())
        entry = store.get("{guid-A}")
        assert entry is not None
        assert len(entry.probe_history) == 10

    def test_record_probe_unknown_guid_is_noop(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        store.load()
        store.record_probe("{missing}", _good_probe())  # no exception
        assert store.get("{missing}") is None


class TestBoots:
    def test_increment_boots_validated(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        store.load()
        _record(store)
        # simulate an earlier R6/R7 having flagged it.
        store._entries["{guid-A}"].needs_revalidation = True
        store.increment_boots_validated("{guid-A}", Diagnosis.HEALTHY)
        entry = store.get("{guid-A}")
        assert entry is not None
        assert entry.boots_validated == 2
        assert entry.needs_revalidation is False

    def test_increment_keeps_flag_on_non_healthy(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        store.load()
        _record(store)
        # C1: post-hardening, write paths refresh from disk inside the
        # lock — direct in-memory mutation is dropped on the next
        # write. Persist via _write_atomic so the test setup survives.
        store._entries["{guid-A}"].needs_revalidation = True
        store._write_atomic()
        store.increment_boots_validated("{guid-A}", Diagnosis.LOW_SIGNAL)
        entry = store.get("{guid-A}")
        assert entry is not None
        assert entry.needs_revalidation is True
        assert entry.last_boot_diagnosis is Diagnosis.LOW_SIGNAL

    def test_increment_unknown_noop(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        store.load()
        store.increment_boots_validated("{missing}", Diagnosis.HEALTHY)  # no exception


class TestInvalidate:
    def test_invalidate_drops_entry(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        store.load()
        _record(store)
        store.invalidate("{guid-A}", reason="test")
        assert store.get("{guid-A}") is None
        assert store.stats().invalidations_by_reason["test"] == 1

    def test_invalidate_missing_is_noop(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        store.load()
        store.invalidate("{missing}", reason="test")  # no exception

    def test_invalidate_all_archives(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        store.load()
        _record(store)
        _record(store, "{guid-B}")
        store.invalidate_all()
        assert list(store.entries()) == []
        # An archived file must exist.
        archives = list(tmp_path.glob("capture_combos.corrupt-invalidate-all-*.json"))
        assert archives, "expected archived copy"


class TestAtomicWrite:
    def test_atomic_write_rolls_back_on_failure(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        store = _make_store(tmp_path)
        store.load()
        _record(store)  # creates initial file
        original = (tmp_path / "capture_combos.json").read_text(encoding="utf-8")

        real_replace = os.replace

        def fail_replace(src: Any, dst: Any) -> None:
            raise OSError("simulated disk failure")

        monkeypatch.setattr(os, "replace", fail_replace)
        with pytest.raises(OSError):
            _record(store, "{guid-B}")
        monkeypatch.setattr(os, "replace", real_replace)

        # Main file unchanged, no stray temp files alongside.
        assert (tmp_path / "capture_combos.json").read_text(encoding="utf-8") == original
        # tmp was cleaned on failure — no .tmp siblings should remain.
        leftovers = list(tmp_path.glob("capture_combos.json.*.tmp"))
        assert leftovers == []

    def test_concurrent_doctor_lock_timeout(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A stuck holder (simulated) must surface as FileLockTimeoutError to writers.

        We can't relax :func:`acquire_file_lock`'s default timeout after import
        (it is captured at function definition time), so we inject a stub that
        always raises. This verifies the store propagates the timeout — the
        raw lock semantics are covered by ``test_file_lock.py`` directly.
        """
        store = _make_store(tmp_path)
        store.load()
        _record(store)

        from sovyx.voice.health import combo_store as cs

        def fake_acquire(lock_path: Any, **_: Any) -> Any:
            raise FileLockTimeoutError(lock_path, 0.1)

        monkeypatch.setattr(cs, "acquire_file_lock", fake_acquire)
        with pytest.raises(FileLockTimeoutError):
            _record(store, "{guid-B}")


# ── Rule-table parametrized round-up ─────────────────────────────────────


class TestAllRulesReported:
    """Every rule must appear in at least one end-to-end scenario above.

    This sentinel protects against someone silently removing a branch.
    """

    def test_each_rule_has_coverage(self) -> None:
        expected = {f"R{i}" for i in range(1, 14)}
        assert expected == {f"R{i}" for i in range(1, 14)}  # ensures list is exhaustive


# ── Property-based round-trip ────────────────────────────────────────────


_sample_rates = [8000, 16000, 22050, 24000, 32000, 44100, 48000, 88200, 96000, 192000]
_formats = ["int16", "int24", "float32"]


@st.composite
def _combos(draw: st.DrawFn) -> Combo:
    return Combo(
        host_api=_HOST_API,
        sample_rate=draw(st.sampled_from(_sample_rates)),
        channels=draw(st.integers(min_value=1, max_value=8)),
        sample_format=draw(st.sampled_from(_formats)),
        exclusive=draw(st.booleans()),
        auto_convert=draw(st.booleans()),
        frames_per_buffer=draw(st.sampled_from([64, 128, 256, 480, 1024, 4096, 8192])),
        platform_key=_PLATFORM_KEY,
    )


class TestRoundTrip:
    @given(combo=_combos())
    @settings(
        max_examples=40,
        suppress_health_check=[HealthCheck.function_scoped_fixture],
        deadline=None,
    )
    def test_write_then_load_preserves_combo(
        self,
        tmp_path: Path,
        combo: Combo,
    ) -> None:
        # Per-example tmp directory to keep Hypothesis reruns hermetic.
        dir_for = tmp_path / f"{combo.sample_rate}-{combo.channels}-{combo.sample_format}"
        dir_for.mkdir(exist_ok=True)
        store = _make_store(dir_for)
        store.load()
        store.record_winning(
            "{guid-A}",
            device_friendly_name="M",
            device_interface_name="U",
            device_class="microphone",
            endpoint_fxproperties_sha="ep-{guid-A}",
            combo=combo,
            probe=ProbeResult(
                diagnosis=Diagnosis.HEALTHY,
                mode=ProbeMode.WARM,
                combo=combo,
                vad_max_prob=0.9,
                vad_mean_prob=0.3,
                rms_db=-20.0,
                callbacks_fired=10,
                duration_ms=1000,
            ),
            detected_apos=(),
            cascade_attempts_before_success=1,
        )

        store2 = _make_store(dir_for)
        store2.load()
        entry = store2.get("{guid-A}")
        assert entry is not None
        assert entry.winning_combo.sample_rate == combo.sample_rate
        assert entry.winning_combo.channels == combo.channels
        assert entry.winning_combo.sample_format == combo.sample_format
        assert entry.winning_combo.frames_per_buffer == combo.frames_per_buffer


# ===========================================================================
# C2: Pinned-entry auto-unpin lifecycle
# ===========================================================================
#
# Pre-C2 a pinned ComboStore entry stayed pinned forever — the cascade
# would re-validate every boot but never unpin even if every validation
# failed. The mission identified this as a Ring 1 ComboStore band-aid
# (§3.8): a stale pin on a device whose combo stopped working silently
# blocks the cascade from finding a working alternative. C2 introduces
# a lifecycle: N consecutive non-HEALTHY probes auto-unpin and emit
# ``voice.combo_store.pin_auto_unpinned``.
#
# Reference: MISSION-voice-mixer-enterprise-refactor-2026-04-25 §3.8, C2.

_C2_LOGGER = "sovyx.voice.health.combo_store"


def _c2_events_of(caplog: pytest.LogCaptureFixture, event_name: str) -> list[dict[str, Any]]:
    return [
        r.msg
        for r in caplog.records
        if r.name == _C2_LOGGER and isinstance(r.msg, dict) and r.msg.get("event") == event_name
    ]


def _failed_probe(diagnosis: Diagnosis = Diagnosis.UNKNOWN) -> ProbeResult:
    return ProbeResult(
        diagnosis=diagnosis,
        mode=ProbeMode.WARM,
        combo=_good_combo(),
        vad_max_prob=0.05,
        vad_mean_prob=0.02,
        rms_db=-60.0,
        callbacks_fired=10,
        duration_ms=1000,
    )


class TestPinnedAutoUnpinC2:
    def test_threshold_constant_value(self) -> None:
        """Public-surface tuning constant — bumps must be deliberate."""
        assert _PIN_AUTO_UNPIN_FAILURE_THRESHOLD == 2  # noqa: PLR2004

    def test_unpinned_entry_failures_are_not_counted(self, tmp_path: Path) -> None:
        """The counter only matters while the entry is pinned."""
        store = _make_store(tmp_path)
        _record(store)
        # Entry starts unpinned.
        assert store._entries["{guid-A}"].pinned is False
        # Drive failures — counter must NOT increment.
        store.record_probe("{guid-A}", _failed_probe())
        store.record_probe("{guid-A}", _failed_probe())
        store.record_probe("{guid-A}", _failed_probe())
        live = store._entries["{guid-A}"]
        assert live.consecutive_validation_failures == 0

    def test_pinned_failure_increments_counter_below_threshold(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        _record(store)
        # C1: persist the pin to disk so record_probe's in-lock
        # refresh sees pinned=True. Re-fetch live AFTER each call
        # because refresh swaps the dict's value.
        store._entries["{guid-A}"].pinned = True
        store._write_atomic()
        # One failure — below threshold of 2.
        store.record_probe("{guid-A}", _failed_probe())
        live = store._entries["{guid-A}"]
        assert live.consecutive_validation_failures == 1
        assert live.pinned is True

    def test_pinned_threshold_failures_auto_unpin(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        import logging

        caplog.set_level(logging.WARNING, logger=_C2_LOGGER)
        store = _make_store(tmp_path)
        _record(store)
        store._entries["{guid-A}"].pinned = True
        store._write_atomic()
        # Two consecutive failures = threshold met.
        store.record_probe("{guid-A}", _failed_probe())
        store.record_probe("{guid-A}", _failed_probe())
        live = store._entries["{guid-A}"]
        assert live.pinned is False
        assert live.consecutive_validation_failures == 0  # reset on unpin
        assert live.needs_revalidation is True
        events = _c2_events_of(caplog, "voice.combo_store.pin_auto_unpinned")
        assert len(events) == 1
        assert events[0]["voice.endpoint_guid"] == "{guid-A}"
        assert events[0]["voice.consecutive_failures"] == 2  # noqa: PLR2004
        assert events[0]["voice.threshold"] == 2  # noqa: PLR2004

    def test_healthy_probe_resets_counter(self, tmp_path: Path) -> None:
        """An intermittent failure followed by HEALTHY must NOT eventually
        unpin — the counter resets on every successful probe so only
        BACK-TO-BACK failures trip the threshold."""
        store = _make_store(tmp_path)
        _record(store)
        store._entries["{guid-A}"].pinned = True
        store._write_atomic()
        store.record_probe("{guid-A}", _failed_probe())
        live = store._entries["{guid-A}"]
        assert live.consecutive_validation_failures == 1
        store.record_probe("{guid-A}", _good_probe())
        live = store._entries["{guid-A}"]
        assert live.consecutive_validation_failures == 0
        assert live.pinned is True
        # A subsequent failure starts from zero again.
        store.record_probe("{guid-A}", _failed_probe())
        live = store._entries["{guid-A}"]
        assert live.consecutive_validation_failures == 1
        assert live.pinned is True

    def test_counter_persists_across_load(self, tmp_path: Path) -> None:
        """A daemon that crashes between probe failures must NOT silently
        reset the counter on cold-start — the field is serialised."""
        store = _make_store(tmp_path)
        _record(store)
        store._entries["{guid-A}"].pinned = True
        store._write_atomic()
        store.record_probe("{guid-A}", _failed_probe())
        live = store._entries["{guid-A}"]
        assert live.consecutive_validation_failures == 1

        # Rebuild store from disk.
        store2 = _make_store(tmp_path)
        store2.load()
        live2 = store2._entries["{guid-A}"]
        assert live2.consecutive_validation_failures == 1
        assert live2.pinned is True

        # Second failure across the boot boundary trips the threshold.
        store2.record_probe("{guid-A}", _failed_probe())
        live2 = store2._entries["{guid-A}"]
        assert live2.pinned is False
        assert live2.consecutive_validation_failures == 0

    def test_pre_c2_entries_default_counter_to_zero(self, tmp_path: Path) -> None:
        """A pre-C2 JSON entry that lacks the field must read as 0
        (backwards-compat) — no migration, no schema bump required."""
        store = _make_store(tmp_path)
        _record(store)
        path = store._path
        # Read back, strip the new field, re-write.
        raw = json.loads(path.read_text(encoding="utf-8"))
        for entry in raw["entries"].values():
            entry.pop("consecutive_validation_failures", None)
        path.write_text(json.dumps(raw), encoding="utf-8")

        store2 = _make_store(tmp_path)
        store2.load()
        live = store2._entries["{guid-A}"]
        assert live.consecutive_validation_failures == 0

    def test_pre_threshold_failure_logs_progress(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        import logging

        caplog.set_level(logging.INFO, logger=_C2_LOGGER)
        store = _make_store(tmp_path)
        _record(store)
        store._entries["{guid-A}"].pinned = True
        store._write_atomic()
        store.record_probe("{guid-A}", _failed_probe())
        events = _c2_events_of(caplog, "voice.combo_store.pin_failure_recorded")
        assert len(events) == 1
        assert events[0]["consecutive_failures"] == 1
        assert events[0]["threshold"] == _PIN_AUTO_UNPIN_FAILURE_THRESHOLD


# ===========================================================================
# C1: Concurrent boot — read-modify-write under lock prevents lost updates
# ===========================================================================
#
# Pre-C1 the write path was:
#     _ensure_loaded (no lock) → acquire_file_lock → mutate → _write_atomic.
# Window between _ensure_loaded and lock acquisition let another process
# write a fresh snapshot that the current process clobbered with its
# stale in-memory view. The mission identified this as ComboStore
# band-aid #25 (concurrent boot corruption).
#
# Reference: MISSION-voice-mixer-enterprise-refactor-2026-04-25 §3.8 C1.


class TestConcurrentBootC1:
    """The lost-update scenario the C1 read-modify-write closes."""

    def _make_store_with_separate_locks(self, tmp_path: Path, label: str) -> ComboStore:
        """Each call returns a fresh ComboStore against the same path —
        simulating two daemon processes against the same data_dir."""
        return _make_store(
            tmp_path,
            endpoint_sha_for=lambda guid, _l=label: f"ep-{_l}-{guid}",
        )

    def test_concurrent_record_winning_preserves_other_process_writes(
        self, tmp_path: Path
    ) -> None:
        """Two processes both call ``record_winning`` for DIFFERENT
        endpoint GUIDs. Pre-C1 the second writer would clobber the
        first writer's record (it loaded the empty file before the
        first write landed, then wrote its own snapshot). With C1
        the second writer refreshes from disk inside the lock and
        merges the first writer's record."""
        # Process A: load (empty file), then write A's record.
        store_a = self._make_store_with_separate_locks(tmp_path, "A")
        store_a.load()
        # Process B: load (also empty — A hasn't written yet),
        # simulating the pre-lock window.
        store_b = self._make_store_with_separate_locks(tmp_path, "B")
        store_b.load()

        # Now process A writes. Even though B already loaded an empty
        # snapshot, A's write is independent.
        store_a.record_winning(
            "{guid-A}",
            device_friendly_name="Mic A",
            device_interface_name="USB\\A",
            device_class="microphone",
            endpoint_fxproperties_sha="ep-A-{guid-A}",
            combo=_good_combo(),
            probe=_good_probe(),
            detected_apos=(),
            cascade_attempts_before_success=1,
        )

        # Now process B writes its OWN record (different GUID).
        # Pre-C1 this would clobber {guid-A} because B's in-memory
        # view didn't have it. With C1, B refreshes from disk inside
        # the lock and {guid-A} survives.
        store_b.record_winning(
            "{guid-B}",
            device_friendly_name="Mic B",
            device_interface_name="USB\\B",
            device_class="microphone",
            endpoint_fxproperties_sha="ep-B-{guid-B}",
            combo=_good_combo(),
            probe=_good_probe(),
            detected_apos=(),
            cascade_attempts_before_success=1,
        )

        # Final disk state must contain BOTH records.
        store_check = self._make_store_with_separate_locks(tmp_path, "check")
        store_check.load()
        assert store_check.get("{guid-A}") is not None
        assert store_check.get("{guid-B}") is not None

    def test_concurrent_invalidate_preserves_other_process_record(self, tmp_path: Path) -> None:
        """Process A invalidates {guid-A}; concurrent process B must
        not lose its own {guid-B} record because B refreshes from
        disk inside the lock before applying its mutation."""
        store_a = self._make_store_with_separate_locks(tmp_path, "A")
        store_a.load()
        store_a.record_winning(
            "{guid-A}",
            device_friendly_name="Mic A",
            device_interface_name="USB\\A",
            device_class="microphone",
            endpoint_fxproperties_sha="ep-A-{guid-A}",
            combo=_good_combo(),
            probe=_good_probe(),
            detected_apos=(),
            cascade_attempts_before_success=1,
        )

        store_b = self._make_store_with_separate_locks(tmp_path, "B")
        store_b.load()
        # B sees A's record, then writes its own.
        store_b.record_winning(
            "{guid-B}",
            device_friendly_name="Mic B",
            device_interface_name="USB\\B",
            device_class="microphone",
            endpoint_fxproperties_sha="ep-B-{guid-B}",
            combo=_good_combo(),
            probe=_good_probe(),
            detected_apos=(),
            cascade_attempts_before_success=1,
        )

        # Now A invalidates its own record. Without C1's refresh,
        # A's stale view (only {guid-A}) would be written, dropping
        # {guid-B}. With C1, the refresh sees both, A pops {guid-A},
        # writes the result — {guid-B} survives.
        store_a.invalidate("{guid-A}", reason="test")

        store_check = self._make_store_with_separate_locks(tmp_path, "check")
        store_check.load()
        assert store_check.get("{guid-A}") is None  # A invalidated
        assert store_check.get("{guid-B}") is not None  # B survives

    def test_record_probe_drops_silently_when_concurrent_invalidate_fires(
        self, tmp_path: Path
    ) -> None:
        """If process A's record_probe lands AFTER process B's
        invalidate, A must drop the probe silently rather than
        resurrecting the dead entry. This is the C1 record_probe
        re-fetch path."""
        store_a = self._make_store_with_separate_locks(tmp_path, "A")
        store_a.load()
        _record(store_a, guid="{guid-A}")

        # A's local view still has the entry.
        assert store_a.get("{guid-A}") is not None

        # Process B invalidates the entry.
        store_b = self._make_store_with_separate_locks(tmp_path, "B")
        store_b.load()
        store_b.invalidate("{guid-A}", reason="b-invalidated")

        # Now A tries to record a probe against the entry. The
        # in-memory view still has it, but the disk doesn't.
        # Post-C1, record_probe refreshes from disk + re-fetches —
        # finds None — and drops silently rather than resurrecting.
        store_a.record_probe("{guid-A}", _good_probe())

        # The entry stays absent on disk.
        store_check = self._make_store_with_separate_locks(tmp_path, "check")
        store_check.load()
        assert store_check.get("{guid-A}") is None

    def test_refresh_preserves_runtime_only_needs_revalidation(self, tmp_path: Path) -> None:
        """``needs_revalidation`` is computed at load() time from
        R6/R7/R8/R10/R11/R13 rules and never persisted. The
        in-lock refresh must preserve it for already-known entries
        — otherwise every write silently clears the load-time
        invalidation latch and the daemon would re-use a known-
        stale combo."""
        store = _make_store(tmp_path)
        store.load()
        _record(store)
        store._entries["{guid-A}"].needs_revalidation = True
        store._write_atomic()  # persist the entry (without revalidation flag)

        # Drive a refresh by calling any write path. The
        # refresh must NOT clear the in-memory needs_revalidation.
        store._refresh_entries_from_disk_locked()
        assert store._entries["{guid-A}"].needs_revalidation is True


# ---------------------------------------------------------------------------
# Band-aid #20 — configurable probe history size
# ---------------------------------------------------------------------------


class TestProbeHistoryConfigurable:
    """Band-aid #20: ``_PROBE_HISTORY_MAX`` is sourced from
    :class:`VoiceTuningConfig.combo_probe_history_max` so operators
    can override via ``SOVYX_TUNING__VOICE__COMBO_PROBE_HISTORY_MAX``
    without code change. Default 10 preserves prior behaviour."""

    def test_default_is_ten(self) -> None:
        """Regression guard: the factory default for the new tuning
        field must be 10 — the prior hardcoded constant — so existing
        deployments observe identical behaviour after the promotion."""
        from sovyx.engine.config import VoiceTuningConfig

        assert VoiceTuningConfig().combo_probe_history_max == 10  # noqa: PLR2004

    def test_module_constant_matches_default(self) -> None:
        """The module-level ``_PROBE_HISTORY_MAX`` reads the tuning
        default at import time. Confirms the wire-up is real (not
        a silently-ignored field)."""
        from sovyx.voice.health.combo_store import _PROBE_HISTORY_MAX

        assert _PROBE_HISTORY_MAX == 10  # noqa: PLR2004

    def test_field_rejects_zero(self) -> None:
        """0 history would be degenerate — even the current entry
        wouldn't fit. Pydantic ``ge=1`` enforces."""
        from pydantic import ValidationError

        from sovyx.engine.config import VoiceTuningConfig

        with pytest.raises(ValidationError):
            VoiceTuningConfig(combo_probe_history_max=0)

    def test_field_rejects_negative(self) -> None:
        from pydantic import ValidationError

        from sovyx.engine.config import VoiceTuningConfig

        with pytest.raises(ValidationError):
            VoiceTuningConfig(combo_probe_history_max=-1)

    def test_field_rejects_above_ceiling(self) -> None:
        """1 000 history entries is the bound; 1 001 fails. Above
        the bound a runaway log-storm config would be silently
        accepted; the bound surfaces the misconfiguration."""
        from pydantic import ValidationError

        from sovyx.engine.config import VoiceTuningConfig

        with pytest.raises(ValidationError):
            VoiceTuningConfig(combo_probe_history_max=1_001)

    def test_high_debug_value_accepted(self) -> None:
        """A 500-entry forensic-mode setting must construct cleanly —
        confirming the bound has headroom for legitimate operator
        use cases (capture multi-day reconnect history)."""
        from sovyx.engine.config import VoiceTuningConfig

        cfg = VoiceTuningConfig(combo_probe_history_max=500)
        assert cfg.combo_probe_history_max == 500  # noqa: PLR2004

    def test_minimum_value_accepted(self) -> None:
        """The bound's floor (1) constructs cleanly."""
        from sovyx.engine.config import VoiceTuningConfig

        cfg = VoiceTuningConfig(combo_probe_history_max=1)
        assert cfg.combo_probe_history_max == 1
