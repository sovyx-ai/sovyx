"""Tests for ``sovyx voice train-wake-word`` — Phase 8 / T8.13.

Backend-only — exercises the CLI command via Typer's CliRunner.
The orchestrator is the real one; the trainer backend is a stub
registered via ``register_default_backend`` (and reset between
tests). Kokoro is patched out because the CLI builds a real
``KokoroTTS`` instance.

Coverage:

* Validation: empty wake_word → exit 2; missing --negatives-dir
  → exit 2; non-ASCII-after-fold wake_word → exit 2.
* No backend registered → exit 1 with install hint.
* Happy path: COMPLETE message rendered + .onnx written +
  exit 0.
* CANCELLED + FAILED state rendering.
"""

from __future__ import annotations

import wave
from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import patch

import numpy as np
import pytest
from typer.testing import CliRunner

from sovyx.cli.main import app
from sovyx.voice.wake_word_training._trainer_protocol import (
    TrainingCancelledError,
    _reset_default_backend_for_tests,
    register_default_backend,
)

if TYPE_CHECKING:
    from collections.abc import Callable


runner = CliRunner()


# ── Stub backend ────────────────────────────────────────────────────


class _StubBackend:
    def __init__(
        self,
        *,
        raise_cancelled: bool = False,
        raise_runtime: bool = False,
    ) -> None:
        self._raise_cancelled = raise_cancelled
        self._raise_runtime = raise_runtime

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
        on_progress: Callable[[float, str], None],
        cancel_check: Callable[[], bool],  # noqa: ARG002
    ) -> Path:
        on_progress(0.5, "halfway")
        if self._raise_cancelled:
            raise TrainingCancelledError("stub cancelled")
        if self._raise_runtime:
            msg = "stub failure"
            raise RuntimeError(msg)
        on_progress(1.0, "complete")
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_bytes(b"fake-onnx")
        return output_path


def _seed_negatives(directory: Path, count: int = 2) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    for i in range(count):
        path = directory / f"n_{i}.wav"
        with wave.open(str(path), "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(16000)
            wf.writeframes(np.zeros(8000, dtype=np.int16).tobytes())


class _StubAudioChunk:
    def __init__(self) -> None:
        self.audio = np.zeros(8000, dtype=np.int16)
        self.sample_rate = 16000


class _StubKokoroTTS:
    """Minimal stub that mimics KokoroTTS for the synthesizer."""

    def __init__(self, model_dir: Path) -> None:  # noqa: ARG002
        pass

    async def synthesize_with(
        self,
        text: str,  # noqa: ARG002
        *,
        voice: str,  # noqa: ARG002
        language: str,  # noqa: ARG002
        speed: float | None = None,  # noqa: ARG002
    ) -> _StubAudioChunk:
        return _StubAudioChunk()


# ── Fixtures: reset backend registry between tests ──────────────────


@pytest.fixture(autouse=True)
def _reset_backend() -> None:
    _reset_default_backend_for_tests()
    yield
    _reset_default_backend_for_tests()


# ── Validation ──────────────────────────────────────────────────────


class TestValidation:
    def test_empty_wake_word_rejected(self, tmp_path: Path) -> None:
        register_default_backend(_StubBackend())
        result = runner.invoke(
            app,
            [
                "voice",
                "train-wake-word",
                "  ",
                "--negatives-dir",
                str(tmp_path / "neg"),
            ],
        )
        assert result.exit_code == 2  # noqa: PLR2004
        assert "non-empty" in result.stdout

    def test_missing_negatives_dir_rejected(self) -> None:
        register_default_backend(_StubBackend())
        result = runner.invoke(
            app,
            ["voice", "train-wake-word", "Lúcia"],
        )
        assert result.exit_code == 2  # noqa: PLR2004
        assert "negatives-dir" in result.stdout

    def test_no_backend_registered(self, tmp_path: Path) -> None:
        # backend reset by autouse fixture; don't register one.
        result = runner.invoke(
            app,
            [
                "voice",
                "train-wake-word",
                "Lúcia",
                "--negatives-dir",
                str(tmp_path / "neg"),
            ],
        )
        assert result.exit_code == 1
        assert "Trainer backend unavailable" in result.stdout

    def test_non_ascii_wake_word_after_fold_rejected(self, tmp_path: Path) -> None:
        """Wake word with no ASCII characters after fold (e.g.
        Chinese-only) produces empty job-id → rejected."""
        register_default_backend(_StubBackend())
        result = runner.invoke(
            app,
            [
                "voice",
                "train-wake-word",
                "你好",  # all-Chinese, ASCII-fold yields empty
                "--negatives-dir",
                str(tmp_path / "neg"),
            ],
        )
        assert result.exit_code == 2  # noqa: PLR2004
        assert "ASCII" in result.stdout


# ── Happy path ──────────────────────────────────────────────────────


class TestHappyPath:
    def test_complete_run(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        register_default_backend(_StubBackend())
        _seed_negatives(tmp_path / "neg")
        output = tmp_path / "out" / "lucia.onnx"

        # Steer EngineConfig.data_dir via the SOVYX_DATA_DIR env var
        # so the CLI's training-root resolution lands inside tmp_path
        # instead of the operator's real home directory.
        # SOVYX_DATABASE__DATA_DIR mirrors EngineConfig's nested-env
        # lookup so DatabaseConfig.data_dir inherits the same value.
        monkeypatch.setenv("SOVYX_DATA_DIR", str(tmp_path))
        monkeypatch.setenv("SOVYX_DATABASE__DATA_DIR", str(tmp_path))

        with patch("sovyx.voice.tts_kokoro.KokoroTTS", _StubKokoroTTS):
            try:
                result = runner.invoke(
                    app,
                    [
                        "voice",
                        "train-wake-word",
                        "Lucia",
                        "--target-samples",
                        "12",  # CLI enforces min=10
                        "--negatives-dir",
                        str(tmp_path / "neg"),
                        "--output",
                        str(output),
                        "--language",
                        "pt-BR",
                        "--voices",
                        "v",
                    ],
                    catch_exceptions=False,
                )
            except SystemExit as exc:
                pytest.fail(
                    f"SystemExit({exc.code}) — typer / click usage error. "
                    f"Argument schema may have drifted.",
                )

        debug_msg = f"exit={result.exit_code} stdout={result.stdout!r} output={result.output!r}"
        assert result.exit_code == 0, debug_msg
        assert "Training complete" in result.stdout
        assert output.exists()


# ── Cancelled / Failed paths ────────────────────────────────────────


class TestCancelledFailedPaths:
    def _common_invoke(
        self,
        tmp_path: Path,
        backend: _StubBackend,
        monkeypatch: pytest.MonkeyPatch,
    ) -> object:
        register_default_backend(backend)
        _seed_negatives(tmp_path / "neg")
        monkeypatch.setenv("SOVYX_DATA_DIR", str(tmp_path))
        monkeypatch.setenv("SOVYX_DATABASE__DATA_DIR", str(tmp_path))

        with patch("sovyx.voice.tts_kokoro.KokoroTTS", _StubKokoroTTS):
            return runner.invoke(
                app,
                [
                    "voice",
                    "train-wake-word",
                    "Test",
                    "--target-samples",
                    "10",  # CLI enforces min=10
                    "--negatives-dir",
                    str(tmp_path / "neg"),
                    "--output",
                    str(tmp_path / "out.onnx"),
                    "--voices",
                    "v",
                ],
            )

    def test_cancelled_backend_renders_yellow_message(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        result = self._common_invoke(
            tmp_path,
            _StubBackend(raise_cancelled=True),
            monkeypatch,
        )
        assert result.exit_code == 1
        assert "Cancelled" in result.stdout

    def test_failed_backend_renders_error(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        result = self._common_invoke(
            tmp_path,
            _StubBackend(raise_runtime=True),
            monkeypatch,
        )
        assert result.exit_code == 1
        assert "Failed" in result.stdout
        assert "stub failure" in result.stdout


# ── Helper unit tests ───────────────────────────────────────────────


class TestSlugifyHelper:
    def test_diacritics_stripped(self) -> None:
        from sovyx.cli.commands.voice import _slugify_for_filesystem

        assert _slugify_for_filesystem("Lúcia") == "lucia"
        assert _slugify_for_filesystem("Müller") == "muller"
        assert _slugify_for_filesystem("Joaquín") == "joaquin"

    def test_non_ascii_returns_underscores(self) -> None:
        from sovyx.cli.commands.voice import _slugify_for_filesystem

        # All-Chinese folds to underscores.
        assert _slugify_for_filesystem("你好") == "__"

    def test_truncates_at_48(self) -> None:
        from sovyx.cli.commands.voice import _slugify_for_filesystem

        assert len(_slugify_for_filesystem("a" * 100)) == 48  # noqa: PLR2004
