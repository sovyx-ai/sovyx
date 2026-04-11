"""Sovyx Plugin Permissions — Capability-based permission model.

Plugins declare required permissions in plugin.yaml. Users approve on
install. PermissionEnforcer checks every access at runtime.

Design: Deno-style capability model (--allow-net, --allow-read).
Principle: "Plugins are GUESTS. They can only access what the HOST allows."

Spec: SPE-008 §4 (Permission model), SPE-008-SANDBOX §3-4
"""

from __future__ import annotations

import enum

from sovyx.observability.logging import get_logger

logger = get_logger(__name__)


# ── Permission Enum ─────────────────────────────────────────────────


class Permission(enum.Enum):
    """Capabilities a plugin can request.

    Each permission maps to a specific PluginContext access object.
    Undeclared permissions → access object is None → PermissionDeniedError.

    Risk levels: LOW (green), MEDIUM (yellow), HIGH (red).
    """

    # Brain access
    BRAIN_READ = "brain:read"
    BRAIN_WRITE = "brain:write"

    # Event bus
    EVENT_SUBSCRIBE = "event:subscribe"
    EVENT_EMIT = "event:emit"

    # Network
    NETWORK_LOCAL = "network:local"
    NETWORK_INTERNET = "network:internet"

    # Filesystem (plugin's data_dir only)
    FS_READ = "fs:read"
    FS_WRITE = "fs:write"

    # Scheduler (timers, reminders)
    SCHEDULER_READ = "scheduler:read"
    SCHEDULER_WRITE = "scheduler:write"

    # Credential vault
    VAULT_READ = "vault:read"
    VAULT_WRITE = "vault:write"

    # Proactive messaging
    PROACTIVE = "proactive"


# ── Risk Classification ─────────────────────────────────────────────

PERMISSION_RISK: dict[str, str] = {
    Permission.BRAIN_READ.value: "low",
    Permission.BRAIN_WRITE.value: "medium",
    Permission.EVENT_SUBSCRIBE.value: "low",
    Permission.EVENT_EMIT.value: "low",
    Permission.NETWORK_LOCAL.value: "medium",
    Permission.NETWORK_INTERNET.value: "high",
    Permission.FS_READ.value: "low",
    Permission.FS_WRITE.value: "medium",
    Permission.SCHEDULER_READ.value: "low",
    Permission.SCHEDULER_WRITE.value: "medium",
    Permission.VAULT_READ.value: "medium",
    Permission.VAULT_WRITE.value: "medium",
    Permission.PROACTIVE.value: "medium",
}

PERMISSION_DESCRIPTIONS: dict[str, str] = {
    Permission.BRAIN_READ.value: "Search and read concepts/episodes in the Mind's memory",
    Permission.BRAIN_WRITE.value: "Create new concepts in the Mind's memory",
    Permission.EVENT_SUBSCRIBE.value: "Listen to engine events (e.g., TimerFired, MindStarted)",
    Permission.EVENT_EMIT.value: "Send custom events (other plugins can listen)",
    Permission.NETWORK_LOCAL.value: (
        "Connect to local network services (Home Assistant, LAN devices)"
    ),
    Permission.NETWORK_INTERNET.value: "Connect to internet domains listed in the manifest",
    Permission.FS_READ.value: "Read files from the plugin's data directory",
    Permission.FS_WRITE.value: "Write files to the plugin's data directory",
    Permission.SCHEDULER_READ.value: "List existing timers and reminders",
    Permission.SCHEDULER_WRITE.value: "Create, modify, or cancel timers and reminders",
    Permission.VAULT_READ.value: "Read API keys and credentials stored for this plugin",
    Permission.VAULT_WRITE.value: "Store new API keys and credentials",
    Permission.PROACTIVE.value: "Send messages to the user without being asked",
}


def get_risk(permission: Permission) -> str:
    """Get risk level for a permission.

    Args:
        permission: Permission enum member.

    Returns:
        Risk level: "low", "medium", or "high".
    """
    return PERMISSION_RISK.get(permission.value, "medium")


def get_risk_emoji(permission: Permission) -> str:
    """Get risk indicator emoji for CLI display.

    Args:
        permission: Permission enum member.

    Returns:
        Colored circle emoji: 🟢 (low), 🟡 (medium), 🔴 (high).
    """
    risk = get_risk(permission)
    return {"low": "🟢", "medium": "🟡", "high": "🔴"}.get(risk, "🟡")


def get_description(permission: Permission) -> str:
    """Get human-readable description for a permission.

    Args:
        permission: Permission enum member.

    Returns:
        Description string explaining what the permission allows.
    """
    return PERMISSION_DESCRIPTIONS.get(permission.value, permission.value)


# ── Errors ──────────────────────────────────────────────────────────


class PermissionDeniedError(Exception):
    """Raised when a plugin tries to access a resource without permission."""

    def __init__(self, plugin: str, permission: str) -> None:
        self.plugin = plugin
        self.permission = permission
        super().__init__(
            f"Plugin '{plugin}' does not have permission '{permission}'. "
            f"Declare it in plugin.yaml and reinstall."
        )


class PluginAutoDisabledError(Exception):
    """Raised when a plugin is auto-disabled after too many permission denials."""

    def __init__(self, plugin: str, denial_count: int) -> None:
        self.plugin = plugin
        self.denial_count = denial_count
        super().__init__(
            f"Plugin '{plugin}' disabled after {denial_count} permission "
            f"denials. This may indicate malicious behavior."
        )


# ── Permission Enforcer ─────────────────────────────────────────────


class PermissionEnforcer:
    """Runtime permission enforcement for plugins.

    Wraps every PluginContext access. Every method call is checked
    against the plugin's granted permissions. This is the LAST LINE
    OF DEFENSE — even if a plugin somehow gets a reference to
    BrainService directly, the enforcer blocks unauthorized calls.

    Auto-disables plugin after ``max_denials`` consecutive permission
    violations (default 10). This prevents brute-force probing.

    Spec: SPE-008-SANDBOX §4.1
    """

    def __init__(
        self,
        plugin_name: str,
        granted: set[str],
        *,
        max_denials: int = 10,
    ) -> None:
        """Initialize enforcer.

        Args:
            plugin_name: Plugin identifier for logging.
            granted: Set of permission value strings (e.g., {"brain:read"}).
            max_denials: Max denials before auto-disable. Default 10.
        """
        self._plugin = plugin_name
        self._granted = frozenset(granted)
        self._denied_count = 0
        self._max_denials = max_denials
        self._disabled = False

    @property
    def plugin_name(self) -> str:
        """Plugin this enforcer protects."""
        return self._plugin

    @property
    def granted_permissions(self) -> frozenset[str]:
        """Set of granted permission strings."""
        return self._granted

    @property
    def denied_count(self) -> int:
        """Number of permission denials so far."""
        return self._denied_count

    @property
    def is_disabled(self) -> bool:
        """Whether plugin has been auto-disabled."""
        return self._disabled

    def check(self, permission: str) -> None:
        """Check if a permission is granted.

        Args:
            permission: Permission value string (e.g., "brain:read").

        Raises:
            PluginAutoDisabledError: Plugin was previously auto-disabled.
            PermissionDeniedError: Permission not granted.
        """
        if self._disabled:
            raise PluginAutoDisabledError(self._plugin, self._denied_count)

        if permission in self._granted:
            return

        self._denied_count += 1
        logger.warning(
            "permission_denied",
            plugin=self._plugin,
            permission=permission,
            denial_count=self._denied_count,
        )

        if self._denied_count >= self._max_denials:
            self._disabled = True
            logger.error(
                "plugin_auto_disabled",
                plugin=self._plugin,
                denial_count=self._denied_count,
            )
            raise PluginAutoDisabledError(self._plugin, self._denied_count)

        raise PermissionDeniedError(self._plugin, permission)

    def has(self, permission: str) -> bool:
        """Check if a permission is granted without raising.

        Useful for feature detection: "if brain available, use it."

        Args:
            permission: Permission value string.

        Returns:
            True if granted, False otherwise.
        """
        return permission in self._granted and not self._disabled
