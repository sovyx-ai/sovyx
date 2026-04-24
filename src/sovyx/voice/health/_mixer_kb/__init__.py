"""Mixer KB package — load, match, score hardware-specific presets (L2.5 F1.C).

Public surface:

* :class:`MixerKBLookup` — orchestrates load + match.
* :class:`~sovyx.voice.health._mixer_kb.matcher.MixerKBMatch` — result
  record returned by :meth:`MixerKBLookup.match`.

Submodules (underscore-prefixed: internal, accessed via this package):

* :mod:`~sovyx.voice.health._mixer_kb.schema` — pydantic v2 YAML models
* :mod:`~sovyx.voice.health._mixer_kb.loader` — YAML → dataclass conversion
* :mod:`~sovyx.voice.health._mixer_kb.matcher` — weighted scoring

See V2 Master Plan Part E.3 + Appendix 2.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from sovyx.observability.logging import get_logger
from sovyx.voice.health._mixer_kb.loader import (
    load_profile_file,
    load_profiles_from_directory,
)
from sovyx.voice.health._mixer_kb.matcher import MixerKBMatch, score_profile

if TYPE_CHECKING:
    from collections.abc import Sequence

    from sovyx.voice.health._mixer_roles import MixerControlRoleResolver
    from sovyx.voice.health.contract import (
        HardwareContext,
        MixerCardSnapshot,
        MixerKBProfile,
    )

logger = get_logger(__name__)


_DEFAULT_AMBIGUITY_WINDOW: float = 0.05
"""Default score-gap below which two matches are considered ambiguous.

If the top two profiles score within this window, :meth:`match`
returns ``None`` (L2.5 defers — the dashboard should offer a choice
card rather than let L2.5 pick arbitrarily between equivalent
candidates). V2 Plan Part E.3.
"""


_SHIPPED_PROFILES_DIR: Path = Path(__file__).parent / "profiles"
"""Directory bundled with the Sovyx wheel that ships KB profiles.

Empty in Phase F1 — Phase F1.H populates per the KB contribution
workflow. :meth:`MixerKBLookup.load_shipped` gracefully returns an
empty lookup if the directory has no profiles.
"""


class MixerKBLookup:
    """Catalogue of KB profiles with hardware-scored matching.

    Enterprise-grade design notes:

    * **Stateless per-match.** One instance can serve many cascade
      passes, endpoints, and hardware contexts.
    * **Two-tier authoring** — shipped profiles (signed, HIL-validated
      in PR review) + user-contributed profiles (untrusted, unsigned,
      surfaced with a "community" badge). :attr:`MixerKBMatch.is_user_contributed`
      partitions telemetry and UI.
    * **Ambiguity-aware** — two near-equal matches always beat one
      arbitrary pick. Dashboard UX is part of the contract; see
      :data:`_DEFAULT_AMBIGUITY_WINDOW`.
    """

    def __init__(
        self,
        profiles: Sequence[MixerKBProfile],
        *,
        resolver: MixerControlRoleResolver,
        user_contributed: Sequence[MixerKBProfile] = (),
    ) -> None:
        """Build a lookup from pre-loaded profiles.

        Args:
            profiles: Shipped profiles. Treated as trusted — Ed25519
                signature verification (F2) runs before construction.
            resolver: Role resolver used by the factory-signature
                scoring step of :meth:`match`. Injected rather than
                built internally so tests can provide custom tables.
            user_contributed: User-contributed profiles. Displayed
                with the community badge; tagged
                ``MixerKBMatch.is_user_contributed=True`` on match.
        """
        self._profiles: tuple[MixerKBProfile, ...] = tuple(profiles)
        self._user_profiles: tuple[MixerKBProfile, ...] = tuple(user_contributed)
        self._resolver = resolver

    @classmethod
    def load_shipped(
        cls,
        *,
        resolver: MixerControlRoleResolver,
    ) -> MixerKBLookup:
        """Load the bundled ``profiles/`` directory.

        Raises nothing — the loader logs + skips malformed files and
        the lookup is happy with zero profiles (F1 ships empty per
        Part F.1; matching just returns ``None``).
        """
        shipped = load_profiles_from_directory(_SHIPPED_PROFILES_DIR)
        return cls(shipped, resolver=resolver)

    @classmethod
    def load_shipped_and_user(
        cls,
        user_dir: Path,
        *,
        resolver: MixerControlRoleResolver,
    ) -> MixerKBLookup:
        """Load shipped + user-contributed profiles.

        The two pools are kept separate so matches can surface
        provenance correctly. Failures in either pool degrade
        gracefully (WARN + empty partial result).

        Paranoid-QA R3 HIGH #7: when the same ``profile_id`` appears
        in BOTH the shipped and user directories, the user-contributed
        copy takes precedence and the shipped copy is dropped. The
        prior behaviour kept both; then :meth:`match` sorted by score
        and the ambiguity-window check (0.05) would trip on the
        identical scores, returning ``None`` → L2.5 DEFERRED on
        hardware the user explicitly authored a profile for. With
        the dedupe, the user override is honoured and the shipped
        copy never enters scoring.

        Args:
            user_dir: Typically ``~/.sovyx/mixer_kb/user/``.
                Missing directory is fine — returns empty user list.
            resolver: Role resolver, as in :meth:`load_shipped`.
        """
        shipped = load_profiles_from_directory(_SHIPPED_PROFILES_DIR)
        user = load_profiles_from_directory(user_dir)
        user_ids = {p.profile_id for p in user}
        deduped_shipped: list[MixerKBProfile] = []
        for profile in shipped:
            if profile.profile_id in user_ids:
                logger.warning(
                    "mixer_kb_user_profile_shadows_shipped",
                    profile_id=profile.profile_id,
                    note=(
                        "user-contributed profile with the same "
                        "profile_id as a shipped profile takes "
                        "precedence; shipped copy dropped from scoring"
                    ),
                )
                continue
            deduped_shipped.append(profile)
        return cls(deduped_shipped, resolver=resolver, user_contributed=user)

    @property
    def profiles(self) -> tuple[MixerKBProfile, ...]:
        """Shipped profiles, as an immutable tuple."""
        return self._profiles

    @property
    def user_contributed_profiles(self) -> tuple[MixerKBProfile, ...]:
        """User-contributed profiles, as an immutable tuple."""
        return self._user_profiles

    def match(
        self,
        hw: HardwareContext,
        mixer_snapshot: Sequence[MixerCardSnapshot],
        *,
        min_score: float | None = None,
        ambiguity_window: float = _DEFAULT_AMBIGUITY_WINDOW,
    ) -> MixerKBMatch | None:
        """Return the highest-scored match above threshold, or ``None``.

        Args:
            hw: Detected hardware context.
            mixer_snapshot: Current mixer state — fed to the
                factory-signature scoring step.
            min_score: Score floor. ``None`` (default) uses each
                profile's own :attr:`MixerKBProfile.match_threshold`.
                Explicit values override uniformly (useful for
                aggressive heuristics via CLI ``--aggressive``).
            ambiguity_window: If the top two matches score within
                this window, return ``None`` and log the ambiguity.
                Default :data:`_DEFAULT_AMBIGUITY_WINDOW`.

        Returns:
            Best match, or ``None`` when:

            * No profile clears the score threshold.
            * Two or more profiles tie within ``ambiguity_window``
              (L2.5 defers; dashboard offers a choice card).
        """
        scored = self._score_all(hw, mixer_snapshot)
        if not scored:
            return None

        # Apply per-profile threshold unless caller overrode it.
        surviving: list[MixerKBMatch] = []
        for match in scored:
            threshold = min_score if min_score is not None else match.profile.match_threshold
            if match.score >= threshold:
                surviving.append(match)

        if not surviving:
            return None

        surviving.sort(key=lambda m: m.score, reverse=True)
        best = surviving[0]
        if len(surviving) >= 2:
            runner_up = surviving[1]
            if best.score - runner_up.score < ambiguity_window:
                logger.info(
                    "mixer_kb_ambiguous_match",
                    best_profile=best.profile.profile_id,
                    best_score=best.score,
                    runner_up_profile=runner_up.profile.profile_id,
                    runner_up_score=runner_up.score,
                    ambiguity_window=ambiguity_window,
                )
                return None

        logger.debug(
            "mixer_kb_match_selected",
            profile_id=best.profile.profile_id,
            score=best.score,
            is_user_contributed=best.is_user_contributed,
        )
        return best

    def _score_all(
        self,
        hw: HardwareContext,
        mixer_snapshot: Sequence[MixerCardSnapshot],
    ) -> list[MixerKBMatch]:
        """Score every profile (shipped + user) against ``hw`` + snapshot.

        Results include zero-score matches so callers can introspect;
        :meth:`match` applies the threshold filter downstream.

        Paranoid-QA R4 MEDIUM-3: collects profile_ids whose
        factory_signature had unmappable roles and emits ONE WARN
        per cascade, instead of N per-profile WARNs. With a 50+
        profile KB, a single resolver TODO would otherwise fire 50
        WARNs per cascade.
        """
        results: list[MixerKBMatch] = []
        unmappable_profile_ids: set[str] = set()
        for profile in self._profiles:
            score, breakdown = score_profile(
                profile,
                hw,
                mixer_snapshot,
                self._resolver,
                unmappable_roles_out=unmappable_profile_ids,
            )
            results.append(
                MixerKBMatch(
                    profile=profile,
                    score=score,
                    per_field_scores=breakdown,
                    is_user_contributed=False,
                ),
            )
        for profile in self._user_profiles:
            score, breakdown = score_profile(
                profile,
                hw,
                mixer_snapshot,
                self._resolver,
                unmappable_roles_out=unmappable_profile_ids,
            )
            results.append(
                MixerKBMatch(
                    profile=profile,
                    score=score,
                    per_field_scores=breakdown,
                    is_user_contributed=True,
                ),
            )
        if unmappable_profile_ids:
            logger.warning(
                "mixer_kb_signature_roles_unmappable_aggregate",
                codec_id=hw.codec_id,
                driver_family=hw.driver_family,
                profile_count=len(unmappable_profile_ids),
                profile_ids=sorted(unmappable_profile_ids),
                note=(
                    "one or more KB profiles declare signature roles "
                    "the resolver cannot map on this codec; individual "
                    "per-profile details are at DEBUG level"
                ),
            )
        return results


__all__ = [
    "MixerKBLookup",
    "MixerKBMatch",
    "load_profile_file",
    "load_profiles_from_directory",
    "score_profile",
]
