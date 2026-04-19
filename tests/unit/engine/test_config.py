"""Tests for sovyx.engine.config — configuration system."""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from sovyx.engine.config import (
    APIConfig,
    BrainTuningConfig,
    DatabaseConfig,
    EngineConfig,
    HardwareConfig,
    LLMDefaultsConfig,
    LLMProviderConfig,
    LLMTuningConfig,
    LoggingConfig,
    RelayConfig,
    SafetyTuningConfig,
    SocketConfig,
    TelemetryConfig,
    VoiceTuningConfig,
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


class TestLogFileResolution:
    """log_file is resolved relative to data_dir by EngineConfig."""

    def test_default_resolves_to_data_dir(self) -> None:
        """log_file=None in LoggingConfig → resolved by EngineConfig."""
        config = EngineConfig()
        expected = Path.home() / ".sovyx" / "logs" / "sovyx.log"
        assert config.log.log_file == expected

    def test_custom_data_dir_propagates(self, tmp_path: Path) -> None:
        """Custom data_dir → log_file under that directory."""
        config = EngineConfig(data_dir=tmp_path / "custom")
        assert config.log.log_file == tmp_path / "custom" / "logs" / "sovyx.log"

    def test_explicit_log_file_preserved(self, tmp_path: Path) -> None:
        """Explicit log_file in LoggingConfig is not overwritten."""
        explicit = tmp_path / "my" / "custom.log"
        config = EngineConfig(
            log=LoggingConfig(log_file=explicit),
        )
        assert config.log.log_file == explicit

    def test_logging_config_standalone_default_none(self) -> None:
        """LoggingConfig alone defaults to None (no auto-resolve)."""
        config = LoggingConfig()
        assert config.log_file is None

    def test_env_var_data_dir(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """SOVYX_DATA_DIR env var propagates to log_file."""
        monkeypatch.setenv("SOVYX_DATA_DIR", str(tmp_path / "env"))
        config = EngineConfig()
        assert config.log.log_file == tmp_path / "env" / "logs" / "sovyx.log"

    def test_yaml_with_custom_data_dir(self, tmp_path: Path) -> None:
        """YAML data_dir → log_file resolved under it."""
        yaml_file = tmp_path / "system.yaml"
        yaml_file.write_text(f"data_dir: {tmp_path / 'yaml-data'}\n")
        config = load_engine_config(config_path=yaml_file)
        assert config.log.log_file == tmp_path / "yaml-data" / "logs" / "sovyx.log"

    def test_yaml_explicit_log_file_not_overridden(self, tmp_path: Path) -> None:
        """YAML with explicit log_file keeps it."""
        explicit = tmp_path / "explicit.log"
        yaml_file = tmp_path / "system.yaml"
        yaml_file.write_text(f"log:\n  log_file: {explicit}\n")
        config = load_engine_config(config_path=yaml_file)
        assert config.log.log_file == explicit


class TestTuningEnvOverrides:
    """Direct instantiation of tuning configs must honour env overrides.

    Subsystem modules (``voice.stt``, ``brain.learning``, ``llm.router``,
    ``cognitive.audit_store`` etc.) capture tuning values at import time
    with the pattern ``_CONST = _TuningCls().field``. That pattern is
    the one documented in the CLAUDE.md anti-pattern #17 note and only
    works if each ``*TuningConfig`` reads env vars on direct instantiation.

    These tests guard against the regression where the tuning configs
    were ``BaseModel`` (not ``BaseSettings``) and silently ignored
    ``SOVYX_TUNING__*`` overrides — the default value always won,
    regardless of what the operator set in the environment.
    """

    def test_voice_tuning_env_override(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("SOVYX_TUNING__VOICE__TRANSCRIBE_TIMEOUT_SECONDS", "99.0")
        assert VoiceTuningConfig().transcribe_timeout_seconds == 99.0

    def test_brain_tuning_env_override(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("SOVYX_TUNING__BRAIN__STAR_TOPOLOGY_K", "42")
        assert BrainTuningConfig().star_topology_k == 42

    def test_llm_tuning_env_override(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("SOVYX_TUNING__LLM__SIMPLE_MAX_LENGTH", "123")
        assert LLMTuningConfig().simple_max_length == 123

    def test_safety_tuning_env_override(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("SOVYX_TUNING__SAFETY__AUDIT_BUFFER_MAX", "77")
        assert SafetyTuningConfig().audit_buffer_max == 77

    def test_tuning_defaults_unchanged_without_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """No env set → defaults preserved (behaviour before the fix)."""
        # Ensure no SOVYX_TUNING__* leaks from the caller's environment.
        for key in list(os.environ):
            if key.startswith("SOVYX_TUNING__"):
                monkeypatch.delenv(key, raising=False)
        assert VoiceTuningConfig().transcribe_timeout_seconds == 10.0
        assert BrainTuningConfig().star_topology_k == 15
        assert LLMTuningConfig().simple_max_length == 500
        assert SafetyTuningConfig().audit_buffer_max == 100

    def test_engine_config_tuning_env_override(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """``EngineConfig`` path still honours nested env overrides."""
        monkeypatch.setenv("SOVYX_TUNING__VOICE__TRANSCRIBE_TIMEOUT_SECONDS", "42.0")
        assert EngineConfig().tuning.voice.transcribe_timeout_seconds == 42.0

    def test_vchl_probe_defaults_match_adr(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """ADR §4.3 diagnosis-table thresholds are the defaults the probe reads."""
        for key in list(os.environ):
            if key.startswith("SOVYX_TUNING__VOICE__"):
                monkeypatch.delenv(key, raising=False)
        cfg = VoiceTuningConfig()
        assert cfg.probe_cold_duration_ms == 1_500
        assert cfg.probe_warm_duration_ms == 3_000
        assert cfg.probe_warmup_discard_ms == 200
        assert cfg.probe_hard_timeout_s == 5.0
        assert cfg.probe_rms_db_no_signal == -70.0
        assert cfg.probe_rms_db_low_signal == -55.0
        assert cfg.probe_vad_apo_degraded_ceiling == 0.05
        assert cfg.probe_vad_healthy_floor == 0.5

    def test_vchl_cascade_defaults_match_adr(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """ADR §5.5 + §5.6 budget + lock-dict capacity defaults."""
        for key in list(os.environ):
            if key.startswith("SOVYX_TUNING__VOICE__"):
                monkeypatch.delenv(key, raising=False)
        cfg = VoiceTuningConfig()
        assert cfg.cascade_total_budget_s == 30.0
        assert cfg.cascade_attempt_budget_s == 5.0
        assert cfg.cascade_wizard_total_budget_s == 45.0
        assert cfg.cascade_lifecycle_lock_max == 64

    def test_vchl_probe_env_overrides(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Every VCHL probe field accepts a ``SOVYX_TUNING__VOICE__PROBE_*`` override."""
        monkeypatch.setenv("SOVYX_TUNING__VOICE__PROBE_COLD_DURATION_MS", "2500")
        monkeypatch.setenv("SOVYX_TUNING__VOICE__PROBE_WARM_DURATION_MS", "4500")
        monkeypatch.setenv("SOVYX_TUNING__VOICE__PROBE_WARMUP_DISCARD_MS", "333")
        monkeypatch.setenv("SOVYX_TUNING__VOICE__PROBE_HARD_TIMEOUT_S", "7.5")
        monkeypatch.setenv("SOVYX_TUNING__VOICE__PROBE_RMS_DB_NO_SIGNAL", "-65.5")
        monkeypatch.setenv("SOVYX_TUNING__VOICE__PROBE_RMS_DB_LOW_SIGNAL", "-50.0")
        monkeypatch.setenv("SOVYX_TUNING__VOICE__PROBE_VAD_APO_DEGRADED_CEILING", "0.08")
        monkeypatch.setenv("SOVYX_TUNING__VOICE__PROBE_VAD_HEALTHY_FLOOR", "0.6")

        cfg = VoiceTuningConfig()
        assert cfg.probe_cold_duration_ms == 2_500
        assert cfg.probe_warm_duration_ms == 4_500
        assert cfg.probe_warmup_discard_ms == 333
        assert cfg.probe_hard_timeout_s == 7.5
        assert cfg.probe_rms_db_no_signal == -65.5
        assert cfg.probe_rms_db_low_signal == -50.0
        assert cfg.probe_vad_apo_degraded_ceiling == 0.08
        assert cfg.probe_vad_healthy_floor == 0.6

    def test_vchl_cascade_env_overrides(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Every VCHL cascade field accepts a ``SOVYX_TUNING__VOICE__CASCADE_*`` override."""
        monkeypatch.setenv("SOVYX_TUNING__VOICE__CASCADE_TOTAL_BUDGET_S", "60.0")
        monkeypatch.setenv("SOVYX_TUNING__VOICE__CASCADE_ATTEMPT_BUDGET_S", "3.0")
        monkeypatch.setenv("SOVYX_TUNING__VOICE__CASCADE_WIZARD_TOTAL_BUDGET_S", "90.0")
        monkeypatch.setenv("SOVYX_TUNING__VOICE__CASCADE_LIFECYCLE_LOCK_MAX", "128")

        cfg = VoiceTuningConfig()
        assert cfg.cascade_total_budget_s == 60.0
        assert cfg.cascade_attempt_budget_s == 3.0
        assert cfg.cascade_wizard_total_budget_s == 90.0
        assert cfg.cascade_lifecycle_lock_max == 128
