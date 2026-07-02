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


class TestBothToolsAnswer:
    """pactl AND pw-dump both answering is the STANDARD PipeWire
    desktop (pipewire-pulse serves pactl) — that is ``pipewire``, not
    ``mixed``; ``mixed`` requires a REAL PulseAudio daemon process
    (audit finding LINUX-19)."""

    def test_standard_pipewire_desktop_classified_pipewire(self) -> None:
        pactl = _FakeCompleted(_pactl_output("module-echo-cancel"))
        pw = _FakeCompleted(
            _pw_dump_output(
                {
                    "node.name": "effect.rnnoise.source",
                    "factory.name": "filter-chain",
                },
            ),
        )
        # pgrep absent from the dispatch table → FileNotFoundError →
        # the hybrid discriminator reports no real PA daemon.
        with (
            patch.object(sys, "platform", "linux"),
            patch("shutil.which", _which_dispatch({"pactl", "pw-dump"})),
            patch("subprocess.run", _run_dispatch({"pactl": pactl, "pw-dump": pw})),
        ):
            reports = detect_capture_apos_linux()
        report = reports[0]
        assert report.session_manager == "pipewire"
        assert "PulseAudio Echo Cancel" in report.known_apos
        assert "PipeWire RNNoise" in report.known_apos
        assert report.echo_cancel_active is True

    def test_compat_layer_pgrep_hit_still_pipewire(self) -> None:
        # pgrep DOES match a pulseaudio process, but its cmdline names
        # the PipeWire shim — not a real PA daemon.
        pactl = _FakeCompleted(_pactl_output("module-echo-cancel"))
        pw = _FakeCompleted(_pw_dump_output({"node.name": "effect.rnnoise.source"}))
        pgrep = _FakeCompleted("812 /usr/bin/pipewire-pulse\n")
        with (
            patch.object(sys, "platform", "linux"),
            patch("shutil.which", _which_dispatch({"pactl", "pw-dump", "pgrep"})),
            patch(
                "subprocess.run",
                _run_dispatch({"pactl": pactl, "pw-dump": pw, "pgrep": pgrep}),
            ),
        ):
            reports = detect_capture_apos_linux()
        assert reports[0].session_manager == "pipewire"

    def test_real_pulseaudio_daemon_coexisting_classified_mixed(self) -> None:
        # A genuine dual-daemon pathology: pgrep finds a pulseaudio
        # process whose cmdline does NOT mention pipewire.
        pactl = _FakeCompleted(_pactl_output("module-echo-cancel"))
        pw = _FakeCompleted(_pw_dump_output({"node.name": "effect.rnnoise.source"}))
        pgrep = _FakeCompleted("1234 /usr/bin/pulseaudio --daemonize=no\n")
        with (
            patch.object(sys, "platform", "linux"),
            patch("shutil.which", _which_dispatch({"pactl", "pw-dump", "pgrep"})),
            patch(
                "subprocess.run",
                _run_dispatch({"pactl": pactl, "pw-dump": pw, "pgrep": pgrep}),
            ),
        ):
            reports = detect_capture_apos_linux()
        assert reports[0].session_manager == "mixed"


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


class TestPactlNonzeroDispatch:
    """v0.38.0 / W3.E1 — F2-M08 (audit §3.O) closure.

    The single opaque ``voice_apo_linux_pactl_nonzero`` DEBUG log was
    split into 3 specific WARNING events keyed off a 512-char stderr
    excerpt so operators can diagnose Linux capture silence from
    production logs. These tests pin the dispatch contract.
    """

    @pytest.mark.parametrize(
        ("stderr_text", "expected_event"),
        [
            ("pactl: command not found", "voice_apo_linux_pactl_command_failed"),
            ("/usr/bin/pactl: No such file or directory", "voice_apo_linux_pactl_command_failed"),
            ("Connection failure: Connection refused", "voice_apo_linux_pactl_daemon_unavailable"),
            ("pa_context_connect() failed", "voice_apo_linux_pactl_nonzero"),
            ("", "voice_apo_linux_pactl_nonzero"),
        ],
    )
    def test_dispatch_event_name_by_stderr_keyword(
        self,
        stderr_text: str,
        expected_event: str,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Each stderr keyword shape selects the right structured event."""
        # The command_failed branch ignores the returncode shape; the
        # daemon_unavailable branch requires returncode == 1.
        rc = 1 if "Connection refused" in stderr_text else 127
        pactl = _FakeCompleted("", returncode=rc, stderr=stderr_text)
        caplog.set_level("WARNING", logger="sovyx.voice._apo_detector_linux")
        with (
            patch.object(sys, "platform", "linux"),
            patch("shutil.which", _which_dispatch({"pactl"})),
            patch("subprocess.run", _run_dispatch({"pactl": pactl})),
        ):
            assert detect_capture_apos_linux() == []
        events = [r.event_dict.get("event") for r in caplog.records if hasattr(r, "event_dict")]
        # Fallback for stdlib LogRecord: search the message text.
        if not events:
            events = [r.getMessage() for r in caplog.records]
        assert any(expected_event in str(e) for e in events), (
            f"expected {expected_event!r} in caplog events; got {events!r}"
        )

    def test_all_branches_emit_warning_level(self, caplog: pytest.LogCaptureFixture) -> None:
        """The split keeps WARNING level (not DEBUG) so operators see it."""
        pactl = _FakeCompleted("", returncode=2, stderr="some other error")
        caplog.set_level("WARNING", logger="sovyx.voice._apo_detector_linux")
        with (
            patch.object(sys, "platform", "linux"),
            patch("shutil.which", _which_dispatch({"pactl"})),
            patch("subprocess.run", _run_dispatch({"pactl": pactl})),
        ):
            detect_capture_apos_linux()
        # At least one WARNING-level record was emitted by the detector.
        warn_records = [r for r in caplog.records if r.levelname == "WARNING"]
        assert warn_records, "pactl-nonzero branch must emit at WARNING level"

    def test_stderr_excerpt_truncates_to_512_chars(self, caplog: pytest.LogCaptureFixture) -> None:
        """Long stderr is truncated to 512 chars to keep log lines bounded."""
        long_stderr = "x" * 1024
        pactl = _FakeCompleted("", returncode=2, stderr=long_stderr)
        caplog.set_level("WARNING", logger="sovyx.voice._apo_detector_linux")
        with (
            patch.object(sys, "platform", "linux"),
            patch("shutil.which", _which_dispatch({"pactl"})),
            patch("subprocess.run", _run_dispatch({"pactl": pactl})),
        ):
            detect_capture_apos_linux()
        # Find the structured log record. structlog routes through stdlib
        # so the LogRecord is reachable via caplog; we check the message
        # rendered length never exceeds 512+key overhead.
        messages = [r.getMessage() for r in caplog.records]
        assert messages, "no log records captured"
        # Ensure the FULL 1024-char stderr was NOT inlined verbatim.
        assert all("x" * 1024 not in m for m in messages), (
            "stderr_excerpt must be truncated, not full 1024 chars"
        )

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
        # Mission H2 §T4.4 — neutral sibling events fire alongside legacy.
        assert self._has_event(caplog, "audio.capture_chain.scan.linux")
        assert self._has_event(caplog, "audio.capture_chain.echo_cancel_detected")

    def test_apo_detector_dual_emit_kill_switch_suppresses_legacy(
        self,
        caplog: pytest.LogCaptureFixture,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Mission H2 — `SOVYX_TUNING__VOICE__APO_DETECTOR_DUAL_EMIT_ENABLED=false`
        suppresses the legacy `audio.apo.scan.linux` +
        `audio.apo.echo_cancel_detected` emissions while preserving the
        neutral `audio.capture_chain.*` siblings always-on. Lets
        operators pre-test the v0.51.0 STRICT flip behaviour without
        waiting for the tag bump.
        """
        import logging

        from sovyx.voice import factory
        from sovyx.voice._apo_detector_linux import LinuxApoReport

        monkeypatch.setenv("SOVYX_TUNING__VOICE__APO_DETECTOR_DUAL_EMIT_ENABLED", "false")
        fake_reports = [
            LinuxApoReport(
                session_manager="pipewire",
                known_apos=("module-echo-cancel",),
                raw_entries=(),
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
            factory._emit_linux_capture_apo_detection(resolved_name="hw:0,0")

        # Neutral events ALWAYS fire (anti-pattern #34 inverse).
        assert self._has_event(caplog, "audio.capture_chain.scan.linux")
        assert self._has_event(caplog, "audio.capture_chain.echo_cancel_detected")
        # Legacy events SUPPRESSED by the kill switch.
        assert not self._has_event(caplog, "audio.apo.scan.linux")
        assert not self._has_event(caplog, "audio.apo.echo_cancel_detected")

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
        # Mission H2 §T4.4 — neutral siblings also fire on zero-hit path.
        assert self._has_event(caplog, "audio.capture_chain.scan.linux")
        assert self._has_event(caplog, "audio.capture_chain.echo_cancel_detected")

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
