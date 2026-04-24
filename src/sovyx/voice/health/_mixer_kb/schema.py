"""Pydantic v2 schema for Mixer KB YAML profiles — L2.5 Phase F1.C.

This module mirrors the YAML schema documented in V2 Master Plan
Appendix 2. It is the single source of truth for what a KB profile
file may contain on disk; the
:class:`~sovyx.voice.health.contract.MixerKBProfile` frozen dataclass
is the in-memory shape every downstream consumer sees.

The two are kept structurally identical but intentionally separate:

* ``KBProfileModel`` (here) is YAML-boundary. Pydantic reports
  excellent error messages on malformed input, supports ``extra="forbid"``
  to catch typos, and can be evolved (``schema_version``) without
  breaking consumers.
* ``MixerKBProfile`` (contract.py) is the runtime type — frozen,
  slotted, already validated. Downstream code treats it as immutable
  data; no pydantic coercion ever re-runs after load.

:func:`model_to_profile` converts one to the other after a successful
``model_validate`` call. YAML values that encode tagged-union preset
values (``{fraction: 1.0}`` / ``{raw: 0}`` / ``{db: -5.0}``) are
dispatched to the right :class:`MixerPresetValue*` variant.

See V2 Master Plan Part E.3 + Appendix 2.
"""

from __future__ import annotations

from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from sovyx.voice.health.contract import (
    FactorySignature,
    MixerControlRole,
    MixerKBProfile,
    MixerPresetControl,
    MixerPresetSpec,
    MixerPresetValue,
    MixerPresetValueDb,
    MixerPresetValueFraction,
    MixerPresetValueRaw,
    ValidationGates,
    VerificationRecord,
)

# ── Sub-models ──────────────────────────────────────────────────────

_STRICT_CONFIG = ConfigDict(extra="forbid", frozen=True)
"""Pydantic config shared by every submodel.

``extra="forbid"`` makes typos fail loud (``"driver_familly"`` instead
of ``"driver_family"`` becomes a ValidationError, not a silent None).
``frozen=True`` matches the frozen-dataclass contract on the runtime
side — models round-tripped through this module cannot be mutated.
"""


class FactorySignatureModel(BaseModel):
    """YAML representation of :class:`FactorySignature`.

    At least one ``expected_*_range`` field must be non-null —
    enforced by :meth:`_at_least_one_range` and mirrored by
    :class:`FactorySignature.__post_init__`.
    """

    model_config = _STRICT_CONFIG

    expected_raw_range: tuple[int, int] | None = None
    expected_fraction_range: tuple[float, float] | None = None
    expected_db_range: tuple[float, float] | None = None

    @model_validator(mode="after")
    def _at_least_one_range(self) -> Self:
        if (
            self.expected_raw_range is None
            and self.expected_fraction_range is None
            and self.expected_db_range is None
        ):
            msg = (
                "factory_signature entry requires at least one of "
                "expected_raw_range / expected_fraction_range / "
                "expected_db_range"
            )
            raise ValueError(msg)
        return self


class PresetValueModel(BaseModel):
    """Tagged-union variant — exactly one of raw/fraction/db set.

    YAML users write ``{fraction: 1.0}`` or ``{raw: 0}`` or
    ``{db: -5.0}`` — pydantic parses into an object with the other
    two fields ``None``, then :meth:`_exactly_one` validates.
    """

    model_config = _STRICT_CONFIG

    raw: int | None = None
    fraction: float | None = Field(default=None, ge=0.0, le=1.0)
    db: float | None = None

    @model_validator(mode="after")
    def _exactly_one(self) -> Self:
        set_count = sum(1 for v in (self.raw, self.fraction, self.db) if v is not None)
        if set_count != 1:
            msg = (
                f"preset value must set exactly one of raw/fraction/db "
                f"(got {set_count}); use e.g. '{{fraction: 1.0}}'"
            )
            raise ValueError(msg)
        return self

    def to_preset_value(self) -> MixerPresetValue:
        """Translate to the frozen runtime variant."""
        if self.raw is not None:
            return MixerPresetValueRaw(raw=self.raw)
        if self.fraction is not None:
            return MixerPresetValueFraction(fraction=self.fraction)
        # self.db is not None guaranteed by _exactly_one.
        assert self.db is not None
        return MixerPresetValueDb(db=self.db)


class PresetControlModel(BaseModel):
    """YAML representation of :class:`MixerPresetControl`.

    ``role`` is validated against :class:`MixerControlRole` values;
    ``UNKNOWN`` is rejected at YAML boundary (same invariant as the
    runtime dataclass — KB authoring bugs where the profile targets
    an unresolvable role should fail load-time, not runtime).
    """

    model_config = _STRICT_CONFIG

    role: str
    value: PresetValueModel
    channel_policy: Literal["all", "left_right_equal"] = "all"

    @field_validator("role")
    @classmethod
    def _role_must_be_known(cls, v: str) -> str:
        try:
            role = MixerControlRole(v)
        except ValueError as exc:
            allowed = sorted(r.value for r in MixerControlRole)
            msg = f"role={v!r} not in {allowed}"
            raise ValueError(msg) from exc
        if role is MixerControlRole.UNKNOWN:
            msg = "role=UNKNOWN is not a valid preset target"
            raise ValueError(msg)
        return v

    def to_preset_control(self) -> MixerPresetControl:
        return MixerPresetControl(
            role=MixerControlRole(self.role),
            value=self.value.to_preset_value(),
            channel_policy=self.channel_policy,
        )


class RecommendedPresetModel(BaseModel):
    """YAML representation of :class:`MixerPresetSpec`."""

    model_config = _STRICT_CONFIG

    controls: tuple[PresetControlModel, ...] = Field(min_length=1)
    auto_mute_mode: Literal["disabled", "enabled", "leave"] = "leave"
    runtime_pm_target: Literal["on", "auto", "leave"] = "leave"

    def to_preset_spec(self) -> MixerPresetSpec:
        return MixerPresetSpec(
            controls=tuple(c.to_preset_control() for c in self.controls),
            auto_mute_mode=self.auto_mute_mode,
            runtime_pm_target=self.runtime_pm_target,
        )


class ValidationModel(BaseModel):
    """YAML representation of :class:`ValidationGates`."""

    model_config = _STRICT_CONFIG

    rms_dbfs_range: tuple[float, float]
    peak_dbfs_max: float
    snr_db_vocal_band_min: float = Field(ge=0.0)
    silero_prob_min: float = Field(ge=0.0, le=1.0)
    wake_word_stage2_prob_min: float = Field(ge=0.0, le=1.0)

    def to_validation_gates(self) -> ValidationGates:
        return ValidationGates(
            rms_dbfs_range=self.rms_dbfs_range,
            peak_dbfs_max=self.peak_dbfs_max,
            snr_db_vocal_band_min=self.snr_db_vocal_band_min,
            silero_prob_min=self.silero_prob_min,
            wake_word_stage2_prob_min=self.wake_word_stage2_prob_min,
        )


class VerificationModel(BaseModel):
    """YAML representation of :class:`VerificationRecord`."""

    model_config = _STRICT_CONFIG

    system_product: str = Field(min_length=1)
    codec_id: str = Field(min_length=1)
    kernel: str = Field(min_length=1)
    distro: str = Field(min_length=1)
    verified_at: str = Field(min_length=1)
    verified_by: str = Field(min_length=1)

    def to_verification_record(self) -> VerificationRecord:
        return VerificationRecord(
            system_product=self.system_product,
            codec_id=self.codec_id,
            kernel=self.kernel,
            distro=self.distro,
            verified_at=self.verified_at,
            verified_by=self.verified_by,
        )


class ChangelogEntryModel(BaseModel):
    """One row of the profile's ``changelog`` YAML block."""

    model_config = _STRICT_CONFIG

    version: int = Field(ge=1)
    date: str = Field(min_length=1)
    change: str = Field(min_length=1)


# ── Top-level profile model ─────────────────────────────────────────


class KBProfileModel(BaseModel):
    """Root schema — one-to-one with a ``profiles/*.yaml`` file.

    Fields mirror V2 Master Plan Appendix 2. ``schema_version`` MUST
    be ``1`` for this loader (future schema bumps raise at load time
    and fall back to generic cascade behaviour, per invariant P6 —
    fail honest, fail fast).

    Ed25519 signature (``signature`` field) is accepted but not
    enforced in F1 — the loader logs a DEBUG when present. F2 adds
    real Ed25519 verification against a public key shipped with the
    package.
    """

    model_config = _STRICT_CONFIG

    schema_version: int = Field(ge=1, le=1)  # bump range when schema v2 lands
    profile_id: str = Field(min_length=1, pattern=r"^[a-z0-9_]+$")
    profile_version: int = Field(ge=1)
    description: str = ""

    # Match criteria
    codec_id_glob: str = Field(min_length=1)
    driver_family: Literal["hda", "sof", "usb-audio", "bt"]
    system_vendor_glob: str | None = None
    system_product_glob: str | None = None
    baseboard_product_glob: str | None = None
    distro_family: str | None = None
    audio_stack: Literal["pipewire", "pulseaudio", "alsa"] | None = None
    kernel_major_minor_glob: str | None = None
    match_threshold: float = Field(default=0.6, ge=0.0, le=1.0)

    # Factory-regime signature — keys are MixerControlRole values.
    factory_regime: Literal["attenuation", "saturation", "mixed", "either"]
    factory_signature: dict[str, FactorySignatureModel] = Field(min_length=1)

    # Corrective preset
    recommended_preset: RecommendedPresetModel

    # Validation gates
    validation: ValidationModel

    # Provenance (required for PR merge)
    verified_on: tuple[VerificationModel, ...] = Field(min_length=1)
    contributed_by: str = Field(min_length=1)

    # Optional metadata
    changelog: tuple[ChangelogEntryModel, ...] = ()
    known_conflicts: tuple[str, ...] = ()
    known_caveats: tuple[str, ...] = ()

    # F1 stub — loader emits DEBUG when signature is present; F2 enforces.
    signature: str | None = None

    @field_validator("factory_signature")
    @classmethod
    def _factory_signature_roles_known(
        cls,
        v: dict[str, FactorySignatureModel],
    ) -> dict[str, FactorySignatureModel]:
        for role_name in v:
            try:
                role = MixerControlRole(role_name)
            except ValueError as exc:
                allowed = sorted(r.value for r in MixerControlRole)
                msg = (
                    f"factory_signature key {role_name!r} is not a valid "
                    f"MixerControlRole (allowed: {allowed})"
                )
                raise ValueError(msg) from exc
            if role is MixerControlRole.UNKNOWN:
                msg = "factory_signature must not contain MixerControlRole.UNKNOWN"
                raise ValueError(msg)
        return v

    def to_profile(self) -> MixerKBProfile:
        """Materialise as the frozen runtime :class:`MixerKBProfile`.

        All sub-models convert via their ``to_*`` methods. The result
        is immutable and ready to hand to the matcher + apply layers.
        """
        factory_signature: dict[MixerControlRole, FactorySignature] = {
            MixerControlRole(role_name): FactorySignature(
                expected_raw_range=sig.expected_raw_range,
                expected_fraction_range=sig.expected_fraction_range,
                expected_db_range=sig.expected_db_range,
            )
            for role_name, sig in self.factory_signature.items()
        }
        return MixerKBProfile(
            profile_id=self.profile_id,
            profile_version=self.profile_version,
            schema_version=self.schema_version,
            codec_id_glob=self.codec_id_glob,
            driver_family=self.driver_family,
            system_vendor_glob=self.system_vendor_glob,
            system_product_glob=self.system_product_glob,
            distro_family=self.distro_family,
            audio_stack=self.audio_stack,
            kernel_major_minor_glob=self.kernel_major_minor_glob,
            match_threshold=self.match_threshold,
            factory_regime=self.factory_regime,
            factory_signature=factory_signature,
            recommended_preset=self.recommended_preset.to_preset_spec(),
            validation_gates=self.validation.to_validation_gates(),
            verified_on=tuple(v.to_verification_record() for v in self.verified_on),
            contributed_by=self.contributed_by,
        )


__all__ = [
    "ChangelogEntryModel",
    "FactorySignatureModel",
    "KBProfileModel",
    "PresetControlModel",
    "PresetValueModel",
    "RecommendedPresetModel",
    "ValidationModel",
    "VerificationModel",
]
