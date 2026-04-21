"""EnvelopeProcessor — injects mandatory envelope fields into every log entry.

The envelope contract (``observability.schema.ENVELOPE_FIELDS``) requires
every structured log entry to carry nine fields. Four of those —
``timestamp``, ``level``, ``logger``, ``event`` — are added by the
default structlog processors (``TimeStamper``, ``add_log_level``,
``add_logger_name``, and the call-site keyword respectively). The
remaining five — ``schema_version``, ``process_id``, ``host``,
``sovyx_version``, ``sequence_no`` — are injected here.

The processor also OPPORTUNISTICALLY copies four contextual ids
(``saga_id``, ``span_id``, ``event_id``, ``cause_id``) from
structlog's bound contextvars when present. These four are NOT in
:data:`ENVELOPE_FIELDS` because they're scope-dependent — only entries
emitted inside a saga/span carry them. ``merge_contextvars`` already
populates them earlier in the processor chain; reading them here is a
belt-and-suspenders guarantee that EnvelopeProcessor remains
self-contained even if chain ordering changes.

Each cached field is computed once at processor construction. The
hot-path emit overhead is four dict assignments + one
:func:`itertools.count` increment + one :func:`get_contextvars` lookup
per record (sub-microsecond), keeping the §23 performance budget
intact.

Aligned with docs-internal/plans/IMPL-OBSERVABILITY-001 §7 Task 1.3
and §8 Task 2.2.
"""

from __future__ import annotations

import itertools
import os
import platform
import sys
import uuid
from importlib.metadata import PackageNotFoundError, version
from typing import TYPE_CHECKING, Any

from structlog.contextvars import get_contextvars

from sovyx.observability.schema import SCHEMA_VERSION

if TYPE_CHECKING:
    from collections.abc import Iterator, MutableMapping


# The four contextual ids EnvelopeProcessor lifts from structlog's
# contextvars. Kept narrow on purpose — adding more ids here means
# every log entry pays the lookup cost; only true scope-spanning ids
# qualify (saga = top-level operation, span = sub-operation, event =
# the EventBus dispatch currently in flight, cause = the parent of
# the current event in a handler chain).
_CONTEXTUAL_IDS: tuple[str, ...] = ("saga_id", "span_id", "event_id", "cause_id")


SERVICE_NAMESPACE: str = "sovyx"
"""OTel ``service.namespace`` — fixed per project so the same trace
backend can host multiple Sovyx-derivative deployments without label
collisions with other tenants. Imported by ``observability.otel`` so
spans and logs share the exact same string."""


SERVICE_INSTANCE_ID: str = str(uuid.uuid4())
"""Per-boot UUIDv4 — survives a daemon restart in the same container
(``process_id`` resets, ``service.instance.id`` rotates). Used to
correlate spans, metrics, and logs emitted by a single boot session
even when the OS recycles the PID. Computed once at module import so
every emitter (envelope + OTel Resource) sees the same value."""


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
    """Structlog processor that adds the cached envelope fields + sequence_no.

    Adds ``schema_version`` (constant), ``process_id`` (``os.getpid()``),
    ``host`` (``platform.node()``), ``sovyx_version``
    (``importlib.metadata.version``), and a per-process monotonic
    ``sequence_no``. The four cached values are resolved once at
    construction; ``sequence_no`` is drawn from an
    :func:`itertools.count` iterator on every call.

    ``itertools.count.__next__`` is atomic under the CPython GIL: even
    with hundreds of threads emitting concurrently, no two records get
    the same sequence number, and no lock is needed. The counter
    starts at 0 and resets per process, so the dedup key is the
    ``(timestamp, process_id, sequence_no)`` tuple — process_id
    discriminates restarts.

    The processor never overwrites a value already present on the
    record — call-site code that explicitly sets ``host=...`` or
    ``sequence_no=...`` (e.g. in a forwarded entry from another node)
    is preserved. This is what makes the processor safe to apply to
    entries originating outside the local daemon.
    """

    __slots__ = ("_cached", "_counter")

    def __init__(self) -> None:
        # Envelope (legacy contract) fields stay verbatim — schema
        # ``ENVELOPE_FIELDS`` and the dashboard query layer key off
        # ``host`` / ``process_id`` / ``sovyx_version``. Renaming
        # would break the wire format. The OTel-semconv-aligned
        # twins below ride alongside as ``extra="allow"`` payload
        # keys, so log forwarders that consume semconv get the
        # canonical names without extra translation.
        self._cached: dict[str, Any] = {
            "schema_version": SCHEMA_VERSION,
            "process_id": os.getpid(),
            "host": platform.node() or "unknown",
            "sovyx_version": _resolve_sovyx_version(),
            # OTel semconv 1.27+ resource attributes (mirrored on
            # every log so log/span join is trivial in the backend).
            "service.namespace": SERVICE_NAMESPACE,
            "service.instance.id": SERVICE_INSTANCE_ID,
            "host.arch": platform.machine() or "unknown",
            "process.runtime.name": sys.implementation.name,
            "process.runtime.version": platform.python_version(),
        }
        self._counter: Iterator[int] = itertools.count()

    def __call__(
        self,
        logger: Any,  # noqa: ANN401 — structlog protocol; opaque logger ref.
        method_name: str,
        event_dict: MutableMapping[str, Any],
    ) -> MutableMapping[str, Any]:
        """Inject cached envelope fields + a fresh ``sequence_no``.

        Also copies the contextual ids in :data:`_CONTEXTUAL_IDS` from
        structlog's bound contextvars when missing — this is normally
        redundant with :func:`structlog.contextvars.merge_contextvars`,
        but the lookup is cheap and the redundancy survives processor
        chain reordering.

        Existing keys win over generated defaults so a forwarded
        entry's ``host``/``process_id``/``sovyx_version``/``sequence_no``
        survive untouched. The local counter is only advanced when it
        actually contributes a value — this preserves the invariant
        ``next(counter) == number of locally-originated records``,
        which downstream gap-detection relies on.
        """
        for key, value in self._cached.items():
            event_dict.setdefault(key, value)
        if "sequence_no" not in event_dict:
            event_dict["sequence_no"] = next(self._counter)
        ctx = get_contextvars()
        for ctx_key in _CONTEXTUAL_IDS:
            if ctx_key not in event_dict:
                ctx_value = ctx.get(ctx_key)
                if ctx_value is not None:
                    event_dict[ctx_key] = ctx_value
        return event_dict


__all__ = ["SERVICE_INSTANCE_ID", "SERVICE_NAMESPACE", "EnvelopeProcessor"]
