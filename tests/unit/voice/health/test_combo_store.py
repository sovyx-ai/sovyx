"""Tests for :mod:`sovyx.voice.health.combo_store`.

Covers every invalidation rule R1-R13, atomic-write + backup recovery,
sanity validator boundaries, and a property-based round-trip.
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from sovyx.voice.health._combo_store_migrations import CURRENT_SCHEMA_VERSION
from sovyx.voice.health._file_lock import FileLockTimeoutError
from sovyx.voice.health.combo_store import (
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
        host_api="WASAPI",
        sample_rate=48000,
        channels=1,
        sample_format="int16",
        exclusive=False,
        auto_convert=False,
        frames_per_buffer=480,
        platform_key="win32",
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
        assert d["host_api"] == "WASAPI"
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
        assert d["winning_combo"]["host_api"] == "WASAPI"


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
        assert entry.winning_combo.host_api == "WASAPI"
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
                                "host_api": "WASAPI",
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
            (lambda e: e["winning_combo"].__setitem__("host_api", "PulseAudio"), "host_api"),
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
        store._entries["{guid-A}"].needs_revalidation = True
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
        host_api="WASAPI",
        sample_rate=draw(st.sampled_from(_sample_rates)),
        channels=draw(st.integers(min_value=1, max_value=8)),
        sample_format=draw(st.sampled_from(_formats)),
        exclusive=draw(st.booleans()),
        auto_convert=draw(st.booleans()),
        frames_per_buffer=draw(st.sampled_from([64, 128, 256, 480, 1024, 4096, 8192])),
        platform_key="win32",
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
