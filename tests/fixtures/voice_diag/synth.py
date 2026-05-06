"""Synthetic diag-tarball builder.

Builds deterministic ``sovyx-voice-diag*.tar.gz`` archives from a
compact :class:`Scenario` spec. The archives are functionally
indistinguishable from a real diag run (same SUMMARY.json shape,
same per-step file layout, same alerts.jsonl semantics) for the
triage analyzer's purposes.

Usage::

    from tests.fixtures.voice_diag import build_tarball, scenario_h10_mixer_attenuated

    tarball = build_tarball(scenario_h10_mixer_attenuated(), tmp_path / "h10.tar.gz")
    triage = triage_tarball(tarball)
    assert triage.winner is not None
    assert triage.winner.hid.value == "H10"

Each scenario factory returns a fresh :class:`Scenario` so callers
can mutate fields on a copy without contaminating other tests.
"""

from __future__ import annotations

import json
import tarfile
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass(slots=True)
class Scenario:
    """Compact spec for one synthetic diag tarball.

    Fields map 1:1 to the SUMMARY.json keys + per-step files the
    triage analyzer reads. The builder writes them as deterministic
    JSON (sort_keys=True, no microseconds drift) so the archive
    bytes are reproducible across runs.
    """

    # SUMMARY.json contents
    toolkit: str = "linux"  # "linux" | "macos" | "windows"
    summary_extras: dict[str, Any] = field(default_factory=dict)

    # alerts.jsonl entries (one per line)
    alerts: list[dict[str, Any]] = field(default_factory=list)

    # Capture analysis files: list of (relative_path, payload_dict)
    capture_files: list[tuple[str, dict[str, Any]]] = field(default_factory=list)

    # Optional extra per-step JSON files (relative_path, payload_dict)
    extra_files: list[tuple[str, dict[str, Any]]] = field(default_factory=list)

    # Optional plain-text files some hypothesis evaluators scan
    # (e.g. doctor_voice.txt, amixer_card0_scontents.txt). Stored as
    # (relative_path, content_str) and written verbatim by the builder.
    text_files: list[tuple[str, str]] = field(default_factory=list)

    # Identity baked into SUMMARY.json
    host: str = "test-host"
    captured_at_utc: str = "2026-05-05T18:00:00Z"
    tool_version: str = "4.3.0"

    def summary(self) -> dict[str, Any]:
        base: dict[str, Any] = {
            "schema_version": 1,
            "tool": "sovyx-voice-diag",
            "tool_version": self.tool_version,
            "host": self.host,
            "captured_at_utc": self.captured_at_utc,
            "os": self.toolkit,
            "status": "complete",
            "final_exit_code": 0,
            "calibration": {"analyzer_selftest_status": "pass"},
            "flags": {"skip_captures": False},
            "steps": {},
        }
        base.update(self.summary_extras)
        return base


def build_tarball(scenario: Scenario, target: Path) -> Path:
    """Materialize a Scenario as a .tar.gz at ``target``.

    The archive root inside the tar is ``sovyx-voice-diag-<host>-<ts>/``,
    matching the real diag's output convention. Returns the absolute
    path written.
    """
    target = target.resolve()
    target.parent.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix="sovyx-synth-") as tmp_root:
        root_dir_name = f"sovyx-voice-diag-{scenario.host}-stamp"
        root_dir = Path(tmp_root) / root_dir_name
        root_dir.mkdir()

        # SUMMARY.json
        summary_path = root_dir / "SUMMARY.json"
        summary_path.write_text(
            json.dumps(scenario.summary(), sort_keys=True, indent=2),
            encoding="utf-8",
        )

        # alerts.jsonl (under _diagnostics/ per real layout; falls back
        # to root rglob if not found)
        diag_dir = root_dir / "_diagnostics"
        diag_dir.mkdir()
        alerts_path = diag_dir / "alerts.jsonl"
        alerts_path.write_text(
            "".join(json.dumps(a, sort_keys=True) + "\n" for a in scenario.alerts),
            encoding="utf-8",
        )

        # Capture files (E_portaudio/captures/<id>/analysis.json)
        for rel_path, payload in scenario.capture_files:
            capture_path = root_dir / rel_path
            capture_path.parent.mkdir(parents=True, exist_ok=True)
            capture_path.write_text(
                json.dumps(payload, sort_keys=True, indent=2),
                encoding="utf-8",
            )

        # Extra per-step files
        for rel_path, payload in scenario.extra_files:
            extra_path = root_dir / rel_path
            extra_path.parent.mkdir(parents=True, exist_ok=True)
            extra_path.write_text(
                json.dumps(payload, sort_keys=True, indent=2),
                encoding="utf-8",
            )

        # Plain-text files (doctor_voice.txt, amixer dumps, etc.)
        for rel_path, content in scenario.text_files:
            text_path = root_dir / rel_path
            text_path.parent.mkdir(parents=True, exist_ok=True)
            text_path.write_text(content, encoding="utf-8")

        # Tar it up. Use deterministic mtime via filter for byte-identity.
        with tarfile.open(target, "w:gz") as tar:
            tar.add(root_dir, arcname=root_dir_name, filter=_deterministic_tarinfo)

    return target


def _deterministic_tarinfo(info: tarfile.TarInfo) -> tarfile.TarInfo:
    """Strip mtime + uid/gid for byte-identical archives across runs."""
    info.mtime = 0
    info.uid = 0
    info.gid = 0
    info.uname = ""
    info.gname = ""
    return info


# ────────────────────────────────────────────────────────────────────
# Scenario factories (one per documented hypothesis case)
# ────────────────────────────────────────────────────────────────────


def scenario_golden_path() -> Scenario:
    """Healthy host -- no hypothesis fires above 0.5 confidence."""
    return Scenario(
        toolkit="linux",
        summary_extras={
            "audio_endpoints": [],
            "host_capability_summary": {"kernel_line": "Linux 6.8.0 generic"},
        },
        alerts=[],
        capture_files=[
            (
                f"E_portaudio/captures/W{i}/analysis.json",
                {"rms_dbfs": -25.0, "classification": "voice"},
            )
            for i in range(3)
        ],
    )


def scenario_h10_mixer_attenuated() -> Scenario:
    """Sony VAIO canonical case: H10 fires with high confidence.

    Three voice captures all silent (rms < -85) + alerts indicating
    mixer attenuation -> the ``_evaluate_hypotheses`` H10 branch
    accumulates evidence + crowns H10 as the winner.
    """
    return Scenario(
        toolkit="linux",
        summary_extras={
            "audio_endpoints": [],
            "host_capability_summary": {"kernel_line": "Linux 6.8.0 generic"},
            "linux_mixer": {
                "controls": {
                    "Capture": {"pct": 5},
                    "Mic Boost": {"pct": 0},
                    "Internal Mic Boost": {"pct": 0},
                },
                "regime": "attenuated",
            },
        },
        alerts=[
            {
                "severity": "error",
                "message": "linux_mixer_saturated regime=attenuated capture=5% boost=0%",
            },
            {
                "severity": "warn",
                "message": "all voice captures silent across 3/3 windows",
            },
        ],
        capture_files=[
            (
                f"E_portaudio/captures/W{i}/analysis.json",
                {"rms_dbfs": -90.0, "classification": "silence"},
            )
            for i in range(3)
        ],
        text_files=[
            (
                "G_sovyx/doctor_voice.txt",
                (
                    "step_id=linux_mixer_saturated regime=attenuated "
                    "capture=5% boost=0% internal_mic_boost=0% "
                    "details: attenuation_warning=true reason=below_silero_floor\n"
                ),
            ),
            (
                "C_alsa/amixer_card0_scontents.txt",
                (
                    "Simple mixer control 'Capture',0\n"
                    "  Capabilities: cvolume cswitch cswitch-joined\n"
                    "  Front Left: Capture 4 [5%] [-30.00dB] [on]\n"
                    "  Front Right: Capture 4 [5%] [-30.00dB] [on]\n"
                    "Simple mixer control 'Mic Boost',0\n"
                    "  Capabilities: volume\n"
                    "  Front Left: 0 [0%] [0.00dB]\n"
                    "  Front Right: 0 [0%] [0.00dB]\n"
                    "Simple mixer control 'Internal Mic Boost',0\n"
                    "  Capabilities: volume\n"
                    "  Front Left: 0 [0%] [0.00dB]\n"
                    "  Front Right: 0 [0%] [0.00dB]\n"
                ),
            ),
        ],
    )


def scenario_h5_macos_tcc_denied() -> Scenario:
    """macOS host with TCC microphone permission denied.

    The triage's H5 branch reads ``F_session/tcc_mic_consents.json``;
    we synthesize one with ``fda_status=denied`` so the rule fires.
    """
    return Scenario(
        toolkit="macos",
        summary_extras={
            "audio_endpoints": [],
            "host_capability_summary": {"kernel_line": "Darwin 24.0"},
        },
        alerts=[
            {
                "severity": "error",
                "message": "tcc_mic_permission_denied for python host process",
            },
        ],
        extra_files=[
            (
                "F_session/tcc_mic_consents.json",
                {
                    "fda_status": "denied",
                    "interesting_chain_clients": [
                        {"client": "python3.12", "auth_value": 0},
                    ],
                },
            ),
        ],
    )


def scenario_h9_hardware_gap() -> Scenario:
    """No capture-capable devices on the host."""
    return Scenario(
        toolkit="linux",
        summary_extras={
            "audio_endpoints": [],
            "host_capability_summary": {"kernel_line": "Linux 6.8.0"},
            "alsa_capture_devices": {"count": 0, "devices": []},
        },
        alerts=[
            {
                "severity": "error",
                "message": "no_capture_devices arecord -l reports zero capture cards",
            },
        ],
    )


def scenario_h1_mic_destroyed_apo() -> Scenario:
    """Windows host with Voice Clarity APO destroying capture signal.

    Three silent voice captures + the silence_across alert pattern
    that the H1 evaluator scans for. H1 fires regardless of toolkit
    (cross-OS catch-all for "mic destroyed upstream of user-space").
    On Windows the H2 evaluator can also fire if the apo data is
    populated; this scenario keeps H2-specific data minimal so H1
    is the dominant verdict.
    """
    return Scenario(
        toolkit="windows",
        summary_extras={
            "audio_endpoints": [],
            "host_capability_summary": {"kernel_line": "Windows 11 26200"},
        },
        alerts=[
            {
                "severity": "error",
                "message": "voice_clarity_destroying capture across all 3 windows",
            },
            {
                "severity": "warn",
                "message": "silence_across all 3/3 voice captures",
            },
        ],
        capture_files=[
            (
                f"E_portaudio/captures/W{i}/analysis.json",
                {"rms_dbfs": -90.0, "classification": "silence"},
            )
            for i in range(3)
        ],
    )


def scenario_h4_pulse_destructive_filter() -> Scenario:
    """Linux PulseAudio destructive filter as the standalone winner.

    Distinct from the multi-hypothesis scenario in that the mixer
    state is healthy, so H10 does NOT also fire -- H4 is the
    unambiguous root cause.
    """
    return Scenario(
        toolkit="linux",
        summary_extras={
            "audio_endpoints": [],
            "host_capability_summary": {"kernel_line": "Linux 6.5.0 generic"},
        },
        alerts=[
            {
                "severity": "error",
                "message": "destructive pactl modules loaded: module-echo-cancel",
            },
            {
                "severity": "warn",
                "message": "destructive filter on capture chain",
            },
        ],
        capture_files=[
            (
                f"E_portaudio/captures/W{i}/analysis.json",
                {"rms_dbfs": -90.0, "classification": "silence"},
            )
            for i in range(3)
        ],
    )


def scenario_h6_selftest_failed() -> Scenario:
    """Analyzer selftest failed -- downstream metrics are suspect.

    Sets ``analyzer_selftest_status=fail`` at BOTH the top level
    (where the H6 evaluator reads it for evidence weight) AND under
    the nested ``calibration`` dict (where the TriageResult reader
    looks first when populating ``selftest_status``). The H6 evaluator
    weights this signal at 1.0 so H6 wins regardless of other
    evidence.
    """
    return Scenario(
        toolkit="linux",
        summary_extras={
            "audio_endpoints": [],
            "host_capability_summary": {"kernel_line": "Linux 6.8.0 generic"},
            "analyzer_selftest_status": "fail",
            "calibration": {"analyzer_selftest_status": "fail"},
        },
        alerts=[
            {
                "severity": "error",
                "message": "analyzer_selftest_failed: amixer probe returned unexpected layout",
            },
        ],
    )


def scenario_multi_hypothesis() -> Scenario:
    """H4 + H10 cross-correlation: PulseAudio destructive filter loaded
    AND mixer is attenuated. Both fire; the ranker picks the highest
    confidence (H10 in our weighting)."""
    return Scenario(
        toolkit="linux",
        summary_extras={
            "audio_endpoints": [],
            "host_capability_summary": {"kernel_line": "Linux 6.8.0"},
            "linux_mixer": {
                "controls": {
                    "Capture": {"pct": 5},
                    "Mic Boost": {"pct": 0},
                },
                "regime": "attenuated",
            },
        },
        alerts=[
            {
                "severity": "error",
                "message": "destructive pactl modules loaded: module-echo-cancel",
            },
            {
                "severity": "error",
                "message": "linux_mixer_saturated regime=attenuated capture=5%",
            },
            {
                "severity": "warn",
                "message": "all voice captures silent across 3/3 windows",
            },
        ],
        capture_files=[
            (
                f"E_portaudio/captures/W{i}/analysis.json",
                {"rms_dbfs": -90.0, "classification": "silence"},
            )
            for i in range(3)
        ],
        text_files=[
            (
                "G_sovyx/doctor_voice.txt",
                (
                    "step_id=linux_mixer_saturated regime=attenuated "
                    "capture=5% boost=0% details: attenuation_warning=true\n"
                ),
            ),
        ],
    )
