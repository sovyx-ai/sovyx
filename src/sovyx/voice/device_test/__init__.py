"""Voice device test — live meter + TTS playback for the setup wizard.

This subpackage powers the browser-side device test on the setup wizard:
a user picks a microphone and watches an RMS/peak/hold meter light up in
real time; a user picks an output device, hits "play", and hears the
pipeline's TTS voice while an output meter shows what was actually sent
to the sink.

Design goals
------------

* **Enterprise-grade by default** — explicit protocol version, typed
  error codes, rate limiting per auth token, bounded session registry,
  OTel metrics, and dependency-injected sources/sinks so everything is
  fully testable without touching PortAudio.
* **Never destabilise the host audio stack** — every open/close is
  idempotent, every blocking call is wrapped in :func:`asyncio.to_thread`,
  PortAudio callbacks use :meth:`asyncio.loop.call_soon_threadsafe`, and
  runaway clients are capped by :class:`TokenReconnectLimiter`.
* **Refuse to collide with the live pipeline** — the router MUST return
  409 ``pipeline_active`` when the production voice pipeline is running;
  this module provides the primitives but enforcement lives in the route
  layer so the kill-switch is testable in isolation.

Public surface
--------------

See ``__all__`` below. Internal helpers live in ``_*.py`` modules and are
not re-exported (``_classify_portaudio_error``, ``_BaseFrame``, etc.).
"""

from __future__ import annotations

from sovyx.voice.device_test._limiter import (
    NoopLimiter,
    TokenReconnectLimiter,
    acquire_for_token,
    hash_token,
)
from sovyx.voice.device_test._meter import MeterReading, PeakHoldMeter
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
    WS_CLOSE_DEVICE_ERROR,
    WS_CLOSE_DISABLED,
    WS_CLOSE_PIPELINE_ACTIVE,
    WS_CLOSE_RATE_LIMITED,
    WS_CLOSE_REPLACED,
    WS_CLOSE_UNAUTHORIZED,
    CloseReason,
    ErrorCode,
    FrameType,
)
from sovyx.voice.device_test._session import (
    SessionConfig,
    SessionRegistry,
    TestSession,
    WSSender,
    monotonic_ms,
    new_session_id,
)
from sovyx.voice.device_test._sink import (
    AudioOutputSink,
    AudioSinkError,
    FakeAudioOutputSink,
    SoundDeviceOutputSink,
)
from sovyx.voice.device_test._source import (
    AudioInputSource,
    AudioSourceError,
    AudioSourceInfo,
    FakeAudioInputSource,
    SoundDeviceInputSource,
)

__all__ = [
    "PROTOCOL_VERSION",
    "WS_CLOSE_DEVICE_ERROR",
    "WS_CLOSE_DISABLED",
    "WS_CLOSE_PIPELINE_ACTIVE",
    "WS_CLOSE_RATE_LIMITED",
    "WS_CLOSE_REPLACED",
    "WS_CLOSE_UNAUTHORIZED",
    "AudioInputSource",
    "AudioOutputSink",
    "AudioSinkError",
    "AudioSourceError",
    "AudioSourceInfo",
    "CloseReason",
    "ClosedFrame",
    "DeviceInfo",
    "DevicesResponse",
    "ErrorCode",
    "ErrorFrame",
    "ErrorResponse",
    "FakeAudioInputSource",
    "FakeAudioOutputSink",
    "FrameType",
    "LevelFrame",
    "MeterReading",
    "NoopLimiter",
    "PeakHoldMeter",
    "ReadyFrame",
    "SessionConfig",
    "SessionRegistry",
    "SoundDeviceInputSource",
    "SoundDeviceOutputSink",
    "TestOutputJob",
    "TestOutputRequest",
    "TestOutputResult",
    "TestSession",
    "TokenReconnectLimiter",
    "WSSender",
    "acquire_for_token",
    "hash_token",
    "monotonic_ms",
    "new_session_id",
]
