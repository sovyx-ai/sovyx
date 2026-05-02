"""Tests for the wake-word training pipeline foundation — Phase 8 / T8.13.

Three modules under test (per CLAUDE.md anti-pattern #16):

* ``_state.py`` — ``TrainingStatus`` StrEnum + ``TrainingJobState``
  frozen dataclass + ``is_legal_transition``.
* ``_progress.py`` — JSONL progress writer/reader.
* ``_trainer_protocol.py`` — Pluggable backend Protocol +
  registration + resolution.

Synthesizer + orchestrator + CLI + dashboard are deferred to a
focused mini-mission (per OPERATOR-DEBT-MASTER notes).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from sovyx.voice.wake_word_training._progress import (
    ProgressTracker,
)
from sovyx.voice.wake_word_training._state import (
    TrainingJobState,
    TrainingStatus,
    is_legal_transition,
)
from sovyx.voice.wake_word_training._trainer_protocol import (
    NoBackendRegisteredError,
    TrainerBackend,
    TrainingCancelledError,
    _reset_default_backend_for_tests,
    register_default_backend,
    resolve_default_backend,
)

if TYPE_CHECKING:
    from collections.abc import Callable


# ── TrainingStatus enum ──────────────────────────────────────────────


class TestTrainingStatus:
    def test_canonical_taxonomy(self) -> None:
        """Mission-mandated taxonomy. Renaming is a breaking schema
        change for downstream auditors."""
        names = {s.value for s in TrainingStatus}
        assert names == {
            "pending",
            "synthesizing",
            "training",
            "complete",
            "failed",
            "cancelled",
        }

    def test_terminal_states(self) -> None:
        assert TrainingStatus.COMPLETE.is_terminal is True
        assert TrainingStatus.FAILED.is_terminal is True
        assert TrainingStatus.CANCELLED.is_terminal is True
        assert TrainingStatus.PENDING.is_terminal is False
        assert TrainingStatus.SYNTHESIZING.is_terminal is False
        assert TrainingStatus.TRAINING.is_terminal is False


# ── TrainingJobState ─────────────────────────────────────────────────


class TestTrainingJobStateInitial:
    def test_initial_defaults(self) -> None:
        state = TrainingJobState.initial(wake_word="Lúcia")
        assert state.wake_word == "Lúcia"
        assert state.mind_id == ""
        assert state.language == "en-US"
        assert state.status is TrainingStatus.PENDING
        assert state.progress == 0.0
        assert state.message == ""
        assert state.started_at == ""
        assert state.completed_at == ""
        assert state.output_path == ""
        assert state.error_summary == ""
        assert state.samples_generated == 0
        assert state.target_samples == 200  # noqa: PLR2004

    def test_initial_explicit_args(self) -> None:
        state = TrainingJobState.initial(
            wake_word="Müller",
            mind_id="aria",
            language="de-DE",
            target_samples=500,
        )
        assert state.mind_id == "aria"
        assert state.language == "de-DE"
        assert state.target_samples == 500  # noqa: PLR2004

    def test_initial_sets_updated_at_to_iso_utc(self) -> None:
        state = TrainingJobState.initial(wake_word="x")
        # ISO-8601 UTC suffix variants: "+00:00" or "Z" are both
        # valid; Python's datetime.isoformat() emits the offset form.
        assert state.updated_at.endswith("+00:00")


class TestStateImmutability:
    def test_dataclass_is_frozen(self) -> None:
        state = TrainingJobState.initial(wake_word="x")
        with pytest.raises((AttributeError, TypeError)):
            state.status = TrainingStatus.TRAINING  # type: ignore[misc]


class TestWithStatus:
    def test_returns_new_instance(self) -> None:
        original = TrainingJobState.initial(wake_word="x")
        new = original.with_status(TrainingStatus.SYNTHESIZING)
        assert new is not original
        assert original.status is TrainingStatus.PENDING  # unchanged
        assert new.status is TrainingStatus.SYNTHESIZING

    def test_sets_started_at_on_first_transition(self) -> None:
        state = TrainingJobState.initial(wake_word="x")
        assert state.started_at == ""
        new = state.with_status(TrainingStatus.SYNTHESIZING)
        assert new.started_at != ""
        assert new.started_at.endswith("+00:00")

    def test_preserves_started_at_on_subsequent_transitions(self) -> None:
        state = TrainingJobState.initial(wake_word="x")
        synth = state.with_status(TrainingStatus.SYNTHESIZING)
        train = synth.with_status(TrainingStatus.TRAINING)
        # started_at set ONCE on first non-PENDING transition.
        assert train.started_at == synth.started_at

    def test_sets_completed_at_on_terminal_transition(self) -> None:
        state = TrainingJobState.initial(wake_word="x").with_status(
            TrainingStatus.SYNTHESIZING,
        )
        assert state.completed_at == ""
        terminal = state.with_status(
            TrainingStatus.CANCELLED,
            message="user cancelled",
        )
        assert terminal.completed_at != ""
        assert terminal.completed_at.endswith("+00:00")
        assert terminal.message == "user cancelled"

    def test_carries_through_unspecified_fields(self) -> None:
        state = TrainingJobState.initial(wake_word="x", target_samples=100)
        new = state.with_status(
            TrainingStatus.SYNTHESIZING,
            progress=0.5,
            samples_generated=50,
        )
        # Unspecified field carries through.
        assert new.target_samples == 100  # noqa: PLR2004
        assert new.progress == 0.5  # noqa: PLR2004
        assert new.samples_generated == 50  # noqa: PLR2004

    def test_sets_output_path_on_complete(self) -> None:
        synth = TrainingJobState.initial(wake_word="x").with_status(
            TrainingStatus.SYNTHESIZING,
        )
        train = synth.with_status(TrainingStatus.TRAINING)
        complete = train.with_status(
            TrainingStatus.COMPLETE,
            output_path="/data/wake_word_models/x.onnx",
            progress=1.0,
        )
        assert complete.output_path == "/data/wake_word_models/x.onnx"
        assert complete.progress == 1.0
        assert complete.completed_at != ""


# ── State serialisation ──────────────────────────────────────────────


class TestToDict:
    def test_to_dict_round_trips_via_json(self) -> None:
        state = TrainingJobState.initial(
            wake_word="Lúcia",
            mind_id="aria",
            language="pt-BR",
            target_samples=150,
        )
        encoded = json.dumps(state.to_dict(), ensure_ascii=False)
        decoded = json.loads(encoded)
        assert decoded["wake_word"] == "Lúcia"
        assert decoded["mind_id"] == "aria"
        assert decoded["language"] == "pt-BR"
        assert decoded["status"] == "pending"
        assert decoded["target_samples"] == 150  # noqa: PLR2004

    def test_status_is_string_value_not_enum(self) -> None:
        state = TrainingJobState.initial(wake_word="x").with_status(
            TrainingStatus.TRAINING,
        )
        d = state.to_dict()
        assert d["status"] == "training"
        assert isinstance(d["status"], str)


# ── State transition rules ───────────────────────────────────────────


class TestTransitionRules:
    @pytest.mark.parametrize(
        ("current", "new", "expected"),
        [
            # PENDING → valid
            (TrainingStatus.PENDING, TrainingStatus.SYNTHESIZING, True),
            (TrainingStatus.PENDING, TrainingStatus.CANCELLED, True),
            (TrainingStatus.PENDING, TrainingStatus.FAILED, True),
            # PENDING → invalid (skip phases)
            (TrainingStatus.PENDING, TrainingStatus.TRAINING, False),
            (TrainingStatus.PENDING, TrainingStatus.COMPLETE, False),
            # SYNTHESIZING → valid
            (TrainingStatus.SYNTHESIZING, TrainingStatus.TRAINING, True),
            (TrainingStatus.SYNTHESIZING, TrainingStatus.CANCELLED, True),
            (TrainingStatus.SYNTHESIZING, TrainingStatus.FAILED, True),
            # SYNTHESIZING → invalid (back to PENDING, skip TRAINING)
            (TrainingStatus.SYNTHESIZING, TrainingStatus.PENDING, False),
            (TrainingStatus.SYNTHESIZING, TrainingStatus.COMPLETE, False),
            # TRAINING → valid
            (TrainingStatus.TRAINING, TrainingStatus.COMPLETE, True),
            (TrainingStatus.TRAINING, TrainingStatus.CANCELLED, True),
            (TrainingStatus.TRAINING, TrainingStatus.FAILED, True),
            # Terminal — no outgoing transitions.
            (TrainingStatus.COMPLETE, TrainingStatus.PENDING, False),
            (TrainingStatus.COMPLETE, TrainingStatus.TRAINING, False),
            (TrainingStatus.FAILED, TrainingStatus.PENDING, False),
            (TrainingStatus.CANCELLED, TrainingStatus.PENDING, False),
        ],
    )
    def test_transitions(
        self,
        current: TrainingStatus,
        new: TrainingStatus,
        expected: bool,
    ) -> None:
        assert is_legal_transition(current, new) is expected


# ── ProgressTracker ──────────────────────────────────────────────────


class TestProgressTrackerEmpty:
    def test_read_missing_path_returns_empty(self, tmp_path: Path) -> None:
        tracker = ProgressTracker(tmp_path / "nonexistent" / "progress.jsonl")
        assert tracker.read_all() == []
        assert tracker.latest() is None


class TestProgressTrackerAppendRead:
    def test_round_trip_single_event(self, tmp_path: Path) -> None:
        tracker = ProgressTracker(tmp_path / "progress.jsonl")
        state = TrainingJobState.initial(wake_word="Lúcia")
        tracker.append(state)

        events = tracker.read_all()
        assert len(events) == 1
        assert events[0].line_no == 1
        assert events[0].state.wake_word == "Lúcia"
        assert events[0].state.status is TrainingStatus.PENDING

    def test_round_trip_multiple_events_preserves_order(
        self,
        tmp_path: Path,
    ) -> None:
        tracker = ProgressTracker(tmp_path / "progress.jsonl")
        s1 = TrainingJobState.initial(wake_word="x")
        s2 = s1.with_status(TrainingStatus.SYNTHESIZING)
        s3 = s2.with_status(TrainingStatus.TRAINING)
        s4 = s3.with_status(TrainingStatus.COMPLETE, output_path="/x.onnx")
        for s in (s1, s2, s3, s4):
            tracker.append(s)

        events = tracker.read_all()
        assert [e.state.status for e in events] == [
            TrainingStatus.PENDING,
            TrainingStatus.SYNTHESIZING,
            TrainingStatus.TRAINING,
            TrainingStatus.COMPLETE,
        ]
        assert [e.line_no for e in events] == [1, 2, 3, 4]
        assert events[-1].state.output_path == "/x.onnx"

    def test_latest_returns_most_recent(self, tmp_path: Path) -> None:
        tracker = ProgressTracker(tmp_path / "progress.jsonl")
        s1 = TrainingJobState.initial(wake_word="x")
        s2 = s1.with_status(TrainingStatus.SYNTHESIZING)
        tracker.append(s1)
        tracker.append(s2)
        latest = tracker.latest()
        assert latest is not None
        assert latest.status is TrainingStatus.SYNTHESIZING

    def test_creates_parent_directory(self, tmp_path: Path) -> None:
        nested = tmp_path / "a" / "b" / "c" / "progress.jsonl"
        tracker = ProgressTracker(nested)
        tracker.append(TrainingJobState.initial(wake_word="x"))
        assert nested.exists()


class TestProgressTrackerRobustness:
    def test_skips_corrupt_line(self, tmp_path: Path) -> None:
        path = tmp_path / "progress.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        # Write good + corrupt + good.
        good_state = TrainingJobState.initial(wake_word="x")
        good_line = json.dumps(good_state.to_dict(), sort_keys=True)
        path.write_text(
            f"{good_line}\nthis is not json\n{good_line}\n",
            encoding="utf-8",
        )

        tracker = ProgressTracker(path)
        events = tracker.read_all()
        # Two good + one skipped.
        assert len(events) == 2
        # Line numbers preserve original positions (1 and 3).
        assert events[0].line_no == 1
        assert events[1].line_no == 3  # noqa: PLR2004

    def test_skips_unknown_status(self, tmp_path: Path) -> None:
        path = tmp_path / "progress.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        # A line with status that's not in the enum.
        path.write_text(
            json.dumps({"status": "running_quantum_inference", "wake_word": "x"}) + "\n",
            encoding="utf-8",
        )
        tracker = ProgressTracker(path)
        assert tracker.read_all() == []

    def test_skips_empty_lines(self, tmp_path: Path) -> None:
        path = tmp_path / "progress.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        good = TrainingJobState.initial(wake_word="x")
        good_line = json.dumps(good.to_dict(), sort_keys=True)
        path.write_text(f"\n{good_line}\n\n\n", encoding="utf-8")
        tracker = ProgressTracker(path)
        events = tracker.read_all()
        assert len(events) == 1


class TestProgressEventImmutability:
    def test_event_is_frozen(self, tmp_path: Path) -> None:
        tracker = ProgressTracker(tmp_path / "progress.jsonl")
        tracker.append(TrainingJobState.initial(wake_word="x"))
        event = tracker.read_all()[0]
        with pytest.raises((AttributeError, TypeError)):
            event.line_no = 999  # type: ignore[misc]


# ── TrainerBackend Protocol ──────────────────────────────────────────


class _StubBackend:
    """Minimal valid backend for Protocol-conformance tests."""

    @property
    def name(self) -> str:
        return "stub"

    def train(
        self,
        *,
        wake_word: str,  # noqa: ARG002
        language: str,  # noqa: ARG002
        positive_samples: list[Path],  # noqa: ARG002
        negative_samples: list[Path],  # noqa: ARG002
        output_path: Path,
        on_progress: Callable[[float, str], None],  # noqa: ARG002
        cancel_check: Callable[[], bool],  # noqa: ARG002
    ) -> Path:
        return output_path


class _IncompleteBackend:
    """Missing the ``train`` method — should fail Protocol check."""

    @property
    def name(self) -> str:
        return "incomplete"


class TestRegisterDefaultBackend:
    def setup_method(self) -> None:
        _reset_default_backend_for_tests()

    def teardown_method(self) -> None:
        _reset_default_backend_for_tests()

    def test_resolve_without_register_raises_with_install_hints(self) -> None:
        with pytest.raises(NoBackendRegisteredError) as exc_info:
            resolve_default_backend()
        # Error message must include actionable, VERIFIED operator
        # paths (no speculative extras). Anchored on each of the 3
        # documented options: external train + drop, custom Backend
        # impl, or STT fallback.
        msg = str(exc_info.value)
        # Path 1: external train + drop into pretrained pool.
        assert "wake_word_models/pretrained" in msg
        assert "openwakeword-trainer" in msg or "OpenWakeWord Colab" in msg
        # Path 2: register custom backend.
        assert "register_default_backend" in msg
        # Path 3: STT fallback (already shipped).
        assert "STT fallback" in msg
        # Must NOT carry the old speculative extras name.
        assert "sovyx[wake-training]" not in msg
        # Must NOT carry the wrong openwakeword extra name.
        assert "openwakeword[training]" not in msg

    def test_register_then_resolve(self) -> None:
        backend = _StubBackend()
        register_default_backend(backend)
        resolved = resolve_default_backend()
        assert resolved is backend

    def test_register_invalid_backend_raises_typeerror(self) -> None:
        with pytest.raises(TypeError, match="does not satisfy the TrainerBackend"):
            register_default_backend(_IncompleteBackend())  # type: ignore[arg-type]

    def test_re_register_replaces(self) -> None:
        first = _StubBackend()
        second = _StubBackend()
        register_default_backend(first)
        register_default_backend(second)
        # Last-write-wins.
        assert resolve_default_backend() is second


class TestTrainingCancelledError:
    def test_is_distinct_exception_class(self) -> None:
        # The orchestrator catches this specifically (vs. generic
        # Exception) so cancellation surfaces as CANCELLED not FAILED.
        # Verifies the class hierarchy contract.
        assert issubclass(TrainingCancelledError, Exception)
        assert not issubclass(TrainingCancelledError, NoBackendRegisteredError)

    def test_can_be_raised_and_caught(self) -> None:
        with pytest.raises(TrainingCancelledError, match="cancelled"):
            raise TrainingCancelledError("user cancelled training")


class TestProtocolRuntimeCheck:
    def test_stub_satisfies_protocol(self) -> None:
        assert isinstance(_StubBackend(), TrainerBackend)

    def test_incomplete_does_not_satisfy_protocol(self) -> None:
        assert not isinstance(_IncompleteBackend(), TrainerBackend)
