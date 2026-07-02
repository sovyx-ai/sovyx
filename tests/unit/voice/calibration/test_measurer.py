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


class TestActiveMicCardIndexPlumbing:
    """v0.31.4 GAP 5 closure: ``capture_measurements`` propagates the
    ``active_mic_card_index`` arg into ``_read_mixer_state(card_index=...)``
    so R10 sees data from the operator's actual mic, not hardcoded
    card 0."""

    def test_active_mic_card_index_passed_to_read_mixer_state(self) -> None:
        with patch.object(
            m,
            "_read_mixer_state",
            return_value={
                "card_index": 2,
                "capture_pct": 5,
                "boost_pct": 0,
                "internal_mic_boost_pct": None,
            },
        ) as mock_read:
            capture_measurements(
                captured_at_utc="2026-05-05T18:00:00Z",
                active_mic_card_index=2,
            )
        # The mocked _read_mixer_state must have been called with card_index=2.
        mock_read.assert_called_once_with(card_index=2)

    def test_active_mic_card_index_default_none_preserves_legacy(self) -> None:
        """Pre-v0.31.4 callers (no kwarg) get the historical card-0
        probe — back-compat preserved."""
        with patch.object(m, "_read_mixer_state") as mock_read:
            mock_read.return_value = {
                "card_index": 0,
                "capture_pct": None,
                "boost_pct": None,
                "internal_mic_boost_pct": None,
            }
            capture_measurements(captured_at_utc="2026-05-05T18:00:00Z")
        mock_read.assert_called_once_with(card_index=None)

    def test_read_mixer_state_card_index_arg_uses_amixer_c_n(self) -> None:
        """``_read_mixer_state(card_index=2)`` invokes
        ``amixer -c 2 scontents``, not ``amixer -c 0``."""
        with (
            patch.object(m.shutil, "which", return_value="/usr/bin/amixer"),
            patch.object(m, "_safe_amixer", return_value="") as mock_amixer,
        ):
            m._read_mixer_state(card_index=2)
        # Even if amixer returns empty (no controls), the cmdline args
        # should reflect card 2.
        mock_amixer.assert_called_once_with(["amixer", "-c", "2", "scontents"])

    def test_read_mixer_state_card_index_none_falls_back_to_zero(self) -> None:
        """Default (no card_index arg) preserves card-0 behaviour."""
        with (
            patch.object(m.shutil, "which", return_value="/usr/bin/amixer"),
            patch.object(m, "_safe_amixer", return_value="") as mock_amixer,
        ):
            m._read_mixer_state()
        mock_amixer.assert_called_once_with(["amixer", "-c", "0", "scontents"])


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


class TestLocalePinning:
    """LINUX-6 residual: the amixer scontents parser string-matches
    English control labels — the subprocess must pin LC_ALL=C via
    linux_tool_env().
    """

    def test_safe_amixer_pins_locale(self) -> None:
        from unittest.mock import MagicMock

        completed = MagicMock(returncode=0, stdout="ok")
        with patch.object(m.subprocess, "run", return_value=completed) as mock_run:
            assert m._safe_amixer(["amixer", "-c", "0", "scontents"]) == "ok"
        env = mock_run.call_args.kwargs.get("env")
        assert env is not None, "amixer ran without a pinned environment"
        assert env["LC_ALL"] == "C"
        assert env["LANG"] == "C"
