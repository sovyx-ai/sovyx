"""Validation thresholds + tuning-derived constants for ComboStore.

All knobs that shape the 13 invalidation rules + the C2 auto-unpin
lifecycle live here, sourced from :class:`VoiceTuningConfig` where
appropriate so ``SOVYX_TUNING__VOICE__*`` env vars override without
reaching into module internals (anti-pattern #17).
"""

from __future__ import annotations

from sovyx.engine.config import VoiceTuningConfig as _VoiceTuning

# ── Per-entry probe-history sizing ────────────────────────────────


_PROBE_HISTORY_MAX = _VoiceTuning().combo_probe_history_max
"""Per-entry probe-history capacity. Sourced from
:attr:`sovyx.engine.config.VoiceTuningConfig.combo_probe_history_max`
at import time so ``SOVYX_TUNING__VOICE__COMBO_PROBE_HISTORY_MAX``
overrides without reaching into the module. Anti-pattern #17 — every
threshold via tuning config; never a hardcoded constant in a
production code path."""


# ── Age-based degradation rules (R10 / R11) ───────────────────────


_AGE_DEGRADED_DAYS = 30
"""Days after which an entry whose last-boot diagnosis was non-HEALTHY
is dropped (R10)."""

_AGE_STALE_DAYS = 90
"""Days after which any entry — regardless of last-boot diagnosis — is
considered stale and dropped (R11). Backstop for endpoints that the
daemon hasn't seen in months."""


# ── Sanity-check ranges (R12) ─────────────────────────────────────


_RMS_DB_MIN = -90.0
_RMS_DB_MAX = 0.0
_VAD_MIN = 0.0
_VAD_MAX = 1.0
_BOOTS_VALIDATED_MIN = 0
_CHANNELS_MIN = 1
_CHANNELS_MAX = 8
_FRAMES_PER_BUFFER_MIN = 64
_FRAMES_PER_BUFFER_MAX = 8192


# ── C2 pin auto-unpin lifecycle ───────────────────────────────────


# C2 (pinned-entry auto-unpin lifecycle).
#
# Pre-C2 a pinned ComboStore entry stayed pinned forever — the cascade
# would re-validate it every boot (R6/R7/R8/R10/R11) but never unpin
# even if every validation failed. The mission identified this as the
# Ring 1 ComboStore band-aid (§3.8): a stale pin on a device that
# stopped working (driver update broke the combo, hardware swapped on
# the same GUID slot) silently prevents the cascade from finding a
# working alternative. C2 introduces a lifecycle contract: if a pinned
# entry's probe returns non-HEALTHY :data:`_PIN_AUTO_UNPIN_FAILURE_THRESHOLD`
# consecutive times the pin is automatically released, surfaced via
# the ``voice.combo_store.pin_auto_unpinned`` event so operators can
# attribute the decision.

_PIN_AUTO_UNPIN_FAILURE_THRESHOLD = 2
"""Number of consecutive non-HEALTHY probe outcomes after which a
pinned entry is auto-unpinned.

Two is the SRE-canonical "twice is coincidence, not pattern but
worth acting on" threshold — a single transient failure (USB
renegotiation, momentary CPU contention) shouldn't release a user
pin, but two in a row is enough signal that the pinned combo no
longer reliably works on this hardware. The pin is released and the
next boot cycle lets the cascade pick a fresh winner; the user can
re-pin manually if the original combo is preferred for non-health
reasons (latency, format compatibility).
"""


__all__ = [
    "_AGE_DEGRADED_DAYS",
    "_AGE_STALE_DAYS",
    "_BOOTS_VALIDATED_MIN",
    "_CHANNELS_MAX",
    "_CHANNELS_MIN",
    "_FRAMES_PER_BUFFER_MAX",
    "_FRAMES_PER_BUFFER_MIN",
    "_PIN_AUTO_UNPIN_FAILURE_THRESHOLD",
    "_PROBE_HISTORY_MAX",
    "_RMS_DB_MAX",
    "_RMS_DB_MIN",
    "_VAD_MAX",
    "_VAD_MIN",
]
