"""Tests for ``sovyx.voice.health._probe_result_cache``.

Mission anchor: ``docs-internal/missions/MISSION-c3-failover-ladder-iteration-2026-05-16.md``
§T2.1.

Pin the cache invariants:

* ``record_probe`` stores latest-wins per ``(endpoint_guid, host_api)``.
* ``record_success`` invalidates dead entries (ADR-D5).
* ``is_known_unopenable`` returns ``True`` only for the
  ``{NO_SIGNAL, INOPERATIVE}`` verdict + ``UNOPENABLE_*`` error
  classes (ADR-D4).
* Cardinality cap evicts oldest entry deterministically.
* Empty ``endpoint_guid`` is a no-op (observability hygiene).
* Module-level singleton is lazy + resettable.
* ``entries()`` returns a snapshot sorted newest-first.

Tests are synchronous + deterministic (cache is pure-Python state).
"""

from __future__ import annotations

import time

import pytest

from sovyx.voice.health._probe_result_cache import (
    ProbeResultCache,
    ProbeResultEntry,
    get_default_probe_result_cache,
    reset_default_probe_result_cache,
)


@pytest.fixture(autouse=True)
def _reset_singleton() -> None:
    """Ensure the singleton is fresh per test."""
    reset_default_probe_result_cache()


def _entry(
    endpoint_guid: str,
    host_api: str = "ALSA",
    *,
    verdict: str = "HEALTHY",
    error_code: str = "",
    error_detail: str = "",
    callbacks_fired: int = 50,
    rms_db: float = -55.0,
    monotonic_ts: float = 0.0,
) -> ProbeResultEntry:
    return ProbeResultEntry(
        endpoint_guid=endpoint_guid,
        host_api=host_api,
        verdict=verdict,
        error_code=error_code,
        error_detail=error_detail,
        callbacks_fired=callbacks_fired,
        rms_db=rms_db,
        monotonic_ts=monotonic_ts,
    )


class TestRecordAndLookup:
    """``record_probe`` stores; ``lookup`` retrieves the latest."""

    def test_record_then_lookup_returns_same_entry(self) -> None:
        cache = ProbeResultCache()
        entry = _entry("dev_a")
        cache.record_probe(entry)
        looked_up = cache.lookup("dev_a", "ALSA")
        assert looked_up is not None
        assert looked_up.endpoint_guid == "dev_a"
        assert looked_up.verdict == "HEALTHY"

    def test_lookup_missing_returns_none(self) -> None:
        cache = ProbeResultCache()
        assert cache.lookup("dev_a", "ALSA") is None

    def test_latest_wins(self) -> None:
        cache = ProbeResultCache()
        cache.record_probe(_entry("dev_a", verdict="HEALTHY"))
        cache.record_probe(_entry("dev_a", verdict="NO_SIGNAL"))
        looked_up = cache.lookup("dev_a", "ALSA")
        assert looked_up is not None
        assert looked_up.verdict == "NO_SIGNAL"

    def test_distinct_host_api_keys(self) -> None:
        """Same endpoint_guid + different host_api = distinct entries."""
        cache = ProbeResultCache()
        cache.record_probe(_entry("dev_a", host_api="WASAPI", verdict="HEALTHY"))
        cache.record_probe(_entry("dev_a", host_api="ALSA", verdict="NO_SIGNAL"))
        assert cache.lookup("dev_a", "WASAPI") is not None
        assert cache.lookup("dev_a", "ALSA") is not None
        assert cache.lookup("dev_a", "WASAPI").verdict == "HEALTHY"  # type: ignore[union-attr]
        assert cache.lookup("dev_a", "ALSA").verdict == "NO_SIGNAL"  # type: ignore[union-attr]

    def test_empty_endpoint_guid_is_noop(self) -> None:
        """ADR-D3 observability hygiene — silent skip on missing key."""
        cache = ProbeResultCache()
        cache.record_probe(_entry(""))
        assert len(cache) == 0

    def test_monotonic_ts_auto_populated(self) -> None:
        """When ``monotonic_ts=0.0`` is recorded, the cache auto-fills."""
        cache = ProbeResultCache()
        before = time.monotonic()
        cache.record_probe(_entry("dev_a", monotonic_ts=0.0))
        after = time.monotonic()
        looked_up = cache.lookup("dev_a", "ALSA")
        assert looked_up is not None
        assert before <= looked_up.monotonic_ts <= after


class TestRecordSuccessInvalidation:
    """ADR-D5 — successful open clears the dead entry."""

    def test_record_success_clears_existing_entry(self) -> None:
        cache = ProbeResultCache()
        cache.record_probe(_entry("dev_a", verdict="NO_SIGNAL"))
        assert cache.lookup("dev_a", "ALSA") is not None
        cache.record_success("dev_a", "ALSA")
        assert cache.lookup("dev_a", "ALSA") is None

    def test_record_success_missing_key_is_noop(self) -> None:
        cache = ProbeResultCache()
        cache.record_success("dev_a", "ALSA")  # MUST NOT raise
        assert len(cache) == 0

    def test_record_success_with_empty_guid_is_noop(self) -> None:
        cache = ProbeResultCache()
        cache.record_probe(_entry("dev_a"))
        cache.record_success("", "ALSA")  # MUST NOT clear dev_a
        assert cache.lookup("dev_a", "ALSA") is not None


class TestIsKnownUnopenable:
    """ADR-D4 — skip-on-bad-probe granularity."""

    def test_no_signal_verdict_is_skip(self) -> None:
        cache = ProbeResultCache()
        cache.record_probe(_entry("dev_a", verdict="NO_SIGNAL"))
        assert cache.is_known_unopenable("dev_a", "ALSA") is True

    def test_inoperative_verdict_is_skip(self) -> None:
        cache = ProbeResultCache()
        cache.record_probe(_entry("dev_a", verdict="INOPERATIVE"))
        assert cache.is_known_unopenable("dev_a", "ALSA") is True

    def test_lowercase_verdict_also_skip(self) -> None:
        """StrEnum lowercases the value side."""
        cache = ProbeResultCache()
        cache.record_probe(_entry("dev_a", verdict="no_signal"))
        assert cache.is_known_unopenable("dev_a", "ALSA") is True

    def test_healthy_verdict_not_skip(self) -> None:
        cache = ProbeResultCache()
        cache.record_probe(_entry("dev_a", verdict="HEALTHY"))
        assert cache.is_known_unopenable("dev_a", "ALSA") is False

    def test_unopenable_permanent_error_is_skip(self) -> None:
        """-9996 paInvalidDevice classifies as UNOPENABLE_PERMANENT."""
        cache = ProbeResultCache()
        cache.record_probe(_entry("dev_a", verdict="", error_code="-9996"))
        assert cache.is_known_unopenable("dev_a", "ALSA") is True

    def test_unopenable_this_boot_error_is_skip(self) -> None:
        """-9985 paDeviceUnavailable classifies as UNOPENABLE_THIS_BOOT."""
        cache = ProbeResultCache()
        cache.record_probe(_entry("dev_a", verdict="", error_code="-9985"))
        assert cache.is_known_unopenable("dev_a", "ALSA") is True

    def test_format_retryable_error_is_not_skip(self) -> None:
        """-9986 paInvalidSampleRate is FORMAT_RETRYABLE — opener handles."""
        cache = ProbeResultCache()
        cache.record_probe(_entry("dev_a", verdict="", error_code="-9986"))
        assert cache.is_known_unopenable("dev_a", "ALSA") is False

    def test_transient_error_is_not_skip(self) -> None:
        """AUDCLNT_E_DEVICE_IN_USE is TRANSIENT — retry-same-device."""
        cache = ProbeResultCache()
        cache.record_probe(
            _entry("dev_a", verdict="", error_code="audclnt_e_device_in_use"),
        )
        assert cache.is_known_unopenable("dev_a", "ALSA") is False

    def test_missing_entry_is_not_skip(self) -> None:
        """Conservative default — absence of info means don't skip."""
        cache = ProbeResultCache()
        assert cache.is_known_unopenable("dev_a", "ALSA") is False

    def test_empty_guid_is_not_skip(self) -> None:
        cache = ProbeResultCache()
        assert cache.is_known_unopenable("", "ALSA") is False

    def test_detail_string_fallback(self) -> None:
        cache = ProbeResultCache()
        cache.record_probe(
            _entry(
                "dev_a",
                verdict="",
                error_code="",
                error_detail="invalid device",
            ),
        )
        assert cache.is_known_unopenable("dev_a", "ALSA") is True


class TestCardinalityCap:
    """Cardinality ceiling evicts oldest deterministically."""

    def test_cap_evicts_oldest(self) -> None:
        cache = ProbeResultCache()
        # Fill to max + 1 with strictly-monotonic timestamps.
        cap = ProbeResultCache._MAX_ENTRIES  # noqa: SLF001
        for i in range(cap):
            cache.record_probe(
                _entry(
                    f"dev_{i}",
                    monotonic_ts=1000.0 + float(i),
                ),
            )
        assert len(cache) == cap
        # Insert one more — should evict dev_0 (oldest).
        cache.record_probe(
            _entry(f"dev_{cap}", monotonic_ts=1000.0 + float(cap)),
        )
        assert len(cache) == cap
        assert cache.lookup("dev_0", "ALSA") is None
        assert cache.lookup(f"dev_{cap}", "ALSA") is not None

    def test_overwrite_same_key_does_not_count_as_new(self) -> None:
        cache = ProbeResultCache()
        cache.record_probe(_entry("dev_a", verdict="HEALTHY"))
        for _ in range(20):
            cache.record_probe(_entry("dev_a", verdict="NO_SIGNAL"))
        assert len(cache) == 1


class TestEntriesSnapshot:
    """``entries()`` returns a snapshot sorted newest-first."""

    def test_entries_sorted_newest_first(self) -> None:
        cache = ProbeResultCache()
        cache.record_probe(_entry("dev_a", monotonic_ts=100.0))
        cache.record_probe(_entry("dev_b", monotonic_ts=200.0))
        cache.record_probe(_entry("dev_c", monotonic_ts=150.0))
        entries = cache.entries()
        assert [e.endpoint_guid for e in entries] == ["dev_b", "dev_c", "dev_a"]

    def test_entries_returns_fresh_list_each_call(self) -> None:
        cache = ProbeResultCache()
        cache.record_probe(_entry("dev_a"))
        first = cache.entries()
        second = cache.entries()
        assert first is not second  # different list instances


class TestSingleton:
    """Module-level lazy singleton."""

    def test_get_returns_same_instance(self) -> None:
        a = get_default_probe_result_cache()
        b = get_default_probe_result_cache()
        assert a is b

    def test_reset_drops_singleton(self) -> None:
        a = get_default_probe_result_cache()
        reset_default_probe_result_cache()
        b = get_default_probe_result_cache()
        assert a is not b

    def test_singleton_starts_empty(self) -> None:
        cache = get_default_probe_result_cache()
        assert len(cache) == 0


# ─────────────────────────────────────────────────────────────────────
# HEALTH-3 (2026-07-02) — AP #53 key discipline. The boot-cascade
# producer keys with planner literals ("WASAPI") + endpoint GUIDs; the
# runtime-failover consumer keys with PortAudio labels
# ("Windows WASAPI") + ``DeviceEntry.canonical_name``. Producer and
# consumer must round-trip through ONE shared key derivation.
# ─────────────────────────────────────────────────────────────────────


class TestHostApiKeyNormalization:
    """Planner literals and PortAudio labels hit the same key."""

    @pytest.mark.parametrize(
        ("producer_label", "consumer_label"),
        [
            ("WASAPI", "Windows WASAPI"),
            ("DirectSound", "Windows DirectSound"),
            ("WDM-KS", "Windows WDM-KS"),
            ("MME", "MME"),
            ("ALSA", "ALSA"),
            ("CoreAudio", "Core Audio"),
        ],
    )
    def test_record_planner_lookup_portaudio(
        self,
        producer_label: str,
        consumer_label: str,
    ) -> None:
        cache = ProbeResultCache()
        cache.record_probe(
            _entry("dev_a", host_api=producer_label, verdict="NO_SIGNAL"),
        )
        assert cache.lookup("dev_a", consumer_label) is not None
        assert cache.is_known_unopenable("dev_a", consumer_label) is True

    def test_record_success_crosses_spellings(self) -> None:
        """ADR-D5 invalidation must clear an entry recorded under the
        planner spelling when the success reports the PortAudio label."""
        cache = ProbeResultCache()
        cache.record_probe(_entry("dev_a", host_api="WASAPI", verdict="NO_SIGNAL"))
        cache.record_success("dev_a", "Windows WASAPI")
        assert cache.lookup("dev_a", "WASAPI") is None

    def test_entry_keeps_verbatim_host_api_for_display(self) -> None:
        """Only the KEY is normalised — the entry's ``host_api`` field
        stays verbatim for the doctor CLI / dashboard widget."""
        cache = ProbeResultCache()
        cache.record_probe(_entry("dev_a", host_api="Windows WASAPI"))
        entry = cache.lookup("dev_a", "WASAPI")
        assert entry is not None
        assert entry.host_api == "Windows WASAPI"


class TestBootProducerRoundTrip:
    """Boot-cascade population is consumable through the runtime
    failover ladder's exact lookup keys (canonical_name + PortAudio
    host-API label)."""

    def test_log_probe_result_round_trips_to_ladder_keys(self) -> None:
        from sovyx.voice.health import Combo, Diagnosis, ProbeMode, ProbeResult
        from sovyx.voice.health.cascade._executor_helpers import _log_probe_result

        combo = Combo(
            host_api="WASAPI",
            sample_rate=16_000,
            channels=1,
            sample_format="int16",
            exclusive=False,
            auto_convert=True,
            frames_per_buffer=480,
            platform_key="win32",
        )
        result = ProbeResult(
            diagnosis=Diagnosis.NO_SIGNAL,
            mode=ProbeMode.COLD,
            combo=combo,
            vad_max_prob=None,
            vad_mean_prob=None,
            rms_db=-90.0,
            callbacks_fired=0,
            duration_ms=200,
            error=None,
        )

        _log_probe_result(
            endpoint_guid="{surrogate-razer-wasapi}",
            attempt=0,
            device_index=4,
            combo=combo,
            result=result,
            physical_device_id="razer blackshark v2 pro",
        )

        cache = get_default_probe_result_cache()
        # Consumer key form 1: the ladder loop body keys by
        # DeviceEntry.canonical_name + PortAudio host-API label.
        assert cache.is_known_unopenable("razer blackshark v2 pro", "Windows WASAPI") is True
        # Consumer key form 2: select_alternative_endpoint keys by the
        # derived endpoint GUID.
        assert cache.is_known_unopenable("{surrogate-razer-wasapi}", "Windows WASAPI") is True

    def test_log_probe_result_without_physical_id_records_guid_only(self) -> None:
        from sovyx.voice.health import Combo, Diagnosis, ProbeMode, ProbeResult
        from sovyx.voice.health.cascade._executor_helpers import _log_probe_result

        combo = Combo(
            host_api="ALSA",
            sample_rate=16_000,
            channels=1,
            sample_format="int16",
            exclusive=False,
            auto_convert=True,
            frames_per_buffer=480,
            platform_key="linux",
        )
        result = ProbeResult(
            diagnosis=Diagnosis.NO_SIGNAL,
            mode=ProbeMode.COLD,
            combo=combo,
            vad_max_prob=None,
            vad_mean_prob=None,
            rms_db=-90.0,
            callbacks_fired=0,
            duration_ms=200,
            error=None,
        )

        _log_probe_result(
            endpoint_guid="{linux-usb-mic}",
            attempt=0,
            device_index=1,
            combo=combo,
            result=result,
        )

        cache = get_default_probe_result_cache()
        assert cache.is_known_unopenable("{linux-usb-mic}", "ALSA") is True
        assert len(cache) == 1
