"""Unit tests for the heartbeat soft-recovery governor.

Mission anchor: ``docs-internal/missions/MISSION-c4-degraded-mode-banner-2026-05-17.md``
§Phase 2 / Option D + §9.1 row "Heartbeat governor".

Tests the governor's pre-condition gate, retry-budget exhaustion +
escalation, cooldown, and integration with
:class:`EngineDegradedStore`. The governor logic lives in
``_heartbeat_mixin._maybe_run_soft_recovery_governor``; we instantiate
a minimal host that mounts both HeartbeatMixin AND SupervisorMixin so
the spawn path resolves.
"""

from __future__ import annotations

import time
from collections.abc import Coroutine, Generator
from dataclasses import dataclass, field
from typing import Any
from unittest.mock import patch

import pytest

from sovyx.engine._degraded_store import (
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
    """Replace ``observability.tasks.spawn`` with a no-op that
    records the call but does NOT schedule onto an event loop. Unit
    tests for the governor's state bookkeeping do not need a loop —
    the recovery primitive itself is exercised by
    test_supervisor_mixin.py.
    """
    spawn_calls: list[str] = []

    def _fake_spawn(
        coro: Coroutine[Any, Any, Any],
        *,
        name: str,
        **kwargs: Any,
    ) -> Any:
        spawn_calls.append(name)
        # Properly close the coroutine to avoid "coroutine was never
        # awaited" warnings — calling .close() on an un-awaited coro
        # is the canonical no-op cleanup.
        coro.close()
        return None

    with patch(
        "sovyx.observability.tasks.spawn",
        side_effect=_fake_spawn,
    ):
        yield spawn_calls


@dataclass
class _FakeTuning:
    """Minimal _config stub exposing the four governor knobs."""

    mind_id: str = "test_mind"
    supervisor_auto_recovery_n_consecutive_deaf: int = 3
    supervisor_auto_recovery_max_retries_per_session: int = 3
    supervisor_auto_recovery_cooldown_s: float = 300.0
    failover_terminal_deaf_warn_min_interval_s: float = 60.0
    # Fields HeartbeatMixin reads in unrelated code paths
    voice_pipeline_heartbeat_interval_s: float = 5.0


@dataclass
class _FakeGovernorHost(SupervisorMixin, HeartbeatMixin):
    """Minimal host exercising the governor in isolation. Inherits
    SupervisorMixin so ``request_soft_recovery`` is available; inherits
    HeartbeatMixin so ``_maybe_run_soft_recovery_governor`` is bound."""

    _config: _FakeTuning = field(default_factory=_FakeTuning)
    _coordinator_terminated: bool = True
    _failover_ladder_exhausted: bool = True
    _deaf_warnings_consecutive: int = 3
    _max_vad_prob_since_heartbeat: float = 0.001
    _vad_frames_since_heartbeat: int = 63
    _last_terminal_deaf_warn_monotonic: float = 0.0
    _supervisor_recovery_attempts: int = 0
    _supervisor_recovery_last_attempt_monotonic: float = 0.0
    _supervisor_escalation_logged: bool = False
    reset_count: int = 0

    def reset_coordinator_after_failover(self) -> None:
        self.reset_count += 1
        self._coordinator_terminated = False
        self._deaf_warnings_consecutive = 0
        self._max_vad_prob_since_heartbeat = 0.0
        self._vad_frames_since_heartbeat = 0


class TestGovernorPreConditions:
    def test_below_n_threshold_no_trigger(self) -> None:
        host = _FakeGovernorHost(_deaf_warnings_consecutive=2)
        host._maybe_run_soft_recovery_governor(now=time.monotonic())
        assert host._supervisor_recovery_attempts == 0

    def test_at_n_threshold_triggers(self) -> None:
        host = _FakeGovernorHost(_deaf_warnings_consecutive=3)
        host._maybe_run_soft_recovery_governor(now=time.monotonic())
        # Counter bumped synchronously even though recovery runs async
        assert host._supervisor_recovery_attempts == 1

    def test_above_n_threshold_triggers(self) -> None:
        host = _FakeGovernorHost(_deaf_warnings_consecutive=10)
        host._maybe_run_soft_recovery_governor(now=time.monotonic())
        assert host._supervisor_recovery_attempts == 1

    def test_max_retries_zero_disables_governor(self) -> None:
        """Knob max_retries=0 = governor disabled (operator escape
        hatch back to pre-Mission-C4 manual-Ctrl-C posture)."""
        cfg = _FakeTuning(supervisor_auto_recovery_max_retries_per_session=0)
        host = _FakeGovernorHost(_config=cfg)
        host._maybe_run_soft_recovery_governor(now=time.monotonic())
        assert host._supervisor_recovery_attempts == 0


class TestGovernorCooldown:
    def test_within_cooldown_blocks_retrigger(self) -> None:
        now = time.monotonic()
        host = _FakeGovernorHost(
            _supervisor_recovery_attempts=1,
            _supervisor_recovery_last_attempt_monotonic=now,
        )
        host._maybe_run_soft_recovery_governor(now=now + 100.0)
        # Cooldown is 300 s default; 100 s elapsed = still blocked
        assert host._supervisor_recovery_attempts == 1

    def test_past_cooldown_allows_retrigger(self) -> None:
        now = time.monotonic()
        host = _FakeGovernorHost(
            _supervisor_recovery_attempts=1,
            _supervisor_recovery_last_attempt_monotonic=now,
        )
        host._maybe_run_soft_recovery_governor(now=now + 301.0)
        assert host._supervisor_recovery_attempts == 2

    def test_at_cooldown_boundary_allows_retrigger_per_anti_pattern_24(self) -> None:
        """Anti-pattern #24 — ``>=`` not ``>`` on monotonic deadlines."""
        now = time.monotonic()
        host = _FakeGovernorHost(
            _supervisor_recovery_attempts=1,
            _supervisor_recovery_last_attempt_monotonic=now,
        )
        host._maybe_run_soft_recovery_governor(now=now + 300.0)
        assert host._supervisor_recovery_attempts == 2


class TestGovernorBudgetExhaustion:
    def test_at_max_retries_escalates_to_critical(self) -> None:
        host = _FakeGovernorHost(_supervisor_recovery_attempts=3)
        store = get_default_degraded_store()
        assert len(store) == 0
        host._maybe_run_soft_recovery_governor(now=time.monotonic())
        # No new attempt (budget exhausted)
        assert host._supervisor_recovery_attempts == 3
        # Voice axis recorded with severity=critical
        entries = store.snapshot()
        assert len(entries) == 1
        assert entries[0].axis == "voice"
        assert entries[0].severity == "critical"

    def test_escalation_logged_once_per_session(self) -> None:
        host = _FakeGovernorHost(_supervisor_recovery_attempts=3)
        host._maybe_run_soft_recovery_governor(now=time.monotonic())
        assert host._supervisor_escalation_logged is True
        store_size_after_first = len(get_default_degraded_store())
        # Second call — should NOT double-emit the escalation event
        host._maybe_run_soft_recovery_governor(now=time.monotonic() + 1000.0)
        assert host._supervisor_escalation_logged is True
        # Store entry stays single (idempotent upsert)
        assert len(get_default_degraded_store()) == store_size_after_first

    def test_escalation_metadata_records_attempt_count(self) -> None:
        host = _FakeGovernorHost(_supervisor_recovery_attempts=3)
        host._maybe_run_soft_recovery_governor(now=time.monotonic())
        entry = get_default_degraded_store().snapshot()[0]
        assert entry.metadata["auto_recovery_exhausted"] is True
        assert entry.metadata["retries_attempted"] == 3


class TestGovernorMissingSupervisor:
    def test_missing_request_soft_recovery_method_is_no_op(self) -> None:
        """Pre-Phase-2 host (during rollback) lacks request_soft_recovery
        — the governor MUST detect this and skip gracefully without
        raising."""

        @dataclass
        class _NoSupervisorHost(HeartbeatMixin):
            _config: _FakeTuning = field(default_factory=_FakeTuning)
            _deaf_warnings_consecutive: int = 3
            _supervisor_recovery_attempts: int = 0
            _supervisor_recovery_last_attempt_monotonic: float = 0.0
            _supervisor_escalation_logged: bool = False

        host = _NoSupervisorHost()
        # Even though governor gates pass, the missing method is
        # detected + skipped — attempt counter STILL bumps (the
        # governor's bookkeeping is independent of the recovery call's
        # availability) but no spawn happens. This is the conservative
        # choice: a wedged host without supervisor still triggers the
        # cooldown gate the next time around.
        host._maybe_run_soft_recovery_governor(now=time.monotonic())
        assert host._supervisor_recovery_attempts == 1
