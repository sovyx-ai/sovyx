"""Linux — reset saturated ALSA mixer gain controls on the active capture card.

Primary remedy for the Linux ALSA-mixer saturation pattern observed on
laptop onboard codecs (Conexant SN6180 / Realtek ALC family / Cirrus
CS42L family): a pair of pre-ADC gain stages (``Internal Mic Boost`` +
``Capture``) sits at maximum by default on fresh installs, summing to
+40 dB or more before the ADC. Every peak of intelligible speech
clips the ADC; SileroVAD — trained on clean speech — classifies the
clipped signal as silence and the pipeline runs deaf despite a healthy
RMS. The fix is to drive those controls back into the analog range
where the ADC does not saturate.

Design:

* :class:`LinuxALSAMixerResetBypass` is a thin orchestrator over
  :func:`sovyx.voice.health._linux_mixer_probe.enumerate_alsa_mixer_snapshots`
  (read), :func:`sovyx.voice.health._linux_mixer_apply.apply_mixer_reset`
  (write), and :func:`restore_mixer_snapshot` (revert).
* Applies only to controls whose probe marked
  :attr:`MixerControlSnapshot.saturation_risk=True` — the strategy
  never touches a control that is already in a safe range, so
  iterating the strategy over time is a no-op when the root cause is
  already resolved.
* Holds the :class:`MixerApplySnapshot` returned by
  :func:`apply_mixer_reset` on ``self`` so :meth:`revert` can restore
  the exact pre-apply raw values (not "reset to some default").

Not sequenced with any stream restart: the ``amixer`` mutation is
observed by PortAudio via the next callback without any client-side
reopen. The capture stream keeps running untouched — so the strategy
is strictly safer than the Windows exclusive-mode path, which tears
down and rebuilds the stream.

See ``docs-internal/plans/linux-alsa-mixer-saturation-fix.md`` §2.3
for the derivation.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from sovyx.engine.config import VoiceTuningConfig
from sovyx.observability.logging import get_logger
from sovyx.voice.health._linux_mixer_apply import (
    apply_mixer_boost_up,
    apply_mixer_reset,
    restore_mixer_snapshot,
)
from sovyx.voice.health._linux_mixer_check import _is_attenuated
from sovyx.voice.health._linux_mixer_probe import enumerate_alsa_mixer_snapshots
from sovyx.voice.health.bypass._strategy import BypassApplyError
from sovyx.voice.health.contract import Eligibility

if TYPE_CHECKING:
    from sovyx.voice.health.contract import (
        BypassContext,
        MixerApplySnapshot,
        MixerCardSnapshot,
    )

logger = get_logger(__name__)


_STRATEGY_NAME = "linux.alsa_mixer_reset"
"""Coordinator-visible strategy identifier — treat as external API.

Dashboards filter bypass outcomes by this string and the per-strategy
metric counter derives its attribute label from it; a rename is a
breaking change.
"""


# Eligibility-reason tokens surfaced in :class:`Eligibility.reason`.
# Listed as module-level constants so the preflight + tests can
# reference them without string-literal drift.
_REASON_NOT_LINUX = "not_linux_platform"
_REASON_DISABLED_BY_TUNING = "alsa_mixer_reset_disabled_by_tuning"
_REASON_NO_SATURATION_DETECTED = "no_saturated_controls_detected"
_REASON_NO_FAULT_DETECTED = "no_saturation_or_attenuation_detected"
_REASON_NO_AMIXER = "amixer_unavailable_on_host"
_REASON_CARD_MATCH_AMBIGUOUS = "card_match_ambiguous"


# Apply-reason tokens surfaced through :class:`BypassApplyError.reason`.
_APPLY_REASON_NO_SNAPSHOTS = "no_mixer_snapshots_at_apply"
_APPLY_REASON_NO_SATURATED = "no_saturated_controls_at_apply"
_APPLY_REASON_NO_ATTENUATED = "no_attenuated_controls_at_apply"
_APPLY_REASON_CARD_GONE = "target_card_unavailable_at_apply"


# Conservative cost hint for the coordinator telemetry. One ``amixer
# sset`` call per control runs in the 10–30 ms range on a healthy host;
# 120 ms covers two controls plus the enumeration + classification
# bookkeeping with head-room for a slow SSD or bus contention.
_APPLY_COST_MS = 120


class LinuxALSAMixerResetBypass:
    """Reduce saturated ALSA boost/capture controls on the active card.

    Eligibility:
        * :attr:`BypassContext.platform_key == "linux"`.
        * :attr:`VoiceTuningConfig.linux_alsa_mixer_reset_enabled` is
          ``True`` (default-on; user can opt out via
          ``SOVYX_TUNING__VOICE__LINUX_ALSA_MIXER_RESET_ENABLED=false``).
        * :func:`enumerate_alsa_mixer_snapshots` reports at least one
          card whose :attr:`MixerCardSnapshot.saturation_warning` is
          ``True``.
        * Exactly one saturating card matches the active endpoint — or,
          when the endpoint name does not disambiguate the set, a
          single saturating card is observed on the host. Ambiguous
          matches (multiple saturating cards, none matching the
          endpoint) are marked :attr:`Eligibility.applicable=False`
          with reason ``card_match_ambiguous`` — the strategy refuses
          to guess.

    Apply:
        Re-probes the mixer state (the eligibility snapshot may be
        stale by the time the coordinator reaches ``apply``), picks
        the same target card, selects controls with
        :attr:`MixerControlSnapshot.saturation_risk=True`, and delegates
        to :func:`apply_mixer_reset`. The returned
        :class:`MixerApplySnapshot` is stashed on ``self`` so
        :meth:`revert` can drive a deterministic rollback — callers
        that ignore ``revert`` still benefit from the atomic per-apply
        rollback that :func:`apply_mixer_reset` performs on its own
        failure paths.

    Revert:
        Calls :func:`restore_mixer_snapshot` on the stashed snapshot.
        Best-effort; a failure on one control is logged at WARNING but
        does not raise — the coordinator is already in teardown.
        Idempotent: a second call after the snapshot has been
        consumed is a no-op.
    """

    name: str = _STRATEGY_NAME

    def __init__(self) -> None:
        self._applied_snapshot: MixerApplySnapshot | None = None

    async def probe_eligibility(
        self,
        context: BypassContext,
    ) -> Eligibility:
        if context.platform_key != "linux":
            return Eligibility(
                applicable=False,
                reason=_REASON_NOT_LINUX,
                estimated_cost_ms=0,
            )
        tuning = _tuning_from_context()
        if not tuning.linux_alsa_mixer_reset_enabled:
            return Eligibility(
                applicable=False,
                reason=_REASON_DISABLED_BY_TUNING,
                estimated_cost_ms=0,
            )
        snapshots = enumerate_alsa_mixer_snapshots()
        if not snapshots:
            # Empty list covers both "not linux" and "amixer missing"
            # and "no controls probed" — on the linux branch we treat
            # this as the host-side tool being unavailable.
            return Eligibility(
                applicable=False,
                reason=_REASON_NO_AMIXER,
                estimated_cost_ms=0,
            )
        # Two regimes — saturation (controls clipping) OR attenuation
        # (capture+boost below VAD floor). Strategy is applicable to
        # either; apply() routes to the right remediation.
        faulted = [s for s in snapshots if s.saturation_warning or _is_attenuated(s)]
        if not faulted:
            return Eligibility(
                applicable=False,
                reason=_REASON_NO_FAULT_DETECTED,
                estimated_cost_ms=0,
            )
        target = _match_target_card(
            faulted=faulted,
            endpoint_friendly_name=context.endpoint_friendly_name,
        )
        if target is None:
            return Eligibility(
                applicable=False,
                reason=_REASON_CARD_MATCH_AMBIGUOUS,
                estimated_cost_ms=0,
            )
        return Eligibility(
            applicable=True,
            reason="",
            estimated_cost_ms=_APPLY_COST_MS,
        )

    async def apply(
        self,
        context: BypassContext,
    ) -> str:
        logger.info(
            "bypass_strategy_apply_begin",
            strategy=_STRATEGY_NAME,
            endpoint_guid=context.endpoint_guid,
            endpoint_name=context.endpoint_friendly_name,
            host_api=context.host_api_name,
        )
        tuning = _tuning_from_context()
        snapshots = enumerate_alsa_mixer_snapshots()
        if not snapshots:
            msg = (
                "enumerate_alsa_mixer_snapshots returned empty at apply time — "
                "mixer state vanished between eligibility and apply"
            )
            raise BypassApplyError(msg, reason=_APPLY_REASON_NO_SNAPSHOTS)
        faulted = [s for s in snapshots if s.saturation_warning or _is_attenuated(s)]
        target = _match_target_card(
            faulted=faulted,
            endpoint_friendly_name=context.endpoint_friendly_name,
        )
        if target is None:
            msg = (
                "no faulted ALSA card matched the active endpoint at apply "
                "time — re-probe shows zero or ambiguous candidates"
            )
            raise BypassApplyError(msg, reason=_APPLY_REASON_CARD_GONE)

        # Route by regime — saturation REDUCES, attenuation BOOSTS UP.
        # The two are mutually exclusive on a single card (saturation
        # needs controls at max_raw; attenuation needs boost at min_raw
        # AND capture below 0.5 fraction); when both flags happen to be
        # set the saturation path takes precedence (more conservative —
        # never deliberately drives a control toward clipping).
        if target.saturation_warning:
            controls_to_reset = [c for c in target.controls if c.saturation_risk]
            if not controls_to_reset:
                msg = (
                    "target card has saturation_warning but no individual control "
                    "flagged saturation_risk — probe/apply classification drift"
                )
                raise BypassApplyError(msg, reason=_APPLY_REASON_NO_SATURATED)
            snapshot = await apply_mixer_reset(
                card_index=target.card_index,
                controls_to_reset=controls_to_reset,
                tuning=tuning,
            )
            outcome = "mixer_reset_applied"
        else:
            # Attenuation regime — lift capture+boost controls.
            controls_to_boost = [
                c
                for c in target.controls
                if "capture" in c.name.lower() or "boost" in c.name.lower()
            ]
            if not controls_to_boost:
                msg = (
                    "target card flagged attenuation but no capture/boost controls "
                    "to lift — probe/apply classification drift"
                )
                raise BypassApplyError(msg, reason=_APPLY_REASON_NO_ATTENUATED)
            snapshot = await apply_mixer_boost_up(
                card_index=target.card_index,
                controls_to_boost=controls_to_boost,
                tuning=tuning,
            )
            outcome = "mixer_boost_up_applied"

        self._applied_snapshot = snapshot
        # v0.22.4 safety — re-probe after apply to confirm we did not
        # flip the regime. Pilot evidence (VAIO VJFE69F11X, 2026-04-25):
        # the first attenuation-fix defaults (0.75/0.66) lifted the
        # attenuated controls past the saturation_ratio_ceiling (0.5),
        # so the next preflight reported saturation while the actual
        # signal was clipped — VAD still classified as deaf, just for
        # a different reason. Detect that here and roll back atomically
        # so the coordinator can mark this strategy as "applied but
        # ineffective" without leaving the mixer in a worse state.
        post_snapshots = enumerate_alsa_mixer_snapshots()
        post_target = next(
            (s for s in post_snapshots if s.card_index == target.card_index),
            None,
        )
        if (
            post_target is not None
            and post_target.saturation_warning
            and not target.saturation_warning
        ):
            logger.warning(
                "bypass_strategy_apply_overcorrected",
                strategy=_STRATEGY_NAME,
                card_index=snapshot.card_index,
                pre_regime="attenuation",
                post_regime="saturation",
                hint=(
                    "boost-up overshot saturation ceiling; rolling back. "
                    "Tune linux_mixer_*_attenuation_fix_fraction lower."
                ),
            )
            await restore_mixer_snapshot(snapshot, tuning=tuning)
            self._applied_snapshot = None
            raise BypassApplyError(
                "boost-up overshot saturation ceiling; rolled back",
                reason="apply_overcorrected_to_saturation",
            )
        logger.info(
            "bypass_strategy_apply_ok",
            strategy=_STRATEGY_NAME,
            endpoint_guid=context.endpoint_guid,
            card_index=snapshot.card_index,
            regime="saturation" if target.saturation_warning else "attenuation",
            controls_changed=[name for name, _ in snapshot.applied_controls],
            controls_count=len(snapshot.applied_controls),
        )
        return outcome

    async def revert(
        self,
        context: BypassContext,
    ) -> None:
        snapshot = self._applied_snapshot
        if snapshot is None:
            return
        tuning = _tuning_from_context()
        await restore_mixer_snapshot(snapshot, tuning=tuning)
        logger.info(
            "bypass_strategy_revert_ok",
            strategy=_STRATEGY_NAME,
            endpoint_guid=context.endpoint_guid,
            card_index=snapshot.card_index,
            controls_restored=[name for name, _ in snapshot.reverted_controls],
        )
        self._applied_snapshot = None


def _tuning_from_context() -> VoiceTuningConfig:
    """Return a fresh :class:`VoiceTuningConfig` — pulls live env overrides.

    Strategies are long-lived relative to a single coordinator session;
    re-reading the tuning each time keeps ``SOVYX_TUNING__VOICE__*``
    overrides observable without bouncing the process.
    """
    return VoiceTuningConfig()


def _match_target_card(
    *,
    faulted: list[MixerCardSnapshot],
    endpoint_friendly_name: str,
) -> MixerCardSnapshot | None:
    """Select the faulted card that most likely backs ``endpoint_friendly_name``.

    A "faulted" card is one with ``saturation_warning=True`` OR that
    matches ``_is_attenuated`` — both regimes the bypass coordinator
    can remediate via this strategy.

    Matching hierarchy:

    1. Exact substring match of ``card_id`` or any contiguous word of
       ``card_longname`` against ``endpoint_friendly_name`` (case-insensitive).
    2. Fallback when exactly one faulted card exists: use it. A
       single-card host has no ambiguity, so forcing a textual match
       against PortAudio's idiosyncratic endpoint naming would only
       fail unnecessarily.
    3. Otherwise: return ``None`` — the coordinator surfaces
       ``card_match_ambiguous`` and the strategy stays out of a
       guessing game.
    """
    if not faulted:
        return None
    name_lower = (endpoint_friendly_name or "").lower()
    if name_lower:
        # Prefer a substring match against the card_id first — it's a
        # short stable identifier (e.g. "Generic_1") that ALSA leaks
        # into PortAudio's endpoint descriptor on many distros. Fall
        # back to any word of card_longname with length ≥ 4 (avoid
        # 2–3 char filler matches like "at", "HD").
        for card in faulted:
            if card.card_id and card.card_id.lower() in name_lower:
                return card
        for card in faulted:
            for token in _tokens(card.card_longname):
                if len(token) >= 4 and token in name_lower:
                    return card
    if len(faulted) == 1:
        return faulted[0]
    return None


def _tokens(text: str) -> list[str]:
    """Split a long card name into lowercase word-ish tokens.

    Keeps alphanumeric runs; drops punctuation and bus-address noise
    (``at 0x10b8000``, ``irq 173``) that bloats the match surface
    without helping disambiguation. Stable ordering preserves the
    "first meaningful word wins" bias in :func:`_match_target_card`.
    """
    lowered = text.lower()
    tokens: list[str] = []
    buf: list[str] = []
    for ch in lowered:
        if ch.isalnum():
            buf.append(ch)
        elif buf:
            tokens.append("".join(buf))
            buf = []
    if buf:
        tokens.append("".join(buf))
    return tokens


__all__ = ["LinuxALSAMixerResetBypass"]
