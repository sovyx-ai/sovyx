"""Tests for :mod:`sovyx.voice.health._file_lock`."""

from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

import pytest

from sovyx.voice.health._file_lock import (
    FileLockTimeoutError,
    acquire_file_lock,
)


class TestAcquireBasic:
    """Happy-path single-process acquisition."""

    def test_acquires_and_releases(self, tmp_path: Path) -> None:
        lock_path = tmp_path / "x.lock"
        with acquire_file_lock(lock_path):
            assert lock_path.exists()
        # Re-acquiring after release must work immediately.
        with acquire_file_lock(lock_path, timeout_s=0.2):
            pass

    def test_creates_parent_dir(self, tmp_path: Path) -> None:
        lock_path = tmp_path / "new" / "nested" / "x.lock"
        with acquire_file_lock(lock_path):
            assert lock_path.parent.is_dir()

    def test_releases_on_exception(self, tmp_path: Path) -> None:
        lock_path = tmp_path / "x.lock"
        with pytest.raises(RuntimeError), acquire_file_lock(lock_path):  # noqa: PT012
            raise RuntimeError("boom")
        # Must be re-acquirable immediately.
        with acquire_file_lock(lock_path, timeout_s=0.2):
            pass


class TestTimeout:
    """Contention and timeout semantics."""

    def test_second_acquire_times_out_while_held(self, tmp_path: Path) -> None:
        lock_path = tmp_path / "x.lock"
        held = threading.Event()
        release = threading.Event()
        errors: list[BaseException] = []

        def holder() -> None:
            try:
                with acquire_file_lock(lock_path):
                    held.set()
                    release.wait(timeout=5.0)
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)

        t = threading.Thread(target=holder, daemon=True)
        t.start()
        assert held.wait(timeout=2.0)
        try:
            with (  # noqa: PT012
                pytest.raises(FileLockTimeoutError) as exc_info,
                acquire_file_lock(lock_path, timeout_s=0.2),
            ):
                pytest.fail("should not acquire while held")
            assert exc_info.value.path == lock_path
            assert exc_info.value.timeout_s == 0.2
            assert "could not acquire" in str(exc_info.value)
        finally:
            release.set()
            t.join(timeout=5.0)
        assert errors == []

    def test_second_acquire_succeeds_after_release(self, tmp_path: Path) -> None:
        lock_path = tmp_path / "x.lock"
        # Short hold in another thread, then long timeout in the main thread.
        released = threading.Event()

        def holder() -> None:
            with acquire_file_lock(lock_path):
                time.sleep(0.05)
            released.set()

        t = threading.Thread(target=holder, daemon=True)
        t.start()
        t.join(timeout=5.0)
        assert released.is_set()
        with acquire_file_lock(lock_path, timeout_s=1.0):
            pass


class TestPosixOnly:
    """POSIX-specific permission tightening."""

    @pytest.mark.skipif(sys.platform == "win32", reason="POSIX only")
    def test_lock_file_is_chmod_600(self, tmp_path: Path) -> None:
        lock_path = tmp_path / "x.lock"
        with acquire_file_lock(lock_path):
            mode = lock_path.stat().st_mode & 0o777
            assert mode == 0o600


class TestWindowsOnly:
    """Windows-specific — msvcrt path covered by the base tests."""

    @pytest.mark.skipif(sys.platform != "win32", reason="Windows only")
    def test_windows_acquires_and_releases(self, tmp_path: Path) -> None:
        lock_path = tmp_path / "x.lock"
        with acquire_file_lock(lock_path):
            pass
        with acquire_file_lock(lock_path, timeout_s=0.2):
            pass
