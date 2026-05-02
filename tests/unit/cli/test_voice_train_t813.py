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


# ── _attempt_hot_reload — Phase 8 / T8.15 CLI wire-up ────────────────


class TestAttemptHotReload:
    """``_attempt_hot_reload`` is the CLI's post-training hook into
    the daemon's ``wake_word.register_mind`` RPC. Best-effort: every
    failure mode falls through to the operator-restart path with a
    clear hint, and NEVER aborts (training already succeeded — model
    is on disk).
    """

    def test_daemon_not_running_renders_restart_hint(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        from sovyx.cli.commands.voice import _attempt_hot_reload

        with patch(
            "sovyx.cli.rpc_client.DaemonClient.is_daemon_running",
            return_value=False,
        ):
            _attempt_hot_reload("lucia", tmp_path / "lucia.onnx")

        out = capsys.readouterr().out
        assert "Daemon not running" in out
        assert "restart" in out.lower()

    def test_daemon_success_renders_green_confirmation(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        from sovyx.cli.commands.voice import _attempt_hot_reload

        async def _fake_call(*_args: object, **_kw: object) -> dict[str, object]:
            return {
                "mind_id": "lucia",
                "model_path": str(tmp_path / "lucia.onnx"),
                "hot_reload_succeeded": True,
            }

        with (
            patch(
                "sovyx.cli.rpc_client.DaemonClient.is_daemon_running",
                return_value=True,
            ),
            patch("sovyx.cli.rpc_client.DaemonClient.call", side_effect=_fake_call),
        ):
            _attempt_hot_reload("lucia", tmp_path / "lucia.onnx")

        out = capsys.readouterr().out
        assert "Hot-reloaded" in out
        assert "lucia" in out

    def test_channel_connection_error_renders_remediation(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        from sovyx.cli.commands.voice import _attempt_hot_reload
        from sovyx.engine.errors import ChannelConnectionError

        async def _fake_call(*_args: object, **_kw: object) -> object:
            msg = "RPC error (-32001): voice subsystem not enabled"
            raise ChannelConnectionError(msg)

        with (
            patch(
                "sovyx.cli.rpc_client.DaemonClient.is_daemon_running",
                return_value=True,
            ),
            patch("sovyx.cli.rpc_client.DaemonClient.call", side_effect=_fake_call),
        ):
            _attempt_hot_reload("lucia", tmp_path / "lucia.onnx")

        out = capsys.readouterr().out
        assert "Hot-reload via daemon failed" in out
        assert "voice subsystem not enabled" in out
        assert "Restart the daemon" in out

    def test_unexpected_response_shape_falls_through_to_warning(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        from sovyx.cli.commands.voice import _attempt_hot_reload

        async def _fake_call(*_args: object, **_kw: object) -> object:
            # Daemon protocol drift / malformed response — the helper
            # MUST surface this rather than pretend success.
            return {"unexpected": "shape"}

        with (
            patch(
                "sovyx.cli.rpc_client.DaemonClient.is_daemon_running",
                return_value=True,
            ),
            patch("sovyx.cli.rpc_client.DaemonClient.call", side_effect=_fake_call),
        ):
            _attempt_hot_reload("lucia", tmp_path / "lucia.onnx")

        out = capsys.readouterr().out
        assert "unexpected response" in out

    def test_arbitrary_exception_does_not_propagate(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """Defence-in-depth — even an unforeseen exception type must
        fall through. The CLI exits 0 (training succeeded); hot-reload
        is convenience, never a correctness gate."""
        from sovyx.cli.commands.voice import _attempt_hot_reload

        async def _fake_call(*_args: object, **_kw: object) -> object:
            raise RuntimeError("unexpected runtime")

        with (
            patch(
                "sovyx.cli.rpc_client.DaemonClient.is_daemon_running",
                return_value=True,
            ),
            patch("sovyx.cli.rpc_client.DaemonClient.call", side_effect=_fake_call),
        ):
            # Must NOT raise.
            _attempt_hot_reload("lucia", tmp_path / "lucia.onnx")

        out = capsys.readouterr().out
        assert "Hot-reload error" in out
        assert "unexpected runtime" in out


# ── End-to-end hot-reload wire through the CLI command ──────────────


class TestTrainCommandHotReload:
    """End-to-end: ``sovyx voice train-wake-word --mind-id=...`` after
    successful training calls the daemon's ``wake_word.register_mind``
    RPC. Validates the message rendering + the call path. The daemon
    is mocked at the DaemonClient layer."""

    def test_no_mind_id_skips_hot_reload_entirely(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        register_default_backend(_StubBackend())
        _seed_negatives(tmp_path / "neg")
        output = tmp_path / "out" / "x.onnx"
        monkeypatch.setenv("SOVYX_DATA_DIR", str(tmp_path))
        monkeypatch.setenv("SOVYX_DATABASE__DATA_DIR", str(tmp_path))

        with (
            patch("sovyx.voice.tts_kokoro.KokoroTTS", _StubKokoroTTS),
            patch(
                "sovyx.cli.rpc_client.DaemonClient.is_daemon_running",
                return_value=True,
            ) as is_running_mock,
        ):
            result = runner.invoke(
                app,
                [
                    "voice",
                    "train-wake-word",
                    "Lucia",
                    "--target-samples",
                    "10",
                    "--negatives-dir",
                    str(tmp_path / "neg"),
                    "--output",
                    str(output),
                    "--voices",
                    "v",
                ],
                catch_exceptions=False,
            )

        assert result.exit_code == 0
        # Without --mind-id, the helper is never called → daemon
        # probe never runs.
        assert is_running_mock.call_count == 0
        # Rich may word-wrap the rendered line; assert on a stable
        # substring that survives wrap.
        assert "Trained without --mind-id" in result.stdout
        assert "next restart" in result.stdout

    def test_with_mind_id_and_daemon_running_calls_rpc(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        register_default_backend(_StubBackend())
        _seed_negatives(tmp_path / "neg")
        output = tmp_path / "out" / "lucia.onnx"
        monkeypatch.setenv("SOVYX_DATA_DIR", str(tmp_path))
        monkeypatch.setenv("SOVYX_DATABASE__DATA_DIR", str(tmp_path))

        captured: dict[str, object] = {}

        async def _fake_call(method: str, params: dict[str, object]) -> dict[str, object]:
            captured["method"] = method
            captured["params"] = params
            return {
                "mind_id": params["mind_id"],
                "model_path": params["model_path"],
                "hot_reload_succeeded": True,
            }

        with (
            patch("sovyx.voice.tts_kokoro.KokoroTTS", _StubKokoroTTS),
            patch(
                "sovyx.cli.rpc_client.DaemonClient.is_daemon_running",
                return_value=True,
            ),
            patch("sovyx.cli.rpc_client.DaemonClient.call", side_effect=_fake_call),
        ):
            result = runner.invoke(
                app,
                [
                    "voice",
                    "train-wake-word",
                    "Lucia",
                    "--mind-id",
                    "lucia",
                    "--target-samples",
                    "10",
                    "--negatives-dir",
                    str(tmp_path / "neg"),
                    "--output",
                    str(output),
                    "--voices",
                    "v",
                ],
                catch_exceptions=False,
            )

        assert result.exit_code == 0
        assert captured["method"] == "wake_word.register_mind"
        params = captured["params"]
        assert isinstance(params, dict)
        assert params["mind_id"] == "lucia"
        assert params["model_path"] == str(output)
        assert "Hot-reloaded" in result.stdout

    def test_with_mind_id_and_daemon_down_renders_restart_hint(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        register_default_backend(_StubBackend())
        _seed_negatives(tmp_path / "neg")
        output = tmp_path / "out" / "lucia.onnx"
        monkeypatch.setenv("SOVYX_DATA_DIR", str(tmp_path))
        monkeypatch.setenv("SOVYX_DATABASE__DATA_DIR", str(tmp_path))

        with (
            patch("sovyx.voice.tts_kokoro.KokoroTTS", _StubKokoroTTS),
            patch(
                "sovyx.cli.rpc_client.DaemonClient.is_daemon_running",
                return_value=False,
            ),
        ):
            result = runner.invoke(
                app,
                [
                    "voice",
                    "train-wake-word",
                    "Lucia",
                    "--mind-id",
                    "lucia",
                    "--target-samples",
                    "10",
                    "--negatives-dir",
                    str(tmp_path / "neg"),
                    "--output",
                    str(output),
                    "--voices",
                    "v",
                ],
                catch_exceptions=False,
            )

        # Training still succeeded (exit 0); restart hint surfaces.
        assert result.exit_code == 0
        assert "Daemon not running" in result.stdout
