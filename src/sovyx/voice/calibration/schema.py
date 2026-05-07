"""Frozen dataclasses for the voice calibration engine.

This module owns the typed schema that the calibration engine reads
(``HardwareFingerprint``, ``MeasurementSnapshot``) and writes
(``CalibrationDecision``, ``ProvenanceTrace``, ``CalibrationProfile``).
All dataclasses are ``frozen=True, slots=True`` so they can be safely
shared across rules without defensive copies + so attribute typos surface
as :class:`AttributeError` at construction.

The schema is the contract between:

* The fingerprint extractor (T2.2 -- ``_fingerprint.py``)
* The targeted measurer (T2.3 -- ``_measurer.py``)
* The rule engine (T2.4 -- ``engine.py`` + ``rules/``)
* The applier (T2.8 -- ``_applier.py``)
* The persistence layer (T2.7 -- ``_persistence.py`` + Ed25519 signing)

It is also the foundation for the L4 KB feedback loop (v0.32+): the
``HardwareFingerprint.fingerprint_hash`` property produces a stable
SHA256 used as the community-KB lookup key, and the
``CalibrationProfile.signature`` field carries the Ed25519 signature
that proves provenance once the operator opts in to fleet sharing.

History: introduced in v0.30.15 as T2.1 of mission
``MISSION-voice-self-calibrating-system-2026-05-05.md`` Layer 2.
The schema_version constants gate forward/backward compatibility;
loaders MUST raise on unknown ``schema_version`` rather than silently
falling back, per anti-pattern #35 (sentinel defaults).
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Mapping

# Bumped on breaking schema changes. Loaders raise on mismatch; never
# silently coerce. Migrations are explicit.
HARDWARE_FINGERPRINT_SCHEMA_VERSION = 1
MEASUREMENT_SNAPSHOT_SCHEMA_VERSION = 1
CALIBRATION_PROFILE_SCHEMA_VERSION = 1


class CalibrationConfidence(StrEnum):
    """Confidence band assigned by a rule to its produced decisions.

    ``EXPERIMENTAL`` decisions are NOT auto-applied by the applier --
    they surface in the dry-run/explain output for operator inspection
    only, until the rule has accumulated enough soak-time evidence to
    be promoted to LOW or higher. The promotion is a code change, not
    a runtime configuration knob, so ML-style learned-policy gradient
    drift is impossible by construction.

    The applier filter is in :attr:`CalibrationProfile.applicable_decisions`.
    """

    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"
    EXPERIMENTAL = "experimental"


@dataclass(frozen=True, slots=True)
class HardwareFingerprint:
    """Stable identity over hardware + audio-stack + interceptor state.

    Captured by :mod:`sovyx.voice.calibration._fingerprint` (T2.2),
    extending the existing :mod:`sovyx.voice.health._fingerprint` with
    the audio-stack-aware fields the calibration engine needs to make
    decisions (codec_id, driver_family, audio_stack version, capture
    topology, APO/HAL/destructive-module presence).

    The :meth:`fingerprint_hash` property produces a deterministic
    SHA256 over the identity-bearing fields (excluding ``schema_version``
    and ``captured_at_utc``), used as the L4 community-KB lookup key.
    """

    schema_version: int
    captured_at_utc: str  # ISO-8601 UTC

    # System-wide
    distro_id: str  # "linuxmint" | "ubuntu" | "fedora" | "arch" | ...
    distro_id_like: str  # "debian" | "rhel" | "" (no like-family)
    kernel_release: str  # "6.8.0-50-generic"
    kernel_major_minor: str  # "6.8"

    # Compute
    cpu_model: str
    cpu_cores: int
    ram_mb: int
    has_gpu: bool
    gpu_vram_mb: int

    # Audio software stack
    audio_stack: str  # "pipewire" | "pulseaudio" | "alsa-only"
    pipewire_version: str | None
    pulseaudio_version: str | None
    alsa_lib_version: str

    # Audio hardware identity
    codec_id: str  # "10ec:0257" (PCI vendor:device of the audio codec)
    driver_family: str  # "hda" | "sof" | "usb-audio" | "bt"
    system_vendor: str  # "Sony"
    system_product: str  # "VJFE69F11X-B0221H"

    # Capture topology
    capture_card_count: int
    capture_devices: tuple[str, ...]  # PortAudio device names, sorted for determinism

    # Interceptor presence (per-OS)
    apo_active: bool  # Win: VocaEffectPack / Voice Clarity
    apo_name: str | None
    hal_interceptors: tuple[str, ...]  # macOS: Krisp, BlackHole, ...
    pulse_modules_destructive: tuple[str, ...]  # Linux: echo-cancel, rnnoise, webrtc

    @property
    def fingerprint_hash(self) -> str:
        """Deterministic SHA256 over identity-bearing fields, hex digest.

        Excludes ``schema_version`` (versioning metadata) and
        ``captured_at_utc`` (timestamp drift). Tuple fields are
        canonicalized to sorted lists before hashing so capture-order
        changes don't shift the hash. The hash is the L4 community-KB
        lookup key; same hardware -> same hash -> same applied profile.
        """
        canonical: dict[str, Any] = {
            "distro_id": self.distro_id,
            "distro_id_like": self.distro_id_like,
            "kernel_release": self.kernel_release,
            "kernel_major_minor": self.kernel_major_minor,
            "cpu_model": self.cpu_model,
            "cpu_cores": self.cpu_cores,
            "ram_mb": self.ram_mb,
            "has_gpu": self.has_gpu,
            "gpu_vram_mb": self.gpu_vram_mb,
            "audio_stack": self.audio_stack,
            "pipewire_version": self.pipewire_version,
            "pulseaudio_version": self.pulseaudio_version,
            "alsa_lib_version": self.alsa_lib_version,
            "codec_id": self.codec_id,
            "driver_family": self.driver_family,
            "system_vendor": self.system_vendor,
            "system_product": self.system_product,
            "capture_card_count": self.capture_card_count,
            "capture_devices": sorted(self.capture_devices),
            "apo_active": self.apo_active,
            "apo_name": self.apo_name,
            "hal_interceptors": sorted(self.hal_interceptors),
            "pulse_modules_destructive": sorted(self.pulse_modules_destructive),
        }
        payload = json.dumps(canonical, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class MeasurementSnapshot:
    """Subset of diag artifacts the calibration rules consume.

    Captured by :mod:`sovyx.voice.calibration._measurer` (T2.3) by
    running a targeted ~30s subset of the full bash diag (no Guardian,
    no full W/K captures -- only the modules that produce the fields
    listed below).

    All numeric fields are pre-quantized at construction time (RMS to
    0.1 dB, latency to 0.1 ms, percentages to integer 0-100) so two
    runs with sub-noise variation produce byte-identical snapshots --
    a determinism gate the engine relies on for repeatability.
    """

    schema_version: int
    captured_at_utc: str
    duration_s: float

    # RMS / VAD signal-energy metrics
    rms_dbfs_per_capture: tuple[float, ...]  # dBFS, per-capture
    vad_speech_probability_max: float  # [0.0, 1.0], peak across captures
    vad_speech_probability_p99: float  # [0.0, 1.0], 99th percentile
    noise_floor_dbfs_estimate: float  # dBFS, estimated noise floor

    # Latency / timing
    capture_callback_p99_ms: float
    capture_jitter_ms: float
    portaudio_latency_advertised_ms: float

    # Mixer state (Linux; None on Windows / macOS)
    mixer_card_index: int | None
    mixer_capture_pct: int | None  # 0-100
    mixer_boost_pct: int | None  # 0-100
    mixer_internal_mic_boost_pct: int | None  # 0-100
    mixer_attenuation_regime: str | None  # "saturated" | "attenuated" | "healthy"

    # Echo / AEC pre-check
    echo_correlation_db: float | None  # Tx->Rx coupling estimate, dB

    # Triage cross-correlation
    triage_winner_hid: str | None  # "H1".."H10"
    triage_winner_confidence: float | None  # [0.0, 1.0]


@dataclass(frozen=True, slots=True)
class ProvenanceTrace:
    """One audit-log entry recording a rule firing during engine execution.

    Built incrementally by :class:`sovyx.voice.calibration._provenance.ProvenanceRecorder`
    inside the engine and frozen into a tuple at the boundary of
    :attr:`CalibrationProfile.provenance`. Operator inspection happens
    via ``sovyx doctor voice --calibrate --explain`` (T2.9), which
    walks the trace + renders matched conditions vs produced decisions.
    """

    rule_id: str  # "R10_mic_attenuated"
    rule_version: int  # Bumped when the rule's logic changes (cache invalidation)
    fired_at_utc: str
    matched_conditions: tuple[str, ...]
    produced_decisions: tuple[str, ...]
    confidence: CalibrationConfidence


@dataclass(frozen=True, slots=True)
class CalibrationDecision:
    """One config field change produced by a rule.

    Multiple decisions compose a :class:`CalibrationProfile`. The
    applier (T2.8) iterates ``applicable_decisions`` (filtered to
    ``operation == "set"`` AND ``confidence != EXPERIMENTAL``) and
    applies each atomically with snapshot+rollback semantics.

    ``operation``:

    * ``"set"`` -- mutates the target field. The applier persists.
    * ``"advise"`` -- recorded as an operator-actionable hint
      (e.g. "run sovyx doctor voice --fix --yes"). The applier does
      NOT mutate; CLI surfaces the advice in green.
    * ``"preserve"`` -- explicit no-op recording the rule's intent
      to keep the current value. Useful for explainability.
    """

    # Pydantic dotted path: e.g. "mind.voice_input_device_name",
    # "tuning.voice.capture_queue_maxsize", or "advice.action".
    target: str
    target_class: str  # "MindConfig" | "MindConfig.voice" | "TuningAdvice"
    operation: str  # "set" | "advise" | "preserve"
    value: str | int | float | bool | None
    rationale: str  # Operator-facing explanation (rendered by --explain)
    rule_id: str
    rule_version: int
    confidence: CalibrationConfidence


@dataclass(frozen=True, slots=True)
class CalibrationProfile:
    """Structured output of one calibration run -- signed, persisted per-mind.

    Persisted to ``<data_dir>/<mind_id>/calibration.json`` with an
    optional Ed25519 signature over the canonical payload (signing
    primitives re-exported from
    :mod:`sovyx.voice.health._mixer_kb._signing`).

    Persistence semantics: the JSON is the **audit + KB-cache feed**
    artifact. It is consumed only by the operator-facing inspection
    paths (``sovyx doctor voice --calibrate --show / --explain /
    --inspect-migration``) and the wizard's FAST_PATH replay
    (``_kb_cache.lookup_profile``). Cross-reboot persistence of
    mixer state is delegated to ``alsactl store`` via the bundled
    systemd unit; the calibration.json is NOT auto-loaded at daemon
    startup.

    The ``signature`` field is ``None`` when the profile has not been
    signed at write time. The default loader mode is LENIENT
    (warns + accepts). STRICT (rejects unsigned) is OPT-IN as of
    v0.31.x; the default flip is gated on operator-driven Ed25519
    key generation landing in the wizard (planned v0.32.0+; see
    :mod:`sovyx.voice.calibration._signing` module docstring for the
    canonical narrative).
    """

    schema_version: int
    profile_id: str  # UUID4 generated at engine evaluation time
    mind_id: str
    fingerprint: HardwareFingerprint
    measurements: MeasurementSnapshot
    decisions: tuple[CalibrationDecision, ...]
    provenance: tuple[ProvenanceTrace, ...]
    generated_by_engine_version: str  # "0.30.15"
    generated_by_rule_set_version: int  # bumped on rule additions/removals
    generated_at_utc: str
    signature: str | None  # Ed25519 signature, base64-encoded; None when unsigned

    @property
    def applicable_decisions(self) -> tuple[CalibrationDecision, ...]:
        """Decisions that should auto-apply: ``operation == "set"`` + non-experimental.

        EXPERIMENTAL-confidence decisions are surfaced via
        ``--explain`` and ``--dry-run`` but never auto-applied,
        regardless of operator override; promotion is a code change.

        ``advise`` and ``preserve`` operations are filtered out -- they
        are recorded for explainability + operator hint surfacing but
        do not mutate state.
        """
        return tuple(
            d
            for d in self.decisions
            if d.operation == "set" and d.confidence != CalibrationConfidence.EXPERIMENTAL
        )

    def canonical_signing_payload(self) -> Mapping[str, Any]:
        """Return the canonical (signature-stripped) payload for Ed25519 signing.

        The signing payload is the profile dict with ``signature``
        removed and re-serialized with sorted keys. Used by
        :mod:`sovyx.voice.calibration._persistence` (T2.7) to compute
        the bytes-to-sign, and by the verifier on load.
        """
        return {
            "schema_version": self.schema_version,
            "profile_id": self.profile_id,
            "mind_id": self.mind_id,
            "fingerprint_hash": self.fingerprint.fingerprint_hash,
            "decisions_count": len(self.decisions),
            "generated_by_engine_version": self.generated_by_engine_version,
            "generated_by_rule_set_version": self.generated_by_rule_set_version,
            "generated_at_utc": self.generated_at_utc,
        }
