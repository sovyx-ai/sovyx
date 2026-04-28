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


class TestVoiceTuningV13EmpiricalDefaults:
    """v1.3 §14 — empirical tuning knobs + L4-A validator.

    Asserts the defaults + env-override surface + validator invariant
    for every knob declared in §14 of the IMPLEMENTATION_PLAN (E1/E2/
    E3/E4). Regression tests for the v0.21.2 probe-window bug live
    here because they check the invariant the validator enforces, not
    the coordinator's runtime behaviour.
    """

    def _clear_voice_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        for key in list(os.environ):
            if key.startswith("SOVYX_TUNING__VOICE__"):
                monkeypatch.delenv(key, raising=False)

    def test_v13_defaults_exact(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """v1.3 freezes these defaults — bumping any requires a plan update."""
        self._clear_voice_env(monkeypatch)
        cfg = VoiceTuningConfig()
        assert cfg.bypass_strategy_post_apply_settle_s == 3.2  # §14.E3
        assert cfg.probe_jitter_margin_s == 0.5  # §14.E1
        assert cfg.improvement_rolloff_factor == 5.0  # §14.E2
        assert cfg.mark_tap_poll_interval_s == 0.05  # §14.E4

    def test_validator_enforces_settle_ge_probe(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """L4-A — misconfig that reintroduces the probe-window bug is rejected."""
        self._clear_voice_env(monkeypatch)
        # The v0.21.2 combination that caused the incident: settle < probe.
        with pytest.raises(Exception) as exc_info:
            VoiceTuningConfig(
                integrity_probe_duration_s=3.0,
                bypass_strategy_post_apply_settle_s=1.5,
            )
        # pydantic wraps the ValueError in a ValidationError; exact class
        # differs across pydantic minor versions, so assert on the name
        # + payload instead of on isinstance.
        assert "bypass_strategy_post_apply_settle_s" in str(exc_info.value)

    def test_default_settle_exceeds_default_probe(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Invariant holds even when future changes bump one knob without the other."""
        self._clear_voice_env(monkeypatch)
        cfg = VoiceTuningConfig()
        assert cfg.bypass_strategy_post_apply_settle_s >= cfg.integrity_probe_duration_s

    def test_v13_env_overrides(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Every §14 knob respects its SOVYX_TUNING__VOICE__* env variable."""
        monkeypatch.setenv("SOVYX_TUNING__VOICE__PROBE_JITTER_MARGIN_S", "0.75")
        monkeypatch.setenv("SOVYX_TUNING__VOICE__IMPROVEMENT_ROLLOFF_FACTOR", "7.5")
        monkeypatch.setenv("SOVYX_TUNING__VOICE__MARK_TAP_POLL_INTERVAL_S", "0.1")
        monkeypatch.setenv(
            "SOVYX_TUNING__VOICE__BYPASS_STRATEGY_POST_APPLY_SETTLE_S",
            "4.0",
        )
        cfg = VoiceTuningConfig()
        assert cfg.probe_jitter_margin_s == 0.75
        assert cfg.improvement_rolloff_factor == 7.5
        assert cfg.mark_tap_poll_interval_s == 0.1
        assert cfg.bypass_strategy_post_apply_settle_s == 4.0

    def test_validator_respects_env_override(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Env override that breaks the invariant is still rejected at boot."""
        monkeypatch.setenv(
            "SOVYX_TUNING__VOICE__BYPASS_STRATEGY_POST_APPLY_SETTLE_S",
            "1.0",
        )
        with pytest.raises(Exception) as exc_info:
            VoiceTuningConfig()
        assert "bypass_strategy_post_apply_settle_s" in str(exc_info.value)

    def test_customization_apply_must_be_strictly_less_than_skip(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Paranoid-QA R2 MEDIUM #2 regression.

        ``apply == skip`` collapses the defer band to zero width.
        The orchestrator's branches use strict ``<`` and ``>``,
        so any score equal to the shared threshold falls through
        both branches and ends up in a defer path for a
        non-existent band. Reject at config-load time.
        """
        self._clear_voice_env(monkeypatch)
        with pytest.raises(Exception) as exc_info:
            VoiceTuningConfig(
                linux_mixer_user_customization_threshold_apply=0.6,
                linux_mixer_user_customization_threshold_skip=0.6,
            )
        assert "linux_mixer_user_customization_threshold_apply" in str(exc_info.value)

    def test_customization_apply_strictly_greater_than_skip_rejected(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Inverted band (apply > skip) is also rejected — same
        validator, preserved from Paranoid-QA HIGH #9."""
        self._clear_voice_env(monkeypatch)
        with pytest.raises(Exception) as exc_info:
            VoiceTuningConfig(
                linux_mixer_user_customization_threshold_apply=0.8,
                linux_mixer_user_customization_threshold_skip=0.75,
            )
        assert "linux_mixer_user_customization_threshold" in str(exc_info.value)

    def test_customization_strict_ordering_accepted(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Strict ``apply < skip`` (the contract) is accepted."""
        self._clear_voice_env(monkeypatch)
        cfg = VoiceTuningConfig(
            linux_mixer_user_customization_threshold_apply=0.4,
            linux_mixer_user_customization_threshold_skip=0.7,
        )
        assert cfg.linux_mixer_user_customization_threshold_apply == 0.4
        assert cfg.linux_mixer_user_customization_threshold_skip == 0.7


# ===========================================================================
# Mission #11: VoiceTuningConfig pydantic Field bounds hardening
# ===========================================================================
#
# Pre-hardening, fields like ``transcribe_timeout_seconds`` and
# ``pipeline_deaf_min_frames`` were bare int/float defaults — env-var
# overrides like ``SOVYX_TUNING__VOICE__TRANSCRIBE_TIMEOUT_SECONDS=0``
# (instant-fail every transcription) or
# ``SOVYX_TUNING__VOICE__PIPELINE_DEAF_MIN_FRAMES=99999999`` (deaf
# detector never fires) loaded silently and produced mysterious
# runtime behaviour. Pydantic ``Field(ge=, le=)`` bounds catch
# misconfiguration at config-load time so the failure is loud
# (ValidationError on instantiation) instead of mysterious.
#
# Reference: MISSION-voice-mixer-enterprise-refactor-2026-04-25
# Appendix A band-aid #11 (Deaf Threshold bounds).


class TestVoiceTuningPydanticBoundsB11:
    """Pydantic Field bounds reject out-of-range values at load time."""

    @staticmethod
    def _clear_voice_env(monkeypatch: pytest.MonkeyPatch) -> None:
        for key in list(os.environ):
            if key.startswith("SOVYX_TUNING__VOICE__"):
                monkeypatch.delenv(key, raising=False)

    # ── transcribe_timeout_seconds bounds ────────────────────────────

    def test_transcribe_timeout_below_floor_rejected(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from pydantic import ValidationError

        self._clear_voice_env(monkeypatch)
        with pytest.raises(ValidationError):
            VoiceTuningConfig(transcribe_timeout_seconds=0.0)

    def test_transcribe_timeout_above_ceiling_rejected(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from pydantic import ValidationError

        self._clear_voice_env(monkeypatch)
        with pytest.raises(ValidationError):
            VoiceTuningConfig(transcribe_timeout_seconds=999.0)

    def test_transcribe_timeout_at_floor_accepted(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._clear_voice_env(monkeypatch)
        cfg = VoiceTuningConfig(transcribe_timeout_seconds=0.5)
        assert cfg.transcribe_timeout_seconds == 0.5

    def test_transcribe_timeout_at_ceiling_accepted(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._clear_voice_env(monkeypatch)
        cfg = VoiceTuningConfig(transcribe_timeout_seconds=120.0)
        assert cfg.transcribe_timeout_seconds == 120.0

    # ── pipeline_deaf_min_frames bounds ──────────────────────────────

    def test_deaf_min_frames_below_floor_rejected(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from pydantic import ValidationError

        self._clear_voice_env(monkeypatch)
        with pytest.raises(ValidationError):
            VoiceTuningConfig(pipeline_deaf_min_frames=5)

    def test_deaf_min_frames_above_ceiling_rejected(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from pydantic import ValidationError

        self._clear_voice_env(monkeypatch)
        with pytest.raises(ValidationError):
            VoiceTuningConfig(pipeline_deaf_min_frames=99_999_999)

    # ── pipeline_deaf_vad_max_threshold bounds ───────────────────────

    def test_deaf_vad_threshold_above_ceiling_rejected(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from pydantic import ValidationError

        self._clear_voice_env(monkeypatch)
        with pytest.raises(ValidationError):
            VoiceTuningConfig(pipeline_deaf_vad_max_threshold=0.95)

    def test_deaf_vad_threshold_negative_rejected(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from pydantic import ValidationError

        self._clear_voice_env(monkeypatch)
        with pytest.raises(ValidationError):
            VoiceTuningConfig(pipeline_deaf_vad_max_threshold=-0.1)

    # ── deaf_warnings_before_exclusive_retry bounds ──────────────────

    def test_deaf_warnings_zero_rejected(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Zero would disable the autofix entirely (defeats the
        feature). Mission #11 requires a floor of 1."""
        from pydantic import ValidationError

        self._clear_voice_env(monkeypatch)
        with pytest.raises(ValidationError):
            VoiceTuningConfig(deaf_warnings_before_exclusive_retry=0)

    def test_deaf_warnings_above_ceiling_rejected(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from pydantic import ValidationError

        self._clear_voice_env(monkeypatch)
        with pytest.raises(ValidationError):
            VoiceTuningConfig(deaf_warnings_before_exclusive_retry=100)

    # ── pipeline_heartbeat_interval_seconds bounds ───────────────────

    def test_heartbeat_interval_below_floor_rejected(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from pydantic import ValidationError

        self._clear_voice_env(monkeypatch)
        with pytest.raises(ValidationError):
            VoiceTuningConfig(pipeline_heartbeat_interval_seconds=0.1)

    def test_heartbeat_interval_above_ceiling_rejected(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from pydantic import ValidationError

        self._clear_voice_env(monkeypatch)
        with pytest.raises(ValidationError):
            VoiceTuningConfig(pipeline_heartbeat_interval_seconds=120.0)

    # ── streaming_drain_seconds bounds ───────────────────────────────

    def test_streaming_drain_above_ceiling_rejected(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from pydantic import ValidationError

        self._clear_voice_env(monkeypatch)
        with pytest.raises(ValidationError):
            VoiceTuningConfig(streaming_drain_seconds=30.0)

    # ── cloud_stt_timeout_seconds bounds ─────────────────────────────

    def test_cloud_stt_timeout_below_floor_rejected(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from pydantic import ValidationError

        self._clear_voice_env(monkeypatch)
        with pytest.raises(ValidationError):
            VoiceTuningConfig(cloud_stt_timeout_seconds=0.0)

    def test_cloud_stt_timeout_above_ceiling_rejected(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from pydantic import ValidationError

        self._clear_voice_env(monkeypatch)
        with pytest.raises(ValidationError):
            VoiceTuningConfig(cloud_stt_timeout_seconds=600.0)

    # ── cloud_stt_max_audio_seconds bounds ───────────────────────────

    def test_cloud_stt_max_audio_above_ceiling_rejected(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from pydantic import ValidationError

        self._clear_voice_env(monkeypatch)
        with pytest.raises(ValidationError):
            VoiceTuningConfig(cloud_stt_max_audio_seconds=999.0)

    # ── Backwards-compat: shipped defaults still validate ────────────

    def test_default_config_passes_hardened_validation(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Regression — the shipped defaults must continue to load
        cleanly after the hardening."""
        self._clear_voice_env(monkeypatch)
        cfg = VoiceTuningConfig()
        # Sanity: read every field that was hardened — instantiation
        # alone proves all bounds passed.
        assert cfg.transcribe_timeout_seconds == 10.0
        assert cfg.streaming_drain_seconds == 0.5
        assert cfg.cloud_stt_timeout_seconds == 30.0
        assert cfg.cloud_stt_max_audio_seconds == 120.0
        assert cfg.pipeline_deaf_min_frames == 150  # noqa: PLR2004
        assert cfg.pipeline_deaf_vad_max_threshold == 0.05
        assert cfg.deaf_warnings_before_exclusive_retry == 2  # noqa: PLR2004
        assert cfg.pipeline_heartbeat_interval_seconds == 5.0


# ===========================================================================
# Mission Phase 1 / T1.28 — pipeline-tuning constants migrated from
# voice/pipeline/_orchestrator.py module-level
# ===========================================================================
#
# Pre-T1.28 the orchestrator carried 8 hardcoded constants (frame-drop
# detector, VAD inference timeout, T1 cancellation timeout, T1.21
# consecutive-failure threshold). The migration promotes them to
# ``VoiceTuningConfig`` so they're discoverable via the centralised
# tuning schema, env-var overridable via ``SOVYX_TUNING__VOICE__<NAME>``,
# and bound-validated against operationally-meaningful ranges.
#
# Reference: docs-internal/missions/MISSION-voice-final-skype-grade-2026.md
# §Phase 1 / T1.28.


class TestVoiceTuningT128Migration:
    """Pydantic Field bounds + default values for the 8 T1.28 knobs."""

    @staticmethod
    def _clear_voice_env(monkeypatch: pytest.MonkeyPatch) -> None:
        for key in list(os.environ):
            if key.startswith("SOVYX_TUNING__VOICE__"):
                monkeypatch.delenv(key, raising=False)

    def test_defaults_match_pre_migration_hardcoded_values(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The T1.28 migration is structural — not a behaviour change.
        Each new field's default MUST equal the pre-migration
        module-level constant value in
        ``voice/pipeline/_orchestrator.py``. This test pins every
        default so a future config-schema edit can't silently drift
        the runtime semantics.
        """
        self._clear_voice_env(monkeypatch)
        cfg = VoiceTuningConfig()
        assert cfg.pipeline_frame_drop_absolute_budget_seconds == 0.064
        assert cfg.pipeline_frame_drop_drift_window_frames == 32  # noqa: PLR2004
        assert cfg.pipeline_frame_drop_drift_ratio == 1.10
        assert cfg.pipeline_frame_drop_drift_rate_limit_seconds == 1.0
        assert cfg.pipeline_vad_inference_timeout_seconds == 0.250
        assert cfg.pipeline_vad_inference_timeout_warn_interval_seconds == 5.0
        assert cfg.pipeline_cancellation_task_timeout_seconds == 1.0
        assert cfg.pipeline_consecutive_tts_failure_threshold == 3  # noqa: PLR2004
        # T1.14 — coordinator-pending watchdog deadline default.
        assert cfg.pipeline_coordinator_pending_timeout_seconds == 30.0  # noqa: PLR2004

    def test_coordinator_pending_timeout_bounds_t114(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """T1.14 — `pipeline_coordinator_pending_timeout_seconds` bounds.
        Floor 1.0s prevents misconfiguration from racing the normal
        teardown; ceiling 300s caps the operator-visible "deaf for
        5 minutes" worst case.
        """
        from pydantic import ValidationError

        self._clear_voice_env(monkeypatch)
        with pytest.raises(ValidationError):
            VoiceTuningConfig(pipeline_coordinator_pending_timeout_seconds=0.5)
        with pytest.raises(ValidationError):
            VoiceTuningConfig(pipeline_coordinator_pending_timeout_seconds=600.0)

    def test_frame_drop_absolute_budget_bounds(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from pydantic import ValidationError

        self._clear_voice_env(monkeypatch)
        with pytest.raises(ValidationError):
            VoiceTuningConfig(pipeline_frame_drop_absolute_budget_seconds=0.001)
        with pytest.raises(ValidationError):
            VoiceTuningConfig(pipeline_frame_drop_absolute_budget_seconds=2.0)

    def test_frame_drop_drift_window_frames_bounds(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from pydantic import ValidationError

        self._clear_voice_env(monkeypatch)
        with pytest.raises(ValidationError):
            VoiceTuningConfig(pipeline_frame_drop_drift_window_frames=4)
        with pytest.raises(ValidationError):
            VoiceTuningConfig(pipeline_frame_drop_drift_window_frames=512)

    def test_frame_drop_drift_ratio_bounds(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from pydantic import ValidationError

        self._clear_voice_env(monkeypatch)
        # 1.0 = no drift detection ever fires; spec floor is 1.05.
        with pytest.raises(ValidationError):
            VoiceTuningConfig(pipeline_frame_drop_drift_ratio=1.0)
        with pytest.raises(ValidationError):
            VoiceTuningConfig(pipeline_frame_drop_drift_ratio=5.0)

    def test_frame_drop_drift_rate_limit_bounds(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from pydantic import ValidationError

        self._clear_voice_env(monkeypatch)
        with pytest.raises(ValidationError):
            VoiceTuningConfig(pipeline_frame_drop_drift_rate_limit_seconds=0.05)
        with pytest.raises(ValidationError):
            VoiceTuningConfig(pipeline_frame_drop_drift_rate_limit_seconds=120.0)

    def test_vad_inference_timeout_bounds(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from pydantic import ValidationError

        self._clear_voice_env(monkeypatch)
        with pytest.raises(ValidationError):
            VoiceTuningConfig(pipeline_vad_inference_timeout_seconds=0.010)
        with pytest.raises(ValidationError):
            VoiceTuningConfig(pipeline_vad_inference_timeout_seconds=10.0)

    def test_vad_inference_timeout_warn_interval_bounds(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from pydantic import ValidationError

        self._clear_voice_env(monkeypatch)
        with pytest.raises(ValidationError):
            VoiceTuningConfig(pipeline_vad_inference_timeout_warn_interval_seconds=0.1)
        with pytest.raises(ValidationError):
            VoiceTuningConfig(pipeline_vad_inference_timeout_warn_interval_seconds=600.0)

    def test_cancellation_task_timeout_bounds(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from pydantic import ValidationError

        self._clear_voice_env(monkeypatch)
        with pytest.raises(ValidationError):
            VoiceTuningConfig(pipeline_cancellation_task_timeout_seconds=0.05)
        with pytest.raises(ValidationError):
            VoiceTuningConfig(pipeline_cancellation_task_timeout_seconds=120.0)

    def test_consecutive_tts_failure_threshold_bounds(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from pydantic import ValidationError

        self._clear_voice_env(monkeypatch)
        # 0 would disable the abort entirely (defeats T1.21).
        with pytest.raises(ValidationError):
            VoiceTuningConfig(pipeline_consecutive_tts_failure_threshold=0)
        with pytest.raises(ValidationError):
            VoiceTuningConfig(pipeline_consecutive_tts_failure_threshold=200)

    def test_env_var_override_roundtrip(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A representative env-var override loads cleanly and the
        instantiated config reflects the overridden value. Pins the
        ``SOVYX_TUNING__VOICE__<NAME>`` plumbing for the new fields.
        """
        self._clear_voice_env(monkeypatch)
        monkeypatch.setenv("SOVYX_TUNING__VOICE__PIPELINE_VAD_INFERENCE_TIMEOUT_SECONDS", "0.5")
        cfg = VoiceTuningConfig()
        assert cfg.pipeline_vad_inference_timeout_seconds == 0.5


class TestDeprecatedMixerOverridesWarning:
    """Mission §9.1.1 / Gap 1b — deprecation surface for the four
    ``linux_mixer_*_fraction`` knobs scheduled for removal in v0.24.0.

    The contract: a stock install (no overrides) emits ZERO WARNs; an
    operator who set a non-default value via env or constructor kwarg
    gets ONE structured WARN per non-default knob, AND the function
    returns the canonical roster-order tuple of triggered fields so
    dashboards can render the "deprecated knobs in use" badge
    deterministically. Tests assert on the public return contract +
    a spy on the structlog ``logger.warning`` invocation count, which
    is invariant across the structlog → stdlib bridge configuration
    state (caplog under the project's structlog setup is sensitive to
    import order and processor configuration; the spy is not).
    """

    @staticmethod
    def _clear_voice_env(monkeypatch: pytest.MonkeyPatch) -> None:
        for key in list(os.environ):
            if key.startswith("SOVYX_TUNING__VOICE__"):
                monkeypatch.delenv(key, raising=False)

    def test_default_install_emits_no_warns(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A fresh install with no env / yaml overrides emits zero
        deprecation WARNs — operators must not see noise."""
        from unittest.mock import MagicMock, patch

        from sovyx.engine import config as config_mod
        from sovyx.engine.config import VoiceTuningConfig, warn_on_deprecated_mixer_overrides

        self._clear_voice_env(monkeypatch)
        spy = MagicMock()
        with patch.object(config_mod, "get_logger", return_value=spy, create=True):
            triggered = warn_on_deprecated_mixer_overrides(VoiceTuningConfig())
        assert triggered == ()
        spy.warning.assert_not_called()

    def test_single_override_emits_single_warn(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Setting ONE non-default value triggers one WARN naming
        only that field. Other defaulted fields stay silent."""
        from unittest.mock import MagicMock, patch

        from sovyx.engine.config import VoiceTuningConfig, warn_on_deprecated_mixer_overrides
        from sovyx.observability import logging as logging_mod

        self._clear_voice_env(monkeypatch)
        cfg = VoiceTuningConfig(linux_mixer_capture_reset_fraction=0.7)
        spy = MagicMock()
        with patch.object(logging_mod, "get_logger", return_value=spy):
            triggered = warn_on_deprecated_mixer_overrides(cfg)
        assert triggered == ("linux_mixer_capture_reset_fraction",)
        assert spy.warning.call_count == 1
        # The structured event name + key fields are passed as kwargs
        # to ``logger.warning(event_name, **labels)``.
        call = spy.warning.call_args
        assert call.args[0] == "voice.config.deprecated_mixer_fraction_in_use"
        kwargs = call.kwargs
        assert kwargs["voice.config.field"] == "linux_mixer_capture_reset_fraction"
        # T1.51 — removal target bumped from v0.24.0 to v0.27.0 (Phase 4).
        # Three deprecation surfaces share this target:
        # 1. ``voice.config.deprecated_mixer_fraction_in_use`` (this WARN,
        #    on the config-knob side).
        # 2. ``voice.deprecation.legacy_mixer_band_aid_call`` (function-
        #    level WARN at ``_linux_mixer_apply.py::_emit_legacy_band_aid_warning``).
        # 3. ``voice.mixer.alsa_band_aid_used`` (bypass-strategy WARN at
        #    ``_linux_alsa_mixer.py``).
        # All three MUST stay aligned for operator dashboards to render
        # a coherent deprecation roadmap.
        assert kwargs["voice.config.removal_target"] == "v0.27.0"
        assert "v0.27.0" in str(kwargs["voice.action_required"])

    def test_all_four_overrides_emit_four_warns(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """All four knobs flipped → four WARNs, one per knob, all on
        the same stable event name."""
        from unittest.mock import MagicMock, patch

        from sovyx.engine.config import VoiceTuningConfig, warn_on_deprecated_mixer_overrides
        from sovyx.observability import logging as logging_mod

        self._clear_voice_env(monkeypatch)
        cfg = VoiceTuningConfig(
            linux_mixer_boost_reset_fraction=0.1,
            linux_mixer_capture_reset_fraction=0.7,
            linux_mixer_capture_attenuation_fix_fraction=0.6,
            linux_mixer_boost_attenuation_fix_fraction=0.5,
        )
        spy = MagicMock()
        with patch.object(logging_mod, "get_logger", return_value=spy):
            triggered = warn_on_deprecated_mixer_overrides(cfg)
        assert set(triggered) == {
            "linux_mixer_boost_reset_fraction",
            "linux_mixer_capture_reset_fraction",
            "linux_mixer_capture_attenuation_fix_fraction",
            "linux_mixer_boost_attenuation_fix_fraction",
        }
        assert spy.warning.call_count == 4  # noqa: PLR2004
        # Every WARN uses the canonical event name.
        for call in spy.warning.call_args_list:
            assert call.args[0] == "voice.config.deprecated_mixer_fraction_in_use"
        # The set of fields emitted matches the set of triggered fields.
        emitted_fields = {call.kwargs["voice.config.field"] for call in spy.warning.call_args_list}
        assert emitted_fields == set(triggered)

    def test_env_override_to_default_value_does_not_warn(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Operator explicitly sets the default value via env → no WARN.
        ``isclose`` comparison guards against YAML 0.50 vs python 0.5
        round-trip false positives."""
        from unittest.mock import MagicMock, patch

        from sovyx.engine.config import VoiceTuningConfig, warn_on_deprecated_mixer_overrides
        from sovyx.observability import logging as logging_mod

        self._clear_voice_env(monkeypatch)
        monkeypatch.setenv(
            "SOVYX_TUNING__VOICE__LINUX_MIXER_CAPTURE_RESET_FRACTION",
            "0.5",
        )
        cfg = VoiceTuningConfig()
        spy = MagicMock()
        with patch.object(logging_mod, "get_logger", return_value=spy):
            triggered = warn_on_deprecated_mixer_overrides(cfg)
        assert triggered == ()
        spy.warning.assert_not_called()

    def test_returned_tuple_is_stable_for_dashboard(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Dashboard surfaces (e.g. a 'deprecated knobs in use' badge)
        rely on the returned tuple — ensure the order matches the
        deprecation roster declaration order so the UI can render
        deterministically."""
        from sovyx.engine.config import (
            _DEPRECATED_MIXER_FRACTIONS,
            VoiceTuningConfig,
            warn_on_deprecated_mixer_overrides,
        )

        self._clear_voice_env(monkeypatch)
        cfg = VoiceTuningConfig(
            linux_mixer_boost_reset_fraction=0.1,
            linux_mixer_capture_reset_fraction=0.7,
            linux_mixer_capture_attenuation_fix_fraction=0.6,
            linux_mixer_boost_attenuation_fix_fraction=0.5,
        )
        triggered = warn_on_deprecated_mixer_overrides(cfg)
        roster_order = tuple(name for name, _ in _DEPRECATED_MIXER_FRACTIONS)
        assert triggered == roster_order


class TestVoiceTuningParanoidMissionFlags:
    """Voice Windows Paranoid Mission (v0.24.0) — 5 feature flags
    + cross-validator.

    See ``docs-internal/missions/MISSION-voice-windows-paranoid-2026-04-26.md``.
    """

    def _clear_voice_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        for key in list(os.environ):
            if key.startswith("SOVYX_TUNING__VOICE__"):
                monkeypatch.delenv(key, raising=False)

    def test_paranoid_mission_flags_default_false(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Foundation phase (v0.24.0) ships every flag default-False —
        plumbing without behaviour change."""
        self._clear_voice_env(monkeypatch)
        cfg = VoiceTuningConfig()
        assert cfg.probe_cold_strict_validation_enabled is False
        assert cfg.bypass_tier1_raw_enabled is False
        assert cfg.bypass_tier2_host_api_rotate_enabled is False
        assert cfg.mm_notification_listener_enabled is False
        assert cfg.cascade_host_api_alignment_enabled is False

    def test_probe_cold_strict_validation_env_override(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Single env-var rollback / opt-in for Furo W-1."""
        monkeypatch.setenv("SOVYX_TUNING__VOICE__PROBE_COLD_STRICT_VALIDATION_ENABLED", "true")
        assert VoiceTuningConfig().probe_cold_strict_validation_enabled is True

    def test_bypass_tier1_raw_env_override(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("SOVYX_TUNING__VOICE__BYPASS_TIER1_RAW_ENABLED", "true")
        assert VoiceTuningConfig().bypass_tier1_raw_enabled is True

    def test_bypass_tier2_host_api_rotate_env_override(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Tier 2 requires alignment — set both so the cross-validator passes.
        monkeypatch.setenv("SOVYX_TUNING__VOICE__BYPASS_TIER2_HOST_API_ROTATE_ENABLED", "true")
        monkeypatch.setenv("SOVYX_TUNING__VOICE__CASCADE_HOST_API_ALIGNMENT_ENABLED", "true")
        cfg = VoiceTuningConfig()
        assert cfg.bypass_tier2_host_api_rotate_enabled is True
        assert cfg.cascade_host_api_alignment_enabled is True

    def test_mm_notification_listener_env_override(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("SOVYX_TUNING__VOICE__MM_NOTIFICATION_LISTENER_ENABLED", "true")
        assert VoiceTuningConfig().mm_notification_listener_enabled is True

    def test_cascade_host_api_alignment_env_override(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("SOVYX_TUNING__VOICE__CASCADE_HOST_API_ALIGNMENT_ENABLED", "true")
        assert VoiceTuningConfig().cascade_host_api_alignment_enabled is True

    def test_tier2_without_alignment_rejected_by_cross_validator(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Cross-validator rejects bypass_tier2_host_api_rotate_enabled=True
        without cascade_host_api_alignment_enabled=True at boot.

        Mission §D4: Tier 2 mutates host_api on the capture stream; without
        the opener alignment fix the next device-error reopen reverts to the
        legacy enumeration order and silently undoes the rotation.
        """
        self._clear_voice_env(monkeypatch)
        with pytest.raises(Exception) as exc_info:
            VoiceTuningConfig(
                bypass_tier2_host_api_rotate_enabled=True,
                cascade_host_api_alignment_enabled=False,
            )
        # pydantic wraps ValueError; assert on payload, not exact class
        # (xdist-safe per CLAUDE.md anti-pattern #8).
        msg = str(exc_info.value)
        assert "bypass_tier2_host_api_rotate_enabled" in msg
        assert "cascade_host_api_alignment_enabled" in msg
        assert "SOVYX_TUNING__VOICE__CASCADE_HOST_API_ALIGNMENT_ENABLED" in msg, (
            "remediation hint must include the env-var to flip"
        )

    def test_tier2_with_alignment_passes_cross_validator(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Tier 2 + alignment together is the supported wire-up combo."""
        self._clear_voice_env(monkeypatch)
        cfg = VoiceTuningConfig(
            bypass_tier2_host_api_rotate_enabled=True,
            cascade_host_api_alignment_enabled=True,
        )
        assert cfg.bypass_tier2_host_api_rotate_enabled is True
        assert cfg.cascade_host_api_alignment_enabled is True

    def test_tier2_disabled_alignment_either_way_passes(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Cross-validator only fires when Tier 2 is True; alignment alone
        without Tier 2 is a valid foundation/wire-up configuration."""
        self._clear_voice_env(monkeypatch)
        cfg_alignment_only = VoiceTuningConfig(
            bypass_tier2_host_api_rotate_enabled=False,
            cascade_host_api_alignment_enabled=True,
        )
        assert cfg_alignment_only.cascade_host_api_alignment_enabled is True
        cfg_neither = VoiceTuningConfig(
            bypass_tier2_host_api_rotate_enabled=False,
            cascade_host_api_alignment_enabled=False,
        )
        assert cfg_neither.bypass_tier2_host_api_rotate_enabled is False
        assert cfg_neither.cascade_host_api_alignment_enabled is False

    def test_engine_config_paranoid_mission_flags_via_nested_env(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``EngineConfig`` reads paranoid-mission flags through the nested
        ``SOVYX_TUNING__VOICE__*`` surface — same as every other voice
        tuning knob."""
        monkeypatch.setenv("SOVYX_TUNING__VOICE__PROBE_COLD_STRICT_VALIDATION_ENABLED", "true")
        monkeypatch.setenv("SOVYX_TUNING__VOICE__BYPASS_TIER1_RAW_ENABLED", "true")
        cfg = EngineConfig()
        assert cfg.tuning.voice.probe_cold_strict_validation_enabled is True
        assert cfg.tuning.voice.bypass_tier1_raw_enabled is True
