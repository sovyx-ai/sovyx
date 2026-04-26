"""L1 — persistent memoization of (endpoint × winning_combo) tuples.

See :mod:`sovyx.voice.health` and ADR-combo-store-schema.md for the
full design. The combo store owns the on-disk shape, atomic writes,
cross-process file lock, and the 13 invalidation rules that make the
fast path safe against drift (driver updates, OS cumulative updates,
new APO chains, hardware changes).

The store is **advisory**. Every entry is re-validated on boot via at
least a cold probe; ``HEALTHY`` results clear the ``needs_revalidation``
flag, anything else falls through to the L2 cascade.

Subpackage layout (post v0.24.x split — anti-pattern #16):

* :mod:`._constants` — validation thresholds + tuning-derived knobs
  (``_PROBE_HISTORY_MAX``, ``_AGE_*``, ``_RMS_DB_*``, ``_VAD_*``,
  ``_BOOTS_VALIDATED_MIN``, ``_CHANNELS_*``, ``_FRAMES_PER_BUFFER_*``,
  ``_PIN_AUTO_UNPIN_FAILURE_THRESHOLD``).
* :mod:`._models` — dataclasses + serialization helpers
  (:class:`_LiveEntry`, :class:`_SanityError`,
  ``_fingerprint_to_dict``, ``_combo_to_dict``, ``_history_to_dict``,
  ``_entry_to_dict``, ``_utc_now``, ``_platform_label``,
  ``_allowed_host_apis``).
* :mod:`._store` — the :class:`ComboStore` class itself (load,
  invalidation rules, atomic writes, fast-path lookups, C2 auto-unpin
  lifecycle).

Public API: :class:`ComboStore`. The underscore-prefixed names are
re-exported here for back-compat with the v0.23.x single-file
``combo_store.py`` import contract — existing call sites in
``capture_overrides.py`` (``_combo_to_dict``) and the test suite
(``_PIN_AUTO_UNPIN_FAILURE_THRESHOLD``, ``_PROBE_HISTORY_MAX``,
``_LiveEntry``, ``_SanityError``, the four serializers) keep working
without import-line changes per anti-pattern #20.
"""

from __future__ import annotations

from sovyx.voice.health.combo_store._constants import (
    _AGE_DEGRADED_DAYS,
    _AGE_STALE_DAYS,
    _BOOTS_VALIDATED_MIN,
    _CHANNELS_MAX,
    _CHANNELS_MIN,
    _FRAMES_PER_BUFFER_MAX,
    _FRAMES_PER_BUFFER_MIN,
    _PIN_AUTO_UNPIN_FAILURE_THRESHOLD,
    _PROBE_HISTORY_MAX,
    _RMS_DB_MAX,
    _RMS_DB_MIN,
    _VAD_MAX,
    _VAD_MIN,
)
from sovyx.voice.health.combo_store._models import (
    _allowed_host_apis,
    _combo_to_dict,
    _entry_to_dict,
    _fingerprint_to_dict,
    _history_to_dict,
    _LiveEntry,
    _platform_label,
    _SanityError,
    _utc_now,
)
from sovyx.voice.health.combo_store._store import ComboStore

# Public API — only ``ComboStore``. Every other entry is a back-compat
# re-export for v0.23.x import contract preservation.
__all__ = [
    "ComboStore",
    "_AGE_DEGRADED_DAYS",
    "_AGE_STALE_DAYS",
    "_BOOTS_VALIDATED_MIN",
    "_CHANNELS_MAX",
    "_CHANNELS_MIN",
    "_FRAMES_PER_BUFFER_MAX",
    "_FRAMES_PER_BUFFER_MIN",
    "_LiveEntry",
    "_PIN_AUTO_UNPIN_FAILURE_THRESHOLD",
    "_PROBE_HISTORY_MAX",
    "_RMS_DB_MAX",
    "_RMS_DB_MIN",
    "_SanityError",
    "_VAD_MAX",
    "_VAD_MIN",
    "_allowed_host_apis",
    "_combo_to_dict",
    "_entry_to_dict",
    "_fingerprint_to_dict",
    "_history_to_dict",
    "_platform_label",
    "_utc_now",
]
