"""VAL-004 to VAL-007: Security, Permissions, Lifecycle, Entry Points."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

from sovyx.plugins.manager import PluginManager
from sovyx.plugins.official.calculator import CalculatorPlugin
from sovyx.plugins.permissions import (
    PermissionDeniedError,
    PermissionEnforcer,
    PluginAutoDisabledError,
)
from sovyx.plugins.sandbox_fs import SandboxedFsAccess
from sovyx.plugins.sandbox_http import SandboxedHttpClient
from sovyx.plugins.sdk import ISovyxPlugin
from sovyx.plugins.sdk import tool as tool_dec
from sovyx.plugins.security import ImportGuard, PluginSecurityScanner

# ═══════════════════════════════════════════════════════════════════
# VAL-004: Security — SSRF, FS Traversal, Import Bypass
# ═══════════════════════════════════════════════════════════════════


class TestSSRFProtection:
    """HTTP sandbox blocks SSRF attempts."""

    @pytest.mark.anyio()
    async def test_aws_metadata_blocked(self) -> None:
        client = SandboxedHttpClient(
            plugin_name="test",
            allowed_domains=["api.example.com"],
        )
        with pytest.raises(PermissionDeniedError):
            await client.get("http://169.254.169.254/latest/meta-data/")

    @pytest.mark.anyio()
    async def test_localhost_blocked(self) -> None:
        client = SandboxedHttpClient(
            plugin_name="test",
            allowed_domains=["api.example.com"],
        )
        with pytest.raises(PermissionDeniedError):
            await client.get("http://127.0.0.1/secret")

    @pytest.mark.anyio()
    async def test_domain_not_in_allowlist(self) -> None:
        client = SandboxedHttpClient(
            plugin_name="test",
            allowed_domains=["api.example.com"],
        )
        with pytest.raises(PermissionDeniedError):
            await client.get("http://evil.com/steal")


class TestFsTraversal:
    """Filesystem sandbox blocks path traversal."""

    @pytest.mark.anyio()
    async def test_traversal_blocked(self, tmp_path: Path) -> None:
        enforcer = PermissionEnforcer("test", {"fs:read", "fs:write"})
        fs = SandboxedFsAccess(
            plugin_name="test",
            data_dir=tmp_path,
            enforcer=enforcer,
        )
        with pytest.raises(PermissionDeniedError):
            await fs.read("../../etc/passwd")

    @pytest.mark.anyio()
    async def test_absolute_path_blocked(self, tmp_path: Path) -> None:
        enforcer = PermissionEnforcer("test", {"fs:read"})
        fs = SandboxedFsAccess(
            plugin_name="test",
            data_dir=tmp_path,
            enforcer=enforcer,
        )
        with pytest.raises(PermissionDeniedError):
            await fs.read("/etc/passwd")


class TestImportGuard:
    """ImportGuard blocks dangerous imports during plugin execution."""

    def test_blocked_module(self) -> None:
        # Remove from cache so ImportGuard can intercept
        saved = sys.modules.pop("antigravity", None)
        try:
            guard = ImportGuard("test", blocked=frozenset({"antigravity"}))
            with guard, pytest.raises(ImportError):
                __import__("antigravity")
        finally:
            if saved is not None:
                sys.modules["antigravity"] = saved

    def test_allowed_outside_guard(self) -> None:
        """Imports work normally outside the guard context."""
        import os  # noqa: F401 — proves no guard active


class TestASTScanner:
    """AST scanner catches dangerous patterns."""

    def test_eval_detected(self) -> None:
        scanner = PluginSecurityScanner()
        findings = scanner.scan_source("result = eval('1+1')\n")
        assert any("eval" in str(f).lower() for f in findings)

    def test_exec_detected(self) -> None:
        scanner = PluginSecurityScanner()
        findings = scanner.scan_source("exec('import os')\n")
        assert any("exec" in str(f).lower() for f in findings)

    def test_safe_code_clean(self) -> None:
        scanner = PluginSecurityScanner()
        findings = scanner.scan_source("x = 1 + 2\nprint(x)\n")
        assert len(findings) == 0


# ═══════════════════════════════════════════════════════════════════
# VAL-005: Permission Boundary
# ═══════════════════════════════════════════════════════════════════


class TestPermissionBoundary:
    """Permission enforcer correctly gates access."""

    def test_brain_read_only_no_write(self) -> None:
        enforcer = PermissionEnforcer("test", {"brain:read"})
        enforcer.check("brain:read")  # OK
        with pytest.raises(PermissionDeniedError):
            enforcer.check("brain:write")  # Denied

    def test_no_permissions_all_denied(self) -> None:
        enforcer = PermissionEnforcer("test", set())
        for perm in ["brain:read", "brain:write", "network:internet", "fs:read"]:
            with pytest.raises(PermissionDeniedError):
                enforcer.check(perm)

    def test_auto_disable_after_10_denials(self) -> None:
        enforcer = PermissionEnforcer("test", set(), max_denials=10)
        for _ in range(9):
            with pytest.raises(PermissionDeniedError):
                enforcer.check("brain:read")
        # 10th denial → auto-disable
        with pytest.raises(PluginAutoDisabledError):
            enforcer.check("brain:read")
        assert enforcer.is_disabled

    @pytest.mark.anyio()
    async def test_permission_denied_separate_from_failure_counter(self, tmp_path: Path) -> None:
        """PermissionDeniedError does NOT count toward 5-failure auto-disable."""
        mgr = PluginManager(data_dir=tmp_path, discover_entry_points=False)

        class PermPlugin(ISovyxPlugin):
            @property
            def name(self) -> str:
                return "perm-test"

            @property
            def version(self) -> str:
                return "1.0.0"

            @property
            def description(self) -> str:
                return "test"

            @tool_dec(description="Raises perm denied")
            async def check(self) -> str:
                raise PermissionDeniedError("perm-test", "brain:write")

        await mgr.load_single(PermPlugin())
        for _ in range(10):
            await mgr.execute("perm-test.check", {})

        health = mgr.get_plugin_health("perm-test")
        assert health["consecutive_failures"] == 0
        assert health["disabled"] is False


# ═══════════════════════════════════════════════════════════════════
# VAL-006: Lifecycle Stress
# ═══════════════════════════════════════════════════════════════════


class TestLifecycleStress:
    """Plugin lifecycle edge cases."""

    @pytest.mark.anyio()
    async def test_load_unload_reload_10x(self, tmp_path: Path) -> None:
        mgr = PluginManager(data_dir=tmp_path, discover_entry_points=False)
        for _ in range(10):
            await mgr.load_single(CalculatorPlugin())
            assert mgr.plugin_count == 1
            result = await mgr.execute("calculator.calculate", {"expression": "1+1"})
            assert result.output == "2"
            await mgr.unload("calculator")
            assert mgr.plugin_count == 0

    @pytest.mark.anyio()
    async def test_setup_failure_not_loaded(self, tmp_path: Path) -> None:
        class FailSetup(ISovyxPlugin):
            @property
            def name(self) -> str:
                return "fail-setup"

            @property
            def version(self) -> str:
                return "1.0.0"

            @property
            def description(self) -> str:
                return "test"

            async def setup(self, ctx: object) -> None:
                msg = "setup failed"
                raise RuntimeError(msg)

        mgr = PluginManager(data_dir=tmp_path, discover_entry_points=False)
        with pytest.raises(RuntimeError, match="setup failed"):
            await mgr.load_single(FailSetup())
        assert mgr.plugin_count == 0

    @pytest.mark.anyio()
    async def test_shutdown_with_zero_plugins(self, tmp_path: Path) -> None:
        mgr = PluginManager(data_dir=tmp_path, discover_entry_points=False)
        await mgr.shutdown()  # No crash
        assert mgr.plugin_count == 0

    @pytest.mark.anyio()
    async def test_concurrent_execute(self, tmp_path: Path) -> None:
        mgr = PluginManager(data_dir=tmp_path, discover_entry_points=False)
        await mgr.load_single(CalculatorPlugin())

        async def calc(expr: str) -> str:
            r = await mgr.execute("calculator.calculate", {"expression": expr})
            return r.output

        results = await asyncio.gather(*[calc(f"{i}+{i}") for i in range(20)])
        assert results == [str(i * 2) for i in range(20)]
        health = mgr.get_plugin_health("calculator")
        assert health["active_tasks"] == 0


# ═══════════════════════════════════════════════════════════════════
# VAL-007: Entry Points + Config
# ═══════════════════════════════════════════════════════════════════


class TestEntryPointsConfig:
    """Entry points discovery and config integration."""

    def test_entry_points_group_matches(self) -> None:
        """The group used in manager matches pyproject.toml."""

        # The group string in the manager
        with open("/root/sovyx/src/sovyx/plugins/manager.py") as f:
            content = f.read()
        assert 'group="sovyx.plugins"' in content

        # The group string in pyproject.toml
        with open("/root/sovyx/pyproject.toml") as f:
            toml = f.read()
        assert '[project.entry-points."sovyx.plugins"]' in toml

    @pytest.mark.anyio()
    async def test_enabled_empty_list_loads_nothing(self, tmp_path: Path) -> None:
        mgr = PluginManager(
            data_dir=tmp_path,
            enabled=set(),  # Empty = nothing
            discover_entry_points=False,
        )
        mgr.register_class(CalculatorPlugin)
        loaded = await mgr.load_all()
        assert len(loaded) == 0

    @pytest.mark.anyio()
    async def test_enabled_none_loads_all(self, tmp_path: Path) -> None:
        mgr = PluginManager(
            data_dir=tmp_path,
            enabled=None,  # None = all
            discover_entry_points=False,
        )
        mgr.register_class(CalculatorPlugin)
        loaded = await mgr.load_all()
        assert len(loaded) == 1
        assert "calculator" in loaded

    @pytest.mark.anyio()
    async def test_disabled_excludes(self, tmp_path: Path) -> None:
        mgr = PluginManager(
            data_dir=tmp_path,
            disabled={"calculator"},
            discover_entry_points=False,
        )
        mgr.register_class(CalculatorPlugin)
        loaded = await mgr.load_all()
        assert len(loaded) == 0

    @pytest.mark.anyio()
    async def test_plugin_in_both_enabled_and_disabled(self, tmp_path: Path) -> None:
        """Plugin in both enabled AND disabled → not loaded."""
        mgr = PluginManager(
            data_dir=tmp_path,
            enabled={"calculator"},
            disabled={"calculator"},
            discover_entry_points=False,
        )
        mgr.register_class(CalculatorPlugin)
        loaded = await mgr.load_all()
        assert len(loaded) == 0
