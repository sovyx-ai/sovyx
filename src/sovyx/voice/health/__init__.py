"""Voice Capture Health Lifecycle (VCHL) — see ADR-voice-capture-health-lifecycle.md.

This subpackage owns end-to-end resilience for microphone capture across
Windows / Linux / macOS:

* L0 :mod:`~sovyx.voice.health.contract` — the dataclass + enum vocabulary
  every other layer speaks (``Diagnosis``, ``ProbeMode``, ``Combo``,
  ``ProbeResult``, ``RemediationHint``, ``ComboEntry``, ``OverrideEntry``).
* L1 :mod:`~sovyx.voice.health.combo_store` — persistent JSON memoization
  of (endpoint × winning_combo) tuples with 13 invalidation rules and
  atomic + locked writes.
* L1 :mod:`~sovyx.voice.health.capture_overrides` — sibling file for
  user-pinned combos that survive ``--reset``.
* L2 :mod:`~sovyx.voice.health.cascade` — cascading open strategies per
  platform, gated by the lifecycle lock and a wall-clock budget.
* L3 :mod:`~sovyx.voice.health.probe` — the single probe entry point
  with cold / warm modes.

Internal helpers (underscore-prefixed) are not part of the public surface
and may move between releases.
"""

from __future__ import annotations

from sovyx.voice.health.capture_overrides import CaptureOverrides
from sovyx.voice.health.combo_store import ComboStore
from sovyx.voice.health.contract import (
    ALLOWED_FORMATS,
    ALLOWED_HOST_APIS_BY_PLATFORM,
    ALLOWED_SAMPLE_RATES,
    AudioSubsystemFingerprint,
    CascadeResult,
    Combo,
    ComboEntry,
    ComboStoreStats,
    Diagnosis,
    LoadReport,
    OverrideEntry,
    ProbeHistoryEntry,
    ProbeMode,
    ProbeResult,
    RemediationHint,
)
from sovyx.voice.health.probe import probe

__all__ = [
    "ALLOWED_FORMATS",
    "ALLOWED_HOST_APIS_BY_PLATFORM",
    "ALLOWED_SAMPLE_RATES",
    "AudioSubsystemFingerprint",
    "CaptureOverrides",
    "CascadeResult",
    "Combo",
    "ComboEntry",
    "ComboStore",
    "ComboStoreStats",
    "Diagnosis",
    "LoadReport",
    "OverrideEntry",
    "ProbeHistoryEntry",
    "ProbeMode",
    "ProbeResult",
    "RemediationHint",
    "probe",
]
