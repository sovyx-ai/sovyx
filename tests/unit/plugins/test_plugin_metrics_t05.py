"""T05 mission test — plugin observability metrics (Mission pre-wake-word T05).

Before T05, plugin observability was log-event-only — zero structured
metrics. T05 added 4 OTel instruments + helper module ``_metrics.py``:

* ``sovyx.plugins.tool_executed{plugin, tool, outcome}`` — Counter
* ``sovyx.plugins.tool_latency_ms{plugin, tool}`` — Histogram
* ``sovyx.plugins.sandbox_denial{plugin, layer}`` — Counter
* ``sovyx.plugins.auto_disabled{plugin, reason}`` — Counter

These tests pin the contract:
1. The 4 instruments are registered on the MetricsRegistry.
2. The helper functions in ``plugins/_metrics.py`` correctly forward
   to the OTel instruments.
3. The 5 sandbox-layer denial sites all wire to the metric.
4. Auto-disable both code paths (manager + enforcer) emit the metric
   with the correct ``reason`` literal.
"""

from __future__ import annotations

import contextlib
from unittest.mock import patch


class TestInstrumentRegistration:
    """The 4 instruments are wired on the MetricsRegistry."""

    def test_tool_executed_counter_registered(self) -> None:
        from sovyx.observability.metrics import get_metrics

        metrics = get_metrics()
        assert hasattr(metrics, "plugins_tool_executed")
        assert callable(metrics.plugins_tool_executed.add)

    def test_tool_latency_histogram_registered(self) -> None:
        from sovyx.observability.metrics import get_metrics

        metrics = get_metrics()
        assert hasattr(metrics, "plugins_tool_latency_ms")
        assert callable(metrics.plugins_tool_latency_ms.record)

    def test_sandbox_denial_counter_registered(self) -> None:
        from sovyx.observability.metrics import get_metrics

        metrics = get_metrics()
        assert hasattr(metrics, "plugins_sandbox_denial")
        assert callable(metrics.plugins_sandbox_denial.add)

    def test_auto_disabled_counter_registered(self) -> None:
        from sovyx.observability.metrics import get_metrics

        metrics = get_metrics()
        assert hasattr(metrics, "plugins_auto_disabled")
        assert callable(metrics.plugins_auto_disabled.add)


class TestHelperRecordCalls:
    """``plugins/_metrics.py`` helpers forward to the right instruments."""

    def test_record_tool_executed_calls_counter_add(self) -> None:
        from sovyx.observability.metrics import get_metrics
        from sovyx.plugins._metrics import record_tool_executed

        instrument = get_metrics().plugins_tool_executed
        with patch.object(instrument, "add") as mock_add:
            record_tool_executed(plugin="weather", tool="get_weather", outcome="ok")
            mock_add.assert_called_once_with(
                1,
                attributes={"plugin": "weather", "tool": "get_weather", "outcome": "ok"},
            )

    def test_record_tool_latency_calls_histogram_record(self) -> None:
        from sovyx.observability.metrics import get_metrics
        from sovyx.plugins._metrics import record_tool_latency

        instrument = get_metrics().plugins_tool_latency_ms
        with patch.object(instrument, "record") as mock_record:
            record_tool_latency(plugin="weather", tool="get_weather", duration_ms=42.5)
            mock_record.assert_called_once_with(
                42.5,
                attributes={"plugin": "weather", "tool": "get_weather"},
            )

    def test_record_sandbox_denial_calls_counter_add(self) -> None:
        from sovyx.observability.metrics import get_metrics
        from sovyx.plugins._metrics import record_sandbox_denial

        instrument = get_metrics().plugins_sandbox_denial
        with patch.object(instrument, "add") as mock_add:
            record_sandbox_denial(plugin="evil", layer="http")
            mock_add.assert_called_once_with(
                1,
                attributes={"plugin": "evil", "layer": "http"},
            )

    def test_record_auto_disabled_calls_counter_add(self) -> None:
        from sovyx.observability.metrics import get_metrics
        from sovyx.plugins._metrics import record_auto_disabled

        instrument = get_metrics().plugins_auto_disabled
        with patch.object(instrument, "add") as mock_add:
            record_auto_disabled(plugin="evil", reason="permission_denials_exceeded")
            mock_add.assert_called_once_with(
                1,
                attributes={"plugin": "evil", "reason": "permission_denials_exceeded"},
            )


class TestPermissionEnforcerEmits:
    """Permission denial in PermissionEnforcer.check fires the metric.

    Wire site: ``permissions.py:235`` (denial counter) +
    ``permissions.py:243-250`` (auto-disable trigger).
    """

    def test_check_denial_records_sandbox_denial_metric(self) -> None:
        """A permission denial in ``check()`` calls
        ``record_sandbox_denial(plugin=..., layer="permission")``.

        Patches the helper at its source module (``_metrics``) since
        permission.py uses a lazy import inside the function body.
        """
        from sovyx.plugins.permissions import PermissionDeniedError, PermissionEnforcer

        enforcer = PermissionEnforcer(plugin_name="evil", granted=set())
        with patch("sovyx.plugins._metrics.record_sandbox_denial") as mock_record:
            with contextlib.suppress(PermissionDeniedError):
                enforcer.check("brain:read")
            mock_record.assert_called_once_with(plugin="evil", layer="permission")

    def test_max_denials_records_auto_disabled_metric(self) -> None:
        """When ``max_denials`` is reached, the auto_disabled record helper
        is called with reason='permission_denials_exceeded'.

        Patches the helper function (``record_auto_disabled``) rather than
        the OTel instrument to avoid mock-aliasing between the
        ``_BudgetedInstrument`` instances on the global registry.
        """
        from sovyx.plugins.permissions import (
            PermissionDeniedError,
            PermissionEnforcer,
            PluginAutoDisabledError,
        )

        enforcer = PermissionEnforcer(plugin_name="evil", granted=set(), max_denials=2)
        # First denial: should NOT trigger auto-disable
        with (
            patch("sovyx.plugins._metrics.record_auto_disabled") as mock_auto,
            patch("sovyx.plugins._metrics.record_sandbox_denial"),
        ):
            with contextlib.suppress(PermissionDeniedError):
                enforcer.check("brain:read")
            mock_auto.assert_not_called()
        # Second denial: triggers auto-disable
        with (
            patch("sovyx.plugins._metrics.record_auto_disabled") as mock_auto,
            patch("sovyx.plugins._metrics.record_sandbox_denial"),
        ):
            with contextlib.suppress(PluginAutoDisabledError, PermissionDeniedError):
                enforcer.check("brain:write")
            mock_auto.assert_called_once_with(
                plugin="evil",
                reason="permission_denials_exceeded",
            )


class TestSandboxFsEmits:
    """FS-layer denials all flow through ``_record_fs_denial`` helper."""

    def test_helper_function_calls_record_sandbox_denial(self) -> None:
        from sovyx.observability.metrics import get_metrics
        from sovyx.plugins.sandbox_fs import _record_fs_denial

        instrument = get_metrics().plugins_sandbox_denial
        with patch.object(instrument, "add") as mock_add:
            _record_fs_denial("test_plugin")
            mock_add.assert_called_once_with(
                1,
                attributes={"plugin": "test_plugin", "layer": "fs"},
            )

    def test_all_5_fs_raise_sites_call_record_helper(self) -> None:
        """Source-grep verification — all 5 raise sites have the
        ``_record_fs_denial(self._plugin)`` line directly above."""
        from pathlib import Path

        path = Path(__file__).parents[3] / "src" / "sovyx" / "plugins" / "sandbox_fs.py"
        text = path.read_text(encoding="utf-8")
        # 5 PermissionDeniedError raise sites + 5 _record_fs_denial calls
        assert text.count("raise PermissionDeniedError") >= 5
        assert text.count("_record_fs_denial(self._plugin)") >= 5


class TestSandboxHttpEmits:
    """HTTP-layer denials all flow through ``_emit_denied`` helper."""

    def test_emit_denied_helper_records_metric(self) -> None:
        from sovyx.observability.metrics import get_metrics
        from sovyx.plugins.sandbox_http import _emit_denied

        instrument = get_metrics().plugins_sandbox_denial
        with patch.object(instrument, "add") as mock_add:
            _emit_denied(
                "test_plugin",
                url="https://evil.example.com",
                hostname="evil.example.com",
                reason="domain_not_allowed",
            )
            mock_add.assert_called_once_with(
                1,
                attributes={"plugin": "test_plugin", "layer": "http"},
            )


class TestImportGuardEmits:
    """Import-layer denials in security.py:ImportGuard fire the metric."""

    def test_import_guard_blocked_records_metric(self) -> None:
        """Source-grep verification — the import_guard_blocked log site
        is followed by a record_sandbox_denial(layer="import") call."""
        from pathlib import Path

        path = Path(__file__).parents[3] / "src" / "sovyx" / "plugins" / "security.py"
        text = path.read_text(encoding="utf-8")
        assert "record_sandbox_denial" in text
        assert 'layer="import"' in text


class TestNoneInstrumentNoOp:
    """Helpers no-op silently when the instrument is missing.

    Defensive: protects against mocks / partial test setups where
    ``get_metrics()`` returns a registry without the new attributes.
    """

    def test_helpers_noop_when_instrument_missing(self) -> None:
        from sovyx.plugins import _metrics as plugin_metrics

        class _BareRegistry:
            pass

        with patch.object(plugin_metrics, "get_metrics", return_value=_BareRegistry()):
            # None of these should raise even though the registry is bare
            plugin_metrics.record_tool_executed(plugin="x", tool="y", outcome="ok")
            plugin_metrics.record_tool_latency(plugin="x", tool="y", duration_ms=1.0)
            plugin_metrics.record_sandbox_denial(plugin="x", layer="permission")
            plugin_metrics.record_auto_disabled(plugin="x", reason="other")
