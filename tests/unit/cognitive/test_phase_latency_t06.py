"""T06 mission test — per-phase cognitive latency histogram (Mission pre-wake-word T06).

Before T06, only the full-loop ``sovyx.cognitive.latency`` histogram
existed. When latency regressed, operators couldn't decompose where
in the loop the time was spent (Perceive / Attend / Think / Act /
Reflect). T06 added ``sovyx.cognitive.phase_latency`` Histogram with
``phase`` attribute (5 closed-set values).

These tests pin:
1. The histogram is registered on the MetricsRegistry.
2. The ``_measure_phase_latency`` helper records elapsed_ms with the
   correct phase attribute.
3. The helper is no-op safe when the registry is missing the attribute
   (defensive — e.g. tests with bare registries).
4. All 5 CognitivePhase values are accepted.
"""

from __future__ import annotations

import time
from unittest.mock import patch


class TestHistogramRegistration:
    """The histogram is wired on the MetricsRegistry post-T06."""

    def test_phase_latency_histogram_registered(self) -> None:
        from sovyx.observability.metrics import get_metrics

        metrics = get_metrics()
        assert hasattr(metrics, "cognitive_phase_latency"), (
            "cognitive_phase_latency must be registered post-T06"
        )
        assert callable(metrics.cognitive_phase_latency.record)


class TestMeasurePhaseLatencyHelper:
    """The ``_measure_phase_latency`` context manager records correctly."""

    def test_helper_records_elapsed_ms_for_each_phase(self) -> None:
        from sovyx.cognitive.loop import _measure_phase_latency
        from sovyx.engine.types import CognitivePhase
        from sovyx.observability.metrics import get_metrics

        instrument = get_metrics().cognitive_phase_latency
        with patch.object(instrument, "record") as mock_record:
            with _measure_phase_latency(CognitivePhase.PERCEIVING):
                time.sleep(0.001)  # ~1 ms
            mock_record.assert_called_once()
            args, kwargs = mock_record.call_args
            elapsed_ms = args[0]
            attrs = kwargs.get("attributes", {})
            assert isinstance(elapsed_ms, float)
            assert elapsed_ms >= 0  # may be sub-1ms on fast clocks
            assert attrs == {"phase": "perceiving"}

    def test_helper_records_for_all_five_phases(self) -> None:
        """All 5 phase values flow through the helper to the attribute."""
        from sovyx.cognitive.loop import _measure_phase_latency
        from sovyx.engine.types import CognitivePhase
        from sovyx.observability.metrics import get_metrics

        instrument = get_metrics().cognitive_phase_latency
        all_phases = [
            CognitivePhase.PERCEIVING,
            CognitivePhase.ATTENDING,
            CognitivePhase.THINKING,
            CognitivePhase.ACTING,
            CognitivePhase.REFLECTING,
        ]
        with patch.object(instrument, "record") as mock_record:
            for phase in all_phases:
                with _measure_phase_latency(phase):
                    pass
        assert mock_record.call_count == 5
        recorded_phases = [
            call.kwargs["attributes"]["phase"] for call in mock_record.call_args_list
        ]
        assert recorded_phases == [
            "perceiving",
            "attending",
            "thinking",
            "acting",
            "reflecting",
        ]

    def test_helper_records_even_on_exception(self) -> None:
        """The histogram fires in the ``finally`` block even when the
        wrapped phase raises — operators see latency for failed phases
        too. Critical for diagnosing slow-then-failing iterations."""
        from sovyx.cognitive.loop import _measure_phase_latency
        from sovyx.engine.types import CognitivePhase
        from sovyx.observability.metrics import get_metrics

        instrument = get_metrics().cognitive_phase_latency
        with patch.object(instrument, "record") as mock_record:
            try:
                with _measure_phase_latency(CognitivePhase.THINKING):
                    raise ValueError("boom")
            except ValueError:
                pass
            mock_record.assert_called_once()
            args, kwargs = mock_record.call_args
            assert kwargs["attributes"]["phase"] == "thinking"


class TestNoOpWhenInstrumentMissing:
    """Defensive: helper no-ops silently when registry lacks the attribute."""

    def test_helper_noop_when_instrument_missing(self) -> None:
        """If the registry doesn't have ``cognitive_phase_latency``
        (e.g. mocked / bare in tests), the helper must NOT raise."""
        from sovyx.cognitive import loop as loop_module
        from sovyx.engine.types import CognitivePhase

        class _BareRegistry:
            pass

        with (
            patch.object(loop_module, "get_metrics", return_value=_BareRegistry()),
            loop_module._measure_phase_latency(CognitivePhase.PERCEIVING),
        ):
            # Must not raise
            pass


class TestSourceWireUpAt5Sites:
    """Source-grep verification — both _execute_loop and
    _execute_loop_streaming wrap all 5 phases."""

    def test_execute_loop_wraps_all_5_phases(self) -> None:
        from pathlib import Path

        path = Path(__file__).parents[3] / "src" / "sovyx" / "cognitive" / "loop.py"
        text = path.read_text(encoding="utf-8")
        # 5 phases × 2 paths (sync + streaming) = 10 occurrences
        assert text.count("_measure_phase_latency(CognitivePhase.PERCEIVING)") >= 2
        assert text.count("_measure_phase_latency(CognitivePhase.ATTENDING)") >= 2
        assert text.count("_measure_phase_latency(CognitivePhase.THINKING)") >= 2
        assert text.count("_measure_phase_latency(CognitivePhase.ACTING)") >= 2
        assert text.count("_measure_phase_latency(CognitivePhase.REFLECTING)") >= 2
