"""Tests for sovyx.engine.config — configuration system."""

from __future__ import annotations

from pathlib import Path

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from sovyx.engine.config import (
    APIConfig,
    DatabaseConfig,
    EngineConfig,
    HardwareConfig,
    LLMDefaultsConfig,
    LLMProviderConfig,
    LoggingConfig,
    RelayConfig,
    SocketConfig,
    TelemetryConfig,
    _deep_merge,
    _migrate_legacy_log_format,
    load_engine_config,
)
from sovyx.engine.errors import ConfigNotFoundError, ConfigValidationError


class TestDefaults:
    """Default configuration works without any files or env vars."""

    def test_engine_config_defaults(self) -> None:
        config = EngineConfig()
        assert config.data_dir == Path.home() / ".sovyx"
        assert config.log.level == "INFO"
        assert config.log.console_format == "text"
        assert config.database.wal_mode is True
        assert config.database.read_pool_size == 3
        assert config.hardware.tier == "auto"
        assert config.telemetry.enabled is False
        assert config.relay.enabled is False
        assert config.api.enabled is True
        assert config.api.port == 7777

    def test_logging_config_defaults(self) -> None:
        config = LoggingConfig()
        assert config.level == "INFO"
        assert config.console_format == "text"

    def test_database_config_defaults(self) -> None:
        config = DatabaseConfig()
        assert config.wal_mode is True
        assert config.mmap_size == 256 * 1024 * 1024
        assert config.cache_size == -64000

    def test_api_config_defaults(self) -> None:
        config = APIConfig()
        assert config.host == "127.0.0.1"
        assert config.port == 7777
        assert "http://localhost:7777" in config.cors_origins

    def test_hardware_config_defaults(self) -> None:
        config = HardwareConfig()
        assert config.tier == "auto"
        assert config.mmap_size_mb == 128

    def test_llm_defaults_config(self) -> None:
        config = LLMDefaultsConfig()
        assert config.routing_strategy == "auto"
        assert config.providers == []
        assert "unavailable" in config.degradation_message

    def test_telemetry_config_defaults(self) -> None:
        assert TelemetryConfig().enabled is False

    def test_relay_config_defaults(self) -> None:
        assert RelayConfig().enabled is False


class TestLLMProviderConfig:
    """LLM provider configuration."""

    def test_minimal(self) -> None:
        config = LLMProviderConfig(name="anthropic", model="claude-sonnet-4-20250514")
        assert config.name == "anthropic"
        assert config.api_key_env == ""
        assert config.endpoint is None
        assert config.timeout_seconds == 30

    def test_full(self) -> None:
        config = LLMProviderConfig(
            name="ollama",
            model="llama3.2:1b",
            endpoint="http://localhost:11434",
            timeout_seconds=60,
        )
        assert config.endpoint == "http://localhost:11434"
        assert config.timeout_seconds == 60


class TestSocketConfig:
    """Socket path auto-resolution."""

    def test_explicit_path(self) -> None:
        config = SocketConfig(path="/tmp/test.sock")
        assert config.path == "/tmp/test.sock"

    def test_auto_resolve_fallback(self) -> None:
        """When /run/sovyx doesn't exist, falls back to ~/.sovyx/."""
        config = SocketConfig()
        assert config.path != ""
        assert "sovyx.sock" in config.path


class TestLoadEngineConfig:
    """Loading configuration from YAML + env + overrides."""

    def test_load_defaults_no_args(self) -> None:
        config = load_engine_config()
        assert isinstance(config, EngineConfig)
        assert config.log.level == "INFO"

    def test_load_from_yaml(self, tmp_path: Path) -> None:
        yaml_file = tmp_path / "system.yaml"
        yaml_file.write_text("log:\n  level: DEBUG\n  format: text\n")
        config = load_engine_config(config_path=yaml_file)
        assert config.log.level == "DEBUG"
        assert config.log.console_format == "text"

    def test_yaml_overrides_defaults(self, tmp_path: Path) -> None:
        yaml_file = tmp_path / "system.yaml"
        yaml_file.write_text("api:\n  port: 9999\n")
        config = load_engine_config(config_path=yaml_file)
        assert config.api.port == 9999
        # Other defaults preserved
        assert config.api.host == "127.0.0.1"

    def test_overrides_override_yaml(self, tmp_path: Path) -> None:
        yaml_file = tmp_path / "system.yaml"
        yaml_file.write_text("log:\n  level: DEBUG\n")
        config = load_engine_config(
            config_path=yaml_file,
            overrides={"log": {"level": "ERROR"}},
        )
        assert config.log.level == "ERROR"

    def test_overrides_without_yaml(self) -> None:
        config = load_engine_config(overrides={"api": {"port": 8080}})
        assert config.api.port == 8080

    def test_env_vars(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("SOVYX_LOG__LEVEL", "WARNING")
        config = load_engine_config()
        assert config.log.level == "WARNING"

    def test_config_not_found(self) -> None:
        with pytest.raises(ConfigNotFoundError, match="not found"):
            load_engine_config(config_path=Path("/nonexistent/system.yaml"))

    def test_invalid_yaml(self, tmp_path: Path) -> None:
        yaml_file = tmp_path / "system.yaml"
        yaml_file.write_text(":\n  bad: [yaml\n")
        with pytest.raises(ConfigValidationError, match="Invalid YAML"):
            load_engine_config(config_path=yaml_file)

    def test_yaml_not_mapping(self, tmp_path: Path) -> None:
        yaml_file = tmp_path / "system.yaml"
        yaml_file.write_text("- item1\n- item2\n")
        with pytest.raises(ConfigValidationError, match="YAML mapping"):
            load_engine_config(config_path=yaml_file)

    def test_invalid_field_value(self, tmp_path: Path) -> None:
        yaml_file = tmp_path / "system.yaml"
        yaml_file.write_text("log:\n  level: INVALID_LEVEL\n")
        with pytest.raises(ConfigValidationError, match="validation failed"):
            load_engine_config(config_path=yaml_file)

    def test_empty_yaml(self, tmp_path: Path) -> None:
        yaml_file = tmp_path / "system.yaml"
        yaml_file.write_text("")
        config = load_engine_config(config_path=yaml_file)
        assert isinstance(config, EngineConfig)

    def test_nested_overrides(self, tmp_path: Path) -> None:
        yaml_file = tmp_path / "system.yaml"
        yaml_file.write_text("database:\n  wal_mode: true\n  read_pool_size: 5\n")
        config = load_engine_config(
            config_path=yaml_file,
            overrides={"database": {"read_pool_size": 10}},
        )
        assert config.database.read_pool_size == 10
        assert config.database.wal_mode is True  # preserved from yaml


class TestDeepMerge:
    """Deep merge utility."""

    def test_simple_merge(self) -> None:
        result = _deep_merge({"a": 1}, {"b": 2})
        assert result == {"a": 1, "b": 2}

    def test_override_value(self) -> None:
        result = _deep_merge({"a": 1}, {"a": 2})
        assert result == {"a": 2}

    def test_nested_merge(self) -> None:
        base = {"log": {"level": "INFO", "console_format": "json"}}
        override = {"log": {"level": "DEBUG"}}
        result = _deep_merge(base, override)
        assert result == {"log": {"level": "DEBUG", "console_format": "json"}}

    def test_original_not_mutated(self) -> None:
        base = {"a": {"b": 1}}
        override = {"a": {"c": 2}}
        _deep_merge(base, override)
        assert "c" not in base["a"]

    def test_empty_base(self) -> None:
        result = _deep_merge({}, {"a": 1})
        assert result == {"a": 1}

    def test_empty_override(self) -> None:
        result = _deep_merge({"a": 1}, {})
        assert result == {"a": 1}


class TestPropertyBased:
    """Property-based tests for configuration robustness."""

    @given(
        level=st.sampled_from(["DEBUG", "INFO", "WARNING", "ERROR"]),
        fmt=st.sampled_from(["json", "text"]),
    )
    @settings(max_examples=20)
    def test_any_valid_logging_config(self, level: str, fmt: str) -> None:
        config = LoggingConfig(level=level, console_format=fmt)  # type: ignore[arg-type]
        assert config.level == level
        assert config.console_format == fmt

    @given(
        tier=st.sampled_from(["auto", "pi", "n100", "gpu"]),
        mmap=st.integers(min_value=1, max_value=4096),
    )
    @settings(max_examples=20)
    def test_any_valid_hardware_config(self, tier: str, mmap: int) -> None:
        config = HardwareConfig(tier=tier, mmap_size_mb=mmap)  # type: ignore[arg-type]
        assert config.tier == tier
        assert config.mmap_size_mb == mmap

    @given(port=st.integers(min_value=1, max_value=65535))
    @settings(max_examples=20)
    def test_any_valid_port(self, port: int) -> None:
        config = APIConfig(port=port)
        assert config.port == port


class TestLegacyLogFormatMigration:
    """Backward compatibility: log.format → log.console_format."""

    def test_format_migrated_to_console_format(self) -> None:
        """Legacy 'format' key is renamed to 'console_format'."""
        data: dict[str, object] = {"log": {"level": "INFO", "format": "text"}}
        _migrate_legacy_log_format(data)
        log_section = data["log"]
        assert isinstance(log_section, dict)
        assert "format" not in log_section
        assert log_section["console_format"] == "text"

    def test_console_format_takes_precedence(self) -> None:
        """If both exist, console_format wins; format is dropped."""
        data: dict[str, object] = {"log": {"format": "json", "console_format": "text"}}
        _migrate_legacy_log_format(data)
        log_section = data["log"]
        assert isinstance(log_section, dict)
        assert "format" not in log_section
        assert log_section["console_format"] == "text"

    def test_no_log_section_is_noop(self) -> None:
        """Missing 'log' section → nothing happens."""
        data: dict[str, object] = {"api": {"port": 9999}}
        _migrate_legacy_log_format(data)
        assert "log" not in data

    def test_no_format_key_is_noop(self) -> None:
        """Log section without 'format' → nothing happens."""
        data: dict[str, object] = {"log": {"level": "DEBUG"}}
        _migrate_legacy_log_format(data)
        log_section = data["log"]
        assert isinstance(log_section, dict)
        assert "console_format" not in log_section

    def test_deprecation_warning_emitted(self) -> None:
        """Migration emits a DeprecationWarning."""
        import warnings

        data: dict[str, object] = {"log": {"format": "json"}}
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            _migrate_legacy_log_format(data)
        deprecations = [w for w in caught if issubclass(w.category, DeprecationWarning)]
        assert len(deprecations) == 1
        assert "console_format" in str(deprecations[0].message)

    def test_yaml_with_legacy_format_loads_correctly(self, tmp_path: Path) -> None:
        """Full integration: YAML with 'format' loads as 'console_format'."""
        yaml_file = tmp_path / "system.yaml"
        yaml_file.write_text("log:\n  level: DEBUG\n  format: text\n")
        config = load_engine_config(config_path=yaml_file)
        assert config.log.level == "DEBUG"
        assert config.log.console_format == "text"

    def test_yaml_with_console_format_works_natively(self, tmp_path: Path) -> None:
        """New-style YAML with 'console_format' works directly."""
        yaml_file = tmp_path / "system.yaml"
        yaml_file.write_text("log:\n  level: INFO\n  console_format: json\n")
        config = load_engine_config(config_path=yaml_file)
        assert config.log.console_format == "json"

    def test_non_dict_log_section_is_noop(self) -> None:
        """If log section is not a dict, skip migration."""
        data: dict[str, object] = {"log": "invalid"}
        _migrate_legacy_log_format(data)
        assert data["log"] == "invalid"
