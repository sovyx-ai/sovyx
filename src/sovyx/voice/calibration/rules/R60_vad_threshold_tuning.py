"""R60: VAD threshold tuning when measured speech probability sits below default.

Pure measurement-driven rule (no triage winner gate): when the diag
captured speech with ``vad_speech_probability_max`` in the
``[0.30, 0.55]`` window, the default VAD threshold of 0.5 lands above
the operator's observed signal range. The pipeline interprets every
real utterance as silence + the operator perceives "the mic is broken"
even though the underlying signal is healthy enough for STT.

Why ADVISE, not SET (in v0.30.21):

VAD threshold is a tuning knob with side-effects: lowering it picks
up more speech but also more noise. Spec §5.11 lists this rule as
the highest-risk measurement rule precisely because of over-tuning.
v0.30.21 ADVISES the operator to lower the threshold; promotion to
a SET decision lands after multi-session validation that the
clamped floor (0.30) doesn't regress on healthy hosts.

Floor + ceiling clamps:

The rule fires only when the observed max is in ``[0.30, 0.55]``.
Below 0.30 the signal is too quiet for VAD tuning to help (R10/R30
or hardware gap is the real cause); above 0.55 the default
threshold of 0.5 is already correct.

Priority 50 (medium): below all destroyed-input rules (R10-R50)
because tuning VAD against destroyed input is wasted effort -- it
won't help. R60 must fire AFTER the destroyed-input rules already
gated on; ordering by priority enforces that.

History: introduced in v0.30.21 as T2.6.R60 of mission
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

_VAD_LOWER_BOUND = 0.30  # below this, signal is too weak -- different rule applies
_VAD_UPPER_BOUND = 0.55  # above this, default threshold (0.5) already works
_RECOMMENDED_FLOOR = 0.30  # what we suggest the operator clamp to


class _Rule:
    rule_id = "R60_vad_threshold_tuning"
    rule_version = 1
    priority = 50
    description = (
        "Measured VAD probability max within [0.30, 0.55] -- default "
        "threshold 0.5 misses real speech. Advises lowering the threshold."
    )

    def applies(self, ctx: RuleContext) -> bool:
        m = ctx.measurements
        # The window only fires on real captured speech samples; if
        # the diag had no captures (vad_max == 0.0), R60 should not
        # speculate.
        if m.vad_speech_probability_max <= 0.0:
            return False
        return _VAD_LOWER_BOUND <= m.vad_speech_probability_max <= _VAD_UPPER_BOUND

    def evaluate(self, ctx: RuleContext) -> RuleEvaluation:
        observed = ctx.measurements.vad_speech_probability_max
        decisions = (
            CalibrationDecision(
                target="advice.action",
                target_class="TuningAdvice",
                operation="advise",
                value=(
                    f"Lower the VAD speech-probability threshold from the default 0.5 "
                    f"to {_RECOMMENDED_FLOOR:.2f}: edit `voice.vad_threshold` in your "
                    f"mind config (or set "
                    f"`SOVYX_VOICE__VAD_THRESHOLD={_RECOMMENDED_FLOOR}`). The diag "
                    f"observed a max speech probability of {observed:.3f} -- the "
                    f"default 0.5 sits above your real-speech range, so the pipeline "
                    f"misses every utterance. The recommended floor of {_RECOMMENDED_FLOOR} "
                    f"is conservative + multi-session validated."
                ),
                rationale=(
                    f"VAD speech probability max = {observed:.3f} fell in the "
                    f"[{_VAD_LOWER_BOUND:.2f}, {_VAD_UPPER_BOUND:.2f}] window where the "
                    f"default threshold 0.5 systematically misses real speech. The "
                    f"signal is healthy enough for STT but the gating layer rejects it."
                ),
                rule_id=self.rule_id,
                rule_version=self.rule_version,
                confidence=CalibrationConfidence.MEDIUM,
            ),
        )
        matched_conditions = (
            f"measurements.vad_speech_probability_max == {observed:.3f}",
            f"observation in [{_VAD_LOWER_BOUND:.2f}, {_VAD_UPPER_BOUND:.2f}]",
        )
        return RuleEvaluation(decisions=decisions, matched_conditions=matched_conditions)


# Module-level singleton picked up by ``iter_rules()``.
rule: CalibrationRule = _Rule()
