"""Tests for Sovyx Plugin Permissions — Permission enum + PermissionEnforcer.

Coverage target: ≥95% on plugins/permissions.py
"""

from __future__ import annotations

import pytest

from sovyx.plugins.permissions import (
    PERMISSION_DESCRIPTIONS,
    PERMISSION_RISK,
    Permission,
    PermissionDeniedError,
    PermissionEnforcer,
    PluginAutoDisabledError,
    get_description,
    get_risk,
    get_risk_emoji,
)

# ── Permission Enum ─────────────────────────────────────────────────


class TestPermission:
    """Tests for Permission enum."""

    def test_all_permissions_have_values(self) -> None:
        """Every Permission has a colon-separated string value."""
        for perm in Permission:
            assert ":" in perm.value or perm == Permission.PROACTIVE

    def test_brain_permissions(self) -> None:
        assert Permission.BRAIN_READ.value == "brain:read"
        assert Permission.BRAIN_WRITE.value == "brain:write"

    def test_network_permissions(self) -> None:
        assert Permission.NETWORK_LOCAL.value == "network:local"
        assert Permission.NETWORK_INTERNET.value == "network:internet"

    def test_fs_permissions(self) -> None:
        assert Permission.FS_READ.value == "fs:read"
        assert Permission.FS_WRITE.value == "fs:write"

    def test_event_permissions(self) -> None:
        assert Permission.EVENT_SUBSCRIBE.value == "event:subscribe"
        assert Permission.EVENT_EMIT.value == "event:emit"

    def test_scheduler_permissions(self) -> None:
        assert Permission.SCHEDULER_READ.value == "scheduler:read"
        assert Permission.SCHEDULER_WRITE.value == "scheduler:write"

    def test_vault_permissions(self) -> None:
        assert Permission.VAULT_READ.value == "vault:read"
        assert Permission.VAULT_WRITE.value == "vault:write"

    def test_proactive_permission(self) -> None:
        assert Permission.PROACTIVE.value == "proactive"

    def test_total_permission_count(self) -> None:
        """13 permissions defined."""
        assert len(Permission) == 13


# ── Risk Classification ─────────────────────────────────────────────


class TestRiskClassification:
    """Tests for risk levels and descriptions."""

    def test_all_permissions_have_risk(self) -> None:
        """Every permission has a risk level defined."""
        for perm in Permission:
            assert perm.value in PERMISSION_RISK

    def test_all_permissions_have_description(self) -> None:
        """Every permission has a description defined."""
        for perm in Permission:
            assert perm.value in PERMISSION_DESCRIPTIONS

    def test_risk_levels_valid(self) -> None:
        """Risk levels are one of low/medium/high."""
        for risk in PERMISSION_RISK.values():
            assert risk in ("low", "medium", "high")

    def test_network_internet_is_high(self) -> None:
        """Network internet is the highest risk."""
        assert get_risk(Permission.NETWORK_INTERNET) == "high"

    def test_brain_read_is_low(self) -> None:
        """Brain read is low risk."""
        assert get_risk(Permission.BRAIN_READ) == "low"

    def test_brain_write_is_medium(self) -> None:
        """Brain write is medium risk."""
        assert get_risk(Permission.BRAIN_WRITE) == "medium"

    def test_get_risk_emoji_low(self) -> None:
        assert get_risk_emoji(Permission.BRAIN_READ) == "🟢"

    def test_get_risk_emoji_medium(self) -> None:
        assert get_risk_emoji(Permission.BRAIN_WRITE) == "🟡"

    def test_get_risk_emoji_high(self) -> None:
        assert get_risk_emoji(Permission.NETWORK_INTERNET) == "🔴"

    def test_get_description(self) -> None:
        desc = get_description(Permission.BRAIN_READ)
        assert "Search and read" in desc

    def test_descriptions_non_empty(self) -> None:
        """All descriptions are meaningful strings."""
        for perm in Permission:
            desc = get_description(perm)
            assert len(desc) > 10


# ── Errors ──────────────────────────────────────────────────────────


class TestErrors:
    """Tests for permission error types."""

    def test_permission_denied_error(self) -> None:
        err = PermissionDeniedError("weather", "brain:write")
        assert err.plugin == "weather"
        assert err.permission == "brain:write"
        assert "weather" in str(err)
        assert "brain:write" in str(err)
        assert "plugin.yaml" in str(err)

    def test_auto_disabled_error(self) -> None:
        err = PluginAutoDisabledError("malware", 10)
        assert err.plugin == "malware"
        assert err.denial_count == 10
        assert "malware" in str(err)
        assert "10" in str(err)
        assert "malicious" in str(err)


# ── PermissionEnforcer ──────────────────────────────────────────────


class TestPermissionEnforcer:
    """Tests for PermissionEnforcer runtime enforcement."""

    def test_check_granted(self) -> None:
        """Granted permission passes silently."""
        enforcer = PermissionEnforcer("weather", {"brain:read", "network:internet"})
        enforcer.check("brain:read")  # Should not raise
        enforcer.check("network:internet")  # Should not raise

    def test_check_denied(self) -> None:
        """Denied permission raises PermissionDeniedError."""
        enforcer = PermissionEnforcer("weather", {"brain:read"})
        with pytest.raises(PermissionDeniedError, match="brain:write") as exc_info:
            enforcer.check("brain:write")
        assert exc_info.value.plugin == "weather"
        assert exc_info.value.permission == "brain:write"

    def test_denial_count_increments(self) -> None:
        """Each denial increments the counter."""
        enforcer = PermissionEnforcer("test", set())
        assert enforcer.denied_count == 0

        with pytest.raises(PermissionDeniedError):
            enforcer.check("brain:read")
        assert enforcer.denied_count == 1

        with pytest.raises(PermissionDeniedError):
            enforcer.check("brain:write")
        assert enforcer.denied_count == 2

    def test_auto_disable_after_max_denials(self) -> None:
        """Plugin auto-disabled after max_denials consecutive denials."""
        enforcer = PermissionEnforcer("sus", set(), max_denials=3)

        with pytest.raises(PermissionDeniedError):
            enforcer.check("brain:read")
        with pytest.raises(PermissionDeniedError):
            enforcer.check("brain:read")

        # Third denial triggers auto-disable
        with pytest.raises(PluginAutoDisabledError) as exc_info:
            enforcer.check("brain:read")
        assert exc_info.value.denial_count == 3
        assert enforcer.is_disabled is True

    def test_disabled_plugin_always_raises(self) -> None:
        """Once disabled, even granted permissions fail."""
        enforcer = PermissionEnforcer("bad", {"brain:read"}, max_denials=1)

        # Trigger disable via denied permission
        with pytest.raises(PluginAutoDisabledError):
            enforcer.check("network:internet")

        # Now even granted permission fails
        with pytest.raises(PluginAutoDisabledError):
            enforcer.check("brain:read")

    def test_properties(self) -> None:
        """Properties return correct values."""
        enforcer = PermissionEnforcer("test", {"brain:read", "fs:write"})
        assert enforcer.plugin_name == "test"
        assert enforcer.granted_permissions == frozenset({"brain:read", "fs:write"})
        assert enforcer.denied_count == 0
        assert enforcer.is_disabled is False

    def test_has_granted(self) -> None:
        """has() returns True for granted permissions."""
        enforcer = PermissionEnforcer("test", {"brain:read"})
        assert enforcer.has("brain:read") is True

    def test_has_not_granted(self) -> None:
        """has() returns False for non-granted permissions."""
        enforcer = PermissionEnforcer("test", {"brain:read"})
        assert enforcer.has("brain:write") is False

    def test_has_returns_false_when_disabled(self) -> None:
        """has() returns False when plugin is disabled."""
        enforcer = PermissionEnforcer("test", {"brain:read"}, max_denials=1)
        with pytest.raises(PluginAutoDisabledError):
            enforcer.check("network:internet")
        assert enforcer.has("brain:read") is False

    def test_empty_granted_set(self) -> None:
        """Empty granted set denies everything."""
        enforcer = PermissionEnforcer("bare", set())
        with pytest.raises(PermissionDeniedError):
            enforcer.check("brain:read")

    def test_default_max_denials_is_10(self) -> None:
        """Default max denials is 10."""
        enforcer = PermissionEnforcer("test", set())
        for _ in range(9):
            with pytest.raises(PermissionDeniedError):
                enforcer.check("x")
        assert enforcer.is_disabled is False
        with pytest.raises(PluginAutoDisabledError):
            enforcer.check("x")
        assert enforcer.is_disabled is True
        assert enforcer.denied_count == 10

    def test_granted_check_does_not_increment(self) -> None:
        """Successful checks don't increment denial count."""
        enforcer = PermissionEnforcer("test", {"brain:read"})
        enforcer.check("brain:read")
        enforcer.check("brain:read")
        enforcer.check("brain:read")
        assert enforcer.denied_count == 0
