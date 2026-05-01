"""Tests for ``voice/_call_quality.py`` — Phase 7 / T7.42 CQD-equivalent.

Pin the snapshot dataclass + emission contract + SNR→MOS curve so
downstream consumers (websocket push, audit trail, dashboard widget)
can rely on stable behaviour.
"""

from __future__ import annotations

import logging

import pytest

from sovyx.voice._call_quality import (
    VoiceCallQualitySnapshot,
    _snr_to_mos,
    snapshot_from_pipeline,
)


class TestVoiceCallQualitySnapshot:
    """Pin the dataclass + emit contract."""

    def test_minimal_construction(self) -> None:
        """Only utterance_id + mind_id + outcome are required."""
        snap = VoiceCallQualitySnapshot(
            utterance_id="u-1",
            mind_id="m-default",
            outcome="completed",
        )
        assert snap.utterance_id == "u-1"
        assert snap.mind_id == "m-default"
        assert snap.outcome == "completed"
        # All optional fields default to None / 0 / empty.
        assert snap.time_to_first_utterance_ms is None
        assert snap.snr_db is None
        assert snap.barge_in_count == 0
        assert snap.extra == {}

    def test_full_construction(self) -> None:
        snap = VoiceCallQualitySnapshot(
            utterance_id="u-full",
            mind_id="m-default",
            outcome="completed",
            time_to_first_utterance_ms=180.0,
            wake_word_detection_ms=420.0,
            stt_latency_ms=350.0,
            tts_synthesis_ms=270.0,
            voice_to_voice_ms=820.0,
            snr_db=22.5,
            aec_erle_db=33.0,
            mos_proxy=3.6,
            wake_word_path="two_stage",
            wake_word_score=0.82,
            stt_confidence=0.91,
            barge_in_count=0,
        )
        assert snap.voice_to_voice_ms == pytest.approx(820.0)
        assert snap.aec_erle_db == pytest.approx(33.0)
        assert snap.wake_word_path == "two_stage"

    def test_emit_writes_structured_event(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        snap = VoiceCallQualitySnapshot(
            utterance_id="u-emit",
            mind_id="m-default",
            outcome="completed",
            voice_to_voice_ms=1234.0,
            snr_db=18.0,
        )
        with caplog.at_level(logging.INFO, logger="sovyx.voice._call_quality"):
            snap.emit()

        records = [r for r in caplog.records if "voice.call_quality.snapshot" in r.getMessage()]
        assert len(records) == 1
        message = records[0].getMessage()
        # Flat-namespace fields land at top level.
        assert "voice.utterance_id" in message
        assert "u-emit" in message
        assert "voice.voice_to_voice_ms" in message
        assert "1234" in message

    def test_emit_extra_lands_under_voice_extra(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """``extra`` is preserved nested, not flattened to root keys.

        Defensive — a buggy caller passing PII into extra shouldn't
        get raw keys flattened into the log namespace.
        """
        snap = VoiceCallQualitySnapshot(
            utterance_id="u-extra",
            mind_id="m-default",
            outcome="completed",
            extra={"future_field": 42, "another": "data"},
        )
        with caplog.at_level(logging.INFO, logger="sovyx.voice._call_quality"):
            snap.emit()

        records = [r for r in caplog.records if "voice.call_quality.snapshot" in r.getMessage()]
        assert len(records) == 1
        message = records[0].getMessage()
        # Extra is nested.
        assert "voice.extra" in message
        assert "future_field" in message
        # ``future_field`` is NOT directly at root.
        assert "voice.future_field" not in message

    def test_emit_skips_extra_when_empty(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        snap = VoiceCallQualitySnapshot(
            utterance_id="u-noextra",
            mind_id="m-default",
            outcome="completed",
        )
        with caplog.at_level(logging.INFO, logger="sovyx.voice._call_quality"):
            snap.emit()

        records = [r for r in caplog.records if "voice.call_quality.snapshot" in r.getMessage()]
        assert len(records) == 1
        message = records[0].getMessage()
        # No ``voice.extra`` key when extra is empty.
        assert "voice.extra" not in message


class TestSnrToMosCurve:
    """Pin the SNR → MOS proxy curve."""

    def test_zero_or_negative_snr_clamps_to_one(self) -> None:
        assert _snr_to_mos(0.0) == 1.0
        assert _snr_to_mos(-10.0) == 1.0

    def test_60_db_clamps_to_five(self) -> None:
        assert _snr_to_mos(60.0) == 5.0
        assert _snr_to_mos(80.0) == 5.0

    def test_20_db_is_acceptable_band(self) -> None:
        # Skype/Zoom acceptable threshold ≈ MOS 3.5 at SNR ~20 dB.
        mos = _snr_to_mos(20.0)
        assert 3.0 < mos < 4.0  # noqa: PLR2004

    def test_30_db_is_good_band(self) -> None:
        # Good band ≈ MOS 4.5 at SNR ~30 dB per the logistic curve.
        mos = _snr_to_mos(30.0)
        assert 4.0 <= mos <= 4.6  # noqa: PLR2004

    def test_monotonic_in_useful_range(self) -> None:
        """MOS is strictly monotonic in [0, 60] dB."""
        prev = _snr_to_mos(0.0)
        for snr in range(1, 61, 5):
            current = _snr_to_mos(float(snr))
            assert current >= prev, f"non-monotonic at SNR={snr}"
            prev = current


class TestSnapshotFromPipeline:
    """Pin the convenience constructor + MOS auto-compute."""

    def test_snapshot_from_pipeline_minimal(self) -> None:
        snap = snapshot_from_pipeline(
            utterance_id="u-from-pipeline",
            mind_id="m-default",
            outcome="completed",
        )
        assert snap.utterance_id == "u-from-pipeline"
        # No SNR provided → no MOS computed.
        assert snap.mos_proxy is None

    def test_snapshot_from_pipeline_computes_mos_from_snr(self) -> None:
        snap = snapshot_from_pipeline(
            utterance_id="u-snr",
            mind_id="m-default",
            outcome="completed",
            snr_db=25.0,
        )
        # MOS_proxy is auto-computed from the curve.
        assert snap.mos_proxy is not None
        assert 3.0 < snap.mos_proxy < 5.0  # noqa: PLR2004
        # Matches the curve.
        assert snap.mos_proxy == pytest.approx(_snr_to_mos(25.0), abs=1e-6)

    def test_snapshot_from_pipeline_passes_through_all_fields(self) -> None:
        snap = snapshot_from_pipeline(
            utterance_id="u-full",
            mind_id="m-default",
            outcome="completed",
            voice_to_voice_ms=900.0,
            time_to_first_utterance_ms=180.0,
            wake_word_detection_ms=420.0,
            wake_word_path="fast_path",
            wake_word_score=0.92,
            stt_latency_ms=400.0,
            stt_confidence=0.88,
            tts_synthesis_ms=300.0,
            snr_db=24.0,
            aec_erle_db=32.0,
            barge_in_count=1,
        )
        assert snap.voice_to_voice_ms == pytest.approx(900.0)
        assert snap.wake_word_path == "fast_path"
        assert snap.barge_in_count == 1
        # MOS computed from snr_db.
        assert snap.mos_proxy is not None
