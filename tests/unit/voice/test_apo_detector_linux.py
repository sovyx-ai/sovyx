"""Unit tests for :mod:`sovyx.voice._apo_detector_linux`.

The detector shells out to ``pactl`` and ``pw-dump``; every test here
patches :func:`shutil.which` and :func:`subprocess.run` so nothing ever
touches the host's audio daemon. Two mock shapes cover the surface:

1. **Happy paths** — ``pactl``-only / ``pw-dump``-only / both — where
   the detector must collapse observations into one
   :class:`LinuxApoReport` and flip ``echo_cancel_active`` on the
   right sentinel labels.

2. **Failure isolation** — timeout, non-zero exit, malformed JSON,
   unknown fields. All collapse to an empty list without raising.
"""

from __future__ import annotations

import json
import subprocess
import sys
from typing import Any
from unittest.mock import patch

import pytest

from sovyx.voice import _apo_detector_linux as detector
from sovyx.voice._apo_detector_linux import (
    LinuxApoReport,
    detect_capture_apos_linux,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _pactl_output(*modules: str) -> str:
    """Render a fake ``pactl list short modules`` body."""
    return "\n".join(f"{idx}\t{name}\t" for idx, name in enumerate(modules, start=1))


def _pw_dump_output(*nodes: dict[str, Any]) -> str:
    """Render a fake ``pw-dump`` JSON body around caller-supplied nodes."""
    payload = [
        {
            "type": "PipeWire:Interface:Node",
            "info": {"props": props},
        }
        for props in nodes
    ]
    return json.dumps(payload)


class _FakeCompleted:
    """Duck-typed :class:`subprocess.CompletedProcess` for ``run`` mocks."""

    def __init__(self, stdout: str = "", *, returncode: int = 0, stderr: str = "") -> None:
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode


def _run_dispatch(responses: dict[str, _FakeCompleted | Exception]):
    """Return a ``subprocess.run`` stand-in keyed by argv[0]."""

    def _run(argv, **_kwargs):  # noqa: ANN001 — patched subprocess signature
        tool = argv[0]
        response = responses.get(tool)
        if isinstance(response, Exception):
            raise response
        if response is None:
            raise FileNotFoundError(tool)
        return response

    return _run


def _which_dispatch(available: set[str]):
    def _which(tool: str) -> str | None:
        return f"/usr/bin/{tool}" if tool in available else None

    return _which


# ---------------------------------------------------------------------------
# Platform gate
# ---------------------------------------------------------------------------


class TestPlatformGate:
    """Non-Linux hosts must collapse to empty without running any tool."""

    def test_non_linux_returns_empty(self) -> None:
        with patch.object(sys, "platform", "win32"):
            assert detect_capture_apos_linux() == []

    def test_darwin_returns_empty(self) -> None:
        with patch.object(sys, "platform", "darwin"):
            assert detect_capture_apos_linux() == []


# ---------------------------------------------------------------------------
# PulseAudio-only paths
# ---------------------------------------------------------------------------


class TestPulseOnly:
    """pactl present, pw-dump absent — PulseAudio-only classification."""

    def test_detects_module_echo_cancel(self) -> None:
        pactl = _FakeCompleted(_pactl_output("module-echo-cancel"))
        with (
            patch.object(sys, "platform", "linux"),
            patch("shutil.which", _which_dispatch({"pactl"})),
            patch("subprocess.run", _run_dispatch({"pactl": pactl})),
        ):
            reports = detect_capture_apos_linux()
        assert len(reports) == 1
        report = reports[0]
        assert report.session_manager == "pulseaudio"
        assert report.echo_cancel_active is True
        assert "PulseAudio Echo Cancel" in report.known_apos

    def test_detects_rnnoise_and_ladspa(self) -> None:
        pactl = _FakeCompleted(
            _pactl_output("module-rnnoise", "module-ladspa-sink"),
        )
        with (
            patch.object(sys, "platform", "linux"),
            patch("shutil.which", _which_dispatch({"pactl"})),
            patch("subprocess.run", _run_dispatch({"pactl": pactl})),
        ):
            reports = detect_capture_apos_linux()
        report = reports[0]
        assert "PulseAudio RNNoise" in report.known_apos
        assert "PulseAudio LADSPA chain" in report.known_apos
        # RNNoise alone does not trip echo_cancel_active.
        assert report.echo_cancel_active is False

    def test_unknown_modules_skipped_from_labels_but_kept_raw(self) -> None:
        pactl = _FakeCompleted(_pactl_output("module-alsa-card", "module-stream-restore"))
        with (
            patch.object(sys, "platform", "linux"),
            patch("shutil.which", _which_dispatch({"pactl"})),
            patch("subprocess.run", _run_dispatch({"pactl": pactl})),
        ):
            reports = detect_capture_apos_linux()
        assert len(reports) == 1
        report = reports[0]
        assert report.known_apos == []
        assert "module-alsa-card" in report.raw_entries
        assert "module-stream-restore" in report.raw_entries


# ---------------------------------------------------------------------------
# PipeWire-only paths
# ---------------------------------------------------------------------------


class TestPipewireOnly:
    """pw-dump present, pactl absent — PipeWire-only classification."""

    def test_detects_filter_chain_echo_cancel(self) -> None:
        pw = _FakeCompleted(
            _pw_dump_output(
                {
                    "node.name": "effect.echo-cancel.source",
                    "node.description": "Echo-Cancelling Source",
                    "factory.name": "filter-chain",
                    "media.class": "Audio/Source",
                },
            ),
        )
        with (
            patch.object(sys, "platform", "linux"),
            patch("shutil.which", _which_dispatch({"pw-dump"})),
            patch("subprocess.run", _run_dispatch({"pw-dump": pw})),
        ):
            reports = detect_capture_apos_linux()
        assert len(reports) == 1
        report = reports[0]
        assert report.session_manager == "pipewire"
        assert report.echo_cancel_active is True
        assert "PipeWire Echo Cancel" in report.known_apos

    def test_dedupes_across_fields(self) -> None:
        pw = _FakeCompleted(
            _pw_dump_output(
                {
                    "node.name": "effect.echo-cancel.one",
                    "node.description": "echo-cancel",
                    "factory.name": "filter-chain",
                },
                {
                    "node.name": "effect.echocancel.two",
                    "factory.name": "filter-chain",
                },
            ),
        )
        with (
            patch.object(sys, "platform", "linux"),
            patch("shutil.which", _which_dispatch({"pw-dump"})),
            patch("subprocess.run", _run_dispatch({"pw-dump": pw})),
        ):
            reports = detect_capture_apos_linux()
        report = reports[0]
        assert report.known_apos.count("PipeWire Echo Cancel") == 1


# ---------------------------------------------------------------------------
# Mixed sessions
# ---------------------------------------------------------------------------


class TestMixedSession:
    """Both pactl and pw-dump reachable — classify as ``mixed``."""

    def test_mixed_classification_and_deduped_labels(self) -> None:
        pactl = _FakeCompleted(_pactl_output("module-echo-cancel"))
        pw = _FakeCompleted(
            _pw_dump_output(
                {
                    "node.name": "effect.rnnoise.source",
                    "factory.name": "filter-chain",
                },
            ),
        )
        with (
            patch.object(sys, "platform", "linux"),
            patch("shutil.which", _which_dispatch({"pactl", "pw-dump"})),
            patch("subprocess.run", _run_dispatch({"pactl": pactl, "pw-dump": pw})),
        ):
            reports = detect_capture_apos_linux()
        report = reports[0]
        assert report.session_manager == "mixed"
        assert "PulseAudio Echo Cancel" in report.known_apos
        assert "PipeWire RNNoise" in report.known_apos
        assert report.echo_cancel_active is True


# ---------------------------------------------------------------------------
# Failure isolation
# ---------------------------------------------------------------------------


class TestFailureIsolation:
    """Every failure mode must degrade to an empty list without raising."""

    def test_no_tools_available(self) -> None:
        with (
            patch.object(sys, "platform", "linux"),
            patch("shutil.which", _which_dispatch(set())),
        ):
            assert detect_capture_apos_linux() == []

    def test_pactl_timeout(self) -> None:
        with (
            patch.object(sys, "platform", "linux"),
            patch("shutil.which", _which_dispatch({"pactl"})),
            patch(
                "subprocess.run",
                _run_dispatch(
                    {"pactl": subprocess.TimeoutExpired(cmd="pactl", timeout=2.0)},
                ),
            ),
        ):
            assert detect_capture_apos_linux() == []

    def test_pactl_nonzero_returncode(self) -> None:
        pactl = _FakeCompleted("", returncode=1, stderr="daemon down")
        with (
            patch.object(sys, "platform", "linux"),
            patch("shutil.which", _which_dispatch({"pactl"})),
            patch("subprocess.run", _run_dispatch({"pactl": pactl})),
        ):
            assert detect_capture_apos_linux() == []

    def test_pwdump_malformed_json(self) -> None:
        pw = _FakeCompleted("{not json")
        with (
            patch.object(sys, "platform", "linux"),
            patch("shutil.which", _which_dispatch({"pw-dump"})),
            patch("subprocess.run", _run_dispatch({"pw-dump": pw})),
        ):
            assert detect_capture_apos_linux() == []

    def test_pwdump_non_list_payload(self) -> None:
        pw = _FakeCompleted(json.dumps({"error": "boom"}))
        with (
            patch.object(sys, "platform", "linux"),
            patch("shutil.which", _which_dispatch({"pw-dump"})),
            patch("subprocess.run", _run_dispatch({"pw-dump": pw})),
        ):
            # pw-dump ran + produced valid JSON → session classified as
            # pipewire even though the payload carried no filter-chain.
            # But with no apos/raw we fall through to empty.
            assert detect_capture_apos_linux() == []

    def test_pwdump_node_without_props(self) -> None:
        pw = _FakeCompleted(
            json.dumps(
                [
                    {"type": "PipeWire:Interface:Node"},
                    {"type": "PipeWire:Interface:Port"},
                ],
            ),
        )
        with (
            patch.object(sys, "platform", "linux"),
            patch("shutil.which", _which_dispatch({"pw-dump"})),
            patch("subprocess.run", _run_dispatch({"pw-dump": pw})),
        ):
            # Valid-JSON + no matches + empty raw → empty list.
            assert detect_capture_apos_linux() == []


# ---------------------------------------------------------------------------
# Dataclass surface
# ---------------------------------------------------------------------------


class TestLinuxApoReport:
    @pytest.mark.parametrize(
        ("session", "labels", "expected_echo"),
        [
            ("pulseaudio", ["PulseAudio Echo Cancel"], True),
            ("pipewire", ["PipeWire Echo Cancel"], True),
            ("pulseaudio", ["PulseAudio RNNoise"], False),
            ("unknown", [], False),
        ],
    )
    def test_echo_cancel_active_mirrors_sentinels(
        self,
        session: str,
        labels: list[str],
        expected_echo: bool,
    ) -> None:
        report = LinuxApoReport(
            session_manager=session,
            known_apos=labels,
            echo_cancel_active=any(label in detector._ECHO_CANCEL_SENTINELS for label in labels),
        )
        assert report.echo_cancel_active is expected_echo


# ---------------------------------------------------------------------------
# Factory-level emitter — mirrors Windows `_emit_capture_apo_detection`.
# ---------------------------------------------------------------------------


class TestFactoryEmitsLinuxApoDetectionEvent:
    """``create_voice_pipeline`` logs ``voice_linux_apo_*`` once per boot.

    The Linux emitter runs alongside the Windows one. Tests use caplog
    to verify the right structured events fire on the right scan
    outcomes.
    """

    def _log_messages(self, caplog: pytest.LogCaptureFixture) -> list[str]:
        return [record.getMessage() for record in caplog.records]

    def _has_event(self, caplog: pytest.LogCaptureFixture, event_name: str) -> bool:
        needles = (f"'event': '{event_name}'", f'"event": "{event_name}"')
        return any(any(needle in msg for needle in needles) for msg in self._log_messages(caplog))

    def test_emits_detected_event_when_filter_present(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        import logging

        from sovyx.voice import factory

        fake_reports = [
            LinuxApoReport(
                session_manager="pipewire",
                known_apos=["PipeWire Echo Cancel", "PipeWire RNNoise"],
                raw_entries=["echo-cancel", "rnnoise"],
                echo_cancel_active=True,
            ),
        ]
        with (
            patch.object(sys, "platform", "linux"),
            patch(
                "sovyx.voice._apo_detector_linux.detect_capture_apos_linux",
                return_value=fake_reports,
            ),
            caplog.at_level(logging.INFO, logger="sovyx.voice.factory"),
        ):
            factory._emit_linux_capture_apo_detection(
                resolved_name="hw:0,0",
            )

        assert self._has_event(caplog, "voice_linux_apo_detected")
        assert self._has_event(caplog, "audio.apo.scan.linux")
        assert self._has_event(caplog, "audio.apo.echo_cancel_detected")

    def test_emits_scan_event_even_when_empty(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Dashboard must know the scan ran — ``audio.apo.scan.linux``
        fires on zero-hits too, so a silent bus doesn't look identical
        to a broken detector.
        """
        import logging

        from sovyx.voice import factory

        with (
            patch.object(sys, "platform", "linux"),
            patch(
                "sovyx.voice._apo_detector_linux.detect_capture_apos_linux",
                return_value=[],
            ),
            caplog.at_level(logging.INFO, logger="sovyx.voice.factory"),
        ):
            factory._emit_linux_capture_apo_detection(resolved_name="hw:0,0")

        # Zero-hit path: no per-endpoint detected event (we only emit
        # that when known_apos is non-empty), but scan + echo_cancel
        # telemetry always fire so the dashboard sees the heartbeat.
        assert not self._has_event(caplog, "voice_linux_apo_detected")
        assert self._has_event(caplog, "audio.apo.scan.linux")
        assert self._has_event(caplog, "audio.apo.echo_cancel_detected")

    def test_noop_on_non_linux(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Non-Linux platforms must not emit Linux telemetry, even if
        the detector is (somehow) called.
        """
        import logging

        from sovyx.voice import factory

        with (
            patch.object(sys, "platform", "win32"),
            patch(
                "sovyx.voice._apo_detector_linux.detect_capture_apos_linux",
                return_value=[],
            ),
            caplog.at_level(logging.INFO, logger="sovyx.voice.factory"),
        ):
            factory._emit_linux_capture_apo_detection(resolved_name="some-mic")

        assert not self._has_event(caplog, "audio.apo.scan.linux")
        assert not self._has_event(caplog, "audio.apo.echo_cancel_detected")
        assert not self._has_event(caplog, "voice_linux_apo_detected")

    def test_emitter_survives_detector_exception(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """A subprocess blow-up MUST NOT crash pipeline startup.

        The emitter swallows the exception, logs at DEBUG, and returns
        cleanly — the scan telemetry is skipped this boot but the
        pipeline continues.
        """
        import logging

        from sovyx.voice import factory

        def _boom() -> list[LinuxApoReport]:
            msg = "pactl mysteriously fell over"
            raise RuntimeError(msg)

        with (
            patch.object(sys, "platform", "linux"),
            patch(
                "sovyx.voice._apo_detector_linux.detect_capture_apos_linux",
                side_effect=_boom,
            ),
            caplog.at_level(logging.DEBUG, logger="sovyx.voice.factory"),
        ):
            factory._emit_linux_capture_apo_detection(resolved_name="hw:0,0")

        # Debug record confirms we hit the failure path, but no INFO
        # scan events fired because we returned before the logger
        # calls.
        assert self._has_event(caplog, "voice_linux_apo_detection_failed")
        assert not self._has_event(caplog, "audio.apo.scan.linux")

    def test_session_manager_propagates_to_scan_event(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Dashboard uses ``voice.session_manager`` to render the
        right label (PipeWire vs PulseAudio vs mixed). Regression
        guard: the field must survive the emitter path intact.
        """
        import logging

        from sovyx.voice import factory

        fake_reports = [
            LinuxApoReport(
                session_manager="mixed",
                known_apos=["PulseAudio Echo Cancel"],
                raw_entries=["module-echo-cancel"],
                echo_cancel_active=True,
            ),
        ]
        with (
            patch.object(sys, "platform", "linux"),
            patch(
                "sovyx.voice._apo_detector_linux.detect_capture_apos_linux",
                return_value=fake_reports,
            ),
            caplog.at_level(logging.INFO, logger="sovyx.voice.factory"),
        ):
            factory._emit_linux_capture_apo_detection(resolved_name="hw:1,0")

        messages = self._log_messages(caplog)
        scan_messages = [msg for msg in messages if "audio.apo.scan.linux" in msg]
        assert scan_messages, "expected one audio.apo.scan.linux record"
        assert any("mixed" in msg for msg in scan_messages)
