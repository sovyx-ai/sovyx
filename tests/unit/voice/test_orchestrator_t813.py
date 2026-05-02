"""Tests for ``TrainingOrchestrator`` — Phase 8 / T8.13 + T8.14 + T8.15.

End-to-end orchestrator runs against:

* Stub synthesizer that writes deterministic WAVs (or simulates
  cancellation/failure).
* Stub backend that returns a deterministic .onnx path (or
  simulates cancellation/failure).

State machine + JSONL progress + cancellation primitive +
hot-reload callback all exercised without real Kokoro / OpenWakeWord.
"""

from __future__ import annotations

import wave
from pathlib import Path
from typing import TYPE_CHECKING

import numpy as np
import pytest

from sovyx.voice.wake_word_training._orchestrator import (
    TrainingOrchestrator,
    TrainingRequest,
)
from sovyx.voice.wake_word_training._progress import ProgressTracker
from sovyx.voice.wake_word_training._state import TrainingStatus
from sovyx.voice.wake_word_training._synthesizer import (
    KokoroSampleSynthesizer,
)
from sovyx.voice.wake_word_training._trainer_protocol import (
    TrainingCancelledError,
)

if TYPE_CHECKING:
    from collections.abc import Callable


# ── Shared stubs ────────────────────────────────────────────────────


class _StubAudioChunk:
    def __init__(self, audio: np.ndarray, sample_rate: int) -> None:
        self.audio = audio
        self.sample_rate = sample_rate


class _StubTTS:
    """Returns deterministic 0.5 s int16 sine at 16 kHz."""

    async def synthesize_with(
        self,
        text: str,  # noqa: ARG002
        *,
        voice: str,  # noqa: ARG002
        language: str,  # noqa: ARG002
        speed: float | None = None,  # noqa: ARG002
    ) -> _StubAudioChunk:
        return _StubAudioChunk(
            np.zeros(8000, dtype=np.int16),
            sample_rate=16000,
        )


class _StubBackend:
    """Records calls + returns the requested ``output_path`` after
    invoking ``on_progress`` once. Tests inject behaviour via
    constructor flags."""

    def __init__(
        self,
        *,
        raise_cancelled: bool = False,
        raise_runtime: bool = False,
        check_cancel_during_train: bool = False,
    ) -> None:
        self.calls: list[dict[str, object]] = []
        self._raise_cancelled = raise_cancelled
        self._raise_runtime = raise_runtime
        self._check_cancel = check_cancel_during_train

    @property
    def name(self) -> str:
        return "stub"

    def train(
        self,
        *,
        wake_word: str,
        language: str,
        positive_samples: list[Path],
        negative_samples: list[Path],
        output_path: Path,
        on_progress: Callable[[float, str], None],
        cancel_check: Callable[[], bool],
    ) -> Path:
        self.calls.append(
            {
                "wake_word": wake_word,
                "language": language,
                "n_positive": len(positive_samples),
                "n_negative": len(negative_samples),
                "output_path": str(output_path),
            }
        )
        on_progress(0.5, "stub: halfway")
        if self._check_cancel and cancel_check():
            raise TrainingCancelledError("Cancelled mid-training")
        if self._raise_cancelled:
            raise TrainingCancelledError("Stub backend cancelled")
        if self._raise_runtime:
            msg = "Stub backend RuntimeError"
            raise RuntimeError(msg)
        on_progress(1.0, "stub: complete")
        # Touch the output file so callers can verify it exists.
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_bytes(b"fake-onnx-bytes")
        return output_path


def _seed_negatives(directory: Path, count: int = 3) -> None:
    """Create ``count`` empty WAV files in ``directory`` so the
    orchestrator's negative-sample collection succeeds."""
    directory.mkdir(parents=True, exist_ok=True)
    for i in range(count):
        path = directory / f"noise_{i:02d}.wav"
        with wave.open(str(path), "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(16000)
            wf.writeframes(np.zeros(8000, dtype=np.int16).tobytes())


def _make_request(
    *,
    job_root: Path,
    wake_word: str = "Lúcia",
    mind_id: str = "lucia",
    target_positive: int = 4,
    output_path: Path | None = None,
) -> TrainingRequest:
    return TrainingRequest(
        wake_word=wake_word,
        mind_id=mind_id,
        language="pt-BR",
        target_positive_samples=target_positive,
        synthesizer_voices=("v",),
        synthesizer_variants=("hi",),
        negative_samples_dir=job_root / "neg",
        output_path=output_path or (job_root / "out" / "lucia.onnx"),
    )


def _make_orchestrator(
    *,
    progress_path: Path,
    backend: _StubBackend | None = None,
    on_complete: Callable[[str, Path], None] | None = None,
) -> tuple[TrainingOrchestrator, _StubBackend, ProgressTracker]:
    backend = backend or _StubBackend()
    progress = ProgressTracker(progress_path)
    synth = KokoroSampleSynthesizer(tts=_StubTTS())
    orch = TrainingOrchestrator(
        synthesizer=synth,
        backend=backend,
        progress_tracker=progress,
        on_complete=on_complete,
    )
    return orch, backend, progress


# ── Happy path ──────────────────────────────────────────────────────


class TestHappyPath:
    @pytest.mark.asyncio
    async def test_complete_flow(self, tmp_path: Path) -> None:
        _seed_negatives(tmp_path / "neg", count=3)
        completions: list[tuple[str, Path]] = []

        def on_complete(mind_id: str, path: Path) -> None:
            completions.append((mind_id, path))

        orch, backend, progress = _make_orchestrator(
            progress_path=tmp_path / "progress.jsonl",
            on_complete=on_complete,
        )
        request = _make_request(job_root=tmp_path)

        final = await orch.run(request, job_dir=tmp_path / "job")

        # Final state is COMPLETE with the trained .onnx path.
        assert final.status is TrainingStatus.COMPLETE
        assert final.output_path == str(request.output_path)
        assert request.output_path.exists()

        # Backend was called with both positives + negatives.
        assert len(backend.calls) == 1
        assert backend.calls[0]["n_positive"] == request.target_positive_samples
        assert backend.calls[0]["n_negative"] == 3  # noqa: PLR2004
        assert backend.calls[0]["wake_word"] == request.wake_word

        # Hot-reload callback fired with the trained path.
        assert completions == [(request.mind_id, request.output_path)]

        # Progress log walks the canonical state graph.
        events = progress.read_all()
        statuses = [e.state.status for e in events]
        # Sequence: PENDING → SYNTHESIZING (multiple updates) →
        # TRAINING (multiple updates) → COMPLETE.
        assert statuses[0] is TrainingStatus.PENDING
        assert statuses[-1] is TrainingStatus.COMPLETE
        assert TrainingStatus.SYNTHESIZING in statuses
        assert TrainingStatus.TRAINING in statuses

    @pytest.mark.asyncio
    async def test_no_on_complete_callback_still_succeeds(
        self,
        tmp_path: Path,
    ) -> None:
        """When ``on_complete`` is None, COMPLETE state is still reached
        without raising."""
        _seed_negatives(tmp_path / "neg")
        orch, _, _ = _make_orchestrator(
            progress_path=tmp_path / "progress.jsonl",
            on_complete=None,
        )
        request = _make_request(job_root=tmp_path)
        final = await orch.run(request, job_dir=tmp_path / "job")
        assert final.status is TrainingStatus.COMPLETE

    @pytest.mark.asyncio
    async def test_on_complete_failure_does_not_roll_back_complete(
        self,
        tmp_path: Path,
    ) -> None:
        """Hot-reload callback failure must NOT change the persisted
        COMPLETE state — the trained .onnx is on disk regardless."""
        _seed_negatives(tmp_path / "neg")

        def crashing_callback(_mind_id: str, _path: Path) -> None:
            msg = "router unavailable"
            raise RuntimeError(msg)

        orch, _, progress = _make_orchestrator(
            progress_path=tmp_path / "progress.jsonl",
            on_complete=crashing_callback,
        )
        request = _make_request(job_root=tmp_path)
        final = await orch.run(request, job_dir=tmp_path / "job")
        assert final.status is TrainingStatus.COMPLETE
        # JSONL tail still says COMPLETE.
        events = progress.read_all()
        assert events[-1].state.status is TrainingStatus.COMPLETE


# ── Cancellation ────────────────────────────────────────────────────


class TestCancellation:
    @pytest.mark.asyncio
    async def test_cancel_before_synthesis(self, tmp_path: Path) -> None:
        """Cancel signal asserted at start → CANCELLED with no
        synthesizer/backend calls."""
        _seed_negatives(tmp_path / "neg")
        orch, backend, progress = _make_orchestrator(
            progress_path=tmp_path / "progress.jsonl",
        )
        request = _make_request(job_root=tmp_path)
        final = await orch.run(
            request,
            job_dir=tmp_path / "job",
            cancel_check=lambda: True,
        )
        assert final.status is TrainingStatus.CANCELLED
        assert backend.calls == []
        # JSONL: PENDING → CANCELLED only.
        events = progress.read_all()
        statuses = [e.state.status for e in events]
        assert statuses == [TrainingStatus.PENDING, TrainingStatus.CANCELLED]

    @pytest.mark.asyncio
    async def test_cancel_during_synthesis(self, tmp_path: Path) -> None:
        """Cancel signal during synthesizer loop → CANCELLED with
        partial sample count surfaced in message."""
        _seed_negatives(tmp_path / "neg")
        orch, backend, _ = _make_orchestrator(
            progress_path=tmp_path / "progress.jsonl",
        )
        request = _make_request(job_root=tmp_path, target_positive=10)
        # Cancel after ~3 polls (synthesizer polls before each sample).
        n = [0]

        def cancel_after_3() -> bool:
            n[0] += 1
            return n[0] > 3  # noqa: PLR2004

        final = await orch.run(
            request,
            job_dir=tmp_path / "job",
            cancel_check=cancel_after_3,
        )
        assert final.status is TrainingStatus.CANCELLED
        assert "Cancelled during synthesis" in final.message
        assert backend.calls == []  # never reached training

    @pytest.mark.asyncio
    async def test_cancel_during_training(self, tmp_path: Path) -> None:
        """Backend raises ``TrainingCancelledError`` → CANCELLED
        (distinct from FAILED)."""
        _seed_negatives(tmp_path / "neg")
        orch, _, progress = _make_orchestrator(
            progress_path=tmp_path / "progress.jsonl",
            backend=_StubBackend(raise_cancelled=True),
        )
        request = _make_request(job_root=tmp_path)
        final = await orch.run(request, job_dir=tmp_path / "job")
        assert final.status is TrainingStatus.CANCELLED
        events = progress.read_all()
        # Got past synthesis; reached TRAINING; ended CANCELLED.
        statuses = [e.state.status for e in events]
        assert TrainingStatus.TRAINING in statuses
        assert statuses[-1] is TrainingStatus.CANCELLED

    @pytest.mark.asyncio
    async def test_filesystem_cancel_signal(self, tmp_path: Path) -> None:
        """Default cancel_check polls ``<job_dir>/.cancel``."""
        _seed_negatives(tmp_path / "neg")
        orch, backend, _ = _make_orchestrator(
            progress_path=tmp_path / "progress.jsonl",
        )
        job_dir = tmp_path / "job"
        job_dir.mkdir()
        # Touch the cancel signal BEFORE invoking run().
        (job_dir / ".cancel").touch()
        request = _make_request(job_root=tmp_path)

        # No cancel_check provided → orchestrator builds the
        # filesystem one + reads our pre-touched file.
        final = await orch.run(request, job_dir=job_dir)
        assert final.status is TrainingStatus.CANCELLED
        assert backend.calls == []


# ── Failure paths ───────────────────────────────────────────────────


class TestFailurePaths:
    @pytest.mark.asyncio
    async def test_missing_negatives_dir_fails(self, tmp_path: Path) -> None:
        """Negative samples dir doesn't exist → FAILED with operator
        guidance in error_summary."""
        # NOTE: NOT calling _seed_negatives — directory absent.
        orch, backend, _ = _make_orchestrator(
            progress_path=tmp_path / "progress.jsonl",
        )
        request = _make_request(job_root=tmp_path)
        final = await orch.run(request, job_dir=tmp_path / "job")
        assert final.status is TrainingStatus.FAILED
        assert "Negative samples directory not found" in final.error_summary
        assert backend.calls == []

    @pytest.mark.asyncio
    async def test_empty_negatives_dir_fails(self, tmp_path: Path) -> None:
        """Empty negatives dir → FAILED with explicit guidance."""
        # Create the dir but no WAV files.
        (tmp_path / "neg").mkdir()
        orch, backend, _ = _make_orchestrator(
            progress_path=tmp_path / "progress.jsonl",
        )
        request = _make_request(job_root=tmp_path)
        final = await orch.run(request, job_dir=tmp_path / "job")
        assert final.status is TrainingStatus.FAILED
        assert "is empty" in final.error_summary
        assert backend.calls == []

    @pytest.mark.asyncio
    async def test_backend_runtime_error_fails(self, tmp_path: Path) -> None:
        """Backend raising arbitrary Exception → FAILED with
        error_summary."""
        _seed_negatives(tmp_path / "neg")
        orch, _, _ = _make_orchestrator(
            progress_path=tmp_path / "progress.jsonl",
            backend=_StubBackend(raise_runtime=True),
        )
        request = _make_request(job_root=tmp_path)
        final = await orch.run(request, job_dir=tmp_path / "job")
        assert final.status is TrainingStatus.FAILED
        assert "Backend.train raised" in final.error_summary
        assert "Stub backend RuntimeError" in final.error_summary


# ── Progress log integrity ──────────────────────────────────────────


class TestProgressLog:
    @pytest.mark.asyncio
    async def test_progress_log_has_per_sample_synth_entries(
        self,
        tmp_path: Path,
    ) -> None:
        _seed_negatives(tmp_path / "neg")
        orch, _, progress = _make_orchestrator(
            progress_path=tmp_path / "progress.jsonl",
        )
        request = _make_request(job_root=tmp_path, target_positive=5)
        await orch.run(request, job_dir=tmp_path / "job")

        events = progress.read_all()
        synthesizing_events = [e for e in events if e.state.status is TrainingStatus.SYNTHESIZING]
        # 1 transition entry + 5 per-sample updates + 1 phase-complete.
        # Bound the count loosely to avoid coupling to exact synthesizer
        # internals; we just want to see SOME granularity.
        assert len(synthesizing_events) >= 5  # noqa: PLR2004

        # Final SYNTHESIZING entry has progress=1.0 + samples_generated=5.
        last_synth = synthesizing_events[-1]
        assert last_synth.state.progress == 1.0
        assert last_synth.state.samples_generated == 5  # noqa: PLR2004

    @pytest.mark.asyncio
    async def test_progress_log_has_training_progress_updates(
        self,
        tmp_path: Path,
    ) -> None:
        _seed_negatives(tmp_path / "neg")
        orch, _, progress = _make_orchestrator(
            progress_path=tmp_path / "progress.jsonl",
        )
        request = _make_request(job_root=tmp_path)
        await orch.run(request, job_dir=tmp_path / "job")

        events = progress.read_all()
        training = [e for e in events if e.state.status is TrainingStatus.TRAINING]
        # Stub backend calls on_progress(0.5, ...) + on_progress(1.0, ...).
        # Plus the entry-transition and the negative-loaded message.
        assert len(training) >= 2  # noqa: PLR2004
        progresses = [e.state.progress for e in training]
        assert 0.5 in progresses or any(p > 0.4 for p in progresses)


# ── TrainingRequest immutability ────────────────────────────────────


class TestRequestImmutability:
    def test_request_is_frozen(self, tmp_path: Path) -> None:
        request = _make_request(job_root=tmp_path)
        with pytest.raises((AttributeError, TypeError)):
            request.wake_word = "tampered"  # type: ignore[misc]
