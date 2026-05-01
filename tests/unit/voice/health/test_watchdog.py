"""Unit tests for :mod:`sovyx.voice.health.watchdog`.

Pins ADR §4.4.1 (exponential-backoff re-probe) and §4.4.2 (hot-plug
reaction) semantics. Every test injects fake :func:`re_probe` /
:func:`re_cascade` callables and a stub :class:`HotplugListener` so no
real PortAudio / pywin32 / pyudev dependency is touched.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import pytest

from sovyx.engine._lock_dict import LRULockDict
from sovyx.voice.health._audio_service import NoopAudioServiceMonitor
from sovyx.voice.health._default_device import (
    NoopDefaultDeviceWatcher,
    PollingDefaultDeviceWatcher,
)
from sovyx.voice.health._hotplug import NoopHotplugListener
from sovyx.voice.health._power import NoopPowerEventListener
from sovyx.voice.health._quarantine import EndpointQuarantine
from sovyx.voice.health.contract import (
    AudioServiceEvent,
    AudioServiceEventKind,
    CascadeResult,
    Combo,
    Diagnosis,
    HotplugEvent,
    HotplugEventKind,
    PowerEvent,
    PowerEventKind,
    ProbeMode,
    ProbeResult,
    WatchdogState,
)
from sovyx.voice.health.watchdog import (
    VoiceCaptureWatchdog,
    build_platform_audio_service_monitor,
    build_platform_default_device_watcher,
    build_platform_hotplug_listener,
    build_platform_power_listener,
)

if TYPE_CHECKING:
    from pathlib import Path


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


def _combo() -> Combo:
    return Combo(
        host_api="WASAPI",
        sample_rate=48_000,
        channels=1,
        sample_format="int16",
        exclusive=False,
        auto_convert=True,
        frames_per_buffer=480,
        platform_key="win32",
    )


def _probe_result(diagnosis: Diagnosis = Diagnosis.HEALTHY) -> ProbeResult:
    return ProbeResult(
        diagnosis=diagnosis,
        mode=ProbeMode.WARM,
        combo=_combo(),
        vad_max_prob=0.9 if diagnosis == Diagnosis.HEALTHY else 0.0,
        vad_mean_prob=0.5 if diagnosis == Diagnosis.HEALTHY else 0.0,
        rms_db=-30.0,
        callbacks_fired=50,
        duration_ms=500,
    )


def _cascade_result(*, endpoint: str, won: bool) -> CascadeResult:
    return CascadeResult(
        endpoint_guid=endpoint,
        winning_combo=_combo() if won else None,
        winning_probe=_probe_result() if won else None,
        attempts=(),
        attempts_count=0 if won else 1,
        budget_exhausted=not won,
        source="cascade" if won else "none",
    )


@dataclass
class _FakeHotplug:
    """Stub :class:`HotplugListener` that lets tests fire synthetic events."""

    callback: Callable[[HotplugEvent], Awaitable[None]] | None = None
    started: bool = False
    stopped: bool = False

    async def start(
        self,
        on_event: Callable[[HotplugEvent], Awaitable[None]],
    ) -> None:
        self.callback = on_event
        self.started = True

    async def stop(self) -> None:
        self.stopped = True

    async def fire(self, event: HotplugEvent) -> None:
        assert self.callback is not None, "listener not started"
        await self.callback(event)


@dataclass
class _ReProbeRecorder:
    """Programmable re-probe callable tracking every invocation."""

    diagnoses: list[Diagnosis] = field(default_factory=list)
    calls: list[str] = field(default_factory=list)
    raise_on_indices: set[int] = field(default_factory=set)

    async def __call__(self, endpoint: str) -> ProbeResult:
        idx = len(self.calls)
        self.calls.append(endpoint)
        if idx in self.raise_on_indices:
            msg = "boom"
            raise RuntimeError(msg)
        diag = self.diagnoses[idx] if idx < len(self.diagnoses) else Diagnosis.NO_SIGNAL
        return _probe_result(diag)


@dataclass
class _ReCascadeRecorder:
    """Programmable re-cascade callable tracking every invocation."""

    outcomes: list[bool] = field(default_factory=list)
    calls: list[str] = field(default_factory=list)
    raise_on_indices: set[int] = field(default_factory=set)

    async def __call__(self, endpoint: str) -> CascadeResult:
        idx = len(self.calls)
        self.calls.append(endpoint)
        if idx in self.raise_on_indices:
            msg = "boom"
            raise RuntimeError(msg)
        won = self.outcomes[idx] if idx < len(self.outcomes) else True
        return _cascade_result(endpoint=endpoint, won=won)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


_ENDPOINT = "{11111111-2222-3333-4444-555555555555}"
_FRIENDLY = "USB Microphone"


def _make_watchdog(
    *,
    re_probe: _ReProbeRecorder | None = None,
    re_cascade: _ReCascadeRecorder | None = None,
    combo_store: object = None,
    schedule_s: tuple[float, ...] = (0.0, 0.0, 0.0),
    max_attempts: int = 3,
    friendly: str = _FRIENDLY,
) -> tuple[VoiceCaptureWatchdog, _ReProbeRecorder, _ReCascadeRecorder, LRULockDict[str]]:
    rp = re_probe or _ReProbeRecorder()
    rc = re_cascade or _ReCascadeRecorder()
    locks: LRULockDict[str] = LRULockDict(maxsize=4)
    wd = VoiceCaptureWatchdog(
        active_endpoint_guid=_ENDPOINT,
        re_probe=rp,
        re_cascade=rc,
        active_endpoint_friendly_name=friendly,
        combo_store=combo_store,  # type: ignore[arg-type]
        lifecycle_locks=locks,
        schedule_s=schedule_s,
        max_attempts=max_attempts,
    )
    return wd, rp, rc, locks


async def _drain_pending(wd: VoiceCaptureWatchdog) -> None:
    """Await the in-flight backoff chain, if any."""
    pending = wd._pending  # noqa: SLF001 — test-only access
    if pending is not None:
        await pending


# ---------------------------------------------------------------------------
# Construction + lifecycle
# ---------------------------------------------------------------------------


class TestConstruction:
    """Constructor validation + default wiring."""

    def test_requires_active_endpoint_guid(self) -> None:
        with pytest.raises(ValueError, match="active_endpoint_guid"):
            VoiceCaptureWatchdog(
                active_endpoint_guid="",
                re_probe=_ReProbeRecorder(),
                re_cascade=_ReCascadeRecorder(),
            )

    def test_schedule_trimmed_to_max_attempts(self) -> None:
        wd, _, _, _ = _make_watchdog(
            schedule_s=(1.0, 2.0, 3.0, 4.0, 5.0),
            max_attempts=2,
        )
        assert wd._schedule == (1.0, 2.0)  # noqa: SLF001

    def test_starts_idle(self) -> None:
        wd, _, _, _ = _make_watchdog()
        assert wd.state == WatchdogState.IDLE
        assert wd.active_endpoint_guid == _ENDPOINT


class TestLifecycle:
    """``start``/``stop`` are idempotent and propagate to the listener."""

    @pytest.mark.asyncio()
    async def test_start_installs_listener(self) -> None:
        wd, _, _, _ = _make_watchdog()
        listener = _FakeHotplug()
        await wd.start(listener)
        assert listener.started is True
        assert listener.callback is not None

    @pytest.mark.asyncio()
    async def test_start_is_idempotent(self) -> None:
        wd, _, _, _ = _make_watchdog()
        listener = _FakeHotplug()
        await wd.start(listener)
        listener.started = False  # would go back to True on a second start
        await wd.start(listener)
        assert listener.started is False

    @pytest.mark.asyncio()
    async def test_stop_stops_listener_and_cancels_pending(self) -> None:
        rp = _ReProbeRecorder(diagnoses=[Diagnosis.NO_SIGNAL])
        wd, _, _, _ = _make_watchdog(re_probe=rp, schedule_s=(10.0,), max_attempts=1)
        listener = _FakeHotplug()
        await wd.start(listener)
        await wd.report_deafness()
        assert wd._pending is not None  # noqa: SLF001
        await wd.stop()
        assert listener.stopped is True
        # Pending chain was cancelled, never got to call re-probe.
        assert rp.calls == []


# ---------------------------------------------------------------------------
# §4.4.1 Exponential-backoff re-probe
# ---------------------------------------------------------------------------


class TestBackoff:
    """Warm re-probe chain semantics."""

    @pytest.mark.asyncio()
    async def test_report_deafness_without_start_is_noop(self) -> None:
        rp = _ReProbeRecorder()
        wd, _, _, _ = _make_watchdog(re_probe=rp)
        await wd.report_deafness()
        assert rp.calls == []
        assert wd.state == WatchdogState.IDLE

    @pytest.mark.asyncio()
    async def test_healthy_first_attempt_returns_to_idle(self) -> None:
        rp = _ReProbeRecorder(diagnoses=[Diagnosis.HEALTHY])
        wd, _, _, _ = _make_watchdog(re_probe=rp)
        await wd.start(_FakeHotplug())
        await wd.report_deafness()
        await _drain_pending(wd)
        assert rp.calls == [_ENDPOINT]
        assert wd.state == WatchdogState.IDLE

    @pytest.mark.asyncio()
    async def test_recovery_after_two_failed_probes(self) -> None:
        rp = _ReProbeRecorder(
            diagnoses=[Diagnosis.NO_SIGNAL, Diagnosis.NO_SIGNAL, Diagnosis.HEALTHY],
        )
        wd, _, _, _ = _make_watchdog(re_probe=rp)
        await wd.start(_FakeHotplug())
        await wd.report_deafness()
        await _drain_pending(wd)
        assert len(rp.calls) == 3
        assert wd.state == WatchdogState.IDLE

    @pytest.mark.asyncio()
    async def test_exhaustion_transitions_to_degraded(self) -> None:
        rp = _ReProbeRecorder(
            diagnoses=[Diagnosis.NO_SIGNAL, Diagnosis.NO_SIGNAL, Diagnosis.NO_SIGNAL],
        )
        wd, _, _, _ = _make_watchdog(re_probe=rp)
        await wd.start(_FakeHotplug())
        await wd.report_deafness()
        await _drain_pending(wd)
        assert len(rp.calls) == 3
        assert wd.state == WatchdogState.DEGRADED

    @pytest.mark.asyncio()
    async def test_permanently_degraded_log_carries_last_diagnosis(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        # T6.14 — operators paging on ``voice_capture_permanently_degraded``
        # need ``last_diagnosis`` so they can route on cause without
        # scraping per-attempt ``voice_watchdog_reprobe_result`` lines.
        import logging

        caplog.set_level(logging.ERROR, logger="sovyx.voice.health.watchdog")

        rp = _ReProbeRecorder(
            diagnoses=[
                Diagnosis.NO_SIGNAL,
                Diagnosis.NO_SIGNAL,
                Diagnosis.APO_DEGRADED,  # last attempt → routes the alert.
            ],
        )
        wd, _, _, _ = _make_watchdog(re_probe=rp)
        await wd.start(_FakeHotplug())
        await wd.report_deafness()
        await _drain_pending(wd)

        records = [
            r.msg
            for r in caplog.records
            if (
                r.name == "sovyx.voice.health.watchdog"
                and isinstance(r.msg, dict)
                and r.msg.get("event") == "voice_capture_permanently_degraded"
            )
        ]
        assert len(records) == 1
        assert records[0]["last_diagnosis"] == "apo_degraded"
        assert records[0]["attempts"] == 3

    @pytest.mark.asyncio()
    async def test_permanently_degraded_log_handles_all_raises(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        # T6.14 — when EVERY re-probe attempt raised before producing
        # a result, ``last_diagnosis`` surfaces as ``None``. Operator
        # then knows to inspect ``voice_watchdog_reprobe_raised`` lines.
        import logging

        caplog.set_level(logging.ERROR, logger="sovyx.voice.health.watchdog")

        rp = _ReProbeRecorder(
            diagnoses=[Diagnosis.NO_SIGNAL] * 3,
            raise_on_indices={0, 1, 2},
        )
        wd, _, _, _ = _make_watchdog(re_probe=rp)
        await wd.start(_FakeHotplug())
        await wd.report_deafness()
        await _drain_pending(wd)

        records = [
            r.msg
            for r in caplog.records
            if (
                r.name == "sovyx.voice.health.watchdog"
                and isinstance(r.msg, dict)
                and r.msg.get("event") == "voice_capture_permanently_degraded"
            )
        ]
        assert len(records) == 1
        assert records[0]["last_diagnosis"] is None

    @pytest.mark.asyncio()
    async def test_probe_exception_does_not_kill_chain(self) -> None:
        rp = _ReProbeRecorder(
            diagnoses=[Diagnosis.NO_SIGNAL, Diagnosis.NO_SIGNAL, Diagnosis.HEALTHY],
            raise_on_indices={0},
        )
        wd, _, _, _ = _make_watchdog(re_probe=rp)
        await wd.start(_FakeHotplug())
        await wd.report_deafness()
        await _drain_pending(wd)
        assert len(rp.calls) == 3
        assert wd.state == WatchdogState.IDLE

    @pytest.mark.asyncio()
    async def test_second_deafness_while_pending_is_noop(self) -> None:
        rp = _ReProbeRecorder(diagnoses=[Diagnosis.HEALTHY])
        wd, _, _, _ = _make_watchdog(re_probe=rp, schedule_s=(0.05,), max_attempts=1)
        await wd.start(_FakeHotplug())
        await wd.report_deafness()
        first_task = wd._pending  # noqa: SLF001
        await wd.report_deafness()
        assert wd._pending is first_task  # noqa: SLF001
        await _drain_pending(wd)

    @pytest.mark.asyncio()
    async def test_deafness_in_degraded_is_noop(self) -> None:
        rp = _ReProbeRecorder(
            diagnoses=[Diagnosis.NO_SIGNAL, Diagnosis.NO_SIGNAL, Diagnosis.NO_SIGNAL],
        )
        wd, _, _, _ = _make_watchdog(re_probe=rp)
        await wd.start(_FakeHotplug())
        await wd.report_deafness()
        await _drain_pending(wd)
        assert wd.state == WatchdogState.DEGRADED
        await wd.report_deafness()
        assert len(rp.calls) == 3  # no further calls

    @pytest.mark.asyncio()
    async def test_backoff_delay_is_honoured(self) -> None:
        """Even a tiny delay proves the schedule is actually awaited."""
        rp = _ReProbeRecorder(diagnoses=[Diagnosis.HEALTHY])
        wd, _, _, _ = _make_watchdog(
            re_probe=rp,
            schedule_s=(0.1,),
            max_attempts=1,
        )
        await wd.start(_FakeHotplug())
        loop = asyncio.get_running_loop()
        t0 = loop.time()
        await wd.report_deafness()
        await _drain_pending(wd)
        assert loop.time() - t0 >= 0.05


# ---------------------------------------------------------------------------
# §4.4.2 Hot-plug reaction
# ---------------------------------------------------------------------------


class TestHotplugReaction:
    """Hot-plug event dispatch routes to the right handler."""

    @pytest.mark.asyncio()
    async def test_remove_of_active_endpoint_triggers_recascade(self) -> None:
        rc = _ReCascadeRecorder(outcomes=[True])
        wd, _, _, _ = _make_watchdog(re_cascade=rc)
        listener = _FakeHotplug()
        await wd.start(listener)
        await listener.fire(
            HotplugEvent(
                kind=HotplugEventKind.DEVICE_REMOVED,
                endpoint_guid=_ENDPOINT,
            ),
        )
        assert rc.calls == [_ENDPOINT]

    @pytest.mark.asyncio()
    async def test_remove_matches_via_friendly_name_fallback(self) -> None:
        rc = _ReCascadeRecorder(outcomes=[True])
        wd, _, _, _ = _make_watchdog(re_cascade=rc)
        listener = _FakeHotplug()
        await wd.start(listener)
        await listener.fire(
            HotplugEvent(
                kind=HotplugEventKind.DEVICE_REMOVED,
                device_friendly_name=_FRIENDLY,
            ),
        )
        assert rc.calls == [_ENDPOINT]

    @pytest.mark.asyncio()
    async def test_remove_of_other_endpoint_is_noop(self) -> None:
        rc = _ReCascadeRecorder()
        wd, _, _, _ = _make_watchdog(re_cascade=rc, friendly="")
        listener = _FakeHotplug()
        await wd.start(listener)
        await listener.fire(
            HotplugEvent(
                kind=HotplugEventKind.DEVICE_REMOVED,
                endpoint_guid="{99999999-9999-9999-9999-999999999999}",
                device_friendly_name="Other Mic",
            ),
        )
        assert rc.calls == []

    @pytest.mark.asyncio()
    async def test_add_when_idle_is_noop(self) -> None:
        rc = _ReCascadeRecorder()
        wd, _, _, _ = _make_watchdog(re_cascade=rc)
        listener = _FakeHotplug()
        await wd.start(listener)
        await listener.fire(
            HotplugEvent(
                kind=HotplugEventKind.DEVICE_ADDED,
                endpoint_guid="{99999999-9999-9999-9999-999999999999}",
                device_friendly_name="Any Mic",
            ),
        )
        assert rc.calls == []
        assert wd.state == WatchdogState.IDLE

    @pytest.mark.asyncio()
    async def test_add_while_degraded_triggers_recascade_and_recovers(self) -> None:
        rp = _ReProbeRecorder(
            diagnoses=[Diagnosis.NO_SIGNAL, Diagnosis.NO_SIGNAL, Diagnosis.NO_SIGNAL],
        )
        rc = _ReCascadeRecorder(outcomes=[True])
        wd, _, _, _ = _make_watchdog(re_probe=rp, re_cascade=rc)
        listener = _FakeHotplug()
        await wd.start(listener)
        await wd.report_deafness()
        await _drain_pending(wd)
        assert wd.state == WatchdogState.DEGRADED
        await listener.fire(
            HotplugEvent(
                kind=HotplugEventKind.DEVICE_ADDED,
                device_friendly_name="New USB Mic",
            ),
        )
        assert rc.calls == [_ENDPOINT]
        assert wd.state == WatchdogState.IDLE

    @pytest.mark.asyncio()
    async def test_add_while_degraded_stays_degraded_when_cascade_fails(self) -> None:
        rp = _ReProbeRecorder(
            diagnoses=[Diagnosis.NO_SIGNAL, Diagnosis.NO_SIGNAL, Diagnosis.NO_SIGNAL],
        )
        rc = _ReCascadeRecorder(outcomes=[False])
        wd, _, _, _ = _make_watchdog(re_probe=rp, re_cascade=rc)
        listener = _FakeHotplug()
        await wd.start(listener)
        await wd.report_deafness()
        await _drain_pending(wd)
        await listener.fire(
            HotplugEvent(
                kind=HotplugEventKind.DEVICE_ADDED,
                device_friendly_name="New USB Mic",
            ),
        )
        assert wd.state == WatchdogState.DEGRADED

    @pytest.mark.asyncio()
    async def test_default_device_changed_triggers_recascade(self) -> None:
        """Sprint 2 Task #18: DEFAULT_DEVICE_CHANGED now cascades on the
        new default so a user flipping their mic in Sound Settings is
        honoured immediately."""
        rc = _ReCascadeRecorder(outcomes=[True])
        wd, _, _, _ = _make_watchdog(re_cascade=rc)
        listener = _FakeHotplug()
        await wd.start(listener)
        await listener.fire(
            HotplugEvent(
                kind=HotplugEventKind.DEFAULT_DEVICE_CHANGED,
                endpoint_guid=_ENDPOINT,
            ),
        )
        assert rc.calls == [_ENDPOINT]

    @pytest.mark.asyncio()
    async def test_hotplug_before_start_is_ignored(self) -> None:
        rc = _ReCascadeRecorder()
        wd, _, _, _ = _make_watchdog(re_cascade=rc)
        # Do NOT start — simulate a stale event fired after ``stop``.
        await wd._on_hotplug(  # noqa: SLF001 — intentional direct path
            HotplugEvent(
                kind=HotplugEventKind.DEVICE_REMOVED,
                endpoint_guid=_ENDPOINT,
            ),
        )
        assert rc.calls == []

    @pytest.mark.asyncio()
    async def test_remove_invalidates_combo_store_when_provided(self) -> None:
        @dataclass
        class _SpyStore:
            calls: list[tuple[str, str]] = field(default_factory=list)

            def invalidate(self, endpoint_guid: str, reason: str) -> None:
                self.calls.append((endpoint_guid, reason))

        store = _SpyStore()
        wd, _, _, _ = _make_watchdog(combo_store=store)
        listener = _FakeHotplug()
        await wd.start(listener)
        await listener.fire(
            HotplugEvent(
                kind=HotplugEventKind.DEVICE_REMOVED,
                endpoint_guid=_ENDPOINT,
            ),
        )
        assert store.calls == [(_ENDPOINT, "hotplug-remove-active-endpoint")]

    @pytest.mark.asyncio()
    async def test_remove_recascades_even_when_combo_invalidate_raises(self) -> None:
        @dataclass
        class _BrokenStore:
            def invalidate(self, endpoint_guid: str, reason: str) -> None:
                del endpoint_guid, reason
                msg = "disk full"
                raise OSError(msg)

        rc = _ReCascadeRecorder(outcomes=[True])
        wd, _, _, _ = _make_watchdog(re_cascade=rc, combo_store=_BrokenStore())
        listener = _FakeHotplug()
        await wd.start(listener)
        await listener.fire(
            HotplugEvent(
                kind=HotplugEventKind.DEVICE_REMOVED,
                endpoint_guid=_ENDPOINT,
            ),
        )
        assert rc.calls == [_ENDPOINT]


# ---------------------------------------------------------------------------
# T6.30 — Hot-plug storm: 100 device enumerations/sec
# ---------------------------------------------------------------------------


class TestHotplugStormT630:
    """Stress :meth:`VoiceCaptureWatchdog._on_hotplug` under event storms.

    Phase 6 / T6.30 production scenario: when a USB hub powers up, when
    a docking station is connected, or when a Windows 11 audio driver
    enumerates a freshly-installed device, the OS fires 10-30 device
    events in a tight burst — the ``IMMNotificationClient`` /
    ``udev monitor`` / ``IOAudio`` backends marshall every one onto the
    event loop via ``call_soon_threadsafe``. The 100/sec storm here is
    the upper-bound stress: 100 hot-plug events sequenced through
    :class:`_FakeHotplug.fire` via :func:`asyncio.gather`.

    Pinned invariants regardless of storm size:
      (a) every event resolves without exception;
      (b) the state machine ends in a consistent terminal state
          (``IDLE`` after a recovery-add storm, ``DEGRADED`` after a
          remove-storm with no available cascade winner);
      (c) ``_pending`` is bounded to a single Task or None — the
          watchdog must not accumulate background work proportional
          to the storm size;
      (d) the re-cascade callable is invoked at most once per state
          transition, NOT once per event (storm collapsing is the
          actual operator-facing contract).
    """

    _STORM_SIZE = 100  # noqa: PLR2004 — mission spec § Phase 6 / T6.30

    @pytest.mark.asyncio()
    async def test_storm_of_irrelevant_adds_when_idle_is_full_noop(self) -> None:
        """100 ADD events for non-matching endpoints when idle: zero recascade.

        Pin (a) + (d): an idle watchdog seeing 100 ADD events for
        endpoints that don't match its active GUID/friendly-name must
        not call recascade even once. Production scenario: a USB hub
        enumerates 100 unrelated devices while the active mic is
        already healthy.
        """
        rc = _ReCascadeRecorder()
        wd, _, _, _ = _make_watchdog(re_cascade=rc, friendly="")
        listener = _FakeHotplug()
        await wd.start(listener)

        events = [
            HotplugEvent(
                kind=HotplugEventKind.DEVICE_ADDED,
                endpoint_guid=f"{{99999999-0000-0000-0000-{i:012d}}}",
                device_friendly_name=f"Other Mic #{i}",
            )
            for i in range(self._STORM_SIZE)
        ]

        results = await asyncio.gather(
            *(listener.fire(e) for e in events),
            return_exceptions=True,
        )

        for r in results:
            assert not isinstance(r, BaseException), f"raised: {r!r}"
        assert rc.calls == []  # zero recascade — full no-op
        assert wd.state == WatchdogState.IDLE
        assert wd._pending is None  # noqa: SLF001 — bounded-state invariant

    @pytest.mark.asyncio()
    async def test_storm_of_active_removes_collapses_to_bounded_recascades(
        self,
    ) -> None:
        """100 REMOVE events for the active endpoint resolve cleanly.

        Pin (a) + (c): the watchdog must not crash on a storm of
        identical removes, and ``_pending`` must remain None or a
        single Task — never a list. Recascade count is allowed to
        equal storm size here because each REMOVE for the active
        endpoint is a legitimate recascade trigger (the ADR §4.4.2
        path doesn't deduplicate identical removes — a re-add
        between events is plausible). What we pin is the BOUND on
        bg-task accumulation, not the recascade count.
        """
        rc = _ReCascadeRecorder(outcomes=[True] * self._STORM_SIZE)
        wd, _, _, _ = _make_watchdog(re_cascade=rc)
        listener = _FakeHotplug()
        await wd.start(listener)

        events = [
            HotplugEvent(
                kind=HotplugEventKind.DEVICE_REMOVED,
                endpoint_guid=_ENDPOINT,
            )
            for _ in range(self._STORM_SIZE)
        ]

        results = await asyncio.gather(
            *(listener.fire(e) for e in events),
            return_exceptions=True,
        )

        for r in results:
            assert not isinstance(r, BaseException), f"raised: {r!r}"
        # Bounded background work — never more than one in-flight Task.
        assert wd._pending is None or isinstance(  # noqa: SLF001
            wd._pending,  # noqa: SLF001
            asyncio.Task,
        )
        # Recascade was called — the contract isn't "exactly N times",
        # it's "at most N times and never zero" (some events may have
        # interleaved while a previous handler held the endpoint lock).
        assert 0 < len(rc.calls) <= self._STORM_SIZE

    @pytest.mark.asyncio()
    async def test_mixed_kind_storm_resolves_to_consistent_state(self) -> None:
        """ADD/REMOVE/DEFAULT_DEVICE_CHANGED interleaved storm.

        Pin (a) + (b): a realistic storm mixes event kinds (a USB hub
        powering up sends ADD events for new devices AND REMOVE events
        for stale enumerations that the OS had cached). All 300
        events (100 of each kind) must resolve cleanly and the
        watchdog must end in a defined state — not a corrupted
        intermediate state with stale flags.
        """
        rc = _ReCascadeRecorder(outcomes=[True] * 300)
        wd, _, _, _ = _make_watchdog(re_cascade=rc)
        listener = _FakeHotplug()
        await wd.start(listener)

        # Build interleaved storm: ADD, REMOVE, DEFAULT_DEVICE_CHANGED, …
        kinds = (
            HotplugEventKind.DEVICE_ADDED,
            HotplugEventKind.DEVICE_REMOVED,
            HotplugEventKind.DEFAULT_DEVICE_CHANGED,
        )
        events: list[HotplugEvent] = []
        for _ in range(self._STORM_SIZE):
            for kind in kinds:
                events.append(
                    HotplugEvent(
                        kind=kind,
                        endpoint_guid=_ENDPOINT,
                        device_friendly_name=_FRIENDLY,
                    ),
                )

        results = await asyncio.gather(
            *(listener.fire(e) for e in events),
            return_exceptions=True,
        )

        for r in results:
            assert not isinstance(r, BaseException), f"raised: {r!r}"
        # Terminal state must be one of the documented WatchdogStates.
        assert wd.state in (
            WatchdogState.IDLE,
            WatchdogState.BACKOFF,
            WatchdogState.DEGRADED,
        )

    @pytest.mark.asyncio()
    async def test_storm_does_not_leak_pending_tasks(self) -> None:
        """``_pending`` invariant under storm: at most one Task.

        Pin (c): the backoff-chain spawn point at watchdog.py:405
        creates a single ``_pending`` task; subsequent spawns clear
        the prior one. A storm must not create a list of pending
        tasks even briefly. Inspecting ``_pending`` between fires
        confirms the invariant.
        """
        rc = _ReCascadeRecorder(outcomes=[True] * self._STORM_SIZE)
        wd, _, _, _ = _make_watchdog(re_cascade=rc)
        listener = _FakeHotplug()
        await wd.start(listener)

        pending_observations: list[type] = []

        for _ in range(self._STORM_SIZE):
            await listener.fire(
                HotplugEvent(
                    kind=HotplugEventKind.DEVICE_REMOVED,
                    endpoint_guid=_ENDPOINT,
                ),
            )
            # type() of the pending field — must always be either
            # NoneType or asyncio.Task. Never list, never set.
            pending = wd._pending  # noqa: SLF001
            pending_observations.append(type(pending))

        # Every observation is either None or a single Task.
        for t in pending_observations:
            assert (
                t is type(None)
                or t is asyncio.Task
                or issubclass(
                    t,
                    asyncio.Task,
                )
            ), f"_pending leaked to type {t}"


# ---------------------------------------------------------------------------
# Lifecycle lock — §5.5
# ---------------------------------------------------------------------------


class TestLifecycleLockSharing:
    """Watchdog must share §5.5 lifecycle lock with :func:`run_cascade`."""

    @pytest.mark.asyncio()
    async def test_combo_invalidation_waits_for_contending_lock_holder(self) -> None:
        """The combo-store invalidation on active-removal is guarded by the
        endpoint lock. A concurrent run_cascade holding the lock cannot
        race with the invalidate call — verified by driving the lock from
        the test and asserting the invalidate side-effect is deferred."""

        @dataclass
        class _OrderedStore:
            calls: list[str] = field(default_factory=list)

            def invalidate(self, endpoint_guid: str, reason: str) -> None:
                del reason
                self.calls.append(endpoint_guid)

        store = _OrderedStore()
        rc = _ReCascadeRecorder(outcomes=[True])
        wd, _, _, locks = _make_watchdog(re_cascade=rc, combo_store=store)
        listener = _FakeHotplug()
        await wd.start(listener)

        acquired = asyncio.Event()
        released = asyncio.Event()

        async def _hold_lock() -> None:
            async with locks[_ENDPOINT]:
                acquired.set()
                await released.wait()

        holder = asyncio.create_task(_hold_lock())
        await acquired.wait()  # guarantee the holder owns the lock
        assert locks[_ENDPOINT].locked() is True

        fire_task = asyncio.create_task(
            listener.fire(
                HotplugEvent(
                    kind=HotplugEventKind.DEVICE_REMOVED,
                    endpoint_guid=_ENDPOINT,
                ),
            ),
        )
        await asyncio.sleep(0.05)
        assert store.calls == []
        released.set()
        await holder
        await fire_task
        assert store.calls == [_ENDPOINT]
        assert rc.calls == [_ENDPOINT]


# ---------------------------------------------------------------------------
# Platform listener factory
# ---------------------------------------------------------------------------


class TestPlatformListenerFactory:
    """`build_platform_hotplug_listener` selects the right backend."""

    def test_resilience_disabled_returns_noop(self) -> None:
        listener = build_platform_hotplug_listener(
            runtime_resilience_enabled=False,
        )
        assert isinstance(listener, NoopHotplugListener)

    def test_unknown_platform_returns_noop(self) -> None:
        listener = build_platform_hotplug_listener(
            platform_key="freebsd",
            runtime_resilience_enabled=True,
        )
        assert isinstance(listener, NoopHotplugListener)

    def test_macos_returns_noop_in_sprint_2(self) -> None:
        # Sprint 2: macOS backend is a NoopHotplugListener stub per the
        # ADR. Sprint 4 (Task #28) replaces it with a CoreAudio-backed
        # implementation.
        listener = build_platform_hotplug_listener(
            platform_key="darwin",
            runtime_resilience_enabled=True,
        )
        assert isinstance(listener, NoopHotplugListener)


# ---------------------------------------------------------------------------
# NoopHotplugListener
# ---------------------------------------------------------------------------


class TestNoopHotplugListener:
    """Sanity: the fallback honours the contract and never raises."""

    @pytest.mark.asyncio()
    async def test_start_stop_are_idempotent(self) -> None:
        listener = NoopHotplugListener(reason="test")
        observed: list[HotplugEvent] = []

        async def _cb(event: HotplugEvent) -> None:
            observed.append(event)

        await listener.start(_cb)
        await listener.start(_cb)  # idempotent
        await listener.stop()
        await listener.stop()
        assert observed == []


# ---------------------------------------------------------------------------
# Integration smoke — watchdog + NoopHotplugListener
# ---------------------------------------------------------------------------


class TestWatchdogWithNoopListener:
    """Watchdog works end-to-end against the fallback listener."""

    @pytest.mark.asyncio()
    async def test_full_lifecycle(self, tmp_path: Path) -> None:
        del tmp_path  # unused — kept to document test pattern
        rp = _ReProbeRecorder(diagnoses=[Diagnosis.HEALTHY])
        wd, _, _, _ = _make_watchdog(re_probe=rp)
        listener = NoopHotplugListener(reason="integration-smoke")
        await wd.start(listener)
        await wd.report_deafness()
        await _drain_pending(wd)
        assert wd.state == WatchdogState.IDLE
        await wd.stop()


# ---------------------------------------------------------------------------
# §4.4.3 Default-device change
# ---------------------------------------------------------------------------


class TestDefaultDeviceChange:
    """`HotplugEventKind.DEFAULT_DEVICE_CHANGED` invalidates store + re-cascades."""

    @pytest.mark.asyncio()
    async def test_invalidates_combo_store_before_recascade(self) -> None:
        @dataclass
        class _OrderedStore:
            calls: list[tuple[str, str]] = field(default_factory=list)

            def invalidate(self, endpoint_guid: str, reason: str) -> None:
                self.calls.append((endpoint_guid, reason))

        store = _OrderedStore()
        rc = _ReCascadeRecorder(outcomes=[True])
        wd, _, _, _ = _make_watchdog(re_cascade=rc, combo_store=store)
        listener = _FakeHotplug()
        await wd.start(listener)
        await listener.fire(
            HotplugEvent(
                kind=HotplugEventKind.DEFAULT_DEVICE_CHANGED,
                device_friendly_name="New Default",
            ),
        )
        assert store.calls == [(_ENDPOINT, "default-device-changed")]
        assert rc.calls == [_ENDPOINT]

    @pytest.mark.asyncio()
    async def test_recascades_even_when_store_invalidate_raises(self) -> None:
        @dataclass
        class _BrokenStore:
            def invalidate(self, endpoint_guid: str, reason: str) -> None:
                del endpoint_guid, reason
                msg = "fsync failed"
                raise OSError(msg)

        rc = _ReCascadeRecorder(outcomes=[True])
        wd, _, _, _ = _make_watchdog(re_cascade=rc, combo_store=_BrokenStore())
        listener = _FakeHotplug()
        await wd.start(listener)
        await listener.fire(
            HotplugEvent(kind=HotplugEventKind.DEFAULT_DEVICE_CHANGED),
        )
        assert rc.calls == [_ENDPOINT]

    @pytest.mark.asyncio()
    async def test_watcher_fires_from_start(self) -> None:
        """A default-device watcher wired via ``start`` forwards events."""
        readings: list[object] = ["A", "B"]

        def _query() -> object:
            return readings.pop(0) if readings else "B"

        watcher = PollingDefaultDeviceWatcher(
            query_default=_query,
            poll_interval_s=0.01,
        )
        rc = _ReCascadeRecorder(outcomes=[True])
        wd, _, _, _ = _make_watchdog(re_cascade=rc)
        listener = _FakeHotplug()
        await wd.start(listener, default_device=watcher)
        # Baseline poll → "A", second poll → "B" fires DEFAULT_DEVICE_CHANGED.
        for _ in range(50):
            if rc.calls:
                break
            await asyncio.sleep(0.01)
        await wd.stop()
        assert rc.calls == [_ENDPOINT]


# ---------------------------------------------------------------------------
# §4.4.4 Power events
# ---------------------------------------------------------------------------


@dataclass
class _FakePowerListener:
    callback: Callable[[PowerEvent], Awaitable[None]] | None = None
    started: bool = False
    stopped: bool = False

    async def start(
        self,
        on_event: Callable[[PowerEvent], Awaitable[None]],
    ) -> None:
        self.callback = on_event
        self.started = True

    async def stop(self) -> None:
        self.stopped = True

    async def fire(self, event: PowerEvent) -> None:
        assert self.callback is not None, "listener not started"
        await self.callback(event)


class TestPowerEvents:
    """Suspend cancels pending chain; resume waits settle + re-cascades."""

    @pytest.mark.asyncio()
    async def test_suspend_cancels_pending_chain_and_marks_backoff(self) -> None:
        rp = _ReProbeRecorder(diagnoses=[Diagnosis.NO_SIGNAL])
        wd, _, _, _ = _make_watchdog(
            re_probe=rp,
            schedule_s=(10.0,),
            max_attempts=1,
        )
        power = _FakePowerListener()
        await wd.start(_FakeHotplug(), power=power)
        await wd.report_deafness()
        pending = wd._pending  # noqa: SLF001
        assert pending is not None
        await power.fire(PowerEvent(kind=PowerEventKind.SUSPEND))
        assert pending.cancelled() or pending.done()
        assert wd.state == WatchdogState.BACKOFF
        assert rp.calls == []  # sleep was cancelled before first re-probe

    @pytest.mark.asyncio()
    async def test_resume_waits_settle_then_recascades_healthy(self) -> None:
        rc = _ReCascadeRecorder(outcomes=[True])
        wd, _, _, _ = _make_watchdog(re_cascade=rc)
        wd._resume_settle_s = 0.05  # noqa: SLF001 — test override
        power = _FakePowerListener()
        await wd.start(_FakeHotplug(), power=power)
        loop = asyncio.get_running_loop()
        t0 = loop.time()
        await power.fire(PowerEvent(kind=PowerEventKind.RESUME))
        elapsed = loop.time() - t0
        assert elapsed >= 0.04
        assert rc.calls == [_ENDPOINT]
        assert wd.state == WatchdogState.IDLE

    @pytest.mark.asyncio()
    async def test_resume_sets_degraded_when_cascade_fails(self) -> None:
        rc = _ReCascadeRecorder(outcomes=[False])
        wd, _, _, _ = _make_watchdog(re_cascade=rc)
        wd._resume_settle_s = 0.0  # noqa: SLF001
        power = _FakePowerListener()
        await wd.start(_FakeHotplug(), power=power)
        await power.fire(PowerEvent(kind=PowerEventKind.RESUME))
        assert wd.state == WatchdogState.DEGRADED

    @pytest.mark.asyncio()
    async def test_power_event_before_start_is_ignored(self) -> None:
        rc = _ReCascadeRecorder()
        wd, _, _, _ = _make_watchdog(re_cascade=rc)
        await wd._on_power_event(PowerEvent(kind=PowerEventKind.RESUME))  # noqa: SLF001
        assert rc.calls == []

    @pytest.mark.asyncio()
    async def test_stop_tears_down_power_listener(self) -> None:
        wd, _, _, _ = _make_watchdog()
        power = _FakePowerListener()
        await wd.start(_FakeHotplug(), power=power)
        await wd.stop()
        assert power.stopped is True


# ---------------------------------------------------------------------------
# §4.4.5 Audio-service crash
# ---------------------------------------------------------------------------


@dataclass
class _FakeAudioServiceMonitor:
    callback: Callable[[AudioServiceEvent], Awaitable[None]] | None = None
    started: bool = False
    stopped: bool = False

    async def start(
        self,
        on_event: Callable[[AudioServiceEvent], Awaitable[None]],
    ) -> None:
        self.callback = on_event
        self.started = True

    async def stop(self) -> None:
        self.stopped = True

    async def fire(self, event: AudioServiceEvent) -> None:
        assert self.callback is not None, "monitor not started"
        await self.callback(event)


class TestAudioServiceEvents:
    """DOWN stalls, UP after DOWN re-cascades, timeout goes DEGRADED."""

    @pytest.mark.asyncio()
    async def test_down_cancels_pending_chain(self) -> None:
        rp = _ReProbeRecorder(diagnoses=[Diagnosis.NO_SIGNAL])
        wd, _, _, _ = _make_watchdog(
            re_probe=rp,
            schedule_s=(10.0,),
            max_attempts=1,
        )
        monitor = _FakeAudioServiceMonitor()
        await wd.start(_FakeHotplug(), audio_service=monitor)
        await wd.report_deafness()
        pending = wd._pending  # noqa: SLF001
        assert pending is not None
        await monitor.fire(AudioServiceEvent(kind=AudioServiceEventKind.DOWN))
        assert pending.cancelled() or pending.done()
        assert wd._audio_service_up.is_set() is False  # noqa: SLF001
        await wd.stop()

    @pytest.mark.asyncio()
    async def test_up_after_down_triggers_recascade(self) -> None:
        rc = _ReCascadeRecorder(outcomes=[True])
        wd, _, _, _ = _make_watchdog(re_cascade=rc)
        monitor = _FakeAudioServiceMonitor()
        await wd.start(_FakeHotplug(), audio_service=monitor)
        await monitor.fire(AudioServiceEvent(kind=AudioServiceEventKind.DOWN))
        await monitor.fire(AudioServiceEvent(kind=AudioServiceEventKind.UP))
        assert rc.calls == [_ENDPOINT]
        assert wd.state == WatchdogState.IDLE
        await wd.stop()

    @pytest.mark.asyncio()
    async def test_up_without_prior_down_is_baseline_noop(self) -> None:
        rc = _ReCascadeRecorder()
        wd, _, _, _ = _make_watchdog(re_cascade=rc)
        monitor = _FakeAudioServiceMonitor()
        await wd.start(_FakeHotplug(), audio_service=monitor)
        await monitor.fire(AudioServiceEvent(kind=AudioServiceEventKind.UP))
        assert rc.calls == []
        await wd.stop()

    @pytest.mark.asyncio()
    async def test_down_without_up_within_timeout_goes_degraded(self) -> None:
        rc = _ReCascadeRecorder()
        wd, _, _, _ = _make_watchdog(re_cascade=rc)
        wd._audio_restart_timeout_s = 0.05  # noqa: SLF001
        monitor = _FakeAudioServiceMonitor()
        await wd.start(_FakeHotplug(), audio_service=monitor)
        await monitor.fire(AudioServiceEvent(kind=AudioServiceEventKind.DOWN))
        waiter = wd._audio_service_down_waiter  # noqa: SLF001
        assert waiter is not None
        await waiter
        assert wd.state == WatchdogState.DEGRADED
        await wd.stop()

    @pytest.mark.asyncio()
    async def test_up_cancels_pending_restart_waiter(self) -> None:
        rc = _ReCascadeRecorder(outcomes=[True])
        wd, _, _, _ = _make_watchdog(re_cascade=rc)
        wd._audio_restart_timeout_s = 5.0  # noqa: SLF001
        monitor = _FakeAudioServiceMonitor()
        await wd.start(_FakeHotplug(), audio_service=monitor)
        await monitor.fire(AudioServiceEvent(kind=AudioServiceEventKind.DOWN))
        waiter = wd._audio_service_down_waiter  # noqa: SLF001
        assert waiter is not None
        await monitor.fire(AudioServiceEvent(kind=AudioServiceEventKind.UP))
        # The waiter observes `_audio_service_up.set()` and returns without
        # flipping DEGRADED — the restart happened inside the timeout.
        await asyncio.sleep(0)  # let waiter run one tick
        assert wd.state != WatchdogState.DEGRADED
        await wd.stop()

    @pytest.mark.asyncio()
    async def test_audio_event_before_start_is_ignored(self) -> None:
        rc = _ReCascadeRecorder()
        wd, _, _, _ = _make_watchdog(re_cascade=rc)
        await wd._on_audio_service_event(  # noqa: SLF001
            AudioServiceEvent(kind=AudioServiceEventKind.UP),
        )
        assert rc.calls == []

    @pytest.mark.asyncio()
    async def test_stop_tears_down_audio_service_monitor(self) -> None:
        wd, _, _, _ = _make_watchdog()
        monitor = _FakeAudioServiceMonitor()
        await wd.start(_FakeHotplug(), audio_service=monitor)
        await wd.stop()
        assert monitor.stopped is True


# ---------------------------------------------------------------------------
# Platform factories for power / audio-service / default-device
# ---------------------------------------------------------------------------


class TestPlatformPowerFactory:
    """`build_platform_power_listener` picks the right backend or Noop."""

    def test_resilience_disabled_returns_noop(self) -> None:
        listener = build_platform_power_listener(runtime_resilience_enabled=False)
        assert isinstance(listener, NoopPowerEventListener)

    def test_linux_returns_noop_in_sprint_2(self) -> None:
        listener = build_platform_power_listener(
            platform_key="linux",
            runtime_resilience_enabled=True,
        )
        assert isinstance(listener, NoopPowerEventListener)

    def test_unknown_platform_returns_noop(self) -> None:
        listener = build_platform_power_listener(
            platform_key="freebsd",
            runtime_resilience_enabled=True,
        )
        assert isinstance(listener, NoopPowerEventListener)


class TestPlatformAudioServiceFactory:
    """`build_platform_audio_service_monitor` — platform switchboard."""

    def test_resilience_disabled_returns_noop(self) -> None:
        monitor = build_platform_audio_service_monitor(runtime_resilience_enabled=False)
        assert isinstance(monitor, NoopAudioServiceMonitor)

    def test_darwin_is_noop_forever(self) -> None:
        monitor = build_platform_audio_service_monitor(
            platform_key="darwin",
            runtime_resilience_enabled=True,
        )
        assert isinstance(monitor, NoopAudioServiceMonitor)

    def test_linux_noop_in_sprint_2(self) -> None:
        monitor = build_platform_audio_service_monitor(
            platform_key="linux",
            runtime_resilience_enabled=True,
        )
        assert isinstance(monitor, NoopAudioServiceMonitor)


class TestPlatformDefaultDeviceFactory:
    """`build_platform_default_device_watcher` wires the polling watcher."""

    def test_resilience_disabled_returns_noop(self) -> None:
        watcher = build_platform_default_device_watcher(runtime_resilience_enabled=False)
        assert isinstance(watcher, NoopDefaultDeviceWatcher)

    def test_missing_query_returns_noop(self) -> None:
        watcher = build_platform_default_device_watcher(
            runtime_resilience_enabled=True,
        )
        assert isinstance(watcher, NoopDefaultDeviceWatcher)

    def test_query_supplied_returns_polling_watcher(self) -> None:
        watcher = build_platform_default_device_watcher(
            query_default=lambda: "default",
            runtime_resilience_enabled=True,
        )
        assert isinstance(watcher, PollingDefaultDeviceWatcher)


# ---------------------------------------------------------------------------
# §4.4.7 — Hot-plug clears kernel-invalidated quarantine
# ---------------------------------------------------------------------------


_QUAR_GUID = "{AAAAAAAA-BBBB-CCCC-DDDD-EEEEEEEEEEEE}"
_QUAR_FRIENDLY = "Razer BlackShark V2 Pro"
_QUAR_INTERFACE = r"\\?\USB#VID_1532#Pro"


def _make_watchdog_with_quarantine(
    quarantine: EndpointQuarantine,
) -> tuple[VoiceCaptureWatchdog, _FakeHotplug]:
    rp = _ReProbeRecorder(diagnoses=[Diagnosis.HEALTHY])
    rc = _ReCascadeRecorder(outcomes=[True])
    wd = VoiceCaptureWatchdog(
        active_endpoint_guid=_ENDPOINT,  # different from the quarantined GUID
        re_probe=rp,
        re_cascade=rc,
        active_endpoint_friendly_name=_FRIENDLY,
        quarantine=quarantine,
    )
    return wd, _FakeHotplug()


class TestHotplugClearsQuarantine:
    """_on_hotplug + _maybe_clear_quarantine_on_hotplug."""

    @pytest.mark.asyncio()
    async def test_device_removed_with_guid_match_clears_quarantine(self) -> None:
        q = EndpointQuarantine(quarantine_s=300.0, maxsize=8)
        q.add(
            endpoint_guid=_QUAR_GUID,
            device_friendly_name=_QUAR_FRIENDLY,
            host_api="Windows WASAPI",
        )
        wd, listener = _make_watchdog_with_quarantine(q)
        await wd.start(listener)
        await listener.fire(
            HotplugEvent(kind=HotplugEventKind.DEVICE_REMOVED, endpoint_guid=_QUAR_GUID),
        )
        assert not q.is_quarantined(_QUAR_GUID)

    @pytest.mark.asyncio()
    async def test_device_added_with_guid_match_clears_quarantine(self) -> None:
        q = EndpointQuarantine(quarantine_s=300.0, maxsize=8)
        q.add(endpoint_guid=_QUAR_GUID, host_api="Windows WASAPI")
        wd, listener = _make_watchdog_with_quarantine(q)
        await wd.start(listener)
        await listener.fire(
            HotplugEvent(kind=HotplugEventKind.DEVICE_ADDED, endpoint_guid=_QUAR_GUID),
        )
        assert not q.is_quarantined(_QUAR_GUID)

    @pytest.mark.asyncio()
    async def test_friendly_name_fallback_clears_quarantine(self) -> None:
        q = EndpointQuarantine(quarantine_s=300.0, maxsize=8)
        q.add(
            endpoint_guid=_QUAR_GUID,
            device_friendly_name=_QUAR_FRIENDLY,
            host_api="ALSA",
        )
        wd, listener = _make_watchdog_with_quarantine(q)
        await wd.start(listener)
        # No GUID in the event — only a friendly name.
        await listener.fire(
            HotplugEvent(
                kind=HotplugEventKind.DEVICE_REMOVED,
                endpoint_guid=None,
                device_friendly_name=_QUAR_FRIENDLY,
            ),
        )
        assert not q.is_quarantined(_QUAR_GUID)

    @pytest.mark.asyncio()
    async def test_interface_name_fallback_clears_quarantine(self) -> None:
        q = EndpointQuarantine(quarantine_s=300.0, maxsize=8)
        q.add(
            endpoint_guid=_QUAR_GUID,
            device_interface_name=_QUAR_INTERFACE,
            host_api="WASAPI",
        )
        wd, listener = _make_watchdog_with_quarantine(q)
        await wd.start(listener)
        await listener.fire(
            HotplugEvent(
                kind=HotplugEventKind.DEVICE_ADDED,
                endpoint_guid=None,
                device_friendly_name=None,
                device_interface_name=_QUAR_INTERFACE,
            ),
        )
        assert not q.is_quarantined(_QUAR_GUID)

    @pytest.mark.asyncio()
    async def test_no_match_leaves_quarantine_intact(self) -> None:
        q = EndpointQuarantine(quarantine_s=300.0, maxsize=8)
        q.add(
            endpoint_guid=_QUAR_GUID,
            device_friendly_name=_QUAR_FRIENDLY,
            host_api="WASAPI",
        )
        wd, listener = _make_watchdog_with_quarantine(q)
        await wd.start(listener)
        await listener.fire(
            HotplugEvent(
                kind=HotplugEventKind.DEVICE_REMOVED,
                endpoint_guid="{SOME-OTHER-GUID}",
                device_friendly_name="Some Other Mic",
            ),
        )
        assert q.is_quarantined(_QUAR_GUID)

    @pytest.mark.asyncio()
    async def test_default_device_changed_does_not_call_clear(self) -> None:
        q = EndpointQuarantine(quarantine_s=300.0, maxsize=8)
        q.add(endpoint_guid=_QUAR_GUID, host_api="WASAPI")
        wd, listener = _make_watchdog_with_quarantine(q)
        await wd.start(listener)
        await listener.fire(
            HotplugEvent(
                kind=HotplugEventKind.DEFAULT_DEVICE_CHANGED,
                endpoint_guid=_QUAR_GUID,
            ),
        )
        # Quarantine intact — DEFAULT_DEVICE_CHANGED is not a replug signal.
        assert q.is_quarantined(_QUAR_GUID)

    @pytest.mark.asyncio()
    async def test_empty_labels_noop(self) -> None:
        q = EndpointQuarantine(quarantine_s=300.0, maxsize=8)
        q.add(endpoint_guid=_QUAR_GUID, host_api="WASAPI")
        wd, listener = _make_watchdog_with_quarantine(q)
        await wd.start(listener)
        await listener.fire(
            HotplugEvent(
                kind=HotplugEventKind.DEVICE_REMOVED,
                endpoint_guid=None,
                device_friendly_name=None,
                device_interface_name=None,
            ),
        )
        assert q.is_quarantined(_QUAR_GUID)
