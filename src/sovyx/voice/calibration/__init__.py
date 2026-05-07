"""Voice calibration: forensic-driven config decision pipeline.

This package owns the proactive auto-configuration surface that turns
the L1 :mod:`sovyx.voice.diagnostics` toolkit (forensic observation
only) into a self-calibrating pipeline that captures hardware
fingerprint + targeted measurements, evaluates a deterministic
forward-chaining rule engine, and produces a
:class:`CalibrationProfile` -- a structured config diff with snapshot +
LIFO-rollback semantics. The profile is **unsigned by default**
(LENIENT-loadable; STRICT-rejected); pass ``--signing-key <pem-path>``
to ``sovyx doctor voice --calibrate`` to sign with an Ed25519 private
key. STRICT default flip is gated on wizard-driven key generation,
planned for v0.32.0+ (see ``_signing.py`` for the canonical narrative).

## Architecture (v0.31.x)

### Schema

* :class:`CalibrationConfidence` -- HIGH | MEDIUM | LOW | EXPERIMENTAL
* :class:`HardwareFingerprint` -- audio-stack-aware host identity
* :class:`MeasurementSnapshot` -- targeted diag artifacts subset
* :class:`ProvenanceTrace` -- per-rule-firing audit log entry
* :class:`CalibrationDecision` -- one config field change
* :class:`CalibrationProfile` -- complete verdict + optional Ed25519 sig

### Engine + rules

* :class:`CalibrationEngine` -- forward-chaining rule engine
* :class:`EngineMode` -- APPLY | DRY_RUN | EXPLAIN
* :class:`CalibrationRule` -- rule Protocol
* :class:`RuleContext` -- per-evaluation inputs
* :class:`RuleEvaluation` -- per-firing output
* :func:`iter_rules` -- discovery helper
* :data:`RULE_SET_VERSION` -- bumped on rule set changes
* 10 rules ship: R10 (set, Linux mixer) + R20..R95 (advise; see
  ``docs/modules/voice-calibration.md`` rules registry for the
  full table + promotion roadmap).

### Persistence

* :func:`load_calibration_profile` -- LENIENT/STRICT-aware loader
  with explicit migration registry walk
  (:mod:`sovyx.voice.calibration._migrations`).
* :func:`save_calibration_profile` -- atomic write + multi-generation
  ``.bak.{1,2,3}`` rotation.
* :func:`rollback_calibration_profile` -- single-step rollback that
  walks the chain.
* :func:`inspect_migrated_profile_dict` -- operator inspection of the
  post-migration shape (wired as ``--inspect-migration`` CLI flag).

### Apply + rollback

* :class:`CalibrationApplier` -- async apply chain with LIFO rollback
  on any sub-decision failure (mirrors
  :mod:`sovyx.voice.health._linux_mixer_apply`).

### Wizard (Layer 3)

* :class:`WizardOrchestrator` -- state machine for dashboard-driven
  jobs (PENDING → PROBING → SLOW_PATH_DIAG → SLOW_PATH_CALIBRATE →
  SLOW_PATH_APPLY → DONE | FAILED | CANCELLED).
* :class:`WizardProgressTracker` -- JSONL tail for live dashboard
  streaming.
* :class:`WizardJobState` + :class:`WizardStatus` -- closed-enum state.

## Design contracts

* **Determinism:** all decisions are rule-based; NO ML / learned
  policy. Same inputs → byte-identical output.
* **EXPERIMENTAL gating:** EXPERIMENTAL-confidence decisions surface
  via ``--explain`` but never auto-apply; promotion is a code change.
* **Signing:** LENIENT default in v0.30.x..v0.31.x; STRICT opt-in via
  explicit ``mode=Mode.STRICT``. Default flip → v0.32.0+ once
  wizard-driven key generation lands (canonical narrative in
  :mod:`sovyx.voice.calibration._signing`).
* **Per-mind isolation:** profiles persisted to
  ``<data_dir>/<mind_id>/calibration.json``; no global mutation.
* **Atomicity:** pre-apply snapshot → apply → validate → persist;
  LIFO rollback on any sub-step failure.
* **Cross-reboot persistence:** delegated to
  ``packaging/systemd/sovyx-audio-mixer-persist.service`` +
  ``alsactl store``. The calibration.json is NOT auto-loaded at
  daemon startup — it is the audit + KB-cache feed only.
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
    CalibrationProfileRollbackError,
    inspect_migrated_profile_dict,
    load_calibration_profile,
    profile_backup_path,
    profile_path,
    rollback_calibration_profile,
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
    "CalibrationProfileRollbackError",
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
    "inspect_migrated_profile_dict",
    "iter_rules",
    "load_calibration_profile",
    "profile_backup_path",
    "profile_path",
    "rollback_calibration_profile",
    "save_calibration_profile",
]
