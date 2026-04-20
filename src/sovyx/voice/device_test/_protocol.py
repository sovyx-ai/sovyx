"""Wire protocol constants for the voice device test WS + HTTP endpoints.

The protocol is versioned explicitly (``PROTOCOL_VERSION``) so future
breaking changes can be rolled out safely — clients negotiate by reading
the ``v`` field on every frame.

All error codes are machine-readable enums the frontend can switch on to
render localised messages. Never send human strings as the primary error
signal; the ``detail`` field is best-effort English for logs / fallback.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Final

PROTOCOL_VERSION: Final[int] = 1

# Close codes (RFC 6455 application range 4000-4999).
WS_CLOSE_UNAUTHORIZED: Final[int] = 4001
WS_CLOSE_RATE_LIMITED: Final[int] = 4002
WS_CLOSE_PIPELINE_ACTIVE: Final[int] = 4009
WS_CLOSE_DISABLED: Final[int] = 4010
WS_CLOSE_REPLACED: Final[int] = 4012  # newer connection from same token
WS_CLOSE_DEVICE_ERROR: Final[int] = 4020


class FrameType(StrEnum):
    """Discriminator values for WS frames (field ``t``)."""

    LEVEL = "level"
    ERROR = "error"
    CLOSED = "closed"
    READY = "ready"  # server→client once stream is open


class ErrorCode(StrEnum):
    """Machine-readable error taxonomy.

    The frontend maps these to localised strings via i18n; the backend
    never sends English in ``code``, only in ``detail``.
    """

    DEVICE_NOT_FOUND = "device_not_found"
    DEVICE_BUSY = "device_busy"
    DEVICE_DISAPPEARED = "device_disappeared"
    PERMISSION_DENIED = "permission_denied"
    UNSUPPORTED_SAMPLERATE = "unsupported_samplerate"
    UNSUPPORTED_CHANNELS = "unsupported_channels"
    UNSUPPORTED_FORMAT = "unsupported_format"
    BUFFER_SIZE_INVALID = "buffer_size_invalid"
    PIPELINE_ACTIVE = "pipeline_active"
    RATE_LIMITED = "rate_limited"
    DISABLED = "disabled"
    REPLACED_BY_NEWER_SESSION = "replaced_by_newer_session"
    INTERNAL_ERROR = "internal_error"
    INVALID_REQUEST = "invalid_request"
    TTS_UNAVAILABLE = "tts_unavailable"
    MODELS_NOT_DOWNLOADED = "models_not_downloaded"
    JOB_NOT_FOUND = "job_not_found"
    JOB_EXPIRED = "job_expired"


class CloseReason(StrEnum):
    """Reason labels for :class:`ClosedFrame`.

    Members:
        CLIENT_DISCONNECT: WebSocket peer closed normally.
        SERVER_SHUTDOWN: Server-initiated close (process shutdown, global
            close-all on pipeline-enable).
        DEVICE_CHANGED: Device configuration changed mid-session.
        SESSION_REPLACED: Newer session from the same auth token bumped
            this one.
        DEVICE_ERROR: PortAudio / source raised; see adjacent ErrorFrame.
        MAX_LIFETIME: v0.20.2 / Bug B — the session exceeded
            :attr:`VoiceTuningConfig.device_test_max_lifetime_s`. Browser
            tabs that freeze / get minimised can keep a WS open but stop
            reading frames; the hard cap releases the mic for the real
            voice pipeline instead of leaking forever.
        PEER_DEAD: v0.20.2 / Bug B — no successful send for
            :attr:`VoiceTuningConfig.device_test_peer_alive_timeout_s`
            seconds. The WS claims to be open but the peer is not
            receiving (backgrounded tab, frozen browser, network path
            gone silent). Close to release the audio device.
    """

    CLIENT_DISCONNECT = "client_disconnect"
    SERVER_SHUTDOWN = "server_shutdown"
    DEVICE_CHANGED = "device_changed"
    SESSION_REPLACED = "session_replaced"
    DEVICE_ERROR = "device_error"
    MAX_LIFETIME = "max_lifetime"
    PEER_DEAD = "peer_dead"
