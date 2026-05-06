"""R20: Windows Voice Clarity APO destroying capture signal.

Translates triage hypothesis H2 (`HypothesisId.H2_VOICE_CLARITY_APO`)
into a deterministic calibration rule. When Windows Voice Clarity
(`VocaEffectPack` / `voiceclarityep`, shipped via Windows Update in
early 2026) registers as a per-endpoint capture APO and destroys
Silero VAD input on affected hardware (max speech probability drops
below 0.01 despite healthy RMS), this rule advises the operator to
enable WASAPI exclusive mode, which bypasses the entire APO chain.

Why ADVISE, not SET (in v0.30.20):

The wire-up to flip ``voice_clarity_autofix=True`` automatically
already exists in :mod:`sovyx.voice._apo_detector` (auto-bypass on
repeated deaf heartbeats). R20 surfaces the same remediation as a
copy-paste command for operators who hit the issue at calibration
time rather than at first deaf-heartbeat. After R20 promotes to a
SET decision (post-soak), the calibration profile would also flip
``capture_wasapi_exclusive=True`` proactively before the runtime
detector ever has to react.

Priority 80 (high): R20 represents a destroyed-input root cause -
fixing it must take precedence over measurement-driven rules (R60+)
that would otherwise tune VAD thresholds against noise.

History: introduced in v0.30.20 as T2.5.R20 of mission
``MISSION-voice-self-calibrating-system-2026-05-05.md`` Layer 2.
Fully gated on triage winner H2 with confidence >= 0.7 to prevent
false positives on clean Windows hosts.
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
    rule_id = "R20_windows_apo_active"
    rule_version = 1
    priority = 80
    description = (
        "Windows Voice Clarity APO destroys capture signal -- advises "
        "WASAPI exclusive mode to bypass the entire APO chain."
    )

    def applies(self, ctx: RuleContext) -> bool:
        # Windows-only: APO is a per-endpoint Windows audio framework
        # concept. macOS (HAL interceptor) and Linux (destructive
        # filter) have their own H3/H4 rules.
        # The fingerprint uses distro_id="windows" plus the
        # apo_active flag set by Windows Voice Clarity detection.
        if not ctx.fingerprint.apo_active:
            return False

        # Triage cross-check: H2 must be the highest-confidence
        # hypothesis with confidence >= 0.7. Without this gate, an
        # APO probe that observed *some* APO (benign vendor effect
        # plugin) would fire R20 even when Voice Clarity isn't the
        # cause. The 0.7 threshold matches R10's forensic UX.
        if ctx.triage_result is None:
            return False
        winner = ctx.triage_result.winner
        if winner is None:
            return False
        if winner.hid != "H2":
            return False
        return winner.confidence >= 0.7

    def evaluate(self, ctx: RuleContext) -> RuleEvaluation:
        winner_confidence = (
            ctx.triage_result.winner.confidence
            if ctx.triage_result is not None and ctx.triage_result.winner is not None
            else 0.0
        )
        apo_name = ctx.fingerprint.apo_name or "unknown APO"
        decisions = (
            CalibrationDecision(
                target="advice.action",
                target_class="TuningAdvice",
                operation="advise",
                value=(
                    "Enable WASAPI exclusive capture: set "
                    "`SOVYX_VOICE__CAPTURE_WASAPI_EXCLUSIVE=true` (or the "
                    "equivalent in voice settings) and restart the daemon. "
                    "This bypasses the Voice Clarity APO chain that destroys "
                    "the capture signal upstream of PortAudio."
                ),
                rationale=(
                    f"Windows capture APO {apo_name!r} detected on the input "
                    f"endpoint with H2 winner confidence={winner_confidence:.2f}. "
                    f"Voice Clarity / VocaEffectPack registers as a per-endpoint "
                    f"APO that drops VAD speech probability below 0.01 even on "
                    f"clean speech. WASAPI exclusive mode bypasses the entire "
                    f"APO chain by design."
                ),
                rule_id=self.rule_id,
                rule_version=self.rule_version,
                confidence=CalibrationConfidence.HIGH,
            ),
        )
        matched_conditions = (
            f"fingerprint.apo_active == True (apo_name={apo_name!r})",
            f"triage_result.winner.hid == 'H2' (confidence={winner_confidence:.2f} >= 0.70)",
        )
        return RuleEvaluation(decisions=decisions, matched_conditions=matched_conditions)


# Module-level singleton picked up by ``iter_rules()``.
rule: CalibrationRule = _Rule()
