"""Sovyx engine configuration.

Loads configuration from system.yaml, environment variables (SOVYX_ prefix),
and programmatic overrides. Priority: overrides > env > yaml > defaults.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from sovyx.engine.errors import ConfigNotFoundError, ConfigValidationError


class LoggingConfig(BaseModel):
    """Structured logging configuration.

    Console and file outputs use **independent** formats:

    - **console_format** controls ``StreamHandler`` output:
      ``"text"`` (default) for colored human-readable logs,
      ``"json"`` for machine-parseable output (CI/systemd).

    - **File handler** always writes JSON (for dashboard log viewer
      and ``sovyx logs --json``).  This is by design — the file is a
      machine interface, not a human one.

    Backward compatibility:
        Legacy ``format`` key in system.yaml is silently migrated
        to ``console_format`` with a deprecation warning.
    """

    level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    console_format: Literal["json", "text"] = "text"
    log_file: Path | None = Field(
        default_factory=lambda: Path.home() / ".sovyx" / "logs" / "sovyx.log",
    )


class DatabaseConfig(BaseModel):
    """SQLite database configuration."""

    data_dir: Path = Field(default_factory=lambda: Path.home() / ".sovyx")
    wal_mode: bool = True
    mmap_size: int = 256 * 1024 * 1024  # 256MB
    cache_size: int = -64000  # 64MB (negative = KB)
    read_pool_size: int = 3


class TelemetryConfig(BaseModel):
    """Telemetry opt-in/out configuration."""

    enabled: bool = False


class RelayConfig(BaseModel):
    """Relay server configuration."""

    enabled: bool = False


class APIConfig(BaseModel):
    """REST API configuration."""

    enabled: bool = True
    host: str = "127.0.0.1"
    port: int = 7777
    cors_origins: list[str] = Field(default_factory=lambda: ["http://localhost:7777"])


class HardwareConfig(BaseModel):
    """Hardware tier detection configuration."""

    tier: Literal["auto", "pi", "n100", "gpu"] = "auto"
    mmap_size_mb: int = 128


class LLMProviderConfig(BaseModel):
    """Configuration for a single LLM provider."""

    name: str
    model: str
    api_key_env: str = ""
    endpoint: str | None = None
    timeout_seconds: int = 30
    circuit_breaker_failures: int = 3
    circuit_breaker_reset_seconds: int = 300


class LLMDefaultsConfig(BaseModel):
    """Engine-level LLM defaults. MindConfig.llm can override per-Mind."""

    routing_strategy: Literal["auto", "always-local", "always-cloud"] = "auto"
    providers: list[LLMProviderConfig] = Field(default_factory=list)
    degradation_message: str = (
        "I'm having trouble thinking clearly right now — "
        "my language models are unavailable. I can still "
        "remember things and listen to you."
    )


class SocketConfig(BaseModel):
    """Unix socket path for daemon RPC.

    Auto-resolves: /run/sovyx/sovyx.sock (systemd) or ~/.sovyx/sovyx.sock (user).
    """

    path: str = ""

    @model_validator(mode="after")
    def resolve_path(self) -> SocketConfig:
        """Auto-resolve socket path based on environment."""
        if not self.path:
            system_path = Path("/run/sovyx")
            if system_path.exists() and os.access(system_path, os.W_OK):
                self.path = "/run/sovyx/sovyx.sock"
            else:
                self.path = str(Path.home() / ".sovyx" / "sovyx.sock")
        return self


class EngineConfig(BaseSettings):
    """Global Sovyx daemon configuration.

    Inherits from BaseSettings (pydantic-settings) for env_prefix support.

    Priority (highest to lowest):
        1. Environment variables (SOVYX_*)
        2. system.yaml
        3. Hardcoded defaults
    """

    model_config = SettingsConfigDict(env_prefix="SOVYX_", env_nested_delimiter="__")

    data_dir: Path = Field(default_factory=lambda: Path.home() / ".sovyx")
    log: LoggingConfig = Field(default_factory=LoggingConfig)
    database: DatabaseConfig = Field(default_factory=DatabaseConfig)
    hardware: HardwareConfig = Field(default_factory=HardwareConfig)
    llm: LLMDefaultsConfig = Field(default_factory=LLMDefaultsConfig)
    telemetry: TelemetryConfig = Field(default_factory=TelemetryConfig)
    relay: RelayConfig = Field(default_factory=RelayConfig)
    api: APIConfig = Field(default_factory=APIConfig)
    socket: SocketConfig = Field(default_factory=SocketConfig)


def load_engine_config(
    config_path: Path | None = None,
    overrides: dict[str, Any] | None = None,
) -> EngineConfig:
    """Load engine configuration with merge: defaults → yaml → env → overrides.

    Args:
        config_path: Path to system.yaml. If None, uses defaults + env only.
        overrides: Programmatic overrides (highest priority after env).

    Returns:
        Fully resolved EngineConfig.

    Raises:
        ConfigNotFoundError: config_path provided but file does not exist.
        ConfigValidationError: YAML contains invalid fields or values.
    """
    yaml_data: dict[str, Any] = {}

    if config_path is not None:
        if not config_path.exists():
            raise ConfigNotFoundError(
                f"Configuration file not found: {config_path}",
                context={"path": str(config_path)},
            )
        try:
            raw = config_path.read_text(encoding="utf-8")
            parsed = yaml.safe_load(raw)
            if parsed is not None:
                if not isinstance(parsed, dict):
                    raise ConfigValidationError(
                        "Configuration file must contain a YAML mapping",
                        context={"path": str(config_path), "type": type(parsed).__name__},
                    )
                yaml_data = parsed
        except yaml.YAMLError as exc:
            raise ConfigValidationError(
                f"Invalid YAML in configuration file: {exc}",
                context={"path": str(config_path)},
            ) from exc

    # Backward compatibility: migrate legacy "format" → "console_format"
    _migrate_legacy_log_format(yaml_data)

    if overrides:
        yaml_data = _deep_merge(yaml_data, overrides)

    try:
        return EngineConfig(**yaml_data)
    except Exception as exc:
        raise ConfigValidationError(
            f"Configuration validation failed: {exc}",
            context={"fields": str(yaml_data.keys())},
        ) from exc


def _migrate_legacy_log_format(data: dict[str, Any]) -> None:
    """Migrate legacy ``log.format`` to ``log.console_format``.

    Mutates *data* in place.  Emits a deprecation warning (via stdlib
    ``warnings``) so users see it once and know to update their YAML.

    The ``format`` field was renamed to ``console_format`` in v0.5.24
    to clarify that it only controls console output (the file handler
    always writes JSON).

    This migration is idempotent: if both ``format`` and
    ``console_format`` exist, ``console_format`` wins and ``format``
    is silently dropped.
    """
    import warnings

    log_section = data.get("log")
    if not isinstance(log_section, dict):
        return

    if "format" not in log_section:
        return

    legacy_value = log_section.pop("format")

    if "console_format" not in log_section:
        log_section["console_format"] = legacy_value
        warnings.warn(
            "Configuration key 'log.format' is deprecated since v0.5.24. "
            "Use 'log.console_format' instead. "
            f"Migrated automatically: console_format={legacy_value!r}",
            DeprecationWarning,
            stacklevel=2,
        )


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    """Deep merge two dicts. Override values win on conflict."""
    result = base.copy()
    for key, value in override.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result
