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
    log_file: Path | None = None


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


class SafetyTuningConfig(BaseSettings):
    """Tunable thresholds for the cognitive safety subsystem.

    All defaults match the previously hardcoded module-level constants —
    overriding via env vars (``SOVYX_TUNING__SAFETY__*``) or
    ``system.yaml`` is purely additive (zero behaviour change at default).

    Inherits from ``BaseSettings`` so that direct instantiation
    (``SafetyTuningConfig()``) honours ``SOVYX_TUNING__SAFETY__*`` env
    overrides — the module-level ``_CONST = _Tuning().field`` pattern
    used by subsystem modules relies on this.
    """

    model_config = SettingsConfigDict(env_prefix="SOVYX_TUNING__SAFETY__", extra="ignore")

    audit_flush_interval_seconds: float = 10.0
    audit_buffer_max: int = 100
    pii_ner_timeout_seconds: float = 2.0
    notification_debounce_seconds: float = 900.0  # 15 minutes


class BrainTuningConfig(BaseSettings):
    """Tunable thresholds for the Brain memory subsystem.

    See :class:`SafetyTuningConfig` for the ``BaseSettings`` rationale.
    """

    model_config = SettingsConfigDict(env_prefix="SOVYX_TUNING__BRAIN__", extra="ignore")

    star_topology_k: int = 15
    novelty_high_similarity: float = 0.85  # >= -> novelty 0.05 (near-dup)
    novelty_low_similarity: float = 0.30  # <= -> novelty 0.95 (very novel)
    cold_start_threshold: int = 10
    cold_start_novelty: float = 0.70
    model_download_cooldown_seconds: int = 900  # 15 minutes


class VoiceTuningConfig(BaseSettings):
    """Tunable thresholds for the voice pipeline + STT/TTS engines.

    See :class:`SafetyTuningConfig` for the ``BaseSettings`` rationale.
    """

    model_config = SettingsConfigDict(env_prefix="SOVYX_TUNING__VOICE__", extra="ignore")

    transcribe_timeout_seconds: float = 10.0
    streaming_drain_seconds: float = 0.5
    cloud_stt_timeout_seconds: float = 30.0
    cloud_stt_max_audio_seconds: float = 120.0
    auto_select_min_gpu_vram_mb: int = 4_000
    auto_select_high_ram_threshold_mb: int = 16_000
    auto_select_low_ram_threshold_mb: int = 2_048
    capture_reconnect_delay_seconds: float = 2.0
    capture_queue_maxsize: int = 256

    # AudioCaptureTask stream health — catches the silent-zeros failure
    # mode where sd.InputStream opens cleanly but delivers all-zero
    # frames (MME + unsupported rate, driver hang, privacy block). See
    # :mod:`sovyx.voice.device_enum` for the root-cause writeup.
    capture_validation_seconds: float = 0.6  # how long to observe frames post-open
    capture_validation_min_rms_db: float = -80.0  # any signal above this = "alive"
    capture_heartbeat_interval_seconds: float = 2.0  # RMS/frames log cadence
    capture_fallback_host_apis: list[str] = Field(
        default_factory=lambda: ["Windows WASAPI", "Windows DirectSound", "Core Audio", "ALSA"],
    )
    # WASAPI-specific opener behaviour. ``auto_convert`` lets the WASAPI
    # backend resample + rechannel + rechannel-type transparently; critical
    # for devices whose Windows mixer format is 2 ch float32 @ 48 kHz but
    # sovyx asks for 1 ch int16 @ 16 kHz (e.g. Razer BlackShark V2 Pro in
    # shared mode). Disable only to reproduce legacy behaviour for A/B.
    capture_wasapi_auto_convert: bool = True
    capture_wasapi_exclusive: bool = False
    # Let the opener upgrade ``channels`` to ``device.max_input_channels``
    # when a mono request is rejected (post-opener mixdown handled by the
    # callback). Hardware that only exposes stereo in shared mode depends
    # on this to pass through without auto_convert.
    capture_allow_channel_upgrade: bool = True

    # Voice device test (setup-wizard meters + TTS test button).
    # Kill-switch + ballistics + rate limiting for the test endpoints.
    device_test_enabled: bool = True
    device_test_frame_rate_hz: int = 30  # WS level frames per second
    device_test_peak_hold_ms: int = 1_500  # peak marker hold duration
    device_test_peak_decay_db_per_sec: float = 20.0  # decay after hold
    device_test_vad_trigger_db: float = -30.0  # shown as marker on meter
    device_test_clipping_db: float = -0.3  # clipping flag threshold
    device_test_reconnect_limit_per_min: int = 10  # per-token budget
    device_test_max_sessions_per_token: int = 1  # singleton per user
    device_test_max_phrase_chars: int = 200  # TTS test phrase cap
    device_test_output_job_ttl_seconds: int = 60  # job cleanup


class LLMTuningConfig(BaseSettings):
    """Tunable thresholds for the LLM router complexity classifier.

    Overridable via ``SOVYX_TUNING__LLM__SIMPLE_MAX_LENGTH=300`` etc.
    See :class:`SafetyTuningConfig` for the ``BaseSettings`` rationale.
    """

    model_config = SettingsConfigDict(env_prefix="SOVYX_TUNING__LLM__", extra="ignore")

    simple_max_length: int = 500
    simple_max_turns: int = 3
    complex_min_length: int = 2000
    complex_min_turns: int = 8


class TuningConfig(BaseModel):
    """Aggregate tuning knobs for cognitive / brain / voice / llm subsystems.

    Single source of truth for previously module-level constants. All
    defaults match the historical hardcoded values; subsystems read from
    a ``TuningConfig`` instance built from ``EngineConfig.tuning``.
    """

    safety: SafetyTuningConfig = Field(default_factory=SafetyTuningConfig)
    brain: BrainTuningConfig = Field(default_factory=BrainTuningConfig)
    voice: VoiceTuningConfig = Field(default_factory=VoiceTuningConfig)
    llm: LLMTuningConfig = Field(default_factory=LLMTuningConfig)


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
    tuning: TuningConfig = Field(default_factory=TuningConfig)

    @model_validator(mode="after")
    def resolve_log_file(self) -> EngineConfig:
        """Resolve log_file relative to data_dir when not explicitly set.

        Default: ``<data_dir>/logs/sovyx.log``.  This ensures that
        ``SOVYX_DATA_DIR=/data/sovyx`` puts logs at
        ``/data/sovyx/logs/sovyx.log`` instead of the hardcoded
        ``~/.sovyx/logs/sovyx.log``.

        If ``log_file`` is explicitly set (YAML, env, or override),
        the explicit value is preserved unchanged.
        """
        if self.log.log_file is None:
            self.log.log_file = self.data_dir / "logs" / "sovyx.log"
        return self


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
    except Exception as exc:  # noqa: BLE001
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
