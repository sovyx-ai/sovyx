"""Tests for the phase-inversion auto-recovery wire-up [Phase 4 T4.46].

Coverage:

* :data:`METRIC_AUDIO_PHASE_INVERSION_RECOVERY` name pin.
* :func:`record_audio_phase_inversion_recovery` no-op safety + state
  propagation.
* :class:`FrameNormalizer` accepts the kwarg, latches engaged after the
  configured run of consecutive inverted blocks, reverts after the
  configured run of clean blocks, and emits one telemetry event per
  TRANSITION (not per block).
* Disabled path bit-exact regression-guard.
* Single inversion does NOT engage (engage threshold = 3).
* Telemetry fires exactly twice over an engage→revert cycle.
* :class:`AudioCaptureTask` plumbing.

Operator opt-in default per ``feedback_staged_adoption``: the new
flag ships disabled (``voice_phase_inversion_auto_recovery_enabled =
False``) so the foundation is observability-only on day one.
"""

from __future__ import annotations

from collections.abc import Generator
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
from sovyx.voice._frame_normalizer import (
    _PHASE_RECOVERY_ENGAGE_THRESHOLD,
    _PHASE_RECOVERY_REVERT_THRESHOLD,
    FrameNormalizer,
)
from sovyx.voice.health._metrics import (
    METRIC_AUDIO_PHASE_INVERSION_RECOVERY,
    record_audio_phase_inversion_recovery,
)

# ── Fixtures ─────────────────────────────────────────────────────────────


@pytest.fixture()
def reader() -> InMemoryMetricReader:
    return InMemoryMetricReader()


@pytest.fixture(autouse=True)
def _reset_otel() -> Generator[None, None, None]:
    from opentelemetry.metrics import _internal as otel_internal

    yield
    otel_internal._METER_PROVIDER_SET_ONCE._done = False
    otel_internal._METER_PROVIDER = None


@pytest.fixture()
def registry(reader: InMemoryMetricReader) -> Generator[MetricsRegistry, None, None]:
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


def _stereo_inverted_block(n: int = 1024) -> np.ndarray:
    """Build a 2-channel int16 block whose two channels are exact inverses.

    Pearson correlation = -1.0 (well below the -0.3 engage threshold).
    """
    signal = (np.sin(np.linspace(0.0, 4.0, n)) * 0.5).astype(np.float32)
    stereo_f = np.column_stack([signal, -signal])
    return (stereo_f * 32767.0).astype(np.int16)


def _stereo_clean_block(n: int = 1024) -> np.ndarray:
    """Build a 2-channel int16 block with identical L/R (correlation ≈ +1.0)."""
    signal = (np.sin(np.linspace(0.0, 4.0, n)) * 0.5).astype(np.float32)
    stereo_f = np.column_stack([signal, signal])
    return (stereo_f * 32767.0).astype(np.int16)


# ── Stable name contract ─────────────────────────────────────────────────


class TestStableNameContract:
    def test_phase_inversion_recovery_name(self) -> None:
        assert (
            METRIC_AUDIO_PHASE_INVERSION_RECOVERY == "sovyx.voice.audio.phase_inversion_recovery"
        )

    def test_engage_threshold_pinned(self) -> None:
        # Three consecutive inverted blocks ≈ 30-90 ms at typical
        # PortAudio block sizes; the band-aid #8 docstring locks the
        # value, this guards against silent drift.
        assert _PHASE_RECOVERY_ENGAGE_THRESHOLD == 3  # noqa: PLR2004

    def test_revert_threshold_pinned(self) -> None:
        # ~50 blocks ≈ 0.5-1.5 s of clean signal — long enough that
        # a transient inversion run doesn't keep flipping the
        # downmix mode in a flapping pattern.
        assert _PHASE_RECOVERY_REVERT_THRESHOLD == 50  # noqa: PLR2004


# ── record_audio_phase_inversion_recovery ────────────────────────────────


class TestRecordAudioPhaseInversionRecovery:
    def test_no_op_without_registry(self) -> None:
        record_audio_phase_inversion_recovery(state="engaged")  # must not raise

    def test_state_label_propagates(
        self,
        registry: MetricsRegistry,
        reader: InMemoryMetricReader,
    ) -> None:
        record_audio_phase_inversion_recovery(state="engaged")
        record_audio_phase_inversion_recovery(state="reverted")
        metric = _find(_collect(reader), METRIC_AUDIO_PHASE_INVERSION_RECOVERY)
        assert metric is not None
        states = sorted(dp["attributes"]["state"] for dp in metric["data_points"])
        assert states == ["engaged", "reverted"]


# ── FrameNormalizer wire-up ──────────────────────────────────────────────


class TestFrameNormalizerPhaseRecoveryWireUp:
    def test_default_disabled(self) -> None:
        norm = FrameNormalizer(source_rate=16_000, source_channels=2)
        assert norm._phase_recovery_enabled is False  # noqa: SLF001
        assert norm._phase_recovery_engaged is False  # noqa: SLF001

    def test_disabled_path_bit_exact_to_pre_t46(self) -> None:
        # Critical regression test: enabling the kwarg with False
        # produces IDENTICAL output to a FrameNormalizer constructed
        # without the kwarg, even on phase-inverted stereo input.
        block = _stereo_inverted_block(2_048)

        baseline = FrameNormalizer(source_rate=16_000, source_channels=2)
        with_flag = FrameNormalizer(
            source_rate=16_000,
            source_channels=2,
            phase_inversion_auto_recovery_enabled=False,
        )
        out_a = baseline.push(block.copy())
        out_b = with_flag.push(block.copy())
        assert len(out_a) == len(out_b)
        for win_a, win_b in zip(out_a, out_b, strict=True):
            np.testing.assert_array_equal(win_a, win_b)

    def test_single_inversion_does_not_engage(
        self,
        registry: MetricsRegistry,
        reader: InMemoryMetricReader,
    ) -> None:
        # A lone inverted block doesn't trip the recovery — the
        # engage threshold is 3 consecutive blocks (de-flap).
        norm = FrameNormalizer(
            source_rate=16_000,
            source_channels=2,
            phase_inversion_auto_recovery_enabled=True,
        )
        norm.push(_stereo_inverted_block())

        assert norm._phase_recovery_engaged is False  # noqa: SLF001
        assert norm._phase_recovery_consecutive_inverted == 1  # noqa: SLF001
        assert _find(_collect(reader), METRIC_AUDIO_PHASE_INVERSION_RECOVERY) is None

    def test_two_inversions_do_not_engage(self) -> None:
        # Bound check: 2 < threshold (3) → still observability-only.
        norm = FrameNormalizer(
            source_rate=16_000,
            source_channels=2,
            phase_inversion_auto_recovery_enabled=True,
        )
        norm.push(_stereo_inverted_block())
        norm.push(_stereo_inverted_block())
        assert norm._phase_recovery_engaged is False  # noqa: SLF001
        assert norm._phase_recovery_consecutive_inverted == 2  # noqa: SLF001, PLR2004

    def test_three_inversions_engage(
        self,
        registry: MetricsRegistry,
        reader: InMemoryMetricReader,
    ) -> None:
        norm = FrameNormalizer(
            source_rate=16_000,
            source_channels=2,
            phase_inversion_auto_recovery_enabled=True,
        )
        for _ in range(_PHASE_RECOVERY_ENGAGE_THRESHOLD):
            norm.push(_stereo_inverted_block())

        assert norm._phase_recovery_engaged is True  # noqa: SLF001

        metric = _find(_collect(reader), METRIC_AUDIO_PHASE_INVERSION_RECOVERY)
        assert metric is not None
        states = [dp["attributes"]["state"] for dp in metric["data_points"]]
        assert states == ["engaged"]

    def test_clean_block_resets_inverted_run(self) -> None:
        # A single clean block in the middle of an inverted run
        # resets the consecutive-inverted counter — engagement is
        # ABOUT a sustained pathology, not a cumulative one.
        norm = FrameNormalizer(
            source_rate=16_000,
            source_channels=2,
            phase_inversion_auto_recovery_enabled=True,
        )
        norm.push(_stereo_inverted_block())
        norm.push(_stereo_inverted_block())
        norm.push(_stereo_clean_block())

        assert norm._phase_recovery_engaged is False  # noqa: SLF001
        assert norm._phase_recovery_consecutive_inverted == 0  # noqa: SLF001

    def test_silent_block_does_not_engage(self) -> None:
        # Pure silence → correlation returns 0.0 (above threshold) →
        # treated as a clean block. Silence must NEVER engage the
        # recovery (would be a self-fulfilling false positive).
        norm = FrameNormalizer(
            source_rate=16_000,
            source_channels=2,
            phase_inversion_auto_recovery_enabled=True,
        )
        for _ in range(10):
            norm.push(np.zeros((1024, 2), dtype=np.int16))
        assert norm._phase_recovery_engaged is False  # noqa: SLF001
        assert norm._phase_recovery_consecutive_inverted == 0  # noqa: SLF001

    def test_engage_then_revert_after_clean_run(
        self,
        registry: MetricsRegistry,
        reader: InMemoryMetricReader,
    ) -> None:
        norm = FrameNormalizer(
            source_rate=16_000,
            source_channels=2,
            phase_inversion_auto_recovery_enabled=True,
        )
        # Engage.
        for _ in range(_PHASE_RECOVERY_ENGAGE_THRESHOLD):
            norm.push(_stereo_inverted_block())
        assert norm._phase_recovery_engaged is True  # noqa: SLF001

        # Walk the clean-block counter to revert.
        for _ in range(_PHASE_RECOVERY_REVERT_THRESHOLD):
            norm.push(_stereo_clean_block())

        assert norm._phase_recovery_engaged is False  # noqa: SLF001
        # Counter resets on revert.
        assert norm._phase_recovery_consecutive_clean == 0  # noqa: SLF001

        metric = _find(_collect(reader), METRIC_AUDIO_PHASE_INVERSION_RECOVERY)
        assert metric is not None
        states = sorted(dp["attributes"]["state"] for dp in metric["data_points"])
        assert states == ["engaged", "reverted"]

    def test_inverted_block_during_clean_run_resets_clean_counter(self) -> None:
        # Engage, then push 49 clean blocks (one short of revert),
        # then a single inverted block — that single inversion
        # MUST reset the clean counter without un-engaging.
        norm = FrameNormalizer(
            source_rate=16_000,
            source_channels=2,
            phase_inversion_auto_recovery_enabled=True,
        )
        for _ in range(_PHASE_RECOVERY_ENGAGE_THRESHOLD):
            norm.push(_stereo_inverted_block())
        assert norm._phase_recovery_engaged is True  # noqa: SLF001

        for _ in range(_PHASE_RECOVERY_REVERT_THRESHOLD - 1):
            norm.push(_stereo_clean_block())
        assert norm._phase_recovery_engaged is True  # noqa: SLF001

        norm.push(_stereo_inverted_block())
        # Still engaged + clean counter reset.
        assert norm._phase_recovery_engaged is True  # noqa: SLF001
        assert norm._phase_recovery_consecutive_clean == 0  # noqa: SLF001

    def test_telemetry_fires_only_on_transition_not_per_block(
        self,
        registry: MetricsRegistry,
        reader: InMemoryMetricReader,
    ) -> None:
        # 100 sustained inverted blocks → ONE engaged event, not 100.
        norm = FrameNormalizer(
            source_rate=16_000,
            source_channels=2,
            phase_inversion_auto_recovery_enabled=True,
        )
        for _ in range(100):
            norm.push(_stereo_inverted_block())

        metric = _find(_collect(reader), METRIC_AUDIO_PHASE_INVERSION_RECOVERY)
        assert metric is not None
        engaged_dps = [
            dp for dp in metric["data_points"] if dp["attributes"]["state"] == "engaged"
        ]
        assert len(engaged_dps) == 1
        # Cumulative counter aggregates by attribute set; one engage
        # transition → value 1 (not 100).
        assert engaged_dps[0]["value"] == 1


# ── AudioCaptureTask plumbing ────────────────────────────────────────────


class TestCaptureTaskPhaseRecoveryPlumbing:
    def _pipeline_stub(self) -> MagicMock:
        return MagicMock()

    def test_default_disabled(self) -> None:
        task = AudioCaptureTask(self._pipeline_stub())
        assert task._phase_inversion_auto_recovery_enabled is False  # noqa: SLF001

    def test_explicit_flag_stored(self) -> None:
        task = AudioCaptureTask(
            self._pipeline_stub(),
            phase_inversion_auto_recovery_enabled=True,
        )
        assert task._phase_inversion_auto_recovery_enabled is True  # noqa: SLF001
