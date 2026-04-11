"""Sovyx Plugin Manifest — plugin.yaml schema and validation.

The manifest is the plugin's declaration of identity, permissions,
dependencies, and configuration schema. Loaded from plugin.yaml
in the plugin directory.

Spec: SPE-008 §5 (Plugin Manifest), SPE-008-PLUGIN-IPC §3
"""

from __future__ import annotations

import typing

import yaml
from pydantic import BaseModel, Field, field_validator

from sovyx.plugins.permissions import Permission

if typing.TYPE_CHECKING:  # pragma: no cover
    from pathlib import Path


# ── Sub-models ──────────────────────────────────────────────────────


class NetworkConfig(BaseModel):
    """Network access configuration for sandboxed HTTP client."""

    allowed_domains: list[str] = Field(default_factory=list)


class PluginDependency(BaseModel):
    """Dependency on another plugin."""

    name: str
    version: str = ">=0.0.0"


class EventDeclaration(BaseModel):
    """Event emitted by the plugin."""

    name: str
    description: str = ""
    schema_: dict[str, object] = Field(default_factory=dict, alias="schema")

    model_config = {"populate_by_name": True}


class EventsConfig(BaseModel):
    """Event declarations for the plugin."""

    emits: list[EventDeclaration] = Field(default_factory=list)
    subscribes: list[str] = Field(default_factory=list)


class ToolDeclaration(BaseModel):
    """Tool declared in manifest (informational, not authoritative)."""

    name: str
    description: str = ""


# ── Main Manifest Model ────────────────────────────────────────────


class PluginManifest(BaseModel):
    """Plugin manifest loaded from plugin.yaml.

    This is the CONTRACT between the plugin and the engine.
    Every field is validated at install/load time.

    Spec: SPE-008 §5
    """

    # Required
    name: str = Field(..., min_length=1, max_length=50, pattern=r"^[a-z][a-z0-9\-]*$")
    version: str = Field(..., min_length=1)
    description: str = Field(..., min_length=1, max_length=200)

    # Optional metadata
    author: str = ""
    license: str = Field(default="", alias="license")
    homepage: str = ""
    min_sovyx_version: str = "0.0.0"

    # Permissions
    permissions: list[str] = Field(default_factory=list)

    # Network
    network: NetworkConfig = Field(default_factory=NetworkConfig)

    # Dependencies (SPE-008-PLUGIN-IPC §3)
    depends: list[PluginDependency] = Field(default_factory=list)
    optional_depends: list[PluginDependency] = Field(default_factory=list)

    # Events (SPE-008-PLUGIN-IPC §1)
    events: EventsConfig = Field(default_factory=EventsConfig)

    # Tools (informational — authoritative list comes from get_tools())
    tools: list[ToolDeclaration] = Field(default_factory=list)

    # Config schema for plugin settings
    config_schema: dict[str, object] = Field(default_factory=dict)

    # Marketplace metadata (optional — for future marketplace)
    category: str = ""  # e.g., "productivity", "finance", "weather"
    tags: list[str] = Field(default_factory=list)
    icon_url: str = ""
    screenshots: list[str] = Field(default_factory=list)
    pricing: str = "free"  # "free", "paid", "freemium"
    price_usd: float | None = None
    trial_days: int = 0

    model_config = {"populate_by_name": True}

    @field_validator("permissions")
    @classmethod
    def validate_permissions(cls, v: list[str]) -> list[str]:
        """Validate that all permissions are known Permission values."""
        valid = {p.value for p in Permission}
        for perm in v:
            if perm not in valid:
                msg = f"Unknown permission '{perm}'. Valid: {sorted(valid)}"
                raise ValueError(msg)
        return v

    @field_validator("name")
    @classmethod
    def validate_name(cls, v: str) -> str:
        """Plugin name must be lowercase, start with letter, use hyphens."""
        if v.startswith("-") or v.endswith("-"):
            msg = "Plugin name cannot start or end with hyphen"
            raise ValueError(msg)
        if "--" in v:
            msg = "Plugin name cannot contain consecutive hyphens"
            raise ValueError(msg)
        return v

    @field_validator("version")
    @classmethod
    def validate_version(cls, v: str) -> str:
        """Basic semver format check (X.Y.Z)."""
        parts = v.split(".")
        if len(parts) < 2:  # noqa: PLR2004
            msg = f"Version must be semver format (X.Y.Z), got '{v}'"
            raise ValueError(msg)
        return v

    def get_permission_enums(self) -> list[Permission]:
        """Convert permission strings to Permission enum members."""
        return [Permission(p) for p in self.permissions]


# ── Loader ──────────────────────────────────────────────────────────


class ManifestError(Exception):
    """Raised when a plugin manifest is invalid."""

    def __init__(self, path: str, reason: str) -> None:
        self.path = path
        self.reason = reason
        super().__init__(f"Invalid manifest at {path}: {reason}")


def load_manifest(plugin_dir: Path) -> PluginManifest:
    """Load and validate a plugin manifest from plugin.yaml.

    Args:
        plugin_dir: Directory containing plugin.yaml.

    Returns:
        Validated PluginManifest.

    Raises:
        ManifestError: File not found, invalid YAML, or validation error.
    """
    manifest_path = plugin_dir / "plugin.yaml"
    if not manifest_path.exists():
        raise ManifestError(str(manifest_path), "plugin.yaml not found")

    try:
        raw = manifest_path.read_text(encoding="utf-8")
    except OSError as e:
        raise ManifestError(str(manifest_path), f"Cannot read file: {e}") from e

    try:
        data = yaml.safe_load(raw)
    except yaml.YAMLError as e:
        raise ManifestError(str(manifest_path), f"Invalid YAML: {e}") from e

    if not isinstance(data, dict):
        raise ManifestError(str(manifest_path), "Manifest must be a YAML mapping")

    try:
        return PluginManifest(**data)
    except Exception as e:
        raise ManifestError(str(manifest_path), str(e)) from e
