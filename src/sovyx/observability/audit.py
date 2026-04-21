"""Dedicated audit log — config / license / permission mutations.

Audit events are first-class evidence for compliance reviews and post-
incident reconstruction; they must survive even when the main log
pipeline is being investigated, drained, or rotated. This module
isolates them in their own file (`<data_dir>/audit/audit.jsonl` by
default) reached via a stdlib logger named ``sovyx.audit`` whose
``propagate`` is set to ``False`` — so audit entries never leak into
``sovyx.log`` and the main file rotation can't displace them.

The structlog processor chain (envelope, secret-masker, PII redactor,
clamp) still applies because the same global structlog configuration
processes every record before the handlers receive it. Audit handlers
just choose where the result lands.

Public surface:
    * :func:`get_audit_logger` — bound structlog logger to use from
      callers (dashboard routes, license validator, plugin permission
      manager).
    * :func:`setup_audit_handler` — idempotent install of the dedicated
      :class:`logging.handlers.RotatingFileHandler`. Called from
      :func:`sovyx.observability.logging.setup_logging` after the main
      file handler is wired so audit retention is decoupled from the
      main log retention budget.
    * :func:`emit_config_change` — convenience helper that emits the
      canonical ``audit.config.changed`` envelope. Routes call this so
      the schema stays consistent across config / settings endpoints.

Aligned with IMPL-OBSERVABILITY-001 §15 (Phase 9, Tasks 9.3–9.5).
"""

from __future__ import annotations

import logging
import logging.handlers
from typing import TYPE_CHECKING, Any

import structlog

from sovyx.observability.logging import get_logger

if TYPE_CHECKING:
    from pathlib import Path

_AUDIT_LOGGER_NAME = "sovyx.audit"
_AUDIT_HANDLER_ATTR = "_sovyx_audit_handler"


def get_audit_logger() -> Any:  # noqa: ANN401 — structlog BoundLogger proxy.
    """Return the bound structlog logger for the ``sovyx.audit`` namespace.

    Always safe to call — if :func:`setup_audit_handler` has not been
    invoked yet, audit events still flow through the main pipeline
    (because ``propagate`` defaults to True until the handler is
    installed). Once the handler is installed, ``propagate`` flips to
    False so audit entries land *only* in the dedicated file.
    """
    return structlog.get_logger(_AUDIT_LOGGER_NAME)


def setup_audit_handler(
    audit_path: Path,
    *,
    max_bytes: int = 10 * 1024 * 1024,
    backup_count: int = 10,
) -> None:
    """Install a dedicated rotating JSON file handler on ``sovyx.audit``.

    Idempotent: previous audit handlers attached by this function are
    closed and removed before the new one is installed, so repeated
    calls (tests, hot reload) don't leak file descriptors.

    Args:
        audit_path: Destination file. Parent directory is created if
            missing.
        max_bytes: Per-file rotation threshold. Defaults to 10 MiB —
            larger than the main log's per-rotation default because
            audit events carry compact envelopes and operators expect
            longer retention.
        backup_count: Rotation depth. Defaults to 10 (≈100 MiB total),
            which on a typical engagement covers ~6 months of config
            changes. Override via :func:`setup_audit_handler` callers
            when stricter retention is required.

    The handler reuses :class:`structlog.stdlib.ProcessorFormatter`
    with a :class:`structlog.processors.JSONRenderer` so audit lines
    have the exact same shape as ``sovyx.log`` entries — the only
    difference is the destination file and the ``logger`` field
    (``sovyx.audit.<sub>`` vs. ``sovyx.<module>``).
    """
    audit_logger = logging.getLogger(_AUDIT_LOGGER_NAME)

    previous = getattr(audit_logger, _AUDIT_HANDLER_ATTR, None)
    if previous is not None:
        audit_logger.removeHandler(previous)
        with _suppress(OSError):
            previous.close()
        setattr(audit_logger, _AUDIT_HANDLER_ATTR, None)

    audit_path.parent.mkdir(parents=True, exist_ok=True)
    formatter = structlog.stdlib.ProcessorFormatter(
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            structlog.processors.JSONRenderer(),
        ],
    )
    handler = logging.handlers.RotatingFileHandler(
        audit_path,
        maxBytes=max_bytes,
        backupCount=backup_count,
        encoding="utf-8",
    )
    handler.setFormatter(formatter)

    audit_logger.addHandler(handler)
    audit_logger.setLevel(logging.INFO)
    # Stop entries from also landing in sovyx.log so audit retention is
    # decoupled from the main log rotation budget. The structlog
    # pipeline (envelope, redaction, clamp) still runs because it is
    # mounted at the structlog level, not the stdlib handler level.
    audit_logger.propagate = False
    setattr(audit_logger, _AUDIT_HANDLER_ATTR, handler)

    get_logger(__name__).info(
        "audit.handler.installed",
        **{
            "audit.path": str(audit_path),
            "audit.max_bytes": max_bytes,
            "audit.backup_count": backup_count,
        },
    )


def emit_config_change(
    field_path: str,
    *,
    old_value_summary: str | None,
    new_value_summary: str | None,
    actor: str,
    request_id: str | None,
    source: str = "dashboard",
) -> None:
    """Emit the canonical ``audit.config.changed`` envelope.

    Args:
        field_path: Dotted path of the changed field, e.g.
            ``safety.child_safe_mode`` or ``log_level``.
        old_value_summary: Human-readable rendering of the previous
            value. Pre-redacted by the caller — the audit log inherits
            secret-masker + PII redaction from the structlog pipeline,
            but callers should still avoid passing raw secrets.
        new_value_summary: Human-readable rendering of the new value.
        actor: Who triggered the change. Free-form for now
            (``"user"``, ``"admin"``, ``"api"``); stricter taxonomy
            arrives with Task 9.5 (permission audit).
        request_id: Optional request correlation id, populated by
            :class:`RequestIdMiddleware` on dashboard routes.
        source: Where the change originated. ``"dashboard"`` for
            interactive PUT endpoints, ``"cli"`` / ``"yaml"`` reserved
            for future hookpoints.
    """
    get_audit_logger().info(
        "audit.config.changed",
        **{
            "audit.field": field_path,
            "audit.old": old_value_summary,
            "audit.new": new_value_summary,
            "audit.actor": actor,
            "audit.request_id": request_id,
            "audit.source": source,
        },
    )


def parse_change_summary(value: str) -> tuple[str | None, str | None]:
    """Split a ``"old → new"`` change marker into its two halves.

    The dashboard config/settings helpers return changes as a flat
    ``dict[str, str]`` where each value is rendered as
    ``f"{old} → {new}"``. The audit emitter wants the two sides
    separated so the resulting JSON line is queryable on either field.

    Returns ``(old, new)`` when the arrow is found; falls back to
    ``(None, value)`` so callers always have a non-empty ``new`` when
    parsing fails (e.g., enforced-by-coherence rewrites that include
    parenthetical context after the arrow).
    """
    arrow = " \u2192 "  # " → " — single source for the rendering convention.
    if arrow in value:
        old, new = value.split(arrow, 1)
        return (old.strip() or None, new.strip() or None)
    return (None, value.strip() or None)


# ── Internal helpers ────────────────────────────────────────────────


class _suppress:
    """Tiny ``contextlib.suppress`` clone — avoids importing contextlib."""

    def __init__(self, *exc: type[BaseException]) -> None:
        self._exc = exc

    def __enter__(self) -> None:  # noqa: D401 — context-manager protocol.
        return None

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> bool:  # noqa: ANN401
        return exc_type is not None and issubclass(exc_type, self._exc)


__all__ = [
    "emit_config_change",
    "get_audit_logger",
    "parse_change_summary",
    "setup_audit_handler",
]
