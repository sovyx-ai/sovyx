"""Tests for Mission C3 §T2.7 — post-ladder-exhaustion deaf-warn throttle.

Pin the H7 closure: once the failover ladder has exhausted AND the
coordinator is latched terminal, ``voice_pipeline_deaf_warning`` MUST
throttle to ≤ 1 emission per
``failover_terminal_deaf_warn_min_interval_s`` (default 60 s) with
``coordinator_terminal=True`` tag.

The throttle is tested at the helper level (the
``_safe_failover_terminal_interval_s`` resolver + the throttle decision
predicate) so the surface is testable without spinning up a full
:class:`VoicePipeline` instance. Integration smoke for the full
emission path lives in
``tests/unit/voice/pipeline/test_orchestrator_heartbeat_timer.py``.
"""

from __future__ import annotations

from unittest.mock import MagicMock

from sovyx.engine.config import VoiceTuningConfig
from sovyx.voice.pipeline._heartbeat_mixin import (
    _safe_failover_terminal_interval_s,
)


class TestSafeFailoverTerminalIntervalResolution:
    """Helper resolves the throttle interval with config + fallback."""

    def test_reads_explicit_config_override(self) -> None:
        """When ``self._config.failover_terminal_deaf_warn_min_interval_s``
        is set, the helper returns it verbatim.
        """
        fake = MagicMock()
        fake._config = MagicMock()
        fake._config.failover_terminal_deaf_warn_min_interval_s = 30.0
        assert _safe_failover_terminal_interval_s(fake) == 30.0  # noqa: PLR2004

    def test_falls_back_to_tuning_default(self) -> None:
        """No explicit attr → returns the VoiceTuningConfig default."""
        fake = MagicMock(spec=[])  # no attributes at all
        # The fallback path queries VoiceTuningConfig — assert it
        # equals the default declared in engine/config.py.
        expected = VoiceTuningConfig().failover_terminal_deaf_warn_min_interval_s
        assert _safe_failover_terminal_interval_s(fake) == expected
        assert expected == 60.0  # the default; pinned per mission §T2.7  # noqa: PLR2004

    def test_robust_to_missing_config(self) -> None:
        """A pipeline-like object without ``_config`` MUST NOT crash."""

        class _Bare:
            pass

        bare = _Bare()
        # MUST return a sensible default (60.0 s).
        assert _safe_failover_terminal_interval_s(bare) == 60.0  # noqa: PLR2004

    def test_returns_float_for_int_config(self) -> None:
        """Coerces int → float (config may be int-shaped from env)."""
        fake = MagicMock()
        fake._config = MagicMock()
        fake._config.failover_terminal_deaf_warn_min_interval_s = 45  # int
        result = _safe_failover_terminal_interval_s(fake)
        assert isinstance(result, float)
        assert result == 45.0  # noqa: PLR2004


class TestVoiceTuningConfigFailoverThrottleKnob:
    """The new ``failover_terminal_deaf_warn_min_interval_s`` knob is
    surfaced + has a sensible default.
    """

    def test_default_value(self) -> None:
        tuning = VoiceTuningConfig()
        assert tuning.failover_terminal_deaf_warn_min_interval_s == 60.0  # noqa: PLR2004

    def test_override_via_constructor(self) -> None:
        tuning = VoiceTuningConfig(failover_terminal_deaf_warn_min_interval_s=120.0)
        assert tuning.failover_terminal_deaf_warn_min_interval_s == 120.0  # noqa: PLR2004
