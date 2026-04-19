"""Unit tests for :mod:`sovyx.voice.health._telemetry`.

Covers the opt-in gate, anonymized bucket aggregation, atomic JSON
flush, snapshot shape, and the module-level singleton accessor.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from sovyx.voice.health._telemetry import (
    CascadeOutcomeBucket,
    CascadeOutcomeKey,
    VoiceHealthTelemetry,
    build_telemetry_from_config,
    get_telemetry,
    record_cascade_outcome,
    set_telemetry,
)

if TYPE_CHECKING:
    from collections.abc import Iterator


@pytest.fixture()
def output_path(tmp_path: Path) -> Path:
    return tmp_path / "voice_health_telemetry.json"


@pytest.fixture(autouse=True)
def _clear_singleton() -> Iterator[None]:
    """Every test starts and ends with no installed recorder."""
    set_telemetry(None)
    yield
    set_telemetry(None)


# ---------------------------------------------------------------------------
# Opt-in gate
# ---------------------------------------------------------------------------


class TestOptInGate:
    def test_disabled_recorder_records_nothing(self, output_path: Path) -> None:
        rec = VoiceHealthTelemetry(enabled=False, output_path=output_path)
        rec.record_cascade_outcome(platform="win32", host_api="WASAPI", success=True)
        snap = rec.snapshot()
        assert snap["buckets"] == []

    def test_disabled_recorder_does_not_touch_disk(self, output_path: Path) -> None:
        rec = VoiceHealthTelemetry(enabled=False, output_path=output_path)
        rec.record_cascade_outcome(platform="win32", host_api="WASAPI", success=True)
        assert rec.flush() is False
        assert not output_path.exists()

    def test_enabled_records_and_persists(self, output_path: Path) -> None:
        rec = VoiceHealthTelemetry(enabled=True, output_path=output_path)
        rec.record_cascade_outcome(platform="win32", host_api="WASAPI", success=True)
        rec.record_cascade_outcome(platform="win32", host_api="WASAPI", success=False)
        assert rec.flush() is True
        assert output_path.exists()
        payload = json.loads(output_path.read_text(encoding="utf-8"))
        assert payload["schema_version"] == 1
        assert len(payload["buckets"]) == 1
        bucket = payload["buckets"][0]
        assert bucket == {
            "platform": "win32",
            "host_api": "WASAPI",
            "success": 1,
            "failure": 1,
            "total": 2,
            "success_rate": 0.5,
        }


# ---------------------------------------------------------------------------
# Anonymisation guarantee
# ---------------------------------------------------------------------------


class TestAnonymisation:
    def test_no_identifying_fields_in_snapshot(self, output_path: Path) -> None:
        rec = VoiceHealthTelemetry(enabled=True, output_path=output_path)
        rec.record_cascade_outcome(platform="darwin", host_api="CoreAudio", success=True)
        payload = rec.snapshot()
        forbidden = {"endpoint_id", "device_name", "address", "fingerprint", "user", "host"}
        for bucket in payload["buckets"]:
            assert not (forbidden & set(bucket.keys()))

    def test_unknown_platform_normalized(self, output_path: Path) -> None:
        rec = VoiceHealthTelemetry(enabled=True, output_path=output_path)
        rec.record_cascade_outcome(platform="", host_api=None, success=True)
        bucket = rec.snapshot()["buckets"][0]
        assert bucket["platform"] == "unknown"
        assert bucket["host_api"] == "unknown"


# ---------------------------------------------------------------------------
# Bucket aggregation
# ---------------------------------------------------------------------------


class TestBucketAggregation:
    def test_distinct_keys_separate_buckets(self, output_path: Path) -> None:
        rec = VoiceHealthTelemetry(enabled=True, output_path=output_path)
        rec.record_cascade_outcome(platform="win32", host_api="WASAPI", success=True)
        rec.record_cascade_outcome(platform="win32", host_api="WDM-KS", success=True)
        rec.record_cascade_outcome(platform="linux", host_api="ALSA", success=False)
        snap = rec.snapshot()
        assert len(snap["buckets"]) == 3

    def test_buckets_sorted_for_stable_output(self, output_path: Path) -> None:
        rec = VoiceHealthTelemetry(enabled=True, output_path=output_path)
        rec.record_cascade_outcome(platform="win32", host_api="WDM-KS", success=True)
        rec.record_cascade_outcome(platform="darwin", host_api="CoreAudio", success=True)
        rec.record_cascade_outcome(platform="linux", host_api="ALSA", success=True)
        snap = rec.snapshot()
        ordered = [(b["platform"], b["host_api"]) for b in snap["buckets"]]
        assert ordered == sorted(ordered)

    def test_success_rate_zero_total_safe(self) -> None:
        bucket = CascadeOutcomeBucket()
        assert bucket.success_rate() == 0.0
        assert bucket.total() == 0

    def test_reset_clears_buckets(self, output_path: Path) -> None:
        rec = VoiceHealthTelemetry(enabled=True, output_path=output_path)
        rec.record_cascade_outcome(platform="win32", host_api="WASAPI", success=True)
        rec.reset()
        assert rec.snapshot()["buckets"] == []


# ---------------------------------------------------------------------------
# Atomic flush + crash safety
# ---------------------------------------------------------------------------


class TestAtomicFlush:
    def test_flush_creates_parent_directory(self, tmp_path: Path) -> None:
        target = tmp_path / "nested" / "deeper" / "telemetry.json"
        rec = VoiceHealthTelemetry(enabled=True, output_path=target)
        rec.record_cascade_outcome(platform="win32", host_api="WASAPI", success=True)
        assert rec.flush() is True
        assert target.exists()

    def test_flush_replaces_existing_file(self, output_path: Path) -> None:
        output_path.write_text("garbage", encoding="utf-8")
        rec = VoiceHealthTelemetry(enabled=True, output_path=output_path)
        rec.record_cascade_outcome(platform="win32", host_api="WASAPI", success=True)
        assert rec.flush() is True
        payload = json.loads(output_path.read_text(encoding="utf-8"))
        assert payload["buckets"][0]["success"] == 1

    def test_flush_failure_returns_false(
        self, output_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        rec = VoiceHealthTelemetry(enabled=True, output_path=output_path)
        rec.record_cascade_outcome(platform="win32", host_api="WASAPI", success=True)

        def boom(*_: object, **__: object) -> None:
            raise OSError("disk full")

        monkeypatch.setattr("os.replace", boom)
        assert rec.flush() is False


# ---------------------------------------------------------------------------
# Singleton accessor + module-level forwarder
# ---------------------------------------------------------------------------


class TestSingleton:
    def test_get_returns_none_when_uninitialised(self) -> None:
        assert get_telemetry() is None

    def test_set_and_get_roundtrip(self, output_path: Path) -> None:
        rec = VoiceHealthTelemetry(enabled=True, output_path=output_path)
        set_telemetry(rec)
        assert get_telemetry() is rec

    def test_module_forwarder_records_through_singleton(self, output_path: Path) -> None:
        rec = VoiceHealthTelemetry(enabled=True, output_path=output_path)
        set_telemetry(rec)
        record_cascade_outcome(platform="win32", host_api="WASAPI", success=True)
        assert rec.snapshot()["buckets"][0]["success"] == 1

    def test_module_forwarder_noop_when_no_singleton(self) -> None:
        record_cascade_outcome(platform="win32", host_api="WASAPI", success=True)
        # No exception, no state — that's the whole assertion.


# ---------------------------------------------------------------------------
# Config-backed factory
# ---------------------------------------------------------------------------


class TestConfigFactory:
    def test_build_uses_engine_data_dir(self, tmp_path: Path) -> None:
        from sovyx.engine.config import EngineConfig

        cfg = EngineConfig()
        cfg.database.data_dir = tmp_path
        cfg.telemetry.enabled = True
        rec = build_telemetry_from_config(cfg)
        assert rec.enabled is True
        assert rec.output_path == tmp_path / "voice_health_telemetry.json"

    def test_build_disabled_when_config_off(self, tmp_path: Path) -> None:
        from sovyx.engine.config import EngineConfig

        cfg = EngineConfig()
        cfg.database.data_dir = tmp_path
        cfg.telemetry.enabled = False
        rec = build_telemetry_from_config(cfg)
        assert rec.enabled is False


# ---------------------------------------------------------------------------
# Data-class surface
# ---------------------------------------------------------------------------


class TestDataClasses:
    def test_outcome_key_is_hashable(self) -> None:
        a = CascadeOutcomeKey(platform="win32", host_api="WASAPI")
        b = CascadeOutcomeKey(platform="win32", host_api="WASAPI")
        assert a == b
        assert hash(a) == hash(b)

    def test_bucket_success_rate_partial(self) -> None:
        bucket = CascadeOutcomeBucket(success=3, failure=1)
        assert bucket.total() == 4
        assert bucket.success_rate() == 0.75
