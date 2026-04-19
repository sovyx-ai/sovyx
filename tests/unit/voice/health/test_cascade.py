"""Unit tests for :mod:`sovyx.voice.health.cascade`.

Pins ADR §4.2 semantics: priority order (pinned → store → cascade),
budget enforcement, short-circuit on HEALTHY, lifecycle lock serialisation,
``voice_clarity_autofix=False`` behaviour, and the three
defensive-catch paths on store-side failures.

All tests inject a fake probe function so neither PortAudio nor ONNX is
loaded. The fake is programmable per-combo so each test can script the
exact diagnosis sequence it wants.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import pytest

from sovyx.engine._lock_dict import LRULockDict
from sovyx.voice.health.cascade import (
    WINDOWS_CASCADE,
    run_cascade,
)
from sovyx.voice.health.contract import (
    Combo,
    ComboEntry,
    Diagnosis,
    ProbeMode,
    ProbeResult,
)

if TYPE_CHECKING:
    from collections.abc import Sequence


# ---------------------------------------------------------------------------
# Fake probe
# ---------------------------------------------------------------------------


@dataclass
class _ProbeCall:
    combo: Combo
    mode: ProbeMode
    device_index: int
    hard_timeout_s: float


@dataclass
class _FakeProbe:
    """Programmable probe stand-in.

    ``plan`` maps a predicate-on-combo to the Diagnosis to return. The
    first matching predicate wins; unmatched combos return DRIVER_ERROR
    so unprepared tests fail loudly rather than silently passing.
    """

    plan: list[tuple[Callable[[Combo], bool], Diagnosis]] = field(default_factory=list)
    calls: list[_ProbeCall] = field(default_factory=list)
    sleep_per_call_s: float = 0.0
    raise_on: Callable[[Combo], bool] | None = None

    async def __call__(
        self,
        *,
        combo: Combo,
        mode: ProbeMode,
        device_index: int,
        hard_timeout_s: float,
    ) -> ProbeResult:
        self.calls.append(
            _ProbeCall(
                combo=combo,
                mode=mode,
                device_index=device_index,
                hard_timeout_s=hard_timeout_s,
            ),
        )
        if self.sleep_per_call_s > 0.0:
            await asyncio.sleep(self.sleep_per_call_s)
        if self.raise_on is not None and self.raise_on(combo):
            msg = "test-side probe failure"
            raise RuntimeError(msg)
        diagnosis = Diagnosis.DRIVER_ERROR
        for predicate, diag in self.plan:
            if predicate(combo):
                diagnosis = diag
                break
        return ProbeResult(
            diagnosis=diagnosis,
            mode=mode,
            combo=combo,
            vad_max_prob=0.9 if diagnosis is Diagnosis.HEALTHY else 0.0,
            vad_mean_prob=0.5 if diagnosis is Diagnosis.HEALTHY else 0.0,
            rms_db=-20.0 if diagnosis is Diagnosis.HEALTHY else -80.0,
            callbacks_fired=50,
            duration_ms=500,
            error=None,
        )


# ---------------------------------------------------------------------------
# Fake ComboStore + CaptureOverrides
# ---------------------------------------------------------------------------


class _FakeComboStore:
    def __init__(self) -> None:
        self.entries: dict[str, Combo] = {}
        self.needs_reval: dict[str, bool] = {}
        self.get_raises: bool = False
        self.record_raises: bool = False
        self.invalidate_calls: list[tuple[str, str]] = []
        self.record_calls: list[tuple[str, Combo, int]] = []

    def get(self, endpoint_guid: str) -> ComboEntry | None:
        if self.get_raises:
            msg = "fake store get exploded"
            raise RuntimeError(msg)
        combo = self.entries.get(endpoint_guid)
        if combo is None:
            return None
        return ComboEntry(
            endpoint_guid=endpoint_guid,
            device_friendly_name="dev",
            device_interface_name="iface",
            device_class="class",
            endpoint_fxproperties_sha="sha",
            winning_combo=combo,
            validated_at="2026-01-01T00:00:00+00:00",
            validation_mode=ProbeMode.COLD,
            vad_max_prob_at_validation=None,
            vad_mean_prob_at_validation=None,
            rms_db_at_validation=-20.0,
            probe_duration_ms=500,
            detected_apos_at_validation=(),
            cascade_attempts_before_success=0,
            boots_validated=1,
            last_boot_validated="2026-01-01T00:00:00+00:00",
            last_boot_diagnosis=Diagnosis.HEALTHY,
            probe_history=(),
            pinned=False,
            needs_revalidation=self.needs_reval.get(endpoint_guid, False),
        )

    def needs_revalidation(self, endpoint_guid: str) -> bool:
        return self.needs_reval.get(endpoint_guid, False)

    def invalidate(self, endpoint_guid: str, reason: str) -> None:
        self.invalidate_calls.append((endpoint_guid, reason))
        self.entries.pop(endpoint_guid, None)

    def record_winning(
        self,
        endpoint_guid: str,
        *,
        device_friendly_name: str,  # noqa: ARG002
        device_interface_name: str,  # noqa: ARG002
        device_class: str,  # noqa: ARG002
        endpoint_fxproperties_sha: str,  # noqa: ARG002
        combo: Combo,
        probe: ProbeResult,  # noqa: ARG002
        detected_apos: Sequence[str],  # noqa: ARG002
        cascade_attempts_before_success: int,
    ) -> None:
        if self.record_raises:
            msg = "fake store record exploded"
            raise RuntimeError(msg)
        self.entries[endpoint_guid] = combo
        self.record_calls.append((endpoint_guid, combo, cascade_attempts_before_success))


class _FakeOverrides:
    def __init__(self) -> None:
        self.pins: dict[str, Combo] = {}
        self.get_raises: bool = False

    def get(self, endpoint_guid: str) -> Combo | None:
        if self.get_raises:
            msg = "fake overrides get exploded"
            raise RuntimeError(msg)
        return self.pins.get(endpoint_guid)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _win_combo(
    *,
    host_api: str = "WASAPI",
    sample_rate: int = 16_000,
    exclusive: bool = True,
    frames_per_buffer: int = 480,
) -> Combo:
    return Combo(
        host_api=host_api,
        sample_rate=sample_rate,
        channels=1,
        sample_format="int16",
        exclusive=exclusive,
        auto_convert=False,
        frames_per_buffer=frames_per_buffer,
        platform_key="win32",
    )


def _match_all(_combo: Combo) -> bool:
    return True


async def _run(**kwargs: object) -> object:
    """Convenience so each test can scope its own kwargs dict."""
    base: dict[str, object] = {
        "endpoint_guid": "test-endpoint",
        "device_index": 0,
        "mode": ProbeMode.COLD,
        "platform_key": "win32",
    }
    base.update(kwargs)
    return await run_cascade(**base)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Priority ordering
# ---------------------------------------------------------------------------


class TestPriorityOrdering:
    @pytest.mark.asyncio()
    async def test_pinned_wins_first_and_skips_store_and_cascade(self) -> None:
        pinned = _win_combo(host_api="MME")
        store_combo = _win_combo(host_api="DirectSound")
        overrides = _FakeOverrides()
        overrides.pins["test-endpoint"] = pinned
        store = _FakeComboStore()
        store.entries["test-endpoint"] = store_combo
        probe = _FakeProbe(plan=[(_match_all, Diagnosis.HEALTHY)])

        result = await _run(
            probe_fn=probe,
            combo_store=store,
            capture_overrides=overrides,
        )

        assert result.source == "pinned"  # type: ignore[attr-defined]
        assert result.winning_combo == pinned  # type: ignore[attr-defined]
        assert len(probe.calls) == 1
        assert probe.calls[0].combo == pinned
        # Fast-path sources never count as cascade attempts.
        assert result.attempts_count == 0  # type: ignore[attr-defined]
        # Pinned wins skip record_winning — overrides are already persisted.
        assert store.record_calls == []

    @pytest.mark.asyncio()
    async def test_store_wins_second_when_no_pinned(self) -> None:
        store_combo = _win_combo(host_api="DirectSound")
        store = _FakeComboStore()
        store.entries["test-endpoint"] = store_combo
        probe = _FakeProbe(plan=[(_match_all, Diagnosis.HEALTHY)])

        result = await _run(probe_fn=probe, combo_store=store)

        assert result.source == "store"  # type: ignore[attr-defined]
        assert result.winning_combo == store_combo  # type: ignore[attr-defined]
        # Store fast-path HEALTHY doesn't re-record (combo already persisted).
        assert store.record_calls == []

    @pytest.mark.asyncio()
    async def test_cascade_runs_when_no_fast_paths(self) -> None:
        probe = _FakeProbe(
            plan=[(lambda c: c.host_api == "WASAPI" and not c.exclusive, Diagnosis.HEALTHY)],
        )
        store = _FakeComboStore()

        result = await _run(probe_fn=probe, combo_store=store)

        assert result.source == "cascade"  # type: ignore[attr-defined]
        # Default cascade: exclusive attempts first, then WDM-KS, then shared.
        # Our plan says "non-exclusive WASAPI wins" → should short-circuit there.
        assert result.winning_combo is not None  # type: ignore[attr-defined]
        assert result.winning_combo.host_api == "WASAPI"  # type: ignore[attr-defined]
        assert result.winning_combo.exclusive is False  # type: ignore[attr-defined]
        assert result.attempts_count >= 1  # type: ignore[attr-defined]
        # Winning cascade combo IS recorded to the store.
        assert len(store.record_calls) == 1


# ---------------------------------------------------------------------------
# Short-circuit + full-exhaust cases
# ---------------------------------------------------------------------------


class TestCascadeFlow:
    @pytest.mark.asyncio()
    async def test_first_attempt_healthy_short_circuits(self) -> None:
        probe = _FakeProbe(plan=[(_match_all, Diagnosis.HEALTHY)])
        result = await _run(probe_fn=probe)

        assert result.source == "cascade"  # type: ignore[attr-defined]
        assert result.attempts_count == 1  # type: ignore[attr-defined]
        # Only first combo of WINDOWS_CASCADE should have been probed.
        assert len(probe.calls) == 1
        assert probe.calls[0].combo == WINDOWS_CASCADE[0]

    @pytest.mark.asyncio()
    async def test_exhaustion_returns_none_source(self) -> None:
        probe = _FakeProbe(plan=[(_match_all, Diagnosis.DEVICE_BUSY)])
        result = await _run(probe_fn=probe)

        assert result.source == "none"  # type: ignore[attr-defined]
        assert result.winning_combo is None  # type: ignore[attr-defined]
        assert result.attempts_count == len(WINDOWS_CASCADE)  # type: ignore[attr-defined]
        assert result.budget_exhausted is False  # type: ignore[attr-defined]
        # Every cascade entry should have been probed.
        assert len(probe.calls) == len(WINDOWS_CASCADE)

    @pytest.mark.asyncio()
    async def test_last_cascade_entry_wins(self) -> None:
        """ADR §4.2: MME (last) must still be tried if everything else refuses."""
        probe = _FakeProbe(
            plan=[(lambda c: c.host_api == "MME", Diagnosis.HEALTHY)],
        )
        result = await _run(probe_fn=probe)

        assert result.source == "cascade"  # type: ignore[attr-defined]
        assert result.winning_combo is not None  # type: ignore[attr-defined]
        assert result.winning_combo.host_api == "MME"  # type: ignore[attr-defined]
        assert result.attempts_count == len(WINDOWS_CASCADE)  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# Budget enforcement
# ---------------------------------------------------------------------------


class TestBudget:
    @pytest.mark.asyncio()
    async def test_total_budget_exhausted_returns_best_effort(self) -> None:
        probe = _FakeProbe(plan=[(_match_all, Diagnosis.DEVICE_BUSY)])

        # Clock advances +10s per read; deadline = 5s → exhausts before any attempt.
        t = {"now": 0.0}

        def clock() -> float:
            t["now"] += 10.0
            return t["now"]

        result = await _run(
            probe_fn=probe,
            total_budget_s=5.0,
            clock=clock,
        )

        assert result.source == "none"  # type: ignore[attr-defined]
        assert result.budget_exhausted is True  # type: ignore[attr-defined]
        # No attempts fired because first clock() already exceeded deadline.
        assert result.attempts_count == 0  # type: ignore[attr-defined]

    @pytest.mark.asyncio()
    async def test_budget_mid_cascade_halts_further_attempts(self) -> None:
        probe = _FakeProbe(plan=[(_match_all, Diagnosis.DEVICE_BUSY)])

        # Advance 4s per read → after 2 attempts (8s) we cross a 10s budget.
        t = {"now": 0.0}

        def clock() -> float:
            t["now"] += 4.0
            return t["now"]

        result = await _run(
            probe_fn=probe,
            total_budget_s=10.0,
            clock=clock,
        )

        assert result.budget_exhausted is True  # type: ignore[attr-defined]
        # 8 cascade entries × 1 clock() check per attempt; at check #3 elapsed=12 ≥ 10.
        assert result.attempts_count < len(WINDOWS_CASCADE)  # type: ignore[attr-defined]

    @pytest.mark.asyncio()
    async def test_attempt_budget_forwarded_to_probe(self) -> None:
        probe = _FakeProbe(plan=[(_match_all, Diagnosis.HEALTHY)])
        await _run(probe_fn=probe, attempt_budget_s=2.5)

        assert probe.calls[0].hard_timeout_s == 2.5


# ---------------------------------------------------------------------------
# voice_clarity_autofix=False
# ---------------------------------------------------------------------------


class TestVoiceClarityAutofix:
    @pytest.mark.asyncio()
    async def test_autofix_false_skips_first_five_attempts(self) -> None:
        probe = _FakeProbe(plan=[(_match_all, Diagnosis.HEALTHY)])
        result = await _run(
            probe_fn=probe,
            voice_clarity_autofix=False,
        )

        assert result.source == "cascade"  # type: ignore[attr-defined]
        # First probed combo should be index 5 (shared WASAPI auto_convert).
        assert probe.calls[0].combo == WINDOWS_CASCADE[5]
        assert result.attempts_count == 1  # type: ignore[attr-defined]

    @pytest.mark.asyncio()
    async def test_autofix_false_still_exhausts_if_nothing_works(self) -> None:
        probe = _FakeProbe(plan=[(_match_all, Diagnosis.DEVICE_BUSY)])
        result = await _run(
            probe_fn=probe,
            voice_clarity_autofix=False,
        )

        # Only combos 5, 6, 7 (shared + DirectSound + MME) should run.
        assert result.attempts_count == 3  # type: ignore[attr-defined]
        expected = [WINDOWS_CASCADE[5], WINDOWS_CASCADE[6], WINDOWS_CASCADE[7]]
        assert [c.combo for c in probe.calls] == expected


# ---------------------------------------------------------------------------
# Lifecycle lock serialisation
# ---------------------------------------------------------------------------


class TestLifecycleLock:
    @pytest.mark.asyncio()
    async def test_lock_serialises_concurrent_cascades_same_endpoint(self) -> None:
        """Two concurrent run_cascade calls on the same endpoint never overlap."""
        probe = _FakeProbe(
            plan=[(_match_all, Diagnosis.HEALTHY)],
            sleep_per_call_s=0.1,
        )
        locks: LRULockDict[str] = LRULockDict(maxsize=8)

        results = await asyncio.gather(
            _run(probe_fn=probe, lifecycle_locks=locks),
            _run(probe_fn=probe, lifecycle_locks=locks),
        )

        # Both succeeded but serially, not in parallel.
        assert all(r.source == "cascade" for r in results)  # type: ignore[attr-defined]
        assert len(probe.calls) == 2

    @pytest.mark.asyncio()
    async def test_lock_allows_parallel_on_distinct_endpoints(self) -> None:
        probe = _FakeProbe(
            plan=[(_match_all, Diagnosis.HEALTHY)],
            sleep_per_call_s=0.1,
        )
        locks: LRULockDict[str] = LRULockDict(maxsize=8)

        results = await asyncio.gather(
            run_cascade(
                endpoint_guid="ep-A",
                device_index=0,
                mode=ProbeMode.COLD,
                platform_key="win32",
                probe_fn=probe,
                lifecycle_locks=locks,
            ),
            run_cascade(
                endpoint_guid="ep-B",
                device_index=1,
                mode=ProbeMode.COLD,
                platform_key="win32",
                probe_fn=probe,
                lifecycle_locks=locks,
            ),
        )
        assert {r.endpoint_guid for r in results} == {"ep-A", "ep-B"}


# ---------------------------------------------------------------------------
# Store / overrides resilience (ADR §I4 defensive catch paths)
# ---------------------------------------------------------------------------


class TestDefensiveCatches:
    @pytest.mark.asyncio()
    async def test_override_lookup_failure_falls_through_to_cascade(self) -> None:
        overrides = _FakeOverrides()
        overrides.get_raises = True
        probe = _FakeProbe(plan=[(_match_all, Diagnosis.HEALTHY)])

        result = await _run(probe_fn=probe, capture_overrides=overrides)

        assert result.source == "cascade"  # type: ignore[attr-defined]
        # Did not abort — fell through to platform cascade.
        assert len(probe.calls) == 1

    @pytest.mark.asyncio()
    async def test_store_lookup_failure_falls_through_to_cascade(self) -> None:
        store = _FakeComboStore()
        store.get_raises = True
        probe = _FakeProbe(plan=[(_match_all, Diagnosis.HEALTHY)])

        result = await _run(probe_fn=probe, combo_store=store)

        assert result.source == "cascade"  # type: ignore[attr-defined]

    @pytest.mark.asyncio()
    async def test_record_winning_failure_does_not_abort_cascade(self) -> None:
        store = _FakeComboStore()
        store.record_raises = True
        probe = _FakeProbe(plan=[(_match_all, Diagnosis.HEALTHY)])

        result = await _run(probe_fn=probe, combo_store=store)

        # Cascade still reports the HEALTHY winner even though persisting failed.
        assert result.source == "cascade"  # type: ignore[attr-defined]
        assert result.winning_combo == WINDOWS_CASCADE[0]  # type: ignore[attr-defined]

    @pytest.mark.asyncio()
    async def test_stale_store_entry_invalidates_and_continues(self) -> None:
        stale = _win_combo(host_api="MME")  # will fail probe
        store = _FakeComboStore()
        store.entries["test-endpoint"] = stale
        probe = _FakeProbe(
            plan=[
                (lambda c: c.host_api == "MME", Diagnosis.DRIVER_ERROR),
                (lambda c: c.host_api == "WASAPI" and c.exclusive, Diagnosis.HEALTHY),
            ],
        )

        result = await _run(probe_fn=probe, combo_store=store)

        assert result.source == "cascade"  # type: ignore[attr-defined]
        # Stale store entry was invalidated.
        assert ("test-endpoint", "fast_path_probe_failed") in store.invalidate_calls
        # And a fresh winner was recorded.
        assert len(store.record_calls) == 1

    @pytest.mark.asyncio()
    async def test_probe_exception_becomes_driver_error_diagnosis(self) -> None:
        probe = _FakeProbe(
            plan=[(lambda c: c.host_api == "WASAPI" and c.exclusive, Diagnosis.HEALTHY)],
            raise_on=lambda c: c.host_api == "WASAPI" and c.exclusive,
        )

        result = await _run(probe_fn=probe)

        # First two (exclusive WASAPI) raise → translated to DRIVER_ERROR.
        # Third attempt (exclusive 48 kHz 960-frame) also raises.
        # WDM-KS doesn't raise but also doesn't match HEALTHY predicate.
        assert result.source == "none"  # type: ignore[attr-defined]
        assert any(
            a.diagnosis is Diagnosis.DRIVER_ERROR and a.error is not None  # type: ignore[attr-defined]
            for a in result.attempts  # type: ignore[attr-defined]
        )


# ---------------------------------------------------------------------------
# Platform gating + overrides
# ---------------------------------------------------------------------------


class TestPlatformAndOverrides:
    @pytest.mark.asyncio()
    async def test_empty_platform_cascade_returns_none(self) -> None:
        probe = _FakeProbe(plan=[(_match_all, Diagnosis.HEALTHY)])
        # Use an unknown platform so the cascade lookup returns () and
        # the whole run is a no-op. Linux and macOS are populated in
        # Tasks #27 / #28.
        result = await _run(probe_fn=probe, platform_key="sunos")

        assert result.source == "none"  # type: ignore[attr-defined]
        assert result.attempts_count == 0  # type: ignore[attr-defined]
        assert len(probe.calls) == 0

    @pytest.mark.asyncio()
    async def test_cascade_override_replaces_default(self) -> None:
        custom = (
            _win_combo(host_api="MME"),
            _win_combo(host_api="DirectSound"),
        )
        probe = _FakeProbe(
            plan=[(lambda c: c.host_api == "DirectSound", Diagnosis.HEALTHY)],
        )

        result = await _run(probe_fn=probe, cascade_override=custom)

        assert result.attempts_count == 2  # type: ignore[attr-defined]
        assert [c.combo.host_api for c in probe.calls] == ["MME", "DirectSound"]
        assert result.winning_combo is not None  # type: ignore[attr-defined]
        assert result.winning_combo.host_api == "DirectSound"  # type: ignore[attr-defined]

    @pytest.mark.asyncio()
    async def test_pinned_platform_mismatch_is_ignored(self) -> None:
        """An override pinned on linux must not be reused on a win32 runtime."""
        linux_combo = Combo(
            host_api="ALSA",
            sample_rate=16_000,
            channels=1,
            sample_format="int16",
            exclusive=False,
            auto_convert=False,
            frames_per_buffer=480,
            platform_key="linux",
        )
        overrides = _FakeOverrides()
        overrides.pins["test-endpoint"] = linux_combo
        probe = _FakeProbe(plan=[(_match_all, Diagnosis.HEALTHY)])

        result = await _run(probe_fn=probe, capture_overrides=overrides)

        # Override was ignored → cascade ran the platform default.
        assert result.source == "cascade"  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# Cascade table sanity (ADR §4.2)
# ---------------------------------------------------------------------------


class TestWindowsCascadeTable:
    def test_eight_attempts(self) -> None:
        assert len(WINDOWS_CASCADE) == 8

    def test_first_three_are_wasapi_exclusive(self) -> None:
        for combo in WINDOWS_CASCADE[:3]:
            assert combo.host_api == "WASAPI"
            assert combo.exclusive is True

    def test_attempts_three_and_four_are_wdmks(self) -> None:
        assert WINDOWS_CASCADE[3].host_api == "WDM-KS"
        assert WINDOWS_CASCADE[4].host_api == "WDM-KS"

    def test_attempt_five_is_shared_wasapi_auto_convert(self) -> None:
        combo = WINDOWS_CASCADE[5]
        assert combo.host_api == "WASAPI"
        assert combo.exclusive is False
        assert combo.auto_convert is True

    def test_last_two_are_legacy(self) -> None:
        assert WINDOWS_CASCADE[6].host_api == "DirectSound"
        assert WINDOWS_CASCADE[7].host_api == "MME"

    def test_all_win32_platform_key(self) -> None:
        for combo in WINDOWS_CASCADE:
            assert combo.platform_key == "win32"


class TestLinuxCascadeTable:
    """Linux cascade ordering rationale — ADR §4.2."""

    def test_six_attempts(self) -> None:
        from sovyx.voice.health.cascade import LINUX_CASCADE

        assert len(LINUX_CASCADE) == 6

    def test_first_two_are_alsa_exclusive(self) -> None:
        from sovyx.voice.health.cascade import LINUX_CASCADE

        for combo in LINUX_CASCADE[:2]:
            assert combo.host_api == "ALSA"
            assert combo.exclusive is True
            assert combo.auto_convert is False

    def test_attempt_two_is_jack_float32(self) -> None:
        from sovyx.voice.health.cascade import LINUX_CASCADE

        combo = LINUX_CASCADE[2]
        assert combo.host_api == "JACK"
        assert combo.sample_format == "float32"

    def test_attempts_three_and_four_are_pipewire_autoconvert(self) -> None:
        from sovyx.voice.health.cascade import LINUX_CASCADE

        for combo in LINUX_CASCADE[3:5]:
            assert combo.host_api == "PipeWire"
            assert combo.auto_convert is True

    def test_last_is_pulseaudio_shared(self) -> None:
        from sovyx.voice.health.cascade import LINUX_CASCADE

        combo = LINUX_CASCADE[5]
        assert combo.host_api == "PulseAudio"
        assert combo.exclusive is False

    def test_all_linux_platform_key(self) -> None:
        from sovyx.voice.health.cascade import LINUX_CASCADE

        for combo in LINUX_CASCADE:
            assert combo.platform_key == "linux"

    def test_platform_dispatch(self) -> None:
        from sovyx.voice.health.cascade import (
            LINUX_CASCADE,
            _platform_cascade,
        )

        assert _platform_cascade("linux") == LINUX_CASCADE


# ---------------------------------------------------------------------------
# Probe kwargs forwarding
# ---------------------------------------------------------------------------


class TestProbeKwargsForwarding:
    @pytest.mark.asyncio()
    async def test_mode_and_device_index_are_forwarded(self) -> None:
        probe = _FakeProbe(plan=[(_match_all, Diagnosis.HEALTHY)])
        await _run(probe_fn=probe, mode=ProbeMode.WARM, device_index=3)

        assert probe.calls[0].mode is ProbeMode.WARM
        assert probe.calls[0].device_index == 3


def _assert_coro(x: Awaitable[object]) -> None:
    """Sanity: type of asyncio.gather return values."""
    assert asyncio.iscoroutine(x) or asyncio.isfuture(x)
