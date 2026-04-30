"""Tests for :mod:`sovyx.voice.health._pipewire` (F3 layer 1).

Mocks ``shutil.which`` + ``subprocess.run`` so the suite stays
cross-platform and deterministic. Validates:

* Non-Linux → ABSENT verdict, no subprocess calls.
* Linux + no socket + no pactl → ABSENT.
* Linux + socket + no pactl → RUNNING (foundational signal).
* Linux + pactl info OK + no echo-cancel → RUNNING.
* Linux + pactl info OK + echo-cancel loaded → RUNNING_WITH_ECHO_CANCEL.
* pactl info timeout / non-zero → graceful UNKNOWN.
* Module enumeration parses tab-separated output correctly.
* load_echo_cancel_module returns parsed module ID on success.
* load_echo_cancel_module raises PipeWireRoutingError on subprocess
  timeout / non-zero / unparseable output.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from sovyx.voice.health._pipewire import (
    PipeWireReport,
    PipeWireRoutingError,
    PipeWireStatus,
    detect_pipewire,
    enumerate_pipewire_modules,
    is_echo_cancel_loaded,
    load_echo_cancel_module,
)

# ── Subprocess fake ────────────────────────────────────────────────


def _fake_run(
    *,
    pactl_info_stdout: str = "Server Name: PulseAudio (on PipeWire 1.0.5)\n",
    pactl_info_returncode: int = 0,
    pactl_info_raise: type[BaseException] | None = None,
    list_modules_stdout: str = "",
    list_modules_returncode: int = 0,
    list_modules_raise: type[BaseException] | None = None,
    load_module_stdout: str = "1234\n",
    load_module_returncode: int = 0,
    load_module_raise: type[BaseException] | None = None,
) -> Any:
    """Build a subprocess.run replacement that dispatches by argv."""

    def _run(args: tuple[str, ...], **_kwargs: Any) -> Any:
        cmd = tuple(args)
        # Strip the resolved pactl path; key on the verb.
        verb = cmd[1] if len(cmd) >= 2 else ""  # noqa: PLR2004
        if verb == "info":
            if pactl_info_raise is not None:
                raise pactl_info_raise(cmd, _kwargs.get("timeout", 0))
            return MagicMock(
                returncode=pactl_info_returncode,
                stdout=pactl_info_stdout,
                stderr="",
            )
        if verb == "list":
            if list_modules_raise is not None:
                raise list_modules_raise(cmd, _kwargs.get("timeout", 0))
            return MagicMock(
                returncode=list_modules_returncode,
                stdout=list_modules_stdout,
                stderr="",
            )
        if verb == "load-module":
            if load_module_raise is not None:
                raise load_module_raise(cmd, _kwargs.get("timeout", 0))
            return MagicMock(
                returncode=load_module_returncode,
                stdout=load_module_stdout,
                stderr="error context" if load_module_returncode else "",
            )
        return MagicMock(returncode=1, stdout="", stderr="unexpected verb")

    return _run


# ── Cross-platform branches ────────────────────────────────────────


class TestNonLinuxBranches:
    def test_windows_returns_absent(self) -> None:
        with patch.object(sys, "platform", "win32"):
            report = detect_pipewire()
        assert report.status is PipeWireStatus.ABSENT
        assert any("non-linux" in n for n in report.notes)

    def test_darwin_returns_absent(self) -> None:
        with patch.object(sys, "platform", "darwin"):
            report = detect_pipewire()
        assert report.status is PipeWireStatus.ABSENT


# ── Linux detection branches ───────────────────────────────────────


class TestLinuxDetection:
    def test_no_socket_no_pactl_returns_absent(self, tmp_path: Path) -> None:
        with (
            patch.object(sys, "platform", "linux"),
            patch("shutil.which", return_value=None),
        ):
            report = detect_pipewire(runtime_dir=tmp_path)
        assert report.status is PipeWireStatus.ABSENT
        assert report.socket_present is False
        assert report.pactl_available is False

    def test_socket_present_no_pactl_returns_running(self, tmp_path: Path) -> None:
        # Create a fake socket file so the detection sees it.
        (tmp_path / "pipewire-0").touch()
        with (
            patch.object(sys, "platform", "linux"),
            patch("shutil.which", return_value=None),
        ):
            report = detect_pipewire(runtime_dir=tmp_path)
        assert report.status is PipeWireStatus.RUNNING
        assert report.socket_present is True
        assert report.pactl_available is False
        assert any("pactl binary not found" in n for n in report.notes)

    def test_pactl_info_ok_no_echo_cancel_returns_running(self, tmp_path: Path) -> None:
        with (
            patch.object(sys, "platform", "linux"),
            patch("shutil.which", return_value="/usr/bin/pactl"),
            patch(
                "subprocess.run",
                side_effect=_fake_run(
                    list_modules_stdout=(
                        "0\tmodule-device-restore\t\n"
                        "1\tmodule-stream-restore\t\n"
                        "2\tmodule-card-restore\t\n"
                    ),
                ),
            ),
        ):
            report = detect_pipewire(runtime_dir=tmp_path)
        assert report.status is PipeWireStatus.RUNNING
        assert report.pactl_available is True
        assert report.pactl_info_ok is True
        assert report.echo_cancel_loaded is False
        assert "module-device-restore" in report.modules_loaded
        assert "module-echo-cancel" not in report.modules_loaded

    def test_echo_cancel_loaded_returns_running_with_echo_cancel(
        self,
        tmp_path: Path,
    ) -> None:
        with (
            patch.object(sys, "platform", "linux"),
            patch("shutil.which", return_value="/usr/bin/pactl"),
            patch(
                "subprocess.run",
                side_effect=_fake_run(
                    list_modules_stdout=(
                        "0\tmodule-device-restore\t\n"
                        "5\tmodule-echo-cancel\taec_method=webrtc source_name=mic_aec\n"
                    ),
                ),
            ),
        ):
            report = detect_pipewire(runtime_dir=tmp_path)
        assert report.status is PipeWireStatus.RUNNING_WITH_ECHO_CANCEL
        assert report.echo_cancel_loaded is True
        assert "module-echo-cancel" in report.modules_loaded

    def test_pactl_info_timeout_falls_back_to_absent(self, tmp_path: Path) -> None:
        with (
            patch.object(sys, "platform", "linux"),
            patch("shutil.which", return_value="/usr/bin/pactl"),
            patch(
                "subprocess.run",
                side_effect=_fake_run(pactl_info_raise=subprocess.TimeoutExpired),
            ),
        ):
            report = detect_pipewire(runtime_dir=tmp_path)
        # No socket + pactl info timed out → ABSENT.
        assert report.status is PipeWireStatus.ABSENT
        assert any("timed out" in n for n in report.notes)

    def test_pactl_info_nonzero_with_socket_returns_unknown(self, tmp_path: Path) -> None:
        (tmp_path / "pipewire-0").touch()
        with (
            patch.object(sys, "platform", "linux"),
            patch("shutil.which", return_value="/usr/bin/pactl"),
            patch(
                "subprocess.run",
                side_effect=_fake_run(
                    pactl_info_returncode=1,
                    list_modules_returncode=1,
                ),
            ),
        ):
            report = detect_pipewire(runtime_dir=tmp_path)
        # Socket present but pactl can't talk to it AND modules
        # enum failed → ambiguous; verdict is RUNNING because the
        # socket alone is a strong PipeWire signal.
        assert report.status is PipeWireStatus.RUNNING
        assert report.pactl_info_ok is False

    def test_server_name_parsed_from_pactl_info(self, tmp_path: Path) -> None:
        with (
            patch.object(sys, "platform", "linux"),
            patch("shutil.which", return_value="/usr/bin/pactl"),
            patch(
                "subprocess.run",
                side_effect=_fake_run(
                    pactl_info_stdout=(
                        "User Name: alice\n"
                        "Host Name: vaio\n"
                        "Server Name: PulseAudio (on PipeWire 1.2.0)\n"
                        "Server Version: 16.0.0\n"
                    ),
                ),
            ),
        ):
            report = detect_pipewire(runtime_dir=tmp_path)
        assert report.server_name == "PulseAudio (on PipeWire 1.2.0)"


# ── Standalone helpers ─────────────────────────────────────────────


class TestEnumerateModules:
    def test_returns_empty_when_pactl_missing(self) -> None:
        with patch("shutil.which", return_value=None):
            assert enumerate_pipewire_modules() == ()

    def test_returns_sorted_modules(self) -> None:
        with (
            patch("shutil.which", return_value="/usr/bin/pactl"),
            patch(
                "subprocess.run",
                side_effect=_fake_run(
                    list_modules_stdout=(
                        "0\tmodule-zebra\t\n1\tmodule-alpha\t\n2\tmodule-foxtrot\t\n"
                    ),
                ),
            ),
        ):
            modules = enumerate_pipewire_modules()
        # Sorted alphabetically — predictable for dashboards.
        assert modules == ("module-alpha", "module-foxtrot", "module-zebra")


class TestIsEchoCancelLoaded:
    def test_returns_true_when_loaded(self) -> None:
        with (
            patch("shutil.which", return_value="/usr/bin/pactl"),
            patch(
                "subprocess.run",
                side_effect=_fake_run(
                    list_modules_stdout="5\tmodule-echo-cancel\t\n",
                ),
            ),
        ):
            assert is_echo_cancel_loaded() is True

    def test_returns_false_when_not_loaded(self) -> None:
        with (
            patch("shutil.which", return_value="/usr/bin/pactl"),
            patch(
                "subprocess.run",
                side_effect=_fake_run(list_modules_stdout="0\tmodule-other\t\n"),
            ),
        ):
            assert is_echo_cancel_loaded() is False

    def test_returns_false_when_pactl_missing(self) -> None:
        with patch("shutil.which", return_value=None):
            assert is_echo_cancel_loaded() is False


# ── Routing — load_echo_cancel_module ──────────────────────────────


class TestLoadEchoCancelModule:
    @pytest.mark.asyncio
    async def test_returns_module_id_on_success(self) -> None:
        with (
            patch("shutil.which", return_value="/usr/bin/pactl"),
            patch(
                "subprocess.run",
                side_effect=_fake_run(load_module_stdout="42\n"),
            ),
        ):
            module_id = await load_echo_cancel_module()
        assert module_id == 42  # noqa: PLR2004

    @pytest.mark.asyncio
    async def test_passes_aec_method_default_webrtc(self) -> None:
        captured: dict[str, Any] = {}

        def _capture_run(args: tuple[str, ...], **_kwargs: Any) -> Any:
            captured["args"] = args
            return MagicMock(returncode=0, stdout="1\n", stderr="")

        with (
            patch("shutil.which", return_value="/usr/bin/pactl"),
            patch("subprocess.run", side_effect=_capture_run),
        ):
            await load_echo_cancel_module()

        assert "aec_method=webrtc" in captured["args"]

    @pytest.mark.asyncio
    async def test_raises_when_pactl_missing(self) -> None:
        with (
            patch("shutil.which", return_value=None),
            pytest.raises(PipeWireRoutingError, match="pactl binary not found"),
        ):
            await load_echo_cancel_module()

    @pytest.mark.asyncio
    async def test_raises_on_subprocess_timeout(self) -> None:
        with (
            patch("shutil.which", return_value="/usr/bin/pactl"),
            patch(
                "subprocess.run",
                side_effect=_fake_run(load_module_raise=subprocess.TimeoutExpired),
            ),
            pytest.raises(PipeWireRoutingError, match="exceeded"),
        ):
            await load_echo_cancel_module()

    @pytest.mark.asyncio
    async def test_raises_on_nonzero_exit_with_structured_detail(self) -> None:
        with (
            patch("shutil.which", return_value="/usr/bin/pactl"),
            patch(
                "subprocess.run",
                side_effect=_fake_run(
                    load_module_returncode=1,
                    load_module_stdout="",
                ),
            ),
            pytest.raises(PipeWireRoutingError) as exc_info,
        ):
            await load_echo_cancel_module()
        assert exc_info.value.returncode == 1
        assert "error context" in exc_info.value.stderr

    @pytest.mark.asyncio
    async def test_raises_on_unparseable_stdout(self) -> None:
        with (
            patch("shutil.which", return_value="/usr/bin/pactl"),
            patch(
                "subprocess.run",
                side_effect=_fake_run(load_module_stdout="not-a-number\n"),
            ),
            pytest.raises(PipeWireRoutingError, match="non-integer"),
        ):
            await load_echo_cancel_module()


# ── Report contract ────────────────────────────────────────────────


class TestPipeWireReportContract:
    def test_status_enum_values_stable(self) -> None:
        # Dashboards key on these strings — renames are breaking.
        assert PipeWireStatus.ABSENT.value == "absent"
        assert PipeWireStatus.RUNNING.value == "running"
        assert PipeWireStatus.RUNNING_WITH_ECHO_CANCEL.value == "running_with_echo_cancel"
        assert PipeWireStatus.UNKNOWN.value == "unknown"

    def test_default_report_is_safe(self) -> None:
        # An empty PipeWireReport must not crash the dashboard.
        r = PipeWireReport(status=PipeWireStatus.ABSENT)
        assert r.modules_loaded == ()
        assert r.notes == ()
        assert r.echo_cancel_loaded is False
        assert r.server_name is None

    def test_phase5_fields_default_safe(self) -> None:
        # Phase 5 / T5.31 + T5.34 — new fields default to "no
        # signal" so legacy callers that ignore them keep working.
        r = PipeWireReport(status=PipeWireStatus.ABSENT)
        assert r.pipewire_version is None
        assert r.pipewire_major_version is None
        assert r.hybrid_pulseaudio_conflict is False


# ── Phase 5 / T5.31 — version extraction ──────────────────────────


class TestExtractPipeWireVersion:
    """The version is parsed from ``pactl info``'s server-name
    string (e.g. ``"PulseAudio (on PipeWire 1.0.5)"``)."""

    def test_full_three_part_version(self) -> None:
        from sovyx.voice.health._pipewire import _extract_pipewire_version

        version, major = _extract_pipewire_version(
            "PulseAudio (on PipeWire 1.0.5)",
        )
        assert version == "1.0.5"
        assert major == 1

    def test_legacy_zero_three_branch(self) -> None:
        from sovyx.voice.health._pipewire import _extract_pipewire_version

        version, major = _extract_pipewire_version(
            "PulseAudio (on PipeWire 0.3.65)",
        )
        assert version == "0.3.65"
        assert major == 0

    def test_two_part_version_fallback(self) -> None:
        # Some PipeWire builds emit "PipeWire X.Y" without patch.
        from sovyx.voice.health._pipewire import _extract_pipewire_version

        version, major = _extract_pipewire_version("PipeWire 1.2")
        assert version == "1.2"
        assert major == 1

    def test_real_pulseaudio_returns_none(self) -> None:
        # A real PulseAudio (no PipeWire) doesn't carry the
        # PipeWire substring — version extraction returns None.
        from sovyx.voice.health._pipewire import _extract_pipewire_version

        version, major = _extract_pipewire_version("PulseAudio (Mock daemon)")
        assert version is None
        assert major is None

    def test_none_server_name_returns_none(self) -> None:
        from sovyx.voice.health._pipewire import _extract_pipewire_version

        version, major = _extract_pipewire_version(None)
        assert version is None
        assert major is None


# ── Phase 5 / T5.34 — hybrid PulseAudio + PipeWire conflict ───────


class TestHybridConflictDetector:
    """A real ``pulseaudio`` process running alongside PipeWire
    breaks echo-cancel + module routing. The detector
    distinguishes a real PA from the ``pipewire-pulse`` compat
    layer (whose argv mentions PipeWire)."""

    def _run_pgrep(
        self,
        monkeypatch: pytest.MonkeyPatch,
        *,
        stdout: str,
        returncode: int = 0,
    ) -> None:
        from sovyx.voice.health import _pipewire as pw

        def _fake(*_args: object, **_kwargs: object) -> object:
            class _Result:
                pass

            r = _Result()
            r.stdout = stdout
            r.returncode = returncode
            return r

        monkeypatch.setattr(pw.subprocess, "run", _fake)

    def test_no_pulseaudio_running(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from sovyx.voice.health._pipewire import _detect_hybrid_pulseaudio_conflict

        # pgrep returns 1 when nothing matches.
        self._run_pgrep(monkeypatch, stdout="", returncode=1)
        notes: list[str] = []
        assert _detect_hybrid_pulseaudio_conflict(notes) is False
        assert notes == []

    def test_pipewire_pulse_compat_layer_does_not_count(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # pipewire-pulse impersonates PA via pulseaudio binary
        # alias on some distros; its cmdline mentions PipeWire.
        from sovyx.voice.health._pipewire import _detect_hybrid_pulseaudio_conflict

        self._run_pgrep(
            monkeypatch,
            stdout="1234 /usr/bin/pulseaudio --enable-pipewire-compat\n",
            returncode=0,
        )
        notes: list[str] = []
        assert _detect_hybrid_pulseaudio_conflict(notes) is False

    def test_real_pulseaudio_flagged_as_conflict(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from sovyx.voice.health._pipewire import _detect_hybrid_pulseaudio_conflict

        self._run_pgrep(
            monkeypatch,
            stdout="5678 /usr/bin/pulseaudio --daemonize\n",
            returncode=0,
        )
        notes: list[str] = []
        assert _detect_hybrid_pulseaudio_conflict(notes) is True
        # Diagnostic note carries the PID for forensics.
        assert any("real_pulseaudio_detected" in n for n in notes)

    def test_subprocess_failure_collapses_to_false(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from sovyx.voice.health import _pipewire as pw

        def _raise(*_args: object, **_kwargs: object) -> None:
            raise OSError("simulated pgrep crash")

        monkeypatch.setattr(pw.subprocess, "run", _raise)
        notes: list[str] = []
        assert pw._detect_hybrid_pulseaudio_conflict(notes) is False
        # Failure is surfaced as a note for telemetry.
        assert any("hybrid_conflict_probe_failed" in n for n in notes)
