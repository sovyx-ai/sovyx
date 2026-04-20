"""EnvelopeProcessor — injects mandatory envelope fields into every log entry.

The envelope contract (``observability.schema.ENVELOPE_FIELDS``) requires
every structured log entry to carry eight fields. Four of those —
``timestamp``, ``level``, ``logger``, ``event`` — are added by the
default structlog processors (``TimeStamper``, ``add_log_level``,
``add_logger_name``, and the call-site keyword respectively). The
remaining four — ``schema_version``, ``process_id``, ``host``,
``sovyx_version`` — are injected here.

Each injected field is computed once at processor construction and
cached. Hot-path emit overhead is therefore four dict assignments per
record (sub-microsecond), keeping the §23 performance budget intact.

Aligned with docs-internal/plans/IMPL-OBSERVABILITY-001 §7 Task 1.3.
"""

from __future__ import annotations

import os
import platform
from importlib.metadata import PackageNotFoundError, version
from typing import TYPE_CHECKING, Any

from sovyx.observability.schema import SCHEMA_VERSION

if TYPE_CHECKING:
    from collections.abc import MutableMapping


def _resolve_sovyx_version() -> str:
    """Return the installed sovyx package version, or ``"unknown"`` if missing.

    The fallback exists so that an ad-hoc developer environment running
    sovyx straight from a checkout (without ``pip install -e .``) does
    not crash logging setup. In that case the daemon prints
    ``sovyx_version="unknown"``, which is preferable to refusing to log.
    """
    try:
        return version("sovyx")
    except PackageNotFoundError:
        return "unknown"


class EnvelopeProcessor:
    """Structlog processor that adds the four cached envelope fields.

    Adds ``schema_version`` (constant), ``process_id`` (``os.getpid()``),
    ``host`` (``platform.node()``), and ``sovyx_version``
    (``importlib.metadata.version``). All values are resolved once when
    the processor is instantiated and reused per record.

    The processor never overwrites a value already present on the
    record — call-site code that explicitly sets ``host=...`` (e.g. in
    a forwarded entry from another node) is preserved. This is what
    makes the processor safe to apply to entries originating outside
    the local daemon.
    """

    __slots__ = ("_cached",)

    def __init__(self) -> None:
        self._cached: dict[str, Any] = {
            "schema_version": SCHEMA_VERSION,
            "process_id": os.getpid(),
            "host": platform.node() or "unknown",
            "sovyx_version": _resolve_sovyx_version(),
        }

    def __call__(
        self,
        logger: Any,  # noqa: ANN401 — structlog protocol; opaque logger ref.
        method_name: str,
        event_dict: MutableMapping[str, Any],
    ) -> MutableMapping[str, Any]:
        """Inject the four cached envelope fields into ``event_dict``.

        Existing keys win over cached defaults so a forwarded entry's
        ``host``/``process_id``/``sovyx_version`` survive untouched.
        """
        for key, value in self._cached.items():
            event_dict.setdefault(key, value)
        return event_dict


__all__ = ["EnvelopeProcessor"]
