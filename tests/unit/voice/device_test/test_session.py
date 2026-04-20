"""Tests for :class:`TestSession` lifecycle and :class:`SessionRegistry`."""

from __future__ import annotations

import asyncio
import contextlib
from typing import TYPE_CHECKING

import numpy as np
import pytest

from sovyx.voice.device_test._models import LevelFrame
from sovyx.voice.device_test._protocol import CloseReason, ErrorCode, FrameType
from sovyx.voice.device_test._session import (
    SessionConfig,
    SessionRegistry,
    TestSession,
    WSSender,
    monotonic_ms,
    new_session_id,
)
from sovyx.voice.device_test._source import (
    AudioSourceError,
    FakeAudioInputSource,
)

if TYPE_CHECKING:
    import numpy.typing as npt


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------


def _tone_frames(amplitude: float, n_frames: int, size: int = 512) -> list[np.ndarray]:
    """Generate ``n_frames`` int16 frames at a flat amplitude."""
    sample = int(amplitude * 32_767)
    return [np.full(size, sample, dtype=np.int16) for _ in range(n_frames)]


class _RecordingSender(WSSender):
    """In-memory :class:`WSSender` capturing every payload."""

    def __init__(self, *, fail_after: int | None = None) -> None:
        self.payloads: list[dict[str, object]] = []
        self.closed_with: tuple[int, str] | None = None
        self._fail_after = fail_after

    async def send_json(self, payload: dict[str, object]) -> None:
        if self._fail_after is not None and len(self.payloads) >= self._fail_after:
            raise ConnectionError("client gone")
        self.payloads.append(payload)

    async def close(self, code: int, reason: str) -> None:
        self.closed_with = (code, reason)


def _config(**overrides: object) -> SessionConfig:
    defaults: dict[str, object] = {
        "frame_rate_hz": 60,
        "peak_hold_ms": 1_000,
        "peak_decay_db_per_sec": 20.0,
        "vad_trigger_db": -30.0,
        "clipping_db": -0.3,
    }
    defaults.update(overrides)
    return SessionConfig(**defaults)  # type: ignore[arg-type]


# --------------------------------------------------------------------------
# Lifecycle tests
# --------------------------------------------------------------------------


class TestSessionLifecycle:
    """Happy-path + error-path session runs."""

    @pytest.mark.asyncio()
    async def test_happy_path_emits_ready_levels_closed(self) -> None:
        frames: list[npt.NDArray[np.int16]] = _tone_frames(0.5, 10)
        source = FakeAudioInputSource(
            frames,
            frame_interval_s=0.01,
            device_name="mic",
        )
        sender = _RecordingSender()
        # 200 Hz emit + 10 ms arrivals guarantees every arrival passes the gate.
        session = TestSession(
            session_id=new_session_id(),
            source=source,
            sender=sender,
            config=_config(frame_rate_hz=200),
        )
        await session.run()

        types = [p["t"] for p in sender.payloads]
        assert types[0] == FrameType.READY.value
        assert FrameType.LEVEL.value in types
        assert types[-1] == FrameType.CLOSED.value
        # Regular close code 1000.
        assert sender.closed_with == (1000, CloseReason.CLIENT_DISCONNECT.value)

    @pytest.mark.asyncio()
    async def test_level_frame_payload_is_valid(self) -> None:
        frames = _tone_frames(0.5, 5)
        source = FakeAudioInputSource(frames, frame_interval_s=0.01)
        sender = _RecordingSender()
        session = TestSession(
            session_id=new_session_id(),
            source=source,
            sender=sender,
            config=_config(frame_rate_hz=200),
        )
        await session.run()

        level_payloads = [p for p in sender.payloads if p["t"] == FrameType.LEVEL.value]
        assert level_payloads, "expected at least one LevelFrame"
        # Round-trip through Pydantic: must re-validate.
        frame = LevelFrame.model_validate(level_payloads[0])
        assert -120.0 <= frame.rms_db <= 6.0
        assert frame.peak_db >= frame.rms_db - 0.5  # tolerance for rounding

    @pytest.mark.asyncio()
    async def test_open_failure_sends_error_and_device_close(self) -> None:
        err = AudioSourceError(ErrorCode.DEVICE_NOT_FOUND, "nope")
        source = FakeAudioInputSource([], error_on_open=err)
        sender = _RecordingSender()
        session = TestSession(
            session_id=new_session_id(),
            source=source,
            sender=sender,
            config=_config(),
        )
        await session.run()

        # Order: ErrorFrame → ClosedFrame.
        types = [p["t"] for p in sender.payloads]
        assert FrameType.ERROR.value in types
        assert types[-1] == FrameType.CLOSED.value
        # device_error close code is 4020.
        assert sender.closed_with is not None
        assert sender.closed_with[0] == 4020

    @pytest.mark.asyncio()
    async def test_client_disconnect_mid_stream(self) -> None:
        frames = _tone_frames(0.2, 10)
        source = FakeAudioInputSource(frames, frame_interval_s=0.0)
        # ready + 1 level frame → then blow up.
        sender = _RecordingSender(fail_after=2)
        session = TestSession(
            session_id=new_session_id(),
            source=source,
            sender=sender,
            config=_config(frame_rate_hz=1),
        )
        await session.run()
        # Session should have closed cleanly despite send failures.
        # The closed payload attempt also fails (contextlib.suppress hides it),
        # but close() itself succeeds.
        assert sender.closed_with is not None

    @pytest.mark.asyncio()
    async def test_stop_triggers_graceful_close(self) -> None:
        # Slow cadence so the loop has time to observe the stop.
        frames = _tone_frames(0.2, 1_000)
        source = FakeAudioInputSource(frames, frame_interval_s=0.01)
        sender = _RecordingSender()
        session = TestSession(
            session_id=new_session_id(),
            source=source,
            sender=sender,
            config=_config(frame_rate_hz=30),
        )
        run_task = asyncio.create_task(session.run())
        # Let it emit a few frames.
        await asyncio.sleep(0.05)
        await session.stop(CloseReason.SESSION_REPLACED)
        await asyncio.wait_for(run_task, timeout=2.0)

        # Last payload is a ClosedFrame carrying the stop reason.
        closed = [p for p in sender.payloads if p["t"] == FrameType.CLOSED.value]
        assert closed
        assert closed[-1]["reason"] == CloseReason.SESSION_REPLACED.value
        # 4012 is the "session replaced" code.
        assert sender.closed_with == (4012, CloseReason.SESSION_REPLACED.value)


class TestSessionLifecycleV2:
    """v0.20.2 / Bug B — wait_closed, force_close, max_lifetime, peer_dead."""

    @pytest.mark.asyncio()
    async def test_wait_closed_resolves_after_run_finalizes(self) -> None:
        source = FakeAudioInputSource(_tone_frames(0.2, 3), frame_interval_s=0.0)
        sender = _RecordingSender()
        session = TestSession(
            session_id=new_session_id(),
            source=source,
            sender=sender,
            config=_config(frame_rate_hz=200),
        )
        # Before run — wait_closed with a short timeout must report False.
        assert await session.wait_closed(timeout=0.05) is False
        await session.run()
        # After run — wait_closed is immediate True.
        assert await session.wait_closed(timeout=0.01) is True

    @pytest.mark.asyncio()
    async def test_force_close_releases_never_started_session(self) -> None:
        source = FakeAudioInputSource([], frame_interval_s=0.0)
        sender = _RecordingSender()
        session = TestSession(
            session_id=new_session_id(),
            source=source,
            sender=sender,
            config=_config(),
        )
        assert source._closed is False  # noqa: SLF001
        await session.force_close()
        assert source._closed is True  # noqa: SLF001
        # Idempotent — second call is a no-op and does not raise.
        await session.force_close()
        # wait_closed is True after force_close.
        assert await session.wait_closed(timeout=0.01) is True

    @pytest.mark.asyncio()
    async def test_force_close_after_run_is_idempotent(self) -> None:
        source = FakeAudioInputSource(_tone_frames(0.2, 2), frame_interval_s=0.0)
        sender = _RecordingSender()
        session = TestSession(
            session_id=new_session_id(),
            source=source,
            sender=sender,
            config=_config(frame_rate_hz=200),
        )
        await session.run()
        # run has fully finalized; force_close must be a no-op that
        # still flips _closed_event.
        await session.force_close()
        assert await session.wait_closed(timeout=0.01) is True

    @pytest.mark.asyncio()
    async def test_max_lifetime_closes_session(self) -> None:
        # Stream frames forever so only the max_lifetime cap stops us.
        frames = _tone_frames(0.2, 10_000)
        source = FakeAudioInputSource(frames, frame_interval_s=0.005)
        sender = _RecordingSender()
        session = TestSession(
            session_id=new_session_id(),
            source=source,
            sender=sender,
            config=_config(
                frame_rate_hz=200,
                max_lifetime_s=0.05,
                peer_alive_timeout_s=0.0,  # disable peer watchdog
            ),
        )
        await asyncio.wait_for(session.run(), timeout=2.0)
        closed = [p for p in sender.payloads if p["t"] == FrameType.CLOSED.value]
        assert closed
        assert closed[-1]["reason"] == CloseReason.MAX_LIFETIME.value
        # Normal closure code 1000 — server hygiene, not an error.
        assert sender.closed_with is not None
        assert sender.closed_with[0] == 1000

    @pytest.mark.asyncio()
    async def test_peer_dead_closes_session(self) -> None:
        # frame_rate_hz=1 → first send is ~1 s away. peer_alive_timeout_s
        # = 0.05 fires long before the first successful send.
        frames = _tone_frames(0.2, 10_000)
        source = FakeAudioInputSource(frames, frame_interval_s=0.005)
        sender = _RecordingSender()
        session = TestSession(
            session_id=new_session_id(),
            source=source,
            sender=sender,
            config=_config(
                frame_rate_hz=1,
                max_lifetime_s=10.0,  # must not fire
                peer_alive_timeout_s=0.05,
            ),
        )
        await asyncio.wait_for(session.run(), timeout=2.0)
        closed = [p for p in sender.payloads if p["t"] == FrameType.CLOSED.value]
        assert closed
        assert closed[-1]["reason"] == CloseReason.PEER_DEAD.value
        assert sender.closed_with is not None
        assert sender.closed_with[0] == 1000

    @pytest.mark.asyncio()
    async def test_stop_reason_is_idempotent_on_concurrent_calls(self) -> None:
        """First caller wins — second stop doesn't overwrite the reason."""
        source = FakeAudioInputSource(_tone_frames(0.2, 1_000), frame_interval_s=0.01)
        sender = _RecordingSender()
        session = TestSession(
            session_id=new_session_id(),
            source=source,
            sender=sender,
            config=_config(frame_rate_hz=30),
        )
        run_task = asyncio.create_task(session.run())
        await asyncio.sleep(0.02)
        await session.stop(CloseReason.SESSION_REPLACED)
        await session.stop(CloseReason.SERVER_SHUTDOWN)  # must not overwrite
        await asyncio.wait_for(run_task, timeout=2.0)

        closed = [p for p in sender.payloads if p["t"] == FrameType.CLOSED.value]
        assert closed[-1]["reason"] == CloseReason.SESSION_REPLACED.value


# --------------------------------------------------------------------------
# Registry tests
# --------------------------------------------------------------------------


def _fake_session() -> TestSession:
    """Build a session against a no-op source for registry tests."""
    source = FakeAudioInputSource([], frame_interval_s=0.0)
    sender = _RecordingSender()
    return TestSession(
        session_id=new_session_id(),
        source=source,
        sender=sender,
        config=_config(),
    )


class TestSessionRegistry:
    """Registry enforces the per-token cap."""

    def test_zero_max_rejected(self) -> None:
        with pytest.raises(ValueError, match="max_per_token must be > 0"):
            SessionRegistry(max_per_token=0)

    @pytest.mark.asyncio()
    async def test_first_session_has_no_evictions(self) -> None:
        reg = SessionRegistry(max_per_token=1)
        s1 = _fake_session()
        evicted = await reg.register("tok", s1)
        assert evicted == []
        assert await reg.active_count("tok") == 1

    @pytest.mark.asyncio()
    async def test_second_session_evicts_first(self) -> None:
        reg = SessionRegistry(max_per_token=1)
        s1 = _fake_session()
        s2 = _fake_session()
        await reg.register("tok", s1)
        evicted = await reg.register("tok", s2)
        assert evicted == [s1]
        assert await reg.active_count("tok") == 1

    @pytest.mark.asyncio()
    async def test_different_tokens_isolated(self) -> None:
        reg = SessionRegistry(max_per_token=1)
        await reg.register("a", _fake_session())
        await reg.register("b", _fake_session())
        assert await reg.active_count("a") == 1
        assert await reg.active_count("b") == 1

    @pytest.mark.asyncio()
    async def test_unregister_drops_session(self) -> None:
        reg = SessionRegistry(max_per_token=2)
        s1 = _fake_session()
        await reg.register("tok", s1)
        await reg.unregister("tok", s1)
        assert await reg.active_count("tok") == 0

    @pytest.mark.asyncio()
    async def test_max_greater_than_one_keeps_prior(self) -> None:
        reg = SessionRegistry(max_per_token=2)
        s1 = _fake_session()
        s2 = _fake_session()
        s3 = _fake_session()
        await reg.register("tok", s1)
        await reg.register("tok", s2)
        evicted = await reg.register("tok", s3)
        # Only the oldest is evicted.
        assert evicted == [s1]
        assert await reg.active_count("tok") == 2

    @pytest.mark.asyncio()
    async def test_close_all_stops_every_session(self) -> None:
        reg = SessionRegistry(max_per_token=3)
        sessions = [_fake_session() for _ in range(3)]
        for s in sessions:
            await reg.register("tok", s)

        # Each session observes .stop() -> sets internal stop_event.
        await reg.close_all()
        for s in sessions:
            # No public flag, but hitting .stop twice must be idempotent.
            await s.stop()


class TestSessionRegistryV2:
    """v0.20.2 / Bug B — registry waits for source release during eviction."""

    @pytest.mark.asyncio()
    async def test_register_releases_evicted_source_before_returning(self) -> None:
        """Hand-off must leave the old source closed synchronously."""
        reg = SessionRegistry(max_per_token=1, force_close_grace_s=0.1)
        s1 = _fake_session()
        await reg.register("tok", s1)
        # s1 never ran — source is not yet closed.
        assert s1._source._closed is False  # noqa: SLF001

        s2 = _fake_session()
        evicted = await reg.register("tok", s2)

        assert evicted == [s1]
        # Critical invariant: by the time register() returns, the old
        # source is closed, so s2 can open PortAudio on the same
        # endpoint without fighting a live owner.
        assert s1._source._closed is True  # noqa: SLF001

    @pytest.mark.asyncio()
    async def test_register_waits_for_running_session_to_finalize(self) -> None:
        """Running session must finalize before register returns."""
        reg = SessionRegistry(max_per_token=1, force_close_grace_s=1.0)

        frames = _tone_frames(0.2, 1_000)
        running_source = FakeAudioInputSource(frames, frame_interval_s=0.005)
        sender = _RecordingSender()
        s1 = TestSession(
            session_id=new_session_id(),
            source=running_source,
            sender=sender,
            config=_config(frame_rate_hz=60),
        )
        await reg.register("tok", s1)
        run_task = asyncio.create_task(s1.run())
        # Wait for run to actually start streaming.
        await asyncio.sleep(0.05)

        s2 = _fake_session()
        await reg.register("tok", s2)
        # s1's source must be closed AND the run task must be done.
        assert running_source._closed is True  # noqa: SLF001
        await asyncio.wait_for(run_task, timeout=1.0)

        # s1 received a SESSION_REPLACED ClosedFrame.
        closed = [p for p in sender.payloads if p["t"] == FrameType.CLOSED.value]
        assert closed[-1]["reason"] == CloseReason.SESSION_REPLACED.value

    @pytest.mark.asyncio()
    async def test_register_force_closes_stuck_session(self) -> None:
        """On wait_closed timeout, force_close releases the source."""
        reg = SessionRegistry(max_per_token=1, force_close_grace_s=0.05)

        class _SlowSender(WSSender):
            """Sender whose send_json hangs for 5 s — simulates a dead peer."""

            def __init__(self) -> None:
                self.closed_with: tuple[int, str] | None = None

            async def send_json(self, payload: dict[str, object]) -> None:
                await asyncio.sleep(5.0)

            async def close(self, code: int, reason: str) -> None:
                self.closed_with = (code, reason)

        frames = _tone_frames(0.2, 10_000)
        slow_source = FakeAudioInputSource(frames, frame_interval_s=0.001)
        slow_sender = _SlowSender()
        s1 = TestSession(
            session_id=new_session_id(),
            source=slow_source,
            sender=slow_sender,
            config=_config(frame_rate_hz=200, peer_alive_timeout_s=0.0),
        )
        await reg.register("tok", s1)
        run_task = asyncio.create_task(s1.run())
        # Let s1 enter _stream_levels and get stuck in send_json.
        await asyncio.sleep(0.02)

        s2 = _fake_session()
        evicted_at = asyncio.get_running_loop().time()
        await reg.register("tok", s2)
        elapsed = asyncio.get_running_loop().time() - evicted_at

        # register must not wait the full 5 s slow_sender delay —
        # force_close kicks after grace_s (0.05 s) and releases the
        # source so s2 can own the endpoint.
        assert elapsed < 1.0, f"register blocked {elapsed:.2f}s waiting for stuck session"
        assert slow_source._closed is True  # noqa: SLF001

        # Cleanup — cancel the stuck run task so the test doesn't leak.
        run_task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await asyncio.wait_for(run_task, timeout=6.0)

    @pytest.mark.asyncio()
    async def test_close_all_awaits_every_source_release(self) -> None:
        """close_all must synchronously release all sources on return."""
        reg = SessionRegistry(max_per_token=3, force_close_grace_s=0.1)
        sessions = [_fake_session() for _ in range(3)]
        for s in sessions:
            await reg.register("tok", s)

        await reg.close_all(reason=CloseReason.SERVER_SHUTDOWN)
        for s in sessions:
            assert s._source._closed is True  # noqa: SLF001


# --------------------------------------------------------------------------
# Helpers module-level
# --------------------------------------------------------------------------


class TestHelpers:
    """Tiny helpers that ship with the session module."""

    def test_new_session_id_is_hex_and_long(self) -> None:
        sid = new_session_id()
        assert len(sid) == 16
        int(sid, 16)

    def test_new_session_id_unique(self) -> None:
        assert new_session_id() != new_session_id()

    def test_monotonic_ms_nondecreasing(self) -> None:
        a = monotonic_ms()
        b = monotonic_ms()
        assert b >= a
