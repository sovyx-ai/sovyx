"""Tests for ``LinuxAudioServiceMonitor._post_up_health_check`` (F2-H04, §3.K).

W2.C1 foundation — the helper isolates the ``pactl info`` round-trip so
the wire-up step (W2.C2) can gate UP-event emission on it. Tests mock
``asyncio.create_subprocess_exec`` so no real subprocess is spawned, and
each pin one branch of the contract:

* rc=0 within 1.0 s → ``True`` (PipeWire / PulseAudio is responsive).
* rc=1 → ``False`` (daemon up but unhappy; defer UP).
* TimeoutError → ``False`` + subprocess killed (no zombie).
* FileNotFoundError (no ``pactl`` binary) → ``False``.
* OSError on spawn → ``False``.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from sovyx.voice.health._audio_service_linux import LinuxAudioServiceMonitor


def _query_stub(_service: str) -> str | None:
    return "active"


def _build_monitor() -> LinuxAudioServiceMonitor:
    return LinuxAudioServiceMonitor(
        services_to_monitor=frozenset({"pipewire.service"}),
        poll_interval_s=2.0,
        query=_query_stub,
    )


class TestPostUpHealthCheck:
    """Pin each branch of the helper's contract."""

    @pytest.mark.asyncio()
    async def test_rc_zero_returns_true(self) -> None:
        """``pactl info`` exit 0 → daemon is responsive."""
        proc = MagicMock()
        proc.wait = AsyncMock(return_value=0)
        proc.kill = MagicMock()
        with patch(
            "asyncio.create_subprocess_exec",
            new=AsyncMock(return_value=proc),
        ) as spawn_mock:
            monitor = _build_monitor()
            result = await monitor._post_up_health_check()
        assert result is True
        spawn_mock.assert_awaited_once()
        # ``proc.kill`` MUST NOT have been called on the happy path.
        proc.kill.assert_not_called()

    @pytest.mark.asyncio()
    async def test_rc_nonzero_returns_false(self) -> None:
        """``pactl info`` non-zero exit → daemon up but unhappy; defer UP."""
        proc = MagicMock()
        proc.wait = AsyncMock(return_value=1)
        with patch(
            "asyncio.create_subprocess_exec",
            new=AsyncMock(return_value=proc),
        ):
            monitor = _build_monitor()
            result = await monitor._post_up_health_check()
        assert result is False

    @pytest.mark.asyncio()
    async def test_timeout_returns_false_and_kills_subprocess(self) -> None:
        """1 s ceiling enforced; timed-out subprocess MUST be killed."""
        proc = MagicMock()
        # proc.wait() awaited only inside the cleanup block (after the
        # kill); patched asyncio.wait_for raises before that point.
        proc.wait = AsyncMock(return_value=0)
        proc.kill = MagicMock()
        with (
            patch(
                "asyncio.create_subprocess_exec",
                new=AsyncMock(return_value=proc),
            ),
            patch(
                "sovyx.voice.health._audio_service_linux.asyncio.wait_for",
                new=AsyncMock(side_effect=TimeoutError()),
            ),
        ):
            monitor = _build_monitor()
            result = await monitor._post_up_health_check()
        assert result is False
        proc.kill.assert_called_once()

    @pytest.mark.asyncio()
    async def test_missing_pactl_returns_false(self) -> None:
        """``pactl`` not on PATH → ``False`` (no zombie cleanup needed)."""
        with patch(
            "asyncio.create_subprocess_exec",
            new=AsyncMock(side_effect=FileNotFoundError("pactl: not found")),
        ):
            monitor = _build_monitor()
            result = await monitor._post_up_health_check()
        assert result is False

    @pytest.mark.asyncio()
    async def test_oserror_on_spawn_returns_false(self) -> None:
        """Spawn-level OSError → ``False`` (e.g. ENOMEM, EPERM)."""
        with patch(
            "asyncio.create_subprocess_exec",
            new=AsyncMock(side_effect=OSError("transient")),
        ):
            monitor = _build_monitor()
            result = await monitor._post_up_health_check()
        assert result is False

    @pytest.mark.asyncio()
    async def test_helper_does_not_raise_on_any_branch(self) -> None:
        """Closure check: helper MUST swallow every error path.

        Per audit §3.K the helper sits on the hot UP-event path; a
        leaked exception would crash the monitor's poll loop and force
        a watchdog restart. Defensive contract: every branch returns
        bool, never raises.
        """
        for side_effect in (FileNotFoundError(), OSError(), PermissionError()):
            with patch(
                "asyncio.create_subprocess_exec",
                new=AsyncMock(side_effect=side_effect),
            ):
                monitor = _build_monitor()
                # MUST NOT raise.
                result = await monitor._post_up_health_check()
                assert result is False
