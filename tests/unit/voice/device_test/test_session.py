"""Tests for :class:`TestSession` lifecycle and :class:`SessionRegistry`."""

from __future__ import annotations

import asyncio
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
