"""End-to-end integration tests for the voice telemetry surface.

Phase 3 of IMPL-OBSERVABILITY-001 wired voice subsystems to emit a
canonical set of OTel-style dotted events through the structlog
pipeline:

* hot-path frame events (``audio.frame``, ``voice.vad.frame``,
  ``voice.wake_word.score``) are rate-sampled by
  :class:`sovyx.observability.sampling.SamplingProcessor` so the file
  handler is not saturated;
* discrete transitions (``voice.vad.state_changed``,
  ``voice.wake_word.detected``) and incident events
  (``voice.deaf.detected``, ``voice.frame_drop.detected``,
  ``voice.barge_in.detected``) are *never* sampled — losing one would
  mask a real failure;
* every voice event carries the active ``saga_id`` when emitted inside
  a saga scope so the dashboard can stitch a single utterance into one
  causal trace.

These tests exercise the *plumbing* (renderer + sampler + envelope +
saga propagation) by emitting the canonical events directly via
``get_logger`` rather than constructing live VAD / wake-word instances
— those would require ONNX runtime models that are unavailable in CI.
The contract being verified here is "an event with this name and
these fields, emitted from this context, lands on disk with these
augmentations" — exactly what the dashboard, KNOWN_EVENTS catalogue,
and JSON-schema CI gate depend on.
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Generator
from pathlib import Path
from typing import Any

import pytest
import structlog
from structlog.contextvars import clear_contextvars

from sovyx.engine.config import LoggingConfig, ObservabilityConfig
from sovyx.observability.logging import (
    get_logger,
    setup_logging,
    shutdown_logging,
)
from sovyx.observability.saga import async_saga_scope, saga_scope


@pytest.fixture()
def _clean_state() -> Generator[None, None, None]:
    """Tear down logging + structlog contextvars between tests."""
    clear_contextvars()
    yield
    shutdown_logging(timeout=2.0)
    structlog.reset_defaults()
    clear_contextvars()
    root = logging.getLogger()
    for handler in list(root.handlers):
        root.removeHandler(handler)


def _wait_for_file(path: Path, *, timeout: float = 3.0) -> None:
    """Block until *path* has at least one byte or *timeout* elapses."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists() and path.stat().st_size > 0:
            return
        time.sleep(0.02)


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    """Return every JSON object from *path* (one per line)."""
    return [
        json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()
    ]


def _make_obs_config(**overrides: Any) -> ObservabilityConfig:
    """Build an ObservabilityConfig with voice_telemetry enabled and tweakable sampling."""
    base: dict[str, Any] = {
        "features": {
            "async_queue": True,
            "pii_redaction": True,
            "saga_propagation": True,
            "voice_telemetry": True,
            "startup_cascade": False,
            "plugin_introspection": False,
            "anomaly_detection": False,
            "tamper_chain": False,
            "schema_validation": False,
            "metrics_exporter": False,
        },
    }
    base.update(overrides)
    return ObservabilityConfig.model_validate(base)


def _setup(
    tmp_path: Path,
    *,
    audio_frame_rate: int = 1,
    vad_frame_rate: int = 1,
    wake_word_score_rate: int = 1,
) -> Path:
    """Configure logging for one test and return the JSON log file path."""
    log_file = tmp_path / "logs" / "voice.log"
    obs_cfg = _make_obs_config(
        sampling={
            "audio_frame_rate": audio_frame_rate,
            "vad_frame_rate": vad_frame_rate,
            "wake_word_score_rate": wake_word_score_rate,
        },
    )
    setup_logging(
        LoggingConfig(level="DEBUG", console_format="json", log_file=log_file),
        obs_cfg,
        data_dir=tmp_path,
    )
    return log_file


class TestAudioFrameEvent:
    """``audio.frame`` lands with all five canonical acoustic fields."""

    def test_audio_frame_carries_canonical_fields(
        self, tmp_path: Path, _clean_state: None
    ) -> None:
        log_file = _setup(tmp_path)
        log = get_logger("sovyx.voice.audio")
        log.info(
            "audio.frame",
            **{
                "audio.rms_db": -27.5,
                "audio.peak_db": -12.0,
                "audio.clipping": 0,
                "voice.stream_id": "stream-abc",
                "voice.device_id": "USB-Microphone",
            },
        )
        shutdown_logging(timeout=3.0)
        _wait_for_file(log_file)

        rec = next(r for r in _read_jsonl(log_file) if r.get("event") == "audio.frame")
        assert rec["audio.rms_db"] == -27.5
        assert rec["audio.peak_db"] == -12.0
        assert rec["audio.clipping"] == 0
        assert rec["voice.stream_id"] == "stream-abc"
        assert rec["voice.device_id"] == "USB-Microphone"


class TestSampledHotPathEvents:
    """SamplingProcessor honours per-event keep-every-N for the three hot paths."""

    def test_audio_frame_is_sampled_at_configured_rate(
        self, tmp_path: Path, _clean_state: None
    ) -> None:
        log_file = _setup(tmp_path, audio_frame_rate=5)
        log = get_logger("sovyx.voice.audio")
        for i in range(10):
            log.info(
                "audio.frame",
                **{
                    "audio.rms_db": -30.0,
                    "audio.peak_db": -15.0,
                    "audio.clipping": 0,
                    "voice.stream_id": "s",
                    "voice.device_id": "dev",
                    "frame.index": i,
                },
            )
        shutdown_logging(timeout=3.0)
        _wait_for_file(log_file)

        kept = [r for r in _read_jsonl(log_file) if r.get("event") == "audio.frame"]
        # rate=5 → keep frames at counter 0 and 5 → 2 records out of 10.
        assert len(kept) == 2
        # First kept must always be the very first emit (counter starts at 0).
        assert kept[0]["frame.index"] == 0
        assert kept[1]["frame.index"] == 5

    def test_vad_frame_is_sampled(self, tmp_path: Path, _clean_state: None) -> None:
        log_file = _setup(tmp_path, vad_frame_rate=4)
        log = get_logger("sovyx.voice.vad")
        for i in range(8):
            log.info(
                "voice.vad.frame",
                **{
                    "voice.probability": 0.5,
                    "voice.rms": 0.01,
                    "voice.state": "SILENCE",
                    "voice.onset_threshold": 0.6,
                    "voice.offset_threshold": 0.4,
                    "frame.index": i,
                },
            )
        shutdown_logging(timeout=3.0)
        _wait_for_file(log_file)

        kept = [r for r in _read_jsonl(log_file) if r.get("event") == "voice.vad.frame"]
        # rate=4 → counter 0 and 4 → 2 records out of 8.
        assert len(kept) == 2
        assert [r["frame.index"] for r in kept] == [0, 4]

    def test_wake_word_score_is_sampled(self, tmp_path: Path, _clean_state: None) -> None:
        log_file = _setup(tmp_path, wake_word_score_rate=3)
        log = get_logger("sovyx.voice.wake_word")
        for i in range(9):
            log.info(
                "voice.wake_word.score",
                **{
                    "voice.score": 0.1,
                    "voice.threshold": 0.5,
                    "voice.stage2_threshold": 0.7,
                    "voice.cooldown_ms_remaining": 0,
                    "voice.state": "LISTENING",
                    "voice.model_name": "alexa",
                    "frame.index": i,
                },
            )
        shutdown_logging(timeout=3.0)
        _wait_for_file(log_file)

        kept = [r for r in _read_jsonl(log_file) if r.get("event") == "voice.wake_word.score"]
        # rate=3 → counter 0, 3, 6 → 3 records out of 9.
        assert len(kept) == 3
        assert [r["frame.index"] for r in kept] == [0, 3, 6]

    def test_sampling_rate_zero_keeps_every_record(
        self, tmp_path: Path, _clean_state: None
    ) -> None:
        # Documented escape hatch: rate ≤ 1 disables sampling for that event,
        # so an operator can live-debug VAD without losing frames.
        log_file = _setup(tmp_path, vad_frame_rate=0)
        log = get_logger("sovyx.voice.vad")
        for i in range(5):
            log.info(
                "voice.vad.frame",
                **{
                    "voice.probability": 0.1,
                    "voice.rms": 0.01,
                    "voice.state": "SILENCE",
                    "voice.onset_threshold": 0.6,
                    "voice.offset_threshold": 0.4,
                    "frame.index": i,
                },
            )
        shutdown_logging(timeout=3.0)
        _wait_for_file(log_file)

        kept = [r for r in _read_jsonl(log_file) if r.get("event") == "voice.vad.frame"]
        assert len(kept) == 5


class TestNeverSampledEvents:
    """Discrete transitions and incidents are too important to drop."""

    def test_vad_state_changed_is_never_sampled(self, tmp_path: Path, _clean_state: None) -> None:
        # Even with the most aggressive sampling, *transitions* must always
        # appear — a missed onset would invalidate downstream reasoning
        # about "did the user actually speak".
        log_file = _setup(tmp_path, vad_frame_rate=10000)
        log = get_logger("sovyx.voice.vad")
        for i in range(6):
            log.info(
                "voice.vad.state_changed",
                **{
                    "voice.from_state": "SILENCE",
                    "voice.to_state": "SPEECH",
                    "voice.probability": 0.85,
                    "voice.rms": 0.03,
                    "voice.onset_threshold": 0.6,
                    "voice.offset_threshold": 0.4,
                    "voice.prob_window": [0.7, 0.8, 0.85],
                    "voice.rms_window": [0.02, 0.03, 0.03],
                    "transition.index": i,
                },
            )
        shutdown_logging(timeout=3.0)
        _wait_for_file(log_file)

        kept = [r for r in _read_jsonl(log_file) if r.get("event") == "voice.vad.state_changed"]
        assert len(kept) == 6

    def test_wake_word_detected_is_never_sampled(self, tmp_path: Path, _clean_state: None) -> None:
        log_file = _setup(tmp_path, wake_word_score_rate=10000)
        log = get_logger("sovyx.voice.wake_word")
        for i in range(4):
            log.info(
                "voice.wake_word.detected",
                **{
                    "voice.score": 0.92,
                    "voice.model_name": "alexa",
                    "voice.stage1_threshold": 0.5,
                    "voice.stage2_threshold": 0.7,
                    "voice.transcription": "alexa",
                    "voice.window_frames": 30,
                    "detection.index": i,
                },
            )
        shutdown_logging(timeout=3.0)
        _wait_for_file(log_file)

        kept = [r for r in _read_jsonl(log_file) if r.get("event") == "voice.wake_word.detected"]
        assert len(kept) == 4

    def test_deaf_detected_is_never_sampled(self, tmp_path: Path, _clean_state: None) -> None:
        log_file = _setup(tmp_path)
        log = get_logger("sovyx.voice.pipeline")
        for i in range(3):
            log.warning(
                "voice.deaf.detected",
                **{
                    "voice.mind_id": "mind-1",
                    "voice.state": "LISTENING",
                    "voice.consecutive_deaf_warnings": 5,
                    "voice.threshold": 5,
                    "voice.max_vad_probability": 0.005,
                    "voice.frames_processed": 250,
                    "voice.voice_clarity_active": True,
                    "incident.index": i,
                },
            )
        shutdown_logging(timeout=3.0)
        _wait_for_file(log_file)

        kept = [r for r in _read_jsonl(log_file) if r.get("event") == "voice.deaf.detected"]
        assert len(kept) == 3
        assert all(r["level"] == "warning" for r in kept)


class TestVoiceSagaPropagation:
    """``saga_id`` rides on every voice event emitted inside a saga scope."""

    def test_audio_frame_inside_saga_carries_saga_id(
        self, tmp_path: Path, _clean_state: None
    ) -> None:
        log_file = _setup(tmp_path)
        log = get_logger("sovyx.voice.audio")
        with saga_scope("voice.utterance") as saga_id:
            log.info(
                "audio.frame",
                **{
                    "audio.rms_db": -25.0,
                    "audio.peak_db": -10.0,
                    "audio.clipping": 0,
                    "voice.stream_id": "s1",
                    "voice.device_id": "dev",
                },
            )
        shutdown_logging(timeout=3.0)
        _wait_for_file(log_file)

        rec = next(r for r in _read_jsonl(log_file) if r.get("event") == "audio.frame")
        assert rec["saga_id"] == saga_id

    def test_vad_state_changed_inside_saga_carries_saga_id(
        self, tmp_path: Path, _clean_state: None
    ) -> None:
        log_file = _setup(tmp_path)
        log = get_logger("sovyx.voice.vad")
        with saga_scope("voice.utterance") as saga_id:
            log.info(
                "voice.vad.state_changed",
                **{
                    "voice.from_state": "SILENCE",
                    "voice.to_state": "SPEECH",
                    "voice.probability": 0.9,
                    "voice.rms": 0.04,
                    "voice.onset_threshold": 0.6,
                    "voice.offset_threshold": 0.4,
                    "voice.prob_window": [0.8, 0.9],
                    "voice.rms_window": [0.03, 0.04],
                },
            )
        shutdown_logging(timeout=3.0)
        _wait_for_file(log_file)

        rec = next(r for r in _read_jsonl(log_file) if r.get("event") == "voice.vad.state_changed")
        assert rec["saga_id"] == saga_id

    @pytest.mark.asyncio
    async def test_frame_drop_inside_async_saga_carries_saga_id(
        self, tmp_path: Path, _clean_state: None
    ) -> None:
        log_file = _setup(tmp_path)
        log = get_logger("sovyx.voice.pipeline")
        async with async_saga_scope("voice.utterance") as saga_id:
            log.warning(
                "voice.frame_drop.detected",
                **{
                    "voice.gap_ms": 240.0,
                    "voice.expected_interval_ms": 20.0,
                    "voice.state": "LISTENING",
                    "voice.mind_id": "mind-1",
                },
            )
        shutdown_logging(timeout=3.0)
        _wait_for_file(log_file)

        rec = next(
            r for r in _read_jsonl(log_file) if r.get("event") == "voice.frame_drop.detected"
        )
        assert rec["saga_id"] == saga_id

    def test_voice_events_outside_saga_have_no_saga_id(
        self, tmp_path: Path, _clean_state: None
    ) -> None:
        log_file = _setup(tmp_path)
        log = get_logger("sovyx.voice.pipeline")
        log.warning(
            "voice.barge_in.detected",
            **{
                "voice.mind_id": "mind-1",
                "voice.frames_sustained": 4,
                "voice.prob": 0.95,
                "voice.threshold_frames": 4,
                "voice.output_was_playing": True,
            },
        )
        shutdown_logging(timeout=3.0)
        _wait_for_file(log_file)

        rec = next(r for r in _read_jsonl(log_file) if r.get("event") == "voice.barge_in.detected")
        assert "saga_id" not in rec


class TestMixedEventStream:
    """A realistic mix of sampled hot-path + unsampled transitions in one saga."""

    def test_utterance_lifecycle_records_appear_with_shared_saga(
        self, tmp_path: Path, _clean_state: None
    ) -> None:
        # Simulate a tiny utterance: 6 audio frames (rate=3 → 2 records),
        # 1 VAD onset transition, 1 wake-word detection — all under one saga.
        log_file = _setup(tmp_path, audio_frame_rate=3, vad_frame_rate=3)
        audio = get_logger("sovyx.voice.audio")
        vad = get_logger("sovyx.voice.vad")
        ww = get_logger("sovyx.voice.wake_word")

        with saga_scope("voice.utterance") as saga_id:
            for i in range(6):
                audio.info(
                    "audio.frame",
                    **{
                        "audio.rms_db": -28.0,
                        "audio.peak_db": -14.0,
                        "audio.clipping": 0,
                        "voice.stream_id": "s",
                        "voice.device_id": "dev",
                        "frame.index": i,
                    },
                )
            vad.info(
                "voice.vad.state_changed",
                **{
                    "voice.from_state": "SILENCE",
                    "voice.to_state": "SPEECH",
                    "voice.probability": 0.88,
                    "voice.rms": 0.04,
                    "voice.onset_threshold": 0.6,
                    "voice.offset_threshold": 0.4,
                    "voice.prob_window": [0.8, 0.85, 0.88],
                    "voice.rms_window": [0.03, 0.04, 0.04],
                },
            )
            ww.info(
                "voice.wake_word.detected",
                **{
                    "voice.score": 0.95,
                    "voice.model_name": "alexa",
                    "voice.stage1_threshold": 0.5,
                    "voice.stage2_threshold": 0.7,
                    "voice.transcription": "alexa",
                    "voice.window_frames": 32,
                },
            )
        shutdown_logging(timeout=3.0)
        _wait_for_file(log_file)

        records = _read_jsonl(log_file)
        audio_recs = [r for r in records if r.get("event") == "audio.frame"]
        vad_recs = [r for r in records if r.get("event") == "voice.vad.state_changed"]
        ww_recs = [r for r in records if r.get("event") == "voice.wake_word.detected"]
        assert len(audio_recs) == 2
        assert len(vad_recs) == 1
        assert len(ww_recs) == 1
        # Every record under the saga inherits the same saga_id.
        for rec in audio_recs + vad_recs + ww_recs:
            assert rec["saga_id"] == saga_id
