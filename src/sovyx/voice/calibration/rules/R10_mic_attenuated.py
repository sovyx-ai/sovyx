"""R10: Linux ALSA mixer attenuated below Silero VAD floor.

Translates triage hypothesis H10 (`HypothesisId.H10_LINUX_MIXER_ATTENUATED`)
into a deterministic calibration rule. When the canonical Sony VAIO
case fires (`mixer_attenuation_regime == "attenuated"` + H10 winner
confidence >= 0.7), the rule emits a HIGH-confidence ADVISE decision
pointing the operator at ``sovyx doctor voice --fix --yes``, which
already exists pre-v0.30.15 and lifts the attenuated controls via
the proven mixer KB apply path (`_linux_mixer_apply.apply_mixer_preset`).

Why ADVISE, not SET (in v0.30.15):

The applier (T2.8) doesn't yet wire calibration into mixer
remediation directly. R10 advising the operator to run ``--fix``
is the conservative bridge that proves the engine end-to-end without
introducing a new mutation path. After 7-day soak (v0.30.16-17),
R10 is a candidate for promotion to a SET decision that calls the
applier directly to skip the manual --fix step.

Priority 95 (very high): R10 represents a known root cause + known
remediation; it should fire before measurement-driven rules (R60+)
that might otherwise burn cycles tuning VAD thresholds against an
attenuated input.

History: introduced in v0.30.15 as T2.5.R10 of mission
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


class _Rule:
    rule_id = "R10_mic_attenuated"
    rule_version = 1
    priority = 95
    description = (
        "Linux ALSA mixer attenuated -- capture+boost below Silero VAD "
        "floor. Advises `sovyx doctor voice --fix --yes` to remediate."
    )

    def applies(self, ctx: RuleContext) -> bool:
        # Audio-stack gate: the bash diag's mixer signature is Linux-
        # specific (amixer + ALSA controls). PulseAudio + PipeWire
        # both expose this layer; raw ALSA-only systems do too.
        if ctx.fingerprint.audio_stack not in ("pipewire", "pulseaudio", "alsa-only"):
            return False

        # Mixer-state gate: H10 only matters when the regime is
        # "attenuated" (controls driven below the VAD floor). The
        # complementary "saturated" regime is handled by the existing
        # `--fix` path and not the calibration rule.
        if ctx.measurements.mixer_attenuation_regime != "attenuated":
            return False

        # Triage cross-check: H10 must be the highest-confidence
        # hypothesis with confidence >= 0.7. Without this gate, a
        # noise-floor measurement that happens to look attenuated
        # could fire R10 even when the true cause is elsewhere
        # (APO interceptor, destructive filter, hardware gap). The
        # 0.7 threshold matches the markdown renderer's
        # "Highest-confidence root cause" cutoff for forensic UX
        # consistency.
        if ctx.triage_result is None:
            return False
        winner = ctx.triage_result.winner
        if winner is None:
            return False
        if winner.hid != "H10":
            return False
        return winner.confidence >= 0.7

    def evaluate(self, ctx: RuleContext) -> RuleEvaluation:
        winner_confidence = (
            ctx.triage_result.winner.confidence
            if ctx.triage_result is not None and ctx.triage_result.winner is not None
            else 0.0
        )
        decisions = (
            CalibrationDecision(
                target="advice.action",
                target_class="TuningAdvice",
                operation="advise",
                value="sovyx doctor voice --fix --yes",
                rationale=(
                    f"Mixer attenuation detected (capture={ctx.measurements.mixer_capture_pct}%, "
                    f"boost={ctx.measurements.mixer_boost_pct}%, internal_mic_boost="
                    f"{ctx.measurements.mixer_internal_mic_boost_pct}%) with H10 winner "
                    f"confidence={winner_confidence:.2f}. The KB-driven mixer apply path "
                    f"(`sovyx doctor voice --fix`) lifts the attenuated controls to safe "
                    f"fractions and re-validates."
                ),
                rule_id=self.rule_id,
                rule_version=self.rule_version,
                confidence=CalibrationConfidence.HIGH,
            ),
        )
        matched_conditions = (
            f"fingerprint.audio_stack == {ctx.fingerprint.audio_stack!r}",
            f"measurements.mixer_attenuation_regime == 'attenuated' "
            f"(capture_pct={ctx.measurements.mixer_capture_pct}, "
            f"boost_pct={ctx.measurements.mixer_boost_pct})",
            f"triage_result.winner.hid == 'H10' (confidence={winner_confidence:.2f} >= 0.70)",
        )
        return RuleEvaluation(decisions=decisions, matched_conditions=matched_conditions)


# Module-level singleton picked up by ``iter_rules()``.
rule: CalibrationRule = _Rule()
