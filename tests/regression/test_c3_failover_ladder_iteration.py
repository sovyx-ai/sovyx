"""Mission C3 §T1.5 regression — failover ladder loop-in-place closure.

Replays the canonical operator-log L1015 → L1063 collapse window from
the 2026-05-14 ``FORENSIC-AUDIT-LOG-2026-05-14-v0.43.1.md`` C3 finding
(lines 122-135).

Operator session timeline (Sony VAIO + Linux Mint + Razer USB):

    L1015 voice.failover.attempted candidates_remaining=3
          (chose idx=4, HD-Audio Generic)
    L1026 PortAudio stderr: Expression 'AlsaOpen' failed in
          pa_linux_alsa.c:1904
    L1030 audio.stream.fallback × 3 (16k↔48k, 1↔2 ch permutations
          on the SAME device, no candidate re-probe)
    L1054 voice_stream_open_failed attempts=4, final_code=device_not_found
    L1063 voice.failover.failed verdict=downgraded_to_source
          → REMAINING 2 CANDIDATES NEVER TRIED.

Pre-Mission-C3 the failover dispatched ONE candidate per closure
invocation and returned on either success or failure; the next
candidate would only be tried (a) 30 s later when the next deaf-signal
heartbeat fires AND (b) after the outer ``failover_cooldown_s`` gate
releases AND (c) only if the C4 coordinator-terminated latch had not
fired in between (which it had). In the operator's session, none of
these conditions held, so candidates 2 (PipeWire) + 3 (OS default)
were never even attempted within the same deaf-signal cycle.

Post-Mission-C3 the helper iterates every non-excluded candidate
within a single closure invocation, dispatching with the per-candidate
intra-ladder cooldown. The test:

1. **Replays the exact 3-candidate failure sequence** from the
   operator log: candidate 1 returns engaged=False
   verdict=DOWNGRADED_TO_SOURCE, candidate 2 returns engaged=True
   verdict=DEVICE_CHANGED_SUCCESS.
2. **Asserts the loop dispatched 2 candidates** within the same
   closure (rather than 1, which was the pre-mission behaviour).
3. **Asserts the legacy event compatibility**: ``voice.failover.failed``
   does NOT fire (the loop succeeded on candidate 2); the new
   ``voice.failover.ladder_complete{verdict=succeeded,
   succeeded_index=1}`` fires; the per-candidate
   ``voice.failover.candidate_attempted`` fires for both 0 and 1.

This is the F2 falsifiability gate from Mission C3 §3 — the test
PASSES post-mission and FAILS on pre-mission HEAD (verified via
``git stash && uv run pytest tests/regression/test_c3_failover_ladder_iteration.py``
which would assert ``await_count == 2`` but the pre-mission code
dispatches exactly 1 per call).
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

_MIND_ID = "jonny"  # operator's mind id from the v0.43.1 session


def _make_capture_task() -> MagicMock:
    """Build a capture-task fake mirroring the operator's hardware shape."""
    task = MagicMock()
    # Operator's currently-active device pre-failover (the Razer that
    # got quarantined per Mission C1 §C3 closure).
    task.active_device_guid = "linux-usb-1532:0528-0-duplex"
    task.active_device_name = "Razer BlackShark V2 Pro"
    task._input_device = 5
    task._host_api_name = "ALSA"
    return task


def _make_pipeline() -> MagicMock:
    pipeline = MagicMock()
    pipeline._config = MagicMock(mind_id=_MIND_ID)
    pipeline._current_mind_id = _MIND_ID
    pipeline.reset_coordinator_after_failover = MagicMock()
    return pipeline


def _make_device_entry(
    *,
    index: int,
    name: str,
    canonical_name: str,
):  # type: ignore[no-untyped-def]
    from sovyx.voice.device_enum import DeviceEntry

    return DeviceEntry(
        index=index,
        name=name,
        canonical_name=canonical_name,
        host_api_index=0,
        host_api_name="ALSA",
        max_input_channels=2,
        max_output_channels=2,
        default_samplerate=48_000,
        is_os_default=False,
    )


def _capture_logs(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, dict[str, object]]]:
    """Spy on the failover module logger.

    Mirrors the helper at ``tests/unit/voice/health/test_runtime_failover.py``
    — Sovyx routes use structlog ``BoundLoggerLazyProxy`` which bypasses
    stdlib ``logging`` so caplog can't see those events. patch.object
    on the module logger is the deterministic alternative per anti-
    pattern #11.
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


class TestC3LadderIterationOperatorReplay:
    """Forensic replay — operator session 2026-05-14 v0.43.1.

    Mission C3 F2 falsifiability gate.
    """

    @pytest.mark.asyncio()
    async def test_l1015_l1063_three_candidate_cascade_recovers_on_candidate_2(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Operator's exact 3-candidate cascade — candidate 1
        (HD-Audio Generic idx=4) returns engaged=False
        verdict=DOWNGRADED_TO_SOURCE; candidate 2 (PipeWire idx=7)
        engages. Post-Mission-C3 the loop iterates both within one
        closure; pre-Mission-C3 only candidate 1 was tried.
        """
        captured = _capture_logs(monkeypatch)
        capture_task = _make_capture_task()
        pipeline = _make_pipeline()
        tuning = VoiceTuningConfig(
            runtime_failover_on_quarantine_enabled=True,
            failover_intra_ladder_cooldown_s=0.0,  # CI budget; prod uses 2.0
            failover_candidate_max_attempts_per_ladder=5,
        )
        state = RuntimeFailoverState()

        # 3 candidates mirroring the operator's host:
        #   idx=4: HD-Audio Generic (the AlsaOpen offender)
        #   idx=7: PipeWire virtual source (the actual recovery)
        #   idx=8: OS default
        candidate_hda = _make_device_entry(
            index=4,
            name="HD-Audio Generic: SN6180 Analog (hw:1,0)",
            canonical_name="hd-audio-generic-sn6180-hw10",
        )
        candidate_pipewire = _make_device_entry(
            index=7,
            name="pipewire",
            canonical_name="pipewire-virtual-source-idx7",
        )

        # The opener's exhaustive permutation pyramid on candidate
        # idx=4 ultimately returns ``OPEN_FAILED_NO_STREAM`` (the
        # ``device_not_found`` final code per L1054), exposed to the
        # failover helper as a ``DeviceChangeRestartResult`` with
        # ``engaged=False``. Candidate idx=7 (PipeWire) engages.
        candidate_1_result = DeviceChangeRestartResult(
            verdict=DeviceChangeRestartVerdict.DOWNGRADED_TO_SOURCE,
            engaged=False,
            target_device_index=4,
            target_host_api="ALSA",
            new_endpoint_guid="hda-generic-guid",
            detail="AlsaOpen failed: pa_linux_alsa.c:1904; final_code=device_not_found",
        )
        candidate_2_result = DeviceChangeRestartResult(
            verdict=DeviceChangeRestartVerdict.DEVICE_CHANGED_SUCCESS,
            engaged=True,
            target_device_index=7,
            target_host_api="ALSA",
            new_endpoint_guid="pipewire-virtual-guid",
        )
        capture_task.request_device_change_restart = AsyncMock(
            side_effect=[candidate_1_result, candidate_2_result],
        )

        resolve_side_effect = [
            (candidate_hda, 3, None),  # pre-loop step 1 — L1015 anchor
            (candidate_pipewire, 2, None),  # iter 1 re-resolve — the recovery
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

        # ─────────────────────────────────────────────────────────────
        # F2 ASSERTIONS — operator-log L1015 → L1063 closure
        # ─────────────────────────────────────────────────────────────

        # Both candidates dispatched within one closure invocation.
        # Pre-Mission-C3 this was ``await_count == 1``.
        assert capture_task.request_device_change_restart.await_count == 2, (  # noqa: PLR2004
            "Mission C3 F2 falsifiability gate: the loop MUST dispatch "
            "candidates 1+2 within a single deaf-signal closure. "
            "Pre-Mission-C3 only candidate 1 was tried, leaving "
            "candidate 2 (PipeWire) stranded for ≥ 30 s of cooldown."
        )

        # Coordinator reset on candidate 2's success (legacy contract
        # preserved — the heartbeat path needs the latch cleared so
        # the new endpoint gets its own deaf-detection cycle).
        pipeline.reset_coordinator_after_failover.assert_called_once()

        # state.attempts bumped exactly ONCE (per-ladder semantic,
        # backward-compatible with the pre-mission cross-invocation
        # cap).
        assert state.attempts == 1

        # ladder_id set on state.
        assert state.ladder_id != ""

        # ladder_exhausted False after success.
        assert state.ladder_exhausted is False

        event_names = [evt for evt, _ in captured]

        # Lenient telemetry STILL fires at the top of the closure —
        # the L1015 anchor event is preserved.
        attempted = [kwargs for evt, kwargs in captured if evt == "voice.failover.attempted"]
        assert len(attempted) == 1
        assert attempted[0]["voice.candidates_remaining"] == 3  # operator L1015
        assert attempted[0]["voice.to_endpoint"] == "hd-audio-generic-sn6180-hw10"

        # ladder_started fires with the ladder_id.
        ladder_started = [
            kwargs for evt, kwargs in captured if evt == "voice.failover.ladder_started"
        ]
        assert len(ladder_started) == 1
        assert ladder_started[0]["voice.ladder_id"] == state.ladder_id

        # 2 candidate_attempted events with monotonically-incrementing
        # index.
        candidate_attempted = [
            kwargs for evt, kwargs in captured if evt == "voice.failover.candidate_attempted"
        ]
        assert len(candidate_attempted) == 2  # noqa: PLR2004
        assert candidate_attempted[0]["voice.index"] == 0
        assert candidate_attempted[1]["voice.index"] == 1
        assert candidate_attempted[0]["voice.target_endpoint"] == "hd-audio-generic-sn6180-hw10"
        assert candidate_attempted[1]["voice.target_endpoint"] == "pipewire-virtual-source-idx7"

        # 1 candidate_failed for the HD-Audio Generic dispatch.
        candidate_failed = [
            kwargs for evt, kwargs in captured if evt == "voice.failover.candidate_failed"
        ]
        assert len(candidate_failed) == 1
        assert candidate_failed[0]["voice.target_endpoint"] == "hd-audio-generic-sn6180-hw10"
        assert candidate_failed[0]["voice.verdict"] == "downgraded_to_source"
        # All candidate_failed events MUST carry the ladder_id so
        # dashboards correlate.
        assert candidate_failed[0]["voice.ladder_id"] == state.ladder_id

        # Legacy ``voice.failover.succeeded`` event preserved.
        succeeded = [kwargs for evt, kwargs in captured if evt == "voice.failover.succeeded"]
        assert len(succeeded) == 1
        assert succeeded[0]["voice.to_endpoint"] == "pipewire-virtual-source-idx7"
        assert succeeded[0]["voice.candidate_index_in_ladder"] == 1
        assert succeeded[0]["voice.ladder_id"] == state.ladder_id

        # ladder_complete with verdict=succeeded, succeeded_index=1.
        ladder_complete = [
            kwargs for evt, kwargs in captured if evt == "voice.failover.ladder_complete"
        ]
        assert len(ladder_complete) == 1
        assert ladder_complete[0]["voice.verdict"] == "succeeded"
        assert ladder_complete[0]["voice.succeeded_index"] == 1
        assert ladder_complete[0]["voice.candidates_tried"] == 2  # noqa: PLR2004
        assert ladder_complete[0]["voice.ladder_id"] == state.ladder_id

        # Crucially — ``voice.failover.failed`` MUST NOT fire (the
        # ladder succeeded). Pre-Mission-C3 this fired with
        # ``verdict=downgraded_to_source`` (the L1063 anchor) and
        # was the terminal event for the closure. The F2 inversion:
        # post-mission, this event is absent because the ladder
        # iterated to a working candidate.
        assert "voice.failover.failed" not in event_names, (
            "Mission C3 F2 falsifiability gate: post-mission, the "
            "ladder succeeds on candidate 2 (PipeWire), so "
            "voice.failover.failed (the L1063 anchor) MUST NOT fire. "
            "Pre-mission this event was the terminal verdict for the "
            "closure with candidates_remaining=2 stranded."
        )
