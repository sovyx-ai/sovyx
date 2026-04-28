"""L2.5 mixer-sanity + eligibility dataclasses.

Split from the legacy ``contract.py`` (CLAUDE.md anti-pattern #16
hygiene) — see ``MISSION-voice-godfile-splits-v0.24.1.md`` Part 3 / T01.

Owns the bypass-eligibility report (:class:`Eligibility`), the mixer-
state snapshot stack (:class:`MixerControlSnapshot`,
:class:`MixerCardSnapshot`, :class:`MixerApplySnapshot`), the
mixer-sanity preset / KB-profile chain
(:class:`MixerControlRole`, :class:`MixerSanityDecision`,
:class:`MixerPresetValueRaw` / :class:`MixerPresetValueFraction` /
:class:`MixerPresetValueDb`, :class:`MixerPresetValue`,
:class:`MixerPresetControl`, :class:`MixerPresetSpec`,
:class:`ValidationGates`, :class:`FactorySignature`,
:class:`VerificationRecord`, :class:`MixerKBProfile`,
:class:`MixerValidationMetrics`, :class:`MixerSanityResult`), and the
hardware-context detection record (:class:`HardwareContext`).

These types implement the bidirectional mixer-sanity layer spec'd in
ADR-voice-mixer-sanity-l2.5-bidirectional and the V2 Master Plan Part D.
They sit between ComboStore's fast-path and the platform cascade walk
so attenuation (Incident B) and saturation (Incident A) regimes are
handled symmetrically, KB-driven, and with full rollback.

Convention (mirrors the rest of this module):
   * User/config/YAML-boundary inputs validate in ``__post_init__``
     (``MixerKBProfile``, ``MixerPresetSpec``, ``ValidationGates``,
     ``FactorySignature``, ``VerificationRecord``,
     ``MixerPresetControl``, ``MixerPresetValueFraction``).
   * Pure computed outputs stay validation-free — the producer (the
     L2.5 orchestrator) is the sole author and tests are the safety net
     (``MixerSanityResult``, ``MixerValidationMetrics``,
     ``MixerPresetValueRaw``, ``MixerPresetValueDb``).

All public names re-exported from :mod:`sovyx.voice.health.contract`.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Literal, TypeAlias

if TYPE_CHECKING:
    from collections.abc import Mapping

    from sovyx.voice.health.contract._diagnosis import Diagnosis
    from sovyx.voice.health.contract._probe_result import RemediationHint


__all__ = [
    "Eligibility",
    "FactorySignature",
    "HardwareContext",
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
    "ValidationGates",
    "VerificationRecord",
]


# ── Eligibility (bypass strategy report) ────────────────────────────────


@dataclass(frozen=True, slots=True)
class Eligibility:
    """Feasibility report from :meth:`PlatformBypassStrategy.probe_eligibility`.

    A strategy whose eligibility check returns ``applicable=False`` is
    skipped by the coordinator without counting toward
    ``bypass_strategy_max_attempts`` — a non-applicable strategy is not
    an attempt.

    Args:
        applicable: ``True`` iff the strategy's preconditions are met
            on the current endpoint + OS + tuning configuration.
        reason: Machine-readable reason token. Stable across minor
            versions so dashboards can key on it. Examples:
            ``"exclusive_mode_disabled_by_policy"``,
            ``"not_wasapi_endpoint"``, ``"alsa_hw_node_unavailable"``,
            ``"not_implemented_phase_3_pipewire"``.
        estimated_cost_ms: Informational forecast of how long the
            subsequent ``apply`` is expected to take. Used by the
            coordinator only for telemetry, never for sequencing.
    """

    applicable: bool
    reason: str = ""
    estimated_cost_ms: int = 0


# ── Linux ALSA mixer snapshots (Phase 3) ────────────────────────────────
#
# These types describe the *state* of the ALSA analog mixer chain on one
# card. They are produced by :mod:`sovyx.voice.health._linux_mixer_probe`
# (read-only), consumed by :mod:`sovyx.voice.health._linux_mixer_apply`
# (writes + snapshot-driven rollback), and serialised onto the dashboard
# via ``GET /api/voice/linux-mixer-diagnostics``.
#
# See ``docs-internal/plans/linux-alsa-mixer-saturation-fix.md`` §2.3.2
# for the derivation of each field + the classification rules.


@dataclass(frozen=True, slots=True)
class MixerControlSnapshot:
    """State of one ``amixer`` simple control on a single ALSA card.

    Produced by :func:`sovyx.voice.health._linux_mixer_probe.enumerate_alsa_mixer_snapshots`.
    Immutable — apply/revert builds :class:`MixerApplySnapshot` from these,
    never mutates them in place.

    Args:
        name: ``amixer`` simple-control name (``"Capture"``,
            ``"Internal Mic Boost"``, …). Case-sensitive; used verbatim
            in the subsequent ``amixer -c N set`` call.
        min_raw: Lower bound of the control's raw integer range (usually
            ``0`` but some codecs expose negative floors).
        max_raw: Upper bound of the raw integer range. Reading
            ``current_raw == max_raw`` on a boost-class control is the
            canonical saturation signal.
        current_raw: Current raw value. The probe reads the Front-Left
            channel; controls with a left/right asymmetry the probe
            picks up are surfaced via :attr:`asymmetric`.
        current_db: Current gain in dB, when the control exposes a dB
            mapping. ``None`` for enum controls or controls without a
            dB tag.
        max_db: DB value at :attr:`max_raw`, for aggregated-boost
            accounting. ``None`` when :attr:`current_db` is ``None``.
        is_boost_control: ``True`` when the control name matches one of
            the boost / capture patterns recognised by the probe — the
            set of controls that saturate ADCs when driven to max.
        saturation_risk: ``True`` iff :attr:`is_boost_control` AND
            ``current_raw / max_raw > linux_mixer_saturation_ratio_ceiling``.
            The coordinator keys off this flag.
        asymmetric: ``True`` when the Front-Left and Front-Right
            readings differ. Surfaced for diagnostics only; apply sets
            both channels to the same target value.
    """

    name: str
    min_raw: int
    max_raw: int
    current_raw: int
    current_db: float | None
    max_db: float | None
    is_boost_control: bool
    saturation_risk: bool
    asymmetric: bool = False


@dataclass(frozen=True, slots=True)
class MixerCardSnapshot:
    """State of all gain-bearing controls on a single ALSA card.

    Args:
        card_index: ``/proc/asound/cards`` index. Used directly in
            ``amixer -c <index>`` subsequent calls.
        card_id: Short identifier from ``/proc/asound/cards`` (e.g.
            ``"Generic_1"``).
        card_longname: Full human-readable name (e.g.
            ``"HDA Intel (Family 17h/19h HD Audio Controller)"``). Used
            for endpoint matching against
            :attr:`BypassContext.endpoint_friendly_name`.
        controls: All controls observed on this card, in ``amixer``
            enumeration order.
        aggregated_boost_db: Sum of :attr:`MixerControlSnapshot.current_db`
            across every boost-class control that exposes a dB mapping.
            ``0.0`` when no boost control reports a dB value.
        saturation_warning: ``True`` iff at least one control has
            :attr:`MixerControlSnapshot.saturation_risk` OR
            :attr:`aggregated_boost_db` exceeds
            ``linux_mixer_aggregated_boost_db_ceiling``.
    """

    card_index: int
    card_id: str
    card_longname: str
    controls: tuple[MixerControlSnapshot, ...]
    aggregated_boost_db: float
    saturation_warning: bool


@dataclass(frozen=True, slots=True)
class MixerApplySnapshot:
    """Record of a completed ``amixer`` mutation — drives revert.

    Produced by :func:`sovyx.voice.health._linux_mixer_apply.apply_mixer_reset`
    on success. Consumed by :func:`restore_mixer_snapshot` to roll back
    the strategy's mutation on coordinator teardown or next-strategy
    advancement.

    Args:
        card_index: Card whose controls were mutated.
        reverted_controls: ``(name, pre_apply_raw_value)`` pairs in the
            order they were mutated. Revert walks this list in reverse
            so the last mutation is undone first.
        applied_controls: ``(name, post_apply_raw_value)`` pairs for
            telemetry + the dashboard's confirmation UI. Never used for
            revert — the revert-from-snapshot contract is "restore the
            pre-apply state", not "set to a different target".
        reverted_enum_controls: ``(name, pre_apply_enum_label)`` pairs
            for enum-typed controls mutated during apply (chiefly
            HDA ``Auto-Mute Mode``). Default ``()`` so callers that
            only touch numeric controls need no new field. Paranoid-QA
            R3 CRIT-1: added to fill the half-heal WAL coverage gap
            — without it, a mid-apply crash between the numeric
            mutations and the auto-mute write would be recovered with
            numerics restored but Auto-Mute stuck in the applied
            ``Disabled``/``Enabled`` state. Revert walks this list in
            reverse AFTER (or before, per the LIFO-order fix)
            ``reverted_controls``.
    """

    card_index: int
    reverted_controls: tuple[tuple[str, int], ...]
    applied_controls: tuple[tuple[str, int], ...]
    reverted_enum_controls: tuple[tuple[str, str], ...] = ()


# ── L2.5 mixer sanity (Phase F1) ────────────────────────────────────────


class MixerControlRole(StrEnum):
    """Canonical role of an ALSA ``amixer`` simple control.

    Role-based discovery replaces driver-family-specific string matching
    so HDA, SOF, USB-audio, and BT codecs are handled by the same KB
    profile schema. The per-driver resolver (``_mixer_roles.py``, Phase
    F1.B) maps raw control names to these roles; the KB (``_mixer_kb/``,
    Phase F1.C) keys presets by role; ``_linux_mixer_apply.apply_mixer_preset``
    (Phase F1.D) translates (role, value) back to (name, raw).

    Members are spec'd in V2 Master Plan Appendix 1. ``UNKNOWN`` is the
    sentinel for controls observed by the probe but not role-mapped —
    they are surfaced for telemetry and ignored by preset apply.
    """

    # Capture path — gain + source
    CAPTURE_MASTER = "capture_master"
    INTERNAL_MIC_BOOST = "internal_mic_boost"
    PREAMP_BOOST = "preamp_boost"
    DIGITAL_CAPTURE = "digital_capture"
    INPUT_SOURCE_SELECTOR = "input_source_selector"
    # Muting
    AUTO_MUTE = "auto_mute"
    CAPTURE_SWITCH = "capture_switch"
    # SOF topology specifics (Intel Tiger Lake / Meteor Lake / Lunar Lake)
    PGA_MASTER = "pga_master"
    PGA_DMIC = "pga_dmic"
    # USB audio class
    USB_MIC_MASTER = "usb_mic_master"
    # Bluetooth
    BT_HFP_GAIN = "bt_hfp_gain"
    # Sentinel
    UNKNOWN = "unknown"


class MixerSanityDecision(StrEnum):
    """Terminal verdict of one :func:`check_and_maybe_heal` invocation.

    The cascade keys off the decision to decide whether to skip the
    platform walk (``HEALED``) or proceed (``SKIPPED_*``, ``DEFERRED_*``,
    ``ROLLED_BACK``, ``ERROR``). See V2 Master Plan Part D.1.2.
    """

    HEALED = "healed"
    """Preset applied, post-apply validation gates passed, persisted via
    ``alsactl store``. Cascade records the winning combo and returns
    without walking the platform list.
    """

    ROLLED_BACK = "rolled_back"
    """Preset applied but at least one validation gate failed. Original
    mixer state restored via :func:`restore_mixer_snapshot`. Cascade
    proceeds to the platform walk; dashboard surfaces a warning.
    """

    SKIPPED_HEALTHY = "skipped_healthy"
    """Mixer probe reports state in the healthy range (no KB match
    required). L2.5 is a no-op; cascade proceeds normally.
    """

    SKIPPED_CUSTOMIZED = "skipped_customized"
    """User-customization heuristic score exceeded the skip threshold
    (``> linux_mixer_user_customization_threshold_skip``). Invariant I4
    — user customization is sacred. Dashboard tip emitted.
    """

    DEFERRED_NO_KB = "deferred_no_kb"
    """State outside healthy range, no KB profile matched above
    ``match_threshold``. Cascade proceeds; hardware flagged for KB
    growth prioritisation.
    """

    DEFERRED_AMBIGUOUS = "deferred_ambiguous"
    """Two or more KB profiles matched with scores within the
    ambiguity window (0.05 by default). Dashboard offers a choice card;
    L2.5 defers this boot.
    """

    DEFERRED_PLATFORM = "deferred_platform"
    """Non-Linux platform (F1 scope is Linux-only). Windows + macOS
    fall through to their existing cascade paths.
    """

    ERROR = "error"
    """Probe, apply, or validation raised unexpectedly. Rollback
    attempted if a mixer mutation was in flight. Error reason token
    captured in :attr:`MixerSanityResult.error`.
    """


# Tagged-union members for preset values. Each variant is a frozen,
# slotted dataclass so pattern-matching in ``apply_mixer_preset`` stays
# explicit (``match value: case MixerPresetValueFraction(...)``).


@dataclass(frozen=True, slots=True)
class MixerPresetValueRaw:
    """Preset value expressed as the codec's native raw integer.

    Use when the KB author has validated the raw value against the
    specific codec (usually via HIL ``amixer cget``). The apply layer
    clamps to ``[min_raw, max_raw]`` of the live control.
    """

    raw: int


@dataclass(frozen=True, slots=True)
class MixerPresetValueFraction:
    """Preset value expressed as a fraction of the control's raw range.

    Portable across codecs with different raw ranges — ``fraction=1.0``
    resolves to ``max_raw``, ``0.0`` to ``min_raw``. Validated at
    construction to the closed unit interval ``[0, 1]``.
    """

    fraction: float

    def __post_init__(self) -> None:
        if not 0.0 <= self.fraction <= 1.0:
            msg = (
                f"fraction={self.fraction!r} must be in [0.0, 1.0]; "
                "use MixerPresetValueRaw for out-of-range values"
            )
            raise ValueError(msg)


@dataclass(frozen=True, slots=True)
class MixerPresetValueDb:
    """Preset value expressed in dB, translated to raw via the control's
    ``amixer``-reported dB mapping.

    Use when the KB author has a dB target from a forensic capture
    (``Capture: 0.0 dB``). The apply layer looks up the closest raw
    value whose ``dB`` column matches and clamps to the control's range.
    """

    db: float


MixerPresetValue: TypeAlias = MixerPresetValueRaw | MixerPresetValueFraction | MixerPresetValueDb
"""Tagged-union type for the value slot of a :class:`MixerPresetControl`.
Apply layer pattern-matches on the concrete variant to translate to raw.
"""


_CHANNEL_POLICIES: frozenset[str] = frozenset({"all", "left_right_equal"})
_AUTO_MUTE_MODES: frozenset[str] = frozenset({"disabled", "enabled", "leave"})
_RUNTIME_PM_TARGETS: frozenset[str] = frozenset({"on", "auto", "leave"})
_DRIVER_FAMILIES: frozenset[str] = frozenset({"hda", "sof", "usb-audio", "bt"})
_FACTORY_REGIMES: frozenset[str] = frozenset(
    {"attenuation", "saturation", "mixed", "either"},
)
_AUDIO_STACKS: frozenset[str] = frozenset({"pipewire", "pulseaudio", "alsa"})
_SANITY_REGIMES: frozenset[str] = frozenset(
    {"saturation", "attenuation", "mixed", "healthy", "unknown"},
)


@dataclass(frozen=True, slots=True)
class MixerPresetControl:
    """One control line in a :class:`MixerPresetSpec`.

    Args:
        role: Canonical role the preset targets. ``UNKNOWN`` is rejected —
            an ``UNKNOWN`` role in a preset is always a KB-authoring bug
            (either the YAML references a role that doesn't exist or the
            resolver table lacks the mapping the preset assumes).
        value: Desired value (raw/fraction/db variant).
        channel_policy: How to drive stereo controls — ``"all"`` sets
            every channel to the same target; ``"left_right_equal"``
            enforces L/R symmetry by asserting they were equal before
            apply (tests raise if asymmetric to protect user intent).

    Raises:
        ValueError: On ``role=UNKNOWN`` or unknown ``channel_policy``.
    """

    role: MixerControlRole
    value: MixerPresetValue
    channel_policy: Literal["all", "left_right_equal"] = "all"

    def __post_init__(self) -> None:
        if self.role is MixerControlRole.UNKNOWN:
            msg = (
                "role=MixerControlRole.UNKNOWN is not a valid preset target; "
                "resolve the control via MixerControlRoleResolver before "
                "building a MixerPresetControl"
            )
            raise ValueError(msg)
        if self.channel_policy not in _CHANNEL_POLICIES:
            msg = f"channel_policy={self.channel_policy!r} not in {sorted(_CHANNEL_POLICIES)}"
            raise ValueError(msg)


@dataclass(frozen=True, slots=True)
class MixerPresetSpec:
    """Immutable corrective preset, sourced from a KB profile.

    Translated to actual ``amixer sset`` calls by
    :func:`~sovyx.voice.health._linux_mixer_apply.apply_mixer_preset`
    (Phase F1.D). Pure data — no side effects at construction.

    Args:
        controls: Non-empty sequence of per-role value assignments. Empty
            presets are rejected because they would produce no apply
            record and thus no rollback capability — a silent no-op is
            worse than an explicit KB authoring error.
        auto_mute_mode: Whether to toggle HDA Auto-Mute Mode. ``"leave"``
            (default) preserves the current setting.
        runtime_pm_target: Whether to request codec runtime_pm change.
            Applied via the systemd oneshot (Phase F1.G), never from the
            daemon directly (invariant I7).

    Raises:
        ValueError: On empty ``controls`` or unknown literal values.
    """

    controls: tuple[MixerPresetControl, ...]
    auto_mute_mode: Literal["disabled", "enabled", "leave"] = "leave"
    runtime_pm_target: Literal["on", "auto", "leave"] = "leave"

    def __post_init__(self) -> None:
        if not self.controls:
            msg = "MixerPresetSpec.controls must be non-empty"
            raise ValueError(msg)
        if self.auto_mute_mode not in _AUTO_MUTE_MODES:
            msg = f"auto_mute_mode={self.auto_mute_mode!r} not in {sorted(_AUTO_MUTE_MODES)}"
            raise ValueError(msg)
        if self.runtime_pm_target not in _RUNTIME_PM_TARGETS:
            msg = (
                f"runtime_pm_target={self.runtime_pm_target!r} not in "
                f"{sorted(_RUNTIME_PM_TARGETS)}"
            )
            raise ValueError(msg)
        # Each role may appear at most once — a preset with two entries
        # for the same role is a KB bug; the apply layer would silently
        # keep only the last write.
        seen: set[MixerControlRole] = set()
        for ctl in self.controls:
            if ctl.role in seen:
                msg = f"role={ctl.role.value!r} appears more than once in MixerPresetSpec.controls"
                raise ValueError(msg)
            seen.add(ctl.role)


@dataclass(frozen=True, slots=True)
class ValidationGates:
    """Post-apply validation thresholds for one KB profile.

    Every gate must pass for the apply to be considered successful;
    any failure triggers rollback (invariant I5). Sourced from a KB
    YAML ``validation:`` block; pure data — gate evaluation happens in
    :func:`~sovyx.voice.health._mixer_sanity._validate_post_apply`.

    Args:
        rms_dbfs_range: Closed interval for RMS of the validation
            window, dBFS. Both bounds must be ``<= 0`` (dBFS is
            non-positive) and ``lo <= hi``.
        peak_dbfs_max: Upper bound on peak dBFS (``<= 0``). Peak above
            this indicates clipping after the preset.
        snr_db_vocal_band_min: Minimum SNR in the 300–3400 Hz vocal
            band, dB. Non-negative.
        silero_prob_min: Minimum ``max_prob`` Silero VAD must emit over
            the validation window. Closed unit interval.
        wake_word_stage2_prob_min: Minimum OpenWakeWord stage-2 probe
            probability. Closed unit interval.

    Raises:
        ValueError: On out-of-range values or inverted intervals.
    """

    rms_dbfs_range: tuple[float, float]
    peak_dbfs_max: float
    snr_db_vocal_band_min: float
    silero_prob_min: float
    wake_word_stage2_prob_min: float

    def __post_init__(self) -> None:
        lo, hi = self.rms_dbfs_range
        if lo > hi:
            msg = (
                f"rms_dbfs_range={self.rms_dbfs_range!r} is inverted (lo > hi); expected lo <= hi"
            )
            raise ValueError(msg)
        if lo > 0.0 or hi > 0.0:
            msg = f"rms_dbfs_range={self.rms_dbfs_range!r} must be non-positive (dBFS <= 0)"
            raise ValueError(msg)
        if self.peak_dbfs_max > 0.0:
            msg = f"peak_dbfs_max={self.peak_dbfs_max!r} must be non-positive (dBFS <= 0)"
            raise ValueError(msg)
        if self.snr_db_vocal_band_min < 0.0:
            msg = f"snr_db_vocal_band_min={self.snr_db_vocal_band_min!r} must be non-negative"
            raise ValueError(msg)
        if not 0.0 <= self.silero_prob_min <= 1.0:
            msg = f"silero_prob_min={self.silero_prob_min!r} must be in [0.0, 1.0]"
            raise ValueError(msg)
        if not 0.0 <= self.wake_word_stage2_prob_min <= 1.0:
            msg = (
                f"wake_word_stage2_prob_min={self.wake_word_stage2_prob_min!r} "
                "must be in [0.0, 1.0]"
            )
            raise ValueError(msg)


@dataclass(frozen=True, slots=True)
class FactorySignature:
    """Expected factory-bad reading for one control role.

    Used by the classifier to disambiguate ``attenuation`` /
    ``saturation`` / ``mixed`` regimes — a probe whose reading falls
    inside one of the declared ranges is evidence the factory regime
    matches the KB profile.

    At least one of the three ``expected_*_range`` fields must be
    non-``None``; an all-``None`` signature would match no reading and
    contribute nothing to classification.

    Args:
        expected_raw_range: Closed inclusive range of raw integer
            values, or ``None`` when the KB author keyed the signature
            on fraction/dB instead.
        expected_fraction_range: Closed inclusive range as fraction of
            the control's raw span (``current_raw / max_raw``). Both
            bounds in ``[0, 1]``; ``lo <= hi``.
        expected_db_range: Closed inclusive dB range. Only meaningful
            for controls whose ``amixer`` output includes a dB mapping.

    Raises:
        ValueError: On all-``None`` signature, inverted interval, or
            fraction bounds outside the unit interval.
    """

    expected_raw_range: tuple[int, int] | None
    expected_fraction_range: tuple[float, float] | None
    expected_db_range: tuple[float, float] | None

    def __post_init__(self) -> None:
        if (
            self.expected_raw_range is None
            and self.expected_fraction_range is None
            and self.expected_db_range is None
        ):
            msg = (
                "FactorySignature requires at least one of "
                "expected_raw_range / expected_fraction_range / "
                "expected_db_range to be non-None"
            )
            raise ValueError(msg)
        # Paranoid-QA HIGH #10: reject degenerate ranges (``lo == hi``)
        # for fraction + db. The only range where a point-match is
        # meaningful is ``expected_raw_range`` (``(0, 0)`` captures
        # "boost muted" on HDA — a real pilot signature). Fraction +
        # dB point-matches have near-zero probability of firing due
        # to float precision and are almost certainly KB authoring
        # bugs; reject loudly rather than ship a profile that never
        # matches in production.
        if self.expected_raw_range is not None:
            lo_r, hi_r = self.expected_raw_range
            if lo_r > hi_r:
                msg = f"expected_raw_range={self.expected_raw_range!r} is inverted"
                raise ValueError(msg)
        if self.expected_fraction_range is not None:
            lo_f, hi_f = self.expected_fraction_range
            if lo_f > hi_f:
                msg = f"expected_fraction_range={self.expected_fraction_range!r} is inverted"
                raise ValueError(msg)
            if not (0.0 <= lo_f <= 1.0 and 0.0 <= hi_f <= 1.0):
                msg = (
                    f"expected_fraction_range={self.expected_fraction_range!r} "
                    "must lie within [0.0, 1.0]"
                )
                raise ValueError(msg)
            if lo_f == hi_f:
                msg = (
                    f"expected_fraction_range={self.expected_fraction_range!r} "
                    "is degenerate (lo == hi); use a band or widen the bounds"
                )
                raise ValueError(msg)
        if self.expected_db_range is not None:
            lo_d, hi_d = self.expected_db_range
            if lo_d > hi_d:
                msg = f"expected_db_range={self.expected_db_range!r} is inverted"
                raise ValueError(msg)
            if lo_d == hi_d:
                msg = (
                    f"expected_db_range={self.expected_db_range!r} is "
                    "degenerate (lo == hi); use a band"
                )
                raise ValueError(msg)


@dataclass(frozen=True, slots=True)
class VerificationRecord:
    """One HIL verification attestation shipped with a KB profile.

    Each profile's ``verified_on`` list must be non-empty before merge —
    the reviewer pair records the exact system + kernel + distro the
    profile was HIL-validated against. Empty fields are rejected so
    ``grep``-ability of the provenance trail is preserved.

    Args:
        system_product: Exact product string (``dmidecode -s
            system-product-name``).
        codec_id: ``<vendor>:<device>`` hex pair from
            ``/proc/asound/card*/codec#*`` or ``lspci``.
        kernel: Exact ``uname -r`` output at verification time.
        distro: Short distro name + version (e.g. ``"linuxmint-22.2"``).
        verified_at: ISO-8601 date of verification.
        verified_by: GitHub username or ``"sovyx-core"`` for first-party.

    Raises:
        ValueError: On any empty field.
    """

    system_product: str
    codec_id: str
    kernel: str
    distro: str
    verified_at: str
    verified_by: str

    def __post_init__(self) -> None:
        for name, value in (
            ("system_product", self.system_product),
            ("codec_id", self.codec_id),
            ("kernel", self.kernel),
            ("distro", self.distro),
            ("verified_at", self.verified_at),
            ("verified_by", self.verified_by),
        ):
            if not value:
                msg = f"VerificationRecord.{name} must be non-empty"
                raise ValueError(msg)


@dataclass(frozen=True, slots=True)
class MixerKBProfile:
    """Validated knowledge-base entry for one hardware class.

    Loaded from a YAML under ``_mixer_kb/profiles/*.yaml`` (Phase F1.C)
    via pydantic v2 and re-constructed as this dataclass so downstream
    consumers (classifier, apply layer, dashboard) see a single type.
    The KB loader (not this dataclass) is responsible for Ed25519
    signature verification when the shipped bundle is enabled.

    Args:
        profile_id: Snake-case unique identifier within the shipped KB.
        profile_version: Monotonic integer starting at 1; bumps on any
            semantic change to preset or factory signature.
        schema_version: YAML schema version the profile was authored
            against. Loader rejects mismatches.
        codec_id_glob: ``fnmatch`` pattern over codec vendor:device
            (e.g. ``"14F1:5045"``). Required — the primary match key.
        driver_family: Which ALSA driver-family resolver applies.
        system_vendor_glob: Optional ``fnmatch`` over dmidecode system
            vendor.
        system_product_glob: Optional ``fnmatch`` over dmidecode system
            product.
        distro_family: Optional coarse distro bucket (``ubuntu-like``
            etc.). ``None`` means "any".
        audio_stack: Optional constraint on the userspace audio stack.
        kernel_major_minor_glob: Optional ``fnmatch`` over ``uname -r``.
        match_threshold: Minimum weighted score required to select this
            profile. Closed unit interval.
        factory_regime: Which regime this profile's signature describes.
        factory_signature: Per-role expected factory-bad readings.
            ``Mapping`` is kept immutable-by-convention (loader builds
            ``MappingProxyType`` wrappers).
        recommended_preset: Corrective preset to apply on match.
        validation_gates: Post-apply gates; all must pass.
        verified_on: Non-empty tuple of HIL attestations.
        contributed_by: Author GitHub username or ``"sovyx-core"``.

    Raises:
        ValueError: On any validation failure.
    """

    profile_id: str
    profile_version: int
    schema_version: int
    codec_id_glob: str
    driver_family: Literal["hda", "sof", "usb-audio", "bt"]
    system_vendor_glob: str | None
    system_product_glob: str | None
    distro_family: str | None
    audio_stack: Literal["pipewire", "pulseaudio", "alsa"] | None
    kernel_major_minor_glob: str | None
    match_threshold: float
    factory_regime: Literal["attenuation", "saturation", "mixed", "either"]
    factory_signature: Mapping[MixerControlRole, FactorySignature]
    recommended_preset: MixerPresetSpec
    validation_gates: ValidationGates
    verified_on: tuple[VerificationRecord, ...]
    contributed_by: str

    def __post_init__(self) -> None:
        if not self.profile_id:
            msg = "MixerKBProfile.profile_id must be non-empty"
            raise ValueError(msg)
        if self.profile_version < 1:
            msg = f"MixerKBProfile.profile_version={self.profile_version} must be >= 1"
            raise ValueError(msg)
        if self.schema_version < 1:
            msg = f"MixerKBProfile.schema_version={self.schema_version} must be >= 1"
            raise ValueError(msg)
        if not self.codec_id_glob:
            msg = "MixerKBProfile.codec_id_glob must be non-empty"
            raise ValueError(msg)
        if self.driver_family not in _DRIVER_FAMILIES:
            msg = f"driver_family={self.driver_family!r} not in {sorted(_DRIVER_FAMILIES)}"
            raise ValueError(msg)
        if self.audio_stack is not None and self.audio_stack not in _AUDIO_STACKS:
            msg = f"audio_stack={self.audio_stack!r} not in {sorted(_AUDIO_STACKS)}"
            raise ValueError(msg)
        if self.factory_regime not in _FACTORY_REGIMES:
            msg = f"factory_regime={self.factory_regime!r} not in {sorted(_FACTORY_REGIMES)}"
            raise ValueError(msg)
        if not 0.0 <= self.match_threshold <= 1.0:
            msg = f"match_threshold={self.match_threshold!r} must be in [0.0, 1.0]"
            raise ValueError(msg)
        if not self.factory_signature:
            msg = "MixerKBProfile.factory_signature must be non-empty"
            raise ValueError(msg)
        if MixerControlRole.UNKNOWN in self.factory_signature:
            msg = (
                "MixerKBProfile.factory_signature must not contain "
                "MixerControlRole.UNKNOWN — resolve roles before building "
                "the profile"
            )
            raise ValueError(msg)
        if not self.verified_on:
            msg = (
                "MixerKBProfile.verified_on must be non-empty — at least "
                "one HIL attestation is required before merge"
            )
            raise ValueError(msg)
        if not self.contributed_by:
            msg = "MixerKBProfile.contributed_by must be non-empty"
            raise ValueError(msg)


@dataclass(frozen=True, slots=True)
class MixerValidationMetrics:
    """Signal-quality snapshot measured post-apply.

    Pure output, populated by
    :func:`~sovyx.voice.health._mixer_sanity._validate_post_apply`.
    Compared field-by-field against a :class:`ValidationGates` to decide
    accept vs rollback. All fields finite; ``-inf`` is normalised to
    ``-120.0`` upstream for JSON friendliness (same convention as
    :class:`IntegrityResult`).

    Args:
        rms_dbfs: RMS of the validation window, dBFS.
        peak_dbfs: Peak magnitude, dBFS.
        snr_db_vocal_band: SNR in the 300–3400 Hz band, dB.
        silero_max_prob: Peak Silero VAD ``speech`` probability.
        silero_mean_prob: Mean Silero VAD ``speech`` probability.
        wake_word_stage2_prob: OpenWakeWord stage-2 probe result.
        measurement_duration_ms: Wall-clock duration of the validation
            window actually captured (may be shorter than requested if
            the ring buffer drained early).
    """

    rms_dbfs: float
    peak_dbfs: float
    snr_db_vocal_band: float
    silero_max_prob: float
    silero_mean_prob: float
    wake_word_stage2_prob: float
    measurement_duration_ms: int


@dataclass(frozen=True, slots=True)
class MixerSanityResult:
    """Terminal record of one :func:`check_and_maybe_heal` invocation.

    Emitted by :mod:`sovyx.voice.health._mixer_sanity`. Consumed by
    :func:`~sovyx.voice.health.cascade.run_cascade` to decide whether to
    skip the platform cascade walk; by telemetry to feed the
    ``mixer_sanity_outcome`` bucket; and by the dashboard to render the
    Mixer Health card.

    Args:
        decision: Terminal verdict — see :class:`MixerSanityDecision`.
        diagnosis_before: Classified state pre-intervention.
        diagnosis_after: Classified state post-intervention. ``None``
            when the decision short-circuited before a post-apply probe
            (``SKIPPED_*``, ``DEFERRED_*``, ``ERROR``-before-probe).
        regime: Categorical regime label. ``"healthy"`` when the probe
            reported a healthy range and no KB match was required;
            ``"unknown"`` when classification was inconclusive.
        matched_kb_profile: ``profile_id`` of the KB profile that drove
            the apply, or ``None`` when no profile matched.
        kb_match_score: Weighted match score of the selected profile,
            or 0.0 when no profile matched. In ``[0, 1]``.
        user_customization_score: 6-signal heuristic result. In
            ``[0, 1]``.
        cards_probed: ``/proc/asound/cards`` indices seen during
            probing.
        controls_modified: Control names actually mutated during apply
            (empty when decision was not ``HEALED`` or ``ROLLED_BACK``).
        rollback_snapshot: The pre-apply snapshot retained for revert.
            Set on ``HEALED`` (for long-tail rollback on later
            invalidation) and on ``ROLLED_BACK`` (for telemetry).
        probe_duration_ms: Wall-clock of the mixer probe step.
        apply_duration_ms: Wall-clock of the apply step, or ``None``
            when no apply ran.
        validation_passed: Whether every :class:`ValidationGates` gate
            was met. ``None`` when no validation ran.
        validation_metrics: Captured metrics, or ``None`` when no
            validation ran.
        remediation: User-facing hint pointer for the dashboard/CLI;
            populated for DEFERRED/SKIPPED_CUSTOMIZED/ERROR.
        error: Machine-readable failure token from Appendix 6 of the
            V2 plan (``"MIXER_SANITY_PROBE_FAILED"`` etc.).
    """

    decision: MixerSanityDecision
    diagnosis_before: Diagnosis
    diagnosis_after: Diagnosis | None
    regime: Literal["saturation", "attenuation", "mixed", "healthy", "unknown"]
    matched_kb_profile: str | None
    kb_match_score: float
    user_customization_score: float
    cards_probed: tuple[int, ...]
    controls_modified: tuple[str, ...]
    rollback_snapshot: MixerApplySnapshot | None
    probe_duration_ms: int
    apply_duration_ms: int | None
    validation_passed: bool | None
    validation_metrics: MixerValidationMetrics | None
    remediation: RemediationHint | None = None
    error: str | None = None


_HARDWARE_DRIVER_FAMILIES: frozenset[str] = frozenset(
    {"hda", "sof", "usb-audio", "bt", "unknown"},
)
"""Driver families accepted by :class:`HardwareContext`.

Superset of ``_DRIVER_FAMILIES`` (the KB-profile set) by the extra
``"unknown"`` sentinel — KB profiles must target a specific family,
but a detected hardware context may legitimately fail to determine
one (e.g., obscure vendor driver on first boot before ``lspci`` /
``/proc/asound/cards`` have populated).
"""


@dataclass(frozen=True, slots=True)
class HardwareContext:
    """Detected audio-hardware identity for L2.5 + KB matching.

    Populated once per cascade pass by the Linux platform probe (or
    the equivalent Windows/macOS stub in F3) and threaded through
    three consumers:

    * :class:`~sovyx.voice.health._mixer_roles.MixerControlRoleResolver`
      — ``codec_id`` drives the per-codec override lookup;
      ``driver_family`` drives the family-table lookup.
    * ``MixerKBLookup.match`` (Phase F1.C) — every field is compared
      ``fnmatch``-style against the corresponding glob on
      :class:`MixerKBProfile`.
    * ``_mixer_sanity._detect_user_customization`` (Phase F1.E) —
      reads ``audio_stack`` / ``distro`` to locate the right config
      paths for PipeWire / PulseAudio / WirePlumber.

    Every field except ``driver_family`` is optional because detection
    can partially fail — a missing ``codec_id`` should degrade the KB
    match score, not abort the cascade.

    Args:
        driver_family: ALSA driver family. ``"unknown"`` is valid and
            means "probe could not determine"; resolver then skips
            the family table and relies on substring fallback.
        codec_id: ``<vendor>:<device>`` hex pair read from
            ``/proc/asound/card*/codec#*`` (e.g. ``"14F1:5045"``).
            Case-normalisation is the producer's responsibility — the
            resolver and KB loader both expect the exact string
            ``/proc/asound`` reports.
        system_vendor: ``dmidecode -s system-manufacturer`` output
            (e.g. ``"Sony Group Corporation"``).
        system_product: ``dmidecode -s system-product-name`` output
            (e.g. ``"VJFE69F11X-B0221H"``).
        distro: Short distro + version label (e.g.
            ``"linuxmint-22.2"``). Used for config path resolution,
            not for matching alone.
        audio_stack: Active userspace audio stack, detected via
            ``wpctl status`` / ``pactl info`` / ``/proc/asound/pcm``.
            ``None`` when no userspace daemon is running.
        kernel: Exact ``uname -r`` output (e.g. ``"6.14.0-37-generic"``).
            Consumed as an ``fnmatch`` target against
            :attr:`MixerKBProfile.kernel_major_minor_glob`.

    Raises:
        ValueError: On unknown ``driver_family`` or ``audio_stack``.
    """

    driver_family: Literal["hda", "sof", "usb-audio", "bt", "unknown"]
    codec_id: str | None = None
    system_vendor: str | None = None
    system_product: str | None = None
    distro: str | None = None
    audio_stack: Literal["pipewire", "pulseaudio", "alsa"] | None = None
    kernel: str | None = None

    def __post_init__(self) -> None:
        if self.driver_family not in _HARDWARE_DRIVER_FAMILIES:
            msg = (
                f"driver_family={self.driver_family!r} not in {sorted(_HARDWARE_DRIVER_FAMILIES)}"
            )
            raise ValueError(msg)
        if self.audio_stack is not None and self.audio_stack not in _AUDIO_STACKS:
            msg = f"audio_stack={self.audio_stack!r} not in {sorted(_AUDIO_STACKS)}"
            raise ValueError(msg)
