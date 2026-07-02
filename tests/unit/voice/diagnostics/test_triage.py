"""Subprocess-level regression tests for the triage analyzer.

Exercises the legacy ``tools/voice_diag_triage.py`` wrapper (which now
imports from :mod:`sovyx.voice.diagnostics.triage`) end-to-end via
``subprocess.run`` to guarantee back-compat with analyst workflows that
shell out to the script. Direct in-process tests for the typed public
API live alongside this module in ``test_triage_api.py``.

Validates schema + the H1..H10 hypotheses against synthetic tarballs
simulating Linux / Windows / macOS toolkit outputs.
"""

from __future__ import annotations

import json
import subprocess
import sys
import tarfile
import zipfile
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[4]
TRIAGE_SCRIPT = REPO_ROOT / "tools" / "voice_diag_triage.py"


def _make_summary(**overrides: Any) -> dict:
    base = {
        "schema_version": 1,
        "tool": "sovyx-voice-diag",
        "tool_version": "4.3.0",
        "captured_at_utc": "2026-04-24T22:00:00Z",
        "host": "test-host",
        "os": "linux",
        "os_version": "Linux Mint 22.1",
        "outdir": "/home/test/sovyx-diag-test",
        "status": "complete",
        "final_exit_code": 0,
    }
    base.update(overrides)
    return base


def _make_linux_tarball(
    tmp_path: Path,
    *,
    summary: dict | None = None,
    alerts: list[dict] | None = None,
    captures: dict[str, dict] | None = None,
) -> Path:
    """Build synthetic Linux tarball with the given fixtures."""
    root_dir = tmp_path / "sovyx-diag-test-host-20260424T220000Z-deadbeef"
    (root_dir / "_diagnostics").mkdir(parents=True)

    summary = summary or _make_summary()
    (root_dir / "SUMMARY.json").write_text(json.dumps(summary, indent=2))

    alerts_file = root_dir / "_diagnostics" / "alerts.jsonl"
    alerts_file.write_text("\n".join(json.dumps(a) for a in (alerts or [])))

    if captures:
        for cid, payload in captures.items():
            cap_dir = root_dir / "E_portaudio" / "captures" / cid
            cap_dir.mkdir(parents=True)
            (cap_dir / "analysis.json").write_text(json.dumps(payload["analysis"]))
            (cap_dir / "silero.json").write_text(json.dumps(payload["silero"]))

    (root_dir / "MANIFEST.md").write_text("# MANIFEST")

    tar_path = tmp_path / "fixture.tar.gz"
    with tarfile.open(tar_path, "w:gz") as tar:
        tar.add(root_dir, arcname=root_dir.name)
    return tar_path


def _run_triage(archive: Path) -> tuple[int, str, str]:
    """Run triage script as subprocess. Returns (rc, stdout, stderr)."""
    proc = subprocess.run(
        [sys.executable, str(TRIAGE_SCRIPT), str(archive)],
        capture_output=True,
        text=True,
        timeout=60,
        encoding="utf-8",
    )
    return proc.returncode, proc.stdout, proc.stderr


class TestSchemaValidation:
    """Schema validation gate — must pass before any analysis runs."""

    def test_valid_schema_passes(self, tmp_path: Path) -> None:
        tar = _make_linux_tarball(tmp_path)
        rc, out, _ = _run_triage(tar)
        assert rc == 0
        assert "Required fields present" in out

    def test_missing_required_field_reported(self, tmp_path: Path) -> None:
        # The only REQUIRED field after the v4.3 schema patch is
        # `schema_version` — host is now in RECOMMENDED (the toolkit
        # uses `hostname`, the D1 spec used `host`; the helper accepts
        # either via the `host|hostname` alternation).
        bad_summary = _make_summary()
        del bad_summary["schema_version"]
        tar = _make_linux_tarball(tmp_path, summary=bad_summary)
        rc, out, _ = _run_triage(tar)
        # Validation failed but still ran analysis (just reports the gap).
        assert "Schema validation FAILED" in out
        assert "missing required: `schema_version`" in out

    def test_missing_recommended_field_reported(self, tmp_path: Path) -> None:
        # Build a summary that has schema_version but no host or hostname.
        bad_summary = _make_summary()
        del bad_summary["host"]  # base summary uses `host`; remove it.
        tar = _make_linux_tarball(tmp_path, summary=bad_summary)
        rc, out, _ = _run_triage(tar)
        assert rc == 0  # not a hard failure
        assert "missing recommended: `host|hostname`" in out

    def test_missing_summary_returns_error(self, tmp_path: Path) -> None:
        # Tarball with no SUMMARY.json.
        root_dir = tmp_path / "broken"
        root_dir.mkdir()
        (root_dir / "MANIFEST.md").write_text("# nothing here")
        tar = tmp_path / "broken.tar.gz"
        with tarfile.open(tar, "w:gz") as t:
            t.add(root_dir, arcname=root_dir.name)
        rc, _, err = _run_triage(tar)
        assert rc == 1
        assert "SUMMARY.json not found" in err

    def test_null_kernel_line_does_not_crash_detect_toolkit(self, tmp_path: Path) -> None:
        """Regression for the Voice-Bash-Diag-Smoke gate failure.

        Bash SUMMARY.json may emit ``"kernel_line": null`` when ``uname``
        was unavailable / returned non-zero. JSON null decodes to
        Python ``None``, and ``dict.get(key, default)`` does NOT
        substitute the default for null values — only for missing
        keys. Pre-fix ``_detect_toolkit`` then evaluated
        ``"Darwin" in None`` → ``TypeError: argument of type 'NoneType'
        is not iterable``. Fix coerces None → "" via ``... or ""``
        before the substring checks. Same defensive pattern applied
        to every other ``a.get("message", "")`` site that feeds into
        a substring ``in`` check.
        """
        bad_summary = _make_summary()
        bad_summary["host_capability_summary"] = {"kernel_line": None}
        tar = _make_linux_tarball(tmp_path, summary=bad_summary)
        rc, out, err = _run_triage(tar)
        # Triage should NOT crash; should default-detect linux toolkit
        # (since tool name + null kernel line don't say macos/windows).
        assert rc == 0, f"triage crashed: stderr={err!r}"
        # Sanity: the run produced a verdict instead of a TypeError.
        assert "TypeError" not in err

    def test_null_alert_message_does_not_crash_h1(self, tmp_path: Path) -> None:
        """Regression for the same null-coercion bug class on
        ``alerts[*].message``. Bash MAY emit ``message: null`` for an
        alert that fired without a structured message body. The H1
        evaluator's ``"silence_across" in a.get("message", "")`` would
        previously crash with the same TypeError; now it coerces None
        → "" before the ``in`` check.
        """
        bad_summary = _make_summary()
        bad_alerts = [{"severity": "warning", "message": None}]
        tar = _make_linux_tarball(tmp_path, summary=bad_summary, alerts=bad_alerts)
        rc, _, err = _run_triage(tar)
        assert rc == 0, f"triage crashed: stderr={err!r}"
        assert "TypeError" not in err


class TestHypothesisH1MicDead:
    """H1 — Microphone signal destroyed (cross-OS)."""

    def test_silence_alert_triggers_h1_high_confidence(self, tmp_path: Path) -> None:
        alerts = [
            {
                "severity": "error",
                "state": "S_ACTIVE",
                "message": "silence_across_default_source_captures: ALL 3 voice "
                "captures silent (rms=-90,-91,-89, vad=0.001) -- mic dead",
                "at": {"utc_iso_ns": "2026-04-24T22:01:00Z", "monotonic_ns": 1000},
            }
        ]
        captures = {
            f"W{i}_pa_test": {
                "analysis": {
                    "capture_id": f"W{i}",
                    "rms_dbfs": -90.0,
                    "classification": "silence",
                },
                "silero": {"available": True, "max_prob": 0.001},
            }
            for i in (11, 12, 13)
        }
        tar = _make_linux_tarball(tmp_path, alerts=alerts, captures=captures)
        rc, out, _ = _run_triage(tar)
        assert rc == 0
        assert "H1: Microphone signal destroyed" in out
        # Both alert (0.5) AND captures (0.4) push confidence to ~0.9.
        assert "confidence=0.90" in out

    def test_non_silent_captures_lower_h1(self, tmp_path: Path) -> None:
        captures = {
            f"W{i}_pa_test": {
                "analysis": {"capture_id": f"W{i}", "rms_dbfs": -22.0, "classification": "voice"},
                "silero": {"available": True, "max_prob": 0.85},
            }
            for i in (11, 12, 13)
        }
        tar = _make_linux_tarball(tmp_path, captures=captures)
        rc, out, _ = _run_triage(tar)
        assert rc == 0
        # Without silence alert and with healthy captures, H1 should be
        # explicitly contradicted.
        assert "Evidence AGAINST" in out
        assert "non-silent" in out


class TestHypothesisH2VoiceClarity:
    """H2 — Windows Voice Clarity APO (anti-pattern #21)."""

    def test_voice_clarity_active_endpoint_triggers_h2(self, tmp_path: Path) -> None:
        summary = _make_summary(
            tool="diagnose-voice-windows",
            os="windows",
            os_version="Windows 11 23H2 (build 22631.4317)",
            outdir="C:/Users/test/voice-diag",
            audio_endpoints=[
                {
                    "is_active": True,
                    "friendly_name": "Microphone (Realtek HD Audio)",
                    "voice_clarity_active": True,
                    "known_apos": ["Windows Voice Clarity"],
                    "all_clsids": [],
                }
            ],
            live_captures={
                "shared": {"ok": True, "rms_dbfs": -92.5, "silero_max_prob": 0.002},
                "exclusive": {"ok": True, "rms_dbfs": -25.3, "silero_max_prob": 0.91},
                "verdict": "voice_clarity_destroying_apo_confirmed",
                "delta_rms_dbfs": 67.2,
                "delta_vad": 0.908,
            },
        )
        # Use zip for Windows toolkit emulation.
        root_dir = tmp_path / "win-test"
        (root_dir / "_diagnostics").mkdir(parents=True)
        (root_dir / "SUMMARY.json").write_text(json.dumps(summary))
        (root_dir / "_diagnostics" / "alerts.jsonl").write_text("")
        (root_dir / "MANIFEST.md").write_text("# Win")
        zip_path = tmp_path / "win.zip"
        with zipfile.ZipFile(zip_path, "w") as z:
            for p in root_dir.rglob("*"):
                if p.is_file():
                    z.write(p, p.relative_to(tmp_path))
        rc, out, _ = _run_triage(zip_path)
        assert rc == 0
        # Both endpoint flag (0.4) AND comparator (0.6) push H2 to 1.0.
        assert "H2: Windows Voice Clarity APO" in out
        assert "confidence=1.00" in out
        assert "capture_wasapi_exclusive=true" in out

    def test_apo_not_culprit_verdict_negates_h2(self, tmp_path: Path) -> None:
        summary = _make_summary(
            tool="diagnose-voice-windows",
            os="windows",
            os_version="Windows 11",
            outdir="C:/Users/test",
            live_captures={
                "shared": {"ok": True, "rms_dbfs": -25.0, "silero_max_prob": 0.85},
                "exclusive": {"ok": True, "rms_dbfs": -26.0, "silero_max_prob": 0.83},
                "verdict": "apo_not_culprit",
                "delta_rms_dbfs": -1.0,
                "delta_vad": -0.02,
            },
        )
        root_dir = tmp_path / "win-test"
        (root_dir / "_diagnostics").mkdir(parents=True)
        (root_dir / "SUMMARY.json").write_text(json.dumps(summary))
        (root_dir / "_diagnostics" / "alerts.jsonl").write_text("")
        (root_dir / "MANIFEST.md").write_text("# Win")
        zip_path = tmp_path / "win.zip"
        with zipfile.ZipFile(zip_path, "w") as z:
            for p in root_dir.rglob("*"):
                if p.is_file():
                    z.write(p, p.relative_to(tmp_path))
        rc, out, _ = _run_triage(zip_path)
        assert rc == 0
        # Either not in top hypotheses or low confidence.
        assert "Evidence AGAINST" in out


class TestHypothesisH3MacOSInterceptor:
    """H3 — macOS HAL plug-in interceptor (Krisp, BlackHole, etc)."""

    def test_krisp_hal_detected_triggers_h3(self, tmp_path: Path) -> None:
        summary = _make_summary(
            tool="sovyx-voice-diag-mac",
            os="macos",
            os_version="14.5.0",
            outdir="/Users/test/sovyx-diag-mac",
        )
        root_dir = tmp_path / "mac-test"
        (root_dir / "_diagnostics").mkdir(parents=True)
        (root_dir / "D_coreaudiod").mkdir(parents=True)
        (root_dir / "SUMMARY.json").write_text(json.dumps(summary))
        (root_dir / "_diagnostics" / "alerts.jsonl").write_text("")
        (root_dir / "MANIFEST.md").write_text("# Mac")
        (root_dir / "D_coreaudiod" / "hal_classifier.json").write_text(
            json.dumps(
                {
                    "interceptors_detected": [
                        {
                            "name": "Krisp Audio.driver",
                            "matched_vendor": "Krisp noise suppression (analog of Voice Clarity APO)",
                            "path": "/Library/Audio/Plug-Ins/HAL/Krisp Audio.driver",
                        },
                    ]
                }
            )
        )
        tar = tmp_path / "mac.tar.gz"
        with tarfile.open(tar, "w:gz") as t:
            t.add(root_dir, arcname=root_dir.name)
        rc, out, _ = _run_triage(tar)
        assert rc == 0
        assert "H3: macOS HAL plug-in" in out
        assert "Krisp" in out


class TestHypothesisH5MicPermissionDenied:
    """H5 — Microphone permission denied (cross-OS)."""

    def test_macos_tcc_denied_triggers_h5(self, tmp_path: Path) -> None:
        summary = _make_summary(
            tool="sovyx-voice-diag-mac",
            os="macos",
            os_version="14.5.0",
            outdir="/Users/test",
        )
        root_dir = tmp_path / "mac-test"
        (root_dir / "_diagnostics").mkdir(parents=True)
        (root_dir / "F_session").mkdir(parents=True)
        (root_dir / "SUMMARY.json").write_text(json.dumps(summary))
        (root_dir / "_diagnostics" / "alerts.jsonl").write_text("")
        (root_dir / "MANIFEST.md").write_text("# Mac")
        (root_dir / "F_session" / "tcc_mic_consents.json").write_text(
            json.dumps(
                {
                    "fda_status": "granted",
                    "interesting_chain_clients": [
                        {
                            "client": "/Users/test/python",
                            "auth": "denied",
                            "reason": "user_consent",
                            "modified": "2026-04-20T10:00:00Z",
                        },
                    ],
                }
            )
        )
        tar = tmp_path / "mac.tar.gz"
        with tarfile.open(tar, "w:gz") as t:
            t.add(root_dir, arcname=root_dir.name)
        rc, out, _ = _run_triage(tar)
        assert rc == 0
        assert "H5: Microphone permission denied" in out


class TestHypothesisH6SelftestFailed:
    """H6 — Analyzer selftest failed (poisons all downstream metrics)."""

    def test_selftest_fail_triggers_h6_max_confidence(self, tmp_path: Path) -> None:
        summary = _make_summary(analyzer_selftest_status="fail")
        tar = _make_linux_tarball(tmp_path, summary=summary)
        rc, out, _ = _run_triage(tar)
        assert rc == 0
        assert "H6: Analyzer selftest failed" in out
        # selftest_fail is +1.0 weight — capped at 1.0.
        assert "confidence=1.00" in out


class TestSelftestStatusAccessor:
    """LINUX-8 — analyzer_selftest_status lives TOP-LEVEL in the
    Windows-v2 SUMMARY but NESTED under 'calibration' in the Linux
    bash finalizer output; both must feed H6 via one accessor."""

    def test_nested_calibration_shape_read(self) -> None:
        from sovyx.voice.diagnostics.triage import _read_selftest_status

        summary = {"calibration": {"analyzer_selftest_status": "fail"}}
        assert _read_selftest_status(summary) == "fail"

    def test_top_level_shape_read(self) -> None:
        from sovyx.voice.diagnostics.triage import _read_selftest_status

        assert _read_selftest_status({"analyzer_selftest_status": "pass"}) == "pass"

    def test_nested_wins_over_top_level(self) -> None:
        from sovyx.voice.diagnostics.triage import _read_selftest_status

        summary = {
            "calibration": {"analyzer_selftest_status": "fail"},
            "analyzer_selftest_status": "pass",
        }
        assert _read_selftest_status(summary) == "fail"

    def test_missing_returns_default(self) -> None:
        from sovyx.voice.diagnostics.triage import _read_selftest_status

        assert _read_selftest_status({}) is None
        assert _read_selftest_status({}, default="?") == "?"

    def test_h6_fires_for_linux_nested_shape(self, tmp_path: Path) -> None:
        # LINUX-8 regression: real Linux tarballs nest the key under
        # 'calibration' (finalize.sh) with NO top-level copy — pre-fix
        # H6 accrued zero weight on every Linux tarball.
        summary = _make_summary(
            calibration={
                "analyzer_selftest_status": "fail",
                "guardian_status": "ok",
                "operator_prompts_status": "ok",
            },
        )
        tar = _make_linux_tarball(tmp_path, summary=summary)
        rc, out, _ = _run_triage(tar)
        assert rc == 0
        assert "H6: Analyzer selftest failed" in out
        assert "confidence=1.00" in out


class TestH10AmixerBlockConfinement:
    """LINUX-20 — the H10 amixer signature must be confined to the
    'Mic Boost' control block; re.DOTALL spanning across blocks
    false-positived on healthy mixers."""

    _HEALTHY_BOOST_THEN_ZEROED_OTHER = (
        "Simple mixer control 'Mic Boost',0\n"
        "  Capabilities: volume\n"
        "  Limits: 0 - 3\n"
        "  Front Left: 3 [100%] [36.00dB]\n"
        "  Front Right: 3 [100%] [36.00dB]\n"
        "Simple mixer control 'Front Mic',0\n"
        "  Capabilities: cvolume cswitch\n"
        "  Limits: Capture 0 - 80\n"
        "  Front Left: Capture 0 [0%] [-34.00dB] [off]\n"
        "  Front Right: Capture 0 [0%] [-34.00dB] [off]\n"
    )

    _ZEROED_BOOST = (
        "Simple mixer control 'Mic Boost',0\n"
        "  Capabilities: volume\n"
        "  Limits: 0 - 3\n"
        "  Front Left: 0 [0%] [0.00dB]\n"
        "  Front Right: 0 [0%] [0.00dB]\n"
    )

    def test_zeroed_mic_boost_detected(self) -> None:
        from sovyx.voice.diagnostics.triage import _amixer_mic_boost_zeroed

        assert _amixer_mic_boost_zeroed(self._ZEROED_BOOST) is True

    def test_nonzero_boost_with_later_zeroed_control_not_flagged(self) -> None:
        # The exact LINUX-20 false-positive shape: healthy Mic Boost at
        # 100%, unused 'Front Mic' parked at 0 [0%] later in the dump.
        from sovyx.voice.diagnostics.triage import _amixer_mic_boost_zeroed

        assert _amixer_mic_boost_zeroed(self._HEALTHY_BOOST_THEN_ZEROED_OTHER) is False

    def test_capture_prefixed_reading_detected(self) -> None:
        from sovyx.voice.diagnostics.triage import _amixer_mic_boost_zeroed

        dump = (
            "Simple mixer control 'Mic Boost',0\n"
            "  Limits: Capture 0 - 3\n"
            "  Front Left: Capture 0 [0%] [0.00dB]\n"
        )
        assert _amixer_mic_boost_zeroed(dump) is True

    def test_no_mic_boost_block_not_flagged(self) -> None:
        from sovyx.voice.diagnostics.triage import _amixer_mic_boost_zeroed

        dump = (
            "Simple mixer control 'Front Mic',0\n"
            "  Limits: Capture 0 - 80\n"
            "  Front Left: Capture 0 [0%] [-34.00dB] [off]\n"
        )
        assert _amixer_mic_boost_zeroed(dump) is False


class TestHypothesisH7NetworkBlocked:
    """H7 — Network unreachable to LLM provider."""

    def test_unreachable_provider_triggers_h7(self, tmp_path: Path) -> None:
        summary = _make_summary(
            tool="diagnose-voice-windows",
            os="windows",
            os_version="Windows 11",
            outdir="C:/Users/test",
            network_llm=[
                {
                    "host": "api.anthropic.com",
                    "port": 443,
                    "dns_ok": False,
                    "tcp_ok": False,
                    "rtt_ms": None,
                },
                {
                    "host": "api.openai.com",
                    "port": 443,
                    "dns_ok": True,
                    "tcp_ok": False,
                    "rtt_ms": None,
                },
            ],
        )
        root_dir = tmp_path / "win-test"
        (root_dir / "_diagnostics").mkdir(parents=True)
        (root_dir / "SUMMARY.json").write_text(json.dumps(summary))
        (root_dir / "_diagnostics" / "alerts.jsonl").write_text("")
        (root_dir / "MANIFEST.md").write_text("# Win")
        zip_path = tmp_path / "win.zip"
        with zipfile.ZipFile(zip_path, "w") as z:
            for p in root_dir.rglob("*"):
                if p.is_file():
                    z.write(p, p.relative_to(tmp_path))
        rc, out, _ = _run_triage(zip_path)
        assert rc == 0
        assert "H7: Network blocked LLM" in out
        # 2 unreachable * 0.3 = 0.6 confidence.
        assert "api.anthropic.com" in out


class TestHypothesisH8DaemonCrash:
    """H8 — Sovyx daemon crashed / unhandled exception in voice pipeline."""

    def test_python_traceback_in_log_triggers_h8(self, tmp_path: Path) -> None:
        root_dir = tmp_path / "diag-crash-test"
        (root_dir / "_diagnostics").mkdir(parents=True)
        (root_dir / "G_sovyx").mkdir(parents=True)
        (root_dir / "SUMMARY.json").write_text(json.dumps(_make_summary()))
        (root_dir / "_diagnostics" / "alerts.jsonl").write_text("")
        (root_dir / "MANIFEST.md").write_text("# manifest")
        (root_dir / "G_sovyx" / "sovyx_log_tail.txt").write_text(
            "2026-04-24 22:00:00 INFO  starting voice\n"
            "2026-04-24 22:00:01 ERROR voice failed\n"
            "Traceback (most recent call last):\n"
            "  File '/usr/lib/sovyx/voice/_capture_task.py', line 100\n"
            "    PortAudioError: Invalid sample rate\n"
        )
        tar = tmp_path / "fixture.tar.gz"
        with tarfile.open(tar, "w:gz") as t:
            t.add(root_dir, arcname=root_dir.name)
        rc, out, _ = _run_triage(tar)
        assert rc == 0
        assert "H8: Sovyx daemon crashed" in out
        assert "Python traceback" in out

    def test_no_crash_log_no_h8(self, tmp_path: Path) -> None:
        # Plain valid tarball with no crash logs.
        tar = _make_linux_tarball(tmp_path)
        rc, out, _ = _run_triage(tar)
        assert rc == 0
        # H8 should be absent or have 0 confidence (filtered out from report).
        assert "H8: Sovyx daemon crashed" not in out


class TestHypothesisH9HardwareGap:
    """H9 — Hardware capability gap (no capture-capable device)."""

    def test_alsa_unresolved_target_card_triggers_h9(self, tmp_path: Path) -> None:
        root_dir = tmp_path / "diag-no-card-test"
        (root_dir / "_diagnostics").mkdir(parents=True)
        (root_dir / "C_alsa").mkdir(parents=True)
        (root_dir / "SUMMARY.json").write_text(json.dumps(_make_summary()))
        (root_dir / "_diagnostics" / "alerts.jsonl").write_text("")
        (root_dir / "MANIFEST.md").write_text("# m")
        (root_dir / "C_alsa" / "target_card.txt").write_text("UNRESOLVED")
        tar = tmp_path / "fixture.tar.gz"
        with tarfile.open(tar, "w:gz") as t:
            t.add(root_dir, arcname=root_dir.name)
        rc, out, _ = _run_triage(tar)
        assert rc == 0
        assert "H9: Hardware capability gap" in out
        assert "UNRESOLVED" in out

    def test_windows_no_active_endpoints_triggers_h9(self, tmp_path: Path) -> None:
        summary = _make_summary(
            tool="diagnose-voice-windows",
            os="windows",
            os_version="Windows 11",
            outdir="C:/Users/test",
            audio_endpoints=[
                {"is_active": False, "friendly_name": "Disabled mic 1"},
                {"is_active": False, "friendly_name": "Disabled mic 2"},
            ],
        )
        root_dir = tmp_path / "win-no-active"
        (root_dir / "_diagnostics").mkdir(parents=True)
        (root_dir / "SUMMARY.json").write_text(json.dumps(summary))
        (root_dir / "_diagnostics" / "alerts.jsonl").write_text("")
        (root_dir / "MANIFEST.md").write_text("# m")
        zip_path = tmp_path / "win.zip"
        with zipfile.ZipFile(zip_path, "w") as z:
            for p in root_dir.rglob("*"):
                if p.is_file():
                    z.write(p, p.relative_to(tmp_path))
        rc, out, _ = _run_triage(zip_path)
        assert rc == 0
        assert "H9: Hardware capability gap" in out
        assert "0 ACTIVE endpoints" in out


class TestExtractDirMode:
    """--extract-dir for analysis on already-extracted dirs."""

    def test_extract_dir_mode_works(self, tmp_path: Path) -> None:
        root_dir = tmp_path / "already-extracted"
        (root_dir / "_diagnostics").mkdir(parents=True)
        (root_dir / "SUMMARY.json").write_text(json.dumps(_make_summary()))
        (root_dir / "_diagnostics" / "alerts.jsonl").write_text("")
        (root_dir / "MANIFEST.md").write_text("# already")
        proc = subprocess.run(
            [sys.executable, str(TRIAGE_SCRIPT), str(root_dir), "--extract-dir"],
            capture_output=True,
            text=True,
            timeout=60,
            encoding="utf-8",
        )
        assert proc.returncode == 0
        assert "Required fields present" in proc.stdout
