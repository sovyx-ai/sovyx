"""R80: AEC (acoustic echo cancellation) engine recommendation.

Pure measurement-driven rule: when the diag observed echo
correlation between TTS output and capture above ``-10 dB`` (a
threshold below which speakers' output is bleeding into the mic
clearly enough to drive STT into self-feedback), advise enabling
the AEC layer or stepping up to a stronger engine variant.

Trigger:

``measurements.echo_correlation_db is not None AND > -10`` -- the
``echo_correlation_db`` field is populated by the diag's K_output
+ E_portaudio cross-correlation when both layers ran in the same
session. ``None`` means the diag couldn't measure correlation
(no captures happened, or output was muted) and R80 should not
fire.

Why ADVISE, not SET (in v0.30.21):

AEC engine selection has a cost-quality trade-off (Speex AEC is
cheaper but less aggressive; WebRTC AEC is more aggressive but
costs CPU). The operator should pick based on their hardware tier;
R80 surfaces the recommendation and lets them decide. SET-promotion
deferred until soak data confirms a default engine doesn't regress
the reactive UX on lower-tier hosts.

Priority 40 (medium-low): below R70 (capture mode = 45) because AEC
is a within-pipeline layer, while exclusive mode is a substrate
choice that affects everything downstream including AEC.

History: introduced in v0.30.21 as T2.6.R80 of mission
``MISSION-voice-self-calibrating-system-2026-05-05.md`` Layer 2.
"""

from __future__ import annotations

from sovyx.voice.calibration.rules._base import (
    CalibrationRule,
    RuleContext,
    RuleEvaluation,
)
from sovyx.voice.calibration.schema import (
    CalibrationConfidence,
    CalibrationDecision,
)

_ECHO_THRESHOLD_DB = -10.0


class _Rule:
    rule_id = "R80_aec_engine"
    rule_version = 1
    priority = 40
    description = (
        "Echo correlation between TTS output and capture exceeds the "
        "self-feedback threshold -- advises enabling/upgrading AEC."
    )

    def applies(self, ctx: RuleContext) -> bool:
        echo = ctx.measurements.echo_correlation_db
        if echo is None:
            return False
        return echo > _ECHO_THRESHOLD_DB

    def evaluate(self, ctx: RuleContext) -> RuleEvaluation:
        echo = ctx.measurements.echo_correlation_db
        # Pick the recommended engine based on hardware tier: any
        # device with cpu_cores >= 4 + ram_mb >= 4096 gets WebRTC
        # AEC; lower-tier (Pi 5 / N100) gets Speex.
        fp = ctx.fingerprint
        if fp.cpu_cores >= 4 and fp.ram_mb >= 4096:
            engine = "webrtc"
            tradeoff = "more aggressive cancellation; ~30 MB extra RAM"
        else:
            engine = "speex"
            tradeoff = "lighter CPU footprint; less aggressive cancellation"

        decisions = (
            CalibrationDecision(
                target="advice.action",
                target_class="TuningAdvice",
                operation="advise",
                value=(
                    f"Enable AEC ({engine}) to cancel TTS-output echo: set "
                    f"`voice.aec_enabled=true` and `voice.aec_engine={engine!r}` "
                    f"in your mind config (or the SOVYX_VOICE__* env equivalents). "
                    f"Trade-off: {tradeoff}."
                ),
                rationale=(
                    f"Echo correlation = {echo:.1f} dB > {_ECHO_THRESHOLD_DB:.0f} dB "
                    f"threshold. TTS output is bleeding into the capture chain "
                    f"strongly enough to drive STT into self-feedback (the model "
                    f"transcribes its own playback). AEC at the {engine!r} engine "
                    f"matches the host's tier (cpu_cores={fp.cpu_cores}, "
                    f"ram_mb={fp.ram_mb})."
                ),
                rule_id=self.rule_id,
                rule_version=self.rule_version,
                confidence=CalibrationConfidence.MEDIUM,
            ),
        )
        matched_conditions = (
            f"measurements.echo_correlation_db == {echo:.1f}",
            f"observation > {_ECHO_THRESHOLD_DB:.0f} dB threshold",
            f"fingerprint hardware tier -> aec_engine={engine!r}",
        )
        return RuleEvaluation(decisions=decisions, matched_conditions=matched_conditions)


# Module-level singleton picked up by ``iter_rules()``.
rule: CalibrationRule = _Rule()
