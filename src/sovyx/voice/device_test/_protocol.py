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
    PIPELINE_ACTIVE = "pipeline_active"
    RATE_LIMITED = "rate_limited"
    DISABLED = "disabled"
    REPLACED_BY_NEWER_SESSION = "replaced_by_newer_session"
    INTERNAL_ERROR = "internal_error"
    INVALID_REQUEST = "invalid_request"
    TTS_UNAVAILABLE = "tts_unavailable"
    JOB_NOT_FOUND = "job_not_found"
    JOB_EXPIRED = "job_expired"


class CloseReason(StrEnum):
    """Reason labels for :class:`ClosedFrame`."""

    CLIENT_DISCONNECT = "client_disconnect"
    SERVER_SHUTDOWN = "server_shutdown"
    DEVICE_CHANGED = "device_changed"
    SESSION_REPLACED = "session_replaced"
    DEVICE_ERROR = "device_error"
