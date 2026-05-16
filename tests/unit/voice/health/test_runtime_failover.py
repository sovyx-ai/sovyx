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
    canonical_name: str | None = None,
):  # type: ignore[no-untyped-def]
    from sovyx.voice.device_enum import DeviceEntry

    return DeviceEntry(
        index=index,
        name=name,
        canonical_name=canonical_name or name.strip().lower()[:30],
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
        # Mission C3 §T1.6: with the loop-in-place refactor, an
        # engaged=False outcome would otherwise cause the loop to
        # iterate to a second candidate (via ``_resolve_target_safe``).
        # The mock here returns the SAME ``target`` every call, so the
        # ladder's defensive ``target_key in attempted`` guard catches
        # the duplicate on iteration 2 and breaks. The
        # ``failover_intra_ladder_cooldown_s=0.0`` override keeps the
        # test fast — without it the loop would sleep 2 s between the
        # failed iteration 1 and the (immediately-breaking) iteration
        # 2, padding test runtime without changing the assertions.
        tuning = VoiceTuningConfig(
            runtime_failover_on_quarantine_enabled=True,
            failover_intra_ladder_cooldown_s=0.0,
        )
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
        # Mission C3 §T1.6: the defensive duplicate-guard breaks the
        # loop on iteration 2 (mock returns same target), so the
        # dispatch fires exactly once.
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
        # Mission C3 §T1.1 — new ladder-level state fields.
        assert state.ladder_id == ""
        assert state.ladder_exhausted is False
        assert state.last_ladder_complete_monotonic == 0.0
        assert state.last_candidates_unreachable == []

    def test_mutable_fields_can_be_updated(self) -> None:
        state = RuntimeFailoverState()
        state.attempts = 5
        state.last_attempt_monotonic = 12345.6
        state.exhausted_emitted = True
        state.ladder_id = "abc123def456"
        state.ladder_exhausted = True
        state.last_ladder_complete_monotonic = 99999.9
        state.last_candidates_unreachable = ["dev_a", "dev_b"]
        assert state.attempts == 5  # noqa: PLR2004
        assert state.last_attempt_monotonic == 12345.6  # noqa: PLR2004
        assert state.exhausted_emitted is True
        assert state.ladder_id == "abc123def456"
        assert state.ladder_exhausted is True
        assert state.last_ladder_complete_monotonic == 99999.9  # noqa: PLR2004
        assert state.last_candidates_unreachable == ["dev_a", "dev_b"]


# ─────────────────────────────────────────────────────────────────────
# Mission C3 §T1.6 — loop-in-place candidate iteration tests
# ─────────────────────────────────────────────────────────────────────


class TestRuntimeFailoverLadderIteration:
    """Mission C3 §T1.6 — bounded loop-in-place over candidates.

    Closes the v0.43.1 operator-log L1015 → L1063 collapse: pre-Mission
    C3 the single-shot dispatch left ``candidates_remaining = 2``
    stranded after the first candidate failed; the loop refactor
    iterates the full set within one closure invocation.
    """

    @pytest.mark.asyncio()
    async def test_first_candidate_fails_second_succeeds_loop_iterates(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Loop iterates: candidate 1 engaged=False → candidate 2
        engaged=True → ladder_complete{succeeded, succeeded_index=1}.

        This is the canonical multi-candidate cascade the v0.43.1
        operator session would have hit if the refactor had been in
        place: idx=4 (HD-Audio Generic, AlsaOpen failed) → idx=7
        (PipeWire, would have engaged).
        """
        captured = _capture_logs(monkeypatch)
        capture_task = _make_capture_task()
        pipeline = _make_pipeline()
        tuning = VoiceTuningConfig(
            runtime_failover_on_quarantine_enabled=True,
            failover_intra_ladder_cooldown_s=0.0,
        )
        state = RuntimeFailoverState()

        candidate_1 = _make_target_entry(
            index=4,
            name="HD-Audio Generic",
            canonical_name="hd-audio-generic-idx4",
        )
        candidate_2 = _make_target_entry(
            index=7,
            name="PipeWire",
            canonical_name="pipewire-idx7",
        )

        # First dispatch fails; second engages.
        failed_result = DeviceChangeRestartResult(
            verdict=DeviceChangeRestartVerdict.OPEN_FAILED_NO_STREAM,
            engaged=False,
            target_device_index=4,
            target_host_api="ALSA",
            new_endpoint_guid="guid-target-4",
            detail="AlsaOpen failed",
        )
        success_result = DeviceChangeRestartResult(
            verdict=DeviceChangeRestartVerdict.DEVICE_CHANGED_SUCCESS,
            engaged=True,
            target_device_index=7,
            target_host_api="ALSA",
            new_endpoint_guid="guid-target-7",
        )
        capture_task.request_device_change_restart = AsyncMock(
            side_effect=[failed_result, success_result],
        )

        # _resolve_target_safe returns candidate_1 first, then candidate_2.
        resolve_side_effect = [
            (candidate_1, 3, None),  # pre-loop step 1 resolve
            (candidate_2, 2, None),  # iteration 1's re-resolve
        ]
        with patch.object(
            failover_mod,
            "_resolve_target_safe",
            side_effect=resolve_side_effect,
        ):
            await _try_runtime_failover(
                capture_task=capture_task,
                pipeline=pipeline,
                tuning=tuning,
                state=state,
            )

        # 2 dispatches happened — the loop iterated.
        assert capture_task.request_device_change_restart.await_count == 2  # noqa: PLR2004
        # Coordinator reset on candidate 2 success.
        pipeline.reset_coordinator_after_failover.assert_called_once()
        # state.attempts bumped exactly ONCE (per-ladder semantic).
        assert state.attempts == 1
        # ladder_id is set.
        assert state.ladder_id != ""
        # ladder_exhausted is False after success.
        assert state.ladder_exhausted is False

        # Events emitted in the right order.
        event_names = [evt for evt, _ in captured]
        assert "voice.failover.attempted" in event_names
        assert "voice.failover.ladder_started" in event_names
        # 2 candidate_attempted events.
        candidate_attempted = [
            kwargs for evt, kwargs in captured if evt == "voice.failover.candidate_attempted"
        ]
        assert len(candidate_attempted) == 2  # noqa: PLR2004
        # 1 candidate_failed event (the first).
        candidate_failed = [
            kwargs for evt, kwargs in captured if evt == "voice.failover.candidate_failed"
        ]
        assert len(candidate_failed) == 1
        # 1 succeeded event (legacy preserved).
        succeeded = [evt for evt, _ in captured if evt == "voice.failover.succeeded"]
        assert len(succeeded) == 1
        # 1 ladder_complete event with succeeded_index=1.
        ladder_complete = [
            kwargs for evt, kwargs in captured if evt == "voice.failover.ladder_complete"
        ]
        assert len(ladder_complete) == 1
        assert ladder_complete[0]["voice.verdict"] == "succeeded"
        assert ladder_complete[0]["voice.succeeded_index"] == 1
        # NO failed event (loop exited via success).
        assert "voice.failover.failed" not in event_names

    @pytest.mark.asyncio()
    async def test_all_candidates_fail_emits_ladder_exhausted(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """3 candidates dispatched, all return engaged=False → loop
        exhausts; emits legacy ``voice.failover.failed`` + new
        ``voice.failover.ladder_complete{verdict=exhausted}``.
        """
        captured = _capture_logs(monkeypatch)
        capture_task = _make_capture_task()
        pipeline = _make_pipeline()
        tuning = VoiceTuningConfig(
            runtime_failover_on_quarantine_enabled=True,
            failover_intra_ladder_cooldown_s=0.0,
            failover_candidate_max_attempts_per_ladder=5,
        )
        state = RuntimeFailoverState()

        candidates = [
            _make_target_entry(index=i, name=f"dev_{i}", canonical_name=f"dev-{i}-canonical")
            for i in (4, 7, 8)
        ]
        failed_result = DeviceChangeRestartResult(
            verdict=DeviceChangeRestartVerdict.OPEN_FAILED_NO_STREAM,
            engaged=False,
            target_device_index=99,
            target_host_api="ALSA",
            new_endpoint_guid="guid-failed",
            detail="all dispatches fail",
        )
        capture_task.request_device_change_restart = AsyncMock(return_value=failed_result)

        resolve_side_effect = [
            (candidates[0], 3, None),  # pre-loop
            (candidates[1], 2, None),  # iter 1
            (candidates[2], 1, None),  # iter 2
            (None, 0, None),  # iter 3 — exhausted
        ]
        with patch.object(
            failover_mod,
            "_resolve_target_safe",
            side_effect=resolve_side_effect,
        ):
            await _try_runtime_failover(
                capture_task=capture_task,
                pipeline=pipeline,
                tuning=tuning,
                state=state,
            )

        # 3 dispatches happened (all candidates tried).
        assert capture_task.request_device_change_restart.await_count == 3  # noqa: PLR2004
        # Coordinator NOT reset (no success).
        pipeline.reset_coordinator_after_failover.assert_not_called()
        # ladder_exhausted flag set on state.
        assert state.ladder_exhausted is True
        # last_candidates_unreachable populated.
        assert len(state.last_candidates_unreachable) == 3  # noqa: PLR2004

        event_names = [evt for evt, _ in captured]
        # Legacy failed event preserved.
        assert "voice.failover.failed" in event_names
        # ladder_complete with verdict=exhausted.
        ladder_complete = [
            kwargs for evt, kwargs in captured if evt == "voice.failover.ladder_complete"
        ]
        assert len(ladder_complete) == 1
        assert ladder_complete[0]["voice.verdict"] == "exhausted"
        assert ladder_complete[0]["voice.succeeded_index"] is None
        assert ladder_complete[0]["voice.candidates_tried"] == 3  # noqa: PLR2004

    @pytest.mark.asyncio()
    async def test_intra_ladder_cooldown_separates_dispatches(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """``failover_intra_ladder_cooldown_s`` introduces an
        ``asyncio.sleep`` between dispatches in the SAME ladder; the
        first dispatch is NOT delayed.
        """
        captured = _capture_logs(monkeypatch)
        capture_task = _make_capture_task()
        pipeline = _make_pipeline()
        tuning = VoiceTuningConfig(
            runtime_failover_on_quarantine_enabled=True,
            failover_intra_ladder_cooldown_s=0.05,  # 50 ms for test budget
            failover_candidate_max_attempts_per_ladder=3,
        )
        state = RuntimeFailoverState()

        candidates = [
            _make_target_entry(index=i, name=f"dev_{i}", canonical_name=f"dev-{i}-canonical")
            for i in (4, 7)
        ]
        failed_result = DeviceChangeRestartResult(
            verdict=DeviceChangeRestartVerdict.OPEN_FAILED_NO_STREAM,
            engaged=False,
            target_device_index=99,
            target_host_api="ALSA",
            new_endpoint_guid="g",
        )
        capture_task.request_device_change_restart = AsyncMock(return_value=failed_result)

        sleep_calls: list[float] = []
        real_sleep = failover_mod.asyncio.sleep

        async def _spy_sleep(delay: float) -> None:
            sleep_calls.append(delay)
            await real_sleep(0)  # yield to event loop without burning wall-clock

        monkeypatch.setattr(failover_mod.asyncio, "sleep", _spy_sleep)

        resolve_side_effect = [
            (candidates[0], 2, None),
            (candidates[1], 1, None),
            (None, 0, None),
        ]
        with patch.object(
            failover_mod,
            "_resolve_target_safe",
            side_effect=resolve_side_effect,
        ):
            await _try_runtime_failover(
                capture_task=capture_task,
                pipeline=pipeline,
                tuning=tuning,
                state=state,
            )

        # asyncio.sleep called exactly once (between dispatch 1 and 2),
        # with the configured intra-ladder cooldown value.
        assert sleep_calls == [0.05]
        # 2 dispatches happened.
        assert capture_task.request_device_change_restart.await_count == 2  # noqa: PLR2004

        # captured asserted as a side-quality check — silence ruff F841.
        assert captured  # at least one event was logged

    @pytest.mark.asyncio()
    async def test_candidate_max_attempts_per_ladder_caps_iteration(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """``failover_candidate_max_attempts_per_ladder`` hard-caps the
        loop. With cap=2 and 4 healthy candidates available, only 2
        are dispatched.
        """
        captured = _capture_logs(monkeypatch)
        capture_task = _make_capture_task()
        pipeline = _make_pipeline()
        tuning = VoiceTuningConfig(
            runtime_failover_on_quarantine_enabled=True,
            failover_intra_ladder_cooldown_s=0.0,
            failover_candidate_max_attempts_per_ladder=2,
        )
        state = RuntimeFailoverState()

        # 4 candidates available, all engaged=False (so loop wants to
        # try them all, but the cap should stop at 2).
        candidates = [
            _make_target_entry(index=i, name=f"dev_{i}", canonical_name=f"dev-{i}-canonical")
            for i in (4, 7, 8, 9)
        ]
        failed_result = DeviceChangeRestartResult(
            verdict=DeviceChangeRestartVerdict.OPEN_FAILED_NO_STREAM,
            engaged=False,
            target_device_index=99,
            target_host_api="ALSA",
            new_endpoint_guid="g",
        )
        capture_task.request_device_change_restart = AsyncMock(return_value=failed_result)

        resolve_side_effect = [
            (candidates[0], 4, None),  # pre-loop
            (candidates[1], 3, None),  # iter 1
            (candidates[2], 2, None),  # iter 2 — but cap should prevent dispatch
            (candidates[3], 1, None),
        ]
        with patch.object(
            failover_mod,
            "_resolve_target_safe",
            side_effect=resolve_side_effect,
        ):
            await _try_runtime_failover(
                capture_task=capture_task,
                pipeline=pipeline,
                tuning=tuning,
                state=state,
            )

        # Cap=2 → exactly 2 dispatches.
        assert capture_task.request_device_change_restart.await_count == 2  # noqa: PLR2004
        # Ladder verdict is exhausted.
        ladder_complete = [
            kwargs for evt, kwargs in captured if evt == "voice.failover.ladder_complete"
        ]
        assert len(ladder_complete) == 1
        assert ladder_complete[0]["voice.verdict"] == "exhausted"
        assert ladder_complete[0]["voice.candidates_tried"] == 2  # noqa: PLR2004

    @pytest.mark.asyncio()
    async def test_exception_during_dispatch_continues_to_next_candidate(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """``request_device_change_restart`` raising MUST NOT crash the
        ladder; the loop logs ``candidate_failed{verdict=exception}``
        and proceeds to the next candidate.
        """
        captured = _capture_logs(monkeypatch)
        capture_task = _make_capture_task()
        pipeline = _make_pipeline()
        tuning = VoiceTuningConfig(
            runtime_failover_on_quarantine_enabled=True,
            failover_intra_ladder_cooldown_s=0.0,
            failover_candidate_max_attempts_per_ladder=3,
        )
        state = RuntimeFailoverState()

        candidates = [
            _make_target_entry(index=i, name=f"dev_{i}", canonical_name=f"dev-{i}-canonical")
            for i in (4, 7)
        ]
        success_result = DeviceChangeRestartResult(
            verdict=DeviceChangeRestartVerdict.DEVICE_CHANGED_SUCCESS,
            engaged=True,
            target_device_index=7,
            target_host_api="ALSA",
            new_endpoint_guid="guid-target-7",
        )

        # First dispatch raises; second engages.
        capture_task.request_device_change_restart = AsyncMock(
            side_effect=[RuntimeError("PortAudio crashed"), success_result],
        )

        resolve_side_effect = [
            (candidates[0], 2, None),
            (candidates[1], 1, None),
        ]
        with patch.object(
            failover_mod,
            "_resolve_target_safe",
            side_effect=resolve_side_effect,
        ):
            await _try_runtime_failover(
                capture_task=capture_task,
                pipeline=pipeline,
                tuning=tuning,
                state=state,
            )

        # 2 dispatches attempted — exception did not abort loop.
        assert capture_task.request_device_change_restart.await_count == 2  # noqa: PLR2004
        # Second succeeded → coordinator reset.
        pipeline.reset_coordinator_after_failover.assert_called_once()

        # candidate_failed{verdict=exception} fired for iter 0.
        candidate_failed = [
            kwargs for evt, kwargs in captured if evt == "voice.failover.candidate_failed"
        ]
        assert len(candidate_failed) == 1
        assert candidate_failed[0]["voice.verdict"] == "exception"
        assert candidate_failed[0]["voice.error_type"] == "RuntimeError"

        # ladder_complete with verdict=succeeded.
        ladder_complete = [
            kwargs for evt, kwargs in captured if evt == "voice.failover.ladder_complete"
        ]
        assert ladder_complete[0]["voice.verdict"] == "succeeded"

    @pytest.mark.asyncio()
    async def test_ladder_in_progress_flag_set_and_cleared(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """``pipeline._failover_ladder_in_progress`` MUST be True
        during the loop body and False after — verified via spy
        capturing attribute writes. Phase 2 §T2.5 reader gates the
        orchestrator's frame-drop emission on this flag.
        """
        _capture_logs(monkeypatch)
        capture_task = _make_capture_task()

        # Build a pipeline that records every setattr to
        # ``_failover_ladder_in_progress`` so we can assert the
        # set→clear sequence.
        flag_writes: list[bool] = []

        class _SpyPipeline:
            def __init__(self) -> None:
                self._config = MagicMock(mind_id=_MIND_ID)
                self._current_mind_id = _MIND_ID
                self._failover_ladder_in_progress = False
                self._reset_calls = 0

            def __setattr__(self, name: str, value: object) -> None:  # noqa: D401
                if name == "_failover_ladder_in_progress" and isinstance(value, bool):
                    flag_writes.append(value)
                object.__setattr__(self, name, value)

            def reset_coordinator_after_failover(self) -> None:
                self._reset_calls += 1

        pipeline = _SpyPipeline()
        tuning = VoiceTuningConfig(
            runtime_failover_on_quarantine_enabled=True,
            failover_intra_ladder_cooldown_s=0.0,
        )
        state = RuntimeFailoverState()
        candidate = _make_target_entry(index=4, canonical_name="d-4")
        success_result = DeviceChangeRestartResult(
            verdict=DeviceChangeRestartVerdict.DEVICE_CHANGED_SUCCESS,
            engaged=True,
            target_device_index=4,
            target_host_api="ALSA",
            new_endpoint_guid="g",
        )
        capture_task.request_device_change_restart = AsyncMock(return_value=success_result)

        with patch.object(
            failover_mod,
            "_resolve_target_safe",
            return_value=(candidate, 2, None),
        ):
            await _try_runtime_failover(
                capture_task=capture_task,
                pipeline=pipeline,
                tuning=tuning,
                state=state,
            )

        # Flag writes: initial False (from __init__), then True (enter),
        # then False (try/finally exit).
        assert flag_writes[-2:] == [True, False]
        # And after the closure, the attribute is False.
        assert pipeline._failover_ladder_in_progress is False

    @pytest.mark.asyncio()
    async def test_ladder_in_progress_cleared_on_exception(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Even if the loop body raises (e.g. an unexpected attribute
        access on the pipeline), the try/finally MUST clear
        ``_failover_ladder_in_progress=False``. Without this, future
        frame-drop suppression (Phase 2 §T2.5) would be permanently
        ON, silently swallowing real frame drops.
        """
        _capture_logs(monkeypatch)
        capture_task = _make_capture_task()

        # Pipeline that crashes when reset_coordinator_after_failover
        # is called — exercises the post-success exception path.
        flag_writes: list[bool] = []

        class _CrashPipeline:
            def __init__(self) -> None:
                self._config = MagicMock(mind_id=_MIND_ID)
                self._current_mind_id = _MIND_ID
                self._failover_ladder_in_progress = False

            def __setattr__(self, name: str, value: object) -> None:
                if name == "_failover_ladder_in_progress" and isinstance(value, bool):
                    flag_writes.append(value)
                object.__setattr__(self, name, value)

            def reset_coordinator_after_failover(self) -> None:
                # Best-effort coordinator reset — the loop handles
                # this exception via logger.warning + continues.
                msg = "coordinator reset broke unexpectedly"
                raise RuntimeError(msg)

        pipeline = _CrashPipeline()
        tuning = VoiceTuningConfig(
            runtime_failover_on_quarantine_enabled=True,
            failover_intra_ladder_cooldown_s=0.0,
        )
        state = RuntimeFailoverState()
        candidate = _make_target_entry(index=4, canonical_name="d-4")
        success_result = DeviceChangeRestartResult(
            verdict=DeviceChangeRestartVerdict.DEVICE_CHANGED_SUCCESS,
            engaged=True,
            target_device_index=4,
            target_host_api="ALSA",
            new_endpoint_guid="g",
        )
        capture_task.request_device_change_restart = AsyncMock(return_value=success_result)

        with patch.object(
            failover_mod,
            "_resolve_target_safe",
            return_value=(candidate, 2, None),
        ):
            # Closure MUST NOT propagate the reset_coordinator
            # exception; the existing helper logs + continues.
            await _try_runtime_failover(
                capture_task=capture_task,
                pipeline=pipeline,
                tuning=tuning,
                state=state,
            )

        # Flag was True during the loop, cleared to False in finally.
        assert flag_writes[-1] is False
        assert pipeline._failover_ladder_in_progress is False

    @pytest.mark.asyncio()
    async def test_outer_cooldown_still_applies_across_ladder_runs(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The outer ``failover_cooldown_s`` still gates SEPARATE
        ladder invocations (cross-deaf-signal-heartbeat). Within a
        ladder, the intra-cooldown is the only gate; across ladder
        invocations, the outer cooldown holds.
        """
        captured = _capture_logs(monkeypatch)
        capture_task = _make_capture_task()
        pipeline = _make_pipeline()
        tuning = VoiceTuningConfig(
            runtime_failover_on_quarantine_enabled=True,
            failover_intra_ladder_cooldown_s=0.0,
            failover_cooldown_s=30.0,
        )
        state = RuntimeFailoverState()
        candidate = _make_target_entry(index=4, canonical_name="d-4")
        success_result = DeviceChangeRestartResult(
            verdict=DeviceChangeRestartVerdict.DEVICE_CHANGED_SUCCESS,
            engaged=True,
            target_device_index=4,
            target_host_api="ALSA",
            new_endpoint_guid="g",
        )
        capture_task.request_device_change_restart = AsyncMock(return_value=success_result)

        with patch.object(
            failover_mod,
            "_resolve_target_safe",
            return_value=(candidate, 2, None),
        ):
            # First ladder invocation — dispatches.
            await _try_runtime_failover(
                capture_task=capture_task,
                pipeline=pipeline,
                tuning=tuning,
                state=state,
            )
            # Immediate second invocation — outer cooldown blocks.
            await _try_runtime_failover(
                capture_task=capture_task,
                pipeline=pipeline,
                tuning=tuning,
                state=state,
            )

        # Exactly 1 dispatch (second call blocked by outer cooldown).
        assert capture_task.request_device_change_restart.await_count == 1
        cooldown_blocked = [evt for evt, _ in captured if evt == "voice.failover.cooldown_blocked"]
        assert len(cooldown_blocked) == 1

    @pytest.mark.asyncio()
    async def test_attempted_in_this_ladder_passes_exclusions_to_resolver(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The loop threads ``attempted_in_this_ladder`` into
        ``_resolve_target_safe`` via the ``additional_excluded_guids``
        keyword, so the resolver sees the running per-ladder exclusion
        set.
        """
        _capture_logs(monkeypatch)
        capture_task = _make_capture_task()
        pipeline = _make_pipeline()
        tuning = VoiceTuningConfig(
            runtime_failover_on_quarantine_enabled=True,
            failover_intra_ladder_cooldown_s=0.0,
            failover_candidate_max_attempts_per_ladder=3,
        )
        state = RuntimeFailoverState()

        candidates = [
            _make_target_entry(index=i, name=f"dev_{i}", canonical_name=f"dev-{i}-can")
            for i in (4, 7, 8)
        ]
        failed_result = DeviceChangeRestartResult(
            verdict=DeviceChangeRestartVerdict.OPEN_FAILED_NO_STREAM,
            engaged=False,
            target_device_index=99,
            target_host_api="ALSA",
            new_endpoint_guid="g",
        )
        capture_task.request_device_change_restart = AsyncMock(return_value=failed_result)

        # Spy on _resolve_target_safe to capture the exclusion-set arg
        # on every call.
        resolver_calls: list[frozenset[str]] = []

        def _spy(*, capture_task: object, additional_excluded_guids: frozenset[str] = frozenset()):
            resolver_calls.append(additional_excluded_guids)
            idx = len(resolver_calls) - 1
            if idx < len(candidates):
                return (candidates[idx], 3 - idx, None)
            return (None, 0, None)

        monkeypatch.setattr(failover_mod, "_resolve_target_safe", _spy)

        await _try_runtime_failover(
            capture_task=capture_task,
            pipeline=pipeline,
            tuning=tuning,
            state=state,
        )

        # First call (pre-loop): exclusion set EMPTY.
        assert resolver_calls[0] == frozenset()
        # Second call (iter 1): exclusion set contains candidate 0's canonical.
        assert "dev-4-can" in resolver_calls[1]
        # Third call (iter 2): exclusion contains candidates 0 + 1.
        assert "dev-4-can" in resolver_calls[2]
        assert "dev-7-can" in resolver_calls[2]


# ─────────────────────────────────────────────────────────────────────
# Mission C3 §T2.4 — probe-result cache short-circuit + classify_error_code
# ─────────────────────────────────────────────────────────────────────


class TestRuntimeFailoverCacheShortCircuit:
    """Mission C3 §T2.4 — cache-driven candidate skip + record-on-outcome.

    The loop body MUST:
    1. Consult ``ProbeResultCache.is_known_unopenable`` BEFORE dispatch;
       on hit, emit ``voice.failover.candidate_skipped`` + advance the
       loop WITHOUT calling ``request_device_change_restart``.
    2. Record every dispatch outcome (success / fail / exception) into
       the cache via ``record_probe`` / ``record_success``.
    3. Emit a real ``FailoverErrorClass`` value on
       ``voice.failover.candidate_failed`` (no more ``"unknown"``
       hardcode).
    """

    @pytest.mark.asyncio()
    async def test_cache_unopenable_candidate_emits_skipped_and_advances(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A candidate flagged in the cache MUST be skipped without
        a dispatch; the loop emits ``candidate_skipped`` + advances
        to the next candidate.
        """
        from sovyx.voice.health._probe_result_cache import (
            ProbeResultEntry,
            get_default_probe_result_cache,
            reset_default_probe_result_cache,
        )

        # Fresh cache per test — anti-pattern singletons require explicit
        # reset between tests.
        reset_default_probe_result_cache()
        cache = get_default_probe_result_cache()

        captured = _capture_logs(monkeypatch)
        capture_task = _make_capture_task()
        pipeline = _make_pipeline()
        tuning = VoiceTuningConfig(
            runtime_failover_on_quarantine_enabled=True,
            failover_intra_ladder_cooldown_s=0.0,
            failover_candidate_max_attempts_per_ladder=3,
        )
        state = RuntimeFailoverState()

        # Candidate 1 is cache-flagged (UNOPENABLE_THIS_BOOT via -9985).
        # Candidate 2 is healthy.
        dead_candidate = _make_target_entry(
            index=4,
            name="dev_dead",
            canonical_name="dead-canonical",
        )
        good_candidate = _make_target_entry(
            index=7,
            name="dev_good",
            canonical_name="good-canonical",
        )

        cache.record_probe(
            ProbeResultEntry(
                endpoint_guid="dead-canonical",
                host_api="ALSA",
                verdict="",
                error_code="-9985",
            ),
        )

        success_result = DeviceChangeRestartResult(
            verdict=DeviceChangeRestartVerdict.DEVICE_CHANGED_SUCCESS,
            engaged=True,
            target_device_index=7,
            target_host_api="ALSA",
            new_endpoint_guid="g",
        )
        capture_task.request_device_change_restart = AsyncMock(return_value=success_result)

        # _resolve_target_safe returns dead candidate first, then good.
        resolve_side_effect = [
            (dead_candidate, 2, None),
            (good_candidate, 1, None),
        ]
        with patch.object(
            failover_mod,
            "_resolve_target_safe",
            side_effect=resolve_side_effect,
        ):
            await _try_runtime_failover(
                capture_task=capture_task,
                pipeline=pipeline,
                tuning=tuning,
                state=state,
            )

        # Dead candidate was SKIPPED — only 1 dispatch happened (for
        # the good candidate).
        assert capture_task.request_device_change_restart.await_count == 1

        # candidate_skipped event fired for the dead candidate.
        skipped = [kwargs for evt, kwargs in captured if evt == "voice.failover.candidate_skipped"]
        assert len(skipped) == 1
        assert skipped[0]["voice.target_endpoint"] == "dead-canonical"
        assert skipped[0]["voice.cached_error_code"] == "-9985"
        assert skipped[0]["voice.reason"] == "probe_cache_unopenable"

        # Cleanup — reset cache for subsequent tests.
        reset_default_probe_result_cache()

    @pytest.mark.asyncio()
    async def test_failed_dispatch_records_probe_with_classified_error(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """On engaged=False with a classifiable error_code, the loop
        emits ``candidate_failed{error_class=<classified>}`` AND
        writes the entry to the cache.
        """
        from sovyx.voice.health._probe_result_cache import (
            get_default_probe_result_cache,
            reset_default_probe_result_cache,
        )

        reset_default_probe_result_cache()
        cache = get_default_probe_result_cache()
        assert len(cache) == 0

        captured = _capture_logs(monkeypatch)
        capture_task = _make_capture_task()
        pipeline = _make_pipeline()
        tuning = VoiceTuningConfig(
            runtime_failover_on_quarantine_enabled=True,
            failover_intra_ladder_cooldown_s=0.0,
            failover_candidate_max_attempts_per_ladder=2,
        )
        state = RuntimeFailoverState()
        candidate = _make_target_entry(
            index=4,
            name="dev_fail",
            canonical_name="fail-canonical",
        )

        # Build a result whose ``detail`` text classifies to
        # UNOPENABLE_THIS_BOOT via the classifier's detail-string
        # fallback. ``DeviceChangeRestartResult`` does not expose a
        # raw PortAudio code field; the runtime reads ``error_code``
        # via ``getattr(result, "error_code", "")`` defensively, so we
        # surface the canonical -9985 signature through ``detail``
        # ("device unavailable" matches the classifier's detail fallback).
        failed_result = DeviceChangeRestartResult(
            verdict=DeviceChangeRestartVerdict.OPEN_FAILED_NO_STREAM,
            engaged=False,
            target_device_index=4,
            target_host_api="ALSA",
            new_endpoint_guid="g",
            detail="AlsaOpen failed: device unavailable",
        )
        capture_task.request_device_change_restart = AsyncMock(return_value=failed_result)

        with patch.object(
            failover_mod,
            "_resolve_target_safe",
            return_value=(candidate, 1, None),
        ):
            await _try_runtime_failover(
                capture_task=capture_task,
                pipeline=pipeline,
                tuning=tuning,
                state=state,
            )

        # candidate_failed event carries the real classified error_class.
        candidate_failed = [
            kwargs for evt, kwargs in captured if evt == "voice.failover.candidate_failed"
        ]
        assert len(candidate_failed) >= 1
        assert candidate_failed[0]["voice.error_class"] == "unopenable_this_boot"
        assert "device unavailable" in candidate_failed[0]["voice.error_detail"]

        # Cache populated with the failed entry. The runtime reads
        # ``error_code`` defensively via getattr; the dataclass has no
        # such field, so the recorded ``error_code`` is the empty
        # string — but the detail captures the canonical signature.
        entry = cache.lookup("fail-canonical", "ALSA")
        assert entry is not None
        assert "device unavailable" in entry.error_detail
        assert entry.verdict == "open_failed_no_stream"

        reset_default_probe_result_cache()

    @pytest.mark.asyncio()
    async def test_success_invalidates_prior_cache_entry(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """ADR-D5 — a successful dispatch MUST clear any prior dead-
        entry in the cache for the same key.
        """
        from sovyx.voice.health._probe_result_cache import (
            ProbeResultEntry,
            get_default_probe_result_cache,
            reset_default_probe_result_cache,
        )

        reset_default_probe_result_cache()
        cache = get_default_probe_result_cache()

        # Pre-populate a dead entry for the candidate.
        cache.record_probe(
            ProbeResultEntry(
                endpoint_guid="cycler-canonical",
                host_api="ALSA",
                verdict="NO_SIGNAL",
            ),
        )
        assert cache.lookup("cycler-canonical", "ALSA") is not None

        _capture_logs(monkeypatch)
        capture_task = _make_capture_task()
        pipeline = _make_pipeline()
        # Use a tuning instance that does NOT trigger the cache-skip
        # short-circuit. To do that, the prior dead entry MUST NOT be
        # is_known_unopenable for the candidate — but NO_SIGNAL IS
        # is_known_unopenable. Workaround: keep the dead entry as
        # HEALTHY (not skip-worthy) but still present, then assert
        # record_success cleared it.
        reset_default_probe_result_cache()
        cache = get_default_probe_result_cache()
        cache.record_probe(
            ProbeResultEntry(
                endpoint_guid="cycler-canonical",
                host_api="ALSA",
                verdict="HEALTHY",  # not skip-worthy
            ),
        )

        tuning = VoiceTuningConfig(
            runtime_failover_on_quarantine_enabled=True,
            failover_intra_ladder_cooldown_s=0.0,
        )
        state = RuntimeFailoverState()
        candidate = _make_target_entry(
            index=4,
            canonical_name="cycler-canonical",
        )
        success_result = DeviceChangeRestartResult(
            verdict=DeviceChangeRestartVerdict.DEVICE_CHANGED_SUCCESS,
            engaged=True,
            target_device_index=4,
            target_host_api="ALSA",
            new_endpoint_guid="g",
        )
        capture_task.request_device_change_restart = AsyncMock(return_value=success_result)

        with patch.object(
            failover_mod,
            "_resolve_target_safe",
            return_value=(candidate, 1, None),
        ):
            await _try_runtime_failover(
                capture_task=capture_task,
                pipeline=pipeline,
                tuning=tuning,
                state=state,
            )

        # Cache entry for the now-successful candidate MUST have been
        # invalidated via record_success.
        assert cache.lookup("cycler-canonical", "ALSA") is None

        reset_default_probe_result_cache()


# ─────────────────────────────────────────────────────────────────────
# Mission C3 §T2.5 — frame-loss-window summary event
# ─────────────────────────────────────────────────────────────────────


class TestRuntimeFailoverFrameLossWindow:
    """Mission C3 §T2.5 — ``voice.failover.frame_loss_window`` summary.

    During a ladder run, the orchestrator's per-frame
    ``voice.frame.drop_detected`` emit is gated (silent); the ladder's
    completion path emits a SINGLE summary if drops occurred. If no
    drops occurred, no summary event fires (zero-drop summaries are
    observability noise).
    """

    @pytest.mark.asyncio()
    async def test_summary_fires_when_drops_occurred_during_ladder(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """When the orchestrator accumulated drops in
        ``_frame_loss_during_ladder``, the success path emits the
        summary with ``frames_dropped`` and ``duration_ms``.
        """
        from sovyx.voice.health._probe_result_cache import (
            reset_default_probe_result_cache,
        )

        reset_default_probe_result_cache()
        captured = _capture_logs(monkeypatch)
        capture_task = _make_capture_task()

        class _PreloadedPipeline:
            def __init__(self) -> None:
                self._config = MagicMock(mind_id=_MIND_ID)
                self._current_mind_id = _MIND_ID
                self._failover_ladder_in_progress = False
                self._frame_loss_during_ladder: list[tuple[float, float]] = []
                self._reset_called = False

            def reset_coordinator_after_failover(self) -> None:
                self._reset_called = True
                # Simulate per-frame drops accumulating during the
                # dispatch window (the orchestrator's emit-site gate
                # would write here in production).
                self._frame_loss_during_ladder.extend(
                    [
                        (0.150, 1000.5),
                        (0.080, 1000.7),
                        (0.200, 1000.9),
                    ],
                )

        pipeline = _PreloadedPipeline()
        tuning = VoiceTuningConfig(
            runtime_failover_on_quarantine_enabled=True,
            failover_intra_ladder_cooldown_s=0.0,
        )
        state = RuntimeFailoverState()
        candidate = _make_target_entry(index=4, canonical_name="d-4")
        success_result = DeviceChangeRestartResult(
            verdict=DeviceChangeRestartVerdict.DEVICE_CHANGED_SUCCESS,
            engaged=True,
            target_device_index=4,
            target_host_api="ALSA",
            new_endpoint_guid="g",
        )
        capture_task.request_device_change_restart = AsyncMock(return_value=success_result)

        with patch.object(
            failover_mod,
            "_resolve_target_safe",
            return_value=(candidate, 1, None),
        ):
            await _try_runtime_failover(
                capture_task=capture_task,
                pipeline=pipeline,
                tuning=tuning,
                state=state,
            )

        summary = [kwargs for evt, kwargs in captured if evt == "voice.failover.frame_loss_window"]
        assert len(summary) == 1
        assert summary[0]["voice.duration_ms"] == 430.0
        assert summary[0]["voice.frames_dropped"] == 3
        assert summary[0]["voice.candidate_count"] == 1
        assert summary[0]["voice.succeeded_candidate_index"] == 0
        assert pipeline._frame_loss_during_ladder == []

        reset_default_probe_result_cache()

    @pytest.mark.asyncio()
    async def test_no_summary_when_no_drops(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Zero-drop ladder MUST NOT emit ``frame_loss_window``."""
        from sovyx.voice.health._probe_result_cache import (
            reset_default_probe_result_cache,
        )

        reset_default_probe_result_cache()
        captured = _capture_logs(monkeypatch)
        capture_task = _make_capture_task()
        pipeline = _make_pipeline()
        tuning = VoiceTuningConfig(
            runtime_failover_on_quarantine_enabled=True,
            failover_intra_ladder_cooldown_s=0.0,
        )
        state = RuntimeFailoverState()
        candidate = _make_target_entry(index=4, canonical_name="d-4")
        success_result = DeviceChangeRestartResult(
            verdict=DeviceChangeRestartVerdict.DEVICE_CHANGED_SUCCESS,
            engaged=True,
            target_device_index=4,
            target_host_api="ALSA",
            new_endpoint_guid="g",
        )
        capture_task.request_device_change_restart = AsyncMock(return_value=success_result)

        with patch.object(
            failover_mod,
            "_resolve_target_safe",
            return_value=(candidate, 1, None),
        ):
            await _try_runtime_failover(
                capture_task=capture_task,
                pipeline=pipeline,
                tuning=tuning,
                state=state,
            )

        summary = [evt for evt, _ in captured if evt == "voice.failover.frame_loss_window"]
        assert summary == []

        reset_default_probe_result_cache()
