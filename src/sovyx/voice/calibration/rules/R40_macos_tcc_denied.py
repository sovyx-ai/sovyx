"""R40: macOS TCC microphone permission denied for the host process.

STATUS AT HEAD — UNREACHABLE (W1.3 honesty pass, anti-pattern #48;
disclosure extended to R40 by MACOS-3 of
MISSION-AUDIO-ENGINE-CROSS-PLATFORM-AUDIT-2026-07-02). This rule cannot
fire from real captured data on two independent grounds:

1. ``applies()`` gates on ``fingerprint.distro_id == "macos"``, but the
   calibration fingerprint reads ``distro_id`` from ``/etc/os-release``
   (``voice/calibration/_fingerprint.py``), which does not exist on
   Darwin — the field is always ``""`` there, never ``"macos"``.
2. ``applies()`` also requires a triage winner of H5, but no Darwin
   triage producer exists — the diagnostic toolkit that feeds
   ``ctx.triage_result`` is the Linux bash toolkit, and the calibration
   engine itself only runs on Linux (``--calibrate`` is Linux-only).

It exists + is unit-tested with synthetic inputs as scaffolding; it is
NOT a live capability. It self-declares this via ``unreachable_reason``
so the ``--evaluate-rules`` preview discloses it and a future maintainer
who wires a Darwin producer (e.g. the MA2 TCC probe verdict) must remove
the marker (a test enforces the set).

Translates triage hypothesis H5 (`HypothesisId.H5_MIC_PERMISSION_DENIED`)
into a deterministic calibration rule. On macOS, the TCC (Transparency,
Consent, and Control) subsystem gates microphone access per-app via the
System Settings → Privacy & Security pane. When the operator runs Sovyx
without granting mic permission to the host shell / launcher, every
capture attempt returns silence.

Why ADVISE, not SET (in v0.30.20 + permanently):

TCC is an OS-level access gate that ONLY the user can flip via the GUI
Privacy & Security pane. Sovyx CANNOT auto-grant the permission
programmatically (by design -- that would defeat the purpose of the
consent model). R40's permanent shape is "render the GUI path the
operator must walk", not a SET decision.

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
    rule_version = 2
    priority = 70
    description = (
        "macOS TCC microphone permission denied -- advises the GUI "
        "Privacy & Security path the operator must walk."
    )
    # W1.3 / MACOS-3 — see module docstring. The distro_id gate can never
    # open (no Darwin /etc/os-release) and no Darwin triage producer
    # exists to make H5 the winner. Documented gap, not a live rule.
    unreachable_reason = (
        "fingerprint.distro_id is read from /etc/os-release (absent on Darwin, "
        "always '') so it is never 'macos', AND no Darwin triage producer exists "
        "to co-occur an H5 winner — gate can never open. Live macOS coverage of "
        "the same failure class ships via the MA2 TCC probe "
        "(voice/health/_mic_permission_mac.py) + `sovyx doctor platform`."
    )

    def applies(self, ctx: RuleContext) -> bool:
        # macOS-only: TCC is a macOS concept. Linux uses its own
        # permission models (PolicyKit, ALSA group); Windows uses
        # APO + capture endpoint default.
        # NOTE (MACOS-3, AP #52): at HEAD this gate is structurally
        # closed — capture_fingerprint reads distro_id from
        # /etc/os-release and never yields "macos" on Darwin. See the
        # module docstring + unreachable_reason.
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
                    "the launcher), and toggle the switch ON. Then run "
                    "`sovyx doctor platform` to confirm the permission verdict. "
                    "Sovyx cannot auto-grant this permission -- it's an "
                    "OS-level consent gate that requires GUI action."
                ),
                rationale=(
                    f"macOS TCC denied microphone access to the host process "
                    f"(H5 winner confidence={winner_confidence:.2f}). When this "
                    f"rule fires, capture opens cleanly but every input stream "
                    f"delivers silence — the TCC gate sits upstream of "
                    f"PortAudio, so no mixer / device / pipeline tuning can "
                    f"recover until the permission is granted."
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
