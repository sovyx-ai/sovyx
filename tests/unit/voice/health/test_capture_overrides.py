"""Tests for :mod:`sovyx.voice.health.capture_overrides`.

Covers load/save round-trip, atomic write + backup recovery, every drop
path in ``_build_entry``, the ``source`` allow-list on ``pin``, ``unpin``
semantics, ``invalidate_all`` archive discipline, and the future-version
downgrade guard.
"""

from __future__ import annotations

import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from sovyx.voice.health.capture_overrides import (
    CURRENT_OVERRIDES_SCHEMA_VERSION,
    CaptureOverrides,
)
from sovyx.voice.health.contract import Combo, OverrideEntry

# ── Fixtures ─────────────────────────────────────────────────────────────


def _current_platform_key() -> str:
    if sys.platform.startswith("win"):
        return "win32"
    if sys.platform == "darwin":
        return "darwin"
    return "linux"


_PLATFORM_KEY = _current_platform_key()
_HOST_API = {"win32": "WASAPI", "linux": "ALSA", "darwin": "CoreAudio"}[_PLATFORM_KEY]


_NOW = datetime(2026, 4, 19, 12, 0, 0, tzinfo=UTC)


def _clock(now: datetime = _NOW) -> Any:
    state = {"t": now}

    def _now_fn() -> datetime:
        return state["t"]

    def advance(delta_seconds: int) -> None:
        state["t"] = datetime.fromtimestamp(state["t"].timestamp() + delta_seconds, tz=UTC)

    _now_fn.advance = advance  # type: ignore[attr-defined]
    return _now_fn


def _combo(
    *,
    host_api: str = _HOST_API,
    sample_rate: int = 48_000,
    channels: int = 1,
    sample_format: str = "int16",
    exclusive: bool = True,
    frames_per_buffer: int = 480,
) -> Combo:
    return Combo(
        host_api=host_api,
        sample_rate=sample_rate,
        channels=channels,
        sample_format=sample_format,
        exclusive=exclusive,
        auto_convert=False,
        frames_per_buffer=frames_per_buffer,
        platform_key=_PLATFORM_KEY,
    )


def _store(tmp_path: Path, *, clock: Any | None = None) -> CaptureOverrides:
    return CaptureOverrides(tmp_path / "capture_overrides.json", clock=clock or _clock())


# ── Empty / missing ──────────────────────────────────────────────────────


class TestLoadEmpty:
    def test_missing_file_yields_empty_store(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        store.load()
        assert list(store.entries()) == []
        assert store.get("any") is None
        assert store.is_pinned("any") is False

    def test_get_without_explicit_load_works(self, tmp_path: Path) -> None:
        """Auto-load on first read path — mirrors ComboStore ergonomics."""
        store = _store(tmp_path)
        assert store.get("any") is None


# ── Pin / unpin ──────────────────────────────────────────────────────────


class TestPin:
    def test_pin_round_trips(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        store.pin(
            "{guid-A}",
            device_friendly_name="Razer BlackShark V2 Pro",
            combo=_combo(),
            source="user",
            reason="Manually pinned",
        )

        # Same instance (warm state).
        got = store.get("{guid-A}")
        assert got is not None
        assert got.host_api == _HOST_API
        assert got.sample_rate == 48_000
        entry = store.get_entry("{guid-A}")
        assert entry is not None
        assert entry.pinned_by == "user"
        assert entry.reason == "Manually pinned"
        assert entry.pinned_at == _NOW.isoformat(timespec="seconds")
        assert entry.device_friendly_name == "Razer BlackShark V2 Pro"

        # Cold reload from disk.
        fresh = _store(tmp_path)
        fresh.load()
        got2 = fresh.get_entry("{guid-A}")
        assert got2 is not None
        # platform_key is a construction-time validation flag, not persisted;
        # compare the load-bearing fields instead.
        assert got2.pinned_combo.host_api == got.host_api
        assert got2.pinned_combo.sample_rate == got.sample_rate
        assert got2.pinned_combo.channels == got.channels
        assert got2.pinned_combo.sample_format == got.sample_format
        assert got2.pinned_combo.exclusive == got.exclusive
        assert got2.pinned_combo.auto_convert == got.auto_convert
        assert got2.pinned_combo.frames_per_buffer == got.frames_per_buffer
        assert got2.device_friendly_name == "Razer BlackShark V2 Pro"

    def test_pin_overwrites_existing(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        store.pin(
            "{guid-A}",
            device_friendly_name="Old",
            combo=_combo(sample_rate=48_000),
            source="user",
        )
        store.pin(
            "{guid-A}",
            device_friendly_name="New",
            combo=_combo(sample_rate=16_000),
            source="wizard",
            reason="re-pinned",
        )
        entry = store.get_entry("{guid-A}")
        assert entry is not None
        assert entry.pinned_combo.sample_rate == 16_000
        assert entry.pinned_by == "wizard"
        assert entry.device_friendly_name == "New"
        assert entry.reason == "re-pinned"

    def test_pin_rejects_empty_guid(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        with pytest.raises(ValueError, match="endpoint_guid must be non-empty"):
            store.pin("", device_friendly_name="x", combo=_combo(), source="user")

    def test_pin_rejects_unknown_source(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        with pytest.raises(ValueError, match="source="):
            store.pin(
                "{guid-A}",
                device_friendly_name="x",
                combo=_combo(),
                source="api",
            )

    @pytest.mark.parametrize("source", ["user", "wizard", "cli"])
    def test_pin_accepts_allowed_sources(self, tmp_path: Path, source: str) -> None:
        store = _store(tmp_path)
        store.pin(
            "{guid-A}",
            device_friendly_name="x",
            combo=_combo(),
            source=source,
        )
        entry = store.get_entry("{guid-A}")
        assert entry is not None
        assert entry.pinned_by == source


class TestUnpin:
    def test_unpin_removes_and_persists(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        store.pin("{guid-A}", device_friendly_name="x", combo=_combo(), source="user")
        assert store.unpin("{guid-A}") is True
        assert store.get("{guid-A}") is None

        fresh = _store(tmp_path)
        fresh.load()
        assert fresh.get("{guid-A}") is None
        assert list(fresh.entries()) == []

    def test_unpin_missing_returns_false(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        assert store.unpin("{guid-A}") is False


# ── invalidate_all ───────────────────────────────────────────────────────


class TestInvalidateAll:
    def test_archives_current_and_resets(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        store.pin("{guid-A}", device_friendly_name="x", combo=_combo(), source="user")
        store.pin("{guid-B}", device_friendly_name="y", combo=_combo(), source="cli")

        store.invalidate_all()

        assert list(store.entries()) == []
        # Main file exists (written empty) and .corrupt archive was produced.
        archives = list(tmp_path.glob("capture_overrides.corrupt-invalidate-all-*.json"))
        assert len(archives) == 1
        # Archive contains the pre-reset entries, not the post-reset ones.
        archive_payload = json.loads(archives[0].read_text(encoding="utf-8"))
        assert set(archive_payload["overrides"].keys()) == {"{guid-A}", "{guid-B}"}

    def test_invalidate_all_on_empty_store_is_safe(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        store.invalidate_all()
        # No archive produced when nothing to archive.
        assert list(tmp_path.glob("*.corrupt-*.json")) == []
        assert list(store.entries()) == []


# ── Entries iteration ────────────────────────────────────────────────────


class TestEntries:
    def test_entries_sorted_by_guid(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        for guid in ["{guid-Z}", "{guid-A}", "{guid-M}"]:
            store.pin(guid, device_friendly_name=guid, combo=_combo(), source="user")
        got = [e.endpoint_guid for e in store.entries()]
        assert got == ["{guid-A}", "{guid-M}", "{guid-Z}"]


# ── load() — corrupt / future / non-dict ─────────────────────────────────


class TestLoadCorrupt:
    def test_non_json_main_is_archived(self, tmp_path: Path) -> None:
        path = tmp_path / "capture_overrides.json"
        path.write_text("this is not json", encoding="utf-8")
        store = CaptureOverrides(path, clock=_clock())
        store.load()
        assert list(store.entries()) == []
        # Archive produced.
        archives = list(tmp_path.glob("capture_overrides.corrupt-parse-error-*.json"))
        assert len(archives) == 1

    def test_json_root_not_object_is_archived(self, tmp_path: Path) -> None:
        path = tmp_path / "capture_overrides.json"
        path.write_text('["list-not-object"]', encoding="utf-8")
        store = CaptureOverrides(path, clock=_clock())
        store.load()
        archives = list(tmp_path.glob("capture_overrides.corrupt-parse-error-*.json"))
        assert len(archives) == 1
        assert list(store.entries()) == []

    def test_future_schema_version_is_archived(self, tmp_path: Path) -> None:
        path = tmp_path / "capture_overrides.json"
        path.write_text(
            json.dumps(
                {
                    "schema_version": CURRENT_OVERRIDES_SCHEMA_VERSION + 7,
                    "last_updated": "2026-04-19T00:00:00+00:00",
                    "overrides": {},
                },
            ),
            encoding="utf-8",
        )
        store = CaptureOverrides(path, clock=_clock())
        store.load()
        assert list(store.entries()) == []
        archives = list(tmp_path.glob("capture_overrides.corrupt-version-newer-*.json"))
        assert len(archives) == 1

    def test_overrides_key_not_dict_is_archived(self, tmp_path: Path) -> None:
        path = tmp_path / "capture_overrides.json"
        path.write_text(
            json.dumps({"schema_version": 1, "overrides": ["not-a-dict"]}),
            encoding="utf-8",
        )
        store = CaptureOverrides(path, clock=_clock())
        store.load()
        assert list(store.entries()) == []
        archives = list(tmp_path.glob("capture_overrides.corrupt-overrides-not-dict-*.json"))
        assert len(archives) == 1

    def test_schema_version_non_int_is_archived(self, tmp_path: Path) -> None:
        path = tmp_path / "capture_overrides.json"
        path.write_text(
            json.dumps({"schema_version": "v1", "overrides": {}}),
            encoding="utf-8",
        )
        store = CaptureOverrides(path, clock=_clock())
        store.load()
        assert list(store.entries()) == []


class TestBackupRecovery:
    def test_corrupt_main_recovers_from_bak(self, tmp_path: Path) -> None:
        path = tmp_path / "capture_overrides.json"
        bak = tmp_path / "capture_overrides.json.bak"

        # Seed a valid backup.
        good = _store(tmp_path)
        good.pin("{guid-A}", device_friendly_name="x", combo=_combo(), source="user")
        # After pin(), main exists. Copy it to .bak then corrupt main.
        bak.write_text(path.read_text(encoding="utf-8"), encoding="utf-8")
        path.write_text("corrupt", encoding="utf-8")

        fresh = CaptureOverrides(path, clock=_clock())
        fresh.load()
        # Backup rescue succeeded: the pin survives.
        entry = fresh.get_entry("{guid-A}")
        assert entry is not None
        assert entry.device_friendly_name == "x"

    def test_both_main_and_bak_corrupt_yields_empty(self, tmp_path: Path) -> None:
        path = tmp_path / "capture_overrides.json"
        bak = tmp_path / "capture_overrides.json.bak"
        path.write_text("main bad", encoding="utf-8")
        bak.write_text("bak bad too", encoding="utf-8")

        store = CaptureOverrides(path, clock=_clock())
        store.load()
        assert list(store.entries()) == []

    def test_bak_root_not_object_skipped(self, tmp_path: Path) -> None:
        path = tmp_path / "capture_overrides.json"
        bak = tmp_path / "capture_overrides.json.bak"
        path.write_text("nope", encoding="utf-8")
        bak.write_text('"just-a-string"', encoding="utf-8")

        store = CaptureOverrides(path, clock=_clock())
        store.load()
        assert list(store.entries()) == []


# ── Per-entry sanity drops ───────────────────────────────────────────────


def _write_overrides(tmp_path: Path, overrides: dict[str, Any]) -> Path:
    path = tmp_path / "capture_overrides.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "last_updated": "2026-04-19T00:00:00+00:00",
                "overrides": overrides,
            },
        ),
        encoding="utf-8",
    )
    return path


def _valid_combo_dict(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "host_api": _HOST_API,
        "sample_rate": 48_000,
        "channels": 1,
        "sample_format": "int16",
        "exclusive": True,
        "auto_convert": False,
        "frames_per_buffer": 480,
    }
    base.update(overrides)
    return base


def _valid_entry_dict(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "endpoint_guid": "{guid-A}",
        "device_friendly_name": "Test Mic",
        "pinned_combo": _valid_combo_dict(),
        "pinned_at": "2026-04-19T00:00:00+00:00",
        "pinned_by": "user",
        "reason": "",
    }
    base.update(overrides)
    return base


class TestLoadDrops:
    def test_drops_empty_guid(self, tmp_path: Path) -> None:
        path = _write_overrides(tmp_path, {"": _valid_entry_dict()})
        store = CaptureOverrides(path, clock=_clock())
        store.load()
        assert list(store.entries()) == []

    def test_drops_non_dict_entry(self, tmp_path: Path) -> None:
        path = _write_overrides(tmp_path, {"{guid-A}": "not-a-dict"})
        store = CaptureOverrides(path, clock=_clock())
        store.load()
        assert list(store.entries()) == []

    def test_drops_missing_pinned_combo(self, tmp_path: Path) -> None:
        path = _write_overrides(tmp_path, {"{guid-A}": {"pinned_by": "user"}})
        store = CaptureOverrides(path, clock=_clock())
        store.load()
        assert list(store.entries()) == []

    @pytest.mark.parametrize(
        ("field", "bad"),
        [
            ("sample_rate", 12_345),
            ("channels", 99),
            ("channels", "one"),
            ("sample_format", "mp3"),
            ("host_api", "BogusAPI"),
            ("host_api", 42),
            ("frames_per_buffer", 10),
            ("frames_per_buffer", 99_999),
            ("frames_per_buffer", "big"),
        ],
    )
    def test_drops_bad_combo_field(self, tmp_path: Path, field: str, bad: Any) -> None:
        combo = _valid_combo_dict(**{field: bad})
        path = _write_overrides(tmp_path, {"{guid-A}": _valid_entry_dict(pinned_combo=combo)})
        store = CaptureOverrides(path, clock=_clock())
        store.load()
        assert list(store.entries()) == []

    def test_drops_unknown_pinned_by(self, tmp_path: Path) -> None:
        path = _write_overrides(
            tmp_path,
            {"{guid-A}": _valid_entry_dict(pinned_by="api")},
        )
        store = CaptureOverrides(path, clock=_clock())
        store.load()
        assert list(store.entries()) == []

    def test_keeps_valid_and_drops_invalid_side_by_side(self, tmp_path: Path) -> None:
        overrides: dict[str, Any] = {
            "{guid-A}": _valid_entry_dict(),
            "{guid-B}": "junk",
            "{guid-C}": _valid_entry_dict(
                pinned_combo=_valid_combo_dict(sample_rate=12_345),
            ),
        }
        path = _write_overrides(tmp_path, overrides)
        store = CaptureOverrides(path, clock=_clock())
        store.load()
        got = [e.endpoint_guid for e in store.entries()]
        assert got == ["{guid-A}"]


# ── Persistence shape ────────────────────────────────────────────────────


class TestSerializedShape:
    def test_written_file_has_expected_top_level_keys(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        store.pin("{guid-A}", device_friendly_name="x", combo=_combo(), source="user")
        raw = json.loads((tmp_path / "capture_overrides.json").read_text(encoding="utf-8"))
        assert raw["schema_version"] == CURRENT_OVERRIDES_SCHEMA_VERSION
        assert raw["last_updated"] == _NOW.isoformat(timespec="seconds")
        assert set(raw["overrides"].keys()) == {"{guid-A}"}
        row = raw["overrides"]["{guid-A}"]
        assert row["pinned_by"] == "user"
        assert row["pinned_combo"]["sample_rate"] == 48_000
        assert row["pinned_combo"]["host_api"] == _HOST_API

    def test_write_preserves_deterministic_ordering(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        for guid in ["{guid-Z}", "{guid-A}", "{guid-M}"]:
            store.pin(guid, device_friendly_name=guid, combo=_combo(), source="user")
        raw_keys = list(
            json.loads(
                (tmp_path / "capture_overrides.json").read_text(encoding="utf-8"),
            )["overrides"],
        )
        assert raw_keys == ["{guid-A}", "{guid-M}", "{guid-Z}"]


# ── OverrideEntry contract sanity ────────────────────────────────────────


class TestOverrideEntry:
    def test_dataclass_is_frozen(self) -> None:
        entry = OverrideEntry(
            endpoint_guid="{x}",
            device_friendly_name="",
            pinned_combo=_combo(),
            pinned_at="2026-04-19T00:00:00+00:00",
            pinned_by="user",
        )
        with pytest.raises((AttributeError, TypeError)):
            entry.endpoint_guid = "{y}"  # type: ignore[misc]
