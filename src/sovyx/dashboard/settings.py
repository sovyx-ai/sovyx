"""Dashboard settings — read/write Engine configuration.

GET: reads current EngineConfig + runtime state.
PUT: updates mutable settings (log level, telemetry, API config).
Persists changes to system.yaml when possible.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

import yaml

from sovyx.observability.logging import get_logger

if TYPE_CHECKING:
    from pathlib import Path

    from sovyx.engine.config import EngineConfig

logger = get_logger(__name__)


def get_settings(config: EngineConfig) -> dict[str, Any]:
    """Build settings response from EngineConfig."""
    return {
        "log_level": config.log.level,
        "log_format": config.log.format,
        "log_file": str(config.log.log_file) if config.log.log_file else None,
        "data_dir": str(config.data_dir),
        "telemetry_enabled": config.telemetry.enabled,
        "api_enabled": config.api.enabled,
        "api_host": config.api.host,
        "api_port": config.api.port,
        "relay_enabled": config.relay.enabled,
    }


def apply_settings(
    config: EngineConfig,
    updates: dict[str, Any],
    config_path: Path | None = None,
) -> dict[str, str]:
    """Apply mutable settings updates.

    Returns dict of changes applied: {"field": "old → new"}.
    Only certain fields are mutable at runtime.
    """
    changes: dict[str, str] = {}
    mutable_fields = {
        "log_level": _update_log_level,
    }

    for key, value in updates.items():
        handler = mutable_fields.get(key)
        if handler is not None:
            old_val = handler(config, value)
            if old_val is not None:
                changes[key] = old_val

    # Persist to yaml if path provided and changes made
    if changes and config_path is not None:
        _persist_to_yaml(config, config_path)

    return changes


def _update_log_level(config: EngineConfig, value: object) -> str | None:
    """Update log level at runtime."""
    valid = ("DEBUG", "INFO", "WARNING", "ERROR")
    level = str(value).upper()
    if level not in valid:
        return None

    old = config.log.level
    if old == level:
        return None

    # Update structlog + stdlib root logger
    logging.getLogger().setLevel(getattr(logging, level))
    # Update config object (LoggingConfig is mutable BaseModel)
    config.log.level = level  # type: ignore[assignment]  # Literal narrowing

    logger.info("log_level_changed", old=old, new=level)
    return f"{old} → {level}"


def _persist_to_yaml(config: EngineConfig, config_path: Path) -> None:
    """Persist current config to system.yaml."""
    try:
        data: dict[str, Any] = {}
        if config_path.exists():
            with config_path.open("r") as f:
                data = yaml.safe_load(f) or {}

        # Update mutable sections
        if "log" not in data:
            data["log"] = {}
        data["log"]["level"] = config.log.level

        with config_path.open("w") as f:
            yaml.dump(data, f, default_flow_style=False)

        logger.debug("settings_persisted", path=str(config_path))
    except Exception:  # noqa: BLE001
        logger.warning("settings_persist_failed", path=str(config_path))
