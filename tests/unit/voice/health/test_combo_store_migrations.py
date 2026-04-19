"""Tests for :mod:`sovyx.voice.health._combo_store_migrations`."""

from __future__ import annotations

from typing import Any

import pytest

from sovyx.voice.health._combo_store_migrations import (
    CURRENT_SCHEMA_VERSION,
    MigrationError,
    migrate_to_current,
)


def _fingerprint_factory() -> dict[str, Any]:
    return {
        "windows_audio_endpoints_sha": "",
        "windows_fxproperties_global_sha": "",
        "linux_pulseaudio_config_sha": "",
        "macos_coreaudio_plugins_sha": "",
        "computed_at": "2026-04-19T00:00:00+00:00",
    }


def _endpoint_sha(_guid: str) -> str:
    return f"sha-{_guid[-4:]}"


class TestMigrateToCurrent:
    def test_already_current_is_noop(self) -> None:
        raw = {"schema_version": CURRENT_SCHEMA_VERSION, "entries": {}}
        out = migrate_to_current(
            raw,
            audio_subsystem_fingerprint_factory=_fingerprint_factory,
            endpoint_fxproperties_sha_for=_endpoint_sha,
        )
        assert out is raw

    def test_refuses_future_version(self) -> None:
        raw = {"schema_version": CURRENT_SCHEMA_VERSION + 5, "entries": {}}
        with pytest.raises(MigrationError) as exc:
            migrate_to_current(
                raw,
                audio_subsystem_fingerprint_factory=_fingerprint_factory,
                endpoint_fxproperties_sha_for=_endpoint_sha,
            )
        assert "newer than runtime" in str(exc.value)

    def test_default_version_is_one(self) -> None:
        raw: dict[str, Any] = {"entries": {}}
        out = migrate_to_current(
            raw,
            audio_subsystem_fingerprint_factory=_fingerprint_factory,
            endpoint_fxproperties_sha_for=_endpoint_sha,
        )
        assert out["schema_version"] == CURRENT_SCHEMA_VERSION


class TestV1ToV2:
    def _v1(self, **extra: Any) -> dict[str, Any]:
        base: dict[str, Any] = {
            "schema_version": 1,
            "entries": {
                "{guid-A}": {
                    "winning_combo": {
                        "host_api": "WASAPI",
                        "sample_rate": 48000,
                        "channels": 2,
                    },
                    "validated_at": "2026-01-01T00:00:00+00:00",
                },
            },
        }
        base.update(extra)
        return base

    def test_backfills_all_v2_defaults(self) -> None:
        out = migrate_to_current(
            self._v1(),
            audio_subsystem_fingerprint_factory=_fingerprint_factory,
            endpoint_fxproperties_sha_for=_endpoint_sha,
        )
        assert out["schema_version"] == CURRENT_SCHEMA_VERSION
        entry = out["entries"]["{guid-A}"]
        assert entry["device_class"] == "other"
        assert entry["validation_mode"] == "warm"
        assert entry["winning_combo"]["sample_format"] == "int16"
        assert entry["winning_combo"]["frames_per_buffer"] == 480
        assert entry["winning_combo"]["auto_convert"] is False
        assert entry["endpoint_fxproperties_sha"] == "sha-d-A}"  # last 4 chars of "{guid-A}"
        assert entry["probe_history"] == []
        assert entry["pinned"] is False
        assert entry["last_boot_diagnosis"] == "healthy"
        assert entry["last_boot_validated"] == "2026-01-01T00:00:00+00:00"
        assert "audio_subsystem_fingerprint" in out
        assert out["wake_word_model_version"] == ""
        assert out["stt_model_version"] == ""

    def test_preserves_existing_fields(self) -> None:
        raw = self._v1()
        raw["entries"]["{guid-A}"]["device_class"] = "headset"
        raw["entries"]["{guid-A}"]["winning_combo"]["sample_format"] = "float32"
        out = migrate_to_current(
            raw,
            audio_subsystem_fingerprint_factory=_fingerprint_factory,
            endpoint_fxproperties_sha_for=_endpoint_sha,
        )
        entry = out["entries"]["{guid-A}"]
        assert entry["device_class"] == "headset"
        assert entry["winning_combo"]["sample_format"] == "float32"

    def test_drops_entries_missing_combo(self) -> None:
        raw = self._v1()
        raw["entries"]["{guid-B}"] = {"validated_at": "2026-01-01T00:00:00+00:00"}
        out = migrate_to_current(
            raw,
            audio_subsystem_fingerprint_factory=_fingerprint_factory,
            endpoint_fxproperties_sha_for=_endpoint_sha,
        )
        assert "{guid-A}" in out["entries"]
        assert "{guid-B}" not in out["entries"]

    def test_drops_non_dict_entry(self) -> None:
        raw = self._v1()
        raw["entries"]["{guid-B}"] = "not-a-dict"
        out = migrate_to_current(
            raw,
            audio_subsystem_fingerprint_factory=_fingerprint_factory,
            endpoint_fxproperties_sha_for=_endpoint_sha,
        )
        assert "{guid-B}" not in out["entries"]

    def test_raises_when_entries_is_not_dict(self) -> None:
        raw = {"schema_version": 1, "entries": []}
        with pytest.raises(MigrationError) as exc:
            migrate_to_current(
                raw,
                audio_subsystem_fingerprint_factory=_fingerprint_factory,
                endpoint_fxproperties_sha_for=_endpoint_sha,
            )
        assert "not a dict" in str(exc.value)

    def test_raises_when_no_handler_for_version(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from sovyx.voice.health import _combo_store_migrations as mod

        monkeypatch.setattr(mod, "_MIGRATIONS", {})
        raw: dict[str, Any] = {"schema_version": 1, "entries": {}}
        with pytest.raises(MigrationError) as exc:
            migrate_to_current(
                raw,
                audio_subsystem_fingerprint_factory=_fingerprint_factory,
                endpoint_fxproperties_sha_for=_endpoint_sha,
            )
        assert "no migration handler" in str(exc.value)


class TestMigrationError:
    def test_is_runtime_error(self) -> None:
        assert issubclass(MigrationError, RuntimeError)
