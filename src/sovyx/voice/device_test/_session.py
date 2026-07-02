"""Live audio meter session.

Each :class:`TestSession` owns one :class:`AudioInputSource`, one
:class:`PeakHoldMeter`, and one WebSocket. It runs as an async context
manager so the audio stream is *always* closed — even if the client
disconnects, raises, or the server shuts down mid-frame.

The :class:`SessionRegistry` enforces at most ``max_sessions_per_token``
concurrent sessions per auth token; newer connections replace older
ones (the server sends a :class:`ClosedFrame` with reason
``session_replaced`` before dropping the old WS).

Liveness contract (DOCTOR-6,
MISSION-AUDIO-ENGINE-CROSS-PLATFORM-AUDIT-2026-07-02): the
max-lifetime / peer-alive / stop checks are enforced by a deadline-
bounded wait that races the next source frame against the stop event —
NOT by the arrival of frames. A device whose callbacks stop firing
(APO wedge, driver freeze) can no longer hold the mic silently behind
a frozen meter: the session emits an honest
:class:`ErrorFrame` (``device_disappeared``) or :class:`ClosedFrame`
and releases the source within the configured caps.
"""

from __future__ import annotations

import asyncio
import contextlib
import secrets
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING

from sovyx.engine._lock_dict import LRULockDict
from sovyx.observability.logging import get_logger
from sovyx.observability.tasks import mark_consumed, spawn
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
    from collections.abc import AsyncIterator

    import numpy as np
    import numpy.typing as npt

    from sovyx.voice.device_test._source import AudioInputSource

logger = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class SessionConfig:
    """Runtime knobs derived from :class:`VoiceTuningConfig`.

    Attributes:
        frame_rate_hz / peak_hold_ms / peak_decay_db_per_sec /
            vad_trigger_db / clipping_db: meter ballistics.
        max_lifetime_s: v0.20.2 / Bug B — hard cap on total session
            wall-clock duration. ``0`` disables. Enforced by a
            deadline-bounded wait (DOCTOR-6) so it fires even when the
            source stops producing frames.
        peer_alive_timeout_s: v0.20.2 / Bug B — if the sender cannot
            deliver a frame for this many seconds (typically because the
            peer tab is frozen / backgrounded / the WS path is dead) the
            session closes with :attr:`CloseReason.PEER_DEAD`. ``0``
            disables. When the window elapses with zero frames FROM THE
            SOURCE as well, the session classifies the fault as
            ``device_disappeared`` (frame starvation) instead of
            blaming the peer (DOCTOR-6).
        force_close_grace_s: v0.20.2 / Bug B — after :meth:`stop` is
            called, wait this long for :meth:`run` to finalize before
            :meth:`force_close` kicks the source shut directly. ``0``
            means no grace (force-close immediately on stop).
    """

    frame_rate_hz: int
    peak_hold_ms: int
    peak_decay_db_per_sec: float
    vad_trigger_db: float
    clipping_db: float
    max_lifetime_s: float = 300.0
    peer_alive_timeout_s: float = 10.0
    force_close_grace_s: float = 2.0


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
        # v0.20.2 / Bug B — signalled after :meth:`_finalize` finishes,
        # so :meth:`wait_closed` can synchronise on actual source
        # release. The plain ``self._closed`` flag is not awaitable and
        # flips earlier (inside finalize) than the source-close path
        # the caller actually needs to wait for.
        self._closed_event = asyncio.Event()
        self._closed = False
        self._started = False
        self._stop_reason: CloseReason | None = None

    @property
    def session_id(self) -> str:
        return self._session_id

    async def stop(self, reason: CloseReason = CloseReason.SERVER_SHUTDOWN) -> None:
        """Request the :meth:`run` loop to drain and exit gracefully.

        Idempotent — subsequent calls keep the original reason so a
        concurrent max-lifetime timer doesn't overwrite a caller's
        explicit ``SESSION_REPLACED`` label.
        """
        if self._stop_reason is None:
            self._stop_reason = reason
        self._stop_event.set()

    async def wait_closed(self, timeout: float | None = None) -> bool:
        """Wait for :meth:`_finalize` to finish releasing the source.

        v0.20.2 / Bug B. Returns ``True`` if the session closed within
        ``timeout`` (or was already closed), ``False`` on timeout. The
        registry awaits this before handing the mic to a new session so
        PortAudio does not get two concurrent owners on the same
        endpoint — the classic cause of the "voice_test holds the mic
        across /api/voice/enable" class of failures.
        """
        if self._closed_event.is_set():
            return True
        try:
            await asyncio.wait_for(self._closed_event.wait(), timeout=timeout)
        except TimeoutError:
            return False
        return True

    async def force_close(self) -> None:
        """Release the audio source synchronously from an outside caller.

        v0.20.2 / Bug B. Used after :meth:`wait_closed` times out — the
        :meth:`run` coroutine is stuck (frozen peer, slow WS drain,
        PortAudio hang) and the mic must be freed NOW so the production
        pipeline can open it. Subsequent close calls from :meth:`run`
        become no-ops via the idempotent ``_closed`` flag in
        :meth:`_finalize`.

        Idempotent and safe to call even when the source never opened.
        """
        if self._closed:
            # :meth:`run` already finalized — nothing to force.
            self._closed_event.set()
            return
        self._closed = True
        await self._safe_close_source()
        self._closed_event.set()
        logger.warning(
            "voice_test_session_force_closed",
            session_id=self._session_id,
            stop_reason=(self._stop_reason.value if self._stop_reason else None),
        )

    async def run(self) -> None:
        """Open the device and stream meter frames until stopped."""
        self._started = True
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
        """Stream meter frames, enforcing liveness independent of frame arrival.

        DOCTOR-6 (MISSION-AUDIO-ENGINE-CROSS-PLATFORM-AUDIT-2026-07-02):
        pre-fix the max-lifetime / peer-dead / stop checks ran only when
        the source yielded a frame — a device whose callbacks stopped
        firing parked the loop inside ``async for`` forever, silently
        holding the mic behind a frozen meter. Post-fix the loop races
        the next-frame await against (a) the stop event (via the
        stop-waiter task, previously spawned but dead code) and (b) the
        nearest enabled deadline, so every check fires on schedule even
        with zero frames. Frame starvation (an entire
        ``peer_alive_timeout_s`` window with no frames from the source)
        raises :class:`AudioSourceError` with
        :attr:`ErrorCode.DEVICE_DISAPPEARED` so :meth:`run` emits an
        honest ErrorFrame + DEVICE_ERROR close instead of mislabelling
        the fault as a dead peer.
        """
        frame_interval = 1.0 / self._config.frame_rate_hz
        open_ts = self._loop.time()
        last_emit = open_ts
        # v0.20.2 / Bug B — track the last *successful* send so we can
        # detect a frozen / backgrounded peer that leaves the WS half-
        # open. Seeded to open_ts so a peer that never reads anything
        # still trips the watchdog after peer_alive_timeout_s.
        last_successful_send = open_ts
        # DOCTOR-6 — track the last frame *arrival* separately from
        # sends so a starved source (callbacks stopped firing) is
        # classified as a device fault, not a dead peer.
        last_frame_ts = open_ts
        explicit_reason: CloseReason | None = None

        iterator = self._source.frames().__aiter__()
        stop_task = spawn(self._stop_event.wait(), name="device-test-stop-waiter")
        frame_task: asyncio.Task[npt.NDArray[np.int16]] | None = None
        try:
            while True:
                now = self._loop.time()
                if self._stop_event.is_set():
                    explicit_reason = self._stop_reason or CloseReason.SERVER_SHUTDOWN
                    break
                # Max-lifetime cap: browser tabs left open for hours
                # must not hold the mic forever.
                if (
                    self._config.max_lifetime_s > 0
                    and (now - open_ts) >= self._config.max_lifetime_s
                ):
                    explicit_reason = CloseReason.MAX_LIFETIME
                    logger.info(
                        "voice_test_session_max_lifetime",
                        session_id=self._session_id,
                        lifetime_s=round(now - open_ts, 2),
                    )
                    break
                # Peer-aliveness watchdog: if we cannot push a frame for
                # peer_alive_timeout_s, either the peer is dead (frames
                # flowed but never reached it) or the SOURCE is starved
                # (no frames at all — DOCTOR-6 honest classification).
                if (
                    self._config.peer_alive_timeout_s > 0
                    and (now - last_successful_send) >= self._config.peer_alive_timeout_s
                ):
                    silent_s = now - last_successful_send
                    if (now - last_frame_ts) >= self._config.peer_alive_timeout_s:
                        logger.warning(
                            "voice_test_session_frame_starved",
                            session_id=self._session_id,
                            silent_s=round(silent_s, 2),
                        )
                        raise AudioSourceError(
                            ErrorCode.DEVICE_DISAPPEARED,
                            f"no frames from device for {silent_s:.1f}s "
                            "(device callbacks stopped firing)",
                        )
                    explicit_reason = CloseReason.PEER_DEAD
                    logger.info(
                        "voice_test_session_peer_dead",
                        session_id=self._session_id,
                        silent_s=round(silent_s, 2),
                    )
                    break

                if frame_task is None:
                    frame_task = asyncio.ensure_future(anext(iterator))
                done, _pending = await asyncio.wait(
                    {frame_task, stop_task},
                    timeout=self._next_wakeup_budget(
                        now,
                        open_ts=open_ts,
                        last_successful_send=last_successful_send,
                    ),
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if frame_task not in done:
                    # Deadline tick or stop event — top of loop decides.
                    continue
                finished = frame_task
                frame_task = None
                try:
                    audio_frame = finished.result()
                except StopAsyncIteration:
                    # Source exhausted / closed underneath us.
                    break

                now = self._loop.time()
                last_frame_ts = now
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
                last_successful_send = self._loop.time()
        finally:
            if frame_task is not None:
                frame_task.cancel()
                try:
                    await frame_task
                except asyncio.CancelledError:
                    pass
                except Exception as exc:  # noqa: BLE001 — teardown drain; close reason already decided
                    logger.debug(
                        "voice_test_frame_task_drain_skipped",
                        session_id=self._session_id,
                        reason=str(exc),
                    )
            if stop_task.done() and not stop_task.cancelled():
                # Stop event fired — result observed via the loop-top
                # check; mark consumed so the task registry doesn't
                # flag a false orphan.
                mark_consumed(stop_task)
            else:
                stop_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await stop_task
            aclose = getattr(iterator, "aclose", None)
            if aclose is not None:
                try:
                    await aclose()
                except Exception as exc:  # noqa: BLE001 — generator finalisation is best-effort
                    logger.debug(
                        "voice_test_iterator_aclose_skipped",
                        session_id=self._session_id,
                        reason=str(exc),
                    )
        return explicit_reason or CloseReason.CLIENT_DISCONNECT

    def _next_wakeup_budget(
        self,
        now: float,
        *,
        open_ts: float,
        last_successful_send: float,
    ) -> float | None:
        """Time until the nearest enabled liveness deadline, or ``None``.

        DOCTOR-6 — bounds the next-frame wait so the deadline checks in
        :meth:`_stream_levels` run on schedule even when the source
        stops yielding frames. ``None`` (both caps disabled) means the
        wait is unbounded; the stop-waiter task still interrupts it.
        """
        deadlines: list[float] = []
        if self._config.max_lifetime_s > 0:
            deadlines.append(open_ts + self._config.max_lifetime_s)
        if self._config.peer_alive_timeout_s > 0:
            deadlines.append(last_successful_send + self._config.peer_alive_timeout_s)
        if not deadlines:
            return None
        return max(0.0, min(deadlines) - now)

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
            # Another caller (force_close) already released resources —
            # still signal the event in case run finalized after force.
            self._closed_event.set()
            return
        self._closed = True
        try:
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
        finally:
            # v0.20.2 / Bug B — unblock wait_closed() regardless of
            # sender errors. The registry's hand-off to a new session
            # cannot deadlock on a broken WebSocket send.
            self._closed_event.set()

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
    """Map internal :class:`CloseReason` to WS close codes.

    MAX_LIFETIME + PEER_DEAD use 1000 (normal closure) — the server is
    the one initiating the close for hygiene, not signalling an
    application-level error that clients should retry against.
    """
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

    v0.20.2 / Bug B — :meth:`register` and :meth:`close_all` now await
    :meth:`TestSession.wait_closed` after :meth:`TestSession.stop` so
    the mic is actually released before the hand-off completes. On
    timeout, :meth:`TestSession.force_close` releases the source
    directly. ``force_close_grace_s`` tunes the wait.
    """

    def __init__(
        self,
        *,
        max_per_token: int = 1,
        force_close_grace_s: float = 2.0,
    ) -> None:
        if max_per_token <= 0:
            msg = "max_per_token must be > 0"
            raise ValueError(msg)
        self._max = max_per_token
        self._force_close_grace_s = force_close_grace_s
        self._sessions: dict[str, list[TestSession]] = {}
        self._locks: LRULockDict[str] = LRULockDict(maxsize=2_048)
        # Mission H4 §T2.4 — register for cohort observability.
        from sovyx.observability._resource_registry import (  # noqa: PLC0415 — lazy import
            register_lock_dict,
        )

        register_lock_dict(
            owner_id="voice.device_test.session_locks",
            dict_ref=self._locks,
        )
        # v0.38.0 / F2-H01 — single-writer claim across the whole
        # process. Used by the wizard recorder (and any future caller
        # that needs an uninterrupted PortAudio window) to fence VU
        # subscribes for the lifetime of a critical section, not just
        # the close_all() call. See :meth:`acquire_exclusive`.
        self.exclusive_lock = asyncio.Lock()

    @asynccontextmanager
    async def acquire_exclusive(
        self,
        *,
        role: str,
        ttl_s: float,
    ) -> AsyncIterator[None]:
        """Hold an exclusive PortAudio claim for the caller's lifetime.

        Closes every live session BEFORE yielding, then keeps the lock
        held while the caller runs. New WebSocket VU subscribes that
        observe ``self.exclusive_lock.locked()`` reject themselves with
        ``1013 recorder_busy`` instead of racing for the device.

        v0.38.0 / F2-H01 closure (audit §3.C). Defensive ceiling on
        the acquire: ``ttl_s + 1.0`` seconds — if the lock is still
        held past that, something is wrong and the caller raises rather
        than blocking the request handler indefinitely.

        Args:
            role: short label used in the timeout error and any future
                telemetry. Examples: ``"wizard_test_record"``.
            ttl_s: caller's expected critical-section duration. The
                acquire deadline is ``ttl_s + 1.0`` seconds.

        Raises:
            RuntimeError: when the lock cannot be acquired within
                ``ttl_s + 1.0`` seconds.
        """
        try:
            await asyncio.wait_for(
                self.exclusive_lock.acquire(),
                timeout=ttl_s + 1.0,
            )
        except TimeoutError as exc:
            msg = (
                f"failed to acquire exclusive PortAudio lock for "
                f"role={role!r} within {ttl_s + 1.0:.1f}s"
            )
            raise RuntimeError(msg) from exc
        try:
            await self.close_all()
            yield
        finally:
            self.exclusive_lock.release()

    async def register(
        self,
        token_key: str,
        session: TestSession,
    ) -> list[TestSession]:
        """Register a new session, returning the list of superseded sessions.

        Pre-v0.20.2 the caller was responsible for stopping superseded
        sessions. v0.20.2 / Bug B — the registry itself now stops +
        waits + force-closes superseded sessions BEFORE returning, so
        the caller can safely open PortAudio on the same endpoint
        without racing the old session's source. Returned list is kept
        for telemetry / logging only — every session in it is already
        fully closed by the time it's returned.
        """
        async with self._locks[token_key]:
            active = self._sessions.setdefault(token_key, [])
            to_evict: list[TestSession] = []
            if len(active) >= self._max:
                to_evict = active[: len(active) - (self._max - 1)]
                active = [s for s in active if s not in to_evict]
            active.append(session)
            self._sessions[token_key] = active

        # Stop + wait_closed + force_close OUTSIDE the lock so a stuck
        # source cannot block other tokens' registrations.
        if to_evict:
            await self._stop_and_wait(to_evict, reason=CloseReason.SESSION_REPLACED)
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

    async def close_all(self, *, reason: CloseReason = CloseReason.SERVER_SHUTDOWN) -> None:
        """Shut down every session and wait for each to release its source.

        Used as a server-shutdown hook AND as a pre-enable hook from
        ``/api/voice/enable`` so the production pipeline can open the
        mic without fighting a live meter session (v0.20.2 / Bug B).
        """
        all_sessions: list[TestSession] = []
        # Snapshot under locks, then release locks before stopping.
        for token_key in list(self._sessions.keys()):
            async with self._locks[token_key]:
                all_sessions.extend(self._sessions.get(token_key, []))
        if all_sessions:
            await self._stop_and_wait(all_sessions, reason=reason)

    async def _stop_and_wait(
        self,
        sessions: list[TestSession],
        *,
        reason: CloseReason,
    ) -> None:
        """Stop each session, await its close, force-close on timeout.

        Runs per-session sequentially on purpose: PortAudio driver
        cleanup on Windows is not reentrant-safe across concurrent
        close() calls on different streams sharing the same endpoint.
        The wait_closed timeout is small (grace window) so N stuck
        sessions still release within ~N × grace_s worst-case.

        Sessions whose :meth:`TestSession.run` was never invoked
        (e.g. registered but never scheduled) skip the wait entirely
        and jump to :meth:`TestSession.force_close` — there is no run
        loop to finalize, so waiting would just burn the grace window.
        """
        for sess in sessions:
            with contextlib.suppress(Exception):
                await sess.stop(reason)
            if not sess._started:
                with contextlib.suppress(Exception):
                    await sess.force_close()
                continue
            released = await sess.wait_closed(timeout=self._force_close_grace_s)
            if not released:
                # Stuck run loop (frozen sender, unresponsive PortAudio).
                # Kick the source shut directly so the next caller can
                # open the mic.
                with contextlib.suppress(Exception):
                    await sess.force_close()


def new_session_id() -> str:
    """Short random id used for log correlation."""
    return secrets.token_hex(8)


def monotonic_ms() -> int:
    return int(time.monotonic() * 1000)
