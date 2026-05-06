"""R90: STT locality recommendation by hardware tier.

Pure fingerprint-driven rule (no measurement gate): the host's
``ram_mb`` + ``cpu_cores`` + ``has_gpu`` triple determines whether
local Moonshine STT will fit comfortably or whether the operator
should default to cloud STT for latency. R90 surfaces the
recommendation; the operator can override if they have offline
constraints (privacy, no network) that outweigh the latency cost.

Trigger:

The rule always evaluates to "applies" once the fingerprint has been
captured -- there is no measurement gate. The decision branch picks
either ``local`` or ``cloud`` based on:

* ``ram_mb >= 4096 AND cpu_cores >= 4`` -> ``local`` (Moonshine fits)
* otherwise -> ``cloud`` (Pi 5 / N100 / older Atom)

Why ADVISE, not SET (in v0.30.21):

STT locality is a privacy + latency trade-off the operator owns.
v0.30.21 surfaces the recommendation as a calibration nudge; the
operator chooses. SET-promotion would assume the operator wants the
recommendation auto-applied, which doesn't respect the privacy
preference. Permanent shape is ADVISE.

Priority 35 (low): well below all destroyed-input + tuning rules.
R90 is a recommendation, not a fix.

History: introduced in v0.30.21 as T2.6.R90 of mission
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

_LOCAL_RAM_THRESHOLD_MB = 4096
_LOCAL_CPU_CORES_THRESHOLD = 4


class _Rule:
    rule_id = "R90_stt_locality"
    rule_version = 1
    priority = 35
    description = (
        "STT locality recommendation: local Moonshine when host can "
        "afford it; cloud STT for resource-constrained hosts."
    )

    def applies(self, ctx: RuleContext) -> bool:
        # R90 always emits a recommendation -- there's always either a
        # "use local" or "use cloud" verdict to surface for the
        # operator. The fingerprint must just be present.
        _ = ctx
        return True

    def evaluate(self, ctx: RuleContext) -> RuleEvaluation:
        fp = ctx.fingerprint
        local_capable = (
            fp.ram_mb >= _LOCAL_RAM_THRESHOLD_MB and fp.cpu_cores >= _LOCAL_CPU_CORES_THRESHOLD
        )

        if local_capable:
            value = (
                "Use local STT (Moonshine v2): set "
                "`SOVYX_VOICE__STT_PROVIDER=moonshine` (default in fresh "
                "installs). Your host has the resources to run it locally "
                "with sub-second latency + zero network dependency."
            )
            rationale = (
                f"Host meets local-STT thresholds: ram_mb={fp.ram_mb} >= "
                f"{_LOCAL_RAM_THRESHOLD_MB} AND cpu_cores={fp.cpu_cores} >= "
                f"{_LOCAL_CPU_CORES_THRESHOLD}. Moonshine v2 ONNX inference "
                f"fits comfortably; no network round-trip needed."
            )
        else:
            value = (
                "Consider cloud STT (OpenAI Whisper API or equivalent): set "
                "`SOVYX_VOICE__STT_PROVIDER=cloud` and configure your BYOK key. "
                "Your host is below the local-STT thresholds and Moonshine "
                "inference will dominate latency. Cloud STT trades privacy "
                "+ network dependency for ~200-400ms transcription times."
            )
            rationale = (
                f"Host is below local-STT thresholds: ram_mb={fp.ram_mb} < "
                f"{_LOCAL_RAM_THRESHOLD_MB} OR cpu_cores={fp.cpu_cores} < "
                f"{_LOCAL_CPU_CORES_THRESHOLD}. Local Moonshine inference "
                f"would dominate latency on this hardware tier."
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
                # LOW confidence because STT locality is operator-preference
                # heavy; the rule's recommendation is informative, not
                # prescriptive.
                confidence=CalibrationConfidence.LOW,
            ),
        )
        matched_conditions = (
            f"fingerprint.ram_mb == {fp.ram_mb}",
            f"fingerprint.cpu_cores == {fp.cpu_cores}",
            f"local_capable == {local_capable}",
        )
        return RuleEvaluation(decisions=decisions, matched_conditions=matched_conditions)


# Module-level singleton picked up by ``iter_rules()``.
rule: CalibrationRule = _Rule()
