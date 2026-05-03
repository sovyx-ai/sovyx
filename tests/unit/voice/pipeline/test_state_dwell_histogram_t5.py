"""T5 mission tests — voice pipeline state-dwell histogram.

Mission: ``MISSION-wake-word-runtime-wireup-2026-05-03.md`` §T5.

Decomposes the per-turn voice latency budget by recording the ms a
turn spends in each :class:`VoicePipelineState` before transitioning
out. One sample per :meth:`PipelineStateMachine.record_transition`
call, attributed by the FROM state. Self-loops are recorded too —
the canonical table allows IDLE/THINKING/SPEAKING self-loops and
dropping them would bias the per-state percentile upward.

Histogram name: ``sovyx.voice.pipeline.state_dwell``. Attribute:
``state`` (one of the 6 closed-set state names — bounded cardinality).
"""

from __future__ import annotations

from collections.abc import Iterator
from unittest.mock import MagicMock, patch

import pytest

from sovyx.voice.pipeline._state import VoicePipelineState
from sovyx.voice.pipeline._state_machine import PipelineStateMachine


@pytest.fixture()
def fake_metrics() -> Iterator[MagicMock]:
    """Patch the get_metrics() lookup to return a stub registry that
    captures every histogram.record() call."""
    histogram = MagicMock()
    fake_registry = MagicMock()
    fake_registry.voice_pipeline_state_dwell = histogram
    with patch("sovyx.observability.metrics.get_metrics", return_value=fake_registry):
        yield histogram


def _make_machine_with_clock(
    clock_values: list[float],
) -> PipelineStateMachine:
    """Construct a state machine whose monotonic clock returns the
    given sequence of timestamps. The first value is consumed at
    construction (sets ``entered_monotonic`` for IDLE)."""
    machine = PipelineStateMachine()
    iterator = iter(clock_values)
    machine._monotonic = lambda: next(iterator)  # type: ignore[method-assign]  # noqa: SLF001
    return machine


# ── Single-transition recording ──────────────────────────────────────


class TestSingleTransitionRecording:
    def test_records_dwell_attributed_by_from_state(self, fake_metrics: MagicMock) -> None:
        # Construct: clock starts at 100.0 (IDLE entry). Transition
        # IDLE → WAKE_DETECTED at 100.250 → dwell = 250.0 ms in IDLE.
        machine = PipelineStateMachine()
        # Override after construction so the IDLE entry timestamp is
        # the one set at __init__; we only control the next call.
        machine._monotonic = lambda: 100.250  # type: ignore[method-assign]  # noqa: SLF001
        # Anchor IDLE entry at exactly 100.0 to make dwell deterministic.
        machine._state.entered_monotonic = 100.0  # noqa: SLF001

        machine.record_transition(
            VoicePipelineState.IDLE,
            VoicePipelineState.WAKE_DETECTED,
        )

        fake_metrics.record.assert_called_once()
        args, kwargs = fake_metrics.record.call_args
        # First positional arg is the dwell value in ms.
        assert args[0] == pytest.approx(250.0, abs=0.001)
        # The state attribute is the FROM state.
        assert kwargs["attributes"] == {"state": "IDLE"}

    def test_self_loop_is_recorded(self, fake_metrics: MagicMock) -> None:
        """The canonical table allows IDLE/THINKING/SPEAKING self-loops;
        dropping them would skew per-state percentiles."""
        machine = PipelineStateMachine()
        machine._monotonic = lambda: 200.100  # type: ignore[method-assign]  # noqa: SLF001
        machine._state.entered_monotonic = 200.0  # noqa: SLF001
        machine._state.current = VoicePipelineState.THINKING  # noqa: SLF001

        machine.record_transition(
            VoicePipelineState.THINKING,
            VoicePipelineState.THINKING,
        )

        fake_metrics.record.assert_called_once()
        args, kwargs = fake_metrics.record.call_args
        assert args[0] == pytest.approx(100.0, abs=0.001)
        assert kwargs["attributes"] == {"state": "THINKING"}

    def test_negative_dwell_clamps_to_zero(self, fake_metrics: MagicMock) -> None:
        """Defensive: clock skew or test-clock injection can yield
        ``now < entered_monotonic``; the code clamps to 0.0 ms so the
        histogram never sees a negative sample."""
        machine = PipelineStateMachine()
        # Clock REGRESSES: now=99.5 vs entered=100.0
        machine._monotonic = lambda: 99.5  # type: ignore[method-assign]  # noqa: SLF001
        machine._state.entered_monotonic = 100.0  # noqa: SLF001

        machine.record_transition(
            VoicePipelineState.IDLE,
            VoicePipelineState.WAKE_DETECTED,
        )

        args, _kwargs = fake_metrics.record.call_args
        assert args[0] == 0.0


# ── Multi-transition flow (per-turn shape) ───────────────────────────


class TestPerTurnFlow:
    """A canonical voice turn flows through 5 states. Verify each
    state's dwell is recorded with the right FROM attribute."""

    def test_full_turn_records_one_sample_per_state(self, fake_metrics: MagicMock) -> None:
        machine = PipelineStateMachine()
        # Anchor IDLE entry at 0.0 so the initial dwell is deterministic.
        machine._state.entered_monotonic = 0.0  # noqa: SLF001

        # Sequence the clock so each call to _monotonic advances by 0.1s.
        clock_values = iter([1.0, 1.5, 2.0, 4.0, 4.5, 5.0])
        machine._monotonic = lambda: next(clock_values)  # type: ignore[method-assign]  # noqa: SLF001

        # IDLE → WAKE_DETECTED → RECORDING → TRANSCRIBING → THINKING → SPEAKING → IDLE
        flow = [
            (VoicePipelineState.IDLE, VoicePipelineState.WAKE_DETECTED),
            (VoicePipelineState.WAKE_DETECTED, VoicePipelineState.RECORDING),
            (VoicePipelineState.RECORDING, VoicePipelineState.TRANSCRIBING),
            (VoicePipelineState.TRANSCRIBING, VoicePipelineState.THINKING),
            (VoicePipelineState.THINKING, VoicePipelineState.SPEAKING),
            (VoicePipelineState.SPEAKING, VoicePipelineState.IDLE),
        ]
        for src, dst in flow:
            machine._state.current = src  # noqa: SLF001
            machine.record_transition(src, dst)

        # 6 transitions → 6 samples.
        assert fake_metrics.record.call_count == 6

        # Verify the FROM-state attribute on each sample.
        observed_states = [
            call.kwargs["attributes"]["state"] for call in fake_metrics.record.call_args_list
        ]
        assert observed_states == [
            "IDLE",
            "WAKE_DETECTED",
            "RECORDING",
            "TRANSCRIBING",
            "THINKING",
            "SPEAKING",
        ]


# ── Robustness when metrics is unavailable ───────────────────────────


class TestMetricsUnavailable:
    """The histogram lookup is best-effort (defensive ``getattr``).
    If metrics init failed or the registry was reset, recording must
    not raise — just silently skip the sample."""

    def test_get_metrics_returns_no_attribute(self) -> None:
        machine = PipelineStateMachine()
        machine._state.entered_monotonic = 0.0  # noqa: SLF001

        # Registry without ``voice_pipeline_state_dwell`` attribute.
        fake_registry = object()
        with patch(
            "sovyx.observability.metrics.get_metrics",
            return_value=fake_registry,
        ):
            # Must NOT raise.
            machine.record_transition(
                VoicePipelineState.IDLE,
                VoicePipelineState.WAKE_DETECTED,
            )

    def test_no_metrics_does_not_break_state_mutation(self) -> None:
        """State + history must still update even when metrics is
        unavailable."""
        machine = PipelineStateMachine()
        machine._state.entered_monotonic = 0.0  # noqa: SLF001
        machine._monotonic = lambda: 1.0  # type: ignore[method-assign]  # noqa: SLF001

        with patch(
            "sovyx.observability.metrics.get_metrics",
            return_value=object(),  # no histogram attr
        ):
            machine.record_transition(
                VoicePipelineState.IDLE,
                VoicePipelineState.WAKE_DETECTED,
            )

        # State + counters updated regardless of metrics path.
        assert machine.current_state is VoicePipelineState.WAKE_DETECTED
        assert machine.transition_count == 1
