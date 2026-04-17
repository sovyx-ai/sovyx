"""Pydantic v2 models for the voice device test protocol.

Every WS frame carries ``v`` (protocol version) + ``t`` (type discriminator)
so frontends can safely switch on type and reject unknown versions.

HTTP request/response bodies follow the same ``{"ok": true/false, ...}``
pattern used by the rest of the dashboard routes (see
:mod:`sovyx.dashboard.routes.voice`), enriched with a machine-readable
``code`` field that the UI localises via i18n.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from sovyx.voice.device_test._protocol import (
    PROTOCOL_VERSION,
    CloseReason,
    ErrorCode,
    FrameType,
)


class _BaseFrame(BaseModel):
    """Common WS envelope (``v`` + ``t``)."""

    model_config = ConfigDict(use_enum_values=True, frozen=True)

    v: int = Field(default=PROTOCOL_VERSION, description="Protocol version")
    t: FrameType


class ReadyFrame(_BaseFrame):
    """Sent once after the server successfully opens the device."""

    t: FrameType = FrameType.READY
    device_id: int | None = None
    device_name: str
    sample_rate: int
    channels: int


class LevelFrame(_BaseFrame):
    """One level-meter tick (streamed at ``device_test_frame_rate_hz``)."""

    t: FrameType = FrameType.LEVEL
    rms_db: float = Field(ge=-120.0, le=6.0, description="RMS level in dBFS")
    peak_db: float = Field(ge=-120.0, le=6.0, description="Instantaneous peak in dBFS")
    hold_db: float = Field(ge=-120.0, le=6.0, description="Peak-hold (ballistic) in dBFS")
    clipping: bool
    vad_trigger: bool = Field(description="True when RMS crossed the VAD threshold")


class ErrorFrame(_BaseFrame):
    """Structured error surfaced to the client."""

    t: FrameType = FrameType.ERROR
    code: ErrorCode
    detail: str = Field(max_length=500)
    retryable: bool = False


class ClosedFrame(_BaseFrame):
    """Graceful close notification — always the last frame the server sends."""

    t: FrameType = FrameType.CLOSED
    reason: CloseReason


class DeviceInfo(BaseModel):
    """Audio device entry (mirrors :mod:`sovyx.dashboard.routes.voice`)."""

    model_config = ConfigDict(frozen=True)

    index: int
    name: str
    is_default: bool
    max_input_channels: int
    max_output_channels: int
    default_samplerate: int


class DevicesResponse(BaseModel):
    """Response for ``GET /api/voice/test/devices``."""

    ok: bool = True
    protocol_version: int = PROTOCOL_VERSION
    input_devices: list[DeviceInfo]
    output_devices: list[DeviceInfo]


class TestOutputRequest(BaseModel):
    """Request body for ``POST /api/voice/test/output``."""

    model_config = ConfigDict(frozen=True)

    device_id: int | None = Field(
        default=None,
        description="PortAudio output device index (None = system default)",
    )
    voice: str | None = Field(
        default=None,
        description="TTS voice id (None = pipeline default)",
        max_length=100,
    )
    phrase_key: str = Field(
        default="default",
        description="Localised test phrase identifier",
        max_length=60,
    )
    language: str = Field(default="en", max_length=10)


class TestOutputJob(BaseModel):
    """Response for ``POST /api/voice/test/output`` (202-style)."""

    ok: bool = True
    job_id: str
    status: str = Field(description="queued | synthesising | playing | done | error")


class TestOutputResult(BaseModel):
    """Final result surface for ``GET /api/voice/test/output/{job_id}``."""

    ok: bool
    job_id: str
    status: str
    code: ErrorCode | None = None
    detail: str | None = None
    phrase: str | None = None
    synthesis_ms: float | None = None
    playback_ms: float | None = None
    peak_db: float | None = None


class ErrorResponse(BaseModel):
    """Shared HTTP error envelope for voice-test endpoints."""

    ok: bool = False
    code: ErrorCode
    detail: str = Field(max_length=500)
