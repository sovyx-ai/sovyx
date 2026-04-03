"""Sovyx structured logging.

Configures structlog with JSON/console output, correlation IDs,
and secret masking for sensitive fields.
"""

from __future__ import annotations

import logging
import sys
from contextvars import ContextVar
from typing import TYPE_CHECKING, Any

import structlog

if TYPE_CHECKING:
    from collections.abc import MutableMapping

    from sovyx.engine.config import LoggingConfig

# ── Correlation ID management ───────────────────────────────────────────────

_correlation_id: ContextVar[str] = ContextVar("correlation_id", default="")


def set_correlation_id(cid: str) -> None:
    """Set correlation ID for the current async context."""
    _correlation_id.set(cid)


def get_correlation_id() -> str:
    """Get correlation ID for the current async context."""
    return _correlation_id.get()


# ── Secret Masking ──────────────────────────────────────────────────────────

_SENSITIVE_KEYS = frozenset({"token", "key", "password", "secret", "api_key", "api_key_env"})


class SecretMasker:
    """Structlog processor that masks sensitive values in log events.

    Any field whose name contains 'token', 'key', 'password', or 'secret'
    will have its value masked: "sk-abc...xyz" (first 3 + last 3 chars).
    Values shorter than 8 chars are fully masked as "***".
    """

    @staticmethod
    def _is_sensitive(key: str) -> bool:
        """Check if a field name indicates a sensitive value."""
        key_lower = key.lower()
        return any(s in key_lower for s in _SENSITIVE_KEYS)

    @staticmethod
    def _mask_value(value: str) -> str:
        """Mask a sensitive string value."""
        if len(value) < 8:
            return "***"
        return f"{value[:3]}...{value[-3:]}"

    def __call__(
        self,
        logger: Any,  # noqa: ANN401
        method_name: str,
        event_dict: MutableMapping[str, Any],
    ) -> MutableMapping[str, Any]:
        """Process log event dict, masking sensitive fields."""
        for key, value in event_dict.items():
            if isinstance(value, str) and self._is_sensitive(key):
                event_dict[key] = self._mask_value(value)
        return event_dict


# ── Correlation ID Processor ────────────────────────────────────────────────


def _add_correlation_id(
    logger: Any,  # noqa: ANN401
    method_name: str,
    event_dict: MutableMapping[str, Any],
) -> MutableMapping[str, Any]:
    """Inject correlation_id from contextvars into every log event."""
    cid = _correlation_id.get()
    if cid:
        event_dict["correlation_id"] = cid
    return event_dict


# ── Setup ───────────────────────────────────────────────────────────────────

_setup_done = False


def setup_logging(config: LoggingConfig) -> None:
    """Configure structlog for the entire application.

    Args:
        config: Logging configuration (level, format).

    Effects:
        - Configures structlog globally with shared processors.
        - Sets stdlib logging level.
        - JSON output for production, colored console for development.
    """
    global _setup_done  # noqa: PLW0603

    shared_processors: list[structlog.types.Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.StackInfoRenderer(),
        _add_correlation_id,
        SecretMasker(),
    ]

    if config.format == "json":
        renderer: structlog.types.Processor = structlog.processors.JSONRenderer()
    else:
        renderer = structlog.dev.ConsoleRenderer()

    structlog.configure(
        processors=[
            *shared_processors,
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )

    # Configure stdlib logging to use structlog formatter
    formatter = structlog.stdlib.ProcessorFormatter(
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            renderer,
        ],
    )

    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(formatter)

    root_logger = logging.getLogger()
    root_logger.handlers.clear()
    root_logger.addHandler(handler)
    root_logger.setLevel(getattr(logging, config.level))

    _setup_done = True


def get_logger(name: str) -> structlog.stdlib.BoundLogger:
    """Return a bound logger for the given module name.

    Args:
        name: Module name (typically __name__).

    Returns:
        Configured structlog BoundLogger.
    """
    result: structlog.stdlib.BoundLogger = structlog.get_logger(name)
    return result
