"""Unit tests for sovyx.voice.diagnostics._runner.run_full_diag (sync + async).

The bash diag toolkit is interactive (8-12 min, asks the operator to
speak). These tests mock the subprocess + filesystem layers so they
run in milliseconds and exercise the orchestration logic without
actually invoking bash. End-to-end exercises happen in the operator's
Linux environment via ``sovyx doctor voice --full-diag``.

Coverage:
* prerequisite enforcement (Linux-only, bash 4+)
* extraction of bash from ``importlib.resources`` to a temp dir
* async subprocess invocation contract (cmd shape, ``--yes`` plus
  optional ``extra_args``)
* result tarball glob under ``output_root``
* failure paths -- non-zero exit, missing tarball, missing bash
* cleanup invariant -- temp script dir always removed
* P2 cancellation: SIGTERM → grace → SIGKILL escalation; graceful
  exit during grace; cancel telemetry events fire correctly
"""

from __future__ import annotations

import asyncio
import subprocess
from dataclasses import FrozenInstanceError
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from sovyx.voice.diagnostics import (
    DiagPrerequisiteError,
    DiagRunError,
    DiagRunResult,
    _runner,
    run_full_diag,
    run_full_diag_async,
)

# ════════════════════════════════════════════════════════════════════
# Helpers
# ════════════════════════════════════════════════════════════════════


class _CompletedProcessStub:
    """Minimal subprocess.CompletedProcess shim for the bash-version probe."""

    def __init__(self, returncode: int, stdout: str = "", stderr: str = "") -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


class _FakeAsyncProcess:
    """Async subprocess shim returned by mocked create_subprocess_exec.

    Behaviour matrix used across tests:

    * ``returncode`` not None at construction → ``wait()`` returns it
      immediately (graceful happy path).
    * ``ignore_sigterm=True`` → ``terminate()`` is a no-op; the test
      asserts the runner escalates to ``kill()`` after the grace
      period.
    * ``hang_seconds`` → ``wait()`` sleeps that long before resolving;
      enables tests that cancel mid-run.
    """

    def __init__(
        self,
        *,
        pid: int = 12345,
        returncode: int = 0,
        ignore_sigterm: bool = False,
        hang_seconds: float = 0.0,
    ) -> None:
        self.pid = pid
        self._returncode = returncode
        self.returncode: int | None = None  # populated on wait()
        self._ignore_sigterm = ignore_sigterm
        self._hang_seconds = hang_seconds
        self._terminate_called = False
        self._kill_called = False
        self._terminated_event = asyncio.Event()

    async def wait(self) -> int:
        if self._hang_seconds > 0:
            import contextlib

            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._terminated_event.wait(), timeout=self._hang_seconds)
        else:
            # Yield once so awaiters that don't actually need to wait
            # still see the event-loop tick.
            await asyncio.sleep(0)
        self.returncode = self._returncode
        return self._returncode

    def terminate(self) -> None:
        self._terminate_called = True
        if not self._ignore_sigterm:
            self._terminated_event.set()

    def kill(self) -> None:
        self._kill_called = True
        self._terminated_event.set()

    @property
    def terminate_called(self) -> bool:
        return self._terminate_called

    @property
    def kill_called(self) -> bool:
        return self._kill_called


def _async_proc_factory(
    *,
    returncode: int = 0,
    ignore_sigterm: bool = False,
    hang_seconds: float = 0.0,
    captured_cmd: list[list[str]] | None = None,
) -> Any:
    """Return a side_effect for asyncio.create_subprocess_exec."""

    async def _factory(*args: Any, **_kwargs: Any) -> _FakeAsyncProcess:
        if captured_cmd is not None:
            captured_cmd.append(list(args))
        return _FakeAsyncProcess(
            returncode=returncode,
            ignore_sigterm=ignore_sigterm,
            hang_seconds=hang_seconds,
        )

    return _factory


def _make_result_tarball(home: Path, *, mtime: float | None = None) -> Path:
    """Create a synthetic ``$HOME/sovyx-diag-X/sovyx-voice-diag_X.tar.gz``."""
    diag_dir = home / "sovyx-diag-host-20260505T160000Z-deadbeef"
    diag_dir.mkdir(parents=True, exist_ok=True)
    tarball = diag_dir / "sovyx-voice-diag_host_20260505T160000Z_deadbeef.tar.gz"
    tarball.write_bytes(b"\x1f\x8b\x08\x00synthetic-gzip-header")
    if mtime is not None:
        import os

        os.utime(tarball, (mtime, mtime))
    return tarball


def _stub_extract_to(target_with_script: Path) -> Any:
    """Build a side_effect for _extract_bash_to_temp returning a prebuilt dir."""

    def side_effect() -> Path:
        return target_with_script

    return side_effect


def _build_extracted(tmp_path: Path) -> Path:
    extracted = tmp_path / "extracted"
    extracted.mkdir()
    (extracted / "sovyx-voice-diag.sh").write_text("#!/usr/bin/env bash\nexit 0\n")
    return extracted


def _build_output_root(tmp_path: Path, *, with_tarball: bool = True) -> Path:
    output_root = tmp_path / "home"
    output_root.mkdir()
    if with_tarball:
        _make_result_tarball(output_root)
    return output_root


# ════════════════════════════════════════════════════════════════════
# Prerequisite checks (sync entry point exercises these via asyncio.run)
# ════════════════════════════════════════════════════════════════════


class TestPrerequisiteChecks:
    """Linux-only + bash 4+ are enforced before any subprocess invocation."""

    def test_non_linux_raises_prerequisite_error(self) -> None:
        with (
            patch.object(_runner.sys, "platform", "win32"),
            pytest.raises(DiagPrerequisiteError, match="Linux-only"),
        ):
            run_full_diag()

    def test_missing_bash_raises_prerequisite_error(self) -> None:
        with (
            patch.object(_runner.sys, "platform", "linux"),
            patch.object(_runner.shutil, "which", return_value=None),
            pytest.raises(DiagPrerequisiteError, match="bash is not installed"),
        ):
            run_full_diag()

    def test_bash_below_4_raises_prerequisite_error(self) -> None:
        with (
            patch.object(_runner.sys, "platform", "linux"),
            patch.object(_runner.shutil, "which", return_value="/bin/bash"),
            patch.object(
                _runner,
                "_read_bash_major_version",
                return_value=3,
            ),
            pytest.raises(DiagPrerequisiteError, match=r"bash 4\+ required"),
        ):
            run_full_diag()

    def test_bash_version_unparseable_treated_as_below_4(self) -> None:
        with patch.object(
            _runner.subprocess,
            "run",
            return_value=_CompletedProcessStub(returncode=0, stdout=""),
        ):
            assert _runner._read_bash_major_version("/bin/bash") == 0

    def test_bash_version_subprocess_timeout_returns_zero(self) -> None:
        def raise_timeout(*_args: Any, **_kwargs: Any) -> None:
            raise subprocess.TimeoutExpired(cmd="bash", timeout=5.0)

        with patch.object(_runner.subprocess, "run", side_effect=raise_timeout):
            assert _runner._read_bash_major_version("/bin/bash") == 0

    def test_bash_version_4_passes(self) -> None:
        with patch.object(
            _runner.subprocess,
            "run",
            return_value=_CompletedProcessStub(returncode=0, stdout="5\n"),
        ):
            assert _runner._read_bash_major_version("/bin/bash") == 5


# ════════════════════════════════════════════════════════════════════
# Successful run path (sync wrapper around async-native runner)
# ════════════════════════════════════════════════════════════════════


class TestSuccessfulRun:
    """Full happy path: prereqs OK, extract OK, run exit 0, tarball found."""

    def test_returns_diag_run_result_with_tarball(self, tmp_path: Path) -> None:
        extracted = _build_extracted(tmp_path)
        output_root = _build_output_root(tmp_path)

        with (
            patch.object(_runner, "_check_prerequisites"),
            patch.object(
                _runner, "_extract_bash_to_temp", side_effect=_stub_extract_to(extracted)
            ),
            patch.object(
                _runner.asyncio,
                "create_subprocess_exec",
                side_effect=_async_proc_factory(returncode=0),
            ),
        ):
            result = run_full_diag(output_root=output_root)

        assert isinstance(result, DiagRunResult)
        assert result.exit_code == 0
        assert result.duration_s >= 0.0

    def test_subprocess_invoked_with_yes_flag(self, tmp_path: Path) -> None:
        extracted = _build_extracted(tmp_path)
        output_root = _build_output_root(tmp_path)

        captured: list[list[str]] = []
        with (
            patch.object(_runner, "_check_prerequisites"),
            patch.object(
                _runner, "_extract_bash_to_temp", side_effect=_stub_extract_to(extracted)
            ),
            patch.object(
                _runner.asyncio,
                "create_subprocess_exec",
                side_effect=_async_proc_factory(returncode=0, captured_cmd=captured),
            ),
        ):
            run_full_diag(output_root=output_root)

        assert len(captured) == 1
        cmd = captured[0]
        assert cmd[0] == "bash"
        assert cmd[1].endswith("sovyx-voice-diag.sh")
        assert cmd[2] == "--yes"

    def test_extra_args_appended_after_yes(self, tmp_path: Path) -> None:
        extracted = _build_extracted(tmp_path)
        output_root = _build_output_root(tmp_path)

        captured: list[list[str]] = []
        with (
            patch.object(_runner, "_check_prerequisites"),
            patch.object(
                _runner, "_extract_bash_to_temp", side_effect=_stub_extract_to(extracted)
            ),
            patch.object(
                _runner.asyncio,
                "create_subprocess_exec",
                side_effect=_async_proc_factory(returncode=0, captured_cmd=captured),
            ),
        ):
            run_full_diag(
                extra_args=("--skip-captures", "--non-interactive"),
                output_root=output_root,
            )

        cmd = captured[0]
        assert cmd[2:] == ["--yes", "--skip-captures", "--non-interactive"]

    def test_temp_dir_cleaned_up_after_success(self, tmp_path: Path) -> None:
        extracted = _build_extracted(tmp_path)
        output_root = _build_output_root(tmp_path)

        with (
            patch.object(_runner, "_check_prerequisites"),
            patch.object(
                _runner, "_extract_bash_to_temp", side_effect=_stub_extract_to(extracted)
            ),
            patch.object(
                _runner.asyncio,
                "create_subprocess_exec",
                side_effect=_async_proc_factory(returncode=0),
            ),
        ):
            run_full_diag(output_root=output_root)

        assert not extracted.exists(), "temp script dir should be removed in finally block"

    def test_picks_newest_tarball_among_multiple(self, tmp_path: Path) -> None:
        extracted = _build_extracted(tmp_path)
        output_root = tmp_path / "home"
        output_root.mkdir()

        old_dir = output_root / "sovyx-diag-old"
        old_dir.mkdir()
        old_tarball = old_dir / "sovyx-voice-diag_old.tar.gz"
        old_tarball.write_bytes(b"old")
        import os

        os.utime(old_tarball, (1000.0, 1000.0))

        new_dir = output_root / "sovyx-diag-new"
        new_dir.mkdir()
        new_tarball = new_dir / "sovyx-voice-diag_new.tar.gz"
        new_tarball.write_bytes(b"new")
        os.utime(new_tarball, (2000.0, 2000.0))

        with (
            patch.object(_runner, "_check_prerequisites"),
            patch.object(
                _runner, "_extract_bash_to_temp", side_effect=_stub_extract_to(extracted)
            ),
            patch.object(
                _runner.asyncio,
                "create_subprocess_exec",
                side_effect=_async_proc_factory(returncode=0),
            ),
        ):
            result = run_full_diag(output_root=output_root)

        assert result.tarball_path == new_tarball


# ════════════════════════════════════════════════════════════════════
# Failure paths
# ════════════════════════════════════════════════════════════════════


class TestFailureModes:
    """DiagRunError on non-zero exits and missing artefacts."""

    def test_non_zero_exit_raises_diagrunerror(self, tmp_path: Path) -> None:
        extracted = _build_extracted(tmp_path)
        output_root = _build_output_root(tmp_path, with_tarball=False)

        with (
            patch.object(_runner, "_check_prerequisites"),
            patch.object(
                _runner, "_extract_bash_to_temp", side_effect=_stub_extract_to(extracted)
            ),
            patch.object(
                _runner.asyncio,
                "create_subprocess_exec",
                side_effect=_async_proc_factory(returncode=3),
            ),
            pytest.raises(DiagRunError) as exc_info,
        ):
            run_full_diag(output_root=output_root)

        assert exc_info.value.exit_code == 3
        assert "selftest" in str(exc_info.value).lower()

    def test_temp_dir_cleaned_up_on_failure(self, tmp_path: Path) -> None:
        extracted = _build_extracted(tmp_path)
        output_root = _build_output_root(tmp_path, with_tarball=False)

        with (
            patch.object(_runner, "_check_prerequisites"),
            patch.object(
                _runner, "_extract_bash_to_temp", side_effect=_stub_extract_to(extracted)
            ),
            patch.object(
                _runner.asyncio,
                "create_subprocess_exec",
                side_effect=_async_proc_factory(returncode=1),
            ),
            pytest.raises(DiagRunError),
        ):
            run_full_diag(output_root=output_root)

        assert not extracted.exists(), "temp dir must be cleaned up even on failure"

    def test_missing_script_raises_diagrunerror_with_negative_exit(self, tmp_path: Path) -> None:
        extracted = tmp_path / "extracted"
        extracted.mkdir()
        # NB: no sovyx-voice-diag.sh written.

        with (
            patch.object(_runner, "_check_prerequisites"),
            patch.object(
                _runner, "_extract_bash_to_temp", side_effect=_stub_extract_to(extracted)
            ),
            pytest.raises(DiagRunError) as exc_info,
        ):
            run_full_diag()

        assert exc_info.value.exit_code == -1
        assert "package data layout regression" in str(exc_info.value)

    def test_clean_exit_with_no_tarball_raises_diagrunerror(self, tmp_path: Path) -> None:
        extracted = _build_extracted(tmp_path)
        output_root = _build_output_root(tmp_path, with_tarball=False)

        with (
            patch.object(_runner, "_check_prerequisites"),
            patch.object(
                _runner, "_extract_bash_to_temp", side_effect=_stub_extract_to(extracted)
            ),
            patch.object(
                _runner.asyncio,
                "create_subprocess_exec",
                side_effect=_async_proc_factory(returncode=0),
            ),
            pytest.raises(DiagRunError, match="no result tarball found"),
        ):
            run_full_diag(output_root=output_root)

    def test_failure_preserves_partial_output_dir(self, tmp_path: Path) -> None:
        extracted = _build_extracted(tmp_path)
        output_root = tmp_path / "home"
        output_root.mkdir()
        partial = output_root / "sovyx-diag-partial"
        partial.mkdir()
        (partial / "stub.txt").write_text("partial output")

        with (
            patch.object(_runner, "_check_prerequisites"),
            patch.object(
                _runner, "_extract_bash_to_temp", side_effect=_stub_extract_to(extracted)
            ),
            patch.object(
                _runner.asyncio,
                "create_subprocess_exec",
                side_effect=_async_proc_factory(returncode=1),
            ),
            pytest.raises(DiagRunError) as exc_info,
        ):
            run_full_diag(output_root=output_root)

        assert exc_info.value.partial_output_dir == partial


# ════════════════════════════════════════════════════════════════════
# Cancellation (P2 v0.30.30) — _cancel_process_tree + run_full_diag_async
# ════════════════════════════════════════════════════════════════════


class TestCancellation:
    """asyncio.CancelledError mid-run triggers SIGTERM → grace → SIGKILL."""

    @pytest.mark.asyncio()
    async def test_cancel_during_wait_terminates_process(self, tmp_path: Path) -> None:
        extracted = _build_extracted(tmp_path)
        output_root = _build_output_root(tmp_path)

        # Process that hangs for 30s; we'll cancel it after 50ms.
        spawned: list[_FakeAsyncProcess] = []

        async def _factory(*_args: Any, **_kwargs: Any) -> _FakeAsyncProcess:
            proc = _FakeAsyncProcess(returncode=0, hang_seconds=30.0)
            spawned.append(proc)
            return proc

        with (
            patch.object(_runner, "_check_prerequisites"),
            patch.object(
                _runner, "_extract_bash_to_temp", side_effect=_stub_extract_to(extracted)
            ),
            patch.object(_runner.asyncio, "create_subprocess_exec", side_effect=_factory),
            patch.object(_runner.sys, "platform", "win32"),  # Use Windows path: terminate()
        ):
            task = asyncio.create_task(run_full_diag_async(output_root=output_root))
            # Yield long enough for the spawn to land + wait() to start.
            await asyncio.sleep(0.05)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

        assert len(spawned) == 1
        assert spawned[0].terminate_called is True

    @pytest.mark.asyncio()
    async def test_sigkill_escalation_when_sigterm_ignored(self, tmp_path: Path) -> None:
        # Process that ignores SIGTERM; runner must escalate to SIGKILL
        # after grace expires. Use a tiny grace_period for fast testing.
        spawned: list[_FakeAsyncProcess] = []

        async def _factory(*_args: Any, **_kwargs: Any) -> _FakeAsyncProcess:
            proc = _FakeAsyncProcess(returncode=0, hang_seconds=30.0, ignore_sigterm=True)
            spawned.append(proc)
            return proc

        # Patch the grace period to 0.05s for fast test execution.
        with (
            patch.object(_runner, "_CANCEL_GRACE_PERIOD_S", 0.05),
            patch.object(_runner, "_CANCEL_SIGKILL_WAIT_S", 0.5),
            patch.object(_runner.asyncio, "create_subprocess_exec", side_effect=_factory),
            patch.object(_runner.sys, "platform", "win32"),  # Windows path: kill()
        ):
            proc = await _runner.asyncio.create_subprocess_exec("dummy")
            await _runner._cancel_process_tree(proc, grace_period_s=0.05)

        assert spawned[0].terminate_called is True
        assert spawned[0].kill_called is True

    @pytest.mark.asyncio()
    async def test_no_escalation_when_sigterm_succeeds(self, tmp_path: Path) -> None:
        spawned: list[_FakeAsyncProcess] = []

        async def _factory(*_args: Any, **_kwargs: Any) -> _FakeAsyncProcess:
            proc = _FakeAsyncProcess(returncode=0, hang_seconds=10.0, ignore_sigterm=False)
            spawned.append(proc)
            return proc

        with (
            patch.object(_runner.asyncio, "create_subprocess_exec", side_effect=_factory),
            patch.object(_runner.sys, "platform", "win32"),
        ):
            proc = await _runner.asyncio.create_subprocess_exec("dummy")
            await _runner._cancel_process_tree(proc, grace_period_s=1.0)

        assert spawned[0].terminate_called is True
        assert spawned[0].kill_called is False

    @pytest.mark.asyncio()
    async def test_cancel_completed_telemetry_fires(self, tmp_path: Path) -> None:
        events: list[tuple[str, dict[str, Any]]] = []

        class _Cap:
            def info(self, event: str, **kwargs: Any) -> None:
                events.append((event, kwargs))

            def warning(self, event: str, **kwargs: Any) -> None:
                events.append((event, kwargs))

        async def _factory(*_args: Any, **_kwargs: Any) -> _FakeAsyncProcess:
            return _FakeAsyncProcess(returncode=0, hang_seconds=2.0, ignore_sigterm=False)

        original = _runner.logger
        try:
            _runner.logger = _Cap()  # type: ignore[assignment]
            with (
                patch.object(_runner.asyncio, "create_subprocess_exec", side_effect=_factory),
                patch.object(_runner.sys, "platform", "win32"),
            ):
                proc = await _runner.asyncio.create_subprocess_exec("dummy")
                await _runner._cancel_process_tree(proc, grace_period_s=1.0)
        finally:
            _runner.logger = original  # type: ignore[assignment]

        completed = next(e for e in events if e[0] == "voice.diagnostics.cancel_completed")
        assert completed[1]["escalated_to_sigkill"] is False
        assert "duration_s" in completed[1]

    @pytest.mark.asyncio()
    async def test_cancel_grace_expired_telemetry_fires_on_escalation(
        self, tmp_path: Path
    ) -> None:
        events: list[tuple[str, dict[str, Any]]] = []

        class _Cap:
            def info(self, event: str, **kwargs: Any) -> None:
                events.append((event, kwargs))

            def warning(self, event: str, **kwargs: Any) -> None:
                events.append((event, kwargs))

        async def _factory(*_args: Any, **_kwargs: Any) -> _FakeAsyncProcess:
            return _FakeAsyncProcess(returncode=0, hang_seconds=30.0, ignore_sigterm=True)

        original = _runner.logger
        try:
            _runner.logger = _Cap()  # type: ignore[assignment]
            with (
                patch.object(_runner.asyncio, "create_subprocess_exec", side_effect=_factory),
                patch.object(_runner.sys, "platform", "win32"),
            ):
                proc = await _runner.asyncio.create_subprocess_exec("dummy")
                await _runner._cancel_process_tree(proc, grace_period_s=0.05)
        finally:
            _runner.logger = original  # type: ignore[assignment]

        grace_expired = next(
            (e for e in events if e[0] == "voice.diagnostics.cancel_grace_expired"),
            None,
        )
        assert grace_expired is not None
        completed = next(e for e in events if e[0] == "voice.diagnostics.cancel_completed")
        assert completed[1]["escalated_to_sigkill"] is True


# ════════════════════════════════════════════════════════════════════
# Helpers (find_latest_*)
# ════════════════════════════════════════════════════════════════════


class TestResultLocators:
    """_find_latest_result_dir and _find_latest_result_tarball edge cases."""

    def test_find_latest_dir_returns_none_when_empty(self, tmp_path: Path) -> None:
        assert _runner._find_latest_result_dir(tmp_path) is None

    def test_find_latest_tarball_returns_none_when_empty(self, tmp_path: Path) -> None:
        assert _runner._find_latest_result_tarball(tmp_path) is None

    def test_find_latest_dir_skips_non_directories(self, tmp_path: Path) -> None:
        (tmp_path / "sovyx-diag-bogus.txt").write_text("not a dir")
        assert _runner._find_latest_result_dir(tmp_path) is None

    def test_find_latest_tarball_skips_non_directories(self, tmp_path: Path) -> None:
        (tmp_path / "sovyx-diag-bogus.txt").write_text("not a dir")
        assert _runner._find_latest_result_tarball(tmp_path) is None


# ════════════════════════════════════════════════════════════════════
# Dataclass invariants
# ════════════════════════════════════════════════════════════════════


class TestDataclassInvariants:
    """DiagRunResult is frozen + slots."""

    def test_diag_run_result_is_frozen(self) -> None:
        result = DiagRunResult(
            tarball_path=Path("/tmp/x.tar.gz"),
            duration_s=10.0,
            exit_code=0,
        )
        with pytest.raises(FrozenInstanceError):
            result.exit_code = 1  # type: ignore[misc]


# ════════════════════════════════════════════════════════════════════
# DiagRunError + DiagPrerequisiteError
# ════════════════════════════════════════════════════════════════════


class TestErrorClasses:
    def test_diag_run_error_carries_exit_code_and_partial_dir(self) -> None:
        partial = Path("/tmp/sovyx-diag-x")
        err = DiagRunError("boom", exit_code=42, partial_output_dir=partial)
        assert str(err) == "boom"
        assert err.exit_code == 42
        assert err.partial_output_dir == partial

    def test_diag_run_error_default_partial_dir_is_none(self) -> None:
        err = DiagRunError("boom", exit_code=1)
        assert err.partial_output_dir is None

    def test_diag_prerequisite_error_is_runtimeerror(self) -> None:
        assert issubclass(DiagPrerequisiteError, RuntimeError)

    def test_diag_run_error_is_runtimeerror(self) -> None:
        assert issubclass(DiagRunError, RuntimeError)


# ════════════════════════════════════════════════════════════════════
# v0.30.24: voice.diagnostics.full_diag_* telemetry events (§8.3)
# ════════════════════════════════════════════════════════════════════


class TestDiagnosticsTelemetry:
    """voice.diagnostics.full_diag_started/completed/failed fire on each path."""

    def _capture(self) -> tuple[list[tuple[str, dict[str, Any]]], object]:
        events: list[tuple[str, dict[str, Any]]] = []

        class _Cap:
            def info(self, event: str, **kwargs: Any) -> None:
                events.append((event, kwargs))

            def warning(self, event: str, **kwargs: Any) -> None:
                events.append((event, kwargs))

        original = _runner.logger
        _runner.logger = _Cap()  # type: ignore[assignment]
        return events, original

    def _restore(self, original: object) -> None:
        _runner.logger = original  # type: ignore[assignment]

    def test_classify_diag_mode_full(self) -> None:
        assert _runner._classify_diag_mode(()) == "full"

    def test_classify_diag_mode_skip_captures(self) -> None:
        assert (
            _runner._classify_diag_mode(("--skip-captures", "--non-interactive"))
            == "skip_captures"
        )

    def test_classify_diag_mode_surgical(self) -> None:
        assert (
            _runner._classify_diag_mode(("--only", "A,C,D,E,J", "--skip-captures")) == "surgical"
        )

    def test_started_and_completed_fire_on_success(self, tmp_path: Path) -> None:
        extracted = _build_extracted(tmp_path)
        output_root = _build_output_root(tmp_path)

        events, original = self._capture()
        try:
            with (
                patch.object(_runner, "_check_prerequisites"),
                patch.object(
                    _runner, "_extract_bash_to_temp", side_effect=_stub_extract_to(extracted)
                ),
                patch.object(
                    _runner.asyncio,
                    "create_subprocess_exec",
                    side_effect=_async_proc_factory(returncode=0),
                ),
            ):
                run_full_diag(output_root=output_root)
        finally:
            self._restore(original)

        names = [e[0] for e in events]
        assert "voice.diagnostics.full_diag_started" in names
        assert "voice.diagnostics.full_diag_completed" in names
        completed = next(e for e in events if e[0] == "voice.diagnostics.full_diag_completed")
        assert completed[1]["exit_code"] == 0
        assert completed[1]["mode"] == "full"

    def test_failed_fires_on_non_zero_exit(self, tmp_path: Path) -> None:
        extracted = _build_extracted(tmp_path)
        output_root = _build_output_root(tmp_path, with_tarball=False)

        events, original = self._capture()
        try:
            with (
                patch.object(_runner, "_check_prerequisites"),
                patch.object(
                    _runner, "_extract_bash_to_temp", side_effect=_stub_extract_to(extracted)
                ),
                patch.object(
                    _runner.asyncio,
                    "create_subprocess_exec",
                    side_effect=_async_proc_factory(returncode=3),
                ),
                pytest.raises(DiagRunError),
            ):
                run_full_diag(output_root=output_root)
        finally:
            self._restore(original)

        failed = next((e for e in events if e[0] == "voice.diagnostics.full_diag_failed"), None)
        assert failed is not None
        assert failed[1]["exit_code"] == 3
        assert failed[1]["failure_reason"] == "selftest_failed"

    def test_started_event_carries_trigger_field(self, tmp_path: Path) -> None:
        extracted = _build_extracted(tmp_path)
        output_root = _build_output_root(tmp_path)

        events, original = self._capture()
        try:
            with (
                patch.object(_runner, "_check_prerequisites"),
                patch.object(
                    _runner, "_extract_bash_to_temp", side_effect=_stub_extract_to(extracted)
                ),
                patch.object(
                    _runner.asyncio,
                    "create_subprocess_exec",
                    side_effect=_async_proc_factory(returncode=0),
                ),
            ):
                run_full_diag(output_root=output_root)
        finally:
            self._restore(original)

        started = next(e for e in events if e[0] == "voice.diagnostics.full_diag_started")
        assert started[1]["trigger"] == "cli"

    def test_started_event_honours_wizard_trigger(self, tmp_path: Path) -> None:
        extracted = _build_extracted(tmp_path)
        output_root = _build_output_root(tmp_path)

        events, original = self._capture()
        try:
            with (
                patch.object(_runner, "_check_prerequisites"),
                patch.object(
                    _runner, "_extract_bash_to_temp", side_effect=_stub_extract_to(extracted)
                ),
                patch.object(
                    _runner.asyncio,
                    "create_subprocess_exec",
                    side_effect=_async_proc_factory(returncode=0),
                ),
            ):
                run_full_diag(output_root=output_root, trigger="wizard")
        finally:
            self._restore(original)

        started = next(e for e in events if e[0] == "voice.diagnostics.full_diag_started")
        assert started[1]["trigger"] == "wizard"

    def test_started_event_coerces_unknown_trigger_to_cli(self, tmp_path: Path) -> None:
        extracted = _build_extracted(tmp_path)
        output_root = _build_output_root(tmp_path)

        events, original = self._capture()
        try:
            with (
                patch.object(_runner, "_check_prerequisites"),
                patch.object(
                    _runner, "_extract_bash_to_temp", side_effect=_stub_extract_to(extracted)
                ),
                patch.object(
                    _runner.asyncio,
                    "create_subprocess_exec",
                    side_effect=_async_proc_factory(returncode=0),
                ),
            ):
                run_full_diag(output_root=output_root, trigger="malicious")
        finally:
            self._restore(original)

        started = next(e for e in events if e[0] == "voice.diagnostics.full_diag_started")
        assert started[1]["trigger"] == "cli"

    def test_completed_event_includes_hypothesis_winner_field(self, tmp_path: Path) -> None:
        extracted = _build_extracted(tmp_path)
        output_root = _build_output_root(tmp_path)

        events, original = self._capture()
        try:
            with (
                patch.object(_runner, "_check_prerequisites"),
                patch.object(
                    _runner, "_extract_bash_to_temp", side_effect=_stub_extract_to(extracted)
                ),
                patch.object(
                    _runner.asyncio,
                    "create_subprocess_exec",
                    side_effect=_async_proc_factory(returncode=0),
                ),
            ):
                run_full_diag(output_root=output_root)
        finally:
            self._restore(original)

        completed = next(e for e in events if e[0] == "voice.diagnostics.full_diag_completed")
        assert "hypothesis_winner" in completed[1]
        assert completed[1]["hypothesis_winner"] == ""

    def test_failed_fires_when_tarball_missing(self, tmp_path: Path) -> None:
        extracted = _build_extracted(tmp_path)
        output_root = _build_output_root(tmp_path, with_tarball=False)

        events, original = self._capture()
        try:
            with (
                patch.object(_runner, "_check_prerequisites"),
                patch.object(
                    _runner, "_extract_bash_to_temp", side_effect=_stub_extract_to(extracted)
                ),
                patch.object(
                    _runner.asyncio,
                    "create_subprocess_exec",
                    side_effect=_async_proc_factory(returncode=0),
                ),
                pytest.raises(DiagRunError),
            ):
                run_full_diag(output_root=output_root)
        finally:
            self._restore(original)

        failed = next((e for e in events if e[0] == "voice.diagnostics.full_diag_failed"), None)
        assert failed is not None
        assert failed[1]["failure_reason"] == "tarball_missing"
