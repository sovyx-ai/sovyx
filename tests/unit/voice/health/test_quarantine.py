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
