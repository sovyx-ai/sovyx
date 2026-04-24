"""Weighted scoring algorithm for KB profile matching — L2.5 Phase F1.C.

Implements the match algorithm from V2 Master Plan §F.2:

1. **Codec gate** — ``codec_id`` against ``codec_id_glob`` is a hard
   requirement; mismatch returns score 0.0 immediately (no point
   scoring the rest — we could apply a preset intended for the wrong
   codec and destroy the signal).
2. **Soft signals** — driver_family, system_vendor_glob,
   system_product_glob, audio_stack, kernel_major_minor_glob. Each
   missed declared glob contributes nothing (spec-faithful lenient
   scoring); missed undeclared glob trivially absent.
3. **Factory signature match** — fraction of
   :attr:`MixerKBProfile.factory_signature` roles whose declared
   range encloses the current reading. Uses the role resolver to
   find the right :class:`MixerControlSnapshot` for each role.

Weights sum to 1.0 when every signal contributes; profiles with
fewer declared criteria normalise against their declared total
(denominator = sum of weights present in the result, not the
maximum), so a profile that declares only codec + factory_sig can
still reach score 1.0.

:class:`MixerKBMatch` carries the per-field breakdown for telemetry
and for the dashboard Mixer Health card.
"""

from __future__ import annotations

import fnmatch
from dataclasses import dataclass
from typing import TYPE_CHECKING

from sovyx.observability.logging import get_logger

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from sovyx.voice.health._mixer_roles import MixerControlRoleResolver
    from sovyx.voice.health.contract import (
        FactorySignature,
        HardwareContext,
        MixerCardSnapshot,
        MixerControlRole,
        MixerControlSnapshot,
        MixerKBProfile,
    )

logger = get_logger(__name__)


# Spec-faithful weights from V2 Master Plan §F.2.
_WEIGHT_CODEC_ID = 0.4
_WEIGHT_DRIVER_FAMILY = 0.1
_WEIGHT_SYSTEM_VENDOR = 0.1
_WEIGHT_SYSTEM_PRODUCT = 0.2
_WEIGHT_AUDIO_STACK = 0.05
_WEIGHT_KERNEL = 0.05
_WEIGHT_FACTORY_SIG = 0.1


@dataclass(frozen=True, slots=True)
class MixerKBMatch:
    """One successful profile match against the current hardware state.

    Emitted by :meth:`MixerKBLookup.match`. Consumed by
    ``_mixer_sanity.check_and_maybe_heal`` (Phase F1.E) to decide
    whether to apply, and by the dashboard + telemetry to render the
    per-match breakdown.

    Args:
        profile: The matched :class:`MixerKBProfile`.
        score: Weighted average in ``[0, 1]``. Monotonic: higher is
            better. Compared against
            :attr:`MixerKBProfile.match_threshold` by the lookup.
        per_field_scores: Breakdown for diagnostics —
            ``(field_name, raw_score, weight)`` triples in spec
            order (codec_id, driver_family, system_vendor,
            system_product, audio_stack, kernel_mm, factory_sig).
            Only fields that contributed appear.
        is_user_contributed: ``True`` if the profile came from
            ``~/.sovyx/mixer_kb/user/*.yaml`` rather than the shipped
            bundle. Affects the dashboard badge ("community profile")
            and telemetry partitioning.
    """

    profile: MixerKBProfile
    score: float
    per_field_scores: tuple[tuple[str, float, float], ...]
    is_user_contributed: bool = False


def score_profile(
    profile: MixerKBProfile,
    hw: HardwareContext,
    mixer_snapshot: Sequence[MixerCardSnapshot],
    resolver: MixerControlRoleResolver,
) -> tuple[float, tuple[tuple[str, float, float], ...]]:
    """Score one profile against detected hardware + current mixer state.

    Returns ``(score, per_field_breakdown)``. ``score = 0.0`` iff the
    codec gate failed (profile does not apply to this hardware).

    Args:
        profile: Candidate KB profile.
        hw: Detected hardware context. ``hw.codec_id`` must be
            non-empty for any non-zero score; every other field may
            be ``None``.
        mixer_snapshot: Current mixer state across all cards. Used
            only for factory-signature scoring.
        resolver: Role resolver — consumed by the factory-signature
            check to map ``profile.factory_signature`` role keys
            onto actual control snapshots.
    """
    # Layer 0: codec gate (hard requirement).
    #
    # Paranoid-QA R2 HIGH #5: we deliberately do NOT log the
    # mismatch here. ``_score_all`` calls this once per KB profile on
    # every cascade — with a growing shipped KB (50+ profiles) this
    # fires a DEBUG event per profile per cascade, polluting logs
    # when operators enable DEBUG to debug something unrelated. The
    # aggregate scoring outcome is logged ONCE at
    # :meth:`MixerKBLookup.match` (``mixer_kb_match_selected`` /
    # ``mixer_kb_ambiguous_match``); per-profile mismatches add no
    # actionable information on top.
    if hw.codec_id is None or not fnmatch.fnmatchcase(
        hw.codec_id,
        profile.codec_id_glob,
    ):
        return (0.0, (("codec_id", 0.0, _WEIGHT_CODEC_ID),))

    scores: list[tuple[str, float, float]] = [
        ("codec_id", 1.0, _WEIGHT_CODEC_ID),
    ]

    if hw.driver_family == profile.driver_family:
        scores.append(("driver_family", 1.0, _WEIGHT_DRIVER_FAMILY))

    # Paranoid-QA CRITICAL #11: ``fnmatchcase`` (not ``fnmatch``)
    # everywhere. ``fnmatch.fnmatch`` applies ``os.path.normcase`` which
    # lowercases on Windows / NTFS — a profile authored with
    # ``SONY*`` matches ``"sony"`` on the dev workstation but NOT on
    # the Linux CI runner, making scoring dependent on the host OS.
    # KB profiles ship case-sensitive by contract.
    if (
        profile.system_vendor_glob
        and hw.system_vendor is not None
        and fnmatch.fnmatchcase(hw.system_vendor, profile.system_vendor_glob)
    ):
        scores.append(("system_vendor", 1.0, _WEIGHT_SYSTEM_VENDOR))

    if (
        profile.system_product_glob
        and hw.system_product is not None
        and fnmatch.fnmatchcase(hw.system_product, profile.system_product_glob)
    ):
        scores.append(("system_product", 1.0, _WEIGHT_SYSTEM_PRODUCT))

    if profile.audio_stack and hw.audio_stack == profile.audio_stack:
        scores.append(("audio_stack", 1.0, _WEIGHT_AUDIO_STACK))

    if (
        profile.kernel_major_minor_glob
        and hw.kernel is not None
        and fnmatch.fnmatchcase(hw.kernel, profile.kernel_major_minor_glob)
    ):
        scores.append(("kernel_mm", 1.0, _WEIGHT_KERNEL))

    sig_result = _match_factory_signature(
        profile.factory_signature,
        mixer_snapshot,
        resolver,
        hw,
    )
    # Paranoid-QA CRITICAL #10 + R2 CRITICAL #5: factory_signature is
    # the PROOF that the observed hardware is in the factory-bad
    # regime the profile is designed to cure. With the soft-weight
    # scheme, a profile matching codec + driver_family alone could
    # reach score 0.8+ even when NONE of the signature roles match
    # current readings — applying the preset on healthy hardware (or
    # user-tuned to a different state) would be harmful.
    #
    # Hard gate: zero roles matched → the profile does NOT apply,
    # regardless of how well other fields scored. We gate on the
    # integer ``roles_matched`` field, NOT on the derived float
    # ``score``. A future signature change that makes the score a
    # weighted combination of per-role closeness (e.g., partial credit
    # for "control reads 0.11 vs expected [0.0, 0.10]") would silently
    # break ``score == 0.0`` — the integer gate is immune to that.
    #
    # Partial signature match (``roles_matched > 0``) is still
    # admitted via the soft-weight path — the post-apply validation
    # gates are the final arbiter of whether the preset was actually
    # the right one.
    # Paranoid-QA R2 HIGH #8: surface resolver coverage gaps as a
    # WARNING even when scoring continues. Operator-facing signal
    # distinct from "healthy hardware silence" so KB authors +
    # support can correlate a silent L2.5 no-op with a role-mapping
    # TODO. Fires before the hard gate so it's visible regardless of
    # whether we reject.
    if sig_result.roles_unmappable > 0:
        logger.warning(
            "mixer_kb_signature_role_unmappable",
            profile_id=profile.profile_id,
            roles_unmappable=sig_result.roles_unmappable,
            roles_total=sig_result.roles_total,
            codec_id=hw.codec_id,
            note=(
                "resolver returned zero candidate controls for at least one "
                "signature role — KB author or resolver missing an alias"
            ),
        )
    if sig_result.roles_matched == 0:
        logger.debug(
            "mixer_kb_factory_signature_zero_match",
            profile_id=profile.profile_id,
            roles_total=sig_result.roles_total,
            roles_unmappable=sig_result.roles_unmappable,
            note="hard gate — profile does not apply when 0 signature roles match",
        )
        scores.append(("factory_sig", 0.0, _WEIGHT_FACTORY_SIG))
        return (0.0, tuple(scores))
    scores.append(("factory_sig", sig_result.score, _WEIGHT_FACTORY_SIG))

    total_weight = sum(w for _, _, w in scores)
    if total_weight == 0.0:
        return (0.0, tuple(scores))
    weighted_sum = sum(s * w for _, s, w in scores)
    return (weighted_sum / total_weight, tuple(scores))


@dataclass(frozen=True, slots=True)
class FactorySignatureMatch:
    """Structured result for :func:`_match_factory_signature`.

    Paranoid-QA R2 CRITICAL #5: the scoring path needs to distinguish
    "zero roles matched" (hard gate trip → profile rejected) from
    "some roles matched but the derived score rounds near zero"
    (legitimate partial match → continue soft-weight scoring). An
    exact-float ``score == 0.0`` comparison collapses those cases,
    which is a fragile foundation for a safety-critical gate. This
    dataclass surfaces the integer ``roles_matched`` count directly.

    Paranoid-QA R2 HIGH #8: also surfaces ``roles_unmappable`` — the
    count of signature roles where the resolver found ZERO candidate
    controls on ANY probed card. This happens when the codec gate
    passed (profile claims this hardware) but the resolver lacks
    aliases for the codec variant; the hard gate still rejects
    (we can't verify the signature), but operators need a distinct
    telemetry signal so resolver coverage gaps don't look like
    "healthy hardware" silence.

    Attributes:
        score: ``roles_matched / roles_total`` in ``[0, 1]``. ``0.0``
            when either the signature is empty (defensive — rejected
            at profile build time) or no roles matched.
        roles_matched: Integer count of signature roles whose readings
            fell inside the expected range on at least one card.
            Compared against zero by the hard gate.
        roles_total: Total number of roles declared in the signature
            (``len(signature)``). Also ``0`` when signature is empty.
        roles_unmappable: Count of signature roles where NO card
            returned any candidate control for the role. Distinct
            from "control found but reading out of range"
            (counted in ``roles_total - roles_matched -
            roles_unmappable``). When positive, :func:`score_profile`
            logs a coverage-gap warning so operators can correlate
            silent L2.5 no-ops with KB-author / resolver TODO.
    """

    score: float
    roles_matched: int
    roles_total: int
    roles_unmappable: int = 0


def _match_factory_signature(
    signature: Mapping[MixerControlRole, FactorySignature],
    snapshot: Sequence[MixerCardSnapshot],
    resolver: MixerControlRoleResolver,
    hw: HardwareContext,
) -> FactorySignatureMatch:
    """Return structured match for ``signature`` against current readings.

    Iterates the profile's ``factory_signature`` entries; for each
    role, walks every card's role mapping looking for a
    :class:`MixerControlSnapshot` whose current reading falls inside
    one of the signature's declared ``expected_*_range`` fields.
    A role matches if *any* of its control candidates matches.
    """
    total = len(signature)
    if total == 0:
        # Defensive: ``MixerKBProfile.__post_init__`` rejects empty
        # signatures, so reaching here means the caller built a
        # profile outside the contract path. Treat as no-match.
        return FactorySignatureMatch(
            score=0.0,
            roles_matched=0,
            roles_total=0,
            roles_unmappable=0,
        )
    matched = 0
    unmappable = 0
    for role, sig in signature.items():
        outcome = _role_outcome_in_snapshot(role, sig, snapshot, resolver, hw)
        if outcome == _RoleOutcome.MATCHED:
            matched += 1
        elif outcome == _RoleOutcome.UNMAPPABLE:
            unmappable += 1
        # UNMATCHED_OUT_OF_RANGE falls through — counted implicitly
        # as ``total - matched - unmappable``.
    return FactorySignatureMatch(
        score=matched / total,
        roles_matched=matched,
        roles_total=total,
        roles_unmappable=unmappable,
    )


class _RoleOutcome:
    """Three-valued outcome for a single signature role.

    Not a StrEnum because this is a local sentinel, not a
    public-surface value — the scorer collapses these back into
    integer counts on the :class:`FactorySignatureMatch`.
    """

    MATCHED = "matched"
    UNMATCHED_OUT_OF_RANGE = "unmatched_out_of_range"
    UNMAPPABLE = "unmappable"


def _role_outcome_in_snapshot(
    role: MixerControlRole,
    sig: FactorySignature,
    snapshot: Sequence[MixerCardSnapshot],
    resolver: MixerControlRoleResolver,
    hw: HardwareContext,
) -> str:
    """Classify a single role's outcome across every probed card.

    Returns ``MATCHED`` if any card has a control whose reading falls
    inside the signature's declared range. Returns ``UNMAPPABLE`` if
    NO card returned any candidate control for the role (resolver
    coverage gap). Otherwise ``UNMATCHED_OUT_OF_RANGE`` (candidate
    controls exist but current readings are out of range).
    """
    any_candidate_found = False
    for card in snapshot:
        role_map = resolver.resolve_card(card, hw)
        candidates = role_map.get(role, ())
        if candidates:
            any_candidate_found = True
            for control in candidates:
                if _control_matches_signature(control, sig):
                    return _RoleOutcome.MATCHED
    if not any_candidate_found:
        return _RoleOutcome.UNMAPPABLE
    return _RoleOutcome.UNMATCHED_OUT_OF_RANGE


def _control_matches_signature(
    control: MixerControlSnapshot,
    sig: FactorySignature,
) -> bool:
    """Return True iff ``control``'s current reading falls in any declared range.

    Evaluates each declared ``expected_*_range`` independently (OR
    semantics) — the signature author may declare raw + fraction +
    dB simultaneously and the match fires if *any* range encloses
    the reading. This matches the intent of multiple-range
    declarations: "the factory-bad signature can present as any of
    these", not "all of these must hold".
    """
    if sig.expected_raw_range is not None:
        lo_r, hi_r = sig.expected_raw_range
        if lo_r <= control.current_raw <= hi_r:
            return True
    if sig.expected_fraction_range is not None:
        lo_f, hi_f = sig.expected_fraction_range
        span = control.max_raw - control.min_raw
        if span > 0:
            frac = (control.current_raw - control.min_raw) / span
            if lo_f <= frac <= hi_f:
                return True
    if sig.expected_db_range is not None and control.current_db is not None:
        lo_d, hi_d = sig.expected_db_range
        if lo_d <= control.current_db <= hi_d:
            return True
    return False


__all__ = [
    "MixerKBMatch",
    "score_profile",
]
