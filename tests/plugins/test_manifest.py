"""Tests for Sovyx Plugin Manifest — plugin.yaml schema and validation.

Coverage target: ≥95% on plugins/manifest.py
"""

from __future__ import annotations

from pathlib import Path

import pytest

from sovyx.plugins.manifest import (
    ManifestError,
    PluginManifest,
    load_manifest,
)
from sovyx.plugins.permissions import Permission


# ── Valid Manifests ─────────────────────────────────────────────────


class TestPluginManifest:
    """Tests for PluginManifest Pydantic model."""

    def test_minimal_valid(self) -> None:
        """Minimal manifest with only required fields."""
        m = PluginManifest(
            name="weather",
            version="1.0.0",
            description="Get weather data.",
        )
        assert m.name == "weather"
        assert m.version == "1.0.0"
        assert m.permissions == []
        assert m.depends == []
        assert m.network.allowed_domains == []

    def test_full_manifest(self) -> None:
        """Full manifest with all fields."""
        m = PluginManifest(
            name="weather",
            version="1.2.3",
            description="Get current weather and forecasts.",
            author="Sovyx Community",
            license="MIT",
            homepage="https://github.com/sovyx/plugin-weather",
            min_sovyx_version="1.0.0",
            permissions=["network:internet", "fs:write"],
            network={"allowed_domains": ["api.open-meteo.com"]},
            depends=[{"name": "core", "version": ">=1.0.0"}],
            optional_depends=[{"name": "calendar", "version": ">=1.0.0"}],
            events={
                "emits": [
                    {"name": "weather_updated", "description": "Weather data refreshed"}
                ],
                "subscribes": ["plugin.timer.completed"],
            },
            tools=[
                {"name": "get_weather", "description": "Get weather"},
                {"name": "get_forecast", "description": "Get forecast"},
            ],
            config_schema={
                "type": "object",
                "properties": {"default_location": {"type": "string"}},
            },
        )
        assert m.author == "Sovyx Community"
        assert len(m.permissions) == 2
        assert m.network.allowed_domains == ["api.open-meteo.com"]
        assert len(m.depends) == 1
        assert m.depends[0].name == "core"
        assert len(m.events.emits) == 1
        assert len(m.tools) == 2

    def test_get_permission_enums(self) -> None:
        """get_permission_enums converts strings to Permission."""
        m = PluginManifest(
            name="test",
            version="1.0.0",
            description="Test.",
            permissions=["brain:read", "brain:write"],
        )
        enums = m.get_permission_enums()
        assert enums == [Permission.BRAIN_READ, Permission.BRAIN_WRITE]


# ── Name Validation ─────────────────────────────────────────────────


class TestNameValidation:
    """Tests for plugin name constraints."""

    def test_valid_names(self) -> None:
        """Various valid plugin names."""
        for name in ["weather", "my-plugin", "x123", "a"]:
            m = PluginManifest(name=name, version="1.0.0", description="Test.")
            assert m.name == name

    def test_name_too_long(self) -> None:
        with pytest.raises(Exception):
            PluginManifest(name="a" * 51, version="1.0.0", description="Test.")

    def test_name_empty(self) -> None:
        with pytest.raises(Exception):
            PluginManifest(name="", version="1.0.0", description="Test.")

    def test_name_uppercase_rejected(self) -> None:
        with pytest.raises(Exception):
            PluginManifest(name="MyPlugin", version="1.0.0", description="Test.")

    def test_name_spaces_rejected(self) -> None:
        with pytest.raises(Exception):
            PluginManifest(name="my plugin", version="1.0.0", description="Test.")

    def test_name_starts_with_number(self) -> None:
        with pytest.raises(Exception):
            PluginManifest(name="1plugin", version="1.0.0", description="Test.")

    def test_name_starts_with_hyphen(self) -> None:
        with pytest.raises(Exception):
            PluginManifest(name="-plugin", version="1.0.0", description="Test.")

    def test_name_ends_with_hyphen(self) -> None:
        with pytest.raises(Exception):
            PluginManifest(name="plugin-", version="1.0.0", description="Test.")

    def test_name_consecutive_hyphens(self) -> None:
        with pytest.raises(Exception):
            PluginManifest(name="my--plugin", version="1.0.0", description="Test.")


# ── Permission Validation ───────────────────────────────────────────


class TestPermissionValidation:
    """Tests for permission string validation."""

    def test_valid_permissions(self) -> None:
        m = PluginManifest(
            name="test",
            version="1.0.0",
            description="Test.",
            permissions=["brain:read", "network:internet", "proactive"],
        )
        assert len(m.permissions) == 3

    def test_unknown_permission_rejected(self) -> None:
        with pytest.raises(Exception, match="Unknown permission"):
            PluginManifest(
                name="test",
                version="1.0.0",
                description="Test.",
                permissions=["brain:read", "hack:everything"],
            )


# ── Version Validation ──────────────────────────────────────────────


class TestVersionValidation:
    """Tests for version format validation."""

    def test_valid_versions(self) -> None:
        for v in ["1.0.0", "0.1.0", "2.3", "1.0.0-beta.1"]:
            m = PluginManifest(name="test", version=v, description="Test.")
            assert m.version == v

    def test_single_number_rejected(self) -> None:
        with pytest.raises(Exception, match="semver"):
            PluginManifest(name="test", version="1", description="Test.")


# ── YAML Loading ────────────────────────────────────────────────────


class TestLoadManifest:
    """Tests for load_manifest YAML loader."""

    def test_load_valid_yaml(self, tmp_path: Path) -> None:
        """Load a valid plugin.yaml."""
        manifest_file = tmp_path / "plugin.yaml"
        manifest_file.write_text(
            'name: weather\nversion: "1.0.0"\ndescription: "Get weather."'
        )
        m = load_manifest(tmp_path)
        assert m.name == "weather"
        assert m.version == "1.0.0"

    def test_load_full_yaml(self, tmp_path: Path) -> None:
        """Load full plugin.yaml with all fields."""
        manifest_file = tmp_path / "plugin.yaml"
        manifest_file.write_text(
            """
name: weather
version: "1.2.0"
description: "Weather data plugin."
author: "Test Author"
license: MIT
permissions:
  - network:internet
  - fs:write
network:
  allowed_domains:
    - api.open-meteo.com
depends:
  - name: core
    version: ">=1.0.0"
tools:
  - name: get_weather
    description: "Get current weather"
config_schema:
  type: object
  properties:
    location:
      type: string
"""
        )
        m = load_manifest(tmp_path)
        assert m.author == "Test Author"
        assert "network:internet" in m.permissions
        assert m.network.allowed_domains == ["api.open-meteo.com"]
        assert len(m.depends) == 1

    def test_load_missing_file(self, tmp_path: Path) -> None:
        """Missing plugin.yaml raises ManifestError."""
        with pytest.raises(ManifestError, match="not found"):
            load_manifest(tmp_path)

    def test_load_invalid_yaml(self, tmp_path: Path) -> None:
        """Invalid YAML raises ManifestError."""
        manifest_file = tmp_path / "plugin.yaml"
        manifest_file.write_text("name: [invalid yaml\n  broken: {")
        with pytest.raises(ManifestError, match="Invalid YAML"):
            load_manifest(tmp_path)

    def test_load_non_dict_yaml(self, tmp_path: Path) -> None:
        """YAML that's not a mapping raises ManifestError."""
        manifest_file = tmp_path / "plugin.yaml"
        manifest_file.write_text("- just\n- a\n- list")
        with pytest.raises(ManifestError, match="YAML mapping"):
            load_manifest(tmp_path)

    def test_load_invalid_schema(self, tmp_path: Path) -> None:
        """YAML that doesn't match schema raises ManifestError."""
        manifest_file = tmp_path / "plugin.yaml"
        manifest_file.write_text('name: ""\nversion: "1"\ndescription: ""')
        with pytest.raises(ManifestError):
            load_manifest(tmp_path)

    def test_manifest_error_attributes(self) -> None:
        """ManifestError has path and reason attributes."""
        err = ManifestError("/path/plugin.yaml", "bad field")
        assert err.path == "/path/plugin.yaml"
        assert err.reason == "bad field"
        assert "/path/plugin.yaml" in str(err)

    def test_load_unreadable_file(self, tmp_path: Path) -> None:
        """Unreadable directory (plugin.yaml is a dir) raises ManifestError."""
        # Create a directory named plugin.yaml to trigger OSError
        manifest_dir = tmp_path / "plugin.yaml"
        manifest_dir.mkdir()
        with pytest.raises(ManifestError, match="Cannot read|Invalid YAML|mapping"):
            load_manifest(tmp_path)
