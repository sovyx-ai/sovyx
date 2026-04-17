"""Live audio meter session.

Each :class:`TestSession` owns one :class:`AudioInputSource`, one
:class:`PeakHoldMeter`, and one WebSocket. It runs as an async context
manager so the audio stream is *always* closed — even if the client
disconnects, raises, or the server shuts down mid-frame.

The :class:`SessionRegistry` enforces at most ``max_sessions_per_token``
concurrent sessions per auth token; newer connections replace older
ones (the server sends a :class:`ClosedFrame` with reason
``session_replaced`` before dropping the old WS).
"""

from __future__ import annotations

import asyncio
import contextlib
import secrets
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

from sovyx.engine._lock_dict import LRULockDict
from sovyx.observability.logging import get_logger
from sovyx.voice.device_test._meter import PeakHoldMeter
from sovyx.voice.device_test._models import (
    ClosedFrame,
    ErrorFrame,
    LevelFrame,
    ReadyFrame,
)
from sovyx.voice.device_test._protocol import CloseReason, ErrorCode
from sovyx.voice.device_test._source import AudioSourceError

if TYPE_CHECKING:
    from sovyx.voice.device_test._source import AudioInputSource

logger = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class SessionConfig:
    """Runtime knobs derived from :class:`VoiceTuningConfig`."""

    frame_rate_hz: int
    peak_hold_ms: int
    peak_decay_db_per_sec: float
    vad_trigger_db: float
    clipping_db: float


class WSSender:
    """Thin adapter so the session can be tested without FastAPI.

    Implementations must be idempotent on close and never raise after
    close — the session calls :meth:`close` in its ``finally`` block.
    """

    async def send_json(self, payload: dict[str, object]) -> None:  # pragma: no cover
        raise NotImplementedError

    async def close(self, code: int, reason: str) -> None:  # pragma: no cover
        raise NotImplementedError


class TestSession:
    """One live meter session.

    The session lifecycle is:

    #. ``open`` — opens the source, sends :class:`ReadyFrame`.
    #. streams :class:`LevelFrame` at ``frame_rate_hz``.
    #. on error → sends :class:`ErrorFrame` + :class:`ClosedFrame`.
    #. on close → always sends :class:`ClosedFrame` before returning.
    """

    __test__ = False  # not a pytest test class

    def __init__(
        self,
        *,
        session_id: str,
        source: AudioInputSource,
        sender: WSSender,
        config: SessionConfig,
        loop: asyncio.AbstractEventLoop | None = None,
    ) -> None:
        self._session_id = session_id
        self._source = source
        self._sender = sender
        self._config = config
        self._meter = PeakHoldMeter(
            hold_ms=config.peak_hold_ms,
            decay_db_per_sec=config.peak_decay_db_per_sec,
            vad_trigger_db=config.vad_trigger_db,
            clipping_db=config.clipping_db,
        )
        self._loop = loop or asyncio.get_event_loop()
        self._stop_event = asyncio.Event()
        self._closed = False

    @property
    def session_id(self) -> str:
        return self._session_id

    async def stop(self, reason: CloseReason = CloseReason.SERVER_SHUTDOWN) -> None:
        """Request the :meth:`run` loop to drain and exit gracefully."""
        self._stop_reason = reason
        self._stop_event.set()

    async def run(self) -> None:
        """Open the device and stream meter frames until stopped."""
        reason = CloseReason.CLIENT_DISCONNECT
        try:
            info = await self._source.open()
        except AudioSourceError as exc:
            await self._send_error_then_close(exc.code, exc.detail, CloseReason.DEVICE_ERROR)
            return
        except Exception as exc:  # noqa: BLE001
            await self._send_error_then_close(
                ErrorCode.INTERNAL_ERROR,
                f"Unexpected source error: {exc}",
                CloseReason.DEVICE_ERROR,
            )
            return

        try:
            await self._sender.send_json(
                ReadyFrame(
                    device_id=info.device_id,
                    device_name=info.device_name,
                    sample_rate=info.sample_rate,
                    channels=info.channels,
                ).model_dump(),
            )
        except Exception:  # noqa: BLE001
            await self._safe_close_source()
            return

        try:
            reason = await self._stream_levels()
        except AudioSourceError as exc:
            await self._send_error(exc.code, exc.detail)
            reason = CloseReason.DEVICE_ERROR
        except asyncio.CancelledError:
            reason = CloseReason.SERVER_SHUTDOWN
            raise
        except Exception as exc:  # noqa: BLE001
            logger.exception("voice_test_session_error", session_id=self._session_id)
            await self._send_error(
                ErrorCode.INTERNAL_ERROR,
                f"Session error: {exc}",
            )
            reason = CloseReason.DEVICE_ERROR
        finally:
            await self._finalize(reason)

    async def _stream_levels(self) -> CloseReason:
        frame_interval = 1.0 / self._config.frame_rate_hz
        last_emit = self._loop.time()
        explicit_reason: CloseReason | None = None

        stop_task = asyncio.create_task(self._stop_event.wait())
        try:
            async for audio_frame in self._source.frames():
                if self._stop_event.is_set():
                    explicit_reason = getattr(
                        self,
                        "_stop_reason",
                        CloseReason.SERVER_SHUTDOWN,
                    )
                    break
                now = self._loop.time()
                reading = self._meter.process(audio_frame, clock_s=now)
                if now - last_emit < frame_interval:
                    # Under-sample: drop frames to maintain target rate.
                    continue
                last_emit = now
                frame = LevelFrame(
                    rms_db=_round(reading.rms_db),
                    peak_db=_round(reading.peak_db),
                    hold_db=_round(reading.hold_db),
                    clipping=reading.clipping,
                    vad_trigger=reading.vad_trigger,
                )
                try:
                    await self._sender.send_json(frame.model_dump())
                except Exception:  # noqa: BLE001
                    # Client went away.
                    return CloseReason.CLIENT_DISCONNECT
        finally:
            stop_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await stop_task
        return explicit_reason or CloseReason.CLIENT_DISCONNECT

    async def _send_error(self, code: ErrorCode, detail: str) -> None:
        try:
            await self._sender.send_json(
                ErrorFrame(code=code, detail=detail, retryable=False).model_dump(),
            )
        except Exception:  # noqa: BLE001
            logger.debug("voice_test_send_error_failed", session_id=self._session_id)

    async def _send_error_then_close(
        self,
        code: ErrorCode,
        detail: str,
        reason: CloseReason,
    ) -> None:
        await self._send_error(code, detail)
        await self._finalize(reason)

    async def _finalize(self, reason: CloseReason) -> None:
        if self._closed:
            return
        self._closed = True
        await self._safe_close_source()
        with contextlib.suppress(Exception):
            await self._sender.send_json(ClosedFrame(reason=reason).model_dump())
        close_code = _close_code_for_reason(reason)
        with contextlib.suppress(Exception):
            await self._sender.close(close_code, reason.value)
        logger.info(
            "voice_test_session_closed",
            session_id=self._session_id,
            reason=reason.value,
        )

    async def _safe_close_source(self) -> None:
        try:
            await self._source.close()
        except Exception as exc:  # noqa: BLE001
            logger.debug(
                "voice_test_source_close_failed",
                session_id=self._session_id,
                error=str(exc),
            )


def _round(value: float) -> float:
    """Keep level frames compact on the wire (1 dp is enough for UI)."""
    return round(value, 1)


def _close_code_for_reason(reason: CloseReason) -> int:
    """Map internal :class:`CloseReason` to WS close codes."""
    from sovyx.voice.device_test._protocol import (
        WS_CLOSE_DEVICE_ERROR,
        WS_CLOSE_REPLACED,
    )

    if reason == CloseReason.DEVICE_ERROR:
        return WS_CLOSE_DEVICE_ERROR
    if reason == CloseReason.SESSION_REPLACED:
        return WS_CLOSE_REPLACED
    return 1000  # normal closure


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


class SessionRegistry:
    """Enforces at most ``max_per_token`` concurrent sessions per token.

    When ``max_per_token`` is 1 (the default), opening a new session for
    a token that already has one causes the older session to receive a
    :class:`ClosedFrame` with reason ``session_replaced`` before being
    dropped. This prevents runaway meter sessions from idle browser tabs.
    """

    def __init__(self, *, max_per_token: int = 1) -> None:
        if max_per_token <= 0:
            msg = "max_per_token must be > 0"
            raise ValueError(msg)
        self._max = max_per_token
        self._sessions: dict[str, list[TestSession]] = {}
        self._locks: LRULockDict[str] = LRULockDict(maxsize=2_048)

    async def register(
        self,
        token_key: str,
        session: TestSession,
    ) -> list[TestSession]:
        """Register a new session, returning the list of superseded sessions.

        The caller is expected to call :meth:`TestSession.stop` on every
        returned session (with reason ``SESSION_REPLACED``) before
        awaiting :meth:`TestSession.run` on the new one.
        """
        async with self._locks[token_key]:
            active = self._sessions.setdefault(token_key, [])
            to_evict = active[: -self._max] if len(active) >= self._max else []
            # Always keep only the last (max - 1) + new one after register.
            if len(active) >= self._max:
                to_evict = active[: len(active) - (self._max - 1)]
                active = [s for s in active if s not in to_evict]
            active.append(session)
            self._sessions[token_key] = active
            return to_evict

    async def unregister(self, token_key: str, session: TestSession) -> None:
        async with self._locks[token_key]:
            active = self._sessions.get(token_key)
            if not active:
                return
            self._sessions[token_key] = [s for s in active if s is not session]
            if not self._sessions[token_key]:
                self._sessions.pop(token_key, None)

    async def active_count(self, token_key: str) -> int:
        async with self._locks[token_key]:
            return len(self._sessions.get(token_key, []))

    async def close_all(self) -> None:
        """Shut down every session (server shutdown hook)."""
        all_sessions: list[TestSession] = []
        # Snapshot without holding locks while we stop.
        for token_key in list(self._sessions.keys()):
            async with self._locks[token_key]:
                all_sessions.extend(self._sessions.get(token_key, []))
        for sess in all_sessions:
            with contextlib.suppress(Exception):
                await sess.stop(CloseReason.SERVER_SHUTDOWN)


def new_session_id() -> str:
    """Short random id used for log correlation."""
    return secrets.token_hex(8)


def monotonic_ms() -> int:
    return int(time.monotonic() * 1000)
