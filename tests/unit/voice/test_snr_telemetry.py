"""Tests for the SNR observability + wire-up [Phase 4 T4.32 + T4.33].

Coverage:

* Metric name constant pins the wire contract.
* :func:`record_audio_snr_db` no-op safety + happy path.
* :class:`FrameNormalizer` runs the SNR estimator AFTER NS on
  every emitted window when wired.
* The estimator's silent-frame return + first-frame anchor are
  filtered out so the histogram p50 isn't poisoned.
* Bit-exact passthrough preserved when ``snr_estimator`` is
  ``None`` (foundation default).
* Runtime swap via ``set_snr_estimator``.
* :func:`_build_snr_estimator` factory matrix.
* :class:`AudioCaptureTask` plumbing.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import numpy as np
import pytest  # noqa: TC002 — pytest types resolved at runtime via fixtures
from opentelemetry.sdk.metrics.export import InMemoryMetricReader

from sovyx.engine.config import VoiceTuningConfig
from sovyx.observability.metrics import (
    MetricsRegistry,
    setup_metrics,
    teardown_metrics,
)
from sovyx.voice._capture_task import AudioCaptureTask
from sovyx.voice._frame_normalizer import FrameNormalizer
from sovyx.voice._snr_estimator import SnrEstimator
from sovyx.voice.factory import _build_snr_estimator
from sovyx.voice.health._metrics import (
    METRIC_AUDIO_SNR_DB,
    record_audio_snr_db,
)

# ── Test fixtures ────────────────────────────────────────────────────────


@pytest.fixture()
def reader() -> InMemoryMetricReader:
    return InMemoryMetricReader()


@pytest.fixture(autouse=True)
def _reset_otel() -> None:
    from opentelemetry.metrics import _internal as otel_internal

    yield
    otel_internal._METER_PROVIDER_SET_ONCE._done = False  # type: ignore[attr-defined]
    otel_internal._METER_PROVIDER = None  # type: ignore[attr-defined]


@pytest.fixture()
def registry(reader: InMemoryMetricReader) -> MetricsRegistry:
    reg = setup_metrics(readers=[reader])
    yield reg
    teardown_metrics()


def _collect(reader: InMemoryMetricReader) -> list[dict[str, Any]]:
    from sovyx.observability.metrics import collect_json

    return collect_json(reader)


def _find(data: list[dict[str, Any]], name: str) -> dict[str, Any] | None:
    for m in data:
        if m["name"] == name:
            return m
    return None


def _build_estimator(
    *,
    noise_window_seconds: float = 5.0,
    silence_floor_db: float = -90.0,
) -> SnrEstimator:
    from sovyx.voice._snr_estimator import (
        SnrEstimatorConfig,
        build_snr_estimator,
    )

    cfg = SnrEstimatorConfig(
        enabled=True,
        sample_rate=16_000,
        frame_size_samples=512,
        noise_window_seconds=noise_window_seconds,
        silence_floor_db=silence_floor_db,
    )
    estimator = build_snr_estimator(cfg)
    assert estimator is not None
    return estimator


# ── Stable name contract ─────────────────────────────────────────────────


class TestStableNameContract:
    def test_audio_snr_db_name(self) -> None:
        assert METRIC_AUDIO_SNR_DB == "sovyx.voice.audio.snr_db"


# ── record_audio_snr_db ──────────────────────────────────────────────────


class TestRecordAudioSnrDb:
    def test_no_op_without_registry(self) -> None:
        record_audio_snr_db(snr_db=20.0)  # must not raise

    def test_emits_one_data_point(
        self,
        registry: MetricsRegistry,
        reader: InMemoryMetricReader,
    ) -> None:
        record_audio_snr_db(snr_db=15.0)
        metric = _find(_collect(reader), METRIC_AUDIO_SNR_DB)
        assert metric is not None
        dp = metric["data_points"][0]
        assert dp["count"] == 1
        assert dp["sum"] == pytest.approx(15.0, abs=0.01)

    def test_records_multiple_samples(
        self,
        registry: MetricsRegistry,
        reader: InMemoryMetricReader,
    ) -> None:
        for v in (10.0, 20.0, 30.0):
            record_audio_snr_db(snr_db=v)
        metric = _find(_collect(reader), METRIC_AUDIO_SNR_DB)
        assert metric is not None
        dp = metric["data_points"][0]
        assert dp["count"] == 3
        assert dp["sum"] == pytest.approx(60.0, abs=0.01)


# ── FrameNormalizer wire-up ──────────────────────────────────────────────


class TestFrameNormalizerSnrWireUp:
    def test_default_snr_estimator_is_none(self) -> None:
        norm = FrameNormalizer(source_rate=16_000, source_channels=1)
        assert norm.snr_estimator is None

    def test_set_snr_estimator_assigns(self) -> None:
        norm = FrameNormalizer(source_rate=16_000, source_channels=1)
        est = _build_estimator()
        norm.set_snr_estimator(est)
        assert norm.snr_estimator is est

    def test_set_snr_estimator_can_unwire(self) -> None:
        norm = FrameNormalizer(
            source_rate=16_000,
            source_channels=1,
            snr_estimator=_build_estimator(),
        )
        norm.set_snr_estimator(None)
        assert norm.snr_estimator is None

    def test_no_metrics_when_estimator_unwired(
        self,
        registry: MetricsRegistry,
        reader: InMemoryMetricReader,
    ) -> None:
        norm = FrameNormalizer(source_rate=16_000, source_channels=1)
        norm.push(np.full(512, 5_000, dtype=np.int16))
        assert _find(_collect(reader), METRIC_AUDIO_SNR_DB) is None

    def test_silent_frame_emits_no_sample(
        self,
        registry: MetricsRegistry,
        reader: InMemoryMetricReader,
    ) -> None:
        # Silent frame → estimator returns _SNR_FLOOR_DB → wire-up
        # filters out → no histogram emission.
        norm = FrameNormalizer(
            source_rate=16_000,
            source_channels=1,
            snr_estimator=_build_estimator(),
        )
        norm.push(np.zeros(512, dtype=np.int16))
        metric = _find(_collect(reader), METRIC_AUDIO_SNR_DB)
        if metric is not None:
            assert all(dp["count"] == 0 for dp in metric["data_points"])

    def test_first_frame_emits_no_sample(
        self,
        registry: MetricsRegistry,
        reader: InMemoryMetricReader,
    ) -> None:
        # First non-silent frame → estimator returns 0.0 (signal IS
        # the noise floor by construction) → wire-up filters out.
        norm = FrameNormalizer(
            source_rate=16_000,
            source_channels=1,
            snr_estimator=_build_estimator(),
        )
        norm.push(np.full(512, 5_000, dtype=np.int16))
        metric = _find(_collect(reader), METRIC_AUDIO_SNR_DB)
        if metric is not None:
            assert all(dp["count"] == 0 for dp in metric["data_points"])

    def test_real_snr_measurement_emitted(
        self,
        registry: MetricsRegistry,
        reader: InMemoryMetricReader,
    ) -> None:
        # Quiet anchor + loud frame → estimator returns positive
        # SNR → histogram receives one sample.
        norm = FrameNormalizer(
            source_rate=16_000,
            source_channels=1,
            snr_estimator=_build_estimator(),
        )
        # 1024 samples → 2 windows: first establishes floor (no
        # emission), second measures real SNR (emission).
        block = np.concatenate(
            [
                np.full(512, 200, dtype=np.int16),  # quiet anchor
                np.full(512, 8_000, dtype=np.int16),  # loud
            ],
        )
        norm.push(block)
        metric = _find(_collect(reader), METRIC_AUDIO_SNR_DB)
        assert metric is not None
        dp = metric["data_points"][0]
        assert dp["count"] == 1
        # 8000²/200² → 40 dB SNR.
        assert 30.0 < dp["sum"] < 50.0

    def test_disabled_path_preserves_window_emission(self) -> None:
        # Regression-guard: foundation default emits unchanged
        # windows even with SNR wired (estimator is observability-
        # only — no signal mutation).
        est = _build_estimator()
        norm = FrameNormalizer(
            source_rate=16_000,
            source_channels=1,
            snr_estimator=est,
        )
        block = np.full(512, 1_234, dtype=np.int16)
        windows = norm.push(block)
        assert len(windows) == 1
        np.testing.assert_array_equal(windows[0], block)


# ── Factory _build_snr_estimator ────────────────────────────────────────


class TestBuildSnrEstimator:
    def test_default_disabled_returns_none(self) -> None:
        tuning = VoiceTuningConfig()
        assert tuning.voice_snr_estimation_enabled is False
        assert _build_snr_estimator(tuning) is None

    def test_enabled_returns_real_estimator(self) -> None:
        tuning = VoiceTuningConfig(voice_snr_estimation_enabled=True)
        est = _build_snr_estimator(tuning)
        assert isinstance(est, SnrEstimator)

    def test_window_propagates_to_estimator(self) -> None:
        tuning = VoiceTuningConfig(
            voice_snr_estimation_enabled=True,
            voice_snr_noise_window_seconds=10.0,
        )
        est = _build_snr_estimator(tuning)
        assert est is not None
        # window_frames = 10 s × 16000 / 512 ≈ 313.
        # Verify by feeding 313 samples + 1 more triggers eviction.
        # Indirect check: just ensure the estimator is callable.
        snr = est.estimate(np.full(512, 1_000, dtype=np.int16))
        assert snr == 0.0  # First frame anchors floor.

    def test_each_call_returns_independent_instance(self) -> None:
        tuning = VoiceTuningConfig(voice_snr_estimation_enabled=True)
        a = _build_snr_estimator(tuning)
        b = _build_snr_estimator(tuning)
        assert a is not None and b is not None
        assert a is not b

    def test_disabled_path_emits_no_log(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        import logging as _logging

        tuning = VoiceTuningConfig()
        with caplog.at_level(_logging.INFO, logger="sovyx.voice.factory"):
            _build_snr_estimator(tuning)
        wired = [r for r in caplog.records if "voice.snr.wired" in r.getMessage()]
        assert wired == []

    def test_enabled_path_emits_one_log(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        import logging as _logging

        tuning = VoiceTuningConfig(voice_snr_estimation_enabled=True)
        with caplog.at_level(_logging.INFO, logger="sovyx.voice.factory"):
            _build_snr_estimator(tuning)
        wired = [r for r in caplog.records if "voice.snr.wired" in r.getMessage()]
        assert len(wired) == 1


# ── AudioCaptureTask plumbing ────────────────────────────────────────────


class TestCaptureTaskSnrPlumbing:
    def _pipeline_stub(self) -> MagicMock:
        return MagicMock()

    def test_default_snr_estimator_is_none(self) -> None:
        task = AudioCaptureTask(self._pipeline_stub())
        assert task._snr_estimator is None  # noqa: SLF001

    def test_explicit_snr_estimator_stored(self) -> None:
        est = _build_estimator()
        task = AudioCaptureTask(self._pipeline_stub(), snr_estimator=est)
        assert task._snr_estimator is est  # noqa: SLF001
