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

import contextlib
import json
import logging
import logging.handlers
import sys
import threading
import uuid
from contextlib import contextmanager
from typing import TYPE_CHECKING, Any

import structlog

from sovyx.observability._clamp_fields import ClampFieldsProcessor
from sovyx.observability._exception_serializer import ExceptionTreeProcessor
from sovyx.observability._fast_path import (
    FastPathFilter,
    FastPathHandler,
    NonFastPathFilter,
)
from sovyx.observability.anomaly import AnomalyDetector
from sovyx.observability.async_handler import AsyncQueueHandler, BackgroundLogWriter
from sovyx.observability.envelope import EnvelopeProcessor
from sovyx.observability.failure_dictionary import ErrorEnricher
from sovyx.observability.pii import PIIRedactor
from sovyx.observability.ringbuffer import RingBufferHandler, install_crash_hooks
from sovyx.observability.sampling import SamplingProcessor

if TYPE_CHECKING:
    from collections.abc import Generator, MutableMapping
    from pathlib import Path

    from sovyx.engine.config import LoggingConfig, ObservabilityConfig

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
_async_writer: BackgroundLogWriter | None = None
_data_dir: Path | None = None
_RUNTIME_OVERRIDE_FILENAME = "runtime_log_overrides.json"


def setup_logging(
    config: LoggingConfig,
    obs_config: ObservabilityConfig | None = None,
    *,
    data_dir: Path | None = None,
) -> None:
    """Configure structlog for the entire application.

    This function is **idempotent and thread-safe**.  Multiple calls
    (tests, hot-reload, daemon reconfiguration) will cleanly tear down
    the previous configuration before applying the new one.  A
    ``threading.Lock`` serializes concurrent calls.

    Args:
        config: Logging configuration (level, console_format, log_file).
        obs_config: Optional observability configuration that turns on
            envelope injection, PII redaction, sampling, the async file
            writer and the in-memory crash ring buffer. ``None``
            preserves the legacy single-handler behaviour for back-compat
            with tests that pre-date the observability subsystem.
        data_dir: Optional resolved data directory used to locate the
            persisted runtime-level overrides file. When ``None``,
            ``runtime_set_level(persist=True)`` becomes a no-op.

    Effects:
        - Configures structlog globally with shared processors.
        - Sets stdlib logging level.
        - Installs a ``StreamHandler`` (console) with the chosen renderer.
        - When ``log_file`` is set: installs a ``RotatingFileHandler``
          (always JSON), wrapped in :class:`AsyncQueueHandler` +
          :class:`BackgroundLogWriter` if ``obs_config.features.async_queue``.
        - When ``obs_config`` is set: installs a :class:`RingBufferHandler`
          and wires :func:`install_crash_hooks` if a crash dump path
          is configured.
        - When ``obs_config.fast_path_file`` is set: installs a
          :class:`FastPathHandler` ahead of the async/file path so
          CRITICAL/security records skip the queue and ``fsync``
          synchronously to disk; the async/file path gets a
          :class:`NonFastPathFilter` to prevent double-emit.

    Processor chain (in order):
        1. ``merge_contextvars`` — inject request-scoped context
        2. ``add_log_level`` — add ``level`` field
        3. ``add_logger_name`` — add ``logger`` field
        4. ``TimeStamper`` — ISO-8601 timestamp
        5. ``StackInfoRenderer`` — optional stack trace
        6. :class:`EnvelopeProcessor` — schema_version, process_id,
           host, sovyx_version (only when ``obs_config`` is given)
        7. :class:`SecretMasker` — redact sensitive values
        8. :class:`ExceptionTreeProcessor` — convert ``exc_info`` into
           a structured cause/context/group tree (PEP 654 aware)
        9. :class:`PIIRedactor` — gated by ``features.pii_redaction``
        10. :class:`SamplingProcessor` — keep-every-N for hot events
        11. :class:`ClampFieldsProcessor` — per-field byte cap
            (``observability.tuning.max_field_bytes``)
        12. Renderer (JSON or console, per handler)

    Idempotency guarantee:
        After each call, the root logger has exactly **1 StreamHandler**,
        **0 or 1** file/async handler (per ``log_file``) and **0 or 1**
        :class:`RingBufferHandler` (per ``obs_config``). No handler
        accumulation, no file descriptor leaks. Any previously running
        :class:`BackgroundLogWriter` is drained and stopped before the
        new pipeline is wired in.
    """
    with _setup_lock:
        _setup_logging_locked(config, obs_config, data_dir)


def _setup_logging_locked(
    config: LoggingConfig,
    obs_config: ObservabilityConfig | None,
    data_dir: Path | None,
) -> None:
    """Inner setup — called under ``_setup_lock``. Not part of public API."""
    global _setup_done, _async_writer, _data_dir  # noqa: PLW0603

    root_logger = logging.getLogger()

    # ── Teardown ──
    # Stop any background log writer first so the old async queue is
    # drained before we tear down its file handler downstream.
    if _async_writer is not None:
        _async_writer.drain_and_stop(timeout=2.0)
        _async_writer = None
    for handler in list(root_logger.handlers):
        if isinstance(handler, (logging.handlers.RotatingFileHandler, FastPathHandler)):
            handler.close()
    root_logger.handlers.clear()

    # Reset structlog cache so new config takes effect on all loggers.
    if _setup_done:
        structlog.reset_defaults()

    _data_dir = data_dir

    # ── Shared processor chain ──
    shared_processors: list[structlog.types.Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.StackInfoRenderer(),
    ]
    if obs_config is not None:
        shared_processors.append(EnvelopeProcessor())
    shared_processors.append(SecretMasker())
    # ExceptionTreeProcessor runs before PII/sampling/clamp so the
    # serialized chain (exc.message, exc.cause_chain entries) flows
    # through the same redaction + size-budget passes as any other
    # field. Always installed — preserving cause chains is a
    # forensics requirement, not a feature toggle.
    shared_processors.append(ExceptionTreeProcessor())
    if obs_config is not None:
        if obs_config.features.pii_redaction:
            shared_processors.append(PIIRedactor(obs_config.pii))
        # ErrorEnricher runs AFTER PIIRedactor so signature regexes don't
        # match against raw PII (e.g., a phone number embedded in a
        # message). Runs BEFORE ClampFieldsProcessor so the diagnosis
        # hint isn't truncated by the per-field budget. Always installed
        # — operators always benefit from diagnosis hints, and the cost
        # is paid only by WARNING+ entries (see ErrorEnricher).
        shared_processors.append(ErrorEnricher())
        # SamplingProcessor is always installed when obs_config is set;
        # it only drops events that are explicitly registered for
        # rate-limiting (see _SAMPLED_EVENTS in observability.sampling).
        shared_processors.append(SamplingProcessor(obs_config.sampling))
        # Per-field clamp runs LAST so it measures post-redaction
        # sizes (a fully-masked credit-card field is small, no point
        # truncating a value that PIIRedactor already shortened).
        # Sits before wrap_for_formatter so JSONRenderer sees clamped
        # values, never the raw 10 MB string. See §22.1.
        shared_processors.append(ClampFieldsProcessor(obs_config.tuning.max_field_bytes))
        # AnomalyDetector observes the FULLY-enriched + clamped entry,
        # so its own emits ride the same envelope/redaction guarantees.
        # Mounted last so its `__call__` is the final read-only pass
        # before wrap_for_formatter hands the dict to the renderer.
        if obs_config.features.anomaly_detection:
            shared_processors.append(AnomalyDetector(obs_config.tuning))

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

    # ── Fast-path handler (synchronous, fsync-on-emit) ──
    # Attached BEFORE the async/file handler so a same-record run
    # through the chain hits the fast-path first. Pair with the
    # NonFastPathFilter on the async handler below: each record
    # ends up in exactly one downstream file.
    if obs_config is not None and obs_config.fast_path_file is not None:
        fast_formatter = structlog.stdlib.ProcessorFormatter(
            processors=[
                structlog.stdlib.ProcessorFormatter.remove_processors_meta,
                structlog.processors.JSONRenderer(),
            ],
        )
        fast_handler = FastPathHandler(obs_config.fast_path_file)
        fast_handler.setFormatter(fast_formatter)
        fast_handler.addFilter(FastPathFilter())
        root_logger.addHandler(fast_handler)

    # ── File handler (RotatingFileHandler → always JSON) ──
    if config.log_file is not None:
        config.log_file.parent.mkdir(parents=True, exist_ok=True)
        json_formatter = structlog.stdlib.ProcessorFormatter(
            processors=[
                structlog.stdlib.ProcessorFormatter.remove_processors_meta,
                structlog.processors.JSONRenderer(),
            ],
        )
        max_bytes = obs_config.file_max_bytes if obs_config is not None else 10 * 1024 * 1024
        backup_count = obs_config.file_backup_count if obs_config is not None else 3
        file_handler = logging.handlers.RotatingFileHandler(
            config.log_file,
            maxBytes=max_bytes,
            backupCount=backup_count,
            encoding="utf-8",
        )
        file_handler.setFormatter(json_formatter)
        # Mirror of FastPathFilter: when fast-path is wired, drop
        # those records here so they don't double-emit through the
        # rotating file. Safe to attach unconditionally — the filter
        # only diverts records the fast-path handler would have
        # caught, and that handler isn't wired without obs_config.
        if obs_config is not None and obs_config.fast_path_file is not None:
            file_handler.addFilter(NonFastPathFilter())
        if obs_config is not None and obs_config.features.async_queue:
            async_handler = AsyncQueueHandler(maxsize=obs_config.async_queue_size)
            if obs_config.fast_path_file is not None:
                async_handler.addFilter(NonFastPathFilter())
            writer = BackgroundLogWriter(async_handler, [file_handler])
            writer.start()
            _async_writer = writer
            root_logger.addHandler(async_handler)
        else:
            root_logger.addHandler(file_handler)

    # ── Ring buffer + crash dump hooks ──
    if obs_config is not None:
        ring_handler = RingBufferHandler(capacity=obs_config.ring_buffer_size)
        ring_formatter = structlog.stdlib.ProcessorFormatter(
            processors=[
                structlog.stdlib.ProcessorFormatter.remove_processors_meta,
                structlog.processors.JSONRenderer(),
            ],
        )
        ring_handler.setFormatter(ring_formatter)
        root_logger.addHandler(ring_handler)
        if obs_config.crash_dump_path is not None:
            install_crash_hooks(ring_handler, obs_config.crash_dump_path)

    # ── Suppress noisy third-party loggers ──
    # httpx/httpcore emit INFO-level "HTTP Request: GET ..." lines that
    # bypass structlog formatting and pollute console output.
    # urllib3 and hpack (HTTP/2) are similarly noisy.
    for noisy_logger in ("httpx", "httpcore", "urllib3", "hpack"):
        logging.getLogger(noisy_logger).setLevel(logging.WARNING)

    # ── Dedicated audit handler ──
    # Audit events (config changes, license activations, permission
    # grants) get their own rotating file so retention is decoupled
    # from main log rotation and they survive even when sovyx.log is
    # being investigated/drained. The handler is attached to the
    # `sovyx.audit` stdlib logger with propagate=False, so audit
    # entries never leak into sovyx.log.
    if data_dir is not None:
        from sovyx.observability.audit import setup_audit_handler  # noqa: PLC0415

        setup_audit_handler(data_dir / "audit" / "audit.jsonl")

    _setup_done = True

    # Re-apply persisted runtime overrides so an investigation that
    # outlives a daemon restart keeps its temporary log levels.
    _load_runtime_overrides()


def runtime_set_level(logger_name: str, level: str, *, persist: bool = False) -> None:
    """Change *logger_name*'s level at runtime without restarting.

    Args:
        logger_name: Dotted logger name (``""`` for the root logger).
        level: Standard level name (``DEBUG``/``INFO``/``WARNING``/...).
        persist: When True, write the override to
            ``<data_dir>/runtime_log_overrides.json`` so subsequent
            ``setup_logging()`` calls re-apply it. No-op when
            ``data_dir`` was not supplied to ``setup_logging``.
    """
    logging.getLogger(logger_name).setLevel(level)
    if persist:
        _persist_override(logger_name, level)


def runtime_get_level(logger_name: str) -> str:
    """Return the *effective* level for *logger_name* (resolving inheritance)."""
    return logging.getLevelName(logging.getLogger(logger_name).getEffectiveLevel())


def _override_path() -> Path | None:
    """Resolve the persisted-override path against the current data_dir."""
    if _data_dir is None:
        return None
    return _data_dir / _RUNTIME_OVERRIDE_FILENAME


def _persist_override(logger_name: str, level: str) -> None:
    """Atomically merge ``{logger_name: level}`` into the override file."""
    path = _override_path()
    if path is None:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    current: dict[str, str] = {}
    if path.exists():
        with contextlib.suppress(OSError, json.JSONDecodeError):
            loaded = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                current = {str(k): str(v) for k, v in loaded.items()}
    current[logger_name] = level
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(current, indent=2, sort_keys=True), encoding="utf-8")
    tmp.replace(path)


def _load_runtime_overrides() -> None:
    """Re-apply persisted overrides on boot. Best-effort; bad entries are ignored."""
    path = _override_path()
    if path is None or not path.exists():
        return
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return
    if not isinstance(data, dict):
        return
    for logger_name, level in data.items():
        if isinstance(logger_name, str) and isinstance(level, str):
            with contextlib.suppress(ValueError):
                logging.getLogger(logger_name).setLevel(level)


def shutdown_logging(timeout: float = 5.0) -> None:
    """Drain the async queue, flush handlers, release file descriptors.

    Idempotent — safe to call multiple times. If the background writer
    cannot drain within *timeout* seconds, the daemon thread is left
    behind (it is a daemon and will exit with the process); the call
    never blocks the process from exiting.
    """
    global _async_writer  # noqa: PLW0603
    if _async_writer is not None:
        _async_writer.drain_and_stop(timeout=timeout)
        _async_writer = None
    root = logging.getLogger()
    for handler in list(root.handlers):
        with contextlib.suppress(Exception):
            handler.flush()
        if isinstance(
            handler,
            logging.handlers.RotatingFileHandler | RingBufferHandler | FastPathHandler,
        ):
            with contextlib.suppress(Exception):
                handler.close()
        root.removeHandler(handler)


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
