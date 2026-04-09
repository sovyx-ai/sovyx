"""Sovyx structured logging.

Configures structlog with JSON/console output, request-scoped context
(mind_id, conversation_id, request_id), and secret masking for sensitive fields.

Context Binding
---------------
Use :func:`bind_request_context` at the entry point of each request
(e.g. CogLoopGate worker) to inject ``mind_id``, ``conversation_id``,
and ``request_id`` into **every** log emitted within that async context.
Use :func:`clear_request_context` (or the :func:`bound_request_context`
context manager) to reset when the request is done.

The context is carried via ``structlog.contextvars``, which is both
thread-safe and asyncio-safe.
"""

from __future__ import annotations

import logging
import logging.handlers
import sys
import threading
import uuid
from contextlib import contextmanager
from typing import TYPE_CHECKING, Any

import structlog

if TYPE_CHECKING:
    from collections.abc import Generator, MutableMapping

    from sovyx.engine.config import LoggingConfig

# ── Request Context (via structlog.contextvars) ─────────────────────────────


def bind_request_context(
    *,
    mind_id: str = "",
    conversation_id: str = "",
    request_id: str | None = None,
    correlation_id: str = "",
    **extra: Any,  # noqa: ANN401
) -> None:
    """Bind request-scoped fields into the structlog context.

    All subsequent log calls in the **same async context** will include
    these fields automatically (via ``merge_contextvars`` processor).

    Args:
        mind_id: The mind being served (e.g. ``"default"``).
        conversation_id: Active conversation identifier.
        request_id: Unique ID for this request.  Auto-generated
            (UUID4 short form) when ``None``.
        correlation_id: Optional correlation / trace ID.  Kept for
            backward compatibility with the event bus.
        **extra: Any additional key-value pairs to include.
    """
    if request_id is None:
        request_id = uuid.uuid4().hex[:12]

    bindings: dict[str, Any] = {
        "request_id": request_id,
    }
    if mind_id:
        bindings["mind_id"] = mind_id
    if conversation_id:
        bindings["conversation_id"] = conversation_id
    if correlation_id:
        bindings["correlation_id"] = correlation_id
    if extra:
        bindings.update(extra)

    structlog.contextvars.bind_contextvars(**bindings)


def clear_request_context() -> None:
    """Remove all request-scoped context from the current async context.

    Clears **only** the keys managed by :func:`bind_request_context`
    plus any extra keys previously bound via ``structlog.contextvars``.
    """
    structlog.contextvars.clear_contextvars()


def get_request_context() -> dict[str, Any]:
    """Return a copy of the current structlog context-var bindings."""
    return dict(structlog.contextvars.get_contextvars())


@contextmanager
def bound_request_context(
    *,
    mind_id: str = "",
    conversation_id: str = "",
    request_id: str | None = None,
    correlation_id: str = "",
    **extra: Any,  # noqa: ANN401
) -> Generator[None, None, None]:
    """Context manager that binds request context on entry and clears on exit.

    Usage::

        with bound_request_context(mind_id="default", conversation_id="abc"):
            logger.info("inside request")  # includes mind_id, conversation_id
        # context is cleared here

    This works correctly in both sync and async code because
    ``structlog.contextvars`` is backed by Python ``contextvars``.
    """
    tokens = structlog.contextvars.bind_contextvars(
        mind_id=mind_id or "",
        conversation_id=conversation_id or "",
        request_id=request_id if request_id is not None else uuid.uuid4().hex[:12],
        **({"correlation_id": correlation_id} if correlation_id else {}),
        **extra,
    )
    try:
        yield
    finally:
        structlog.contextvars.reset_contextvars(**tokens)


# ── Backward Compatibility ──────────────────────────────────────────────────
# Events module uses set_correlation_id / get_correlation_id.
# Keep working — now delegates to structlog.contextvars.


def set_correlation_id(cid: str) -> None:
    """Set correlation ID for the current async context.

    .. deprecated:: 0.2
        Use :func:`bind_request_context` instead.
    """
    if cid:
        structlog.contextvars.bind_contextvars(correlation_id=cid)
    else:
        structlog.contextvars.unbind_contextvars("correlation_id")


def get_correlation_id() -> str:
    """Get correlation ID for the current async context.

    .. deprecated:: 0.2
        Use :func:`get_request_context` instead.
    """
    ctx = structlog.contextvars.get_contextvars()
    return str(ctx.get("correlation_id", ""))


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


# ── Setup ───────────────────────────────────────────────────────────────────

_setup_lock = threading.Lock()
_setup_done = False


def setup_logging(config: LoggingConfig) -> None:
    """Configure structlog for the entire application.

    This function is **idempotent and thread-safe**.  Multiple calls
    (tests, hot-reload, daemon reconfiguration) will cleanly tear down
    the previous configuration before applying the new one.  A
    ``threading.Lock`` serializes concurrent calls.

    Args:
        config: Logging configuration (level, console_format, log_file).

    Effects:
        - Configures structlog globally with shared processors.
        - Sets stdlib logging level.
        - Installs a ``StreamHandler`` (console) with the chosen renderer.
        - Optionally installs a ``RotatingFileHandler`` (always JSON).

    Processor chain (in order):
        1. ``merge_contextvars`` — inject request-scoped context
        2. ``add_log_level`` — add ``level`` field
        3. ``add_logger_name`` — add ``logger`` field
        4. ``TimeStamper`` — ISO-8601 timestamp
        5. ``StackInfoRenderer`` — optional stack trace
        6. ``SecretMasker`` — redact sensitive values
        7. Renderer (JSON or console, per handler)

    Idempotency guarantee:
        After each call, the root logger has exactly **1 StreamHandler**
        and **0 or 1 RotatingFileHandler** (depending on ``log_file``).
        No handler accumulation, no file descriptor leaks.
    """
    with _setup_lock:
        _setup_logging_locked(config)


def _setup_logging_locked(config: LoggingConfig) -> None:
    """Inner setup — called under ``_setup_lock``. Not part of public API."""
    global _setup_done  # noqa: PLW0603

    root_logger = logging.getLogger()

    # ── Teardown: close file handlers, clear all handlers ──
    # Close RotatingFileHandlers explicitly to release file descriptors.
    # Then clear the handler list entirely to prevent accumulation.
    for handler in list(root_logger.handlers):
        if isinstance(handler, logging.handlers.RotatingFileHandler):
            handler.close()
    root_logger.handlers.clear()

    # Reset structlog cache so new config takes effect on all loggers.
    if _setup_done:
        structlog.reset_defaults()

    # ── Shared processor chain ──
    shared_processors: list[structlog.types.Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.StackInfoRenderer(),
        SecretMasker(),
    ]

    # ── Console renderer ──
    if config.console_format == "json":
        console_renderer: structlog.types.Processor = structlog.processors.JSONRenderer()
    else:
        console_renderer = structlog.dev.ConsoleRenderer()

    structlog.configure(
        processors=[
            *shared_processors,
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )

    # ── Console handler (StreamHandler → stderr) ──
    console_formatter = structlog.stdlib.ProcessorFormatter(
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            console_renderer,
        ],
    )
    console_handler = logging.StreamHandler(sys.stderr)
    console_handler.setFormatter(console_formatter)
    root_logger.addHandler(console_handler)
    root_logger.setLevel(getattr(logging, config.level))

    # ── File handler (RotatingFileHandler → always JSON) ──
    if config.log_file is not None:
        config.log_file.parent.mkdir(parents=True, exist_ok=True)
        json_formatter = structlog.stdlib.ProcessorFormatter(
            processors=[
                structlog.stdlib.ProcessorFormatter.remove_processors_meta,
                structlog.processors.JSONRenderer(),
            ],
        )
        file_handler = logging.handlers.RotatingFileHandler(
            config.log_file,
            maxBytes=10 * 1024 * 1024,  # 10 MB
            backupCount=3,
            encoding="utf-8",
        )
        file_handler.setFormatter(json_formatter)
        root_logger.addHandler(file_handler)

    # ── Suppress noisy third-party loggers ──
    # httpx/httpcore emit INFO-level "HTTP Request: GET ..." lines that
    # bypass structlog formatting and pollute console output.
    # urllib3 and hpack (HTTP/2) are similarly noisy.
    for noisy_logger in ("httpx", "httpcore", "urllib3", "hpack"):
        logging.getLogger(noisy_logger).setLevel(logging.WARNING)

    _setup_done = True


def get_logger(name: str) -> structlog.stdlib.BoundLogger:
    """Return a bound logger for the given module name.

    Args:
        name: Module name (typically __name__).

    Returns:
        Configured structlog BoundLogger.  Any context bound via
        :func:`bind_request_context` is automatically included in
        every log call from this logger.
    """
    result: structlog.stdlib.BoundLogger = structlog.get_logger(name)
    return result
