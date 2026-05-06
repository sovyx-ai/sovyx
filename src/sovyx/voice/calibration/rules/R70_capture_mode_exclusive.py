"""R70: Windows capture mode -- prefer WASAPI exclusive when latency is high.

Pure measurement-driven rule (no triage winner gate): on Windows
hosts where the diag observed elevated PortAudio latency or jitter,
WASAPI exclusive mode reduces buffer contention and APO interference
even when no specific APO has been crowned by triage. R70 is
preventive rather than reactive (vs R20 which gates on confirmed H2
APO winner).

Trigger thresholds (chosen empirically per the L1 diag's
``E_portaudio`` baseline):

* ``portaudio_latency_advertised_ms > 30`` indicates the shared-mode
  buffer is paying APO+session-mixer overhead beyond a clean
  capture chain (which advertises ~10ms on healthy Win11 hosts).
* ``capture_jitter_ms > 5`` indicates the kernel is preempting the
  capture thread aggressively, also a shared-mode tell.

Either signal is sufficient to advise exclusive mode; both being
above threshold raises the recommendation's confidence (still MEDIUM
band; SET promotion deferred until soak data confirms exclusive
mode doesn't regress for operators on benign hardware).

Why ADVISE, not SET (in v0.30.21):

Exclusive mode locks the device against other applications -- which
the operator may not want. R70 SURFACES the recommendation but lets
the operator weigh the trade-off. SET-promotion lands when the
calibration profile gains an explicit "capture-grab consent" gate
(post-v0.30.21 work).

Priority 45 (medium-low): below R20 (confirmed APO=80) and the
issue-driven rules (R30=75, R40=70, R50=65) because R70 is a
preventive recommendation rather than a destroyed-input fix.

History: introduced in v0.30.21 as T2.6.R70 of mission
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

_LATENCY_THRESHOLD_MS = 30.0
_JITTER_THRESHOLD_MS = 5.0


class _Rule:
    rule_id = "R70_capture_mode_exclusive"
    rule_version = 1
    priority = 45
    description = (
        "Windows capture path with elevated latency or jitter -- advises "
        "WASAPI exclusive mode to reduce APO/session-mixer overhead."
    )

    def applies(self, ctx: RuleContext) -> bool:
        if ctx.fingerprint.distro_id != "windows":
            return False
        m = ctx.measurements
        return (
            m.portaudio_latency_advertised_ms > _LATENCY_THRESHOLD_MS
            or m.capture_jitter_ms > _JITTER_THRESHOLD_MS
        )

    def evaluate(self, ctx: RuleContext) -> RuleEvaluation:
        m = ctx.measurements
        decisions = (
            CalibrationDecision(
                target="advice.action",
                target_class="TuningAdvice",
                operation="advise",
                value=(
                    "Try WASAPI exclusive capture mode: set "
                    "`SOVYX_VOICE__CAPTURE_WASAPI_EXCLUSIVE=true` (or the "
                    "equivalent in voice settings) and restart the daemon. "
                    "Trade-off: exclusive mode locks the input device against "
                    "other applications while Sovyx is running, but eliminates "
                    "APO + session-mixer overhead that the diag observed."
                ),
                rationale=(
                    f"Windows capture chain showed elevated overhead -- "
                    f"latency={m.portaudio_latency_advertised_ms:.1f}ms "
                    f"(threshold {_LATENCY_THRESHOLD_MS:.0f}ms), "
                    f"jitter={m.capture_jitter_ms:.1f}ms "
                    f"(threshold {_JITTER_THRESHOLD_MS:.0f}ms). Exclusive "
                    f"mode bypasses the shared-mode pipeline + reduces "
                    f"the per-frame work to a single device-direct path."
                ),
                rule_id=self.rule_id,
                rule_version=self.rule_version,
                confidence=CalibrationConfidence.MEDIUM,
            ),
        )
        matched_conditions = (
            f"fingerprint.distro_id == {ctx.fingerprint.distro_id!r}",
            (
                f"latency_ms={m.portaudio_latency_advertised_ms:.1f} > "
                f"{_LATENCY_THRESHOLD_MS:.0f} OR "
                f"jitter_ms={m.capture_jitter_ms:.1f} > {_JITTER_THRESHOLD_MS:.0f}"
            ),
        )
        return RuleEvaluation(decisions=decisions, matched_conditions=matched_conditions)


# Module-level singleton picked up by ``iter_rules()``.
rule: CalibrationRule = _Rule()
