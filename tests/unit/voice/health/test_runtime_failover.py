"""Tests for ``sovyx.voice.health._runtime_failover``.

Mission anchor:
``docs-internal/missions/MISSION-voice-linux-silent-mic-remediation-2026-05-04.md``
§Phase 2 T2.6.

Pin every leg of :func:`_try_runtime_failover`:

* lenient telemetry mode (gate=False) emits ``voice.failover.attempted``
  but does NOT call ``request_device_change_restart``;
* gate=True happy path dispatches the restart, resets the coordinator,
  emits ``voice.failover.succeeded``;
* no-candidate path emits ``voice.failover.exhausted`` (idempotent);
* max-attempts cap emits ``voice.failover.exhausted`` (idempotent);
* cooldown blocks rapid re-attempts;
* failed restart emits ``voice.failover.failed`` and does NOT reset
  the coordinator latch (so cooldown rate-limits the next retry);
* exception in the helper does not propagate beyond the closure.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from sovyx.engine.config import VoiceTuningConfig
from sovyx.voice.capture._restart import (
    DeviceChangeRestartResult,
    DeviceChangeRestartVerdict,
)
from sovyx.voice.health import _runtime_failover as failover_mod
from sovyx.voice.health._runtime_failover import (
    RuntimeFailoverState,
    _try_runtime_failover,
)

_MIND_ID = "jonny"


def _make_capture_task(
    *,
    active_guid: str = "guid-source-7",
    active_name: str = "default",
    restart_result: DeviceChangeRestartResult | None = None,
) -> MagicMock:
    task = MagicMock()
    task.active_device_guid = active_guid
    task.active_device_name = active_name
    task._input_device = 7
    task._host_api_name = "ALSA"
    if restart_result is None:
        restart_result = DeviceChangeRestartResult(
            verdict=DeviceChangeRestartVerdict.DEVICE_CHANGED_SUCCESS,
            engaged=True,
            target_device_index=4,
            target_host_api="ALSA",
            new_endpoint_guid="guid-target-4",
        )
    task.request_device_change_restart = AsyncMock(return_value=restart_result)
    return task


def _make_pipeline() -> MagicMock:
    pipeline = MagicMock()
    pipeline._config = MagicMock(mind_id=_MIND_ID)
    pipeline.reset_coordinator_after_failover = MagicMock()
    return pipeline


def _make_target_entry(
    index: int = 4,
    name: str = "HD-Audio Generic: SN6180 Analog (hw:1,0)",
):  # type: ignore[no-untyped-def]
    from sovyx.voice.device_enum import DeviceEntry

    return DeviceEntry(
        index=index,
        name=name,
        canonical_name=name.strip().lower()[:30],
        host_api_index=0,
        host_api_name="ALSA",
        max_input_channels=2,
        max_output_channels=2,
        default_samplerate=48_000,
        is_os_default=False,
    )


def _capture_logs(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, dict[str, object]]]:
    """Spy on ``failover_mod.logger`` so caplog limitations don't bite.

    Sovyx routes use structlog ``BoundLoggerLazyProxy`` which bypasses
    stdlib ``logging`` — caplog can't see those events. patch.object
    on the module logger is the deterministic alternative
    (CLAUDE.md anti-pattern #11).
    """
    captured: list[tuple[str, dict[str, object]]] = []
    real = failover_mod.logger

    def _capture(level: str):  # type: ignore[no-untyped-def]
        real_method = getattr(real, level)

        def _spy(event: str, *args: object, **kwargs: object) -> object:
            captured.append((event, dict(kwargs)))
            return real_method(event, *args, **kwargs)

        return _spy

    monkeypatch.setattr(failover_mod.logger, "warning", _capture("warning"))
    monkeypatch.setattr(failover_mod.logger, "error", _capture("error"))
    monkeypatch.setattr(failover_mod.logger, "info", _capture("info"))
    return captured


class TestRuntimeFailoverLenientMode:
    """Gate=False — emit telemetry, never mutate."""

    @pytest.mark.asyncio()
    async def test_emits_attempted_event_but_does_not_dispatch_restart(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        captured = _capture_logs(monkeypatch)
        capture_task = _make_capture_task()
        pipeline = _make_pipeline()
        tuning = VoiceTuningConfig(runtime_failover_on_quarantine_enabled=False)
        state = RuntimeFailoverState()
        target = _make_target_entry()

        with (
            patch.object(
                failover_mod,
                "_resolve_target_safe",
                return_value=(target, 2, None),
            ),
        ):
            await _try_runtime_failover(
                capture_task=capture_task,
                pipeline=pipeline,
                tuning=tuning,
                state=state,
            )

        attempted = [
            (evt, kwargs) for evt, kwargs in captured if evt == "voice.failover.attempted"
        ]
        assert len(attempted) == 1
        _, kwargs = attempted[0]
        assert kwargs["voice.gate_enabled"] is False
        assert kwargs["voice.from_endpoint"] == "guid-source-7"
        assert "SN6180" in kwargs["voice.to_friendly_name"]
        assert kwargs["voice.candidates_remaining"] == 2  # noqa: PLR2004

        # Restart NOT dispatched, coordinator NOT reset, no attempt
        # counter bump.
        capture_task.request_device_change_restart.assert_not_called()
        pipeline.reset_coordinator_after_failover.assert_not_called()
        assert state.attempts == 0

        # No succeeded / failed / exhausted events fired.
        for evt, _ in captured:
            assert evt not in (
                "voice.failover.succeeded",
                "voice.failover.failed",
                "voice.failover.exhausted",
            )


class TestRuntimeFailoverGateEnabled:
    """Gate=True — full dispatch + coordinator reset on success."""

    @pytest.mark.asyncio()
    async def test_dispatches_restart_resets_coordinator_emits_succeeded(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        captured = _capture_logs(monkeypatch)
        capture_task = _make_capture_task()
        pipeline = _make_pipeline()
        tuning = VoiceTuningConfig(runtime_failover_on_quarantine_enabled=True)
        state = RuntimeFailoverState()
        target = _make_target_entry()

        with patch.object(
            failover_mod,
            "_resolve_target_safe",
            return_value=(target, 2, None),
        ):
            await _try_runtime_failover(
                capture_task=capture_task,
                pipeline=pipeline,
                tuning=tuning,
                state=state,
            )

        capture_task.request_device_change_restart.assert_awaited_once()
        call_kwargs = capture_task.request_device_change_restart.call_args
        assert call_kwargs.args[0] is target
        assert call_kwargs.kwargs["reason"] == "endpoint_quarantined"

        pipeline.reset_coordinator_after_failover.assert_called_once()
        assert state.attempts == 1
        assert state.last_attempt_monotonic > 0.0

        succeeded = [
            (evt, kwargs) for evt, kwargs in captured if evt == "voice.failover.succeeded"
        ]
        assert len(succeeded) == 1
        _, kwargs = succeeded[0]
        assert kwargs["voice.from_endpoint"] == "guid-source-7"
        assert kwargs["voice.new_endpoint_guid"] == "guid-target-4"


class TestRuntimeFailoverNoCandidate:
    """Selection returned None — exhausted (idempotent)."""

    @pytest.mark.asyncio()
    async def test_no_candidate_emits_exhausted_and_is_idempotent(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        captured = _capture_logs(monkeypatch)
        capture_task = _make_capture_task()
        pipeline = _make_pipeline()
        tuning = VoiceTuningConfig(runtime_failover_on_quarantine_enabled=True)
        state = RuntimeFailoverState()

        with patch.object(
            failover_mod,
            "_resolve_target_safe",
            return_value=(None, 0, None),
        ):
            # First call → emits exhausted, sets idempotency flag.
            await _try_runtime_failover(
                capture_task=capture_task,
                pipeline=pipeline,
                tuning=tuning,
                state=state,
            )
            # Second call → must NOT re-emit (idempotent).
            await _try_runtime_failover(
                capture_task=capture_task,
                pipeline=pipeline,
                tuning=tuning,
                state=state,
            )

        capture_task.request_device_change_restart.assert_not_called()
        exhausted = [evt for evt, _ in captured if evt == "voice.failover.exhausted"]
        assert len(exhausted) == 1, (
            f"expected exactly one exhausted event (idempotent), got {len(exhausted)}"
        )
        assert state.exhausted_emitted is True


class TestRuntimeFailoverMaxAttempts:
    """Attempts cap reached — exhausted, no further dispatches."""

    @pytest.mark.asyncio()
    async def test_max_attempts_caps_loop_emits_exhausted(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        captured = _capture_logs(monkeypatch)
        capture_task = _make_capture_task()
        pipeline = _make_pipeline()
        tuning = VoiceTuningConfig(
            runtime_failover_on_quarantine_enabled=True,
            max_failover_attempts=2,
            failover_cooldown_s=0.0,  # disable cooldown for this test
        )
        state = RuntimeFailoverState()
        target = _make_target_entry()

        with patch.object(
            failover_mod,
            "_resolve_target_safe",
            return_value=(target, 2, None),
        ):
            # 3 calls — first 2 succeed, third hits the cap.
            for _ in range(3):
                await _try_runtime_failover(
                    capture_task=capture_task,
                    pipeline=pipeline,
                    tuning=tuning,
                    state=state,
                )

        # Only 2 actual restarts dispatched.
        assert capture_task.request_device_change_restart.await_count == 2  # noqa: PLR2004
        exhausted = [
            (evt, kwargs) for evt, kwargs in captured if evt == "voice.failover.exhausted"
        ]
        assert len(exhausted) == 1
        _, kwargs = exhausted[0]
        assert kwargs["voice.cause"] == "max_attempts"
        assert kwargs["voice.attempts"] == 2  # noqa: PLR2004


class TestRuntimeFailoverCooldown:
    """Cooldown blocks rapid re-attempts."""

    @pytest.mark.asyncio()
    async def test_cooldown_blocks_immediate_reattempt(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        captured = _capture_logs(monkeypatch)
        capture_task = _make_capture_task()
        pipeline = _make_pipeline()
        tuning = VoiceTuningConfig(
            runtime_failover_on_quarantine_enabled=True,
            failover_cooldown_s=30.0,
        )
        state = RuntimeFailoverState()
        target = _make_target_entry()

        with patch.object(
            failover_mod,
            "_resolve_target_safe",
            return_value=(target, 2, None),
        ):
            # First call dispatches.
            await _try_runtime_failover(
                capture_task=capture_task,
                pipeline=pipeline,
                tuning=tuning,
                state=state,
            )
            # Immediate second call — cooldown blocks.
            await _try_runtime_failover(
                capture_task=capture_task,
                pipeline=pipeline,
                tuning=tuning,
                state=state,
            )

        # Restart dispatched ONCE (first call).
        capture_task.request_device_change_restart.assert_awaited_once()
        cooldown_blocked = [
            (evt, kwargs) for evt, kwargs in captured if evt == "voice.failover.cooldown_blocked"
        ]
        assert len(cooldown_blocked) == 1
        _, kwargs = cooldown_blocked[0]
        assert kwargs["voice.cooldown_remaining_s"] > 0.0


class TestRuntimeFailoverRestartFailed:
    """Restart returned engaged=False — emit failed, do NOT reset latch."""

    @pytest.mark.asyncio()
    async def test_restart_failed_emits_failed_event_no_coordinator_reset(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        captured = _capture_logs(monkeypatch)
        failed_result = DeviceChangeRestartResult(
            verdict=DeviceChangeRestartVerdict.OPEN_FAILED_NO_STREAM,
            engaged=False,
            target_device_index=4,
            target_host_api="ALSA",
            new_endpoint_guid="guid-target-4",
            detail="target open failed",
        )
        capture_task = _make_capture_task(restart_result=failed_result)
        pipeline = _make_pipeline()
        tuning = VoiceTuningConfig(runtime_failover_on_quarantine_enabled=True)
        state = RuntimeFailoverState()
        target = _make_target_entry()

        with patch.object(
            failover_mod,
            "_resolve_target_safe",
            return_value=(target, 2, None),
        ):
            await _try_runtime_failover(
                capture_task=capture_task,
                pipeline=pipeline,
                tuning=tuning,
                state=state,
            )

        # Restart was attempted but engaged=False → coordinator NOT reset.
        capture_task.request_device_change_restart.assert_awaited_once()
        pipeline.reset_coordinator_after_failover.assert_not_called()

        failed = [(evt, kwargs) for evt, kwargs in captured if evt == "voice.failover.failed"]
        assert len(failed) == 1
        _, kwargs = failed[0]
        assert kwargs["voice.target_endpoint"] != ""


class TestRuntimeFailoverSelectionFailure:
    """Selection raised — emit selection_failed after the lenient event."""

    @pytest.mark.asyncio()
    async def test_selection_exception_emits_selection_failed(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        captured = _capture_logs(monkeypatch)
        capture_task = _make_capture_task()
        pipeline = _make_pipeline()
        tuning = VoiceTuningConfig(runtime_failover_on_quarantine_enabled=True)
        state = RuntimeFailoverState()

        boom = RuntimeError("enumerate_devices crashed")
        with patch.object(
            failover_mod,
            "_resolve_target_safe",
            return_value=(None, 0, boom),
        ):
            await _try_runtime_failover(
                capture_task=capture_task,
                pipeline=pipeline,
                tuning=tuning,
                state=state,
            )

        attempted = [evt for evt, _ in captured if evt == "voice.failover.attempted"]
        selection_failed = [
            (evt, kwargs) for evt, kwargs in captured if evt == "voice.failover.selection_failed"
        ]
        # Lenient telemetry STILL fires before the selection_failed event.
        assert len(attempted) == 1
        assert len(selection_failed) == 1
        _, kwargs = selection_failed[0]
        assert kwargs["voice.error_type"] == "RuntimeError"
        capture_task.request_device_change_restart.assert_not_called()


class TestRuntimeFailoverState:
    """RuntimeFailoverState dataclass contract."""

    def test_default_values(self) -> None:
        state = RuntimeFailoverState()
        assert state.attempts == 0
        assert state.last_attempt_monotonic == 0.0
        assert state.exhausted_emitted is False

    def test_mutable_fields_can_be_updated(self) -> None:
        state = RuntimeFailoverState()
        state.attempts = 5
        state.last_attempt_monotonic = 12345.6
        state.exhausted_emitted = True
        assert state.attempts == 5  # noqa: PLR2004
        assert state.last_attempt_monotonic == 12345.6  # noqa: PLR2004
        assert state.exhausted_emitted is True
