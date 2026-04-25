"""Tests for :mod:`sovyx.voice._chaos` (TS3 chaos injection foundation).

Covers:

* ChaosSite enum + allowlist guard
* Global kill switch + per-site rate env-var contract
* should_inject() rate accuracy (deterministic with seed)
* Counter accuracy (injected/skipped/total)
* Malformed env var degrades to no-injection (loud-WARN)
* Thread safety under concurrent should_inject() calls

Reference: MISSION-voice-mixer-enterprise-refactor-2026-04-25 §6 TS3.
"""

from __future__ import annotations

import logging
import threading

import pytest

from sovyx.voice._chaos import (
    _ENABLED_ENV_VAR,
    _KNOWN_SITES,
    _RATE_ENV_VAR_PREFIX,
    _RATE_ENV_VAR_SUFFIX,
    ChaosInjector,
    ChaosSite,
)


def _rate_var(site_id: str) -> str:
    return f"{_RATE_ENV_VAR_PREFIX}{site_id.upper()}{_RATE_ENV_VAR_SUFFIX}"


# ── ChaosSite enum ────────────────────────────────────────────────


class TestChaosSiteEnum:
    def test_values_are_lowercase_snake(self) -> None:
        for site in ChaosSite:
            assert site.value == site.value.lower()
            assert " " not in site.value

    def test_str_enum_value_comparison(self) -> None:
        """Anti-pattern #9 — string equality must work."""
        assert ChaosSite.STT_TIMEOUT == "stt_timeout"

    def test_known_sites_matches_enum(self) -> None:
        assert frozenset(s.value for s in ChaosSite) == _KNOWN_SITES


# ── ChaosInjector construction ────────────────────────────────────


class TestChaosInjectorInit:
    def test_unknown_site_id_rejected(self) -> None:
        with pytest.raises(ValueError, match="not in the chaos allowlist"):
            ChaosInjector(site_id="totally_made_up")

    def test_known_site_id_accepted(self) -> None:
        injector = ChaosInjector(site_id=ChaosSite.STT_TIMEOUT.value)
        assert injector.site_id == ChaosSite.STT_TIMEOUT.value

    def test_initial_counters_zero(self) -> None:
        injector = ChaosInjector(site_id=ChaosSite.STT_TIMEOUT.value)
        assert injector.injected_count == 0
        assert injector.skipped_count == 0
        assert injector.total_count == 0
        assert injector.realised_rate_pct == 0.0


# ── Global kill switch ────────────────────────────────────────────


class TestKillSwitch:
    def test_disabled_by_default(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.delenv(_ENABLED_ENV_VAR, raising=False)
        # Even at 100% rate, no injections when global is off.
        monkeypatch.setenv(_rate_var(ChaosSite.STT_TIMEOUT.value), "100")
        injector = ChaosInjector(site_id=ChaosSite.STT_TIMEOUT.value, seed=42)
        for _ in range(50):
            assert injector.should_inject() is False
        assert injector.injected_count == 0
        assert injector.skipped_count == 50

    def test_enabled_via_true(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv(_ENABLED_ENV_VAR, "true")
        monkeypatch.setenv(_rate_var(ChaosSite.STT_TIMEOUT.value), "100")
        injector = ChaosInjector(site_id=ChaosSite.STT_TIMEOUT.value, seed=42)
        # 100% rate → every call injects.
        for _ in range(20):
            assert injector.should_inject() is True

    def test_enabled_via_one(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv(_ENABLED_ENV_VAR, "1")
        monkeypatch.setenv(_rate_var(ChaosSite.STT_TIMEOUT.value), "100")
        injector = ChaosInjector(site_id=ChaosSite.STT_TIMEOUT.value, seed=42)
        assert injector.should_inject() is True

    def test_enabled_via_yes(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv(_ENABLED_ENV_VAR, "YES")
        monkeypatch.setenv(_rate_var(ChaosSite.STT_TIMEOUT.value), "100")
        injector = ChaosInjector(site_id=ChaosSite.STT_TIMEOUT.value, seed=42)
        assert injector.should_inject() is True

    def test_ambiguous_value_treated_as_disabled(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """`"on"` / `"yep"` are NOT recognised — strict bool."""
        monkeypatch.setenv(_ENABLED_ENV_VAR, "on")
        monkeypatch.setenv(_rate_var(ChaosSite.STT_TIMEOUT.value), "100")
        injector = ChaosInjector(site_id=ChaosSite.STT_TIMEOUT.value, seed=42)
        assert injector.should_inject() is False


# ── Rate accuracy ─────────────────────────────────────────────────


class TestRateAccuracy:
    def test_zero_rate_never_injects(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv(_ENABLED_ENV_VAR, "true")
        monkeypatch.setenv(_rate_var(ChaosSite.STT_TIMEOUT.value), "0")
        injector = ChaosInjector(site_id=ChaosSite.STT_TIMEOUT.value, seed=42)
        for _ in range(100):
            assert injector.should_inject() is False
        assert injector.injected_count == 0

    def test_unset_rate_never_injects(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv(_ENABLED_ENV_VAR, "true")
        monkeypatch.delenv(_rate_var(ChaosSite.STT_TIMEOUT.value), raising=False)
        injector = ChaosInjector(site_id=ChaosSite.STT_TIMEOUT.value, seed=42)
        for _ in range(50):
            assert injector.should_inject() is False

    def test_100_percent_always_injects(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv(_ENABLED_ENV_VAR, "true")
        monkeypatch.setenv(_rate_var(ChaosSite.STT_TIMEOUT.value), "100")
        injector = ChaosInjector(site_id=ChaosSite.STT_TIMEOUT.value, seed=42)
        for _ in range(100):
            assert injector.should_inject() is True

    def test_rate_within_confidence_interval(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """At 10% rate over 1000 trials, realised rate should be
        within ±5% of target. Wider interval would let regressions
        slip through; tighter would flake on outliers."""
        monkeypatch.setenv(_ENABLED_ENV_VAR, "true")
        monkeypatch.setenv(_rate_var(ChaosSite.STT_TIMEOUT.value), "10")
        injector = ChaosInjector(site_id=ChaosSite.STT_TIMEOUT.value, seed=42)
        n = 1000
        for _ in range(n):
            injector.should_inject()
        realised = injector.realised_rate_pct
        # 10% ± 5% absolute → [5, 15].
        assert 5.0 <= realised <= 15.0

    def test_deterministic_with_same_seed(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Two injectors with the same seed produce the same
        decision sequence."""
        monkeypatch.setenv(_ENABLED_ENV_VAR, "true")
        monkeypatch.setenv(_rate_var(ChaosSite.STT_TIMEOUT.value), "30")
        a = ChaosInjector(site_id=ChaosSite.STT_TIMEOUT.value, seed=99)
        b = ChaosInjector(site_id=ChaosSite.STT_TIMEOUT.value, seed=99)
        decisions_a = [a.should_inject() for _ in range(50)]
        decisions_b = [b.should_inject() for _ in range(50)]
        assert decisions_a == decisions_b


# ── Counter accuracy ──────────────────────────────────────────────


class TestCounters:
    def test_total_equals_injected_plus_skipped(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv(_ENABLED_ENV_VAR, "true")
        monkeypatch.setenv(_rate_var(ChaosSite.STT_TIMEOUT.value), "50")
        injector = ChaosInjector(site_id=ChaosSite.STT_TIMEOUT.value, seed=7)
        for _ in range(100):
            injector.should_inject()
        assert injector.total_count == 100
        assert injector.injected_count + injector.skipped_count == 100

    def test_reset_zeros_counters(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv(_ENABLED_ENV_VAR, "true")
        monkeypatch.setenv(_rate_var(ChaosSite.STT_TIMEOUT.value), "100")
        injector = ChaosInjector(site_id=ChaosSite.STT_TIMEOUT.value, seed=7)
        for _ in range(10):
            injector.should_inject()
        injector.reset_counters()
        assert injector.injected_count == 0
        assert injector.skipped_count == 0
        assert injector.total_count == 0


# ── Malformed env var ─────────────────────────────────────────────


class TestMalformedEnv:
    def test_non_integer_rate_warns_and_degrades(
        self,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        monkeypatch.setenv(_ENABLED_ENV_VAR, "true")
        monkeypatch.setenv(_rate_var(ChaosSite.STT_TIMEOUT.value), "ten percent")
        injector = ChaosInjector(site_id=ChaosSite.STT_TIMEOUT.value, seed=42)
        with caplog.at_level(logging.WARNING):
            for _ in range(10):
                assert injector.should_inject() is False
        assert any("voice.chaos.malformed_rate_env_var" in str(r.msg) for r in caplog.records)

    def test_out_of_range_rate_warns_and_degrades(
        self,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        monkeypatch.setenv(_ENABLED_ENV_VAR, "true")
        monkeypatch.setenv(_rate_var(ChaosSite.STT_TIMEOUT.value), "150")
        injector = ChaosInjector(site_id=ChaosSite.STT_TIMEOUT.value, seed=42)
        with caplog.at_level(logging.WARNING):
            for _ in range(10):
                assert injector.should_inject() is False
        assert any("voice.chaos.rate_out_of_range" in str(r.msg) for r in caplog.records)

    def test_negative_rate_warns_and_degrades(
        self,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        monkeypatch.setenv(_ENABLED_ENV_VAR, "true")
        monkeypatch.setenv(_rate_var(ChaosSite.STT_TIMEOUT.value), "-5")
        injector = ChaosInjector(site_id=ChaosSite.STT_TIMEOUT.value, seed=42)
        with caplog.at_level(logging.WARNING):
            for _ in range(5):
                assert injector.should_inject() is False
        assert any("voice.chaos.rate_out_of_range" in str(r.msg) for r in caplog.records)


# ── Per-site isolation ────────────────────────────────────────────


class TestPerSiteIsolation:
    def test_distinct_sites_use_distinct_env_vars(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv(_ENABLED_ENV_VAR, "true")
        # Site A at 100%, Site B at 0%.
        monkeypatch.setenv(_rate_var(ChaosSite.STT_TIMEOUT.value), "100")
        monkeypatch.setenv(_rate_var(ChaosSite.TTS_ZERO_ENERGY.value), "0")
        a = ChaosInjector(site_id=ChaosSite.STT_TIMEOUT.value, seed=1)
        b = ChaosInjector(site_id=ChaosSite.TTS_ZERO_ENERGY.value, seed=1)
        for _ in range(10):
            assert a.should_inject() is True
            assert b.should_inject() is False


# ── Thread safety ─────────────────────────────────────────────────


class TestThreadSafety:
    def test_concurrent_inject_decisions_consistent(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv(_ENABLED_ENV_VAR, "true")
        monkeypatch.setenv(_rate_var(ChaosSite.STT_TIMEOUT.value), "50")
        injector = ChaosInjector(site_id=ChaosSite.STT_TIMEOUT.value, seed=42)
        n_threads = 8
        per_thread = 100
        barrier = threading.Barrier(n_threads)

        def worker() -> None:
            barrier.wait()
            for _ in range(per_thread):
                injector.should_inject()

        threads = [threading.Thread(target=worker) for _ in range(n_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        # No counter loss under concurrent access.
        assert injector.total_count == n_threads * per_thread
        assert injector.injected_count + injector.skipped_count == n_threads * per_thread
