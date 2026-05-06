"""Unit tests for sovyx.voice.calibration._measurer.capture_measurements.

Coverage:
* capture_measurements happy path: mocked mixer + tarball -> populated snapshot
* Mixer regime classification: attenuated / saturated / healthy / None
* _extract_first_pct parsing of amixer scontents output
* Triage result extraction: winner_hid + winner_confidence flow through
* No-input path: returns sentinel snapshot without raising
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

from sovyx.voice.calibration import (
    MeasurementSnapshot,
    capture_measurements,
)
from sovyx.voice.calibration import _measurer as m

# ====================================================================
# capture_measurements top-level
# ====================================================================


class TestCaptureMeasurements:
    """capture_measurements composes mixer + tarball + triage data."""

    def test_no_input_returns_sentinel_snapshot(self) -> None:
        with patch.object(
            m,
            "_read_mixer_state",
            return_value={
                "card_index": None,
                "capture_pct": None,
                "boost_pct": None,
                "internal_mic_boost_pct": None,
            },
        ):
            result = capture_measurements(captured_at_utc="2026-05-05T18:00:00Z")
        assert isinstance(result, MeasurementSnapshot)
        assert result.rms_dbfs_per_capture == ()
        assert result.vad_speech_probability_max == 0.0
        assert result.mixer_attenuation_regime is None
        assert result.triage_winner_hid is None

    def test_attenuated_mixer_classified(self) -> None:
        # Sony VAIO canonical case: capture=40%, boost=0%, internal=0%.
        with patch.object(
            m,
            "_read_mixer_state",
            return_value={
                "card_index": 0,
                "capture_pct": 5,
                "boost_pct": 0,
                "internal_mic_boost_pct": 0,
            },
        ):
            result = capture_measurements(captured_at_utc="2026-05-05T18:00:00Z")
        assert result.mixer_attenuation_regime == "attenuated"
        assert result.mixer_capture_pct == 5
        assert result.mixer_boost_pct == 0


# ====================================================================
# _classify_mixer_regime
# ====================================================================


class TestClassifyMixerRegime:
    """Threshold-based: attenuated / saturated / healthy / None."""

    def test_no_signal_returns_none(self) -> None:
        result = m._classify_mixer_regime(
            {
                "capture_pct": None,
                "boost_pct": None,
                "internal_mic_boost_pct": None,
            }
        )
        assert result is None

    def test_attenuated_capture_low_boost_low(self) -> None:
        result = m._classify_mixer_regime(
            {
                "capture_pct": 5,
                "boost_pct": 0,
                "internal_mic_boost_pct": 0,
            }
        )
        assert result == "attenuated"

    def test_attenuated_capture_low_internal_boost_low(self) -> None:
        # Either boost or internal_mic_boost being low qualifies.
        result = m._classify_mixer_regime(
            {
                "capture_pct": 5,
                "boost_pct": None,
                "internal_mic_boost_pct": 0,
            }
        )
        assert result == "attenuated"

    def test_saturated_capture_high_boost_high(self) -> None:
        result = m._classify_mixer_regime(
            {
                "capture_pct": 100,
                "boost_pct": 100,
                "internal_mic_boost_pct": 0,
            }
        )
        assert result == "saturated"

    def test_healthy_mid_range(self) -> None:
        result = m._classify_mixer_regime(
            {
                "capture_pct": 50,
                "boost_pct": 50,
                "internal_mic_boost_pct": 25,
            }
        )
        assert result == "healthy"

    def test_capture_low_but_boost_normal_is_healthy(self) -> None:
        # Edge case: capture is low but boost is normal -> healthy.
        # The rule fires only when BOTH capture is low AND a boost is low.
        result = m._classify_mixer_regime(
            {
                "capture_pct": 5,
                "boost_pct": 50,
                "internal_mic_boost_pct": 50,
            }
        )
        assert result == "healthy"


# ====================================================================
# _extract_first_pct
# ====================================================================


class TestExtractFirstPct:
    """Parse percentage from amixer scontents text."""

    def test_simple_capture_control(self) -> None:
        sample = """Simple mixer control 'Capture',0
  Capabilities: cvolume cvolume-joined cswitch
  Capture channels: Mono
  Limits: Capture 0 - 80
  Mono: Capture 53 [66%] [-13.50dB] [on]
"""
        assert m._extract_first_pct(sample, ("Capture",)) == 66

    def test_mic_boost_control(self) -> None:
        sample = """Simple mixer control 'Mic Boost',0
  Capabilities: volume volume-joined penum
  Playback channels: Mono
  Limits: 0 - 3
  Mono: 0 [0%] [0.00dB]
"""
        assert m._extract_first_pct(sample, ("Mic Boost",)) == 0

    def test_pattern_not_found_returns_none(self) -> None:
        sample = "Simple mixer control 'Master',0\n  Mono: 100 [100%]\n"
        assert m._extract_first_pct(sample, ("Capture",)) is None

    def test_empty_input_returns_none(self) -> None:
        assert m._extract_first_pct("", ("Capture",)) is None


# ====================================================================
# Diag tarball extraction
# ====================================================================


class TestExtractCaptureMetrics:
    """Read RMS/VAD from analysis.json + silero.json under E_portaudio/captures."""

    def test_no_tarball_root_returns_empty(self) -> None:
        result = m._extract_capture_metrics(None)
        assert result == ((), 0.0, 0.0)

    def test_missing_captures_dir_returns_empty(self, tmp_path: Path) -> None:
        # Root exists but no E_portaudio/captures subdir.
        result = m._extract_capture_metrics(tmp_path)
        assert result == ((), 0.0, 0.0)

    def test_reads_rms_and_vad(self, tmp_path: Path) -> None:
        captures = tmp_path / "E_portaudio" / "captures"
        captures.mkdir(parents=True)
        cap_dir = captures / "W11_pa_test"
        cap_dir.mkdir()
        (cap_dir / "analysis.json").write_text(
            json.dumps({"rms_dbfs": -25.5, "classification": "voice"})
        )
        (cap_dir / "silero.json").write_text(json.dumps({"available": True, "max_prob": 0.92}))

        rms, vad_max, vad_p99 = m._extract_capture_metrics(tmp_path)
        assert rms == (-25.5,)
        assert vad_max == 0.92
        assert vad_p99 == 0.92  # n=1 -> max == p99


# ====================================================================
# Triage result flow-through
# ====================================================================


class TestTriageFlowThrough:
    """triage_result populates triage_winner_hid + triage_winner_confidence."""

    def test_with_triage_result_winner(self) -> None:
        from sovyx.voice.diagnostics import (
            AlertsSummary,
            HypothesisId,
            HypothesisVerdict,
            SchemaValidation,
            TriageResult,
        )

        triage = TriageResult(
            schema_version=1,
            toolkit="linux",
            tarball_root=Path("/tmp/x"),
            tool_name="sovyx-voice-diag",
            tool_version="4.3",
            host="t",
            captured_at_utc="2026-05-05T18:00:00Z",
            os_descriptor="linux",
            status="complete",
            exit_code="0",
            selftest_status="pass",
            steps={},
            skip_captures=False,
            schema_validation=SchemaValidation(
                ok=True, missing_required=(), missing_recommended=(), warnings=()
            ),
            alerts=AlertsSummary(error_count=0, warn_count=0, info_count=0, error_messages=()),
            hypotheses=(
                HypothesisVerdict(
                    hid=HypothesisId.H10_LINUX_MIXER_ATTENUATED,
                    title="x",
                    confidence=0.95,
                    evidence_for=(),
                    evidence_against=(),
                    recommended_action=None,
                ),
            ),
        )
        with patch.object(
            m,
            "_read_mixer_state",
            return_value={
                "card_index": None,
                "capture_pct": None,
                "boost_pct": None,
                "internal_mic_boost_pct": None,
            },
        ):
            result = capture_measurements(
                triage_result=triage,
                captured_at_utc="2026-05-05T18:00:00Z",
            )
        assert result.triage_winner_hid == "H10"
        assert result.triage_winner_confidence == 0.95

    def test_without_triage_returns_none(self) -> None:
        with patch.object(
            m,
            "_read_mixer_state",
            return_value={
                "card_index": None,
                "capture_pct": None,
                "boost_pct": None,
                "internal_mic_boost_pct": None,
            },
        ):
            result = capture_measurements(captured_at_utc="2026-05-05T18:00:00Z")
        assert result.triage_winner_hid is None
        assert result.triage_winner_confidence is None
