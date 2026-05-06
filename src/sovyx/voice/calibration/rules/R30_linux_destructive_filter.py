"""R30: Linux PulseAudio destructive filter on capture chain.

Translates triage hypothesis H4
(`HypothesisId.H4_LINUX_DESTRUCTIVE_FILTER`) into a deterministic
calibration rule. When PulseAudio (or PipeWire's pulse compatibility
layer) loads ``module-echo-cancel``, ``module-noise-reduction``, or
similar destructive filters on the capture chain, the user's mic
signal can be muted to silence at the speex/webrtc DSP layer before
PortAudio ever sees it. The bash diag's ``D_pipewire`` layer captures
the destructive-modules list; H4 fires when at least one is present
on the input chain.

Why ADVISE, not SET (in v0.30.20):

PulseAudio module unload is non-destructive but reversible across
sessions: a PA daemon restart re-loads the module. R30 advising the
operator to add a permanent ``unload`` line to ``~/.config/pulse/default.pa``
gives a durable fix; an automatic SET decision would need to write
that file (a config mutation outside the calibration profile's
target surface), which is out of scope for v0.30.20.

Priority 75 (high, just below R20): destructive-filter and APO are
both signal-destruction roots; APO is more aggressive (full chain
destroy), so R20 wins when both fire. R30 also yields to R10
(priority 95) on cross-distro mixed-cause runs.

History: introduced in v0.30.20 as T2.5.R30 of mission
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
    rule_id = "R30_linux_destructive_filter"
    rule_version = 1
    priority = 75
    description = (
        "Linux PulseAudio/PipeWire destructive filter on capture chain "
        "(echo-cancel, noise-reduction) muting the mic signal -- advises "
        "permanent module unload."
    )

    def applies(self, ctx: RuleContext) -> bool:
        # Linux audio-stack gate: pulseaudio + pipewire (which also
        # speaks the pulse module surface). Pure ALSA-only systems
        # don't have this layer.
        if ctx.fingerprint.audio_stack not in ("pipewire", "pulseaudio"):
            return False

        # Fingerprint signal: at least one destructive module loaded.
        # Without this gate the rule would fire on healthy systems
        # where H4 is the highest-confidence hypothesis but not actually
        # the cause.
        if not ctx.fingerprint.pulse_modules_destructive:
            return False

        # Triage cross-check: H4 must be the highest-confidence
        # hypothesis with confidence >= 0.7.
        if ctx.triage_result is None:
            return False
        winner = ctx.triage_result.winner
        if winner is None:
            return False
        if winner.hid != "H4":
            return False
        return winner.confidence >= 0.7

    def evaluate(self, ctx: RuleContext) -> RuleEvaluation:
        winner_confidence = (
            ctx.triage_result.winner.confidence
            if ctx.triage_result is not None and ctx.triage_result.winner is not None
            else 0.0
        )
        modules = ctx.fingerprint.pulse_modules_destructive
        modules_str = ", ".join(modules) if modules else "(unknown)"
        first_module = modules[0] if modules else "module-echo-cancel"
        unload_pattern = modules_str.replace(", ", "|")
        decisions = (
            CalibrationDecision(
                target="advice.action",
                target_class="TuningAdvice",
                operation="advise",
                value=(
                    f"Unload destructive PulseAudio modules: run "
                    f"`pactl list short modules | grep -E '{unload_pattern}' "
                    f"| awk '{{print $1}}' | xargs -r -n1 pactl unload-module` "
                    f"to disable for the current session, AND add "
                    f"`unload-module {first_module}` to "
                    f"~/.config/pulse/default.pa to persist across daemon restarts."
                ),
                rationale=(
                    f"Detected destructive capture-chain module(s): {modules_str}. "
                    f"H4 winner confidence={winner_confidence:.2f}. These modules "
                    f"apply DSP (echo-cancel, noise-reduction) at the PulseAudio "
                    f"layer that can mute the mic to silence on certain hardware "
                    f"+ codec combinations. PortAudio sees zero PCM despite a "
                    f"healthy hardware capture stream."
                ),
                rule_id=self.rule_id,
                rule_version=self.rule_version,
                confidence=CalibrationConfidence.HIGH,
            ),
        )
        matched_conditions = (
            f"fingerprint.audio_stack == {ctx.fingerprint.audio_stack!r}",
            f"fingerprint.pulse_modules_destructive == {modules!r}",
            f"triage_result.winner.hid == 'H4' (confidence={winner_confidence:.2f} >= 0.70)",
        )
        return RuleEvaluation(decisions=decisions, matched_conditions=matched_conditions)


# Module-level singleton picked up by ``iter_rules()``.
rule: CalibrationRule = _Rule()
