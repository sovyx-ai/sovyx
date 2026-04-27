"""L2 — Cascading open strategies.

See ADR §4.2 + §5.5 + §5.6. Given an endpoint the cascade tries combos in
priority order until a probe returns :attr:`~sovyx.voice.health.contract.Diagnosis.HEALTHY`:

1. :class:`~sovyx.voice.health.capture_overrides.CaptureOverrides` — the
   user-pinned combo for this endpoint, if one exists (source ``"pinned"``).
2. :class:`~sovyx.voice.health.combo_store.ComboStore` fast path — the last
   known-good combo for this endpoint, if one exists and isn't flagged
   ``needs_revalidation`` (source ``"store"``).
3. Platform cascade — :data:`WINDOWS_CASCADE` / :data:`LINUX_CASCADE` /
   :data:`MACOS_CASCADE`, tried in declaration order (source ``"cascade"``).

The cascade is wrapped in two safety rails:

* **Lifecycle lock** (ADR §5.5). Per-endpoint :class:`asyncio.Lock`
  stored in an :class:`~sovyx.engine._lock_dict.LRULockDict` so only one
  cascade / invalidation / record-winning ever runs against a given
  endpoint at a time. Prevents hot-plug races and doctor-vs-daemon
  races. Bounded to 64 endpoints to satisfy CLAUDE.md anti-pattern #15.

* **Time budget** (ADR §5.6). Total 30 s wall-clock for the whole
  cascade (6 default attempts × ~5 s each, 8 for the opt-in aggressive
  variant); per-attempt 5 s via the probe's hard timeout. On
  total-budget exhaustion the cascade returns with
  ``budget_exhausted=True`` and the best attempt so far (or none).

On a HEALTHY winner the cascade records the combo to the ComboStore
(unless the winner came from the store already) so the next boot hits
the fast path.

Cross-platform note: Linux and macOS cascade tables are defined here
but marked empty for Sprint 1 — Tasks #27 / #28 populate them with the
ALSA / CoreAudio-specific entries from ADR §4.2. A cascade on an
unsupported platform returns ``source="none"`` with no attempts; the
caller is expected to fall back to the legacy single-open path.

Module layout (split per CLAUDE.md anti-pattern #16 — see
``MISSION-voice-godfile-splits-v0.24.1.md`` Part 3 / T02):

* :mod:`._planner` — pure cascade builders + the four public cascade
  tuples + :func:`build_linux_cascade_for_device`.
* :mod:`._alignment` — pinned override / ComboStore fast-path lookups
  + L2.5 mixer-sanity helper.
* :mod:`._budget` — tuning constants + lifecycle locks +
  quarantine/record-winner helpers.
* :mod:`._executor` — :func:`run_cascade`, :func:`run_cascade_for_candidates`,
  :class:`ProbeCallable` + per-attempt probe wrapper + structured log
  helpers.

Every public-by-history symbol is re-exported below; importers may
continue to use ``from sovyx.voice.health.cascade import X`` unchanged.
"""

from __future__ import annotations

from sovyx.voice.health.cascade._alignment import (
    _run_mixer_sanity,  # noqa: F401 — re-exported for test_cascade_mixer_sanity.py direct calls
)
from sovyx.voice.health.cascade._executor import (
    ProbeCallable,
    run_cascade,
    run_cascade_for_candidates,
)

# ``_platform_cascade`` is an internal helper but
# ``tests/unit/voice/health/test_cascade.py::test_platform_dispatch``
# imports it directly to verify the platform → cascade-table dispatch.
# Re-exported via ``noqa: F401`` to preserve the test import without
# giving it a public commitment in ``__all__``.
from sovyx.voice.health.cascade._planner import (
    LINUX_CASCADE,
    MACOS_CASCADE,
    WINDOWS_CASCADE,
    WINDOWS_CASCADE_AGGRESSIVE,
    _platform_cascade,  # noqa: F401 — re-exported for test_cascade.py
    build_linux_cascade_for_device,
)

__all__ = [
    "LINUX_CASCADE",
    "MACOS_CASCADE",
    "WINDOWS_CASCADE",
    "WINDOWS_CASCADE_AGGRESSIVE",
    "ProbeCallable",
    "build_linux_cascade_for_device",
    "run_cascade",
    "run_cascade_for_candidates",
]
