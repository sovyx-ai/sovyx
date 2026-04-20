"""Per-field byte clamp — defends the entry-level budget from a single fat field.

The entry-level clamp (default 64 KB; see ``observability.tuning.max_entry_bytes``)
is necessary but not sufficient: a single 10 MB string field would
inflate the entry, and even after the entry-level clamp lopped the
tail off, that one field would have monopolised the budget and
pushed every other field out. Per-field clamp closes that hole by
capping each individual field at ``max_field_bytes`` (default 8 KB)
*before* the entry-level clamp runs.

Applies to ``str`` and ``bytes``/``bytearray`` values only — those
are the realistic attack/bug vectors (a wedged transcript, a giant
prompt, a 50k-frame audio buffer logged by accident). Numeric and
boolean types can't get pathologically large; nested ``dict``/``list``
values are deliberately *not* recursed because that would blow the
§23 hot-path budget (``logger.info(...)`` p99 < 200 µs).

Truncated values get a ``…[truncated:N]`` suffix where ``N`` is the
original byte size, so operators see at a glance both that the value
was clamped and how much was lost. The processor also accumulates a
per-field counter exposed via :meth:`flush_truncations` so a
downstream snapshotter (Phase 6) can emit aggregated
``logging.field_truncated`` events on a periodic cadence — emitting
inline would recurse through the processor chain.

Aligned with docs-internal/plans/IMPL-OBSERVABILITY-001 §22.1.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import MutableMapping


_PROTECTED_FIELDS: frozenset[str] = frozenset(
    {
        # Envelope (Task 1.3) — must never be truncated.
        "timestamp",
        "level",
        "logger",
        "event",
        "schema_version",
        "process_id",
        "host",
        "sovyx_version",
        # Causality envelope (Phase 2) — small UUIDs, never large.
        "saga_id",
        "span_id",
        "cause_id",
        "event_id",
        "sequence_no",
        # Internal meta the processor itself emits — clamping our own
        # truncation-report would be a self-defeating loop.
        "_field_truncations",
    }
)


def _truncate_str(value: str, max_bytes: int) -> tuple[str, int]:
    """Return ``(truncated, original_size)`` if *value* exceeds *max_bytes*.

    Encodes once to count UTF-8 bytes (``len(str)`` would over-count on
    multi-byte runes). On overflow, slices the byte buffer to make
    room for the suffix and decodes with ``errors="ignore"`` so a
    multi-byte sequence cut mid-codepoint produces a clean string
    instead of raising :class:`UnicodeDecodeError`.

    Returns the original ``(value, size)`` unchanged when within budget
    so the caller can do a single-statement guard.
    """
    encoded = value.encode("utf-8")
    size = len(encoded)
    if size <= max_bytes:
        return value, size
    suffix = f"…[truncated:{size}]"
    suffix_bytes = len(suffix.encode("utf-8"))
    head_budget = max(0, max_bytes - suffix_bytes)
    head = encoded[:head_budget].decode("utf-8", errors="ignore")
    return head + suffix, size


def _truncate_bytes(value: bytes | bytearray, max_bytes: int) -> tuple[bytes, int]:
    """Return ``(truncated, original_size)`` for byte values exceeding *max_bytes*.

    Mirrors :func:`_truncate_str` but operates on raw bytes — no
    decode step, so binary payloads (e.g. an audio chunk accidentally
    bound as a log field) stay intact up to the cap. Suffix is
    appended as ASCII bytes; the total returned length never exceeds
    ``max_bytes``.
    """
    size = len(value)
    if size <= max_bytes:
        return bytes(value), size
    suffix = f"…[truncated:{size}]".encode()
    head_budget = max(0, max_bytes - len(suffix))
    return bytes(value[:head_budget]) + suffix, size


class ClampFieldsProcessor:
    """Structlog processor that caps each field's byte size at ``max_bytes``.

    Hot-path: skips protected envelope keys, ignores non-string/bytes
    types, and short-circuits when a value is already within budget
    (the common case). Per-field truncations are recorded both inline
    (``_field_truncations`` on the current entry) and aggregated in
    :attr:`_truncations` for periodic emission of
    ``logging.field_truncated`` by an external snapshotter.

    Inline reporting is what makes a clamped entry self-describing —
    the operator immediately sees which field got chopped without
    waiting for the next snapshot tick. The aggregate counter exists
    because emitting a fresh log record from inside a processor would
    recurse through the same chain.
    """

    __slots__ = ("_max_bytes", "_truncations")

    def __init__(self, max_bytes: int) -> None:
        self._max_bytes = max_bytes
        self._truncations: dict[str, int] = {}

    def __call__(
        self,
        logger: Any,  # noqa: ANN401 — structlog protocol; opaque logger ref.
        method_name: str,
        event_dict: MutableMapping[str, Any],
    ) -> MutableMapping[str, Any]:
        """Clamp every oversized ``str``/``bytes`` field in ``event_dict``.

        Iterates a snapshot of items so we can mutate ``event_dict``
        in place (Python forbids mutating a dict during iteration of
        its live view). Protected fields are skipped first to keep
        the inner loop branch-free for the common case.
        """
        truncated_meta: list[dict[str, int | str]] = []
        for key, value in list(event_dict.items()):
            if key in _PROTECTED_FIELDS:
                continue
            if isinstance(value, str):
                new_value, size = _truncate_str(value, self._max_bytes)
                if new_value is not value:
                    event_dict[key] = new_value
                    self._truncations[key] = self._truncations.get(key, 0) + 1
                    truncated_meta.append({"field": key, "original_size": size})
            elif isinstance(value, (bytes, bytearray)):
                new_bytes, size = _truncate_bytes(value, self._max_bytes)
                if len(new_bytes) != size:
                    event_dict[key] = new_bytes
                    self._truncations[key] = self._truncations.get(key, 0) + 1
                    truncated_meta.append({"field": key, "original_size": size})
        if truncated_meta:
            event_dict["_field_truncations"] = truncated_meta
        return event_dict

    def flush_truncations(self) -> dict[str, int]:
        """Return + reset the per-field truncation counts.

        Intended for the Phase 6 hot-path snapshotter, which polls
        every ``perf_hotpath_interval_seconds`` and emits a
        ``logging.field_truncated`` aggregate event with the totals
        since the last poll.
        """
        out = self._truncations.copy()
        self._truncations.clear()
        return out


__all__ = ["ClampFieldsProcessor"]
