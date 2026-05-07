"""Tests for §4.4.7 kernel-invalidated endpoint quarantine store."""

from __future__ import annotations

import dataclasses
from typing import TYPE_CHECKING

import pytest

from sovyx.voice.health import (
    EndpointQuarantine,
    QuarantineEntry,
    get_default_quarantine,
    reset_default_quarantine,
)

if TYPE_CHECKING:
    from collections.abc import Generator


class _FakeClock:
    """Monotonic clock whose value is advanced manually by tests."""

    def __init__(self, start: float = 1_000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture()
def clock() -> _FakeClock:
    return _FakeClock()


@pytest.fixture()
def store(clock: _FakeClock) -> EndpointQuarantine:
    return EndpointQuarantine(quarantine_s=60.0, maxsize=4, clock=clock)


@pytest.fixture(autouse=True)
def _reset_singleton() -> Generator[None, None, None]:
    reset_default_quarantine()
    yield
    reset_default_quarantine()


class TestConstructor:
    """EndpointQuarantine constructor validation."""

    def test_rejects_non_positive_quarantine_s(self) -> None:
        with pytest.raises(ValueError, match="quarantine_s must be positive"):
            EndpointQuarantine(quarantine_s=0.0)
        with pytest.raises(ValueError, match="quarantine_s must be positive"):
            EndpointQuarantine(quarantine_s=-1.0)

    def test_rejects_non_positive_maxsize(self) -> None:
        with pytest.raises(ValueError, match="maxsize must be positive"):
            EndpointQuarantine(quarantine_s=60.0, maxsize=0)
        with pytest.raises(ValueError, match="maxsize must be positive"):
            EndpointQuarantine(quarantine_s=60.0, maxsize=-5)

    def test_default_clock_is_monotonic(self) -> None:
        """With no injected clock, real time.monotonic drives expiry."""
        eq = EndpointQuarantine(quarantine_s=60.0)
        entry = eq.add(endpoint_guid="{AAA}")
        # expires_at should be > added_at and difference is quarantine_s
        assert entry.expires_at_monotonic - entry.added_at_monotonic == pytest.approx(60.0)


class TestQuarantineEntry:
    """QuarantineEntry frozen-dataclass semantics."""

    def test_entry_is_frozen(self, store: EndpointQuarantine) -> None:
        entry = store.add(endpoint_guid="{AAA}")
        with pytest.raises(dataclasses.FrozenInstanceError):
            entry.endpoint_guid = "{BBB}"  # type: ignore[misc]

    def test_entry_captures_all_fields(self, store: EndpointQuarantine, clock: _FakeClock) -> None:
        entry = store.add(
            endpoint_guid="{GUID-1}",
            device_friendly_name="Razer BlackShark V2 Pro",
            device_interface_name=r"\\?\USB#VID_1532",
            host_api="Windows WASAPI",
            reason="probe",
        )
        assert entry.endpoint_guid == "{GUID-1}"
        assert entry.device_friendly_name == "Razer BlackShark V2 Pro"
        assert entry.device_interface_name == r"\\?\USB#VID_1532"
        assert entry.host_api == "Windows WASAPI"
        assert entry.reason == "probe"
        assert entry.added_at_monotonic == clock.now
        assert entry.expires_at_monotonic == clock.now + 60.0


class TestAdd:
    """add() insertion, replacement, and capacity eviction."""

    def test_add_rejects_empty_guid(self, store: EndpointQuarantine) -> None:
        with pytest.raises(ValueError, match="endpoint_guid must be a non-empty string"):
            store.add(endpoint_guid="")

    def test_add_then_is_quarantined(self, store: EndpointQuarantine) -> None:
        store.add(endpoint_guid="{AAA}")
        assert store.is_quarantined("{AAA}")
        assert "{AAA}" in store

    def test_add_replace_resets_deadline(
        self, store: EndpointQuarantine, clock: _FakeClock
    ) -> None:
        first = store.add(endpoint_guid="{AAA}")
        clock.advance(30.0)
        second = store.add(endpoint_guid="{AAA}", reason="watchdog_recheck")
        assert second.added_at_monotonic > first.added_at_monotonic
        assert second.expires_at_monotonic > first.expires_at_monotonic
        assert second.reason == "watchdog_recheck"
        # Only one live entry for this GUID.
        assert len(store) == 1

    def test_add_evicts_oldest_at_capacity(self, store: EndpointQuarantine) -> None:
        # maxsize=4 from fixture.
        for i in range(4):
            store.add(endpoint_guid=f"{{G{i}}}")
        assert len(store) == 4
        # Fifth insert evicts {G0}.
        store.add(endpoint_guid="{G4}")
        assert len(store) == 4
        assert "{G0}" not in store
        assert "{G4}" in store

    def test_add_replace_does_not_advance_eviction_position(
        self, store: EndpointQuarantine
    ) -> None:
        # With maxsize=4, replacing {G0} should NOT protect it from being
        # the oldest — the pop+reinsert puts it at the tail.
        for i in range(4):
            store.add(endpoint_guid=f"{{G{i}}}")
        # Re-add G0: it moves to the tail.
        store.add(endpoint_guid="{G0}")
        # Now G1 is oldest. Inserting G4 evicts G1.
        store.add(endpoint_guid="{G4}")
        assert "{G1}" not in store
        assert "{G0}" in store
        assert "{G4}" in store


class TestClear:
    """clear() removes entries and reports whether anything was removed."""

    def test_clear_known_returns_true(self, store: EndpointQuarantine) -> None:
        store.add(endpoint_guid="{AAA}")
        assert store.clear("{AAA}", reason="hotplug") is True
        assert "{AAA}" not in store

    def test_clear_unknown_returns_false(self, store: EndpointQuarantine) -> None:
        assert store.clear("{MISSING}") is False

    def test_clear_with_default_reason_does_not_error(self, store: EndpointQuarantine) -> None:
        store.add(endpoint_guid="{AAA}")
        assert store.clear("{AAA}") is True


class TestExpiry:
    """Lazy and eager expiry semantics."""

    def test_is_quarantined_lazy_purges_expired(
        self, store: EndpointQuarantine, clock: _FakeClock
    ) -> None:
        store.add(endpoint_guid="{AAA}")
        clock.advance(60.1)
        assert store.is_quarantined("{AAA}") is False
        # Entry has been removed — subsequent clear returns False.
        assert store.clear("{AAA}") is False

    def test_get_returns_live_entry(self, store: EndpointQuarantine) -> None:
        store.add(endpoint_guid="{AAA}", host_api="Windows WASAPI")
        entry = store.get("{AAA}")
        assert entry is not None
        assert entry.host_api == "Windows WASAPI"

    def test_get_returns_none_for_unknown(self, store: EndpointQuarantine) -> None:
        assert store.get("{MISSING}") is None

    def test_get_lazy_purges_expired(self, store: EndpointQuarantine, clock: _FakeClock) -> None:
        store.add(endpoint_guid="{AAA}")
        clock.advance(120.0)
        assert store.get("{AAA}") is None
        assert "{AAA}" not in store

    def test_purge_expired_returns_evicted(
        self, store: EndpointQuarantine, clock: _FakeClock
    ) -> None:
        store.add(endpoint_guid="{OLD}")
        clock.advance(30.0)
        store.add(endpoint_guid="{FRESH}")
        clock.advance(40.0)
        # OLD is 70s old (>60s), FRESH is 40s old (<60s).
        evicted = store.purge_expired()
        assert [e.endpoint_guid for e in evicted] == ["{OLD}"]
        assert store.is_quarantined("{FRESH}")
        assert not store.is_quarantined("{OLD}")

    def test_purge_expired_no_op_when_all_live(self, store: EndpointQuarantine) -> None:
        store.add(endpoint_guid="{A}")
        store.add(endpoint_guid="{B}")
        assert store.purge_expired() == []
        assert len(store) == 2


class TestSnapshot:
    """snapshot() returns immutable tuple of live entries only."""

    def test_snapshot_returns_tuple(self, store: EndpointQuarantine) -> None:
        store.add(endpoint_guid="{AAA}")
        snap = store.snapshot()
        assert isinstance(snap, tuple)
        assert len(snap) == 1
        assert snap[0].endpoint_guid == "{AAA}"

    def test_snapshot_empty_store(self, store: EndpointQuarantine) -> None:
        assert store.snapshot() == ()

    def test_snapshot_drops_expired_and_purges_store(
        self, store: EndpointQuarantine, clock: _FakeClock
    ) -> None:
        store.add(endpoint_guid="{OLD}")
        clock.advance(30.0)
        store.add(endpoint_guid="{FRESH}")
        clock.advance(40.0)
        snap = store.snapshot()
        assert [e.endpoint_guid for e in snap] == ["{FRESH}"]
        # Store was purged as a side effect.
        assert "{OLD}" not in store

    def test_snapshot_preserves_insertion_order(self, store: EndpointQuarantine) -> None:
        for i in range(3):
            store.add(endpoint_guid=f"{{G{i}}}")
        snap = store.snapshot()
        assert [e.endpoint_guid for e in snap] == ["{G0}", "{G1}", "{G2}"]


class TestMagicMethods:
    """__len__ / __contains__ / endpoints()."""

    def test_len_counts_live_only(self, store: EndpointQuarantine, clock: _FakeClock) -> None:
        store.add(endpoint_guid="{A}")
        store.add(endpoint_guid="{B}")
        clock.advance(120.0)
        store.add(endpoint_guid="{C}")
        # A and B expired; only C is live.
        assert len(store) == 1

    def test_contains_rejects_non_string(self, store: EndpointQuarantine) -> None:
        store.add(endpoint_guid="{A}")
        assert ("{A}" in store) is True
        assert (123 in store) is False  # type: ignore[operator]
        assert (None in store) is False  # type: ignore[operator]

    def test_endpoints_yields_live_guids(
        self, store: EndpointQuarantine, clock: _FakeClock
    ) -> None:
        store.add(endpoint_guid="{A}")
        store.add(endpoint_guid="{B}")
        clock.advance(120.0)
        store.add(endpoint_guid="{C}")
        assert list(store.endpoints()) == ["{C}"]


class TestSingleton:
    """get_default_quarantine / reset_default_quarantine."""

    def test_first_call_constructs_instance(self) -> None:
        eq = get_default_quarantine(quarantine_s=30.0)
        assert isinstance(eq, EndpointQuarantine)
        assert eq._quarantine_s == 30.0

    def test_subsequent_calls_return_same_instance(self) -> None:
        first = get_default_quarantine(quarantine_s=30.0)
        second = get_default_quarantine(quarantine_s=30.0)
        assert first is second

    def test_subsequent_call_with_different_ttl_ignored(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        first = get_default_quarantine(quarantine_s=30.0)
        # Must not raise and must not replace instance; log is emitted via structlog,
        # which doesn't go through caplog by default — so just assert identity.
        second = get_default_quarantine(quarantine_s=900.0)
        assert first is second
        assert second._quarantine_s == 30.0

    def test_reset_drops_singleton(self) -> None:
        first = get_default_quarantine(quarantine_s=30.0)
        reset_default_quarantine()
        second = get_default_quarantine(quarantine_s=45.0)
        assert first is not second
        assert second._quarantine_s == 45.0

    def test_default_ttl_from_tuning_config(self) -> None:
        """When quarantine_s is None, value comes from VoiceTuningConfig."""
        from sovyx.engine.config import VoiceTuningConfig

        expected = VoiceTuningConfig().kernel_invalidated_quarantine_s
        eq = get_default_quarantine()
        assert eq._quarantine_s == expected

    def test_maxsize_defaults_to_64(self) -> None:
        eq = get_default_quarantine(quarantine_s=30.0)
        assert eq._maxsize == 64

    def test_maxsize_override_honored_on_first_call(self) -> None:
        eq = get_default_quarantine(quarantine_s=30.0, maxsize=8)
        assert eq._maxsize == 8


class TestEntryValue:
    """QuarantineEntry defaults-aware field coverage."""

    def test_add_defaults_empty_name_and_interface(self, store: EndpointQuarantine) -> None:
        entry = store.add(endpoint_guid="{AAA}")
        assert entry.device_friendly_name == ""
        assert entry.device_interface_name == ""
        assert entry.host_api == ""
        assert entry.reason == "probe"
        assert entry.physical_device_id == ""


class TestPhysicalDeviceScope:
    """v0.20.4 physical-device-scoped quarantine (Razer regression)."""

    def test_add_records_physical_device_id(self, store: EndpointQuarantine) -> None:
        entry = store.add(
            endpoint_guid="{WASAPI-GUID}",
            physical_device_id="razer blackshark v2 pro",
        )
        assert entry.physical_device_id == "razer blackshark v2 pro"

    def test_is_quarantined_physical_matches_any_alias(self, store: EndpointQuarantine) -> None:
        # One physical device quarantined via its WASAPI alias.
        store.add(
            endpoint_guid="{WASAPI-GUID}",
            physical_device_id="razer blackshark v2 pro",
        )
        # Every host-API alias of the same physical device matches.
        assert store.is_quarantined_physical("razer blackshark v2 pro") is True
        # Different physical device doesn't.
        assert store.is_quarantined_physical("laptop array mic") is False

    def test_is_quarantined_physical_empty_id_never_matches(
        self, store: EndpointQuarantine
    ) -> None:
        # Even with a quarantined entry that stored an empty physical id,
        # querying with "" must not match — an unspecified identity
        # matches nothing.
        store.add(endpoint_guid="{GUID}", physical_device_id="")
        assert store.is_quarantined_physical("") is False

    def test_is_quarantined_physical_skips_expired(
        self, store: EndpointQuarantine, clock: _FakeClock
    ) -> None:
        store.add(
            endpoint_guid="{GUID}",
            physical_device_id="razer blackshark v2 pro",
        )
        # Entry is live.
        assert store.is_quarantined_physical("razer blackshark v2 pro") is True
        # Walk past the 60 s TTL — the match should lapse and the entry
        # should be purged lazily.
        clock.advance(120.0)
        assert store.is_quarantined_physical("razer blackshark v2 pro") is False
        assert store.is_quarantined("{GUID}") is False

    def test_kernel_reset_regression_razer_blackshark(self, store: EndpointQuarantine) -> None:
        """End-to-end: the Razer BlackShark V2 Pro failure mode (2026-04-20).

        The real incident: WASAPI-exclusive probe against Razer Razer
        BlackShark V2 Pro returned ``AUDCLNT_E_DEVICE_INVALIDATED`` →
        cascade quarantined the WASAPI endpoint. Factory fail-over
        picked the MME surrogate (different GUID, same physical mic)
        → re-cascaded into WDM-KS → hard reset.

        v0.20.4 fix: the quarantine entry carries the canonical
        physical-device identity, and ``is_quarantined_physical``
        lets the fail-over picker reject every alias atomically.
        """
        # Simulate the cascade's quarantine add after
        # AUDCLNT_E_DEVICE_INVALIDATED on the WASAPI exclusive probe.
        store.add(
            endpoint_guid="{WASAPI-razer-guid}",
            device_friendly_name="Razer BlackShark V2 Pro",
            host_api="Windows WASAPI",
            reason="probe_cascade",
            physical_device_id="razer blackshark v2 pro",
        )
        # Before v0.20.4, the MME alias (distinct surrogate GUID) was
        # NOT flagged and the factory would fail over into it.
        assert store.is_quarantined("{MME-razer-surrogate}") is False
        # After v0.20.4, the physical-scope check rejects the alias.
        assert store.is_quarantined_physical("razer blackshark v2 pro") is True

    def test_equality_by_value(self, clock: _FakeClock) -> None:
        e1 = QuarantineEntry(
            endpoint_guid="{A}",
            device_friendly_name="Mic",
            device_interface_name="",
            host_api="WASAPI",
            added_at_monotonic=1.0,
            expires_at_monotonic=61.0,
            reason="probe",
        )
        e2 = QuarantineEntry(
            endpoint_guid="{A}",
            device_friendly_name="Mic",
            device_interface_name="",
            host_api="WASAPI",
            added_at_monotonic=1.0,
            expires_at_monotonic=61.0,
            reason="probe",
        )
        assert e1 == e2
        assert hash(e1) == hash(e2)


# ── T6.17 — ping-pong detection ──────────────────────────────────────


_QUARANTINE_LOGGER = "sovyx.voice.health._quarantine"


def _events_of(
    caplog: pytest.LogCaptureFixture,
    event_name: str,
) -> list[dict[str, object]]:
    """Return all structlog dict-records matching ``event_name``."""
    return [
        r.msg
        for r in caplog.records
        if (
            r.name == _QUARANTINE_LOGGER
            and isinstance(r.msg, dict)
            and r.msg.get("event") == event_name
        )
    ]


class TestPingpongConstructor:
    """Constructor validation for the new T6.17 + T6.18 thresholds."""

    def test_rejects_non_positive_pingpong_threshold(self) -> None:
        with pytest.raises(ValueError, match="pingpong_threshold must be positive"):
            EndpointQuarantine(quarantine_s=60.0, pingpong_threshold=0)
        with pytest.raises(ValueError, match="pingpong_threshold must be positive"):
            EndpointQuarantine(quarantine_s=60.0, pingpong_threshold=-3)

    def test_rejects_non_positive_pingpong_window_s(self) -> None:
        with pytest.raises(ValueError, match="pingpong_window_s must be positive"):
            EndpointQuarantine(quarantine_s=60.0, pingpong_window_s=0.0)
        with pytest.raises(ValueError, match="pingpong_window_s must be positive"):
            EndpointQuarantine(quarantine_s=60.0, pingpong_window_s=-30.0)

    def test_rejects_negative_rapid_requarantine_window_s(self) -> None:
        # Zero IS allowed (effectively disables T6.18 emission); negative is not.
        EndpointQuarantine(quarantine_s=60.0, rapid_requarantine_window_s=0.0)
        with pytest.raises(ValueError, match="rapid_requarantine_window_s must be non-negative"):
            EndpointQuarantine(quarantine_s=60.0, rapid_requarantine_window_s=-1.0)


class TestPingpongDetection:
    """T6.17 — re-quarantine count within window triggers the event."""

    def test_below_threshold_no_emission(
        self,
        clock: _FakeClock,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        import logging

        caplog.set_level(logging.WARNING, logger=_QUARANTINE_LOGGER)

        store = EndpointQuarantine(
            quarantine_s=60.0,
            clock=clock,
            pingpong_threshold=3,
            pingpong_window_s=300.0,
        )
        # 2 adds — under the threshold of 3.
        store.add(endpoint_guid="{A}")
        store.add(endpoint_guid="{A}")
        assert _events_of(caplog, "voice_quarantine_re_quarantine_event") == []

    def test_threshold_reached_emits_event(
        self,
        clock: _FakeClock,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        import logging

        caplog.set_level(logging.WARNING, logger=_QUARANTINE_LOGGER)

        store = EndpointQuarantine(
            quarantine_s=60.0,
            clock=clock,
            pingpong_threshold=3,
            pingpong_window_s=300.0,
        )
        store.add(endpoint_guid="{A}", host_api="WASAPI")
        store.add(endpoint_guid="{A}", host_api="WASAPI")
        store.add(endpoint_guid="{A}", host_api="WASAPI")
        events = _events_of(caplog, "voice_quarantine_re_quarantine_event")
        assert len(events) == 1
        assert events[0]["count_in_window"] == 3
        assert events[0]["threshold"] == 3
        assert events[0]["window_s"] == 300.0
        assert events[0]["endpoint"] == "{A}"

    def test_window_expiry_resets_count(
        self,
        clock: _FakeClock,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        import logging

        caplog.set_level(logging.WARNING, logger=_QUARANTINE_LOGGER)

        store = EndpointQuarantine(
            quarantine_s=60.0,
            clock=clock,
            pingpong_threshold=3,
            pingpong_window_s=300.0,
        )
        # Two adds, then advance past the window, then one more — must
        # NOT trigger because the older adds fall outside the window.
        store.add(endpoint_guid="{A}")
        store.add(endpoint_guid="{A}")
        clock.advance(301.0)  # Past the 300 s window.
        store.add(endpoint_guid="{A}")
        assert _events_of(caplog, "voice_quarantine_re_quarantine_event") == []

    def test_separate_endpoints_have_independent_counts(
        self,
        clock: _FakeClock,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        import logging

        caplog.set_level(logging.WARNING, logger=_QUARANTINE_LOGGER)

        store = EndpointQuarantine(
            quarantine_s=60.0,
            clock=clock,
            pingpong_threshold=3,
            pingpong_window_s=300.0,
        )
        store.add(endpoint_guid="{A}")
        store.add(endpoint_guid="{B}")
        store.add(endpoint_guid="{A}")
        store.add(endpoint_guid="{B}")
        # Each endpoint at 2 — under threshold for both.
        assert _events_of(caplog, "voice_quarantine_re_quarantine_event") == []

    def test_threshold_triggers_on_every_add_after_first_hit(
        self,
        clock: _FakeClock,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        # Once threshold is met, subsequent adds within the window
        # also trigger — the event surfaces sustained ping-pong, not
        # just the threshold-crossing edge. Operators get repeated
        # signals if the condition persists.
        import logging

        caplog.set_level(logging.WARNING, logger=_QUARANTINE_LOGGER)

        store = EndpointQuarantine(
            quarantine_s=60.0,
            clock=clock,
            pingpong_threshold=3,
            pingpong_window_s=300.0,
        )
        for _ in range(5):
            store.add(endpoint_guid="{A}")
        events = _events_of(caplog, "voice_quarantine_re_quarantine_event")
        # Threshold met at add #3, then again at #4 and #5.
        assert len(events) == 3


# ── T6.18 — TTL-expiry rapid re-quarantine ──────────────────────────


class TestRapidRequarantine:
    """T6.18 — endpoint re-added shortly after TTL-expiry purge."""

    def test_re_add_after_natural_expiry_within_window_emits(
        self,
        clock: _FakeClock,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        import logging

        caplog.set_level(logging.WARNING, logger=_QUARANTINE_LOGGER)

        store = EndpointQuarantine(
            quarantine_s=60.0,
            clock=clock,
            rapid_requarantine_window_s=60.0,
        )
        store.add(endpoint_guid="{A}", host_api="WASAPI")
        # Advance past TTL — purge_expired records the expiry.
        clock.advance(70.0)
        store.purge_expired()
        # Re-add shortly after — within rapid window.
        clock.advance(30.0)
        store.add(endpoint_guid="{A}", host_api="WASAPI")

        events = _events_of(caplog, "voice_endpoint_repeatedly_failing")
        assert len(events) == 1
        assert events[0]["endpoint"] == "{A}"
        assert events[0]["seconds_since_expiry"] == pytest.approx(30.0)

    def test_re_add_after_window_does_not_emit(
        self,
        clock: _FakeClock,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        import logging

        caplog.set_level(logging.WARNING, logger=_QUARANTINE_LOGGER)

        store = EndpointQuarantine(
            quarantine_s=60.0,
            clock=clock,
            rapid_requarantine_window_s=60.0,
        )
        store.add(endpoint_guid="{A}")
        clock.advance(70.0)
        store.purge_expired()
        # Re-add LONG after rapid window.
        clock.advance(300.0)
        store.add(endpoint_guid="{A}")

        assert _events_of(caplog, "voice_endpoint_repeatedly_failing") == []

    def test_lazy_purge_via_is_quarantined_records_expiry(
        self,
        clock: _FakeClock,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        # is_quarantined() lazily purges expired entries on lookup.
        # The lazy-purge path must also feed _recent_expiries so a
        # subsequent add() within the rapid window fires T6.18.
        import logging

        caplog.set_level(logging.WARNING, logger=_QUARANTINE_LOGGER)

        store = EndpointQuarantine(
            quarantine_s=60.0,
            clock=clock,
            rapid_requarantine_window_s=60.0,
        )
        store.add(endpoint_guid="{A}")
        clock.advance(65.0)
        # Lazy purge path — no purge_expired() call.
        assert store.is_quarantined("{A}") is False
        clock.advance(10.0)
        store.add(endpoint_guid="{A}")
        events = _events_of(caplog, "voice_endpoint_repeatedly_failing")
        assert len(events) == 1

    def test_first_add_does_not_emit(
        self,
        clock: _FakeClock,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        # Sanity — the very first add for an endpoint can't be a "re-add".
        import logging

        caplog.set_level(logging.WARNING, logger=_QUARANTINE_LOGGER)

        store = EndpointQuarantine(
            quarantine_s=60.0,
            clock=clock,
            rapid_requarantine_window_s=60.0,
        )
        store.add(endpoint_guid="{NEW}")
        assert _events_of(caplog, "voice_endpoint_repeatedly_failing") == []

    def test_explicit_clear_does_not_trigger_rapid_event(
        self,
        clock: _FakeClock,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        # An operator-driven clear() (e.g., hot-plug recovery) is NOT
        # an expiry — re-adding after a clear should NOT fire T6.18.
        import logging

        caplog.set_level(logging.WARNING, logger=_QUARANTINE_LOGGER)

        store = EndpointQuarantine(
            quarantine_s=60.0,
            clock=clock,
            rapid_requarantine_window_s=60.0,
        )
        store.add(endpoint_guid="{A}")
        store.clear("{A}", reason="hotplug")
        clock.advance(10.0)
        store.add(endpoint_guid="{A}")
        assert _events_of(caplog, "voice_endpoint_repeatedly_failing") == []

    def test_replacement_during_active_quarantine_does_not_trigger(
        self,
        clock: _FakeClock,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        # Re-adding while still quarantined (TTL not expired) is a
        # routine refresh — must NOT fire T6.18 (which is specifically
        # the "recovered then immediately re-failed" pattern).
        import logging

        caplog.set_level(logging.WARNING, logger=_QUARANTINE_LOGGER)

        store = EndpointQuarantine(
            quarantine_s=60.0,
            clock=clock,
            rapid_requarantine_window_s=60.0,
        )
        store.add(endpoint_guid="{A}")
        clock.advance(10.0)
        store.add(endpoint_guid="{A}")  # Still in quarantine; refresh.
        assert _events_of(caplog, "voice_endpoint_repeatedly_failing") == []


# ── Tuning flag wiring ──────────────────────────────────────────────


class TestTuningFlagsExposed:
    def test_default_pingpong_threshold_is_3(self) -> None:
        from sovyx.engine.config import VoiceTuningConfig

        cfg = VoiceTuningConfig()
        assert cfg.quarantine_pingpong_threshold == 3

    def test_default_pingpong_window_s_is_300(self) -> None:
        from sovyx.engine.config import VoiceTuningConfig

        cfg = VoiceTuningConfig()
        assert cfg.quarantine_pingpong_window_s == 300.0

    def test_default_rapid_requarantine_window_is_60(self) -> None:
        from sovyx.engine.config import VoiceTuningConfig

        cfg = VoiceTuningConfig()
        assert cfg.quarantine_rapid_requarantine_window_s == 60.0

    def test_singleton_picks_up_tuning_defaults(self) -> None:
        # The factory-constructed singleton must wire all three knobs.
        store = get_default_quarantine(quarantine_s=60.0)
        # Internal access for the wire-check; production code never
        # reads these directly. Regression guard against forgetting
        # to plumb the kwargs through the factory.
        assert store._pingpong_threshold == 3
        assert store._pingpong_window_s == 300.0
        assert store._rapid_requarantine_window_s == 60.0


class TestQuarantineSPropertyExposed:
    """v0.31.3: ``EndpointQuarantine.quarantine_s`` property exposes the
    constructor's literal float so route handlers can clamp
    ``seconds_until_expiry`` to its honest upper bound. The property is
    read-only by contract — there is no setter; quarantine duration is
    immutable per store instance."""

    def test_quarantine_s_returns_constructor_value(self) -> None:
        store = EndpointQuarantine(quarantine_s=42.5)
        assert store.quarantine_s == 42.5  # noqa: PLR2004

    def test_quarantine_s_default_singleton_60s(self) -> None:
        """Tracks the production default sourced from
        ``VoiceTuningConfig.kernel_invalidated_quarantine_s``."""
        store = get_default_quarantine(quarantine_s=60.0)
        assert store.quarantine_s == 60.0  # noqa: PLR2004

    def test_quarantine_s_is_read_only(self) -> None:
        """The property has no setter — assignment must raise."""
        store = EndpointQuarantine(quarantine_s=60.0)
        with pytest.raises(AttributeError):
            store.quarantine_s = 30.0  # type: ignore[misc]
