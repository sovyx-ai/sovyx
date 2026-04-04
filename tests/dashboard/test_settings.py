"""Tests for sovyx.dashboard.settings — settings read/update."""

from __future__ import annotations

import logging
from pathlib import Path
from unittest.mock import MagicMock

import yaml

from sovyx.dashboard.settings import apply_settings, get_settings


def _mock_config() -> MagicMock:
    """Create a mock EngineConfig."""
    config = MagicMock()
    config.log.level = "INFO"
    config.log.format = "json"
    config.log.log_file = Path("/tmp/sovyx.log")  # noqa: S108
    config.data_dir = Path.home() / ".sovyx"
    config.telemetry.enabled = False
    config.api.enabled = True
    config.api.host = "127.0.0.1"
    config.api.port = 7777
    config.relay.enabled = False
    return config


class TestGetSettings:
    def test_returns_all_fields(self) -> None:
        config = _mock_config()
        result = get_settings(config)

        assert result["log_level"] == "INFO"
        assert result["log_format"] == "json"
        assert result["log_file"] == "/tmp/sovyx.log"
        assert result["telemetry_enabled"] is False
        assert result["api_enabled"] is True
        assert result["api_host"] == "127.0.0.1"
        assert result["api_port"] == 7777
        assert result["relay_enabled"] is False

    def test_none_log_file(self) -> None:
        config = _mock_config()
        config.log.log_file = None
        result = get_settings(config)
        assert result["log_file"] is None


class TestApplySettings:
    def test_update_log_level(self) -> None:
        config = _mock_config()
        changes = apply_settings(config, {"log_level": "DEBUG"})

        assert "log_level" in changes
        assert "INFO" in changes["log_level"]
        assert "DEBUG" in changes["log_level"]

    def test_invalid_log_level_ignored(self) -> None:
        config = _mock_config()
        changes = apply_settings(config, {"log_level": "TRACE"})
        assert changes == {}

    def test_same_level_no_change(self) -> None:
        config = _mock_config()
        changes = apply_settings(config, {"log_level": "INFO"})
        assert changes == {}

    def test_unknown_field_ignored(self) -> None:
        config = _mock_config()
        changes = apply_settings(config, {"unknown_field": "value"})
        assert changes == {}

    def test_persist_to_yaml(self, tmp_path: Path) -> None:
        config = _mock_config()
        yaml_path = tmp_path / "system.yaml"
        yaml_path.write_text("log:\n  level: INFO\n")

        apply_settings(config, {"log_level": "DEBUG"}, config_path=yaml_path)

        data = yaml.safe_load(yaml_path.read_text())
        assert data["log"]["level"] == "DEBUG"

    def test_persist_creates_log_section(self, tmp_path: Path) -> None:
        config = _mock_config()
        yaml_path = tmp_path / "system.yaml"
        yaml_path.write_text("{}")

        apply_settings(config, {"log_level": "WARNING"}, config_path=yaml_path)

        data = yaml.safe_load(yaml_path.read_text())
        assert data["log"]["level"] == "WARNING"

    def test_no_persist_without_path(self) -> None:
        config = _mock_config()
        changes = apply_settings(config, {"log_level": "ERROR"})
        assert "log_level" in changes
        # No crash — just doesn't persist

    def test_log_level_case_insensitive(self) -> None:
        config = _mock_config()
        changes = apply_settings(config, {"log_level": "debug"})
        assert "log_level" in changes

    def test_updates_stdlib_logger(self) -> None:
        config = _mock_config()
        root = logging.getLogger()
        old_level = root.level

        try:
            apply_settings(config, {"log_level": "ERROR"})
            assert root.level == logging.ERROR
        finally:
            root.setLevel(old_level)
