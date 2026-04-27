"""L0 — the contract every VCHL layer speaks.

This module defines the vocabulary used across the Voice Capture Health
Lifecycle: enums for diagnoses + probe modes, dataclasses for combos +
probe results + persisted entries + cascade outcomes, plus the small set
of platform-aware sanity validators that gate object creation.

The contract is intentionally validation-strict at construction time so a
malformed :class:`Combo` (e.g. 192 channels, sample rate 12345) cannot
propagate beyond the boundary that built it. Persisted JSON entries
re-validate on load via :class:`~sovyx.voice.health.combo_store.ComboStore`
so a corrupted on-disk file cannot poison the runtime either.

Module layout (split per CLAUDE.md anti-pattern #16 — see
``MISSION-voice-godfile-splits-v0.24.1.md`` Part 3 / T01):

* :mod:`._diagnosis` — :class:`Diagnosis` enum.
* :mod:`._combo` — cascade-combo + persistence types
  (:class:`Combo`, :class:`CandidateEndpoint`, :class:`CandidateSource`,
  :class:`CascadeResult`, :class:`ComboEntry`, :class:`OverrideEntry`,
  :class:`ProbeMode`, :class:`LoadReport`, :class:`ComboStoreStats`)
  plus the platform validation tables (``ALLOWED_*`` constants).
* :mod:`._probe_result` — probe outcomes + abstract capture interface
  (:class:`ProbeResult`, :class:`ProbeHistoryEntry`,
  :class:`RemediationHint`, :class:`AudioSubsystemFingerprint`,
  :class:`CaptureTaskProto`).
* :mod:`._bypass` — capture-integrity + bypass + watchdog event types
  (:class:`IntegrityVerdict`, :class:`IntegrityResult`,
  :class:`BypassVerdict`, :class:`BypassContext`, :class:`BypassOutcome`,
  :class:`HotplugEvent`, :class:`PowerEvent`, :class:`AudioServiceEvent`,
  :class:`WatchdogState` + ``*Kind`` enums).
* :mod:`._eligibility` — bypass eligibility + L2.5 mixer-sanity types
  (:class:`Eligibility`, all ``Mixer*`` types,
  :class:`HardwareContext`).

Every public-by-history symbol is re-exported below; importers may
continue to use ``from sovyx.voice.health.contract import X`` unchanged.
"""

from __future__ import annotations

from sovyx.voice.health.contract._bypass import (
    AudioServiceEvent,
    AudioServiceEventKind,
    BypassContext,
    BypassOutcome,
    BypassVerdict,
    HotplugEvent,
    HotplugEventKind,
    IntegrityResult,
    IntegrityVerdict,
    PowerEvent,
    PowerEventKind,
    WatchdogState,
)
from sovyx.voice.health.contract._combo import (
    ALLOWED_FORMATS,
    ALLOWED_HOST_APIS_BY_PLATFORM,
    ALLOWED_SAMPLE_RATES,
    CandidateEndpoint,
    CandidateSource,
    CascadeResult,
    Combo,
    ComboEntry,
    ComboStoreStats,
    LoadReport,
    OverrideEntry,
    ProbeMode,
)
from sovyx.voice.health.contract._diagnosis import Diagnosis
from sovyx.voice.health.contract._eligibility import (
    Eligibility,
    FactorySignature,
    HardwareContext,
    MixerApplySnapshot,
    MixerCardSnapshot,
    MixerControlRole,
    MixerControlSnapshot,
    MixerKBProfile,
    MixerPresetControl,
    MixerPresetSpec,
    MixerPresetValue,
    MixerPresetValueDb,
    MixerPresetValueFraction,
    MixerPresetValueRaw,
    MixerSanityDecision,
    MixerSanityResult,
    MixerValidationMetrics,
    ValidationGates,
    VerificationRecord,
)
from sovyx.voice.health.contract._probe_result import (
    AudioSubsystemFingerprint,
    CaptureTaskProto,
    ProbeHistoryEntry,
    ProbeResult,
    RemediationHint,
)

__all__ = [
    "ALLOWED_FORMATS",
    "ALLOWED_HOST_APIS_BY_PLATFORM",
    "ALLOWED_SAMPLE_RATES",
    "AudioServiceEvent",
    "AudioServiceEventKind",
    "AudioSubsystemFingerprint",
    "BypassContext",
    "BypassOutcome",
    "BypassVerdict",
    "CandidateEndpoint",
    "CandidateSource",
    "CaptureTaskProto",
    "CascadeResult",
    "Combo",
    "ComboEntry",
    "ComboStoreStats",
    "Diagnosis",
    "Eligibility",
    "FactorySignature",
    "HardwareContext",
    "HotplugEvent",
    "HotplugEventKind",
    "IntegrityResult",
    "IntegrityVerdict",
    "LoadReport",
    "MixerApplySnapshot",
    "MixerCardSnapshot",
    "MixerControlRole",
    "MixerControlSnapshot",
    "MixerKBProfile",
    "MixerPresetControl",
    "MixerPresetSpec",
    "MixerPresetValue",
    "MixerPresetValueDb",
    "MixerPresetValueFraction",
    "MixerPresetValueRaw",
    "MixerSanityDecision",
    "MixerSanityResult",
    "MixerValidationMetrics",
    "OverrideEntry",
    "PowerEvent",
    "PowerEventKind",
    "ProbeHistoryEntry",
    "ProbeMode",
    "ProbeResult",
    "RemediationHint",
    "ValidationGates",
    "VerificationRecord",
    "WatchdogState",
]
