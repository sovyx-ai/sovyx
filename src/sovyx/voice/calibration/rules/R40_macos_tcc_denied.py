"""R40: macOS TCC microphone permission denied for the host process.

Translates triage hypothesis H5 (`HypothesisId.H5_MIC_PERMISSION_DENIED`)
into a deterministic calibration rule. On macOS, the kernel's TCC
(Transparency, Consent, and Control) subsystem gates microphone
access per-app via Privacy & Security settings. When the operator
runs Sovyx without granting mic permission to the host shell /
launcher, every capture attempt returns silence + the bash diag
records ``H5`` as the highest-confidence root cause.

Why ADVISE, not SET (in v0.30.20 + permanently):

TCC is a kernel-level access gate that ONLY the user can flip via
the GUI Privacy & Security pane. Sovyx CANNOT auto-grant the
permission programmatically (by design -- that would defeat the
purpose of the consent model). R40's permanent shape is "render
the GUI path the operator must walk", not a SET decision.

Priority 70 (high): TCC denial blocks the entire pipeline; fixing
it must precede measurement-driven tuning rules. Below R20 (APO,
priority 80) because APO can theoretically be bypassed via
exclusive mode, while TCC denial cannot be bypassed at all without
operator action.

History: introduced in v0.30.20 as T2.5.R40 of mission
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
    rule_id = "R40_macos_tcc_denied"
    rule_version = 1
    priority = 70
    description = (
        "macOS TCC microphone permission denied -- advises the GUI "
        "Privacy & Security path the operator must walk."
    )

    def applies(self, ctx: RuleContext) -> bool:
        # macOS-only: TCC is a macOS kernel concept. Linux uses its
        # own permission models (PolicyKit, ALSA group); Windows
        # uses APO + capture endpoint default.
        # The fingerprint distro_id reports "macos" for the macOS
        # platform (set by capture_fingerprint on Darwin hosts).
        if ctx.fingerprint.distro_id != "macos":
            return False

        # Triage cross-check: H5 must be the highest-confidence
        # hypothesis with confidence >= 0.7.
        if ctx.triage_result is None:
            return False
        winner = ctx.triage_result.winner
        if winner is None:
            return False
        if winner.hid != "H5":
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
                value=(
                    "Grant microphone permission via macOS Privacy & Security: "
                    "open System Settings -> Privacy & Security -> Microphone, "
                    "find the application running Sovyx (Terminal, iTerm2, or "
                    "the launcher), and toggle the switch ON. Then re-run "
                    "`sovyx doctor voice --calibrate` to confirm the fix. "
                    "Sovyx cannot auto-grant this permission -- it's a "
                    "kernel-level consent gate that requires GUI action."
                ),
                rationale=(
                    f"macOS TCC denied microphone access to the host process. "
                    f"H5 winner confidence={winner_confidence:.2f}. The bash "
                    f"diag's E_portaudio probe captured silence on every "
                    f"opened input stream + the macOS HAL-side log shows "
                    f"`AVCaptureDeviceTypeBuiltInMicrophone` access denied. "
                    f"Until TCC is granted, no capture is possible regardless "
                    f"of mixer / device / pipeline state."
                ),
                rule_id=self.rule_id,
                rule_version=self.rule_version,
                confidence=CalibrationConfidence.HIGH,
            ),
        )
        matched_conditions = (
            f"fingerprint.distro_id == {ctx.fingerprint.distro_id!r}",
            f"triage_result.winner.hid == 'H5' (confidence={winner_confidence:.2f} >= 0.70)",
        )
        return RuleEvaluation(decisions=decisions, matched_conditions=matched_conditions)


# Module-level singleton picked up by ``iter_rules()``.
rule: CalibrationRule = _Rule()
