"""Tests for :mod:`sovyx.voice.device_test._models` — Pydantic wire models."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from sovyx.voice.device_test._models import (
    ClosedFrame,
    DeviceInfo,
    DevicesResponse,
    ErrorFrame,
    ErrorResponse,
    LevelFrame,
    ReadyFrame,
    TestOutputJob,
    TestOutputRequest,
    TestOutputResult,
)
from sovyx.voice.device_test._protocol import (
    PROTOCOL_VERSION,
    CloseReason,
    ErrorCode,
    FrameType,
)


class TestEnvelope:
    """Every frame carries ``v`` (protocol) + ``t`` (discriminator)."""

    def test_level_frame_emits_type_and_version(self) -> None:
        frame = LevelFrame(
            rms_db=-30.0,
            peak_db=-20.0,
            hold_db=-20.0,
            clipping=False,
            vad_trigger=False,
        )
        payload = frame.model_dump()
        assert payload["v"] == PROTOCOL_VERSION
        assert payload["t"] == FrameType.LEVEL.value

    def test_ready_frame_type(self) -> None:
        frame = ReadyFrame(
            device_name="mic",
            sample_rate=16_000,
            channels=1,
        )
        assert frame.model_dump()["t"] == FrameType.READY.value

    def test_error_frame_type(self) -> None:
        frame = ErrorFrame(code=ErrorCode.DEVICE_BUSY, detail="busy")
        payload = frame.model_dump()
        assert payload["t"] == FrameType.ERROR.value
        assert payload["code"] == ErrorCode.DEVICE_BUSY.value

    def test_closed_frame_type(self) -> None:
        frame = ClosedFrame(reason=CloseReason.CLIENT_DISCONNECT)
        payload = frame.model_dump()
        assert payload["t"] == FrameType.CLOSED.value
        assert payload["reason"] == CloseReason.CLIENT_DISCONNECT.value


class TestLevelFrameBounds:
    """Pydantic enforces the dB range documented in the protocol."""

    def test_rms_below_floor_rejected(self) -> None:
        with pytest.raises(ValidationError):
            LevelFrame(
                rms_db=-121.0,
                peak_db=-20.0,
                hold_db=-20.0,
                clipping=False,
                vad_trigger=False,
            )

    def test_peak_above_ceiling_rejected(self) -> None:
        with pytest.raises(ValidationError):
            LevelFrame(
                rms_db=-30.0,
                peak_db=7.0,
                hold_db=-20.0,
                clipping=False,
                vad_trigger=False,
            )


class TestLevelFrameFrozen:
    """Frames are immutable so they can be safely shared across coroutines."""

    def test_cannot_mutate_field(self) -> None:
        frame = LevelFrame(
            rms_db=-30.0,
            peak_db=-20.0,
            hold_db=-20.0,
            clipping=False,
            vad_trigger=False,
        )
        with pytest.raises(ValidationError):
            frame.rms_db = -40.0  # type: ignore[misc]


class TestErrorFrame:
    """:class:`ErrorFrame` caps detail length for log hygiene."""

    def test_detail_too_long_rejected(self) -> None:
        with pytest.raises(ValidationError):
            ErrorFrame(code=ErrorCode.INTERNAL_ERROR, detail="x" * 501)

    def test_retryable_defaults_false(self) -> None:
        frame = ErrorFrame(code=ErrorCode.DEVICE_BUSY, detail="held")
        assert frame.retryable is False


class TestTestOutputRequest:
    """Input sanitisation for ``POST /api/voice/test/output``."""

    def test_defaults(self) -> None:
        req = TestOutputRequest()
        assert req.device_id is None
        assert req.voice is None
        assert req.phrase_key == "default"
        assert req.language == "en"

    def test_voice_length_cap(self) -> None:
        with pytest.raises(ValidationError):
            TestOutputRequest(voice="v" * 101)

    def test_phrase_key_length_cap(self) -> None:
        with pytest.raises(ValidationError):
            TestOutputRequest(phrase_key="k" * 61)

    def test_language_length_cap(self) -> None:
        with pytest.raises(ValidationError):
            TestOutputRequest(language="l" * 11)


class TestDevicesResponse:
    """Discovery response includes the current protocol version."""

    def test_round_trip(self) -> None:
        resp = DevicesResponse(
            input_devices=[
                DeviceInfo(
                    index=0,
                    name="Mic",
                    is_default=True,
                    max_input_channels=2,
                    max_output_channels=0,
                    default_samplerate=48_000,
                ),
            ],
            output_devices=[],
        )
        payload = resp.model_dump()
        assert payload["ok"] is True
        assert payload["protocol_version"] == PROTOCOL_VERSION
        assert payload["input_devices"][0]["name"] == "Mic"


class TestTestOutputJob:
    """Job launcher payload."""

    def test_has_job_id_and_status(self) -> None:
        job = TestOutputJob(job_id="abcd1234", status="queued")
        payload = job.model_dump()
        assert payload["ok"] is True
        assert payload["job_id"] == "abcd1234"
        assert payload["status"] == "queued"


class TestTestOutputResult:
    """Terminal result is allowed to carry optional diagnostic fields."""

    def test_error_result(self) -> None:
        result = TestOutputResult(
            ok=False,
            job_id="abcd1234",
            status="error",
            code=ErrorCode.TTS_UNAVAILABLE,
            detail="no voice model",
        )
        payload = result.model_dump()
        assert payload["code"] == ErrorCode.TTS_UNAVAILABLE.value
        assert payload["detail"] == "no voice model"

    def test_success_result_optional_fields(self) -> None:
        result = TestOutputResult(
            ok=True,
            job_id="abcd1234",
            status="done",
            phrase="Hello",
            synthesis_ms=120.5,
            playback_ms=450.0,
            peak_db=-3.2,
        )
        assert result.code is None


class TestErrorResponse:
    """HTTP error envelope uses the same ``{ok, code, detail}`` pattern."""

    def test_ok_is_false(self) -> None:
        resp = ErrorResponse(code=ErrorCode.DISABLED, detail="kill-switch")
        payload = resp.model_dump()
        assert payload["ok"] is False
        assert payload["code"] == ErrorCode.DISABLED.value


class TestProtocolConstants:
    """Stable enum values — breaking these is a wire-protocol break."""

    def test_protocol_version_is_one(self) -> None:
        assert PROTOCOL_VERSION == 1

    def test_frame_type_values(self) -> None:
        assert FrameType.LEVEL.value == "level"
        assert FrameType.ERROR.value == "error"
        assert FrameType.CLOSED.value == "closed"
        assert FrameType.READY.value == "ready"

    def test_error_code_values(self) -> None:
        # Spot-check a handful — any drift must be intentional.
        assert ErrorCode.DEVICE_BUSY.value == "device_busy"
        assert ErrorCode.PIPELINE_ACTIVE.value == "pipeline_active"
        assert ErrorCode.RATE_LIMITED.value == "rate_limited"
        assert ErrorCode.DISABLED.value == "disabled"
        assert ErrorCode.TTS_UNAVAILABLE.value == "tts_unavailable"

    def test_close_reason_values(self) -> None:
        assert CloseReason.CLIENT_DISCONNECT.value == "client_disconnect"
        assert CloseReason.SESSION_REPLACED.value == "session_replaced"
        assert CloseReason.DEVICE_ERROR.value == "device_error"
