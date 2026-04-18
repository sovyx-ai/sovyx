"""Tests for ``sovyx.voice.model_status`` — disk-truth + download orchestration."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from sovyx.voice import model_status
from sovyx.voice.model_registry import VOICE_MODELS
from sovyx.voice.model_status import (
    _RELATIVE_PATHS,
    ModelDownloadProgress,
    VoiceModelDiskStatus,
    _DownloadEntry,
    check_voice_models_status,
    collect_missing_models,
    prune_finished,
    start_download,
)


class TestCheckVoiceModelsStatus:
    """``check_voice_models_status`` reports disk presence honestly."""

    def test_all_missing_when_directory_empty(self, tmp_path: Path) -> None:
        status = check_voice_models_status(tmp_path)

        assert status.model_dir == str(tmp_path)
        assert status.all_installed is False
        # Every downloadable registry entry is counted as missing.
        downloadable = [m for m in VOICE_MODELS.values() if m.download_available]
        # Subtract any registry entries with no disk-path contract (e.g. moonshine).
        with_path = [m for m in downloadable if m.name in _RELATIVE_PATHS]
        assert status.missing_count == len(with_path)
        assert all(not e.installed for e in status.models)

    def test_installed_when_file_present(self, tmp_path: Path) -> None:
        # Touch silero_vad.onnx with a tiny payload.
        (tmp_path / "silero_vad.onnx").write_bytes(b"\x00" * 1024)

        status = check_voice_models_status(tmp_path)
        silero = next(e for e in status.models if e.name == "silero-vad-v5")
        assert silero.installed is True
        assert silero.size_mb >= 0.0
        assert "silero_vad.onnx" in silero.path

    def test_kokoro_paths_are_nested_under_subdir(self, tmp_path: Path) -> None:
        (tmp_path / "kokoro").mkdir()
        (tmp_path / "kokoro" / "kokoro-v1.0.int8.onnx").write_bytes(b"X")
        (tmp_path / "kokoro" / "voices-v1.0.bin").write_bytes(b"X")

        status = check_voice_models_status(tmp_path)
        kokoro = next(e for e in status.models if e.name == "kokoro-v1.0-int8")
        voices = next(e for e in status.models if e.name == "kokoro-voices-v1.0")

        assert kokoro.installed is True
        assert voices.installed is True
        # all_installed also depends on silero — not present here.
        assert status.all_installed is False

    def test_expected_filename_matches_registry(self, tmp_path: Path) -> None:
        # Regression: the filename the UI reports must match the filename
        # the KokoroTTS loader looks for. The historical drift was
        # ``kokoro-v1.0-q8.onnx`` (wrong) vs ``kokoro-v1.0.int8.onnx`` (right).
        status = check_voice_models_status(tmp_path)
        kokoro = next(e for e in status.models if e.name == "kokoro-v1.0-int8")
        assert "kokoro-v1.0.int8.onnx" in kokoro.path
        assert "q8" not in kokoro.path

    def test_zero_byte_file_is_not_installed(self, tmp_path: Path) -> None:
        """A 0-byte partial is NOT installed — regression for wizard false-positive.

        A download that crashes between ``tempfile.mkstemp`` and the first
        write leaves a zero-length file at the expected path. ``path.exists()``
        returns True, so a naive check flips the model to "installed ✓" in
        the wizard. The ONNX runtime then raises an opaque parse error at
        load time and the user has no recovery CTA. Require non-zero size.
        """
        (tmp_path / "silero_vad.onnx").write_bytes(b"")  # touch -- 0 bytes

        status = check_voice_models_status(tmp_path)
        silero = next(e for e in status.models if e.name == "silero-vad-v5")
        assert silero.installed is False
        assert silero.size_mb == 0.0
        assert status.missing_count >= 1

    def test_all_installed_flips_to_true(self, tmp_path: Path) -> None:
        (tmp_path / "silero_vad.onnx").write_bytes(b"X")
        (tmp_path / "kokoro").mkdir()
        (tmp_path / "kokoro" / "kokoro-v1.0.int8.onnx").write_bytes(b"X")
        (tmp_path / "kokoro" / "voices-v1.0.bin").write_bytes(b"X")

        status = check_voice_models_status(tmp_path)
        # moonshine-tiny has download_available=False and no disk path,
        # so it is reported but not counted as missing.
        assert status.missing_count == 0
        assert status.all_installed is True


class TestCollectMissingModels:
    def test_includes_only_downloadable_missing(self, tmp_path: Path) -> None:
        missing = collect_missing_models(tmp_path)
        names = {m.name for m in missing}
        assert "silero-vad-v5" in names
        assert "kokoro-v1.0-int8" in names
        assert "kokoro-voices-v1.0" in names
        # moonshine-tiny has download_available=False — excluded.
        assert "moonshine-tiny" not in names

    def test_installed_models_omitted(self, tmp_path: Path) -> None:
        (tmp_path / "silero_vad.onnx").write_bytes(b"X")
        missing = collect_missing_models(tmp_path)
        names = {m.name for m in missing}
        assert "silero-vad-v5" not in names


class TestStartDownload:
    """Background download orchestration."""

    def test_noop_when_nothing_missing(self, tmp_path: Path) -> None:
        # Pre-populate every expected file so the resolver has nothing to do.
        (tmp_path / "silero_vad.onnx").write_bytes(b"X")
        (tmp_path / "kokoro").mkdir()
        (tmp_path / "kokoro" / "kokoro-v1.0.int8.onnx").write_bytes(b"X")
        (tmp_path / "kokoro" / "voices-v1.0.bin").write_bytes(b"X")

        tracker: dict[str, _DownloadEntry] = {}
        entry = start_download(tracker, model_dir=tmp_path)
        assert entry.progress.status == "done"
        assert entry.progress.total_models == 0
        assert entry.task is None

    @pytest.mark.asyncio()
    async def test_single_flight_returns_existing(self, tmp_path: Path) -> None:
        # Create a running task that never completes, then try to start again.
        running = ModelDownloadProgress(
            task_id="existing",
            status="running",
            total_models=2,
            completed_models=0,
            current_model="silero-vad-v5",
            error=None,
            created_at=0.0,
        )
        tracker: dict[str, _DownloadEntry] = {"existing": _DownloadEntry(progress=running)}
        entry = start_download(tracker, model_dir=tmp_path)
        assert entry.progress.task_id == "existing"
        assert len(tracker) == 1

    @pytest.mark.asyncio()
    async def test_spawns_task_and_completes(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        calls: list[str] = []

        async def fake_silero(model_dir: Path) -> Path:
            calls.append("silero")
            return model_dir / "silero_vad.onnx"

        async def fake_kokoro(model_dir: Path) -> Path:
            calls.append("kokoro")
            return model_dir / "kokoro"

        monkeypatch.setattr(model_status, "ensure_silero_vad", fake_silero)
        monkeypatch.setattr(model_status, "ensure_kokoro_tts", fake_kokoro)

        tracker: dict[str, _DownloadEntry] = {}
        entry = start_download(tracker, model_dir=tmp_path)
        assert entry.task is not None
        await entry.task

        assert entry.progress.status == "done"
        # Both helpers ran.
        assert "silero" in calls
        assert "kokoro" in calls
        # Completed count equals the number of missing entries (3:
        # silero + kokoro model + kokoro voices).
        assert entry.progress.completed_models == 3  # noqa: PLR2004
        assert entry.progress.error is None

    @pytest.mark.asyncio()
    async def test_error_propagates_to_progress(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        async def boom_silero(model_dir: Path) -> Path:
            msg = "network down"
            raise RuntimeError(msg)

        async def fake_kokoro(model_dir: Path) -> Path:
            return model_dir / "kokoro"

        monkeypatch.setattr(model_status, "ensure_silero_vad", boom_silero)
        monkeypatch.setattr(model_status, "ensure_kokoro_tts", fake_kokoro)

        tracker: dict[str, _DownloadEntry] = {}
        entry = start_download(tracker, model_dir=tmp_path)
        assert entry.task is not None
        await entry.task

        assert entry.progress.status == "error"
        assert entry.progress.error == "network down"
        # No partial advance on the failing helper.
        assert entry.progress.completed_models == 0


class TestPruneFinished:
    def test_keeps_recent_entries(self) -> None:
        fresh = ModelDownloadProgress(
            task_id="fresh",
            status="done",
            total_models=1,
            completed_models=1,
            current_model=None,
            error=None,
            created_at=0.0,
            finished_at=asyncio.get_event_loop_policy().new_event_loop().time(),
        )
        tracker: dict[str, _DownloadEntry] = {"fresh": _DownloadEntry(progress=fresh)}
        prune_finished(tracker, ttl_s=60.0)
        assert "fresh" in tracker

    def test_drops_stale_entries(self) -> None:
        stale = ModelDownloadProgress(
            task_id="stale",
            status="done",
            total_models=1,
            completed_models=1,
            current_model=None,
            error=None,
            created_at=0.0,
            finished_at=0.0,  # very old
        )
        tracker: dict[str, _DownloadEntry] = {"stale": _DownloadEntry(progress=stale)}
        prune_finished(tracker, ttl_s=0.001)
        assert "stale" not in tracker


class TestVoiceModelDiskStatusContract:
    def test_fields_are_json_serialisable_primitives(self, tmp_path: Path) -> None:
        status = check_voice_models_status(tmp_path)
        # Each field must be a primitive the UI's zod schema accepts.
        for m in status.models:
            assert isinstance(m, VoiceModelDiskStatus)
            assert isinstance(m.name, str)
            assert isinstance(m.installed, bool)
            assert isinstance(m.size_mb, float)
            assert isinstance(m.expected_size_mb, float)
            assert isinstance(m.download_available, bool)
