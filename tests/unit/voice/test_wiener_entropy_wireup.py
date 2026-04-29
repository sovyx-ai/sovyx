"""Tests for the Wiener entropy wire-up [Phase 4 T4.44.b].

Coverage:

* :data:`METRIC_AUDIO_SIGNAL_DESTROYED` name pin.
* :func:`record_audio_signal_destroyed` no-op safety + happy path.
* :class:`FrameNormalizer` accepts the entropy kwargs and emits
  ``voice.audio.signal_destroyed{state}`` once per push.
* The ``wiener_entropy_check_enabled=False`` path is bit-exact
  to the pre-T4.44.b conversion (regression-guard).
* Destroyed-vs-clean classification on synthetic signals.
* :class:`AudioCaptureTask` plumbing for the entropy flags.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import numpy as np
import pytest  # noqa: TC002 — pytest types resolved at runtime via fixtures
from opentelemetry.sdk.metrics.export import InMemoryMetricReader

from sovyx.observability.metrics import (
    MetricsRegistry,
    setup_metrics,
    teardown_metrics,
)
from sovyx.voice._capture_task import AudioCaptureTask
from sovyx.voice._frame_normalizer import FrameNormalizer
from sovyx.voice.health._metrics import (
    METRIC_AUDIO_SIGNAL_DESTROYED,
    record_audio_signal_destroyed,
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


# ── Stable name contract ─────────────────────────────────────────────────


class TestStableNameContract:
    def test_audio_signal_destroyed_name(self) -> None:
        assert METRIC_AUDIO_SIGNAL_DESTROYED == "sovyx.voice.audio.signal_destroyed"


# ── record_audio_signal_destroyed ────────────────────────────────────────


class TestRecordAudioSignalDestroyed:
    def test_no_op_without_registry(self) -> None:
        record_audio_signal_destroyed(state="destroyed")  # must not raise

    def test_state_label_propagates(
        self,
        registry: MetricsRegistry,
        reader: InMemoryMetricReader,
    ) -> None:
        record_audio_signal_destroyed(state="destroyed")
        record_audio_signal_destroyed(state="clean")
        metric = _find(_collect(reader), METRIC_AUDIO_SIGNAL_DESTROYED)
        assert metric is not None
        states = sorted(dp["attributes"]["state"] for dp in metric["data_points"])
        assert states == ["clean", "destroyed"]


# ── FrameNormalizer wire-up ──────────────────────────────────────────────


class TestFrameNormalizerEntropyWireUp:
    def test_default_disabled(self) -> None:
        norm = FrameNormalizer(source_rate=16_000, source_channels=1)
        assert norm._wiener_entropy_check_enabled is False  # noqa: SLF001
        assert norm._wiener_entropy_threshold == 0.5  # noqa: SLF001

    def test_no_metrics_when_disabled(
        self,
        registry: MetricsRegistry,
        reader: InMemoryMetricReader,
    ) -> None:
        # Foundation default: no entropy check → no signal_destroyed
        # metric emitted.
        norm = FrameNormalizer(
            source_rate=44_100,
            source_channels=1,
            source_format="float32",
        )
        norm.push(
            (np.sin(np.arange(2_048) * 0.1) * 0.5).astype(np.float32),
        )
        assert _find(_collect(reader), METRIC_AUDIO_SIGNAL_DESTROYED) is None

    def test_clean_state_on_pure_tone(
        self,
        registry: MetricsRegistry,
        reader: InMemoryMetricReader,
    ) -> None:
        # Pure tone → entropy ~ 0 → "clean".
        norm = FrameNormalizer(
            source_rate=44_100,
            source_channels=1,
            source_format="float32",
            wiener_entropy_check_enabled=True,
        )
        n = 2_048
        t = np.arange(n) / 44_100.0
        tone = (np.sin(2 * np.pi * 1_000 * t) * 0.3).astype(np.float32)
        norm.push(tone)

        metric = _find(_collect(reader), METRIC_AUDIO_SIGNAL_DESTROYED)
        assert metric is not None
        states = [dp["attributes"]["state"] for dp in metric["data_points"]]
        assert states == ["clean"]

    def test_destroyed_state_on_white_noise(
        self,
        registry: MetricsRegistry,
        reader: InMemoryMetricReader,
    ) -> None:
        # Loud white noise + lowered threshold to guarantee firing.
        rng = np.random.default_rng(0)
        noise = (rng.standard_normal(2_048) * 0.3).astype(np.float32)

        norm = FrameNormalizer(
            source_rate=44_100,
            source_channels=1,
            source_format="float32",
            wiener_entropy_check_enabled=True,
            wiener_entropy_threshold=0.3,
        )
        norm.push(noise)

        metric = _find(_collect(reader), METRIC_AUDIO_SIGNAL_DESTROYED)
        assert metric is not None
        states = [dp["attributes"]["state"] for dp in metric["data_points"]]
        assert states == ["destroyed"]

    def test_disabled_path_bit_exact_to_pre_t44b(self) -> None:
        # Critical regression test: with wiener_entropy_check_enabled
        # explicitly False (or omitted), output is IDENTICAL to a
        # FrameNormalizer constructed without the kwarg.
        rng = np.random.default_rng(0)
        block = (rng.standard_normal(2_048) * 0.5).astype(np.float32)

        baseline = FrameNormalizer(
            source_rate=44_100,
            source_channels=1,
            source_format="float32",
        )
        with_flag = FrameNormalizer(
            source_rate=44_100,
            source_channels=1,
            source_format="float32",
            wiener_entropy_check_enabled=False,
        )
        out_a = baseline.push(block.copy())
        out_b = with_flag.push(block.copy())
        assert len(out_a) == len(out_b)
        for win_a, win_b in zip(out_a, out_b, strict=True):
            np.testing.assert_array_equal(win_a, win_b)

    def test_destroyed_signal_still_flows_through(
        self,
        registry: MetricsRegistry,
        reader: InMemoryMetricReader,
    ) -> None:
        # Foundation contract: destruction is observability only.
        # The frame still reaches the pipeline (windows emitted).
        rng = np.random.default_rng(0)
        noise = (rng.standard_normal(2_048) * 0.3).astype(np.float32)

        norm = FrameNormalizer(
            source_rate=44_100,
            source_channels=1,
            source_format="float32",
            wiener_entropy_check_enabled=True,
            wiener_entropy_threshold=0.3,
        )
        windows = norm.push(noise)
        # 2048 @ 44.1kHz = 743 samples @ 16kHz → at least one
        # 512-sample window must emit.
        assert len(windows) >= 1


# ── AudioCaptureTask plumbing ────────────────────────────────────────────


class TestCaptureTaskEntropyPlumbing:
    def _pipeline_stub(self) -> MagicMock:
        return MagicMock()

    def test_default_disabled(self) -> None:
        task = AudioCaptureTask(self._pipeline_stub())
        assert task._wiener_entropy_check_enabled is False  # noqa: SLF001
        assert task._wiener_entropy_threshold == 0.5  # noqa: SLF001

    def test_explicit_flags_stored(self) -> None:
        task = AudioCaptureTask(
            self._pipeline_stub(),
            wiener_entropy_check_enabled=True,
            wiener_entropy_threshold=0.3,
        )
        assert task._wiener_entropy_check_enabled is True  # noqa: SLF001
        assert task._wiener_entropy_threshold == 0.3  # noqa: SLF001
