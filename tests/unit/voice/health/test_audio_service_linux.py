"""Tests for ``sovyx.voice.health._audio_service_linux`` (Phase 6 / T6.21).

Pre-T6.21 the Linux audio-service monitor (338 LOC) had ZERO direct
test coverage — every code path was only exercised transitively via
``VoiceCaptureWatchdog`` integration tests. This module pins:

* ``_query_service_state`` parser branches: ``active`` / ``inactive``
  / ``failed`` / empty stdout / FileNotFoundError / SubprocessError /
  TimeoutExpired / OSError. Non-zero ``returncode`` with valid
  stdout is the documented ``is-active`` semantic — must read state
  from stdout regardless of exit code.
* ``_query_service_load_state`` parser branches: ``loaded`` /
  ``not-found`` / ``masked`` / spawn failures.
* ``_probe_existing_services`` candidate filtering by ``LoadState``
  (fixtures mirror REAL systemctl behaviour — verified live on
  systemd 255: ``is-active`` prints ``inactive`` even for NOT-FOUND
  units, so installation is probed via ``show -p LoadState``; audit
  finding LINUX-2 / Debugging Rule #13).
* ``LinuxAudioServiceMonitor.__init__`` validation: empty services
  rejected, zero / negative interval rejected.
* ``start`` / ``stop`` lifecycle mirroring the Windows path.
* ``_run`` aggregate-state transitions: baseline-seed (no spurious
  UP); ANY-down transition emits aggregate DOWN once (correlated
  flaps don't double-fire); ALL-up emits UP; transient query
  failures (None) preserve prior state; handler exception swallowed
  but CancelledError propagates.
* ``build_linux_audio_service_monitor`` factory: Noop when probe
  finds no services; real monitor when any service exists.
"""

from __future__ import annotations

import asyncio
import subprocess
from collections.abc import Awaitable, Callable
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from sovyx.voice.health._audio_service import NoopAudioServiceMonitor
from sovyx.voice.health._audio_service_linux import (
    _AUDIO_SERVICE_CANDIDATES,
    LinuxAudioServiceMonitor,
    _probe_existing_services,
    _query_service_load_state,
    _query_service_state,
    build_linux_audio_service_monitor,
)
from sovyx.voice.health.contract import AudioServiceEvent, AudioServiceEventKind

# ── _query_service_state parser ───────────────────────────────────────


class TestQueryServiceState:
    """systemctl --user is-active parser — every branch."""

    def _patch_run(self, *, returncode: int, stdout: str) -> Any:  # noqa: ANN401
        completed = MagicMock(spec=subprocess.CompletedProcess)
        completed.returncode = returncode
        completed.stdout = stdout
        return patch(
            "sovyx.voice.health._audio_service_linux.subprocess.run",
            return_value=completed,
        )

    def test_active_returncode_zero(self) -> None:
        # Healthy unit: returncode=0 + stdout="active".
        with self._patch_run(returncode=0, stdout="active\n"):
            assert _query_service_state("pipewire.service") == "active"

    def test_inactive_with_nonzero_returncode(self) -> None:
        # is-active exits non-zero for inactive units but the state is
        # still in stdout. The function MUST read stdout regardless of
        # exit code (only treats subprocess failures as None).
        with self._patch_run(returncode=3, stdout="inactive\n"):
            assert _query_service_state("pipewire.service") == "inactive"

    def test_failed_unit(self) -> None:
        with self._patch_run(returncode=3, stdout="failed\n"):
            assert _query_service_state("pulseaudio.service") == "failed"

    def test_not_installed_unit_reports_inactive(self) -> None:
        # REAL systemctl behaviour (verified on systemd 255): a unit
        # whose unit file does NOT exist prints "inactive" with exit 4
        # — "unknown" is never emitted. This is exactly why is-active
        # cannot drive the installed-probe (audit finding LINUX-2);
        # the parser must still return the literal state.
        with self._patch_run(returncode=4, stdout="inactive\n"):
            assert _query_service_state("ghost.service") == "inactive"

    def test_empty_stdout_returns_none(self) -> None:
        with self._patch_run(returncode=0, stdout=""):
            assert _query_service_state("pipewire.service") is None

    def test_whitespace_only_stdout_returns_none(self) -> None:
        with self._patch_run(returncode=0, stdout="   \n  \n"):
            assert _query_service_state("pipewire.service") is None

    def test_file_not_found_returns_none(self) -> None:
        # systemctl absent (Alpine, raw container).
        with patch(
            "sovyx.voice.health._audio_service_linux.subprocess.run",
            side_effect=FileNotFoundError("systemctl"),
        ):
            assert _query_service_state("pipewire.service") is None

    def test_subprocess_error_returns_none(self) -> None:
        with patch(
            "sovyx.voice.health._audio_service_linux.subprocess.run",
            side_effect=subprocess.SubprocessError("crash"),
        ):
            assert _query_service_state("pipewire.service") is None

    def test_timeout_expired_returns_none(self) -> None:
        with patch(
            "sovyx.voice.health._audio_service_linux.subprocess.run",
            side_effect=subprocess.TimeoutExpired("systemctl", 3.0),
        ):
            assert _query_service_state("pipewire.service") is None

    def test_os_error_returns_none(self) -> None:
        # User bus inaccessible / handle exhaustion.
        with patch(
            "sovyx.voice.health._audio_service_linux.subprocess.run",
            side_effect=OSError("bus unavailable"),
        ):
            assert _query_service_state("pipewire.service") is None

    def test_multi_line_stdout_takes_first_line(self) -> None:
        # Defensive — systemctl shouldn't emit multi-line state output
        # for `is-active`, but if it does we read the first line only.
        with self._patch_run(returncode=0, stdout="active\nleftover\n"):
            assert _query_service_state("pipewire.service") == "active"


# ── _probe_existing_services ─────────────────────────────────────────


class TestQueryServiceLoadState:
    """systemctl --user show -p LoadState parser — real output shapes."""

    def _patch_run(self, *, returncode: int, stdout: str) -> Any:  # noqa: ANN401
        completed = MagicMock(spec=subprocess.CompletedProcess)
        completed.returncode = returncode
        completed.stdout = stdout
        return patch(
            "sovyx.voice.health._audio_service_linux.subprocess.run",
            return_value=completed,
        )

    def test_loaded_unit(self) -> None:
        # Real fixture: `systemctl --user show -p LoadState dbus.service`
        # → "LoadState=loaded" rc=0 (captured on systemd 255).
        with self._patch_run(returncode=0, stdout="LoadState=loaded\n"):
            assert _query_service_load_state("dbus.service") == "loaded"

    def test_not_found_unit(self) -> None:
        # Real fixture: nonexistent unit → "LoadState=not-found" rc=0.
        with self._patch_run(returncode=0, stdout="LoadState=not-found\n"):
            assert _query_service_load_state("ghost.service") == "not-found"

    def test_masked_unit(self) -> None:
        with self._patch_run(returncode=0, stdout="LoadState=masked\n"):
            assert _query_service_load_state("pulseaudio.service") == "masked"

    def test_missing_key_returns_none(self) -> None:
        with self._patch_run(returncode=0, stdout="Other=thing\n"):
            assert _query_service_load_state("x.service") is None

    def test_nonzero_returncode_returns_none(self) -> None:
        with self._patch_run(returncode=1, stdout=""):
            assert _query_service_load_state("x.service") is None

    def test_spawn_failure_returns_none(self) -> None:
        with patch(
            "sovyx.voice.health._audio_service_linux.subprocess.run",
            side_effect=FileNotFoundError("systemctl"),
        ):
            assert _query_service_load_state("x.service") is None

    def test_timeout_returns_none(self) -> None:
        with patch(
            "sovyx.voice.health._audio_service_linux.subprocess.run",
            side_effect=subprocess.TimeoutExpired("systemctl", 3.0),
        ):
            assert _query_service_load_state("x.service") is None


def _load_states(mapping: dict[str, str | None]) -> Callable[[str], str | None]:
    """Fixture builder — mirrors real `show -p LoadState` values."""

    def _lq(svc: str) -> str | None:
        return mapping.get(svc)

    return _lq


class TestProbeExistingServices:
    def test_all_candidates_loaded(self) -> None:
        result = _probe_existing_services(
            load_state_query=_load_states(dict.fromkeys(_AUDIO_SERVICE_CANDIDATES, "loaded")),
        )
        assert result == set(_AUDIO_SERVICE_CANDIDATES)

    def test_pipewire_only_host_excludes_not_found_pulseaudio(self) -> None:
        # Modern PipeWire host: real systemctl reports the uninstalled
        # pulseaudio.service as LoadState=not-found (NOT a None/other
        # sentinel — Debugging Rule #13; audit finding LINUX-2). The
        # ghost unit must be excluded or the aggregate is permanently
        # False and DOWN/UP never fires.
        result = _probe_existing_services(
            load_state_query=_load_states(
                {
                    "pipewire.service": "loaded",
                    "wireplumber.service": "loaded",
                    "pipewire-pulse.service": "loaded",
                    "pulseaudio.service": "not-found",
                },
            ),
        )
        assert result == {
            "pipewire.service",
            "wireplumber.service",
            "pipewire-pulse.service",
        }

    def test_masked_unit_excluded(self) -> None:
        # `systemctl --user mask pulseaudio.service` is the canonical
        # PipeWire-distro state — a masked unit can never become
        # active, so watching it would recreate the stuck-False bug.
        result = _probe_existing_services(
            load_state_query=_load_states(
                {
                    "pipewire.service": "loaded",
                    "wireplumber.service": "loaded",
                    "pipewire-pulse.service": "loaded",
                    "pulseaudio.service": "masked",
                },
            ),
        )
        assert "pulseaudio.service" not in result
        assert "pipewire.service" in result

    def test_pure_pulseaudio_host(self) -> None:
        result = _probe_existing_services(
            load_state_query=_load_states(
                {
                    "pipewire.service": "not-found",
                    "wireplumber.service": "not-found",
                    "pipewire-pulse.service": "not-found",
                    "pulseaudio.service": "loaded",
                },
            ),
        )
        assert result == {"pulseaudio.service"}

    def test_systemctl_unavailable_returns_empty(self) -> None:
        # Non-systemd host / no user bus → every load-state query is
        # None → empty set → factory routes to Noop.
        result = _probe_existing_services(load_state_query=lambda _svc: None)
        assert result == set()

    def test_custom_candidates_parameter(self) -> None:
        result = _probe_existing_services(
            candidates=("pipewire.service",),
            load_state_query=_load_states({"pipewire.service": "loaded"}),
        )
        assert result == {"pipewire.service"}


# ── Constructor validation ────────────────────────────────────────────


class TestConstructor:
    def test_default_query_factory(self) -> None:
        monitor = LinuxAudioServiceMonitor(
            services_to_monitor=frozenset({"pipewire.service"}),
        )
        # Internal access — guards the default-wire-up.
        assert monitor._query is _query_service_state  # noqa: SLF001

    def test_explicit_query_takes_precedence(self) -> None:
        fake = lambda _svc: "active"  # noqa: E731 — terse test stub
        monitor = LinuxAudioServiceMonitor(
            services_to_monitor=frozenset({"pipewire.service"}),
            query=fake,
        )
        assert monitor._query is fake  # noqa: SLF001

    def test_empty_services_rejected(self) -> None:
        with pytest.raises(ValueError, match="services_to_monitor must be non-empty"):
            LinuxAudioServiceMonitor(services_to_monitor=frozenset())

    def test_zero_interval_rejected(self) -> None:
        with pytest.raises(ValueError, match="poll_interval_s must be"):
            LinuxAudioServiceMonitor(
                services_to_monitor=frozenset({"pipewire.service"}),
                poll_interval_s=0.0,
            )

    def test_negative_interval_rejected(self) -> None:
        with pytest.raises(ValueError, match="poll_interval_s must be"):
            LinuxAudioServiceMonitor(
                services_to_monitor=frozenset({"pipewire.service"}),
                poll_interval_s=-1.0,
            )


# ── Lifecycle ─────────────────────────────────────────────────────────


async def _noop_handler(_event: AudioServiceEvent) -> None:
    return None


def _all_active() -> Callable[[str], str]:
    def _q(_svc: str) -> str:
        return "active"

    return _q


class TestLifecycle:
    @pytest.mark.asyncio()
    async def test_start_launches_task(self) -> None:
        monitor = LinuxAudioServiceMonitor(
            services_to_monitor=frozenset({"pipewire.service"}),
            poll_interval_s=10.0,
            query=_all_active(),
        )
        await monitor.start(_noop_handler)
        assert monitor._task is not None  # noqa: SLF001
        assert not monitor._task.done()  # noqa: SLF001
        await monitor.stop()

    @pytest.mark.asyncio()
    async def test_start_idempotent(self) -> None:
        monitor = LinuxAudioServiceMonitor(
            services_to_monitor=frozenset({"pipewire.service"}),
            poll_interval_s=10.0,
            query=_all_active(),
        )
        await monitor.start(_noop_handler)
        first_task = monitor._task  # noqa: SLF001
        await monitor.start(_noop_handler)
        assert monitor._task is first_task  # noqa: SLF001
        await monitor.stop()

    @pytest.mark.asyncio()
    async def test_stop_cancels_in_flight(self) -> None:
        monitor = LinuxAudioServiceMonitor(
            services_to_monitor=frozenset({"pipewire.service"}),
            poll_interval_s=100.0,
            query=_all_active(),
        )
        await monitor.start(_noop_handler)
        task = monitor._task  # noqa: SLF001
        await monitor.stop()
        assert task is not None
        assert task.done()
        assert monitor._task is None  # noqa: SLF001

    @pytest.mark.asyncio()
    async def test_stop_without_start_is_noop(self) -> None:
        monitor = LinuxAudioServiceMonitor(
            services_to_monitor=frozenset({"pipewire.service"}),
            query=_all_active(),
        )
        await monitor.stop()  # should not raise
        assert monitor._task is None  # noqa: SLF001


# ── Aggregate transition logic ────────────────────────────────────────


class _MultiServiceQuery:
    """Programmable per-service query stand-in.

    ``timeline`` maps a poll round (0-indexed) to a per-service state
    dict. Service names not in the dict for a round inherit the
    previous round's value, defaulting to "active" on round 0.
    """

    def __init__(
        self,
        timeline: list[dict[str, str | None]],
        services: set[str],
    ) -> None:
        self._timeline = list(timeline)
        self._services = services
        self._round_starts: dict[str, int] = dict.fromkeys(services, 0)
        self._calls_in_round = 0
        self._round = 0
        # Persistent per-service state — a round inherits the prior
        # state until the timeline overrides it.
        self._state: dict[str, str | None] = dict.fromkeys(services, "active")
        self.calls = 0

    def __call__(self, service: str) -> str | None:
        self.calls += 1
        # Detect end-of-round: when we've queried every service this
        # round, advance the round + apply the next timeline overrides.
        if service not in self._round_starts:
            self._round_starts[service] = self._round
        self._round_starts[service] = self._round
        # Apply timeline overrides for this round if not yet applied.
        if self._round < len(self._timeline):
            self._state.update(self._timeline[self._round])
        result = self._state.get(service, "active")
        # Bump round counter when all services seen in current round.
        self._calls_in_round += 1
        if self._calls_in_round >= len(self._services):
            self._round += 1
            self._calls_in_round = 0
        return result


class _EventCapture:
    def __init__(self) -> None:
        self.events: list[AudioServiceEvent] = []

    async def __call__(self, event: AudioServiceEvent) -> None:
        self.events.append(event)


async def _drive_polls(
    monitor: LinuxAudioServiceMonitor,
    handler: Callable[[AudioServiceEvent], Awaitable[None]],
    *,
    expected_rounds: int,
    query: _MultiServiceQuery,
    services: set[str],
) -> None:
    """Run the monitor until ``query`` has completed ``expected_rounds`` polls.

    Each round queries every service once. Loop waits for an extra
    "drain" round past the timeline so the production loop has time
    to process the final transition + invoke the handler — without
    the drain margin, the cancellation from ``stop()`` can land while
    the gather() call is in-flight, dropping the last event.
    """
    await monitor.start(handler)
    # Bump by +1 round so we observe the production loop finishing
    # the last timeline round AND starting its next sleep cycle —
    # guarantees the handler dispatch for round N has completed.
    expected_calls = (expected_rounds + 1) * len(services)
    deadline = asyncio.get_event_loop().time() + 2.0
    while query.calls < expected_calls:
        if asyncio.get_event_loop().time() > deadline:
            await monitor.stop()
            msg = (
                f"Monitor did not complete {expected_rounds} polls in 2 s "
                f"(observed {query.calls} calls)"
            )
            raise AssertionError(msg)
        await asyncio.sleep(0.005)
    await monitor.stop()


class TestAggregateTransitions:
    @pytest.mark.asyncio()
    async def test_baseline_seed_no_event_on_first_poll(self) -> None:
        services = {"pipewire.service"}
        query = _MultiServiceQuery([{}], services)
        capture = _EventCapture()
        monitor = LinuxAudioServiceMonitor(
            services_to_monitor=frozenset(services),
            poll_interval_s=0.001,
            query=query,
        )
        await _drive_polls(
            monitor,
            capture,
            expected_rounds=1,
            query=query,
            services=services,
        )
        assert capture.events == []

    @pytest.mark.asyncio()
    async def test_active_to_inactive_aggregate_emits_down(self) -> None:
        services = {"pipewire.service"}
        # Round 0: active. Round 1: inactive. → DOWN emitted.
        query = _MultiServiceQuery(
            [{}, {"pipewire.service": "inactive"}],
            services,
        )
        capture = _EventCapture()
        monitor = LinuxAudioServiceMonitor(
            services_to_monitor=frozenset(services),
            poll_interval_s=0.001,
            query=query,
        )
        await _drive_polls(
            monitor,
            capture,
            expected_rounds=2,
            query=query,
            services=services,
        )
        assert len(capture.events) == 1
        assert capture.events[0].kind is AudioServiceEventKind.DOWN

    @pytest.mark.asyncio()
    async def test_correlated_multi_service_failure_emits_single_down(
        self,
    ) -> None:
        # Both pipewire AND wireplumber go inactive in the same round
        # → aggregate transitions True→False ONCE. Operators see one
        # DOWN, not two — the whole rationale for aggregating across
        # the user-session audio stack.
        services = {"pipewire.service", "wireplumber.service"}
        query = _MultiServiceQuery(
            [
                {},  # baseline: both active.
                {
                    "pipewire.service": "inactive",
                    "wireplumber.service": "failed",
                },
            ],
            services,
        )
        capture = _EventCapture()
        monitor = LinuxAudioServiceMonitor(
            services_to_monitor=frozenset(services),
            poll_interval_s=0.001,
            query=query,
        )
        await _drive_polls(
            monitor,
            capture,
            expected_rounds=2,
            query=query,
            services=services,
        )
        # Single DOWN event despite 2 services flipping.
        assert len(capture.events) == 1
        assert capture.events[0].kind is AudioServiceEventKind.DOWN

    @pytest.mark.asyncio()
    async def test_full_recovery_emits_up(self) -> None:
        services = {"pipewire.service"}
        query = _MultiServiceQuery(
            [
                {},  # round 0: active (baseline)
                {"pipewire.service": "inactive"},  # round 1: DOWN
                {"pipewire.service": "active"},  # round 2: UP
            ],
            services,
        )
        capture = _EventCapture()
        monitor = LinuxAudioServiceMonitor(
            services_to_monitor=frozenset(services),
            poll_interval_s=0.001,
            query=query,
        )
        # F2-H04 (audit §3.K) — the production path now gates UP on
        # ``_post_up_health_check`` (pactl info verify). Tests that
        # spawn the real helper would fail on hosts without ``pactl``
        # binary (Windows CI, lean containers). Mock to True so this
        # unit test continues to pin the aggregate-state machine
        # WITHOUT being coupled to the pactl subprocess gate (which
        # has dedicated coverage in
        # ``test_audio_service_linux_post_up.py`` +
        # ``test_pipewire_restart_resilience.py``).
        from unittest.mock import AsyncMock

        monitor._post_up_health_check = AsyncMock(return_value=True)  # type: ignore[method-assign]
        await _drive_polls(
            monitor,
            capture,
            expected_rounds=3,
            query=query,
            services=services,
        )
        assert [e.kind for e in capture.events] == [
            AudioServiceEventKind.DOWN,
            AudioServiceEventKind.UP,
        ]

    @pytest.mark.asyncio()
    async def test_query_none_preserves_prior_aggregate_state(self) -> None:
        # Transient systemctl failure mid-round → aggregate is None →
        # state preserved → no spurious flap.
        services = {"pipewire.service"}
        query = _MultiServiceQuery(
            [
                {},  # round 0: active.
                {"pipewire.service": None},  # round 1: subprocess failure.
                {},  # round 2: active again (inherited).
            ],
            services,
        )
        capture = _EventCapture()
        monitor = LinuxAudioServiceMonitor(
            services_to_monitor=frozenset(services),
            poll_interval_s=0.001,
            query=query,
        )
        await _drive_polls(
            monitor,
            capture,
            expected_rounds=3,
            query=query,
            services=services,
        )
        # No transition — flaky poll did NOT bounce the state.
        assert capture.events == []

    @pytest.mark.asyncio()
    async def test_handler_exception_swallowed(self) -> None:
        services = {"pipewire.service"}
        query = _MultiServiceQuery(
            [
                {},
                {"pipewire.service": "inactive"},
                {"pipewire.service": "active"},
            ],
            services,
        )

        async def _handler(_event: AudioServiceEvent) -> None:
            msg = "downstream blew up"
            raise RuntimeError(msg)

        monitor = LinuxAudioServiceMonitor(
            services_to_monitor=frozenset(services),
            poll_interval_s=0.001,
            query=query,
        )
        # Loop must keep polling despite the handler raising on every event.
        await _drive_polls(
            monitor,
            _handler,
            expected_rounds=3,
            query=query,
            services=services,
        )

    @pytest.mark.asyncio()
    async def test_handler_cancelled_propagates(self) -> None:
        services = {"pipewire.service"}
        query = _MultiServiceQuery(
            [{}, {"pipewire.service": "inactive"}],
            services,
        )

        async def _handler(_event: AudioServiceEvent) -> None:
            raise asyncio.CancelledError

        monitor = LinuxAudioServiceMonitor(
            services_to_monitor=frozenset(services),
            poll_interval_s=0.001,
            query=query,
        )
        await monitor.start(_handler)
        deadline = asyncio.get_event_loop().time() + 2.0
        while monitor._task is not None and not monitor._task.done():  # noqa: SLF001
            if asyncio.get_event_loop().time() > deadline:
                await monitor.stop()
                msg = "Cancellation did not propagate within 2 s"
                raise AssertionError(msg)
            await asyncio.sleep(0.01)
        await monitor.stop()


class TestPipeWireOnlyHostRegression:
    """LINUX-2 end-to-end regression: on a PipeWire-only host the
    monitor must watch ONLY the installed units, so a pipewire death
    fires DOWN and its recovery fires UP. Pre-fix the not-found
    pulseaudio.service ghost was included in the watch set with a
    permanently-"inactive" reading, pinning the aggregate to False and
    making the monitor structurally inert on every real host."""

    @pytest.mark.asyncio()
    async def test_pipewire_death_and_recovery_fire_down_then_up(self) -> None:
        candidates = _AUDIO_SERVICE_CANDIDATES
        load_states = {
            "pipewire.service": "loaded",
            "wireplumber.service": "loaded",
            "pipewire-pulse.service": "loaded",
            "pulseaudio.service": "not-found",  # real systemctl shape
        }
        watched = _probe_existing_services(
            candidates,
            load_state_query=lambda svc: load_states.get(svc),
        )
        assert watched == {
            "pipewire.service",
            "wireplumber.service",
            "pipewire-pulse.service",
        }

        # Real is-active semantics for the poll: the ghost unit would
        # answer "inactive" forever — but it is no longer watched.
        query = _MultiServiceQuery(
            [
                {},  # round 0: baseline, all watched units active.
                {"pipewire.service": "inactive"},  # round 1: daemon died.
                {"pipewire.service": "active"},  # round 2: recovered.
            ],
            set(watched),
        )
        capture = _EventCapture()
        monitor = LinuxAudioServiceMonitor(
            services_to_monitor=frozenset(watched),
            poll_interval_s=0.001,
            query=query,
        )
        from unittest.mock import AsyncMock

        monitor._post_up_health_check = AsyncMock(return_value=True)  # type: ignore[method-assign]
        await _drive_polls(
            monitor,
            capture,
            expected_rounds=3,
            query=query,
            services=set(watched),
        )
        assert [e.kind for e in capture.events] == [
            AudioServiceEventKind.DOWN,
            AudioServiceEventKind.UP,
        ]


# ── Factory ───────────────────────────────────────────────────────────


class TestFactory:
    def test_returns_noop_when_no_services_present(self) -> None:
        with patch(
            "sovyx.voice.health._audio_service_linux._probe_existing_services",
            return_value=set(),
        ):
            monitor = build_linux_audio_service_monitor()
        assert isinstance(monitor, NoopAudioServiceMonitor)

    def test_returns_real_monitor_when_services_present(self) -> None:
        with patch(
            "sovyx.voice.health._audio_service_linux._probe_existing_services",
            return_value={"pipewire.service"},
        ):
            monitor = build_linux_audio_service_monitor()
        assert isinstance(monitor, LinuxAudioServiceMonitor)

    def test_factory_propagates_query_injection_to_real_monitor(self) -> None:
        # When the factory builds the real monitor, the injected
        # is-active query must flow through to the monitor while the
        # injected load-state query drives the installed-probe.
        def _q(_svc: str) -> str:
            return "active"

        monitor = build_linux_audio_service_monitor(
            query=_q,
            load_state_query=lambda _svc: "loaded",
        )
        assert isinstance(monitor, LinuxAudioServiceMonitor)
        assert monitor._query is _q  # noqa: SLF001

    def test_factory_noop_on_ghost_only_host(self) -> None:
        # Every candidate LoadState=not-found (real systemctl shape for
        # a host with no audio units) → Noop.
        monitor = build_linux_audio_service_monitor(
            load_state_query=lambda _svc: "not-found",
        )
        assert isinstance(monitor, NoopAudioServiceMonitor)
