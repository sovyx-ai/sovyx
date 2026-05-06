"""Voice calibration: forensic-driven config decision pipeline.

This package owns the proactive auto-configuration surface that turns
the L1 :mod:`sovyx.voice.diagnostics` toolkit (forensic observation
only) into a self-calibrating pipeline that captures hardware
fingerprint + targeted measurements, evaluates a deterministic
forward-chaining rule engine, and produces a signed
:class:`CalibrationProfile` -- a structured config diff to apply
atomically with snapshot+rollback semantics.

Public surface (post-T2.1, mission
``MISSION-voice-self-calibrating-system-2026-05-05.md`` Layer 2):

Schema (this commit):
    * :class:`CalibrationConfidence` -- HIGH | MEDIUM | LOW | EXPERIMENTAL
    * :class:`HardwareFingerprint` -- audio-stack-aware identity
    * :class:`MeasurementSnapshot` -- targeted diag artifacts subset
    * :class:`ProvenanceTrace` -- per-rule-firing audit log entry
    * :class:`CalibrationDecision` -- one config field change
    * :class:`CalibrationProfile` -- complete signed verdict

Provenance (this commit):
    * :class:`ProvenanceRecorder` -- engine-internal trace builder

Engine + Rules (T2.4 + T2.5.R10):
    * :class:`CalibrationEngine` -- forward-chaining rule engine
    * :class:`EngineMode` -- APPLY | DRY_RUN | EXPLAIN
    * :class:`CalibrationRule` -- rule Protocol
    * :class:`RuleContext` -- per-evaluation inputs
    * :class:`RuleEvaluation` -- per-firing output
    * :func:`iter_rules` -- discovery helper
    * :data:`RULE_SET_VERSION` -- bumped on rule set changes
    * Rule R10_mic_attenuated -- first rule (Linux mixer attenuation)

Future commits in v0.30.15:
    * T2.7 -- ``load_calibration_profile`` + ``save_calibration_profile``
      with Ed25519 signing (LENIENT mode default)
    * T2.8 -- :class:`CalibrationApplier` with atomic apply + rollback
    * T2.2 -- ``capture_fingerprint`` extending health/_fingerprint
    * T2.3 -- targeted measurer reusing the bash diag with --only flags
    * T2.5 -- rules/{R20..R50}_*.py (4 more issue-driven rules)
    * T2.9 -- ``sovyx doctor voice --calibrate`` CLI

Design contracts (ratified per mission spec):

* All decisions are deterministic and rule-based; NO ML / learned
  policy in v0.30.x or v0.31.0. Same inputs -> byte-identical output.
* ``EXPERIMENTAL``-confidence decisions are surfaced via ``--explain``
  but never auto-applied; promotion is a code change.
* Signing follows the ``_mixer_kb`` precedent: LENIENT mode in
  v0.30.15-17 (warns on missing/invalid signature, accepts), STRICT
  in v0.31.x.
* Per-mind isolation: profiles persisted to
  ``<data_dir>/<mind_id>/calibration.json``; no global mutation.
* Atomicity: pre-apply snapshot, apply, validate, persist; rollback
  on any sub-step failure (mirrors ``_linux_mixer_apply.py``).
"""

from __future__ import annotations

from sovyx.voice.calibration._applier import (
    ApplyError,
    ApplyResult,
    CalibrationApplier,
)
from sovyx.voice.calibration._fingerprint import capture_fingerprint
from sovyx.voice.calibration._measurer import capture_measurements
from sovyx.voice.calibration._persistence import (
    CalibrationProfileLoadError,
    load_calibration_profile,
    profile_path,
    save_calibration_profile,
)
from sovyx.voice.calibration._provenance import ProvenanceRecorder
from sovyx.voice.calibration._wizard_orchestrator import WizardOrchestrator
from sovyx.voice.calibration._wizard_progress import (
    ProgressEvent,
    WizardProgressTracker,
)
from sovyx.voice.calibration._wizard_state import WizardJobState, WizardStatus
from sovyx.voice.calibration.engine import CalibrationEngine, EngineMode
from sovyx.voice.calibration.rules import (
    RULE_SET_VERSION,
    CalibrationRule,
    RuleContext,
    RuleEvaluation,
    iter_rules,
)
from sovyx.voice.calibration.schema import (
    CALIBRATION_PROFILE_SCHEMA_VERSION,
    HARDWARE_FINGERPRINT_SCHEMA_VERSION,
    MEASUREMENT_SNAPSHOT_SCHEMA_VERSION,
    CalibrationConfidence,
    CalibrationDecision,
    CalibrationProfile,
    HardwareFingerprint,
    MeasurementSnapshot,
    ProvenanceTrace,
)

__all__ = [
    "CALIBRATION_PROFILE_SCHEMA_VERSION",
    "HARDWARE_FINGERPRINT_SCHEMA_VERSION",
    "MEASUREMENT_SNAPSHOT_SCHEMA_VERSION",
    "RULE_SET_VERSION",
    "ApplyError",
    "ApplyResult",
    "CalibrationApplier",
    "CalibrationConfidence",
    "CalibrationDecision",
    "CalibrationEngine",
    "CalibrationProfile",
    "CalibrationProfileLoadError",
    "CalibrationRule",
    "EngineMode",
    "HardwareFingerprint",
    "MeasurementSnapshot",
    "ProvenanceRecorder",
    "ProvenanceTrace",
    "RuleContext",
    "RuleEvaluation",
    "ProgressEvent",
    "WizardJobState",
    "WizardOrchestrator",
    "WizardProgressTracker",
    "WizardStatus",
    "capture_fingerprint",
    "capture_measurements",
    "iter_rules",
    "load_calibration_profile",
    "profile_path",
    "save_calibration_profile",
]
