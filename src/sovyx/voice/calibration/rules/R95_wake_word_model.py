"""R95: wake-word model selection by hardware tier.

Pure fingerprint-driven rule: like R90, the host's tier determines
whether the operator should pick the higher-accuracy wake-word
model or the lighter one. Lower-tier hosts (Pi 5, N100, < 4 cores
or < 4 GB RAM) save CPU at the cost of slightly higher false-accept
rate; higher-tier hosts comfortably run the better model and get
~20% lower false-accept on the same threshold.

Trigger: the rule always emits a recommendation.

Why ADVISE, not SET (in v0.30.21):

Wake-word model selection has measurable false-accept / false-reject
trade-offs that depend on operator usage patterns (busy office vs
quiet home). v0.30.21 SURFACES the per-tier recommendation; the
operator picks. SET-promotion deferred until soak data + L4 fleet
KB shows the recommendation default isn't regressing per-environment.

Priority 30 (lowest): wake-word selection is a polish-tier
recommendation that should fire AFTER all destroyed-input,
threshold, and locality rules.

History: introduced in v0.30.21 as T2.6.R95 of mission
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

_HIGH_TIER_RAM_THRESHOLD_MB = 4096
_HIGH_TIER_CPU_CORES_THRESHOLD = 4


class _Rule:
    rule_id = "R95_wake_word_model"
    rule_version = 1
    priority = 30
    description = (
        "Wake-word model recommendation: heavier accurate model on "
        "higher-tier hosts; lighter model on Pi5/N100-class hosts."
    )

    def applies(self, ctx: RuleContext) -> bool:
        _ = ctx
        return True

    def evaluate(self, ctx: RuleContext) -> RuleEvaluation:
        fp = ctx.fingerprint
        high_tier = (
            fp.ram_mb >= _HIGH_TIER_RAM_THRESHOLD_MB
            and fp.cpu_cores >= _HIGH_TIER_CPU_CORES_THRESHOLD
        )

        if high_tier:
            value = (
                "Use the accurate wake-word model variant: keep "
                "`voice.wake_word_model_variant='accurate'` (default) for "
                "lower false-accept rate at slightly higher CPU cost. Your "
                "host comfortably runs it."
            )
            rationale = (
                f"High-tier host (ram_mb={fp.ram_mb} >= {_HIGH_TIER_RAM_THRESHOLD_MB}, "
                f"cpu_cores={fp.cpu_cores} >= {_HIGH_TIER_CPU_CORES_THRESHOLD}); "
                f"accurate variant gives ~20% lower false-accept rate at the "
                f"same operator-visible threshold."
            )
        else:
            value = (
                "Use the lightweight wake-word model variant: set "
                "`voice.wake_word_model_variant='light'`. Saves CPU on this "
                "host tier at a cost of slightly higher false-accept rate. "
                "Pair with `voice.wake_word_threshold` increase if false "
                "accepts become noticeable."
            )
            rationale = (
                f"Lower-tier host (ram_mb={fp.ram_mb} < {_HIGH_TIER_RAM_THRESHOLD_MB} "
                f"OR cpu_cores={fp.cpu_cores} < {_HIGH_TIER_CPU_CORES_THRESHOLD}); "
                f"the lightweight variant fits the constraint envelope while "
                f"keeping the wake-word path responsive."
            )

        decisions = (
            CalibrationDecision(
                target="advice.action",
                target_class="TuningAdvice",
                operation="advise",
                value=value,
                rationale=rationale,
                rule_id=self.rule_id,
                rule_version=self.rule_version,
                confidence=CalibrationConfidence.LOW,
            ),
        )
        matched_conditions = (
            f"fingerprint.ram_mb == {fp.ram_mb}",
            f"fingerprint.cpu_cores == {fp.cpu_cores}",
            f"high_tier == {high_tier}",
        )
        return RuleEvaluation(decisions=decisions, matched_conditions=matched_conditions)


# Module-level singleton picked up by ``iter_rules()``.
rule: CalibrationRule = _Rule()
