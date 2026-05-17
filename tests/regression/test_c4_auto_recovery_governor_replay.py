"""C4 regression — replay the operator's post-C3-throttle deaf-warning
storm and assert the soft-recovery governor fires within the 3-warning
window + recovers cleanly.

Mission anchor: ``docs-internal/missions/MISSION-c4-degraded-mode-banner-2026-05-17.md``
§Phase 2 / Option D + §9.4 row "test_c4_auto_recovery_governor_replay".

Pre-mission HEAD (v0.46.2): the heartbeat throttles redundant deaf-
warnings (C3 §T2.7) but offers NO auto-recovery — the operator's only
recourse is manual Ctrl-C. The governor does not exist.

Post-mission HEAD (v0.46.3): after N=3 consecutive deaf-warnings while
both ``_coordinator_terminated`` AND ``_failover_ladder_exhausted`` are
True, the governor calls SupervisorMixin.request_soft_recovery() which
clears the latch state + voice axis from the EngineDegradedStore.

F2 falsifiability: the governor's bookkeeping path is testable
synchronously without spawning into a real event loop (the spawn is
mocked here; the SUPERVISOR primitive is exercised separately by
test_supervisor_mixin.py).
"""

from __future__ import annotations

import time
from collections.abc import Coroutine, Generator
from dataclasses import dataclass, field
from typing import Any
from unittest.mock import patch

import pytest

from sovyx.engine._degraded_store import (
    DegradedEntry,
    get_default_degraded_store,
    reset_default_degraded_store,
)
from sovyx.voice.pipeline._heartbeat_mixin import HeartbeatMixin
from sovyx.voice.pipeline._supervisor_mixin import SupervisorMixin


@pytest.fixture(autouse=True)
def _reset_store() -> Generator[None, None, None]:
    reset_default_degraded_store()
    yield
    reset_default_degraded_store()


@pytest.fixture(autouse=True)
def _patch_spawn() -> Generator[list[str], None, None]:
    spawn_calls: list[str] = []

    def _fake_spawn(
        coro: Coroutine[Any, Any, Any],
        *,
        name: str,
        **kwargs: Any,
    ) -> Any:
        spawn_calls.append(name)
        coro.close()
        return None

    with patch("sovyx.observability.tasks.spawn", side_effect=_fake_spawn):
        yield spawn_calls


@dataclass
class _OperatorSessionTuning:
    """Mirrors the operator's v0.43.1 session config — mind_id=jonny
    + default governor knobs."""

    mind_id: str = "jonny"
    supervisor_auto_recovery_n_consecutive_deaf: int = 3
    supervisor_auto_recovery_max_retries_per_session: int = 3
    supervisor_auto_recovery_cooldown_s: float = 300.0
    failover_terminal_deaf_warn_min_interval_s: float = 60.0
    voice_pipeline_heartbeat_interval_s: float = 5.0


@dataclass
class _OperatorSessionHost(SupervisorMixin, HeartbeatMixin):
    """Recreates the v0.43.1 operator-session pipeline state at the
    moment the failover ladder exhausted (L1063) AND coordinator
    latched terminal (L1068)."""

    _config: _OperatorSessionTuning = field(
        default_factory=_OperatorSessionTuning,
    )
    _coordinator_terminated: bool = True
    _failover_ladder_exhausted: bool = True
    _deaf_warnings_consecutive: int = 0
    _max_vad_prob_since_heartbeat: float = 0.001
    _vad_frames_since_heartbeat: int = 63
    _last_terminal_deaf_warn_monotonic: float = 0.0
    _supervisor_recovery_attempts: int = 0
    _supervisor_recovery_last_attempt_monotonic: float = 0.0
    _supervisor_escalation_logged: bool = False

    def reset_coordinator_after_failover(self) -> None:
        self._coordinator_terminated = False
        self._deaf_warnings_consecutive = 0
        self._max_vad_prob_since_heartbeat = 0.0
        self._vad_frames_since_heartbeat = 0


def _seed_voice_axis() -> None:
    """Mirror the failover-exhausted wire shim's store record (Phase 1.A
    §T1.4) so the recovery's clear_axis path has something to clear."""
    store = get_default_degraded_store()
    _now = time.monotonic()
    store.record(
        DegradedEntry(
            axis="voice",
            reason="failover_ladder_exhausted",
            severity="error",
            title_token="degraded.voice.ladderExhausted.title",
            body_token="degraded.voice.ladderExhausted.body",
            action_chips=(),
            metadata={
                "candidates_tried": 1,
                "candidates_unreachable": ["hd-audio-generic-hw10"],
                "ladder_id": "operator_session_replay",
            },
            first_observed_monotonic=_now,
            last_observed_monotonic=_now,
            occurrence_count=1,
        ),
    )


class TestC4AutoRecoveryGovernorReplay:
    def test_governor_fires_at_3rd_consecutive_deaf(
        self,
        _patch_spawn: list[str],
    ) -> None:
        """Operator session emitted 29 deaf-warnings before manual
        Ctrl-C. Post-mission: governor MUST trigger at the 3rd
        consecutive warning."""
        host = _OperatorSessionHost()
        _seed_voice_axis()
        now = time.monotonic()
        for i in range(1, 4):
            host._deaf_warnings_consecutive = i
            host._maybe_run_soft_recovery_governor(now=now + i * 5.0)
        # Governor fired exactly once (at i==3)
        assert host._supervisor_recovery_attempts == 1
        assert len(_patch_spawn) == 1
        assert _patch_spawn[0] == "voice-supervisor-soft-recovery"

    def test_governor_does_not_fire_below_threshold(
        self,
        _patch_spawn: list[str],
    ) -> None:
        host = _OperatorSessionHost()
        _seed_voice_axis()
        now = time.monotonic()
        for i in (1, 2):
            host._deaf_warnings_consecutive = i
            host._maybe_run_soft_recovery_governor(now=now + i * 5.0)
        assert host._supervisor_recovery_attempts == 0
        assert len(_patch_spawn) == 0

    def test_retry_budget_exhausted_escalates(
        self,
        _patch_spawn: list[str],
    ) -> None:
        """After max_retries=3 attempts, the 4th attempt is blocked AND
        the voice axis severity escalates to critical."""
        host = _OperatorSessionHost()
        host._deaf_warnings_consecutive = 3
        now = time.monotonic()
        # Fire 3 attempts past cooldown
        for i in range(3):
            host._maybe_run_soft_recovery_governor(now=now + i * 400.0)
        assert host._supervisor_recovery_attempts == 3
        assert len(_patch_spawn) == 3
        # 4th — budget exhausted, no spawn, escalation fires
        host._maybe_run_soft_recovery_governor(now=now + 4 * 400.0)
        assert host._supervisor_recovery_attempts == 3
        assert len(_patch_spawn) == 3
        # Voice axis upgraded to critical
        entries = get_default_degraded_store().snapshot()
        critical_voice = [e for e in entries if e.axis == "voice" and e.severity == "critical"]
        assert len(critical_voice) == 1
        assert critical_voice[0].metadata["auto_recovery_exhausted"] is True

    def test_post_recovery_state_reset_chain(self) -> None:
        """Soft recovery via the SupervisorMixin (NOT via governor — that
        spawns async; this is the synchronous primitive)."""

        @dataclass
        class _SyncHost(SupervisorMixin):
            _config: _OperatorSessionTuning = field(
                default_factory=_OperatorSessionTuning,
            )
            _coordinator_terminated: bool = True
            _failover_ladder_exhausted: bool = True
            _deaf_warnings_consecutive: int = 29
            _max_vad_prob_since_heartbeat: float = 0.001
            _vad_frames_since_heartbeat: int = 63
            _last_terminal_deaf_warn_monotonic: float = 12345.0

            def reset_coordinator_after_failover(self) -> None:
                self._coordinator_terminated = False
                self._deaf_warnings_consecutive = 0
                self._max_vad_prob_since_heartbeat = 0.0
                self._vad_frames_since_heartbeat = 0

        import asyncio

        _seed_voice_axis()
        host = _SyncHost()

        result = asyncio.run(host.request_soft_recovery(reason="replay"))
        assert result.success is True

        # Latch cleared
        assert host._coordinator_terminated is False
        assert host._failover_ladder_exhausted is False
        assert host._deaf_warnings_consecutive == 0
        assert host._last_terminal_deaf_warn_monotonic == 0.0
        # Voice axis cleared from store — banner stops surfacing
        assert len(get_default_degraded_store()) == 0
